"""Gate #90: real HTTP and durable receipts; native SDK context is scripted."""
import asyncio
import hashlib
import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from atlas_gate.schemas import SteerRequest
from atlas_gate.gate.operations import Operations, body_sha256
from atlas_gate.gate.store import Store
from .test_network_gate import create, events, network  # noqa: F401


CAP = {'version': 1, 'expectedTurnId': True, 'clientMessageId': True,
       'durableAbsorption': True, 'delivery': 'next_boundary',
       'requiredHeader': 'X-Atlas-Steer-Contract', 'targetNodeId': 'a', 'targetBootId': 'one',
       'attachments': {'images': False, 'documents': False}}


def headers_for(client, prefix, key=None, **extra):
    headers = dict(client.steer_headers[prefix])
    if key is not None:
        headers['Idempotency-Key'] = key
    return {**headers, **extra}


async def capture_target(client, prefix):
    metadata = await client.get(prefix + '/sdk-tools', params={'steering': '1'})
    assert metadata.status_code == 200, metadata.text
    if not hasattr(client, 'steer_headers'):
        client.steer_headers = {}
    cap = metadata.json()['steering']
    client.steer_headers[prefix] = cap.get('requiredHeaders') or {
        'X-Atlas-Steer-Contract': '1', 'X-Atlas-Steer-Target-Boot': cap.get('targetBootId', 'unsupported')}


def payload(**overrides):
    return {'expectedTurnId': 'original', 'clientMessageId': str(uuid.uuid4()),
            'text': 'одинаковый текст', 'attachments': [], **overrides}


@pytest.mark.parametrize('body', [
    {'text': 'x', 'expectedTurnId': 't'}, {'text': 'x', 'clientMessageId': str(uuid.uuid4())},
    payload(expectedTurnId=''), payload(clientMessageId='not-uuid'), payload(text=123),
    payload(clientMessageId='B62A0539-A97C-4219-983A-C3DB7EE41F22'), {'text': 'x', 'attachments': [{'kind': 'text'}]},
    payload(attachmentFiles=[{'kind': 'image', 'data': 'YWJj'}]),
])
def test_exact_steer_identity_is_typed(body):
    with pytest.raises(ValidationError):
        SteerRequest.model_validate(body)


def test_absorption_precedes_ack_and_survives_restart_with_original_identity(tmp_path):
    path = str(tmp_path / 'gate.db')
    store = Store(path)
    device = SimpleNamespace(device_id='d', org='o')
    binding = {'id': 'public', 'local_id': 'native', 'node_id': 'a', 'boot_id': 'boot'}
    body = payload()
    evidence = {'version': 1, 'sessionId': 'native', 'turnId': body['expectedTurnId'],
                'clientMessageId': body['clientMessageId'], 'uuid': None, 'state': 'unknown',
                'bodySha256': body_sha256(body)}
    operations = Operations(store)
    op, fresh = operations.begin(device, binding, 'POST', '/v1/sessions/native/steer', body,
                                  'original-key', steer=evidence)
    assert fresh and op['turn_id'] is None and op['quota_day'] is None
    event = {'type': 'steer_absorbed', 'id': 'original', 'uuid': 'native-user-id',
             'clientMessageId': body['clientMessageId']}
    operations.absorbed(binding, dict(event, id='next-turn'))
    assert json.loads(store._one('SELECT steer FROM router_operations')['steer'])['state'] == 'unknown'
    operations.absorbed(binding, event)
    ack = {**evidence, 'state': 'accepted', 'uuid': event['uuid']}
    operations.complete(op['id'], httpx.Response(200, json={'ok': True, 'steer': ack}))
    with pytest.raises(ValueError):
        operations.observe_steer(op['id'], {**ack, 'bodySha256': 'f' * 64})
    store.close()
    store = Store(path)
    try:
        operations = Operations(store)
        replay, fresh = operations.begin(device, binding, 'POST', '/v1/sessions/native/steer', body,
                                          'original-key', steer=evidence)
        assert not fresh
        saved = json.loads(replay['steer'])
        assert saved == {**evidence, 'state': 'absorbed', 'uuid': event['uuid']}
        assert replay['body_sha256'] == body_sha256(body)
        assert store._one('SELECT COUNT(*) n FROM router_operations')['n'] == 1
    finally:
        store.close()


