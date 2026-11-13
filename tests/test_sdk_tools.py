import base64
import json
import hashlib
import httpx
import pytest
from atlas_gate.sdk_tools import SdkTools, permitted
from atlas_gate.gate.errors import GateError
from atlas_gate.gate.routing import Routing
from atlas_gate.gate.store import Store
from .test_routing import FakeNodes, device
from .test_tool_result_images import image_network
from .test_network_gate import network


def test_policy_no_implicit_enable_and_modes():
    assert permitted(SdkTools(), SdkTools())
    assert not permitted(SdkTools(files=True), SdkTools())
    assert not permitted(SdkTools(webSearch='live'), SdkTools(webSearch='cached'))
    assert permitted(SdkTools(webSearch='cached'), SdkTools(webSearch='live'))


async def test_capability_routing_requires_matching_ack(tmp_path):
    nodes = FakeNodes(capacity=0)
    nodes.snapshots['b']['sdkTools'] = {'webSearch':{'modes':['live']}}
    store = Store(str(tmp_path/'gate.db')); routing = Routing(store,nodes)
    with pytest.raises(GateError) as fail:
        await routing.create(device('d'), 'sub', 'claude', {'sdkTools':{'webSearch':'live'}}, 0)
    assert fail.value.code == 'command_outcome_unknown'
    row = store._one('SELECT * FROM router_bindings')
    assert row['node_id'] == 'b' and row['status'] == 'unknown'
    store.close()


@pytest.mark.parametrize('original_size', [20, 33 * 1024 * 1024])
async def test_http_sdk_files_direct_and_connector(image_network, monkeypatch, original_size):
    client, app, routers, settings, creds, other = image_network
    async def logged_in(): return {'loggedIn':True}
    for router in routers:
        monkeypatch.setattr(router.state.backend, 'name', 'claude')
        monkeypatch.setattr(router.state.cli, 'auth_status', logged_in)
        monkeypatch.setattr(router.state.files, 'available', lambda: True)
        original = router.state.backend.open_session
        async def create(*args, _original=original, sdk_tools=None, **kwargs):
            return await _original(*args, **kwargs)
        monkeypatch.setattr(router.state.backend, 'open_session', create)
    state=app.state.gate
    org=state.orgs['test-org']; org.sdk_tools=SdkTools(webSearch='live',files=True)
    opts={'version':1,'webSearch':'live','files':True}
    response=await client.post('/harness/agent/sessions',json={'model':'stub-fast','tools':[],'sdkTools':opts})
    assert response.status_code == 200, response.text
    assert response.json()['sdkTools']==opts
    sid=response.json()['id']; path='/harness/agent/sessions/'+sid
    assert (await client.get(path+'/sdk-tools')).json()['sdkTools']==opts
    original = b'x' * original_size
    payload={'id':'upload','attachments':[{'kind':'file','filename':'book.xlsx','data':base64.b64encode(original).decode()}]}
    response=await client.post(path+'/files',json=payload,headers={'Idempotency-Key':'upload-once'},timeout=None)
    assert response.status_code==200, response.text
    ident=response.json()['files'][0]['id']
    assert ident == hashlib.sha256(original).hexdigest()
    assert response.json()['files'][0]['sizeBytes'] == original_size
    again=await client.post(path+'/files',json=payload,headers={'Idempotency-Key':'upload-once'},timeout=None)
    assert again.json()==response.json()
    assert len((await client.get(path+'/files')).json()['files'])==1
    denied=await client.get(path+'/files',headers={'Authorization':'Bearer '+other['device_token']})
    assert denied.status_code==404
    org.sdk_tools=SdkTools()
    assert (await client.post(path+'/prompt',json={'id':'deny','text':'read file'})).status_code==403
    assert (await client.get(path+'/sdk-tools')).status_code==200
    assert (await client.post(path+'/interrupt',json={},headers={'Idempotency-Key':'stop-revoked'})).status_code==200
    org.sdk_tools=SdkTools(files=True,webSearch='live')
    assert (await client.delete(path+'/files/'+ident,headers={'Idempotency-Key':'delete-once'})).status_code==200
    assert (await client.get(path+'/files')).json()['files']==[]
    org.sdk_tools=SdkTools()
    assert (await client.delete(path,headers={'Idempotency-Key':'close-revoked'})).status_code==200


