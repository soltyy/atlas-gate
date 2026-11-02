import concurrent.futures
import json
import sqlite3
import threading
import httpx
import pytest
from atlas_gate.gate.operations import Operations, body_sha256
from atlas_gate.gate.errors import GateError
from atlas_gate.gate.store import Store
from .test_routing import device
from .test_network_gate import network, create


def binding():
    return {'id':'session','node_id':'node','boot_id':'boot','route':'sub','model':'model','roles':'[]'}


def test_fence_survives_restart_and_blocks_legacy_admission_insert(tmp_path):
    path=str(tmp_path/'gate.db');s=Store(path);o=Operations(s);b=binding();d=device('owner')
    body={'id':'turn','text':'привет'};digest=body_sha256(body)
    p=o.resolve_prompt(d,b,'key','turn',digest)
    assert p['outcome']=='not_sent' and p['sealed'] and p['bodySha256']==digest
    s.close();s=Store(path);o=Operations(s)
    assert o.resolve_prompt(d,b,'key','turn',digest)==p
    with pytest.raises(GateError) as blocked:o.begin(d,b,'POST','/v1/sessions/local/prompt',body,'key','turn')
    assert blocked.value.code=='command_not_sent'
    # A legacy Gate rollback still writes before forwarding. SQLite enforces it.
    with pytest.raises(sqlite3.IntegrityError,match='admission_fenced'):
        s._exec("INSERT INTO router_operations(id,device_id,session_id,turn_id,fingerprint,node_id,method,path,state,created) VALUES (?,?,?,?,?,'node','POST','/prompt','prepared',0)",
                (o.key(d,'key'),d.device_id,b['id'],'turn','legacy'))
    assert s._one('SELECT COUNT(*) n FROM router_operations')['n']==0
    s.close()


def test_linearizable_resolve_vs_admit(tmp_path):
    s=Store(str(tmp_path/'race.db'));o=Operations(s);b=binding();d=device('d')
    for i in range(20):
        body={'id':str(i),'text':'work'};key='k'+str(i);barrier=threading.Barrier(2)
        def admit():
            barrier.wait()
            try:return o.begin(d,b,'POST','/v1/sessions/local/prompt',body,key,str(i))
            except GateError as e:return e.code
        def resolve():
            barrier.wait();return o.resolve_prompt(d,b,key,str(i),body_sha256(body))
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(admit);r=pool.submit(resolve);admitted=a.result();proof=r.result()
        exists=s._one('SELECT id FROM router_operations WHERE id=?',(o.key(d,key),))
        sealed=s._one('SELECT id FROM router_admission_fences WHERE id=?',(o.key(d,key),))
        assert bool(exists)!=bool(sealed)
        if proof['outcome']=='not_sent':assert admitted=='command_not_sent' and not exists
        else:assert proof['outcome']=='unknown' and exists and not sealed
    s.close()


def test_existing_unknown_or_complete_can_never_be_sealed(tmp_path):
    s=Store(str(tmp_path/'existing.db'));o=Operations(s);b=binding();d=device('d');body={'id':'t','text':'work'}
    op,_=o.begin(d,b,'POST','/v1/sessions/local/prompt',body,'k','t')
    for state in ('prepared','unknown','complete'):
        if state=='unknown':o.unknown(op['id'])
        if state=='complete':o.complete(op['id'],httpx.Response(202,json={'turnId':'t','seq':0}))
        assert o.resolve_prompt(d,b,'k','t',body_sha256(body))['outcome']=='unknown'
        assert s._one('SELECT COUNT(*) n FROM router_admission_fences')['n']==0
    with pytest.raises(GateError):o.resolve_prompt(d,b,'k','t','0'*64)
    assert o.resolve_prompt(d,b,'other-key','t',body_sha256(body))['outcome']=='unknown'
    s.close()


@pytest.mark.asyncio
async def test_http_fence_closed_binding_ownership_and_no_execution(network):
    c,app,routers,settings,creds,other=network
    sid=await create(c);body={'id':'old','text':'call:echo'};key='prompt:old'
    request={'version':1,'requestId':key,'turnId':'old','bodySha256':body_sha256(body)}
    url=f'/harness/agent/sessions/{sid}'
    wrong=await c.post(url+'/commands/reconcile',json=request,headers={'Authorization':'Bearer '+other['device_token']})
    assert wrong.status_code==404
    sealed=await c.post(url+'/commands/reconcile',json=request)
    assert sealed.status_code==409,sealed.text
    proof=sealed.json()['recovery'];assert proof['sealed'] and proof['outcome']=='not_sent'
    late=await c.post(url+'/prompt?mode=async',json=body,headers={'Idempotency-Key':key})
    assert late.status_code==409 and late.json()['error']['code']=='command_not_sent'
    mutated=await c.post(url+'/prompt?mode=async',json={**body,'text':'different'},headers={'Idempotency-Key':key})
    assert mutated.status_code==409 and mutated.json()['error']['code']=='idempotency_conflict'
    receipt=await c.get(url+'/commands',params={'requestId':key})
    assert receipt.content==sealed.content and receipt.headers['X-Atlas-Command-State']=='complete'
    assert app.state.gate.store._one('SELECT COUNT(*) n FROM router_operations')['n']==0
    assert all(not e.buffers for r in routers for e in r.state.registry._entries.values())
    await c.delete(url)
    repeated=await c.post(url+'/commands/reconcile',json=request)
    assert repeated.content==sealed.content
    assert (await c.get(url+'/commands',params={'requestId':key})).content==sealed.content


@pytest.mark.asyncio
async def test_http_successor_tools_execute_once_and_old_intent_stays_retired(network):
    import asyncio
    c,app,routers,*_=network;sid=await create(c);url=f'/harness/agent/sessions/{sid}'
    old={'id':'old','text':'call:echo'}
    sealed=await c.post(url+'/commands/reconcile',json={'version':1,'requestId':'old-key','turnId':'old','bodySha256':body_sha256(old)})
    assert sealed.status_code==409
    response=await c.post(url+'/prompt?mode=async',json={'id':'successor','text':'call:echo'},headers={'Idempotency-Key':'new-key'})
    assert response.status_code==202
    calls=[]
    for _ in range(200):
        doc=(await c.get(url+'/turns/successor/events',params={'after':0,'wait':10})).json()
        calls=[e for e in doc['events'] if e['type']=='tool_call']
        if calls:break
        await asyncio.sleep(.01)
    assert len(calls)==1
    await c.post(url+'/tool_result',json={'callId':calls[0]['callId'],'content':'counter=1'})
    for _ in range(200):
        doc=(await c.get(url+'/turns/successor/events',params={'after':0,'wait':10})).json()
        if doc['done']:break
        await asyncio.sleep(.01)
    assert doc['done'] and len([e for e in doc['events'] if e['type']=='tool_call'])==1
    for _ in range(2):
        assert (await c.post(url+'/prompt?mode=async',json=old,headers={'Idempotency-Key':'old-key'})).status_code==409
        assert (await c.get(url+'/turns/successor/events',params={'after':0,'wait':0})).json()==doc
    assert app.state.gate.store._one('SELECT COUNT(*) n FROM usage WHERE session_id=? AND model_call=?',(sid,'successor'))['n']==1
