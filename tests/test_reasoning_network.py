"""Gate -> two real Router HTTP surfaces; no subscription/business actions."""
import copy
import pytest
from .test_network_gate import network


CAPS={'version':1,'status':'supported','efforts':['low','medium','high'],
      'defaultEffort':'medium','disabled':False,'source':'test','resolvedModel':'stub-fast'}


async def configure(network,monkeypatch):
    client,app,routers,*_=network
    for router in routers:
        backend=router.state.backend
        original=backend.models
        async def models(original=original):
            doc=await original()
            doc=copy.deepcopy(doc)
            doc['models'][0]['reasoning']=dict(CAPS)
            return doc
        monkeypatch.setattr(backend,'models',models)
    await app.state.gate.nodes.refresh()
    return client,app,routers


async def test_gate_router_decision_default_reload_replay_and_owner(network,monkeypatch):
    from atlas_gate.reasoning import ReasoningPolicy
    client,app,routers=await configure(network,monkeypatch)
    state=app.state.gate
    model=next(m for m in state.orgs['test-org'].models if m.model=='stub-fast')
    model.reasoningPolicy=ReasoningPolicy(default={'effort':'low'})
    body={'model':'stub-fast','tools':[]}
    headers={'Idempotency-Key':'create-reasoning'}
    created=await client.post('/harness/agent/sessions',json=body,headers=headers)
    assert created.status_code==200,created.text
    original=created.json();sid=original['id'];prefix='/harness/agent/sessions/'+sid
    assert original['configuredReasoning']['configured']['effort']=='low'
    assert original['configuredReasoning']['source']=='gate_default'
    read=await client.get(prefix+'/reasoning')
    assert read.status_code==200,read.text
    assert read.json()['configuredReasoning']==original['configuredReasoning']
    prompt={'id':'pinned','text':'hello'}
    accepted=await client.post(prefix+'/prompt?mode=async',json=prompt)
    assert accepted.status_code==202,accepted.text
    for _ in range(100):
        events=(await client.get(prefix+'/turns/pinned/events',params={'wait':100})).json()
        if events['done']:break
    assert events['done'] and any(e['type']=='result' and e['configuredReasoning']['configured']['effort']=='low' for e in events['events'])
    model.reasoningPolicy=ReasoningPolicy(default={'effort':'high'})
    replay=await client.post('/harness/agent/sessions',json=body,headers=headers)
    assert replay.json()==original
    replay=await client.post(prefix+'/prompt?mode=async',json=prompt)
    assert replay.status_code==202,replay.text
    new=await client.post(prefix+'/prompt?mode=async',json={'id':'new','text':'hello'})
    assert new.status_code==409 and 'reasoning_rebind_required' in new.text
    foreign=await client.get(prefix+'/reasoning',headers={'Authorization':'Bearer '+network[-1]['device_token']})
    assert foreign.status_code==404
    assert (await client.delete(prefix)).status_code==200
    closed=await client.get(prefix+'/reasoning')
    assert closed.status_code==200 and closed.json()['configuredReasoning']==original['configuredReasoning']


async def test_rollout_old_node_rejected_before_create_and_legacy_unchanged(network,monkeypatch):
    client,app,routers=await configure(network,monkeypatch)
    state=app.state.gate
    # Simulate a pre-v1 Router snapshot, including refresh during admission.
    async def old_refresh():
        for info in state.nodes.snapshots.values():info.pop('reasoningSettings',None)
    monkeypatch.setattr(state.nodes,'refresh',old_refresh)
    await old_refresh()
    r=await client.post('/harness/agent/sessions',json={'model':'stub-fast','reasoning':{'effort':'high'}})
    assert r.status_code==422 and 'protocol_unsupported' in r.text
    assert all(len(r.state.registry)==0 for r in routers)
    role=await client.post('/harness/agent/sessions',json={'model':'stub-fast','agents':[
        {'name':'child','description':'d','prompt':'p','reasoning':{'effort':'low'}}]})
    assert role.status_code==422 and 'subagent_unsupported' in role.text
    assert all(len(r.state.registry)==0 for r in routers)
    legacy=await client.post('/harness/agent/sessions',json={'model':'stub-fast'})
    assert legacy.status_code==200,legacy.text
    assert legacy.json()['configuredReasoning'] is None
