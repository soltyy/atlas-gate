"""Long commands over real HTTP: idempotence, ownership and cleanup safety."""
import asyncio
import httpx
from .test_network_gate import network, create, serve
from atlas_gate.main import create_app


async def held_compact(network, monkeypatch):
    client, app, routers, settings, creds, other = network
    sid = await create(client)
    binding = app.state.gate.store._one("SELECT * FROM router_bindings WHERE id=?", (sid,))
    entry = routers[0 if binding['node_id'] == 'a' else 1].state.registry.get(binding['local_id'])
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def compact(instructions):
        calls.append(instructions)
        started.set()
        await release.wait()
        return {'summary': 'KEEP-729', 'preTokens': 100, 'postTokens': 10,
                'durationMs': 1, 'context': None}
    monkeypatch.setattr(entry.session, 'compact', compact)
    return client, app, sid, started, release, calls, settings, other


async def test_async_receipt_preserves_session_and_executes_once(network, monkeypatch):
    client, app, sid, started, release, calls, settings, other = await held_compact(network, monkeypatch)
    path = '/harness/agent/sessions/' + sid
    headers = {'Prefer': 'respond-async', 'Idempotency-Key': 'held-compact'}
    body = {'instructions': 'KEEP-729'}
    first = await client.post(path + '/compact', headers=headers, json=body)
    assert first.status_code == 202
    cid = first.json()['commandId']
    await asyncio.wait_for(started.wait(), 5)
    try:
        assert (await client.get(path + '/commands/' + cid)).status_code == 202
        again = await client.post(path + '/compact', headers=headers, json=body)
        assert again.json()['commandId'] == cid
        assert (await client.delete(path)).status_code == 409
        foreign = await client.get(path + '/commands/' + cid, headers={'Authorization': 'Bearer ' + other['device_token']})
        assert foreign.status_code in (403, 404)
        conflict = await client.post(path + '/compact', headers=headers, json={'instructions': 'different'})
        assert conflict.status_code == 409
    finally:
        release.set()
    for _ in range(100):
        done = await client.get(path + '/commands/' + cid)
        if done.status_code != 202:
            break
        await asyncio.sleep(.02)
    assert done.status_code == 200 and done.json()['summary'] == 'KEEP-729'
    duplicate = await client.post(path + '/compact', headers=headers, json=body)
    assert duplicate.status_code == 202
    assert (await client.get(path + '/commands/' + cid)).json() == done.json()
    assert calls == ['KEEP-729']
    assert (await client.delete(path)).status_code == 200


async def test_legacy_wait_has_no_shared_node_read_deadline(network, monkeypatch):
    client, app, sid, started, release, calls, *_ = await held_compact(network, monkeypatch)
    path = '/harness/agent/sessions/' + sid
    for node in app.state.gate.nodes.clients.values():
        node.timeout = httpx.Timeout(.05, connect=5)
    request = asyncio.create_task(client.post(path + '/compact', json={'instructions': 'long'},
                                            headers={'Idempotency-Key': 'long'}))
    await asyncio.wait_for(started.wait(), 5)
    await asyncio.sleep(.2)
    assert not request.done()
    release.set()
    response = await request
    assert response.status_code == 200 and calls == ['long']


async def test_receipt_after_gate_restart_never_replays_command(network, monkeypatch):
    client, app, sid, started, release, calls, settings, *_ = await held_compact(network, monkeypatch)
    path = '/harness/agent/sessions/' + sid
    first = await client.post(path + '/compact', headers={'Prefer': 'respond-async', 'Idempotency-Key': 'restart'}, json={})
    cid = first.json()['commandId']
    token = client.headers['Authorization']
    await asyncio.wait_for(started.wait(), 5)
    await app.state.gate.close()
    new_app = create_app(settings)
    async with serve(new_app) as url, httpx.AsyncClient(base_url=url, headers={'Authorization': token}) as resumed:
        await new_app.state.gate.ready.wait()
        assert (await resumed.get(path + '/commands/' + cid)).status_code == 202
        release.set()
        for _ in range(100):
            done = await resumed.get(path + '/commands/' + cid)
            if done.status_code != 202:
                break
            await asyncio.sleep(.02)
        assert done.status_code == 200 and calls == [None]


async def test_lost_connector_reply_uses_receipt_not_false_terminal_502(network, monkeypatch):
    client, app, sid, started, release, calls, *_ = await held_compact(network, monkeypatch)
    state = app.state.gate
    binding = state.store._one("SELECT * FROM router_bindings WHERE id=?", (sid,))
    original = state.nodes.clients[binding['node_id']]
    running = []
    class LostReply:
        async def request(self, method, path, **kwargs):
            if method == 'POST' and path.endswith('/compact'):
                running.append(asyncio.create_task(original.request(method, path, **kwargs)))
                await started.wait()
                return httpx.Response(502, json={'detail': 'connector HTTP reply lost'})
            return await original.request(method, path, **kwargs)
        async def get(self, path):
            return await self.request('GET', path)
        async def aclose(self):
            await original.aclose()
    state.nodes.clients[binding['node_id']] = LostReply()
    prefix = '/harness/agent/sessions/' + sid
    ack = await client.post(prefix + '/compact', headers={'Prefer': 'respond-async', 'Idempotency-Key': 'relay'}, json={})
    cid = ack.json()['commandId']
    await started.wait()
    await asyncio.sleep(.1)
    assert (await client.get(prefix + '/commands/' + cid)).status_code == 202
    release.set()
    await asyncio.gather(*running)
    final = await client.get(prefix + '/commands/' + cid)
    assert final.status_code == 200 and final.json()['summary'] == 'KEEP-729'
    assert len(calls) == 1
