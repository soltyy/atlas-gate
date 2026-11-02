import asyncio,base64
import pytest
from atlas_gate.gate.input_recovery import validate
from atlas_gate.gate.errors import GateError
from .test_network_gate import network


@pytest.mark.parametrize('field,value', [('sessionId','other'),('logicalSdkSessionId','foreign'),
    ('generation',-1),('version',True),('actionsReplayed',True),('state','pretend')])
def test_bound_empty_proof_rejects_mismatched_or_unversioned(field,value):
    proof={'version':1,'sessionId':'s','logicalSdkSessionId':'sdk','state':'empty_confirmed',
           'generation':1,'actionsReplayed':False}
    validate(proof,'s','sdk')
    with pytest.raises(GateError):validate(dict(proof,**{field:value}),'s','sdk')


async def test_gate_cold_files_restore_keeps_scope_owner_and_upload_ack(network,monkeypatch,tmp_path):
    from atlas_router.backends.codex import CodexBackend
    from atlas_router.settings import Settings
    from atlas_router.sdk_tools import SdkTools
    import sys,os
    # Reuse the scenario RPC from the explicitly configured Router test checkout.
    import importlib.util
    root=os.environ['ATLAS_TEST_ROUTER_ROOT']
    # Its helper imports sibling tests, so use a unique package to avoid Gate.tests collision.
    package=importlib.util.spec_from_file_location('router_input_tests',root+'/tests/__init__.py',submodule_search_locations=[root+'/tests'])
    mod=importlib.util.module_from_spec(package);sys.modules[package.name]=mod;package.loader.exec_module(mod)
    from router_input_tests.test_codex_backend import ScriptedRpc
    monkeypatch.setattr('atlas_router.backends.codex.CodexRpc',ScriptedRpc)
    monkeypatch.setattr('atlas_router.file_runtime.FileRuntime.available',lambda self:True)
    ScriptedRpc.histories,ScriptedRpc.instances={},[]
    client,gate,routers,_,creds,other=network
    gate.state.gate.orgs['test-org'].sdk_tools=SdkTools(files=True)
    backends=[]
    try:
        for i,node in enumerate(routers):
            backend=CodexBackend(Settings(_env_file=None,ATLAS_DOCUMENTS_DIR=str(tmp_path/f'd{i}'),ATLAS_CODEX_HOME=str(tmp_path/f'c{i}')))
            backends.append(backend)
            monkeypatch.setattr(node.state.backend,'name','codex')
            monkeypatch.setattr(node.state.backend,'open_session',backend.open_session)
            async def logged_in():return {'loggedIn':True}
            monkeypatch.setattr(node.state.cli,'auth_status',logged_in)
        for name in gate.state.gate.nodes.config:await gate.state.gate.nodes.probe(name)
        created=await client.post('/harness/agent/sessions',json={'model':'stub-fast','tools':[],
            'sdkTools':{'version':1,'files':True,'webSearch':'disabled','imageGeneration':False}},headers={'Idempotency-Key':'virgin-create'})
        assert created.status_code==200,created.text
        sid=created.json()['id'];prefix='/harness/agent/sessions/'+sid
        upload={'id':'own-upload','attachments':[{'kind':'file','filename':'own.txt','mediaType':'text/plain','data':base64.b64encode(b'owned').decode()}]}
        first=await client.post(prefix+'/files',json=upload,headers={'Idempotency-Key':'original-upload'})
        assert first.status_code==200,first.text
        before=(await client.get(prefix+'/files')).json()
        binding=gate.state.gate.store._one('SELECT * FROM router_bindings WHERE id=?',(sid,))
        node=routers[0 if binding['node_id']=='a' else 1]
        await node.state.registry.remove(binding['local_id'],persist_close=False)
        ScriptedRpc.histories={}
        gate.state.gate.store._exec('UPDATE router_bindings SET boot_id=? WHERE id=?',('prior-boot',sid))
        result=await client.get(prefix+'/files')
        assert result.status_code==200,result.text
        assert result.json()['files']==before['files']
        assert result.json()['inputRecovery']['generation']==1
        assert result.json()['inputRecovery']['sessionId']==sid
        assert result.json()['inputRecovery']['logicalSdkSessionId']==binding['sdk_alias']
        assert node.state.registry.get(binding['local_id']).session.session_id not in result.text
        assert result.json()['inputRecovery']['logicalSdkSessionId']==before['inputRecovery']['logicalSdkSessionId']
        replay=await client.post(prefix+'/files',json=upload,headers={'Idempotency-Key':'original-upload'})
        assert replay.content==first.content and len((await client.get(prefix+'/files')).json()['files'])==1
        assert (await client.get(prefix+'/files',headers={'Authorization':'Bearer '+other['device_token']})).status_code==404
        assert not any(ScriptedRpc.histories.values()),'No prompt was submitted during recovery'
    finally:
        for backend in backends:await backend.close()


def test_paused_input_keeps_retry_hint_without_reclassifying_protocol():
    import httpx
    from atlas_gate.gate.agent import _response
    body={'error':{'code':'input_preparation_paused','deliveryState':'not_sent','scope':'attempt'}}
    reply=_response(httpx.Response(503,json=body,headers={'Retry-After':'1'}))
    assert reply.status_code==503 and reply.headers['Retry-After']=='1'
    import json
    assert json.loads(reply.body)==body