def test_current_unknown_receipt_outranks_the_saved_accepted_http_body(tmp_path):
    store = Store(str(tmp_path / 'gate.db'))
    try:
        operations = Operations(store)
        body = payload()
        receipt = {'version': 1, 'sessionId': 'native', 'turnId': 'original',
                   'clientMessageId': body['clientMessageId'], 'uuid': 'sdk-identity',
                   'state': 'accepted', 'bodySha256': body_sha256(body)}
        op, _ = operations.begin(SimpleNamespace(device_id='d', org='o'),
            {'id': 's', 'node_id': 'a', 'boot_id': 'boot'}, 'POST', '/v1/sessions/native/steer',
            body, 'key', steer={**receipt, 'state': 'unknown', 'uuid': None})
        old_ack = httpx.Response(200, json={'ok': True, 'steer': receipt})
        operations.complete(op['id'], old_ack)
        operations.complete(op['id'], old_ack, steer_receipt={**receipt, 'state': 'unknown'})
        assert json.loads(store._one('SELECT steer FROM router_operations')['steer'])['state'] == 'unknown'
        operations.observe_steer(op['id'], {**receipt, 'state': 'unknown'})
        operations.complete(op['id'], old_ack)  # delayed ACK raced with current lookup
        assert json.loads(store._one('SELECT steer FROM router_operations')['steer'])['state'] == 'unknown'
        operations.observe_steer(op['id'], {**receipt, 'state': 'absorbed'})
        assert json.loads(store._one('SELECT steer FROM router_operations')['steer'])['state'] == 'absorbed'
    finally:
        store.close()


async def connector_node(client, state, tmp_path, return_connector=False):
    from atlas_router.connector import Connector, private_key, csr_for, save_identity
    from atlas_gate.gate.node_channel import ChannelClient
    from atlas_gate.gate.nodes import Node
    node = state.nodes.config['a']
    router_url = node.url
    await state.nodes.clients['a'].aclose()
    state.nodes.config['a'] = Node(node_id='a', transport='connector', orgs=node.orgs,
                                   routes=node.routes, account_group=node.account_group)
    state.nodes.clients['a'] = ChannelClient(state.channel, 'a')
    state.nodes.config['b'].enabled = False
    admin = {'X-Atlas-Token': 'admin'}
    invite = (await client.post('/harness/admin/nodes/a/invite', headers=admin)).json()['invitation']
    root = tmp_path / 'connector'
    key = private_key(root)
    assert (await client.post('/node/enroll', json={'invitation': invite, 'csr': csr_for(key, 'a')})).status_code == 200
    rid = (await client.get('/harness/admin/node-enrollments', headers=admin)).json()['enrollments'][0]['id']
    assert (await client.post('/harness/admin/node-enrollments/' + rid + '/approve', headers=admin)).status_code == 200
    identity = (await client.post('/node/enroll/poll', json={'invitation': invite})).json()
    save_identity(root, key, 'a', str(client.base_url), identity)
    connector = Connector(root, router_url, 'test-router')
    task = asyncio.create_task(connector.run())
    return (task, connector) if return_connector else task


async def setup_native(network, monkeypatch):
    """Router still performs admission, dedup, journal and original-turn event delivery."""
    from atlas_router.backends.stub import StubSession
    client, app, routers, *_ = network
    consumed, calls = {}, []

    async def steer_v1(self, text, *, turn_id, uid, client_message_id, observe, attachments=None):
        calls.append((turn_id, uid, client_message_id, text, attachments))
        consumed[client_message_id] = lambda: observe('absorbed', proof={
            'kind': 'native_context', 'messageId': uid})

    monkeypatch.setattr(StubSession, 'steer_v1', steer_v1, raising=False)
    for node in routers:
        monkeypatch.setattr(node.state.backend, 'steering_capabilities', lambda: True, raising=False)
    for name in app.state.gate.nodes.config:
        await app.state.gate.nodes.probe(name)
    return consumed, calls


async def active(client):
    sid = await create(client)
    prefix = '/harness/agent/sessions/' + sid
    await capture_target(client, prefix)
    started = await client.post(prefix + '/prompt?mode=async', json={'id': 'original', 'text': 'call:echo'})
    assert started.status_code == 202, started.text
    await events(client, sid, 'original')
    return sid, prefix


