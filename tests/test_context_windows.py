import json
import time
from types import SimpleNamespace

import pytest
from atlas_gate.gate.context_windows import ContextWindows, FRESH_SECONDS
from atlas_gate.gate.crypto import verify_profile
from atlas_gate.gate.store import Store
from .test_network_gate import network, create, events


def state_for(path):
    store = Store(str(path))
    catalog = [{'value':'gpt-6.1-sol','resolved':'gpt-6.1-sol'},{'value':'other','resolved':'other'}]
    nodes = SimpleNamespace(config={'node':SimpleNamespace(node_id='node',enabled=True,orgs=['org'],routes=['sub'])},
        catalog={'node':catalog},snapshots={'node':{'bootId':'boot','models':catalog,'observed':time.monotonic()}},revoked=lambda node:False)
    state = SimpleNamespace(store=store,nodes=nodes)
    state.context_windows = ContextWindows(state)
    return state


def binding(created=1,model='gpt-6.1-sol'):
    return {'id':'session','node_id':'node','boot_id':'boot','model':model,'created':created}


def observe(state,value,order=1,created=1,turn=1,model='gpt-6.1-sol'):
    return state.context_windows.observe(binding(created,model),{'model':model,'maxTokens':value},order,turn)


def projection(state,model='gpt-6.1-sol'):
    return state.context_windows.project('org','sub',model)


def test_authority_increase_decrease_order_and_persist(tmp_path):
    path=tmp_path/'windows.db';s=state_for(path)
    assert projection(s)['context_window'] is None
    assert observe(s,475000,10)
    assert observe(s,600000,20)
    assert not observe(s,258400,15)
    assert projection(s)['context_window']==600000
    assert observe(s,200000,30)
    assert projection(s)['context_window']==200000
    # An older SDK thread or replayed older turn cannot overwrite a newer thread observation.
    assert observe(s,475000,40,created=2)
    assert not observe(s,258400,50,created=1)
    assert not observe(s,258400,60,created=2,turn=0)
    assert projection(s,'other')['context_window'] is None
    assert s.context_windows.project('org','another-route','gpt-6.1-sol')['context_window'] is None
    s.store.close();s=state_for(path)
    assert projection(s)['context_window']==475000
    s.store.close()


def test_invalid_stale_unavailable_and_wrong_boot_model(tmp_path):
    s=state_for(tmp_path/'windows.db')
    assert observe(s,475000)
    assert not s.context_windows.observe(binding(),{'model':'other','maxTokens':900000},2,1)
    assert observe(s,0,3)
    assert projection(s)['context_window'] is None
    assert observe(s,475000,4)
    s.store._exec('UPDATE router_context_windows SET observed=?',(time.time()-FRESH_SECONDS-1,))
    assert projection(s)['context_status']=='stale' and projection(s)['context_window'] is None
    assert observe(s,475000,5)
    s.nodes.snapshots['node']['bootId']='new-boot'
    assert projection(s)['context_status']=='unavailable'
    assert not observe(s,900000,6)
    s.store.close()


def test_catalog_revision_and_full_effective_semantics(tmp_path):
    s=state_for(tmp_path/'windows.db')
    m=s.nodes.snapshots['node']['models'][0]
    m['contextWindow']={'source':'sdk','revision':2,'effectiveTokens':475000,'fullTokens':500000}
    s.context_windows.catalog()
    assert projection(s)['context_window']==475000 and projection(s)['context_full_window']==500000
    m['contextWindow'].update(revision=1,effectiveTokens=258400)
    s.context_windows.catalog();assert projection(s)['context_window']==475000
    assert not observe(s,258400,100)
    m['contextWindow']=None
    s.context_windows.catalog();assert projection(s)['context_window'] is None
    m['contextWindow']={'source':'sdk','revision':3,'effectiveTokens':475000,'fullTokens':500000}
    s.context_windows.catalog();assert projection(s)['context_window']==475000
    m['contextWindow'].update(revision=4,effectiveTokens=None)
    s.context_windows.catalog();assert projection(s)['context_window'] is None
    s.store.close()


@pytest.mark.asyncio
async def test_http_context_updates_same_signed_profile_discovery_admin_and_chat(network,monkeypatch):
    client,app,routers,settings,*_=network
    state=app.state.gate
    sid=await create(client)
    row=state.store._one('SELECT * FROM router_bindings WHERE id=?',(sid,))
    owner=routers[0 if row['node_id']=='a' else 1]
    # Only one eligible node in this route: another unknown source cannot promise a larger window.
    for node in state.nodes.config.values():
        if node.node_id!=row['node_id']:node.enabled=False
    entry=owner.state.registry.get(row['local_id'])
    window={'value':475000}
    async def actual():
        return {'model':'stub-fast','maxTokens':window['value'],'totalTokens':100,'percentage':1}
    monkeypatch.setattr(entry.session,'context_usage',actual)
    first=(await client.get('/harness/settings')).json()
    version=first['profile']['profile_version']
    agents=first['profile']['agents']
    old_etag=(await client.get('/harness/settings')).headers['etag']
    for value in (475000,200000,600000):
        window['value']=value
        # Background poll uses authenticated HTTP /v1/context, without user inference or session recreation.
        await state.context_windows.poll()
        fresh=await client.get('/harness/settings',headers={'If-None-Match':old_etag})
        assert fresh.status_code==200
        doc=fresh.json();profile=doc['profile']
        m=next(x for x in profile['models'] if x['model']=='stub-fast' and x['route_id']=='claude-sub')
        assert m['context_window']==value and m['context_semantics']=='effective'
        assert profile['profile_version']>version and profile['agents']==agents
        assert verify_profile(doc['profile_jws'],doc['profile_signing_key'])==profile
        public=(await client.get('/harness/discovery')).json()
        assert next(x for x in public['models'] if x['model']=='stub-fast')['context_window']==value
        admin=(await client.get('/harness/admin/orgs/test-org',headers={'X-Atlas-Token':'admin'})).json()
        assert admin['model_contracts']['claude-sub']['stub-fast']['context_window']==value
        current=state.store._one('SELECT * FROM router_bindings WHERE id=?',(sid,))
        assert current['local_id']==row['local_id'] and current['node_id']==row['node_id']
        version=profile['profile_version'];old_etag=fresh.headers['etag']
        await state.reload()
        assert (await client.get('/harness/settings',headers={'If-None-Match':old_etag})).status_code==304
    # Terminal SDK observation is wired too, not just explicit context requests.
    window['value']=475000
    assert (await client.post(f'/harness/agent/sessions/{sid}/prompt?mode=async',json={'id':'new-context','text':'hello'})).status_code==202
    await events(client,sid,'new-context',True)
    doc=(await client.get('/harness/settings')).json()
    assert next(x for x in doc['profile']['models'] if x['model']=='stub-fast')['context_window']==475000
    assert (await client.get(f'/harness/agent/sessions/{sid}/context')).json()['maxTokens']==475000
    assert state.store._one('SELECT COUNT(*) n FROM router_bindings')['n']==1
