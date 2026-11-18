"""Real Gate/Router HTTP and connector, scripted SDK user-consumption boundary."""
import asyncio
import hashlib
import json
import uuid

import httpx
import pytest

from .test_network_gate import network  # noqa: F401
from .test_steering90 import connector_node
from atlas_gate.gate.question_answers import HEADER, TARGET_HEADER, Answers, merge
from atlas_gate.gate.errors import GateError


async def prepare(network, monkeypatch):
    from atlas_router.backends.stub import StubSession
    client, app, routers, *_ = network
    queued, observed, technical = [], {}, []
    async def steer(self, text, *, turn_id, uid, client_message_id, observe, attachments=None):
        queued.append((text, turn_id, uid, client_message_id))
        observed[self.session_id] = observe
    original = StubSession.tool_result
    async def result(self, call, text, is_error):
        technical.append((call, text))
        value = await original(self, call, text, is_error)
        if self.session_id in observed:
            observed[self.session_id]('absorbed', proof={'kind':'scripted_native_user'})
        return value
    monkeypatch.setattr(StubSession, 'steer_v1', steer, raising=False)
    monkeypatch.setattr(StubSession, 'tool_result', result)
    for router in routers:
        router.state.backend.steering_capabilities = lambda: True
    await app.state.gate.nodes.refresh()
    return queued, technical


async def active(client):
    created = await client.post('/harness/agent/sessions', json={'model':'stub-fast','tools':[
        {'name':'ask_user','description':'question','parameters':{'type':'object'}}]})
    assert created.status_code==200,created.text
    data=created.json();sid=data['id'];prefix='/harness/agent/sessions/'+sid
    assert data['questionAnswers']['supported'],data
    assert (await client.post(prefix+'/prompt?mode=async',json={'id':'source','text':'call:ask_user'})).status_code==202
    for _ in range(100):
        page=await client.get(prefix+'/turns/source/events',params={'wait':100})
        if any(e['type']=='tool_call' for e in page.json()['events']):break
    assert any(e['type']=='tool_call' for e in page.json()['events'])
    return sid,prefix,data['questionAnswers']['requiredHeaders']


def body():
    return {'version':1,'turnId':'source','callId':'stub-call-1',
        'clientMessageId':str(uuid.uuid4()),'text':'  Ответ человека\nстрока 2  '}


def raw(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':')).encode()


async def observe(client,prefix,value,data):
    return await client.get(prefix+'/question-answers/'+value['clientMessageId'],params={
        'turnId':value['turnId'],'callId':value['callId'],'bodySha256':hashlib.sha256(data).hexdigest()})


@pytest.mark.parametrize('transport',['direct','connector'])
@pytest.mark.parametrize('loss',['lost_post','lost_ack'])
async def test_live_http_original_answer_after_loss_once_owner_and_byte_identity(network,monkeypatch,tmp_path,transport,loss):
    queued,technical=await prepare(network,monkeypatch)
    client,app,routers,_,creds,other=network
    state=app.state.gate
    task=await connector_node(client,state,tmp_path) if transport=='connector' else None
    try:
        sid,prefix,headers=await active(client)
        value=body();data=raw(value)
        binding=state.store._one('SELECT * FROM router_bindings WHERE id=?',(sid,))
        upstream=state.nodes.clients[binding['node_id']]
        original=upstream.request;failed=False
        async def interrupted(method,path,**kwargs):
            nonlocal failed
            if method=='POST' and path.endswith('/question-answer') and not failed:
                failed=True
                if loss=='lost_ack':await original(method,path,**kwargs)
                raise httpx.ReadError(loss)
            return await original(method,path,**kwargs)
        monkeypatch.setattr(upstream,'request',interrupted)
        send_headers={**headers,'Idempotency-Key':'original-answer','Content-Type':'application/json'}
        first=await client.post(prefix+'/question-answer',content=data,headers=send_headers)
        assert first.status_code==503,first.text
        for _ in range(4):
            replay=await client.post(prefix+'/question-answer',content=data,headers=send_headers)
            assert replay.status_code in (200,202),replay.text
        for _ in range(100):
            result=await observe(client,prefix,value,data)
            if result.json()['questionAnswer']['state']=='applied':break
            await asyncio.sleep(.01)
        receipt=result.json()['questionAnswer']
        assert receipt['state']=='applied' and receipt['sessionId']==sid
        assert receipt['requestId']=='original-answer' and receipt['bodySha256']==hashlib.sha256(data).hexdigest()
        assert queued==[(value['text'],'source',receipt['uuid'],value['clientMessageId'])]
        assert len(technical)==1
        assert value['text'] not in technical[0][1]
        wrong=await client.post(prefix+'/question-answer',content=json.dumps(value,ensure_ascii=False,indent=1).encode(),headers=send_headers)
        assert wrong.status_code==409
        foreign=await client.get(prefix+'/question-answers/'+value['clientMessageId'],
            params={'turnId':'source','callId':value['callId'],'bodySha256':receipt['bodySha256']},
            headers={'Authorization':'Bearer '+other['device_token']})
        assert foreign.status_code in (403,404)
        # A Gate-store reopen preserves the same accepted Router operation.
        state.question_answers=Answers(state.store)
        assert (await observe(client,prefix,value,data)).json()['questionAnswer']==receipt
        source=routers[0 if binding['node_id']=='a' else 1].state.registry.get(binding['local_id'])
        assert source.session._turns==1 and source.session._calls==1
        assert len(queued)==len(technical)==1
    finally:
        if task:
            task.cancel();await asyncio.gather(task,return_exceptions=True)


