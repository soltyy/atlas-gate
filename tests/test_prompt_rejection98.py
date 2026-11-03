"""#98: own loopback HTTP/SQLite only; no Router SDK or model requests."""
import asyncio
import concurrent.futures
import hashlib
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from atlas_gate.gate.errors import GateError, GateFreshNodeError
from atlas_gate.gate.nodes import Node
from atlas_gate.gate.operations import Operations, body_sha256
from atlas_gate.gate.store import Store
from .test_discovery import configured, enroll
from .test_network_gate import serve


def identity():
    device = SimpleNamespace(device_id='device', org='org', user_id='user')
    binding = dict(id='session', device_id='device', org='org', node_id='node', boot_id='boot',
                   local_id='local', route='route', model='model', roles='[]')
    return device, binding


def rejected(operations, device, binding, key='key', turn='turn'):
    body = {'id': turn, 'text': 'привет'}
    path = '/v1/sessions/local/prompt'
    op, _ = operations.begin(device, binding, 'POST', path, body, key, turn)
    response = httpx.Response(503, json={'error': {'code': 'node_unavailable',
        'message': 'идентичность Router не подтверждена', 'deliveryState': 'not_sent'}})
    failure = GateFreshNodeError('идентичность Router не подтверждена', node_id='node',
        method='POST', path=path, reason='identity_unconfirmed')
    operations.complete(op['id'], response, pre_dispatch=(binding, failure))
    return body, op, response


def test_rejection_proof_restart_original_receipt_and_target_are_immutable(tmp_path):
    path = str(tmp_path/'gate.db')
    store = Store(path)
    operations = Operations(store)
    device, binding = identity()
    body, op, response = rejected(operations, device, binding)
    original = store._one('SELECT * FROM router_operations WHERE id=?', (op['id'],))
    # The current binding may advance after a read-only native recovery.
    binding['boot_id'] = 'later-boot'
    proof = operations.resolve_prompt(device, binding, 'key', 'turn', body_sha256(body))
    assert proof == {**proof, 'version': 1, 'outcome': 'not_sent', 'proofType': 'admission_fence',
        'commandId': op['id'], 'requestId': 'key', 'deviceId': 'device', 'org': 'org',
        'sessionId': 'session', 'turnId': 'turn', 'bodySha256': body_sha256(body),
        'nodeId': 'node', 'bootId': 'boot', 'route': 'route', 'model': 'model',
        'sealed': True, 'requiresNewIntent': True}
    operations.complete(op['id'], httpx.Response(202, json={'id': 'turn'}))
    assert store._one('SELECT * FROM router_operations WHERE id=?', (op['id'],)) == original
    store.close()
    store = Store(path)
    operations = Operations(store)
    assert operations.resolve_prompt(device, binding, 'key', 'turn', body_sha256(body)) == proof
    assert store._one('SELECT response FROM router_operations WHERE id=?', (op['id'],))['response'] == response.text
    with pytest.raises(GateError, match='SHA'):
        operations.resolve_prompt(device, binding, 'key', 'turn', '0'*64)
    for changed in ({**binding, 'id': 'another-session'},):
        with pytest.raises(GateError):
            operations.resolve_prompt(device, changed, 'key', 'turn', body_sha256(body))
    store.close()


@pytest.mark.parametrize('case', ['legacy503', 'prepared', 'unknown', 'accepted', 'arbitrary503',
                                 'missing_hash', 'malformed', 'foreign_turn'])
def test_non_proof_receipts_remain_observation_only(tmp_path, case):
    store = Store(str(tmp_path/'gate.db'))
    operations = Operations(store)
    device, binding = identity()
    body, op, _ = rejected(operations, device, binding)
    if case == 'legacy503':
        # Exact historical #98 public body is insufficient without provenance.
        store._exec('UPDATE router_operations SET admission_rejection=NULL WHERE id=?', (op['id'],))
    elif case in ('prepared', 'unknown'):
        store._exec('UPDATE router_operations SET state=? WHERE id=?', (case, op['id']))
    elif case == 'accepted':
        store._exec('UPDATE router_operations SET status=202 WHERE id=?', (op['id'],))
    elif case == 'arbitrary503':
        store._exec('UPDATE router_operations SET admission_rejection=NULL,response=? WHERE id=?',
                    ('{"error":{"deliveryState":"not_sent"}}', op['id']))
    elif case == 'missing_hash':
        store._exec('UPDATE router_operations SET body_sha256=NULL WHERE id=?', (op['id'],))
    elif case == 'malformed':
        store._exec("UPDATE router_operations SET admission_rejection='[]' WHERE id=?", (op['id'],))
    else:
        operations.begin(device, binding, 'POST', '/v1/sessions/local/prompt', body, 'other', 'turn')
    proof = operations.resolve_prompt(device, binding, 'key', 'turn', body_sha256(body))
    assert proof['outcome'] == 'unknown'
    assert not store._one('SELECT * FROM router_admission_fences')
    store.close()