@pytest.mark.parametrize('transport', ['direct', 'connector'])
async def test_real_http_accepted_absorbed_lost_ack_owner_and_same_text_ids(network, monkeypatch, tmp_path, transport):
    consumed, calls = await setup_native(network, monkeypatch)
    client, app, routers, _, creds, other = network
    state = app.state.gate
    task = await connector_node(client, state, tmp_path) if transport == 'connector' else None
    try:
        sid, prefix = await active(client)
        body = payload()
        headers = headers_for(client, prefix, 'steer-original')
        accepted = await client.post(prefix + '/steer', json=body, headers=headers)
        assert accepted.status_code == 200, accepted.text
        proof = accepted.json()['steer']
        assert proof['state'] == 'accepted' and proof['sessionId'] == sid
        assert proof['bodySha256'] == hashlib.sha256(accepted.request.content).hexdigest() and len(calls) == 1
        command = accepted.headers['X-Atlas-Command-Id']
        # Reading receipt and repeating the same POST only observe the original SDK identity.
        looked_up = await client.get(prefix + '/commands', params={'requestId': 'steer-original'})
        assert looked_up.json()['steer'] == proof
        duplicate = await client.post(prefix + '/steer', json=body, headers=headers)
        assert duplicate.json()['steer'] == proof and len(calls) == 1
        denied = await client.get(prefix + '/commands/' + command, headers={'Authorization': 'Bearer ' + other['device_token']})
        assert denied.status_code == 404
        conflict = await client.post(prefix + '/steer', json=dict(body, text='changed'), headers=headers)
        assert conflict.status_code == 409 and len(calls) == 1
        consumed[body['clientMessageId']]()
        events_after = await events(client, sid, 'original')
        event = next(e for e in events_after if e['type'] == 'steer_absorbed')
        assert event['id'] == 'original' and event['uuid'] == proof['uuid']
        receipt = await client.get(prefix + '/commands/' + command)
        assert receipt.json()['steer']['state'] == 'absorbed'
        # Same caption is another intent, never deduplicated by text.
        second = await client.post(prefix + '/steer', json=payload(), headers=headers_for(client, prefix, 'second'))
        assert second.status_code == 200 and len(calls) == 2
        assert second.json()['steer']['uuid'] != proof['uuid']
        assert state.store._one('SELECT COUNT(*) n FROM agent_turns')['n'] == 1
        assert (await client.post(prefix + '/interrupt', json={})).status_code == 200
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_strong_unknown_missing_receipt_never_redelivers_and_mixed_rejection_is_atomic(network, monkeypatch):
    _, calls = await setup_native(network, monkeypatch)
    monkeypatch.setattr('atlas_router.main.steering_capability', lambda backend, journal, node_id, boot_id: {
        **CAP, 'targetNodeId': node_id, 'targetBootId': boot_id})
    client, app, routers, *_ = network
    state = app.state.gate
    sid, prefix = await active(client)
    body = payload(attachments=[{'kind': 'image', 'data': 'YWJj', 'mediaType': 'image/png'}])
    mixed = await client.post(prefix + '/steer', json=body, headers=headers_for(client, prefix, 'mixed'))
    assert mixed.status_code == 422 and mixed.json()['error']['code'] == 'steer_attachments_unsupported'
    assert not calls
    saved = state.store._one("SELECT * FROM router_operations WHERE path LIKE '%/steer'")
    assert saved['state'] == 'complete' and json.loads(saved['steer'])['state'] == 'not_applied'
    # The rejected intent remains rejected if capability later becomes available.
    monkeypatch.setattr('atlas_router.main.steering_capability', lambda backend, journal, node_id, boot_id: {
        **CAP, 'targetNodeId': node_id, 'targetBootId': boot_id, 'attachments': {'images': True, 'documents': True}})
    repeated = await client.post(prefix + '/steer', json=body, headers=headers_for(client, prefix, 'mixed'))
    assert repeated.status_code == 422 and repeated.json()['steer']['state'] == 'not_applied'
    assert not calls
    binding = state.store._one('SELECT * FROM router_bindings WHERE id=?', (sid,))
    upstream = state.nodes.clients[binding['node_id']]
    original = upstream.request
    attempts = []

    async def lost(method, path, **kwargs):
        if method == 'POST' and path.endswith('/steer'):
            attempts.append(path)
            raise httpx.ReadError('lost initial request ACK')
        return await original(method, path, **kwargs)

    monkeypatch.setattr(upstream, 'request', lost)
    body = payload()
    for _ in range(3):
        unknown = await client.post(prefix + '/steer', json=body, headers=headers_for(client, prefix, 'lost-key'))
        assert unknown.status_code == 503, unknown.text
    assert len(attempts) == 1 and not calls
    pending = await client.get(prefix + '/commands', params={'requestId': 'lost-key'})
    assert pending.status_code == 202 and pending.json()['steer']['state'] == 'unknown'
    assert (await client.post(prefix + '/interrupt', json={})).status_code == 200


