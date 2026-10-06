"""Opt-in настоящая подписка: PDF через независимый Gate, четыре пути доставки."""
import asyncio
import base64
import json
import os
from pathlib import Path
import sys

import httpx
import pytest
from atlas_gate.main import create_app
from atlas_gate.settings import Settings
from .test_network_gate import serve

pytestmark = pytest.mark.skipif(os.environ.get('ATLAS_LIVE_CODEX') != '1', reason='нужен ATLAS_LIVE_CODEX=1')


@pytest.mark.parametrize('transport', ['direct', 'connector'])
@pytest.mark.parametrize('mode', ['sse', 'async'])
async def test_live_pdf_connection_variants(tmp_path, monkeypatch, transport, mode):
    sys.path.insert(0, os.environ['ATLAS_TEST_ROUTER_ROOT'])
    from atlas_router.main import create_app as make_router
    from atlas_router.settings import Settings as RouterSettings
    from atlas_router.connector import Connector
    from atlas_gate.gate.node_channel import ChannelClient
    router = make_router(RouterSettings(_env_file=None, ATLAS_BACKEND='codex', ATLAS_GATE_ENABLED=False,
        ATLAS_TRUST_LOCAL=False, ATLAS_TOKEN='test-node', ATLAS_NODE_ID='live', ATLAS_NODE_SUBAGENTS=0,
        ATLAS_DOCUMENTS_DIR=str(tmp_path/'documents'), ATLAS_NODE_DB=str(tmp_path/'node.db')))
    orgs=tmp_path/'orgs'; orgs.mkdir()
    org=json.loads((Path(__file__).parents[1]/'atlas_gate/gate/orgs/test-org.json').read_text(encoding='utf-8'))
    org['routes']=[r for r in org['routes'] if r['kind']=='router-agent']
    org['models']=[{'route_id':'claude-sub','model':'gpt-6.1-sol'}]
    org['pricing']=[]; org['agents']=[]; org['mcp']=[]
    org['policy']['routes_by_class']={k:(['claude-sub'] if k in ('public','internal') else []) for k in org['policy']['data_classes']}
    (orgs/'test-org.json').write_text(json.dumps(org),encoding='utf-8')
    monkeypatch.setenv('LIVE_DOCUMENT_NODE_TOKEN','test-node')
    async with serve(router) as router_url:
        nodes=tmp_path/'nodes.json'
        nodes.write_text(json.dumps([{'node_id':'live','url':router_url,'token_env':'LIVE_DOCUMENT_NODE_TOKEN',
            'orgs':['test-org'],'routes':['claude-sub'],'account_group':'isolated-live'}]))
        gate=create_app(Settings(_env_file=None, ATLAS_TOKEN='test-admin', ATLAS_GATE_TEST=False,
            ATLAS_GATE_DB=str(tmp_path/'gate.db'), ATLAS_GATE_KEY_PATH=str(tmp_path/'key.pem'),
            ATLAS_GATE_KEYRING_SECRET='test-secret', ATLAS_GATE_ORGS_DIR=str(orgs),
            ATLAS_GATE_NODES_FILE=str(nodes), ATLAS_GATE_KEYS_FILE=str(tmp_path/'no-keys'),
            ATLAS_GATE_NODE_PKI_DIR=str(tmp_path/'pki')))
        async with serve(gate) as gate_url, httpx.AsyncClient(base_url=gate_url,timeout=180) as client:
            state=gate.state.gate; await state.ready.wait()
            pump_task=None; connector=None
            if transport=='connector':
                connector=object.__new__(Connector); connector.identity={'nodeId':'live'}
                connector.router=httpx.AsyncClient(base_url=router_url,headers={'X-Atlas-Token':'test-node'},timeout=180)
                await state.nodes.clients['live'].aclose()
                state.nodes.clients['live']=ChannelClient(state.channel,'live')
                async def pump():
                    while True:
                        work=await state.channel.poll('live',wait=.1)
                        if work:
                            result=await connector.execute(work)
                            state.channel.complete('live',work['id'],result['status'],base64.b64decode(result['body']))
                pump_task=asyncio.create_task(pump())
            try:
                start=(await client.post('/harness/enroll/start',json={'device_name':'isolated-pdf','platform':'test','app_version':'1'})).json()
                approved=await client.post('/harness/enroll/approve',headers={'X-Atlas-Token':'test-admin'},
                    json={'user_code':start['user_code'],'org':'test-org','user':{'id':'test'}})
                assert approved.status_code==200,approved.text
                creds=(await client.post('/harness/enroll/poll',json={'device_code':start['device_code']})).json()
                client.headers['Authorization']='Bearer '+creds['device_token']
                created=await client.post('/harness/agent/sessions',json={'model':'gpt-6.1-sol',
                    'system':'Читай приложенный PDF через atlas_document. Для страниц со сканом используй image. Отвечай кратко.'})
                assert created.status_code==200,created.text
                prefix='/harness/agent/sessions/'+created.json()['id']
                data=(Path(__file__).parent/'fixtures/mixed.pdf').read_bytes()+b'\n%'+b'X'*920000
                payload={'id':'pdf','text':'Прочитай обе страницы PDF. Назови число рядом с TEXT на первой и рядом с SCAN на второй. Вторая содержит изображение: используй image.',
                    'attachments':[{'kind':'pdf','filename':'mixed.pdf','data':base64.b64encode(data).decode()}]}
                if mode=='sse':
                    events=[]
                    async with client.stream('POST',prefix+'/prompt',json=payload) as reply:
                        assert reply.status_code==200
                        async for line in reply.aiter_lines():
                            if line.startswith('data: '): events.append(json.loads(line[6:]))
                else:
                    reply=await client.post(prefix+'/prompt?mode=async',json=payload)
                    assert reply.status_code==202,reply.text
                    deadline=asyncio.get_running_loop().time()+180
                    while asyncio.get_running_loop().time()<deadline:
                        answer=(await client.get(prefix+'/turns/pdf/events',params={'wait':1000})).json()
                        if answer['done']: break
                        await asyncio.sleep(.05)
                    assert answer['done'],answer
                    events=answer['events']
                result=next(e for e in events if e['type']=='result')
                assert result['ok'] and '42017' in result['text'] and '7391' in result['text'],result
                docs=(await client.get(prefix+'/documents')).json()['documents']
                assert len(docs)==1 and docs[0]['pageCount']==2
                assert (await client.get(prefix+'/attachments')).json()['attachments']['agentDocumentTools']
            finally:
                if pump_task:
                    pump_task.cancel(); await asyncio.gather(pump_task,return_exceptions=True)
                if connector: await connector.router.aclose()