@pytest.mark.parametrize('transport', ['direct', 'connector', 'connector-v2'])
async def test_adaptive_files_over_gate_keeps_legacy_and_recovers_ack(network, transport, monkeypatch):
    from atlas_router.adaptive_files import execute_file_tool
    from atlas_router.backends.stub import StubSession
    from atlas_router.connector import Connector
    from atlas_gate.gate.node_channel import ChannelClient

    client, app, routers, _, _, other = network
    state = app.state.gate
    state.orgs['test-org'].sdk_tools = SdkTools(files=True)
    workers = []
    connections = []
    raw = b'adaptive original'
    ident = hashlib.sha256(raw).hexdigest()
    worker_inputs = []

    async def logged_in():
        return {'loggedIn': True}

    async def run_sources(sources, code):
        worker_inputs.append([path.read_bytes() for _, path in sources])
        return {'exitCode': 0, 'stdout': 'checked', 'stderr': ''}

    async def prompt(self, turn, text, attachments=None):
        yield {'type': 'session', 'id': turn, 'sessionId': self.session_id}
        text = text.splitlines()[0]
        mode = 'list' if text == 'catalog' else 'python'
        if text != 'plain':
            try:
                await execute_file_tool(self, 'call', {'operation': mode, 'documentIds': [ident]})
            except ValueError as failed:
                yield {'type': 'assistant', 'id': turn, 'text': str(failed)}
        yield {'type': 'result', 'id': turn, 'ok': True, 'text': 'done', 'usage': {}}

    monkeypatch.setattr(StubSession, 'prompt', prompt)
    for router in routers:
        backend = router.state.backend
        monkeypatch.setattr(backend, 'name', 'claude')
        monkeypatch.setattr(router.state.cli, 'auth_status', logged_in)
        monkeypatch.setattr(router.state.files, 'available', lambda: True)
        monkeypatch.setattr(router.state.files, 'run_sources', run_sources)
        original = backend.open_session

        async def create(*args, _original=original, sdk_tools=None, **kwargs):
            return await _original(*args, **kwargs)

        monkeypatch.setattr(backend, 'open_session', create)

    import asyncio
    if transport != 'direct':
        protocol = 3 if transport == 'connector' else 2
        for node_id, node in state.nodes.config.items():
            connector = object.__new__(Connector)
            connector.identity = {'nodeId': node_id}
            connector.router = httpx.AsyncClient(base_url=node.url, headers={'X-Atlas-Token': 'test-router'})
            connections.append(connector.router)
            await state.nodes.clients[node_id].aclose()
            state.nodes.clients[node_id] = ChannelClient(state.channel, node_id)

            async def pump(n=node_id, c=connector):
                while True:
                    work = await state.channel.poll(n, wait=.1, protocol=protocol)
                    if work:
                        result = await c.execute(work)
                        state.channel.complete(n, work['id'], result['status'],
                                               base64.b64decode(result['body']), result['headers'])

            workers.append(asyncio.create_task(pump()))

    async def next_request(path, turn):
        response = await client.get(f'{path}/turns/{turn}/events', params={'wait': 1000})
        assert response.status_code == 200, response.text
        return next((ev for ev in response.json()['events'] if ev['type'] == 'adaptive_file_request'), None)

    async def wait_request(path, turn):
        for _ in range(100):
            event = await next_request(path, turn)
            if event:
                return event
            await asyncio.sleep(.02)
        pytest.fail('no adaptive_file_request')

    async def wait_done(path, turn):
        for _ in range(100):
            doc = (await client.get(f'{path}/turns/{turn}/events')).json()
            if doc.get('done'):
                return doc['events']
            await asyncio.sleep(.02)
        pytest.fail('turn did not finish')

    try:
        create_body = {'model': 'stub-fast', 'sdkTools': {'files': True}}
        old = await client.post('/harness/agent/sessions', json=create_body)
        assert old.status_code == 200, old.text
        legacy = '/harness/agent/sessions/' + old.json()['id']
        assert 'adaptiveFiles' not in old.json()
        assert (await client.get(legacy+'/files/adaptive')).status_code == 409
        assert (await client.post(legacy+'/files', json={'id': 'old', 'attachments': [{
            'kind': 'file', 'filename': 'legacy.bin', 'data': 'eA=='}]})).status_code == 200

        opened = await client.post('/harness/agent/sessions', json=dict(create_body, adaptiveFiles={
            'version': 1, 'branchId': 'branch-a'}))
        assert opened.status_code == 200, opened.text
        if transport == 'connector-v2':
            assert 'adaptiveFiles' not in opened.json()
            return
        ack = opened.json()['adaptiveFiles']
        path = '/harness/agent/sessions/' + opened.json()['id']
        assert (await client.get(path+'/files/adaptive')).json()['adaptiveFiles'] == ack
        stale = await client.post(path+'/prompt?mode=async', json={'id': 'stale', 'text': 'plain',
            'adaptiveFiles': {'version': 1, 'bootId': 'old-boot'}})
        assert stale.status_code == 409
        assert stale.json()['error']['code'] == 'adaptive_files_reconfirmation_required'
        offer = {'version': 1, 'bootId': ack['bootId']}

        async def start(turn, text, marker=offer):
            body = {'id': turn, 'text': text}
            if marker is not None:
                body['adaptiveFiles'] = marker
            result = await client.post(path+'/prompt?mode=async', json=body)
            assert result.status_code == 202, result.text

        for turn in ('plain', 'plain-next'):
            await start(turn, 'plain')
            assert all(event['type'] != 'adaptive_file_request' for event in await wait_done(path, turn))
        await start('legacy-call', 'catalog', marker=None)
        assert all(event['type'] != 'adaptive_file_request' for event in await wait_done(path, 'legacy-call'))

        for turn, query, byte_needed in [('catalog', 'catalog', None), ('first', 'read', True), ('cached', 'read', False), ('revoked', 'read', False)]:
            await start(turn, query)
            event = await wait_request(path, turn)
            endpoint = path+'/files/adaptive/'+event['requestId']
            headers = {'Idempotency-Key': event['requestId']}
            fields = {key: event[key] for key in ('bootId', 'branchId', 'turnId', 'callId')}
            assert (await client.get(endpoint)).json()['status'] == 'pending'
            wrong = {'Authorization': 'Bearer '+other['device_token'], **headers}
            assert (await client.get(endpoint, headers=wrong)).status_code == 404
            body = dict(fields, status='available')
            if byte_needed is None:
                body['files'] = [{'sha256': ident, 'filename': 'old.bin', 'sizeBytes': len(raw)}] + [
                    {'sha256': hashlib.sha256(str(index).encode()).hexdigest(),
                     'filename': f'other-{index}.bin', 'sizeBytes': index}
                    for index in range(1, 209)]
            else:
                assert event['needsBytes'] is byte_needed
                body['sha256'] = ident
                if byte_needed:
                    body['data'] = base64.b64encode(raw).decode()
            if turn == 'first':
                broken = dict(body, data='eA==')
                assert (await client.post(endpoint, headers=headers, json=broken)).status_code == 422
                assert (await client.post(endpoint, headers=headers, json=dict(body, branchId='foreign'))).status_code == 409
                assert (await client.post(endpoint, headers=wrong, json=body)).status_code == 404
            if turn == 'revoked':
                body['status'] = 'unavailable'
            accepted = await client.post(endpoint, headers=headers, json=body)
            assert accepted.status_code == 200, accepted.text
            assert (await client.get(endpoint)).json() == accepted.json()
            assert (await client.post(endpoint, headers=headers, json=body)).json() == accepted.json()
            changed = 'unavailable' if body['status'] == 'available' else 'available'
            conflict = await client.post(endpoint, headers=headers, json=dict(body, status=changed))
            assert conflict.status_code == 409
            await wait_done(path, turn)
        assert worker_inputs == [[raw], [raw]]

        await start('stop', 'read')
        event = await wait_request(path, 'stop')
        stopped = await client.post(path+'/interrupt', json={}, headers={'Idempotency-Key': 'stop-adaptive'})
        assert stopped.status_code == 200, stopped.text
        endpoint = path+'/files/adaptive/'+event['requestId']
        body = {key: event[key] for key in ('bootId', 'branchId', 'turnId', 'callId', 'sha256')}
        late = await client.post(endpoint, headers={'Idempotency-Key': event['requestId']},
                                 json=dict(body, status='available'))
        assert late.status_code == 409, late.text
        await wait_done(path, 'stop')
    finally:
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        for connection in connections:
            await connection.aclose()