async def test_profile_exact_nodes_refresh_public_discovery_and_legacy_compatibility(network, monkeypatch):
    from atlas_gate.gate.state import GateState
    client, app, routers, *_ = network
    state = app.state.gate
    node = state.nodes.config['a']
    fake = SimpleNamespace(nodes=SimpleNamespace(config={'a': node, 'b': node}, snapshots={
        'a': {'bootId': 'one', 'models': [{'value': 'm'}], 'steering': CAP},
        'b': {'bootId': 'two', 'models': [{'value': 'm'}]}}))
    projected = GateState.project_steering(fake, 'test-org', 'claude-sub', 'm')
    assert projected['nodes']['a']['durableAbsorption'] is True
    assert projected['nodes']['b']['supported'] is False
    assert 'durableAbsorption' not in projected
    fake.nodes.snapshots['a']['steering'] = {'supported': False}
    assert GateState.project_steering(fake, 'test-org', 'claude-sub', 'm')['nodes']['a']['supported'] is False
    async def old_steer(self, text):
        return 'old-sdk-identity'
    from atlas_router.backends.stub import StubSession
    monkeypatch.setattr(StubSession, 'steer', old_steer)
    sid, prefix = await active(client)
    # An old text-only request requires no new negotiated promise.
    legacy = await client.post(prefix + '/steer', json={'text': 'legacy'})
    assert legacy.status_code == 200, legacy.text
    client.steer_headers[prefix]['X-Atlas-Steer-Target-Boot'] = state.store._one(
        'SELECT boot_id FROM router_bindings WHERE id=?', (sid,))['boot_id']
    unavailable = await client.post(prefix + '/steer', json=payload(), headers=headers_for(client, prefix, 'new'))
    assert unavailable.status_code == 422 and unavailable.json()['error']['code'] == 'steering_unsupported'
    monkeypatch.setattr(routers[0].state.backend, 'steering_capabilities', lambda: True, raising=False)
    await state.context_windows.poll()
    private = await client.get('/harness/settings')
    doc = private.json()
    from atlas_gate.gate.crypto import verify_profile
    assert verify_profile(doc['profile_jws'], doc['profile_signing_key']) == doc['profile']
    model = next(m for m in doc['profile']['models'] if m['model'] == 'stub-fast')
    assert model['steering']['scope'] == 'pinned_node'
    assert model['steering']['nodes']['a']['durableAbsorption'] is True
    assert model['steering']['nodes']['b']['supported'] is False
    public = await client.get('/harness/discovery')
    assert 'steering' not in public.text and 'nodeId' not in public.text
    monkeypatch.setattr(routers[0].state.backend, 'steering_capabilities', lambda: False)
    await state.context_windows.poll()
    refreshed = await client.get('/harness/settings', headers={'If-None-Match': private.headers['etag']})
    assert refreshed.status_code == 200
    model = next(m for m in refreshed.json()['profile']['models'] if m['model'] == 'stub-fast')
    assert model['steering']['nodes']['a']['supported'] is False
    from atlas_gate.gate.model_contracts import ModelContract, ProfileModel
    assert 'steering' not in ModelContract.model_fields and 'steering' in ProfileModel.model_fields
    assert (await client.post(prefix + '/interrupt', json={})).status_code == 200


async def test_contract_headers_and_original_boot_are_required_without_auto_rebind(network, monkeypatch):
    _, calls = await setup_native(network, monkeypatch)
    client, app, routers, *_ = network
    sid, prefix = await active(client)
    body = payload()
    for headers in ({'Idempotency-Key': 'missing'},
                    {'Idempotency-Key': 'missing-boot', 'X-Atlas-Steer-Contract': '1'},
                    {**headers_for(client, prefix, 'bad-version'), 'X-Atlas-Steer-Contract': '2'}):
        result = await client.post(prefix + '/steer', json=body, headers=headers)
        assert result.status_code == 422 and result.json()['error']['code'] == 'steer_contract_required'
    disguised = await client.post(prefix + '/steer', json={'text': 'must not become legacy'},
                                  headers=headers_for(client, prefix, 'missing-ids'))
    assert disguised.status_code == 422 and not calls
    state = app.state.gate
    original_binding = state.store._one('SELECT * FROM router_bindings WHERE id=?', (sid,))
    original_probe = state.nodes.probe
    async def rebooted(node):
        value = await original_probe(node)
        return {**value, 'bootId': 'new-boot'} if node == original_binding['node_id'] else value
    monkeypatch.setattr(state.nodes, 'probe', rebooted)
    result = await client.post(prefix + '/steer', json=body, headers=headers_for(client, prefix, 'old-boot'))
    assert result.status_code == 409 and result.json()['steer']['state'] == 'not_applied'
    assert result.json()['steer']['targetBootId'] == original_binding['boot_id'] and not calls
    assert state.store._one('SELECT boot_id FROM router_bindings WHERE id=?', (sid,))['boot_id'] == original_binding['boot_id']
    monkeypatch.setattr(state.nodes, 'probe', original_probe)
    accepted = await client.post(prefix + '/steer', json=payload(), headers=headers_for(client, prefix, 'accepted-boot'))
    assert accepted.status_code == 200 and len(calls) == 1
    wrong = {**headers_for(client, prefix, 'accepted-boot'), 'X-Atlas-Steer-Target-Boot': 'changed',
             'Content-Type': 'application/json'}
    conflict = await client.post(prefix + '/steer', content=accepted.request.content, headers=wrong)
    assert conflict.status_code == 409 and len(calls) == 1, conflict.text
    read = await client.get(prefix + '/commands', params={'requestId': 'accepted-boot'},
                            headers=headers_for(client, prefix))
    assert read.status_code == 200 and read.json()['steer']['targetBootId'] == original_binding['boot_id']
    state.store._exec('UPDATE router_bindings SET boot_id=? WHERE id=?', ('subsequent-boot', sid))
    historical = await client.get(prefix + '/commands', params={'requestId': 'accepted-boot'},
                                   headers=headers_for(client, prefix))
    assert historical.status_code == 200 and historical.json()['steer']['targetBootId'] == original_binding['boot_id']
    replay = await client.post(prefix + '/steer', content=accepted.request.content,
                              headers={**headers_for(client, prefix, 'accepted-boot'), 'Content-Type': 'application/json'})
    assert replay.status_code == 200 and len(calls) == 1
    wrong_read = await client.get(prefix + '/commands', params={'requestId': 'accepted-boot'}, headers=wrong)
    assert wrong_read.status_code == 409
    state.store._exec('UPDATE router_bindings SET boot_id=? WHERE id=?', (original_binding['boot_id'], sid))
    assert (await client.post(prefix + '/interrupt', json={})).status_code == 200


