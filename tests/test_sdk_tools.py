import base64
import json
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


async def test_http_sdk_files_direct_and_connector(image_network, monkeypatch):
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
    payload={'id':'upload','attachments':[{'kind':'file','filename':'book.xlsx','data':base64.b64encode(b'original excel bytes').decode()}]}
    response=await client.post(path+'/files',json=payload,headers={'Idempotency-Key':'upload-once'})
    assert response.status_code==200, response.text
    ident=response.json()['files'][0]['id']
    again=await client.post(path+'/files',json=payload,headers={'Idempotency-Key':'upload-once'})
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
