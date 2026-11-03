"""Isolated Gate24 -> Router23 first-prompt reconciliation, no native SDK."""
import json

from atlas_gate.gate.operations import body_sha256
from .test_network_gate import network


async def test_virgin_unknown_first_prompt_alias_plan_seal_and_late_post(network, monkeypatch):
    c, app, routers, _, _, foreign = network
    state = app.state.gate
    for node in routers:
        # Exercise the actual Codex virgin journal branch with the deterministic
        # backend. Any prompt/tool execution below is a failure of the test.
        monkeypatch.setattr(node.state.backend, 'name', 'codex')
        async def authenticated_stub():
            return {'loggedIn':True}
        monkeypatch.setattr(node.state.cli, 'auth_status', authenticated_stub)
    created = await c.post('/harness/agent/sessions',json={'system':'test','model':'stub-fast',
        'tools':[{'name':'echo','description':'echo','parameters':{'type':'object'}}]})
    assert created.status_code==200,created.text
    assert 'sdkSessionId' not in created.json()
    sid = created.json()['id']
    binding = state.store._one('SELECT * FROM router_bindings WHERE id=?', (sid,))
    owner = routers[0 if binding['node_id']=='a' else 1]
    session = owner.state.registry.get(binding['local_id']).session
    async def forbidden_prompt(*args, **kwargs):
        raise AssertionError('Recovery must not execute any prompt')
        yield  # async generator contract
    monkeypatch.setattr(session, 'prompt', forbidden_prompt)
    assert state.store._one('SELECT COUNT(*) n FROM router_operations')['n']==0
    assert owner.state.journal.db.execute('SELECT COUNT(*) FROM turns').fetchone()[0]==0
    observation = (await state.nodes.request(binding['node_id'], 'GET', '/v1/sessions/'+binding['local_id']+'/reconcile')).json()
    assert observation['inputRecovery']['state']=='empty_confirmed'
    assert observation['inputRecovery']['generation']==0
    listing = await c.get('/harness/agent/sessions')
    assert listing.status_code==200
    info = next(x for x in listing.json()['sessions'] if x['id']==sid)
    assert info['sdkSessionId']==binding['sdk_alias'] and observation['sdkSessionId'] not in listing.text
    foreign_list = await c.get('/harness/agent/sessions', headers={'Authorization':'Bearer '+foreign['device_token']})
    assert sid not in foreign_list.text
    payload = {'id':'first-unknown-turn','text':'must never execute'}
    async def post(path, body, key):
        r = await c.post(path,json=body,headers={'Idempotency-Key':key})
        assert r.status_code==200,r.text
        return r.json()
    inv = await post('/harness/recovery/inventories', {'version':1,'conversationId':'chat',
        'branchId':'branch','clientStoreEpoch':'store','checkpointInventoryVersion':1,
        'sources':[{'sourceRef':'source','sourceRunId':'run','sessionId':sid}]},'inv')
    manifest = {'version':1,'inventoryId':inv['inventoryId'],'sourceRef':'source','sourceRunId':'run',
        'conversationId':'chat','branchId':'branch','sourceCheckpointVersion':1,
        'source':{'gateRoot':state.public_url,'sessionId':sid,'historyAlias':info['sdkSessionId'],
                  'routeId':binding['route'],'model':binding['model']},
        'operations':[{'kind':'prompt','requestId':'first-unknown-key','turnId':payload['id'],
                       'bodySha256':body_sha256(payload),'localState':'outcome_unknown'}],
        'permissions':{'allowedRecovery':['read_receipt','resolve_source_checkpoint'], 'businessReplayAllowed':False}}
    plan = await post('/harness/recovery/cases',manifest,'case')
    action = next(a for a in plan['actions'] if a['kind']=='seal_prompt')
    path='/harness/recovery/cases/'+plan['recoveryId']
    apply = {'version':1,'planId':plan['planId'],'expectedRevision':plan['revision'],
             'sourceManifestSha256':plan['sourceManifestSha256'],'branchFence':plan['branchFence'],
             'actionIds':[action['id']]}
    sealed = await post(path+'/apply',apply,'seal-first')
    assert sealed['steps'][0]['outcome']=='not_sent'
    assert await post(path+'/apply',apply,'seal-first')==sealed
    observed = (await c.get(path)).json()
    assert observed['operations'][0]['outcome']=='sealed_not_sent'
    assert any(a['kind']=='resolve_source_checkpoint' and a['executor']=='harness' for a in observed['actions'])
    assert (await c.get(path,headers={'Authorization':'Bearer '+foreign['device_token']})).status_code==404
    late = await c.post('/harness/agent/sessions/'+sid+'/prompt?mode=async',json=payload,
                        headers={'Idempotency-Key':'first-unknown-key'})
    assert late.status_code==409 and late.json()['error']['code']=='command_not_sent',late.text
    assert owner.state.journal.db.execute('SELECT COUNT(*) FROM turns').fetchone()[0]==0
    assert state.store._one('SELECT COUNT(*) n FROM router_operations')['n']==0
    fixture={'sessionCreatedHasAlias':False,'listing':listing.json(),'manifest':manifest,'plan':plan,
             'applyRequest':apply,'applyResult':sealed,'observed':observed,
             'lateOriginal':{'status':late.status_code,'body':late.json()},
             'promptAndToolExecutions':0,'nativeSdkCalls':0,'routerObservation':observation}
    import os
    from pathlib import Path
    if os.environ.get('ATLAS_RECON226_FIXTURE'):
        target=Path(os.environ['ATLAS_RECON226_FIXTURE'])
        target.write_text(json.dumps(fixture,indent=2),encoding='utf-8')
