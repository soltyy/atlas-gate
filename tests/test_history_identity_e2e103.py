from atlas_gate.gate.crypto import verify_profile
from atlas_gate.gate.operations import body_sha256
from .test_network_gate import network
from .history103_fixture import seed


async def test_profile_proof_and_coordinator_read_existing_terminal_without_replay(network):
    c,app,routers,_,credentials,_=network
    state=app.state.gate
    setup=await seed(c,state)
    settings=(await c.get('/harness/settings')).json()
    profile=verify_profile(settings['profile_jws'],credentials['profile_signing_key'])
    capability=profile['sessionRecoveryCoordinator']['sourceHistoryIdentity']
    assert capability['version']==1 and capability['readOnly'] and capability['businessReplayAllowed'] is False
    assert '/harness/agent/sessions/{sid}/history-identity' in (await c.get('/openapi.json')).json()['paths']
    baseline=[r.state.journal.db.execute('SELECT COUNT(*) FROM turns').fetchone()[0] for r in routers]
    ops=state.store._one('SELECT COUNT(*) n FROM router_operations')['n']
    path=capability['pathTemplate'].replace('{sid}',setup['currentSID'])
    response=await c.get(path,params={'previousAlias':setup['previousAlias']})
    assert response.status_code==200,response.text
    proof=response.json()
    assert proof['continuity']['kind']=='same_native_history' and proof['historyAlias']==setup['historyAlias']
    async def post(path,body,key):
        result=await c.post(path,json=body,headers={'Idempotency-Key':key})
        assert result.status_code==200,result.text
        return result.json()
    inv=await post('/harness/recovery/inventories',{'version':1,'conversationId':'chat','branchId':'branch',
        'clientStoreEpoch':'store','checkpointInventoryVersion':1,
        'sources':[{'sourceRef':'current','sourceRunId':setup['sourceRunId'],'sessionId':setup['currentSID']}]},'inventory')
    manifest={'version':1,'inventoryId':inv['inventoryId'],'conversationId':'chat','branchId':'branch',
        'sourceRef':'current','sourceRunId':setup['sourceRunId'],'sourceCheckpointVersion':1,
        'source':{'gateRoot':proof['gateRoot'],'sessionId':proof['sessionId'],'historyAlias':proof['historyAlias'],
                  'routeId':proof['routeId'],'model':proof['model']},
        'operations':[{'kind':'prompt','requestId':setup['requestId'],'turnId':setup['turnId'],
            'bodySha256':body_sha256(setup['originalPromptBody'])}],
        'permissions':{'allowedRecovery':['read_receipt','resolve_source_checkpoint'],'businessReplayAllowed':False}}
    wrong={**manifest,'source':{**manifest['source'],'historyAlias':setup['previousAlias']}}
    refused=await c.post('/harness/recovery/cases',json=wrong,headers={'Idempotency-Key':'old-alias-case'})
    assert refused.status_code==409 and refused.json()['error']['code']=='recovery_source_conflict'
    inv2=await post('/harness/recovery/inventories',{'version':1,'conversationId':'chat','branchId':'branch',
        'clientStoreEpoch':'store','checkpointInventoryVersion':2,
        'sources':[{'sourceRef':'current','sourceRunId':setup['sourceRunId'],'sessionId':setup['currentSID']}]},'fresh-inventory')
    assert inv2['inventoryId']!=inv['inventoryId']
    assert inv2['branchFence']['recoveryGeneration']==inv['branchFence']['recoveryGeneration']+1
    manifest.update(inventoryId=inv2['inventoryId'],sourceCheckpointVersion=2)
    case=await post('/harness/recovery/cases',manifest,'case')
    assert case['operations'][0]['outcome']=='terminal_success'
    terminal=[e['body']['payload'] for e in case['evidence'] if e['body']['kind']=='prompt']
    assert len(terminal)==1 and terminal[0]['events'][-1]['ok'] is True
    assert [r.state.journal.db.execute('SELECT COUNT(*) FROM turns').fetchone()[0] for r in routers]==baseline
    assert state.store._one('SELECT COUNT(*) n FROM router_operations')['n']==ops