async def test_different_key_cannot_turn_original_unknown_client_identity_into_not_applied(network, monkeypatch):
    _, calls = await setup_native(network, monkeypatch)
    client, app, routers, *_ = network
    sid, prefix = await active(client)
    state = app.state.gate
    binding = state.store._one('SELECT * FROM router_bindings WHERE id=?', (sid,))
    upstream = state.nodes.clients[binding['node_id']]
    original = upstream.request
    async def lost(method, path, **kwargs):
        if method == 'POST' and path.endswith('/steer'):
            raise httpx.ReadError('unknown initial dispatch')
        return await original(method, path, **kwargs)
    monkeypatch.setattr(upstream, 'request', lost)
    body = payload()
    first = await client.post(prefix + '/steer', json=body, headers=headers_for(client, prefix, 'original-key'))
    assert first.status_code == 503
    for invalid in ({'Idempotency-Key': 'original-key'},
                    {'Idempotency-Key': 'original-key', 'X-Atlas-Steer-Contract': '1'}):
        malformed = await client.post(prefix + '/steer', json=body, headers=invalid)
        assert malformed.status_code == 422 and malformed.json()['error']['state'] == 'unknown'
    second = await client.post(prefix + '/steer', json=body, headers={
        **headers_for(client, prefix, 'new-key'), 'X-Atlas-Steer-Target-Boot': 'stale-target'})
    assert second.status_code == 409
    assert second.json()['error']['code'] == 'steer_message_identity_conflict'
    assert second.json()['error']['state'] == 'unknown'
    assert second.json()['error']['steer']['state'] == 'unknown'
    assert not calls
    assert state.store._one("SELECT COUNT(*) n FROM router_operations WHERE steer IS NOT NULL")['n'] == 1
    assert (await client.post(prefix + '/interrupt', json={})).status_code == 200


