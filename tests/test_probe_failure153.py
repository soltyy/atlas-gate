import asyncio
import collections
import time
from types import SimpleNamespace

import httpx
import pytest

from atlas_gate.gate import nodes as nodes_module
from atlas_gate.gate.errors import GateError, GateFreshNodeError
from .test_probe_isolation144 import info, node_pool


@pytest.mark.asyncio
async def test_fast_probe_outage_is_shared_per_node_and_recovers_on_next_read(tmp_path, monkeypatch):
    nodes = await node_pool(tmp_path)
    clock = [100.0]
    monkeypatch.setattr(nodes_module, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    offline = {'codex', 'claude'}
    probes = collections.Counter()
    reads = []
    errors = []

    def respond(request):
        node = request.url.host
        if request.url.path == '/v1/node':
            probes[node] += 1
            if node in offline:
                raise httpx.ConnectError('private endpoint and credentials', request=request)
            return httpx.Response(200, json=info(node))
        reads.append((node, request.method, request.url.params['after'], request.headers['X-Atlas-Boot']))
        return httpx.Response(200, json={'events': []})

    async def read(node):
        return await nodes.request(node, 'GET', '/v1/sessions/private-session/turns/t/events',
                                   expected_boot='boot-' + node, params={'after': 17})

    for node in nodes.config:
        nodes.clients[node] = httpx.AsyncClient(base_url='http://' + node, transport=httpx.MockTransport(respond))
        nodes.snapshots[node] = dict(info(node), observed=0)
    try:
        # 62 Codex and 36 Claude callers spread over the incident's 2.377929 s.
        for n in range(98):
            clock[0] = 100 + n * 2.377929 / 97
            node = 'claude' if (n + 1) * 36 // 98 != n * 36 // 98 else 'codex'
            with pytest.raises(GateFreshNodeError) as failure:
                await read(node)
            errors.append(failure.value)
        assert probes == {'codex': 3, 'claude': 3}
        assert reads == [] and nodes.snapshots == {}
        assert len({e.extra['correlationId'] for e in errors}) == 6
        assert all(e.retryable and e.reason == 'identity_unconfirmed'
                   and e.extra['reason'] == 'transport_unavailable'
                   and e.extra['phase'] == 'probe' and e.extra['cause'] == 'ConnectError'
                   and 'private' not in str(e.extra) for e in errors)
        offline.remove('codex')
        clock[0] += 1.1
        assert (await read('codex')).status_code == 200
        assert reads == [('codex', 'GET', '17', 'boot-codex')]
        assert not nodes.probe_retryable('codex')
        assert nodes.probe_retryable('claude')
        assert probes == {'codex': 4, 'claude': 3}
    finally:
        await nodes.close()


@pytest.mark.asyncio
async def test_verified_wrong_identity_cannot_be_treated_as_transport_outage(tmp_path):
    nodes = await node_pool(tmp_path)
    seen = []

    def wrong_identity(request):
        seen.append(request.url.path)
        return httpx.Response(200, json=info('other-node'))

    nodes.clients['codex'] = httpx.AsyncClient(base_url='http://node', transport=httpx.MockTransport(wrong_identity))
    try:
        with pytest.raises(GateFreshNodeError) as failure:
            await nodes.request('codex', 'GET', '/private-path', expected_boot='original')
        assert seen == ['/v1/node']
        assert failure.value.extra['reason'] == 'node_id_mismatch'
        assert not failure.value.retryable and not nodes.snapshots
    finally:
        await nodes.close()


@pytest.mark.asyncio
async def test_transport_loss_after_passing_probe_keeps_mutation_outcome_unknown(tmp_path):
    nodes = await node_pool(tmp_path)
    seen = []

    def lose_ack(request):
        seen.append((request.method, request.url.path, request.headers.get('X-Atlas-Boot')))
        if request.url.path == '/v1/node':
            return httpx.Response(200, json=info('codex'))
        raise httpx.ReadError('private transport diagnostic', request=request)

    nodes.clients['codex'] = httpx.AsyncClient(base_url='http://node', transport=httpx.MockTransport(lose_ack))
    try:
        with pytest.raises(GateError) as failure:
            await nodes.request('codex', 'POST', '/v1/sessions/s/prompt', expected_boot='original', request_id='key')
        assert seen == [('GET', '/v1/node', None), ('POST', '/v1/sessions/s/prompt', 'original')]
        assert failure.value.code == 'command_outcome_unknown'
        assert failure.value.extra['phase'] == 'request' and failure.value.extra['cause'] == 'ReadError'
        assert 'private' not in str(failure.value.extra)
    finally:
        await nodes.close()


@pytest.mark.asyncio
async def test_revocation_while_probe_in_flight_blocks_all_observers(tmp_path, monkeypatch):
    nodes = await node_pool(tmp_path)
    revoked = set()
    monkeypatch.setattr(nodes, 'revoked', revoked.__contains__)
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def pause_probe(request):
        seen.append(request.url.path)
        entered.set()
        await release.wait()
        return httpx.Response(200, json=info('codex'))

    nodes.clients['codex'] = httpx.AsyncClient(base_url='http://node', transport=httpx.MockTransport(pause_probe))
    calls = [asyncio.create_task(nodes.request('codex', 'GET', '/private-path', expected_boot='original'))
             for _ in range(4)]
    try:
        await entered.wait()
        revoked.add('codex')
        release.set()
        errors = await asyncio.gather(*calls, return_exceptions=True)
        assert all(isinstance(e, GateFreshNodeError) and e.reason == 'node_unavailable'
                   and not e.retryable for e in errors)
        assert seen == ['/v1/node'] and not nodes.snapshots
    finally:
        release.set()
        await asyncio.gather(*calls, return_exceptions=True)
        await nodes.close()
