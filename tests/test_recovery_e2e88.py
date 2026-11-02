"""Real standalone Gate -> Router104 HTTP, synthetic executor, no provider SDK."""
import asyncio

from atlas_gate.gate.operations import body_sha256
from .test_network_gate import network, create, events


async def test_old_result_after_stop_and_closed_binding_new_intent_without_tool_replay(network):
    c, app, routers, *_ = network
    state = app.state.gate
    sid = await create(c)
    prefix = '/harness/agent/sessions/' + sid
    prompt = {'id': 'source-turn', 'text': 'call:echo'}
    assert (await c.post(prefix + '/prompt?mode=async', json=prompt, headers={'Idempotency-Key': 'source-prompt'})).status_code == 202
    call = next(e for e in await events(c, sid, 'source-turn') if e['type'] == 'tool_call')
    output = {'callId': call['callId'], 'content': 'synthetic durable effect receipt'}
    result = await c.post(prefix + '/tool_result', json=output, headers={'Idempotency-Key': 'source-output'})
    assert result.status_code == 200
    await events(c, sid, 'source-turn', terminal=True)
    assert (await c.delete(prefix)).status_code == 200
    binding = state.store._one('SELECT * FROM router_bindings WHERE id=?', (sid,))
    old_usage = state.store._one("SELECT COUNT(*) n FROM usage WHERE kind='agent'")['n']

    async def post(path, body, key):
        r = await c.post(path, json=body, headers={'Idempotency-Key': key})
        assert r.status_code == 200, r.text
        return r.json()
    inventory = await post('/harness/recovery/inventories', {'version': 1,
        'conversationId': 'conversation', 'branchId': 'branch', 'clientStoreEpoch': 'store',
        'checkpointInventoryVersion': 1,
        'sources': [{'sourceRef': 'source', 'sourceRunId': 'old-run', 'sessionId': sid}]}, 'inventory')
    manifest = {'version': 1, 'inventoryId': inventory['inventoryId'], 'sourceRef': 'source',
        'conversationId': 'conversation', 'branchId': 'branch', 'sourceRunId': 'old-run', 'sourceCheckpointVersion': 2,
        'source': {'gateRoot': state.public_url, 'sessionId': sid, 'historyAlias': binding['sdk_alias'],
                   'routeId': binding['route'], 'model': binding['model']},
        'operations': [
            {'kind': 'prompt', 'requestId': 'source-prompt', 'turnId': prompt['id'],
             'bodySha256': body_sha256(prompt), 'localState': 'outcome_unknown'},
            {'kind': 'tool_result', 'requestId': 'source-output', 'turnId': prompt['id'], 'callId': output['callId'],
             'bodySha256': body_sha256(output), 'localState': 'output_durable', 'outputRef': 'local-output'}],
        'permissions': {'allowedRecovery': ['read_receipt', 'apply_saved_ack', 'resolve_source_checkpoint'], 'businessReplayAllowed': False}}
    plan = await post('/harness/recovery/cases', manifest, 'case')
    assert [o['outcome'] for o in plan['operations']] == ['terminal_success', 'accepted']
    path = '/harness/recovery/cases/' + plan['recoveryId']
    await post('/harness/recovery/inventories/' + inventory['inventoryId'] + '/seal', {'version': 1}, 'seal')
    current = (await c.get(path)).json()
    await post(path + '/cancel', {'version': 1, 'expectedRevision': current['revision'],
        'sourceManifestSha256': current['sourceManifestSha256'], 'branchFence': current['branchFence']}, 'cancel')
    # Stop does not revoke old receipts or historical local application.
    cancelled = (await c.get(path)).json()
    assert cancelled['state'] == 'cancelled' and cancelled['evidence']
    actions = [a for a in plan['actions'] if a['executor'] == 'harness']
    ack = {'version': 1, 'ackKey': 'late-ack', 'planId': plan['planId'], 'planRevision': plan['revision'],
        'sourceManifestSha256': plan['sourceManifestSha256'], 'clientStoreEpoch': 'store',
        'sourceRunId': 'old-run', 'sourceSessionId': sid, 'sourceCheckpointVersion': 2,
        'appliedActions': [{'actionId': a['id'], 'evidenceSha256': a['evidenceSha256']} for a in actions],
        'localCommitRef': 'local-commit', 'localCommitSha256': 'f' * 64}
    first = await c.post(path + '/ack', json=ack)
    assert first.status_code == 200 and first.json()['state'] == 'historical-applied', first.text
    assert (await c.post(path + '/ack', json=ack)).json() == first.json()

    inv2 = await post('/harness/recovery/inventories', {'version': 1, 'conversationId': 'conversation',
        'branchId': 'branch', 'clientStoreEpoch': 'store', 'checkpointInventoryVersion': 2, 'sources': []}, 'inventory-2')
    ready = await post('/harness/recovery/inventories/' + inv2['inventoryId'] + '/seal', {'version': 1}, 'seal-2')
    opened = await c.post('/harness/agent/sessions', json={'system': 'test', 'model': binding['model'],
        'tools': [{'name': 'echo', 'description': 'echo', 'parameters': {'type': 'object'}}],
        'resumeSessionId': binding['sdk_alias']}, headers={'Idempotency-Key': 'new-transport'})
    assert opened.status_code == 200 and opened.json()['resumed'] is True, opened.text
    new_sid = opened.json()['id']
    fence = ready['branchFence']
    headers = {'X-Atlas-Conversation-Id': 'conversation', 'X-Atlas-Branch-Id': 'branch',
        'X-Atlas-Recovery-Inventory': inv2['inventoryId'], 'X-Atlas-Recovery-Generation': str(fence['recoveryGeneration']),
        'X-Atlas-Control-Epoch': str(fence['controlEpoch']), 'Idempotency-Key': 'new-intent'}
    sent = await c.post(f'/harness/agent/sessions/{new_sid}/prompt?mode=async',
        json={'id': 'new-turn', 'text': 'new explicit intent'}, headers=headers)
    assert sent.status_code == 202, sent.text
    await events(c, new_sid, 'new-turn', terminal=True)
    assert state.store._one("SELECT COUNT(*) n FROM router_operations WHERE path LIKE '%/tool_result'")['n'] == 1
    native_calls = []
    for router in routers:
        native_calls.extend(row for row in router.state.journal.db.execute('SELECT body FROM events')
                            if '"type": "tool_call"' in row['body'])
    assert len(native_calls) == 1
    assert state.store._one("SELECT COUNT(*) n FROM usage WHERE kind='agent'")['n'] == old_usage + 1
    fresh_binding = state.store._one('SELECT * FROM router_bindings WHERE id=?', (new_sid,))
    assert fresh_binding['node_id'] == binding['node_id'] and fresh_binding['sdk_id'] == binding['sdk_id']


