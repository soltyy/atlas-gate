"""#60: реальные Gate/Router HTTP и управляемая потеря доставки/ответа."""
import asyncio
import httpx
import pytest

from .test_network_gate import network, create, events, serve


@pytest.fixture(params=['direct', 'connector'])
async def delivery_network(network, request, tmp_path):
    client, app, *_ = network
    if request.param == 'direct':
        yield network
        return
    from atlas_router.connector import Connector, private_key, csr_for, save_identity
    from atlas_gate.gate.node_channel import ChannelClient
    from atlas_gate.gate.nodes import Node
    state = app.state.gate
    node = state.nodes.config['a']
    router_url = node.url
    await state.nodes.clients['a'].aclose()
    state.nodes.config['a'] = Node(node_id='a', transport='connector', orgs=node.orgs,
                                  routes=node.routes, account_group=node.account_group)
    state.nodes.clients['a'] = ChannelClient(state.channel, 'a')
    state.nodes.config['b'].enabled = False
    admin = {'X-Atlas-Token':'admin'}
    invitation = (await client.post('/harness/admin/nodes/a/invite', headers=admin)).json()['invitation']
    root = tmp_path / 'connector'
    key = private_key(root)
    enrolled = await client.post('/node/enroll', json={'invitation':invitation, 'csr':csr_for(key, 'a')})
    assert enrolled.status_code == 200
    rid = (await client.get('/harness/admin/node-enrollments', headers=admin)).json()['enrollments'][0]['id']
    assert (await client.post(f'/harness/admin/node-enrollments/{rid}/approve', headers=admin)).status_code == 200
    identity = (await client.post('/node/enroll/poll', json={'invitation':invitation})).json()
    save_identity(root, key, 'a', str(client.base_url), identity)
    connector = Connector(root, router_url, 'test-router')
    running = asyncio.create_task(connector.run())
    try:
        yield network
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.parametrize('operation', ['prompt', 'tools'])
@pytest.mark.parametrize('fault', ['before', 'after_502'])
async def test_mutation_recovery_never_caches_relay_failure(delivery_network, monkeypatch, operation, fault):
    client, app, routers, *_ = delivery_network
    sid = await create(client)
    state = app.state.gate
    binding = state.store._one('SELECT * FROM router_bindings WHERE id=?', (sid,))
    upstream = state.nodes.clients[binding['node_id']]
    original = upstream.request
    attempts, accepted = [], []
    async def request(method, path, **kw):
        if method == 'POST' and path.endswith('/' + operation):
            attempts.append(kw['headers']['Idempotency-Key'])
            if len(attempts) == 1 and fault == 'before':
                raise httpx.ConnectError('not sent')
            response = await original(method, path, **kw)
            accepted.append(response)
            if len(attempts) == 1 and fault == 'after_502':
                return httpx.Response(502, json={'detail':'relay lost the reply'})
            return response
        return await original(method, path, **kw)
    monkeypatch.setattr(upstream, 'request', request)
    path = f'/harness/agent/sessions/{sid}/' + operation
    body = {'id':'once', 'text':'hello'} if operation == 'prompt' else {'tools':[]}
    params = {'mode':'async'} if operation == 'prompt' else {}
    headers = {'Idempotency-Key':'one-operation'}
    first = await client.post(path, json=body, params=params, headers=headers)
    if fault == 'before':
        assert first.status_code == 503
    second = await client.post(path, json=body, params=params, headers=headers)
    assert second.status_code == (202 if operation == 'prompt' else 200), second.text
    assert len(accepted) == 1 and len(set(attempts)) == 1
    op = state.store._one('SELECT * FROM router_operations WHERE session_id=?', (sid,))
    receipt = await client.get(f'/harness/agent/sessions/{sid}/commands/{op["id"]}')
    assert receipt.status_code == second.status_code
    assert receipt.headers['X-Atlas-Command-State'] == 'complete'
    assert receipt.json() == second.json()
    by_key = await client.get(f'/harness/agent/sessions/{sid}/commands', params={'requestId':'one-operation'})
    assert by_key.json() == second.json()
    assert by_key.headers['X-Atlas-Command-Id'] == op['id']


async def test_receipt_applies_data_class_policy_before_read(network):
    client, app, *_ = network
    sid = await create(client)
    path = f'/harness/agent/sessions/{sid}'
    ack = await client.post(path + '/compact', json={}, headers={'Prefer':'respond-async', 'Idempotency-Key':'compact'})
    assert ack.status_code == 202
    response = await client.get(path + '/commands/' + ack.json()['commandId'], headers={'X-Harness-Data-Class':'nonexistent'})
    assert response.status_code == 403


@pytest.mark.parametrize('saved', [None, [], {'state':'started'}, {'state':'unknown'},
    {'state':'expired'}, {'state':'complete', 'status':True, 'response':{}},
    {'requestId':'foreign', 'state':'complete', 'status':200, 'response':{}},
    {'state':'complete', 'status':200}])