def test_reconcile_and_late_post_races_do_not_dispatch(tmp_path):
    store = Store(str(tmp_path/'gate.db'))
    operations = Operations(store)
    device, binding = identity()
    for i in range(12):
        body, op, _ = rejected(operations, device, binding, 'key'+str(i), 'turn'+str(i))
        barrier = threading.Barrier(3)
        def reconcile():
            barrier.wait()
            return operations.resolve_prompt(device, binding, 'key'+str(i), 'turn'+str(i), body_sha256(body))
        def late_post():
            barrier.wait()
            return operations.begin(device, binding, 'POST', '/v1/sessions/local/prompt', body,
                                    'key'+str(i), 'turn'+str(i))
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            a, b, late = pool.submit(reconcile), pool.submit(reconcile), pool.submit(late_post)
            assert a.result() == b.result()
            replay, fresh = late.result()
            assert not fresh and replay['state'] == 'complete' and replay['status'] == 503
    store.close()


def test_complete_rejection_vs_resolve_is_atomic(tmp_path):
    store = Store(str(tmp_path/'gate.db'))
    operations = Operations(store)
    device, binding = identity()
    for i in range(12):
        body, key, turn = {'id': str(i), 'text': 'work'}, 'key'+str(i), str(i)
        path = '/v1/sessions/local/prompt'
        op, _ = operations.begin(device, binding, 'POST', path, body, key, turn)
        barrier = threading.Barrier(2)
        def complete():
            barrier.wait()
            operations.complete(op['id'], httpx.Response(503, json={'error': {'code': 'node_unavailable'}}),
                pre_dispatch=(binding, GateFreshNodeError('unavailable', node_id='node', method='POST',
                                                         path=path, reason='node_unavailable')))
        def resolve():
            barrier.wait()
            return operations.resolve_prompt(device, binding, key, turn, body_sha256(body))
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            done, result = pool.submit(complete), pool.submit(resolve)
            done.result()
            first = result.result()
        final = operations.resolve_prompt(device, binding, key, turn, body_sha256(body))
        assert final['outcome'] == 'not_sent'
        if first['outcome'] == 'not_sent':
            assert first == final
        else:
            assert first['receiptState'] == 'prepared'
    store.close()


@pytest.fixture
async def admission_http(configured):
    client, state, _ = configured
    credentials = await enroll(client)
    client.headers['Authorization'] = 'Bearer '+credentials['device_token']
    fake = FastAPI()
    control = SimpleNamespace(available=False, mode='success', effects=0, posts=0, receipts={})

    @fake.get('/v1/node')
    async def node():
        if not control.available:
            return JSONResponse({'detail': 'identity is not ready'}, status_code=503)
        return {'nodeId': 'node', 'bootId': 'boot', 'protocolVersion': 1, 'capacity': 0,
                'models': [{'value': 'gpt-6.1-sol'}], 'capabilities': ['durable_commands', 'durable_events']}

    @fake.post('/v1/sessions/local/prompt')
    async def prompt(request: Request):
        control.posts += 1
        key = request.headers['Idempotency-Key']
        if key in control.receipts:
            saved = control.receipts[key]
            return JSONResponse(saved['response'], status_code=saved['status'])
        control.effects += 1
        if control.mode == 'success':
            status, body = 202, {'turnId': (await request.json())['id']}
        else:
            status, body = 503, {'error': {'code': 'node_unavailable',
                'message': 'идентичность Router не подтверждена', 'deliveryState': 'not_sent'}}
        control.receipts[key] = {'state': 'complete', 'requestId': key, 'status': status, 'response': body}
        if control.mode == 'lost_ack':
            control.receipts[key]['state'] = 'started'
        return JSONResponse(body, status_code=status)

    @fake.get('/v1/commands/{key}')
    async def receipt(key: str):
        return JSONResponse(control.receipts.get(key, {}), status_code=200 if key in control.receipts else 404)

    @fake.get('/v1/sessions/local/turns/{turn}/events')
    async def events(turn: str):
        return {'events': [], 'done': True, 'nextAfter': 0}

    async with serve(fake) as url:
        state.nodes.config['node'] = Node(node_id='node', url=url, token_env='INERT', orgs=['first'],
                                         routes=['private-route'], account_group='node')
        state.nodes.clients['node'] = httpx.AsyncClient(base_url=url)
        state.store._exec('''INSERT INTO router_bindings
            (id,device_id,org,route,node_id,boot_id,local_id,model,roles,status,created,sdk_alias)
            VALUES (?,?,?,?,?,?,?,?,?,'ready',?,'history')''',
            ('session', credentials['device_id'], 'first', 'private-route', 'node', 'boot',
             'local', 'gpt-6.1-sol', '[]', time.time()))
        yield client, state, control, credentials


