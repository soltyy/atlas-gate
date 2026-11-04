"""Loopback Java acceptance fixture, temporary Gate + two deterministic Router."""
import asyncio
import json
import os
from pathlib import Path
import tempfile

from fastapi import Request
import pytest

from atlas_gate.gate.operations import body_sha256
from .test_network_gate import network, create, events


async def seed(client, state, suffix=''):
    prefix='history103'+('-'+suffix if suffix else '')
    old = await create(client)
    # A transport already owns its logical native identity before inference.
    # Only the CURRENT transport below executes one synthetic seed prompt.
    assert (await client.get('/harness/agent/sessions')).status_code == 200
    previous = state.store._one('SELECT * FROM router_bindings WHERE id=?',(old,))
    assert (await client.delete('/harness/agent/sessions/'+old)).status_code == 200
    opened = await client.post('/harness/agent/sessions',json={'system':'test','model':'stub-fast',
        'tools':[{'name':'echo','description':'echo','parameters':{'type':'object'}}],
        'resumeSessionId':previous['sdk_alias']},headers={'Idempotency-Key':prefix+'-resume'})
    assert opened.status_code==200 and opened.json()['resumed'] is True,opened.text
    current=opened.json()['id']
    payload={'id':prefix+'-current-turn','text':'saved current answer exactly once'+(' '+suffix if suffix else '')}
    result=await client.post(f'/harness/agent/sessions/{current}/prompt?mode=async',json=payload,
                            headers={'Idempotency-Key':payload['id']})
    assert result.status_code==202,result.text
    saved=await events(client,current,payload['id'],terminal=True)
    listing=(await client.get('/harness/agent/sessions')).json()['sessions']
    fresh=next(x for x in listing if x['id']==current)
    return {'currentSID':current,'previousSID':old,'previousAlias':previous['sdk_alias'],
            'historyAlias':fresh['sdkSessionId'],'sourceRunId':payload['id'],'turnId':payload['id'],
            'requestId':payload['id'],'originalPromptBody':payload,'bodySha256':body_sha256(payload),
            'gateRoot':state.public_url,'orgId':'test-org','routeId':'claude-sub','model':'stub-fast',
            'savedTerminal':next(e for e in saved if e['type']=='result')}


async def main():
    with tempfile.TemporaryDirectory(prefix='gate-history103-java-') as directory:
        patch=pytest.MonkeyPatch()
        fixture=network.__wrapped__(Path(directory),patch)
        client,app,routers,settings,credentials,other=await anext(fixture)
        try:
            state=app.state.gate
            settings.ATLAS_GATE_PUBLIC_URL=str(client.base_url).rstrip('/')
            org=state.orgs['test-org'].model_dump(mode='json')
            org['agents']=[dict(id='stub-agent',name='Acceptance',version='1',default=True,model='inherit',
                allowed_routes=['claude-sub'],tools_allowlist=['*'],system_prompt='test')]
            folder=Path(settings.ATLAS_GATE_ORGS_DIR);folder.mkdir(parents=True,exist_ok=True)
            (folder/'test-org.json').write_text(json.dumps(org),encoding='utf-8')
            await state.reload()
            count=int(os.environ.get('ATLAS_HISTORY103_CHATS','1'))
            assert count>=1
            chats=[]
            for i in range(count):
                chat=await seed(client,state,'chat-'+str(i+1) if count>1 else '')
                damaged=count==1 or i<2
                chat.update(org=chat['orgId'],route=chat['routeId'],session_id=chat['currentSID'],
                    previous_alias=chat['previousAlias'],run_id=chat['sourceRunId'],
                    request_id=chat['requestId'],prompt_body=chat['originalPromptBody'],
                    expected_text=chat['savedTerminal'].get('text'),
                    conversation_id='lineage228-chat'+('-'+str(i+1) if count>1 else ''),
                    branch_id='lineage228-branch'+('-'+str(i+1) if count>1 else ''),
                    damaged=damaged,checkpointAlias=chat['previousAlias'] if damaged else chat['historyAlias'])
                chats.append(chat)
            setup=dict(chats[0],chats=chats)
            setup.update(credentials=credentials,foreignCredentials=other,url=str(client.base_url))
            setup.update(org=setup['orgId'],route=setup['routeId'],session_id=setup['currentSID'],
                previous_alias=setup['previousAlias'],run_id=setup['sourceRunId'],
                request_id=setup['requestId'],prompt_body=setup['originalPromptBody'],
                expected_text=setup['savedTerminal'].get('text'),conversation_id='lineage228-chat',branch_id='lineage228-branch')
            stopped=asyncio.Event()
            def authorize(request):
                assert request.headers.get('X-Test-Key')=='isolated-lineage228'

            @app.get('/__test/lineage228/setup')
            async def bootstrap(request:Request):
                authorize(request);return setup

            @app.get('/__test/lineage228/state')
            async def inspect(request:Request):
                authorize(request)
                turns=sum(r.state.journal.db.execute('SELECT COUNT(*) FROM turns').fetchone()[0] for r in routers)
                tools=sum(r.state.journal.db.execute("SELECT COUNT(*) FROM events WHERE json_extract(body,'$.type')='tool_call'").fetchone()[0] for r in routers)
                return {'promptExecutions':turns,'toolExecutions':tools,
                        'operationCount':state.store._one('SELECT COUNT(*) n FROM router_operations')['n'],'turnCount':turns}

            @app.post('/__test/lineage228/stop')
            async def stop(request:Request):
                authorize(request);stopped.set();return {'ok':True}

            print(json.dumps({'url':str(client.base_url)}),flush=True)
            await stopped.wait()
        finally:
            await fixture.aclose();patch.undo()


if __name__=='__main__':
    asyncio.run(main())