async def test_unconfirmed_receipt_never_authorizes_redelivery(network, monkeypatch, saved):
    client, app, *_ = network
    sid = await create(client)
    state = app.state.gate
    binding = state.store._one('SELECT * FROM router_bindings WHERE id=?', (sid,))
    upstream = state.nodes.clients[binding['node_id']]
    original = upstream.request
    writes = []
    async def request(method, path, **kw):
        if method == 'POST' and path.endswith('/tools'):
            writes.append(path)
            raise httpx.ReadError('unknown delivery')
        if path.startswith('/v1/commands/'):
            return httpx.Response(503, json={}) if saved is None else httpx.Response(200, json=saved)
        return await original(method, path, **kw)
    monkeypatch.setattr(upstream, 'request', request)
    path = f'/harness/agent/sessions/{sid}'
    for _ in range(2):
        response = await client.post(path + '/tools', json={'tools':[]}, headers={'Idempotency-Key':'uncertain'})
        assert response.status_code == 503 and response.json()['error']['code'] == 'command_outcome_unknown'
    op = state.store._one('SELECT * FROM router_operations WHERE session_id=?', (sid,))
    receipt = await client.get(path + '/commands/' + op['id'])
    assert receipt.status_code in (202, 503)
    assert receipt.headers.get('X-Atlas-Command-State') != 'complete'
    await state.operations.reconcile(state.nodes)  # malformed receipt must not kill background recovery
    assert state.store._one('SELECT state FROM router_operations WHERE id=?', (op['id'],))['state'] != 'complete'
    assert len(writes) == 1


@pytest.mark.parametrize('guard', ['changed_boot', 'legacy_boot', 'no_durable_commands'])
async def test_missing_receipt_cannot_replay_with_insufficient_boot_evidence(network, monkeypatch, guard):
    client, app, *_ = network
    sid = await create(client)
    state = app.state.gate
    binding = state.store._one('SELECT * FROM router_bindings WHERE id=?', (sid,))
    upstream = state.nodes.clients[binding['node_id']]
    original = upstream.request
    writes = []
    async def request(method, path, **kw):
        if method == 'POST' and path.endswith('/tools'):
            writes.append(path)
            raise httpx.ConnectError('not sent')
        return await original(method, path, **kw)
    monkeypatch.setattr(upstream, 'request', request)
    path = f'/harness/agent/sessions/{sid}/tools'
    assert (await client.post(path, json={'tools':[]}, headers={'Idempotency-Key':'guard'})).status_code == 503
    if guard == 'legacy_boot':
        state.store._exec('UPDATE router_operations SET boot_id=NULL WHERE session_id=?', (sid,))
    probe = state.nodes.probe
    async def guarded(node):
        info = await probe(node)
        return dict(info, **({'bootId':'other'} if guard == 'changed_boot' else {'capabilities':[]} if guard == 'no_durable_commands' else {}))
    monkeypatch.setattr(state.nodes, 'probe', guarded)
    assert (await client.post(path, json={'tools':[]}, headers={'Idempotency-Key':'guard'})).status_code == 503
    assert len(writes) == 1


async def test_receipt_replay_after_gate_restart_and_foreign_access(network):
    from atlas_gate.main import create_app
    client, app, routers, settings, creds, other = network
    sid = await create(client)
    path = f'/harness/agent/sessions/{sid}'
    headers = {'Idempotency-Key':'stored'}
    ack = await client.post(path + '/tools', json={'tools':[]}, headers=headers)
    assert ack.status_code == 200
    command = ack.headers['X-Atlas-Command-Id']
    assert (await client.get(path + '/commands/' + command, headers={'Authorization':'Bearer '+other['device_token']})).status_code == 404
    await app.state.gate.close()
    new_app = create_app(settings)
    async with serve(new_app) as url, httpx.AsyncClient(base_url=url, headers={'Authorization':'Bearer '+creds['device_token']}) as restored:
        await new_app.state.gate.ready.wait()
        receipt = await restored.get(path + '/commands', params={'requestId':'stored'})
        assert receipt.status_code == 200 and receipt.json() == ack.json()
        assert (await restored.post(path + '/tools', json={'tools':[{'name':'different'}]}, headers=headers)).status_code == 409
        new_app.state.gate.nodes.config[new_app.state.gate.store._one('SELECT node_id FROM router_bindings WHERE id=?',(sid,))['node_id']].enabled = False
        assert (await restored.get(path + '/commands/' + command)).status_code == 403


