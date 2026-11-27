import asyncio

import httpx
import pytest

from atlas_gate.gate.errors import GateError
from .test_probe_isolation144 import info, node_pool


KEY = '/v1/commands/test-receipt'
IDENTITY = '/v1/node/identity'


def identity(boot='boot-codex'):
    return {'nodeId': 'codex', 'bootId': boot, 'protocolVersion': 1}


@pytest.mark.asyncio
async def test_receipt_reads_while_sdk_probe_is_still_blocked(tmp_path):
    nodes = await node_pool(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def respond(req):
        calls.append((req.method, req.url.path))
        assert req.headers['X-Atlas-Token'] == 'test'
        if req.url.path == '/v1/node':
            entered.set()
            await release.wait()
            return httpx.Response(200, json=info('codex'))
        assert req.headers['X-Atlas-Node'] == 'codex'
        if req.url.path == IDENTITY:
            return httpx.Response(200, json=identity())
        assert req.url.path == KEY and req.headers['X-Atlas-Boot'] == 'boot-codex'
        return httpx.Response(200, json={'state':'complete','requestId':'test-receipt'})

    nodes.clients['codex'] = httpx.AsyncClient(base_url='http://router', headers={'X-Atlas-Token':'test'},
                                               transport=httpx.MockTransport(respond))
    sdk = asyncio.create_task(nodes.probe('codex'))
    try:
        await entered.wait()
        saved = await asyncio.wait_for(nodes.request('codex', 'GET', KEY), 1)
        assert saved.json()['state'] == 'complete' and not sdk.done()
        assert calls == [('GET', '/v1/node'), ('GET', IDENTITY), ('GET', KEY)]
        assert not nodes.snapshots and not nodes.catalog
    finally:
        release.set()
        await sdk
        await nodes.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('status,body', [
    (409, {'detail':'router_identity_mismatch'}), (401, {}), (403, {}), (503, {}),
    (404, {'detail':'other error'}), (200, dict(identity(), nodeId='claude')),
    (200, identity('wrong-boot')), (200, dict(identity(), protocolVersion=True)),
])
async def test_invalid_identity_never_reads_journal_or_falls_back(tmp_path, status, body):
    nodes = await node_pool(tmp_path)
    called = []
    def respond(req):
        called.append(req.url.path)
        return httpx.Response(status, json=body)
    nodes.clients['codex'] = httpx.AsyncClient(base_url='http://router', transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(GateError):
            await nodes.request('codex', 'GET', KEY, expected_boot='boot-codex')
        assert called == [IDENTITY]
    finally:
        await nodes.close()


@pytest.mark.asyncio
async def test_missing_receipt_on_changed_boot_cannot_prove_absence(tmp_path):
    nodes = await node_pool(tmp_path)
    called = []
    def respond(req):
        called.append((req.url.path, req.headers.get('X-Atlas-Boot')))
        if req.url.path == KEY:
            return httpx.Response(404, json={'detail': 'command_not_found'})
        return httpx.Response(200, json=identity('changed' if len(called) > 1 else 'original'))
    nodes.clients['codex'] = httpx.AsyncClient(base_url='http://router', transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(GateError):
            await nodes.request('codex', 'GET', KEY)
        assert called == [(IDENTITY, None), (KEY, 'original'), (IDENTITY, 'original')]
    finally:
        await nodes.close()


@pytest.mark.asyncio
async def test_legacy_router_with_only_full_identity_remains_supported(tmp_path):
    nodes = await node_pool(tmp_path)
    called = []
    def respond(req):
        called.append(req.url.path)
        if req.url.path == IDENTITY:
            return httpx.Response(404, json={'detail':'Not Found'})
        if req.url.path == '/v1/node':
            return httpx.Response(200, json=info('codex'))
        assert req.headers['X-Atlas-Boot'] == 'boot-codex'
        return httpx.Response(200, json={'state':'complete','requestId':'test-receipt'})
    nodes.clients['codex'] = httpx.AsyncClient(base_url='http://router', transport=httpx.MockTransport(respond))
    try:
        assert (await nodes.request('codex','GET',KEY)).status_code == 200
        assert called == [IDENTITY, '/v1/node', KEY]
    finally:
        await nodes.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('when', ['identity', 'receipt'])
async def test_revocation_during_get_blocks_confirmation(tmp_path, monkeypatch, when):
    nodes = await node_pool(tmp_path)
    revoked = set()
    monkeypatch.setattr(nodes, 'revoked', revoked.__contains__)
    visits = []
    def respond(req):
        visits.append(req.url.path)
        at = 'identity' if req.url.path == IDENTITY else 'receipt'
        if at == when:
            revoked.add('codex')
        return httpx.Response(200, json=identity() if at == 'identity' else {'state': 'complete'})
    nodes.clients['codex'] = httpx.AsyncClient(base_url='http://router', transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(GateError):
            await nodes.request('codex', 'GET', KEY)
        assert visits == ([IDENTITY] if when == 'identity' else [IDENTITY, KEY])
    finally:
        await nodes.close()