async def test_unknown_inbox_identity_cannot_move_to_another_session_or_model(network, monkeypatch):
    import copy
    _, calls = await setup_native(network, monkeypatch)
    client, app, routers, *_ = network
    state = app.state.gate
    sid, prefix = await active(client)
    binding = state.store._one('SELECT * FROM router_bindings WHERE id=?', (sid,))
    upstream = state.nodes.clients[binding['node_id']]
    original = upstream.request
    async def lost(method, path, **kwargs):
        if method == 'POST' and path.endswith('/steer'):
            raise httpx.ReadError('original admission remains unknown')
        return await original(method, path, **kwargs)
    monkeypatch.setattr(upstream, 'request', lost)
    body = payload()
    first = await client.post(prefix + '/steer', json=body, headers=headers_for(client, prefix, 'original-card'))
    assert first.status_code == 503
    model = next(m for m in state.orgs['test-org'].models if m.route_id == binding['route'] and m.model == 'stub-fast')
    state.orgs['test-org'].models.append(model.model_copy(update={'model': 'stub-slow'}))
    projection = copy.deepcopy(next(m for m in state.bodies['test-org']['models'] if m['route_id'] == binding['route'] and m['model'] == 'stub-fast'))
    projection['model'] = 'stub-slow'
    state.bodies['test-org']['models'].append(projection)
    created = await client.post('/harness/agent/sessions', json={'model': 'stub-slow', 'tools': [
        {'name': 'echo', 'parameters': {'type': 'object'}}]})
    assert created.status_code == 200, created.text
    moved = '/harness/agent/sessions/' + created.json()['id']
    await capture_target(client, moved)
    assert (await client.post(moved + '/prompt?mode=async', json={'id': 'another-model-turn', 'text': 'call:echo'})).status_code == 202
    candidate = dict(body, expectedTurnId='another-model-turn')
    conflict = await client.post(moved + '/steer', json=candidate, headers=headers_for(client, moved, 'new-card-key'))
    assert conflict.status_code == 409 and conflict.json()['error']['state'] == 'unknown'
    proof = conflict.json()['error']['steer']
    assert proof['sessionId'] == sid and proof['turnId'] == 'original' and proof['state'] == 'unknown'
    assert not calls and state.store._one('SELECT COUNT(*) n FROM router_operations WHERE steer IS NOT NULL')['n'] == 1
    # An explicit ordinary follow-up is a separate prompt intent, not another strong UUID admission.
    assert (await client.post(prefix + '/interrupt', json={})).status_code == 200
    assert (await client.post(moved + '/interrupt', json={})).status_code == 200


async def test_old_connector_header_loss_is_rejected_during_metadata_before_native_post(network, monkeypatch, tmp_path):
    _, calls = await setup_native(network, monkeypatch)
    client, app, routers, *_ = network
    state = app.state.gate
    task, connector = await connector_node(client, state, tmp_path, return_connector=True)
    original = connector.router.request
    native_posts = []
    async def legacy_headers(method, path, **kwargs):
        outgoing = dict(kwargs.get('headers') or {})
        outgoing.pop('x-atlas-steer-contract', None)
        outgoing.pop('X-Atlas-Steer-Contract', None)
        kwargs['headers'] = outgoing
        if method == 'POST' and path.endswith('/steer'):
            native_posts.append(path)
        return await original(method, path, **kwargs)
    monkeypatch.setattr(connector.router, 'request', legacy_headers)
    try:
        created = await client.post('/harness/agent/sessions', json={'model': 'stub-fast', 'tools': [
            {'name': 'echo', 'parameters': {'type': 'object'}}]})
        assert created.status_code == 200 and created.json()['steering']['supported'] is False, created.text
        sid = created.json()['id'];prefix = '/harness/agent/sessions/' + sid
        binding = state.store._one('SELECT * FROM router_bindings WHERE id=?', (sid,))
        assert (await client.get(prefix + '/sdk-tools')).json()['steering']['supported'] is False
        assert (await client.post(prefix + '/prompt?mode=async', json={'id': 'original', 'text': 'call:echo'})).status_code == 202
        # Simulate a capability cached before the connector rollback.
        headers = {'Idempotency-Key': 'old-connector', 'X-Atlas-Steer-Contract': '1',
                   'X-Atlas-Steer-Target-Boot': binding['boot_id']}
        rejected = await client.post(prefix + '/steer', json=payload(), headers=headers)
        assert rejected.status_code == 409 and rejected.json()['steer']['state'] == 'not_applied', rejected.text
        assert rejected.json()['error']['code'] == 'steer_target_identity_required'
        assert not calls and not native_posts
        assert (await client.post(prefix + '/interrupt', json={})).status_code == 200
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_lost_native_ack_is_observed_by_key_without_redelivery_and_stop_is_stronger(network, monkeypatch):
    consumed, calls = await setup_native(network, monkeypatch)
    client, app, routers, _, creds, other = network
    state = app.state.gate
    sid, prefix = await active(client)
    binding = state.store._one('SELECT * FROM router_bindings WHERE id=?', (sid,))
    upstream = state.nodes.clients[binding['node_id']]
    original = upstream.request
    lost = False

    async def lose_ack(method, path, **kwargs):
        nonlocal lost
        response = await original(method, path, **kwargs)
        if method == 'POST' and path.endswith('/steer') and not lost:
            lost = True
            raise httpx.ReadError('ACK lost after Router durable acceptance')
        return response

    monkeypatch.setattr(upstream, 'request', lose_ack)
    body = payload()
    # Owner/policy are checked before contacting the original Router.
    foreign = await client.post(prefix + '/steer', json=body, headers={
        **headers_for(client, prefix, 'foreign'), 'Authorization': 'Bearer ' + other['device_token']})
    assert foreign.status_code == 404 and not calls
    denied = await client.post(prefix + '/steer', json=body, headers={
        **headers_for(client, prefix, 'class'), 'X-Harness-Data-Class': 'not-an-org-class'})
    assert denied.status_code == 403 and not calls
    first = await client.post(prefix + '/steer', json=body, headers=headers_for(client, prefix, 'lost-ack'))
    assert first.status_code == 503 and len(calls) == 1
    observed = await client.get(prefix + '/commands', params={'requestId': 'lost-ack'})
    assert observed.status_code == 200 and observed.json()['steer']['state'] == 'accepted'
    # Recreate the Operations manager over durable Gate state, not a memory lookup.
    state.operations = Operations(state.store)
    consumed[body['clientMessageId']]()
    observed = await client.get(prefix + '/commands', params={'requestId': 'lost-ack'})
    assert observed.json()['steer']['state'] == 'absorbed'
    replay = await client.post(prefix + '/steer', json=body, headers=headers_for(client, prefix, 'lost-ack'))
    assert replay.json()['steer']['state'] == 'absorbed' and len(calls) == 1
    assert (await client.post(prefix + '/interrupt', json={})).status_code == 200
    await events(client, sid, 'original', True)
    next_turn = await client.post(prefix + '/prompt?mode=async', json={'id': 'next', 'text': 'call:echo'})
    assert next_turn.status_code == 202
    await events(client, sid, 'next')
    late = await client.post(prefix + '/steer', json=payload(), headers=headers_for(client, prefix, 'after-stop'))
    assert late.status_code == 409, late.text
    assert late.json()['steer']['state'] == 'not_applied' and len(calls) == 1
    old_receipt = await client.get(prefix + '/commands', params={'requestId': 'lost-ack'})
    assert old_receipt.json()['steer']['turnId'] == 'original'
    assert old_receipt.json()['steer']['state'] == 'absorbed'
    assert (await client.post(prefix + '/interrupt', json={})).status_code == 200