async def test_real_router_502_is_terminal_only_with_saved_receipt(network, monkeypatch):
    client, app, routers, *_ = network
    sid = await create(client)
    state = app.state.gate
    binding = state.store._one('SELECT * FROM router_bindings WHERE id=?',(sid,))
    router = routers[0 if binding['node_id'] == 'a' else 1]
    calls = []
    async def failed(instructions):
        calls.append(instructions)
        raise RuntimeError('known SDK failure')
    monkeypatch.setattr(router.state.registry.get(binding['local_id']).session, 'compact', failed)
    path = f'/harness/agent/sessions/{sid}'
    ack = await client.post(path + '/compact', json={}, headers={'Prefer':'respond-async', 'Idempotency-Key':'failed-compact'})
    for _ in range(100):
        receipt = await client.get(path + '/commands/' + ack.json()['commandId'])
        if receipt.status_code != 202:
            break
        await asyncio.sleep(.02)
    assert receipt.status_code == 502 and receipt.headers['X-Atlas-Command-State'] == 'complete'
    assert 'known SDK failure' in receipt.text
    again = await client.post(path + '/compact', json={}, headers={'Idempotency-Key':'failed-compact'})
    assert again.status_code == 502 and len(calls) == 1


async def test_lost_ack_recovered_from_restarted_router_journal_without_redelivery(network, monkeypatch):
    from atlas_router.main import create_app as router_app
    client, app, routers, *_ = network
    sid = await create(client)
    state = app.state.gate
    binding = state.store._one('SELECT * FROM router_bindings WHERE id=?',(sid,))
    old = routers[0 if binding['node_id'] == 'a' else 1]
    original = state.nodes.clients[binding['node_id']].request
    async def lost(method, path, **kw):
        response = await original(method, path, **kw)
        if method == 'POST' and path.endswith('/tools'):
            raise httpx.ReadError('ACK lost after journal commit')
        return response
    monkeypatch.setattr(state.nodes.clients[binding['node_id']], 'request', lost)
    path = f'/harness/agent/sessions/{sid}'
    assert (await client.post(path + '/tools', json={'tools':[]}, headers={'Idempotency-Key':'reboot'})).status_code == 503
    # Release the old runtime and its exclusive journal lock before the new boot.
    await old.state.registry.close_all()
    await old.state.backend.close()
    old.state.journal.close()
    replacement = router_app(old.state.settings)
    async with serve(replacement) as url:
        await state.nodes.clients[binding['node_id']].aclose()
        state.nodes.config[binding['node_id']].url = url
        state.nodes.clients[binding['node_id']] = httpx.AsyncClient(base_url=url, headers={'X-Atlas-Token':'test-router'})
        state.nodes.snapshots.pop(binding['node_id'], None)
        result = await client.get(path + '/commands', params={'requestId':'reboot'})
        assert result.status_code == 200 and result.headers['X-Atlas-Command-State'] == 'complete'
        assert replacement.state.registry.list() == [], 'reading a receipt must not reconstruct/start an SDK session'


async def test_files_originals_lost_ack_deduplicates_without_resending_bytes(network, monkeypatch):
    import base64
    from atlas_gate.sdk_tools import SdkTools
    client, app, routers, *_ = network
    async def logged_in(): return {'loggedIn':True}
    for router in routers:
        monkeypatch.setattr(router.state.backend, 'name', 'claude')
        monkeypatch.setattr(router.state.cli, 'auth_status', logged_in)
        monkeypatch.setattr(router.state.files, 'available', lambda:True)
        original = router.state.backend.open_session
        async def open_session(*args, _original=original, sdk_tools=None, **kw):
            return await _original(*args, **kw)
        monkeypatch.setattr(router.state.backend, 'open_session', open_session)
    state = app.state.gate
    state.orgs['test-org'].sdk_tools = SdkTools(files=True)
    created = await client.post('/harness/agent/sessions',json={'model':'stub-fast','sdkTools':{'files':True}})
    assert created.status_code == 200, created.text
    sid = created.json()['id']; path = f'/harness/agent/sessions/{sid}'
    binding = state.store._one('SELECT * FROM router_bindings WHERE id=?',(sid,))
    upstream = state.nodes.clients[binding['node_id']]
    original = upstream.request
    sends = []
    async def lost(method, uri, **kw):
        response = await original(method, uri, **kw)
        if method == 'POST' and uri.endswith('/files'):
            sends.append(uri)
            return httpx.Response(502,json={'detail':'relay lost ACK'})
        return response
    monkeypatch.setattr(upstream,'request',lost)
    body = {'id':'original','attachments':[{'kind':'file','filename':'note.txt','mediaType':'text/plain','data':base64.b64encode(b'KEEP-729').decode()}]}
    first = await client.post(path+'/files',json=body,headers={'Idempotency-Key':'original'})
    assert first.status_code == 200, first.text
    repeat = await client.post(path+'/files',json=body,headers={'Idempotency-Key':'original'})
    assert repeat.json() == first.json() and len(sends) == 1
    assert len((await client.get(path+'/files')).json()['files']) == 1
