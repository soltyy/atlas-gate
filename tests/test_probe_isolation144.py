import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from atlas_gate.gate.nodes import Nodes
from atlas_gate.gate.routing import Routing
from atlas_gate.gate.store import Store
from atlas_gate.gate.errors import GateError


async def node_pool(tmp_path):
    path = tmp_path / 'nodes.json'
    path.write_text(json.dumps([{'node_id': n, 'url': 'http://127.0.0.1:1',
        'token_env': 'TEST', 'orgs': ['org'], 'routes': [n], 'account_group': n}
        for n in ('codex', 'claude')]))
    nodes = Nodes(str(path), tokens={'TEST': 'test-only'})
    for client in nodes.clients.values():
        await client.aclose()
    return nodes


def info(node):
    return {'nodeId': node, 'bootId': 'boot-' + node, 'protocolVersion': 1,
        'models': [{'value': node}], 'capacity': 20, 'ready': True,
        'capabilities': ['durable_commands', 'durable_events']}


@pytest.mark.asyncio
async def test_concurrent_probe_shares_one_request_and_waiter_cancel_is_local(tmp_path):
    nodes = await node_pool(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def handler(request):
        calls.append(request.url.path)
        entered.set()
        await release.wait()
        return httpx.Response(200, json=info('claude'))
    nodes.clients['claude'] = httpx.AsyncClient(base_url='http://node', transport=httpx.MockTransport(handler))
    tasks = [asyncio.create_task(nodes.probe('claude')) for _ in range(4)]
    try:
        await entered.wait()
        await asyncio.sleep(0)
        tasks[0].cancel()
        await asyncio.gather(tasks[0], return_exceptions=True)
        release.set()
        results = await asyncio.gather(*tasks[1:])
        assert all(r['bootId'] == 'boot-claude' for r in results)
        assert calls == ['/v1/node']
        # A later explicit probe still verifies the current boot, not a stale cache.
        assert await nodes.probe('claude')
        assert len(calls) == 2
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await nodes.close()


@pytest.mark.asyncio
async def test_probe_failure_never_promotes_old_snapshot_to_fresh(tmp_path):
    nodes = await node_pool(tmp_path)
    nodes.snapshots['codex'] = dict(info('codex'), observed=time.monotonic() - 100)
    async def handler(request):
        raise httpx.ReadTimeout('test outage', request=request)
    nodes.clients['codex'] = httpx.AsyncClient(base_url='http://node', transport=httpx.MockTransport(handler))
    try:
        assert await nodes.probe('codex') is None
        assert 'codex' not in nodes.snapshots
        assert nodes.probe_retryable('codex')
    finally:
        await nodes.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('slow,healthy', [('claude', 'codex'), ('codex', 'claude')])
async def test_create_does_not_hold_global_reservation_lock_during_other_probe(tmp_path, slow, healthy):
    nodes = await node_pool(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    sent = []
    def handler_for(node):
        async def handler(request):
            if request.url.path == '/v1/node':
                if node == slow:
                    entered.set()
                    await release.wait()
                return httpx.Response(200, json=info(node))
            assert request.method == 'POST' and request.url.path == '/v1/sessions'
            sent.append((node, request.headers.get('Idempotency-Key')))
            return httpx.Response(200, json={'id': 'local-' + node, 'tools': 0})
        return handler
    for node in nodes.config:
        nodes.clients[node] = httpx.AsyncClient(base_url='http://node', transport=httpx.MockTransport(handler_for(node)))
    store = Store(str(tmp_path / 'gate.db'))
    routing = Routing(store, nodes)
    device = SimpleNamespace(device_id='same-device', org='org', user_id='owner')
    blocked = asyncio.create_task(routing.create(device, slow, slow, {}, 20, request_id='slow'))
    try:
        await entered.wait()
        status, body = await asyncio.wait_for(routing.create(device, healthy, healthy, {}, 20,
            request_id='healthy'), 1)
        assert status == 200 and not blocked.done()
        # Replay observes the same CREATE receipt and cannot dispatch a second writer.
        assert await routing.create(device, healthy, healthy, {}, 20, request_id='healthy') == (status, body)
        assert [n for n, _ in sent] == [healthy]
        assert json.loads(body)['id']
    finally:
        release.set()
        await asyncio.gather(blocked, return_exceptions=True)
        await nodes.close()
        store.close()


@pytest.mark.asyncio
async def test_observed_boot_change_fences_steer_before_late_old_discovery():
    from atlas_gate.gate.steering import verify_target
    caps = {'version': 1, 'expectedTurnId': True, 'clientMessageId': True,
        'durableAbsorption': True, 'delivery': 'next_boundary',
        'requiredHeader': 'X-Atlas-Steer-Contract', 'targetNodeId': 'a',
        'targetBootId': 'original', 'attachments': {'images': False, 'documents': False}}
    original = {'bootId': 'original', 'observed': time.monotonic(), 'steering': caps}
    nodes = SimpleNamespace(snapshots={'a': original})
    reads = []
    async def request(node, method, path, **kwargs):
        reads.append((method, path))
        nodes.snapshots[node] = dict(original, bootId='new-boot')
        # Another request has already read the previous boot; its late reply
        # arrives on the next event-loop tick, before the metadata retry.
        asyncio.get_running_loop().call_soon(nodes.snapshots.__setitem__, node, original)
        try:
            raise httpx.RemoteProtocolError('lost metadata reply')
        except httpx.RemoteProtocolError as failed:
            raise GateError(503, 'node_unavailable', 'link lost') from failed
    nodes.request = request
    with pytest.raises(GateError) as refusal:
        await verify_target(nodes, {'node_id': 'a', 'boot_id': 'original', 'local_id': 's'},
                            'original', SimpleNamespace(attachments=[]))
    assert refusal.value.status == 409 and refusal.value.code == 'steer_target_boot_mismatch'
    await asyncio.sleep(0)
    assert nodes.snapshots['a']['bootId'] == 'original'
    assert len(reads) == 1 and reads[0][0] == 'GET'


@pytest.mark.asyncio
async def test_slow_resume_check_does_not_block_another_chat_on_same_router(tmp_path):
    nodes = await node_pool(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    serial = 0
    async def handler(request):
        nonlocal serial
        if request.url.path == '/v1/node':
            return httpx.Response(200, json=info('claude'))
        if request.url.path.endswith('/reconcile'):
            entered.set()
            await release.wait()
            return httpx.Response(200, json={'id': 'local-1', 'nodeId': 'claude',
                'bootId': 'boot-claude', 'sdkSessionId': 'native-first',
                'sessionRecovery': {'busy': False}})
        assert request.method == 'POST'
        serial += 1
        body = json.loads(request.content)
        return httpx.Response(200, json={'id': 'local-' + str(serial), 'tools': 0,
            'resumed': bool(body.get('resumeSessionId'))})
    nodes.clients['claude'] = httpx.AsyncClient(base_url='http://node', transport=httpx.MockTransport(handler))
    store = Store(str(tmp_path / 'gate.db'))
    routing = Routing(store, nodes)
    device = SimpleNamespace(device_id='same-device', org='org', user_id='owner')
    async def forbidden_device_scan(*args):
        raise AssertionError('ordinary CREATE must not scan every other chat')
    routing.sync_bindings = forbidden_device_scan
    blocked = None
    try:
        _, raw = await routing.create(device, 'claude', 'claude', {}, 20, request_id='first')
        first = routing.get(json.loads(raw)['id'], device)
        store._exec('UPDATE router_bindings SET sdk_id=? WHERE id=?', ('native-first', first['id']))
        blocked = asyncio.create_task(routing.create(device, 'claude', 'claude',
            {'resumeSessionId': first['sdk_alias']}, 20, request_id='resume'))
        await entered.wait()
        status, _ = await asyncio.wait_for(routing.create(device, 'claude', 'claude', {},
            20, request_id='another-chat'), 1)
        assert status == 200 and not blocked.done()
    finally:
        release.set()
        if blocked:
            await asyncio.gather(blocked, return_exceptions=True)
        await nodes.close()
        store.close()