async def test_http_rejection_exact_receipt_fence_successor_effect_once_and_late_races(admission_http):
    client, state, fake, credentials = admission_http
    url = '/harness/agent/sessions/session'
    body = {'id': 'old', 'text': 'работа'}
    headers = {'Idempotency-Key': 'original'}
    first = await client.post(url+'/prompt?mode=async', json=body, headers=headers)
    assert first.status_code == 503 and first.json()['error']['deliveryState'] == 'not_sent'
    assert first.headers['X-Atlas-Command-State'] == 'complete'
    command = first.headers['X-Atlas-Command-Id']
    original = state.store._one('SELECT * FROM router_operations WHERE id=?', (command,))
    assert original['admission_rejection'] and fake.posts == 0
    assert (await client.get(url+'/commands/'+command)).content == first.content
    query = {'version': 1, 'requestId': 'original', 'turnId': 'old', 'bodySha256': body_sha256(body)}
    # Complete 503 is replayed even before the negative proof is requested.
    assert (await client.post(url+'/prompt?mode=async', json=body, headers=headers)).content == first.content
    fake.available = True
    async def late():
        return await client.post(url+'/prompt?mode=async', json=body, headers=headers)
    results = await asyncio.gather(client.post(url+'/commands/reconcile', json=query), late(),
                                   client.post(url+'/commands/reconcile', json=query), late())
    assert results[0].status_code == 409 and results[0].content == results[2].content
    assert results[1].content == results[3].content == first.content
    proof = results[0].json()['recovery']
    assert proof['bodySha256'] == query['bodySha256'] and proof['deviceId'] == credentials['device_id']
    assert proof['org'] == 'first' and proof['sealed'] and proof['requiresNewIntent']
    assert results[0].headers['X-Atlas-Recovery-Version'] == '1'
    assert results[0].headers['X-Atlas-Command-Outcome'] == 'not_sent'
    for change in ({'bodySha256': 'f'*64}, {'turnId': 'another'}):
        assert (await client.post(url+'/commands/reconcile', json={**query, **change})).status_code == 409
    foreign = await enroll(client, 'second')
    assert (await client.post(url+'/commands/reconcile', json=query,
        headers={'Authorization': 'Bearer '+foreign['device_token']})).status_code == 404
    assert (await client.post(url+'/prompt?mode=async', json={'id': 'new', 'text': 'работа'},
        headers={'Idempotency-Key': 'successor'})).status_code == 202
    assert fake.posts == fake.effects == 1
    assert (await late()).content == first.content
    assert (await client.get(url+'/commands/'+command)).content == first.content
    assert state.store._one('SELECT * FROM router_operations WHERE id=?', (command,)) == original
    assert fake.posts == fake.effects == 1


@pytest.mark.parametrize('mode', ['arbitrary503', 'lost_ack', 'read_timeout', 'generic_node_unavailable'])
async def test_http_router_503_or_accepted_lost_ack_never_get_negative_proof(admission_http, monkeypatch, mode):
    client, state, fake, _ = admission_http
    fake.available, fake.mode = True, mode
    if mode in ('read_timeout', 'generic_node_unavailable'):
        upstream = state.nodes.clients['node']
        original = upstream.request
        async def lost(method, path, **kwargs):
            response = await original(method, path, **kwargs)
            if method == 'POST':
                if mode == 'read_timeout':
                    raise httpx.ReadTimeout('accepted, ACK lost')
                raise GateError(503, 'node_unavailable', 'same public code after dispatch')
            return response
        monkeypatch.setattr(upstream, 'request', lost)
    url = '/harness/agent/sessions/session'
    body = {'id': 'uncertain', 'text': 'work'}
    first = await client.post(url+'/prompt?mode=async', json=body, headers={'Idempotency-Key': 'uncertain'})
    assert first.status_code == 503
    proof = await client.post(url+'/commands/reconcile', json={'version': 1, 'requestId': 'uncertain',
        'turnId': 'uncertain', 'bodySha256': body_sha256(body)})
    assert proof.status_code == 202 and proof.json()['recovery']['outcome'] == 'unknown'
    assert not state.store._one('SELECT * FROM router_admission_fences')
    assert fake.posts == fake.effects == 1


