"""Isolated source Gate + two stub Router HTTP servers for Java acceptance.

Run with ATLAS_TEST_ROUTER_ROOT and `python -m tests.auth97_fixture`.
Only loopback listeners, temporary DBs/credentials, no SDK/provider connections.
"""
import asyncio
import json
from pathlib import Path
import tempfile
import time

from fastapi import Request
import pytest
from atlas_gate.gate.auth_recovery import refresh
from .test_network_gate import network, create


async def main():
    with tempfile.TemporaryDirectory(prefix='gate-auth97-java-') as root:
        patch = pytest.MonkeyPatch()
        fixture = network.__wrapped__(Path(root), patch)
        client, app, routers, settings, creds, other = await anext(fixture)
        try:
            # The standard network fixture's agent targets a scripted API model. This
            # acceptance uses its existing stub Router route, with an explicit compatible agent.
            state = app.state.gate
            org = state.orgs['test-org'].model_dump(mode='json')
            org['agents'] = [dict(id='stub-agent',name='Stub acceptance',version='1',default=True,
                model='inherit',allowed_routes=['claude-sub'],tools_allowlist=['*'],system_prompt='test')]
            directory = Path(settings.ATLAS_GATE_ORGS_DIR)
            directory.mkdir(parents=True,exist_ok=True)
            (directory/'test-org.json').write_text(json.dumps(org),encoding='utf8')
            await state.reload()
            sid = await create(client)
            sent = await client.post('/harness/agent/sessions/'+sid+'/prompt?mode=async',
                json={'id':'seed-once','text':'saved history marker'})
            assert sent.status_code == 202, sent.text
            while True:
                old = (await client.get('/harness/agent/sessions/'+sid+'/turns/seed-once/events',params={'wait':200})).json()
                if old['done']: break

            @app.get('/__test/auth97/setup')
            async def setup(request: Request):
                assert request.headers.get('X-Test-Key') == 'isolated-auth97'
                return dict(credentials=creds,session_id=sid)

            @app.post('/__test/auth97/rotate')
            async def rotate(request: Request):
                assert request.headers.get('X-Test-Key') == 'isolated-auth97'
                import uuid
                current = json.loads(state.keyring.open(state.store._one('SELECT payload FROM auth_current WHERE device_id=?',(creds['device_id'],))['payload']))
                newer = refresh(state,current['refresh_token'],str(uuid.uuid4()))
                refresh(state,newer['refresh_token'],str(uuid.uuid4()))
                state.store._exec('UPDATE auth_access SET expires_at=? WHERE device_id=?',(time.time()-1,creds['device_id']))
                return {'ok':True}

            @app.post('/__test/auth97/confirm')
            async def confirm(request: Request):
                assert request.headers.get('X-Test-Key') == 'isolated-auth97'
                start = (await client.post('/harness/auth/v1/enroll/start',json=dict(device_name='same-device recovery',platform='edt',app_version='test'))).json()
                approved = await client.post('/harness/auth/v1/enroll/approve',headers={'X-Atlas-Token':'admin'},json=dict(
                    user_code=start['user_code'],org='test-org',user={'id':'first'},recover_device_id=creds['device_id']))
                assert approved.status_code == 200, approved.text
                result = await client.post('/harness/auth/v1/enroll/poll',json={'device_code':start['device_code']})
                assert result.status_code == 200, result.text
                return result.json()

            @app.get('/__test/auth97/state')
            async def inspect(request: Request):
                assert request.headers.get('X-Test-Key') == 'isolated-auth97'
                binding = state.store._one('SELECT id,device_id,local_id,node_id FROM router_bindings WHERE id=?',(sid,))
                count = state.store._one("SELECT COUNT(*) n FROM router_operations WHERE turn_id='seed-once'")['n']
                total = state.store._one('SELECT COUNT(*) n FROM router_operations')['n']
                return dict(binding=binding,seed_operations=count,total_operations=total,
                    devices=len(state.store.devices()))

            print(json.dumps({'url':str(client.base_url)}),flush=True)
            await asyncio.Event().wait()
        finally:
            await fixture.aclose()
            patch.undo()


if __name__ == '__main__':
    asyncio.run(main())