async def test_actual_gate_router_transport_apply_restores_only_same_history_once(network, monkeypatch):
    c, app, routers, *_ = network
    state = app.state.gate
    sid = await create(c)
    prompt = {'id': 'old-turn', 'text': 'inert completed source'}
    assert (await c.post(f'/harness/agent/sessions/{sid}/prompt?mode=async', json=prompt,
        headers={'Idempotency-Key': 'old-prompt'})).status_code == 202
    await events(c, sid, prompt['id'], terminal=True)
    binding = state.store._one('SELECT * FROM router_bindings WHERE id=?', (sid,))
    owner = routers[0 if binding['node_id'] == 'a' else 1]
    await owner.state.registry.remove(binding['local_id'], persist_close=False)
    native_open = owner.state.backend.open_session
    opened = []
    async def counted(*args, **kwargs):
        opened.append(True)
        return await native_open(*args, **kwargs)
    monkeypatch.setattr(owner.state.backend, 'open_session', counted)
    async def post(path, body, key):
        response = await c.post(path, json=body, headers={'Idempotency-Key': key})
        assert response.status_code == 200, response.text
        return response.json()
    inv = await post('/harness/recovery/inventories', {'version': 1, 'conversationId': 'chat', 'branchId': 'branch',
        'clientStoreEpoch': 'store', 'checkpointInventoryVersion': 1,
        'sources': [{'sourceRef': 'source', 'sourceRunId': 'old-run', 'sessionId': sid}]}, 'inventory')
    manifest = {'version': 1, 'inventoryId': inv['inventoryId'], 'sourceRef': 'source',
        'conversationId': 'chat', 'branchId': 'branch', 'sourceRunId': 'old-run', 'sourceCheckpointVersion': 1,
        'source': {'gateRoot': state.public_url, 'sessionId': sid, 'historyAlias': binding['sdk_alias'],
                   'routeId': binding['route'], 'model': binding['model']},
        'operations': [{'kind': 'prompt', 'requestId': 'old-prompt', 'turnId': prompt['id'],
                        'bodySha256': body_sha256(prompt), 'localState': 'outcome_unknown'}],
        'permissions': {'allowedRecovery': ['read_receipt', 'resolve_source_checkpoint', 'resume_transport'],
                        'businessReplayAllowed': False}}
    plan = await post('/harness/recovery/cases', manifest, 'case')
    await post('/harness/recovery/inventories/' + inv['inventoryId'] + '/seal', {'version': 1}, 'seal')
    path = '/harness/recovery/cases/' + plan['recoveryId']
    ack = {'version': 1, 'ackKey': 'client-ack', 'planId': plan['planId'], 'planRevision': plan['revision'],
        'sourceManifestSha256': plan['sourceManifestSha256'], 'clientStoreEpoch': 'store',
        'sourceRunId': 'old-run', 'sourceSessionId': sid, 'sourceCheckpointVersion': 1,
        'appliedActions': [{'actionId': a['id'], 'evidenceSha256': a['evidenceSha256']}
                          for a in plan['actions'] if a['executor'] == 'harness'],
        'localCommitRef': 'commit', 'localCommitSha256': 'f' * 64}
    assert (await c.post(path + '/ack', json=ack)).status_code == 200
    current = (await c.get(path)).json()
    native_actions = [a['id'] for a in current['actions'] if a['executor'] == 'gate' and a['kind'] == 'resume_transport']
    assert len(native_actions) == 1, current
    body = {'version': 1, 'planId': current['planId'], 'expectedRevision': current['revision'],
        'sourceManifestSha256': current['sourceManifestSha256'], 'branchFence': current['branchFence'],
        'actionIds': native_actions}
    accepted = await post(path + '/apply', body, 'native-apply')
    assert await post(path + '/apply', body, 'native-apply') == accepted
    assert opened == [True]
    resumed = owner.state.registry.get(binding['local_id'])
    assert resumed.session.resumed and resumed.session.session_id == binding['sdk_id']
    assert owner.state.journal.db.execute('SELECT COUNT(*) FROM turns').fetchone()[0] == 1
    final = (await c.get(path)).json()
    assert final['state'] == 'completed' and final['next']['businessDispatchAllowed'] is True, final
