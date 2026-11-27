"""Regression for a subscription chat stuck before its first prompt on POST /tools."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from atlas_gate.gate.agent import _deliver_operation
from atlas_gate.gate.errors import GateError, GateFreshNodeError
from atlas_gate.gate.operations import Operations
from atlas_gate.gate.routing import Routing
from atlas_gate.gate.store import Store
from .test_network_gate import create, network
from .test_routing import FakeNodes, device


LEGACY = {'error': {'code': 'node_unavailable', 'message': 'идентичность Router не подтверждена',
                    'deliveryState': 'not_sent'}}
BODY = b'{"tools": []}'


@pytest.mark.parametrize('receipt', ['missing', 'already_applied', 'unavailable', 'changed_boot'])
async def test_saved_045_tools_registration_checks_its_own_receipt(tmp_path, monkeypatch, receipt):
    store = Store(str(tmp_path / 'gate.db'))
    nodes = FakeNodes()
    owner = device('owner')
    routing = Routing(store, nodes)
    _, ack = await routing.create(owner, 'sub', 'claude', {}, 10)
    binding = routing.get(json.loads(ack)['id'], owner)
    path = '/v1/sessions/' + binding['local_id'] + '/tools'
    operations = Operations(store)
    op, _ = operations.begin(owner, binding, 'POST', path, json.loads(BODY), 'registration')
    operations.complete(op['id'], httpx.Response(503, json=LEGACY))
    code = 200
    result = {'tools': 0}
    checks, sends = [], []

    async def probe(node_id):
        assert node_id == binding['node_id']
        return {'bootId': 'new' if receipt == 'changed_boot' else binding['boot_id'],
                'capabilities': ['durable_commands']}

    async def request(node_id, method, url, **options):
        assert node_id == binding['node_id']
        if method == 'GET':
            assert url == '/v1/commands/' + op['id']
            checks.append(url)
            if receipt == 'already_applied':
                return httpx.Response(200, json={'requestId': op['id'], 'state': 'complete',
                                                  'status': code, 'response': result})
            return httpx.Response(503 if receipt == 'unavailable' else 404)
        assert method == 'POST' and url == path
        sends.append((options['request_id'], options['content'], options['expected_boot']))
        return httpx.Response(code, json=result)

    monkeypatch.setattr(nodes, 'request', request)
    monkeypatch.setattr(nodes, 'probe', probe, raising=False)
    state = SimpleNamespace(operations=operations, store=store, nodes=nodes)
    try:
        async def send():
            saved, fresh = operations.begin(owner, binding, 'POST', path, json.loads(BODY), 'registration')
            assert saved['id'] == op['id'] and not fresh
            return await _deliver_operation(state, binding, saved, fresh, BODY, {})

        if receipt in ('unavailable', 'changed_boot'):
            with pytest.raises(GateError) as pending:
                await send()
            assert pending.value.code == 'command_outcome_unknown'
            assert checks and not sends
        else:
            replies = await asyncio.gather(*(send() for _ in range(4)))
            assert [r.status_code for r in replies] == [200] * 4
            assert all(r.json() == result for r in replies)
            assert len(checks) == 1
            assert sends == ([(op['id'], BODY, binding['boot_id'])] if receipt == 'missing' else [])
            saved = store._one('SELECT * FROM router_operations WHERE id=?', (op['id'],))
            assert saved['state'] == 'complete' and saved['status'] == 200
    finally:
        store.close()


@pytest.mark.parametrize('retryable', [True, False])
async def test_fresh_tools_registration_respects_pre_dispatch_proof(network, monkeypatch, retryable):
    client, app, *_ = network
    sid = await create(client)
    state = app.state.gate
    binding = state.store._one('SELECT * FROM router_bindings WHERE id=?', (sid,))
    prefix = '/harness/agent/sessions/' + sid
    headers = {'Idempotency-Key': 'registration', 'Content-Type': 'application/json'}
    original = state.nodes.request
    blocked = True
    sends = []

    async def observe(node, method, path, **kwargs):
        if method == 'POST' and path.endswith('/tools'):
            if blocked:
                raise GateFreshNodeError('идентичность Router не подтверждена', node_id=node,
                                         method=method, path=path, reason='identity_unconfirmed',
                                         retryable=retryable)
            sends.append((kwargs['request_id'], kwargs['content']))
        return await original(node, method, path, **kwargs)

    monkeypatch.setattr(state.nodes, 'request', observe)
    first = await client.post(prefix + '/tools', content=BODY, headers=headers)
    assert first.status_code == 503 and sends == []
    blocked = False
    stored = state.store._one('SELECT * FROM router_operations WHERE session_id=?', (sid,))
    assert stored['boot_id'] == binding['boot_id']
    different = await client.post(prefix + '/tools', headers=headers, json={'tools': [
        {'name': 'other', 'description': 'different', 'parameters': {'type': 'object'}}]})
    assert different.status_code == 409 and sends == []
    receipt = await client.get(prefix + '/commands', params={'requestId': 'registration'})
    if retryable:
        assert receipt.headers.get('X-Atlas-Command-State') != 'complete'
        with monkeypatch.context() as revoked:
            revoked.setattr(state.nodes, 'revoked', lambda node: True)
            rejected = await client.post(prefix + '/tools', content=BODY, headers=headers)
            assert rejected.status_code >= 400 and sends == []
        responses = await asyncio.gather(*(client.post(prefix + '/tools', content=BODY, headers=headers)
                                           for _ in range(4)))
        assert all(r.status_code == 200 for r in responses)
        assert {r.headers['X-Atlas-Command-Id'] for r in responses} == {stored['id']}
        assert sends == [(stored['id'], BODY)]
    else:
        assert receipt.status_code == 503
        assert receipt.headers['X-Atlas-Command-State'] == 'complete'
        again = await client.post(prefix + '/tools', content=BODY, headers=headers)
        assert again.content == first.content and sends == []
