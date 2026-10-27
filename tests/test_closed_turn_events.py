"""Gate #71 / Harness #372: lost local ACK must never replay business work."""
import hashlib
import base64
import io
import asyncio

import pytest

from .test_network_gate import network, create, events  # noqa: F401


async def completed_closed(network, monkeypatch, count=2):
    from atlas_router.backends.stub import StubSession
    client, app, routers, *_ = network
    calls = []

    async def prompt(self, turn_id, text, attachments=None):
        calls.append(turn_id)
        for i in range(count):
            yield {'type': 'delta', 'id': turn_id, 'text': str(i)}
        yield {'type': 'media_artifact', 'id': turn_id, 'artifact': {'id': 'a'*64, 'turnId': turn_id}}
        yield {'type': 'result', 'id': turn_id, 'ok': True, 'text': 'original complete answer',
               'usage': {'input_tokens': 7, 'output_tokens': 5}}

    monkeypatch.setattr(StubSession, 'prompt', prompt)
    sid = await create(client)
    prefix = '/harness/agent/sessions/' + sid
    r = await client.post(prefix+'/prompt?mode=async', json={'id': 'original', 'text': 'original'})
    assert r.status_code == 202, r.text
    original, after = [], 0
    for _ in range(100):
        r = await client.get(prefix+'/turns/original/events', params={'after': after, 'wait': 100})
        assert r.status_code == 200, r.text
        page = r.json(); original.extend(page['events']); after = page['nextAfter']
        if page['done']:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError('original turn did not finish')
    assert (await client.delete(prefix)).status_code == 200
    binding = app.state.gate.store._one('SELECT * FROM router_bindings WHERE id=?', (sid,))
    node = routers[0 if binding['node_id'] == 'a' else 1]
    assert binding['status'] == 'closed'
    assert node.state.registry.get(binding['local_id']) is None
    return sid, prefix, original, binding, node, calls


async def test_closed_exact_turn_pagination_replay_usage_and_no_dispatch(network, monkeypatch):
    client, app, *_ = network
    sid, prefix, original, binding, node, calls = await completed_closed(network, monkeypatch, 600)
    store = app.state.gate.store
    before = store._all('SELECT * FROM usage')
    before_receipts = store._all('SELECT * FROM router_usage_receipts')
    upstream = app.state.gate.nodes.clients[binding['node_id']]
    request = upstream.request
    methods = []

    async def reads_only(method, path, **kwargs):
        methods.append(method)
        assert method == 'GET', (method, path)
        return await request(method, path, **kwargs)

    monkeypatch.setattr(upstream, 'request', reads_only)
    for _ in range(2):
        after, received, pages = 0, [], 0
        while True:
            response = await client.get(prefix+'/turns/original/events', params={'after': after, 'wait': 0})
            assert response.status_code == 200, response.text
            d = response.json(); pages += 1
            assert d['turnId'] == 'original'
            received += d['events']
            assert all(e['seq'] > after for e in d['events'])
            assert d['nextAfter'] > after
            after = d['nextAfter']
            if d['done']:
                assert d['state'] == 'done'
                break
        assert pages >= 2
        assert received == original
        assert received[-1]['text'] == 'original complete answer'
        assert received[-1]['usage'] == {'input_tokens': 7, 'output_tokens': 5}
        assert any(e['type'] == 'media_artifact' for e in received)
        tail = (await client.get(prefix+'/turns/original/events', params={'after': after})).json()
        assert tail['done'] and tail['events'] == [] and tail['nextAfter'] == after
    assert calls == ['original']
    assert methods and set(methods) == {'GET'}
    assert store._all('SELECT * FROM usage') == before
    assert store._all('SELECT * FROM router_usage_receipts') == before_receipts
    assert store._one('SELECT status FROM router_bindings WHERE id=?', (sid,))['status'] == 'closed'


async def test_closed_turn_owner_org_turn_and_mutation_guards(network, monkeypatch):
    client, app, routers, settings, creds, other = network
    sid, prefix, *_ = await completed_closed(network, monkeypatch)
    response = await client.get(prefix+'/turns/original/events', headers={'Authorization': 'Bearer '+other['device_token']})
    assert response.status_code == 404
    state = app.state.gate
    state.store._exec('UPDATE devices SET org=? WHERE device_id=?', ('foreign-org', creds['device_id']))
    assert (await client.get(prefix+'/turns/original/events')).status_code == 404
    state.store._exec('UPDATE devices SET org=? WHERE device_id=?', ('test-org', creds['device_id']))
    assert (await client.get(prefix+'/turns/not-this-turn/events')).status_code == 404
    # A real turn under another local session is still not accessible through this binding.
    another = await create(client)
    assert (await client.get('/harness/agent/sessions/'+another+'/turns/original/events')).status_code == 404
    for suffix, body in [('/prompt?mode=async', {'id': 'new', 'text': 'never'}),
                         ('/tool_result', {'callId': 'old', 'content': 'never'}),
                         ('/interrupt', {}), ('/compact', {})]:
        assert (await client.post(prefix+suffix, json=body)).status_code == 404
    assert (await client.get(prefix+'/turns/original/events')).status_code == 200