async def test_raw_utf8_body_hash_preserves_unicode_omission_and_serialization(network, monkeypatch):
    _, calls = await setup_native(network, monkeypatch)
    client, app, routers, *_ = network
    sid, prefix = await active(client)
    body = payload(text='К🙂 é')
    del body['attachments']
    raw = json.dumps(body, ensure_ascii=False, indent=2).encode('utf-8')
    headers = headers_for(client, prefix, 'raw-outbox', **{'Content-Type': 'application/json'})
    first = await client.post(prefix + '/steer', content=raw, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()['steer']['bodySha256'] == hashlib.sha256(raw).hexdigest()
    assert first.json()['steer']['bodySha256'] != body_sha256(body)
    replay = await client.post(prefix + '/steer', content=raw, headers=headers)
    assert replay.json()['steer'] == first.json()['steer'] and len(calls) == 1
    for changed in (json.dumps(body).encode(), json.dumps(dict(body, attachments=[])).encode()):
        conflict = await client.post(prefix + '/steer', content=changed, headers=headers)
        assert conflict.status_code == 409, conflict.text
    assert len(calls) == 1
    assert (await client.post(prefix + '/interrupt', json={})).status_code == 200


async def test_strong_metadata_preflight_never_cold_opens_sdk(network, monkeypatch):
    _, calls = await setup_native(network, monkeypatch)
    client, app, routers, *_ = network
    sid = await create(client)
    prefix = '/harness/agent/sessions/' + sid
    await capture_target(client, prefix)
    binding = app.state.gate.store._one('SELECT * FROM router_bindings WHERE id=?', (sid,))
    router = routers[0 if binding['node_id'] == 'a' else 1]
    await router.state.registry.remove(binding['local_id'], persist_close=False)
    opened = []
    async def forbidden(*args, **kwargs):
        opened.append(True)
        raise AssertionError('strong steering preflight cold-opened the SDK')
    monkeypatch.setattr(router.state.backend, 'open_session', forbidden)
    result = await client.post(prefix + '/steer', json=payload(), headers=headers_for(client, prefix, 'missing-runtime'))
    assert result.status_code == 409, result.text
    assert result.json()['error']['state'] == 'not_applied'
    assert not opened and not calls
    saved = app.state.gate.store._one("SELECT * FROM router_operations WHERE path LIKE '%/steer'")
    assert saved['state'] == 'complete' and json.loads(saved['steer'])['state'] == 'not_applied'


async def test_supported_caption_and_document_share_one_native_identity(network, monkeypatch):
    consumed, calls = await setup_native(network, monkeypatch)
    client, app, routers, *_ = network
    sid, prefix = await active(client)
    body = payload(text='caption', attachments=[{'kind': 'text', 'filename': 'note.txt',
                                                'data': 'содержимое документа'}])
    result = await client.post(prefix + '/steer', json=body, headers=headers_for(client, prefix, 'caption-files'))
    assert result.status_code == 200, result.text
    assert len(calls) == 1 and calls[0][3] == 'caption'
    assert len(calls[0][4]) == 1 and calls[0][4][0]['data'] == 'содержимое документа'
    consumed[body['clientMessageId']]()
    receipt = await client.get(prefix + '/commands', params={'requestId': 'caption-files'})
    assert receipt.json()['steer']['state'] == 'absorbed'
    assert receipt.json()['steer']['uuid'] == calls[0][1]
    assert (await client.post(prefix + '/interrupt', json={})).status_code == 200


async def test_strong_input_does_not_inherit_legacy_prompt_upload_ceilings(network, monkeypatch):
    _, calls = await setup_native(network, monkeypatch)
    client, app, routers, *_ = network
    sid, prefix = await active(client)
    nodes = app.state.gate.nodes
    original = nodes.probe
    async def low_legacy_limits(node):
        value = await original(node)
        return {**value, 'attachments': {**value['attachments'], 'maxFiles': 1, 'maxRawBytes': 1,
                                        'maxTotalBytes': 1, 'maxTextChars': 1}}
    monkeypatch.setattr(nodes, 'probe', low_legacy_limits)
    body = payload(text='caption beyond legacy prompt ceiling', attachments=[
        {'kind': 'text', 'filename': 'first.txt', 'data': 'whole document'},
        {'kind': 'text', 'filename': 'second.txt', 'data': 'second whole document'}])
    result = await client.post(prefix + '/steer', json=body, headers=headers_for(client, prefix, 'provider-bounds'))
    assert result.status_code == 200, result.text
    assert len(calls) == 1 and len(calls[0][4]) == 2
    assert (await client.post(prefix + '/interrupt', json={})).status_code == 200


async def test_mismatched_native_ack_cannot_confirm_another_body(network, monkeypatch):
    _, calls = await setup_native(network, monkeypatch)
    client, app, routers, *_ = network
    sid, prefix = await active(client)
    state = app.state.gate
    binding = state.store._one('SELECT * FROM router_bindings WHERE id=?', (sid,))
    upstream = state.nodes.clients[binding['node_id']]
    original = upstream.request
    async def wrong_proof(method, path, **kwargs):
        response = await original(method, path, **kwargs)
        if method == 'POST' and path.endswith('/steer'):
            doc = response.json()
            doc['steer']['bodySha256'] = '0' * 64
            return httpx.Response(200, json=doc)
        return response
    monkeypatch.setattr(upstream, 'request', wrong_proof)
    result = await client.post(prefix + '/steer', json=payload(), headers=headers_for(client, prefix, 'bad-proof'))
    assert result.status_code == 503 and result.json()['error']['code'] == 'command_outcome_unknown'
    assert len(calls) == 1
    current = await client.get(prefix + '/commands', params={'requestId': 'bad-proof'})
    assert current.status_code == 200 and current.json()['steer']['state'] == 'accepted'
    assert len(calls) == 1
    assert (await client.post(prefix + '/interrupt', json={})).status_code == 200


async def test_absorbed_event_is_readable_when_original_http_receipt_remains_unknown(network, monkeypatch):
    consumed, calls = await setup_native(network, monkeypatch)
    client, app, routers, *_ = network
    sid, prefix = await active(client)
    state = app.state.gate
    binding = state.store._one('SELECT * FROM router_bindings WHERE id=?', (sid,))
    upstream = state.nodes.clients[binding['node_id']]
    original = upstream.request
    async def unavailable(method, path, **kwargs):
        if path.startswith('/v1/commands/'):
            return httpx.Response(200, json={'state': 'unknown'})
        response = await original(method, path, **kwargs)
        if method == 'POST' and path.endswith('/steer'):
            raise httpx.ReadError('lost original HTTP receipt')
        return response
    monkeypatch.setattr(upstream, 'request', unavailable)
    body = payload()
    first = await client.post(prefix + '/steer', json=body, headers=headers_for(client, prefix, 'event-only'))
    assert first.status_code == 503 and len(calls) == 1
    consumed[body['clientMessageId']]()
    await events(client, sid, 'original')
    receipt = await client.get(prefix + '/commands', params={'requestId': 'event-only'})
    assert receipt.status_code == 202
    assert receipt.json()['steer']['state'] == 'absorbed'
    assert receipt.json()['receiptState'] == 'unknown'
    assert (await client.post(prefix + '/interrupt', json={})).status_code == 200
