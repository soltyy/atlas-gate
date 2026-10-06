"""Codex адаптер, PDFium и HTTP Gate/Router; inference RPC сценарный."""
import asyncio
import base64
import json
import os
import uuid
from pathlib import Path
import httpx
import pytest
from .test_network_gate import network, create

pytestmark=pytest.mark.skipif(not os.environ.get('ATLAS_TEST_ROUTER_ROOT'),reason='нужны исходники Router')


class DocumentRpc:
    def __init__(self,settings,cwd,handler):
        self.handler=handler; self.queue=asyncio.Queue(); self.jobs=[]
    async def start(self): pass
    async def notification(self): return await self.queue.get()
    async def close(self):
        for task in self.jobs: task.cancel()
        await asyncio.gather(*self.jobs,return_exceptions=True)
    async def call(self,method,params=None):
        params=params or {}
        if method=='account/read': return {'account':{'type':'chatgpt'}}
        if method=='model/list': return {'data':[{'id':'stub-fast','model':'stub-fast','displayName':'test'}]}
        if method in ('thread/start','thread/resume'):
            self.thread=params.get('threadId') or str(uuid.uuid4())
            return {'thread':{'id':self.thread},'model':'stub-fast'}
        if method=='turn/start':
            self.turn=str(uuid.uuid4())
            self.jobs.append(asyncio.create_task(self.run(params)))
            return {'turn':{'id':self.turn}}
        return {}
    async def run(self,params):
        assert all('JVBER' not in i.get('text','') for i in params['input'])
        docs=await self.handler('item/tool/call',{'tool':'atlas_document','arguments':{'operation':'list'}})
        doc=json.loads(docs['contentItems'][0]['text'])[0]['id']
        text=await self.handler('item/tool/call',{'tool':'atlas_call_tool','arguments':{'name':'atlas_document',
            'arguments':{'operation':'text','documentId':doc,'page':1}}})
        assert '42017' in text['contentItems'][0]['text']
        image=await self.handler('item/tool/call',{'tool':'atlas_document','arguments':{'operation':'image','documentId':doc,'page':2}})
        assert image['success'] and image['contentItems'][1]['type']=='inputImage'
        for method,params in [('item/completed',{'item':{'type':'agentMessage','text':'TEXT 42017; raster delivered'}}),
                              ('turn/completed',{'turn':{'id':self.turn,'status':'completed'}})]:
            self.queue.put_nowait((method,dict(params,threadId=self.thread,turnId=self.turn)))


@pytest.fixture
async def codex_network(monkeypatch,network):
    # network стартует на stub; новый session backend заменяется до первого create.
    from atlas_router.backends.codex import CodexBackend
    monkeypatch.setattr('atlas_router.backends.codex.CodexRpc',DocumentRpc)
    client,app,routers,*rest=network
    # create_app closures держат исходный backend: меняем factory до нового create невозможно.
    # Подменяем open_session в том же backend; документы читаются по общей директории settings.
    backends=[]
    for router in routers:
        b=CodexBackend(router.state.settings); backends.append(b)
        monkeypatch.setattr(router.state.backend,'open_session',b.open_session)
        monkeypatch.setattr(router.state.backend,'name','codex')
        monkeypatch.setattr(router.state.backend,'models',b.models)
        monkeypatch.setattr(router.state.cli,'auth_status',b.auth_status)
    yield network
    for b in backends: await b.close()


@pytest.mark.parametrize('transport',['direct','connector'])
@pytest.mark.parametrize('mode',['sse','async'])
async def test_codex_pdf_all_gate_transports(codex_network,transport,mode):
    client,app,routers,settings,creds,other=codex_network
    state=app.state.gate
    workers=[]; connections=[]
    if transport=='connector':
        from atlas_gate.gate.node_channel import ChannelClient
        from atlas_router.connector import Connector
        for node_id,node in state.nodes.config.items():
            connector=object.__new__(Connector); connector.identity={'nodeId':node_id}
            connector.router=httpx.AsyncClient(base_url=node.url,headers={'X-Atlas-Token':'test-router'})
            connections.append(connector.router)
            await state.nodes.clients[node_id].aclose()
            state.nodes.clients[node_id]=ChannelClient(state.channel,node_id)
            async def pump(n=node_id,c=connector):
                while True:
                    work=await state.channel.poll(n,wait=.1)
                    if work:
                        result=await c.execute(work)
                        state.channel.complete(n,work['id'],result['status'],base64.b64decode(result['body']))
            workers.append(asyncio.create_task(pump()))
    try:
        sid=await create(client)
        prefix='/harness/agent/sessions/'+sid
        data=(Path(__file__).parent/'fixtures'/'mixed.pdf').read_bytes()+b'\n%'+b'X'*920000
        payload={'id':'pdf','text':'read','attachments':[{'kind':'pdf','filename':'mixed.pdf','data':base64.b64encode(data).decode()}]}
        if mode=='async':
            reply=await client.post(prefix+'/prompt?mode=async',json=payload)
            assert reply.status_code==202,reply.text
            for _ in range(300):
                events=(await client.get(prefix+'/turns/pdf/events',params={'wait':200})).json()
                if events['done']: break
                await asyncio.sleep(.05)
            events=events['events']
        else:
            events=[]
            async with client.stream('POST',prefix+'/prompt',json=payload) as response:
                assert response.status_code==200
                async for line in response.aiter_lines():
                    if line.startswith('data: '): events.append(json.loads(line[6:]))
        assert any(e['type']=='result' and e['ok'] for e in events),events
        docs=(await client.get(prefix+'/documents')).json()['documents']
        assert len(docs)==1
        image=await client.get(prefix+'/documents/'+docs[0]['id'],params={'operation':'image','page':2})
        assert image.status_code==200,image.text
        assert (await client.get(prefix+'/attachments')).json()['attachments']['agentDocumentTools']
    finally:
        for task in workers: task.cancel()
        await asyncio.gather(*workers,return_exceptions=True)
        for connection in connections: await connection.aclose()