async def test_http_proof_and_original_receipt_survive_gate_restart(admission_http):
    from atlas_gate.main import create_app
    client, state, fake, credentials = admission_http
    url = '/harness/agent/sessions/session'
    body = {'id': 'old', 'text': 'work'}
    response = await client.post(url+'/prompt?mode=async', json=body, headers={'Idempotency-Key': 'restart'})
    assert response.status_code == 503
    request = {'version': 1, 'requestId': 'restart', 'turnId': 'old', 'bodySha256': body_sha256(body)}
    proof = await client.post(url+'/commands/reconcile', json=request)
    assert proof.status_code == 409
    node = state.nodes.config['node']
    # Closed owner bindings still permit historical reconciliation.
    state.store._exec("UPDATE router_bindings SET status='closed' WHERE id='session'")
    await state.close()
    app = create_app(state.settings)
    async with serve(app) as origin, httpx.AsyncClient(base_url=origin,
            headers={'Authorization': 'Bearer '+credentials['device_token']}) as restored:
        await app.state.gate.ready.wait()
        app.state.gate.nodes.config['node'] = node
        app.state.gate.nodes.clients['node'] = httpx.AsyncClient(base_url=node.url)
        repeated = await restored.post(url+'/commands/reconcile', json=request)
        assert repeated.content == proof.content and repeated.headers['X-Atlas-Command-Outcome'] == 'not_sent'
        receipt = await restored.get(url+'/commands', params={'requestId': 'restart'})
        assert receipt.status_code == 503 and receipt.content == response.content
        assert fake.posts == fake.effects == 0


async def test_offline_operator_witness_preserves_legacy_receipt_and_closed_original_target(admission_http):
    client, state, fake, _ = admission_http
    url = '/harness/agent/sessions/session'
    body = {'id': 'legacy', 'text': 'work'}
    response = await client.post(url+'/prompt?mode=async', json=body, headers={'Idempotency-Key': 'legacy'})
    command = response.headers['X-Atlas-Command-Id']
    original = state.store._one('SELECT * FROM router_operations WHERE id=?', (command,))
    witness = {**json.loads(original['admission_rejection']), 'source': 'operator_process_down',
        'commandId': command, 'created': original['created'], 'httpStatus': 503,
        'responseSha256': hashlib.sha256(original['response'].encode()).hexdigest(),
        'stoppedAt': original['created']-10, 'rejectedAt': original['created']+1,
        'startedAt': original['created']+20, 'auditRef': '/var/backups/atlas/audit98.json',
        'auditSha256': 'a'*64}
    state.store._exec('UPDATE router_operations SET admission_rejection=NULL WHERE id=?', (command,))
    state.store._exec("UPDATE router_bindings SET status='closed',boot_id='later-boot' WHERE id='session'")
    request = {'version': 1, 'requestId': 'legacy', 'turnId': 'legacy', 'bodySha256': body_sha256(body)}
    assert (await client.post(url+'/commands/reconcile', json=request)).status_code == 202
    for changed in ({'responseSha256': 'f'*64}, {'bodySha256': 'f'*64}, {'org': 'second'},
                    {'rejectedAt': witness['startedAt']+1}, {'created': original['created']+1},
                    {'stoppedAt': original['created']+1}, {'bootId': 'later-boot'}):
        with pytest.raises(GateError) as failure:
            state.operations.import_prompt_rejection(command, {**witness, **changed})
        assert failure.value.code == 'admission_witness_conflict'
    assert state.operations.import_prompt_rejection(command, witness) == witness
    assert state.operations.import_prompt_rejection(command, witness) == witness
    with pytest.raises(GateError):
        state.operations.import_prompt_rejection(command, {**witness, 'auditSha256': 'b'*64})
    proof = await client.post(url+'/commands/reconcile', json=request)
    assert proof.status_code == 409 and proof.json()['recovery']['bootId'] == 'boot'
    assert 'auditRef' not in proof.text  # Private operator evidence is not a client payload.
    saved = state.store._one('SELECT * FROM router_operations WHERE id=?', (command,))
    assert {k: v for k, v in saved.items() if k != 'admission_rejection'} == {
        k: v for k, v in original.items() if k != 'admission_rejection'}
    receipt = await client.get(url+'/commands', params={'requestId': 'legacy'})
    assert receipt.status_code == 503 and receipt.content == response.content
    assert fake.posts == fake.effects == 0
