import json
import pytest
from atlas_gate.gate.errors import GateError
from atlas_gate.gate.routing import Routing
from atlas_gate.gate.store import Store
from .test_routing import FakeNodes, device
from .test_network_gate import network


@pytest.mark.asyncio
async def test_pre_admission_login_wait_can_reuse_exact_create_after_login(tmp_path):
    store, nodes = Store(str(tmp_path/'gate.db')), FakeNodes(capacity=0)
    routing = Routing(store, nodes)
    for info in nodes.snapshots.values():
        info.update(ready=False, authentication={'state':'required'})
    with pytest.raises(GateError) as denied:
        await routing.create(device('d'), 'sub', 'claude', {}, 0, request_id='same')
    assert denied.value.code == 'sdk_auth_required' and denied.value.extra['outcome'] == 'not_sent'
    assert store._one('SELECT COUNT(*) n FROM router_bindings')['n'] == 0
    for info in nodes.snapshots.values(): info.update(ready=True, authentication={'state':'authenticated'})
    status, first = await routing.create(device('d'), 'sub', 'claude', {}, 0, request_id='same')
    _, repeat = await routing.create(device('d'), 'sub', 'claude', {}, 0, request_id='same')
    assert status == 200 and repeat == first
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('state,model,code', [('unknown','claude','sdk_auth_unknown'), ('authenticated','missing','model_unavailable')])
async def test_auth_unknown_and_catalog_mismatch_are_not_capacity(tmp_path,state,model,code):
    store, nodes = Store(str(tmp_path/'gate.db')), FakeNodes()
    for info in nodes.snapshots.values(): info.update(ready=state=='authenticated', authentication={'state':state})
    with pytest.raises(GateError) as error:
        await Routing(store,nodes).create(device('d'),'sub',model,{},0)
    assert error.value.code == code
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('history_present', [True, False])
@pytest.mark.parametrize('lost_ack', [False, True])
async def test_existing_create_path_replaces_closed_transport_after_login(network, monkeypatch, history_present, lost_ack):
    client, app, routers, settings, creds, other = network
    from atlas_router.backends.stub import StubBackend
    native = 'auth79-history'
    StubBackend.memories[native] = ['completed-tool-marker']
    state = app.state.gate
    state.store._exec('INSERT INTO legacy_router_history VALUES (?,?,?,?)', (native,'a',creds['device_id'],'test-org'))
    body = {'model':'stub-fast','system':'test','tools':[], 'resumeSessionId':native}
    first = await client.post('/harness/agent/sessions',json=body,headers={'Idempotency-Key':'old'})
    sid=first.json()['id']; binding=state.routing.get(sid,device=type('D',(),{'device_id':creds['device_id'],'org':'test-org'})())
    # Listing saves the opaque SDK alias, exactly as the existing client does.
    listed=(await client.get('/harness/agent/sessions')).json()['sessions']
    alias=next(s['sdkSessionId'] for s in listed if s['id']==sid)
    await client.delete('/harness/agent/sessions/'+sid)
    if not history_present: StubBackend.memories.pop(native,None)
    request=dict(body,resumeSessionId=alias,allowHistoryFallback=True)
    refresh=state.nodes.refresh
    async def logged_out(*args,**kwargs):
        await refresh(*args,**kwargs)
        state.nodes.snapshots['a'].update(ready=False,authentication={'state':'required'})
    monkeypatch.setattr(state.nodes,'refresh',logged_out)
    waiting=await client.post('/harness/agent/sessions',json=request,headers={'Idempotency-Key':'after-login'})
    assert waiting.status_code==503 and waiting.json()['error']['code']=='sdk_auth_required'
    monkeypatch.setattr(state.nodes,'refresh',refresh)
    node_request=state.nodes.request
    dropped=False
    async def lose_once(node,method,path,**kw):
        nonlocal dropped
        response=await node_request(node,method,path,**kw)
        if lost_ack and method=='POST' and path=='/v1/sessions' and not dropped:
            dropped=True
            raise GateError(503,'command_outcome_unknown','lost ACK')
        return response
    monkeypatch.setattr(state.nodes,'request',lose_once)
    recovered=await client.post('/harness/agent/sessions',json=request,headers={'Idempotency-Key':'after-login'})
    if lost_ack:
        assert recovered.status_code==503
        recovered=await client.post('/harness/agent/sessions',json=request,headers={'Idempotency-Key':'after-login'})
    assert recovered.status_code==200,recovered.text
    ack=recovered.json()
    assert ack['id']!=sid and ack['resumed'] is history_present
    assert ack['sessionRecovery']['mode']==('resumed' if history_present else 'history_fallback')
    assert ack['sessionRecovery']['previousSessionId']==sid
    assert not ack['sessionRecovery']['actionsReplayed']
    replay=await client.post('/harness/agent/sessions',json=request,headers={'Idempotency-Key':'after-login'})
    assert replay.content==recovered.content
    old=await client.post('/harness/agent/sessions',json=body,headers={'Idempotency-Key':'old'})
    assert old.content==first.content, old.text  # immutable original create receipt
    assert state.store._one("SELECT COUNT(*) n FROM router_operations WHERE method!='DELETE'")['n']==0
    assert all(not e.buffers for router in routers for e in router.state.registry._entries.values())
    if history_present: assert StubBackend.memories[native]==['completed-tool-marker']


@pytest.mark.asyncio
async def test_pending_old_turn_blocks_replacement_before_sdk_create(network):
    client,app,routers,settings,creds,other=network
    body={'model':'stub-fast','system':'test','tools':[]}
    sid=(await client.post('/harness/agent/sessions',json=body)).json()['id']
    state=app.state.gate
    binding=state.store._one('SELECT * FROM router_bindings WHERE id=?',(sid,))
    node=routers[0] if binding['node_id']=='a' else routers[1]
    node.state.journal.event(binding['local_id'],'old-turn',{'type':'session','sessionId':'pending-history','seq':1},'running',False)
    state.store._exec('UPDATE router_bindings SET sdk_id=? WHERE id=?',('pending-history',sid))
    request=dict(body,resumeSessionId=binding['sdk_alias'],allowHistoryFallback=True)
    before=sum(len(r.state.registry) for r in routers)
    result=await client.post('/harness/agent/sessions',json=request)
    assert result.status_code==409 and result.json()['error']['code']=='session_recovery_pending'
    assert sum(len(r.state.registry) for r in routers)==before