@pytest.mark.parametrize('expired', [False, True])
async def test_closed_turn_missing_or_expired_never_claims_success(network, monkeypatch, expired):
    client, app, *_ = network
    sid, prefix, original, binding, node, calls = await completed_closed(network, monkeypatch)
    if expired:
        node.state.journal.purge(-1)
    path = prefix+'/turns/'+('original' if expired else 'missing')+'/events'
    before = app.state.gate.store._all('SELECT * FROM usage')
    for _ in range(2):
        response = await client.get(path)
        assert response.status_code == (410 if expired else 404), response.text
        assert 'events' not in response.json() and 'done' not in response.json()
    assert app.state.gate.store._all('SELECT * FROM usage') == before
    assert calls == ['original']


@pytest.mark.parametrize('guard', ['recover', 'wrong_sdk', 'wrong_identity', 'node_policy', 'data_policy'])
async def test_closed_turn_keeps_boot_identity_and_policy_checks(network, monkeypatch, guard):
    import httpx
    client, app, *_ = network
    sid, prefix, original, binding, node, calls = await completed_closed(network, monkeypatch)
    state = app.state.gate
    if guard in ('recover', 'wrong_sdk', 'wrong_identity'):
        state.store._exec('UPDATE router_bindings SET boot_id=? WHERE id=?', ('previous-boot', sid))
    if guard == 'wrong_sdk':
        state.store._exec('UPDATE router_bindings SET sdk_id=? WHERE id=?', ('foreign-sdk', sid))
    if guard == 'wrong_identity':
        upstream = state.nodes.clients[binding['node_id']]
        original_request = upstream.request
        async def wrong_identity(method, path, **kwargs):
            r = await original_request(method, path, **kwargs)
            if path.endswith('/reconcile'):
                return httpx.Response(200, json=dict(r.json(), nodeId='foreign-node'))
            return r
        monkeypatch.setattr(upstream, 'request', wrong_identity)
    if guard == 'node_policy':
        state.nodes.config[binding['node_id']].orgs = ['foreign-org']
    headers = {'X-Harness-Data-Class': 'unknown-secret-class'} if guard == 'data_policy' else {}
    response = await client.get(prefix+'/turns/original/events', headers=headers)
    assert response.status_code == (200 if guard == 'recover' else 403 if guard in ('node_policy','data_policy') else 409), response.text
    if guard == 'recover':
        assert response.json()['events'] == original
        saved = state.store._one('SELECT status,boot_id FROM router_bindings WHERE id=?', (sid,))
        assert saved == {'status': 'closed', 'boot_id': binding['boot_id']}
    assert calls == ['original']


async def test_closed_generated_media_bytes_remain_owned(network, monkeypatch):
    from PIL import Image
    from atlas_router.backends.stub import StubSession
    client, app, routers, settings, creds, other = network
    buffer = io.BytesIO(); Image.new('RGB', (8,8), 'green').save(buffer, format='PNG')
    raw = buffer.getvalue()
    async def generated(self, turn_id, text, attachments=None):
        artifact = self.media.save(self._media_scope, base64.b64encode(raw).decode(), call_id='image', turn_id=turn_id, source='test')
        yield {'type': 'media_artifact', 'id': turn_id, 'artifact': artifact}
        yield {'type': 'result', 'id': turn_id, 'ok': True, 'text': 'original image'}
    monkeypatch.setattr(StubSession, 'prompt', generated)
    sid = await create(client); prefix = '/harness/agent/sessions/'+sid
    assert (await client.post(prefix+'/prompt?mode=async', json={'id':'image', 'text':'original'})).status_code == 202
    original = await events(client, sid, 'image', True)
    artifact = next(e['artifact'] for e in original if e['type']=='media_artifact')
    assert (await client.delete(prefix)).status_code == 200
    assert (await client.get(prefix+'/media')).json()['artifacts'] == [artifact]
    result = await client.get(prefix+'/media/'+artifact['id'])
    assert result.status_code == 200, result.text
    assert hashlib.sha256(base64.b64decode(result.json()['data'])).hexdigest() == artifact['sha256']
    assert (await client.get(prefix+'/media/'+artifact['id'], headers={'Authorization':'Bearer '+other['device_token']})).status_code == 404