async def test_old_connector_cannot_publish_a_usable_question_capability(network,monkeypatch,tmp_path):
    queued,technical=await prepare(network,monkeypatch)
    client,app,*_=network
    from atlas_router.connector import Connector
    original=Connector.execute
    async def legacy(self,work):
        if '/question-answer' in work['path']:raise ValueError('legacy path')
        work={**work,'headers':{k:v for k,v in work.get('headers',{}).items() if k.lower()!=HEADER.lower()}}
        return await original(self,work)
    monkeypatch.setattr(Connector,'execute',legacy)
    task=await connector_node(client,app.state.gate,tmp_path)
    try:
        created=await client.post('/harness/agent/sessions',json={'model':'stub-fast','tools':[]})
        assert created.status_code==200,created.text
        assert created.json()['questionAnswers']['supported'] is False
        sid=created.json()['id'];prefix='/harness/agent/sessions/'+sid
        metadata=await client.get(prefix+'/sdk-tools')
        assert metadata.status_code==200 and metadata.json()['questionAnswers']['supported'] is False
        assert not queued and not technical
    finally:
        task.cancel();await asyncio.gather(task,return_exceptions=True)


async def test_unsupported_native_backend_and_target_boot_do_not_dispatch(network,monkeypatch):
    queued,technical=await prepare(network,monkeypatch)
    client,app,routers,*_=network
    sid,prefix,headers=await active(client)
    value=body()
    wrong=await client.post(prefix+'/question-answer',content=raw(value),headers={
        **headers,TARGET_HEADER:'foreign-boot','Idempotency-Key':'foreign','Content-Type':'application/json'})
    assert wrong.status_code==409 and wrong.json()['questionAnswer']['state']=='not_applied'
    for router in routers:router.state.backend.steering_capabilities=lambda:False
    value=body()
    denied=await client.post(prefix+'/question-answer',content=raw(value),headers={
        **headers,'Idempotency-Key':'unsupported','Content-Type':'application/json'})
    assert denied.status_code==422 and denied.json()['questionAnswer']['state']=='not_applied'
    assert not queued and not technical
    # Legacy callback API remains usable when no linked operation owns this call.
    legacy=await client.post(prefix+'/tool_result',json={'callId':'stub-call-1','content':'legacy','isError':False})
    assert legacy.status_code==200,legacy.text


async def test_receipt_identity_and_monotonicity_reject_foreign_proof(network,monkeypatch):
    await prepare(network,monkeypatch)
    client,app,*_=network
    sid,prefix,headers=await active(client)
    value=body();data=raw(value)
    posted=await client.post(prefix+'/question-answer',content=data,headers={
        **headers,'Idempotency-Key':'monotonic','Content-Type':'application/json'})
    assert posted.status_code in (200,202)
    state=app.state.gate
    for _ in range(100):
        result=await observe(client,prefix,value,data)
        if result.json()['questionAnswer']['state']=='applied':break
        await asyncio.sleep(.01)
    complete=result.json()['questionAnswer'];assert complete['state']=='applied'
    row=state.question_answers.get(sid=sid,message=value['clientMessageId'],device=type('Device',(),{
        'device_id':state.store._one('SELECT device_id FROM gate_question_answers WHERE session_id=?',(sid,))['device_id'],
        'org':'test-org'})())
    binding=state.store._one('SELECT * FROM router_bindings WHERE id=?',(sid,))
    upstream={**complete,'sessionId':binding['local_id'],'requestId':row['id']}
    for field,wrong in [('uuid','foreign'),('bodySha256','0'*64),('callId','foreign'),
            ('targetBootId','successor'),('sessionId','foreign')]:
        with pytest.raises(GateError):merge(row,binding,{**upstream,field:wrong})
    stale={**complete,'state':'admitted','application':'pending','callback':'pending'}
    assert state.question_answers.save(row,stale)==complete
    # A successor boot may be queried for old durable evidence, but is never
    # allowed to receive a fresh delivery under the old target identity.
    state.store._exec('UPDATE router_bindings SET boot_id=? WHERE id=?',('successor',sid))
    assert (await observe(client,prefix,value,data)).json()['questionAnswer']==complete
