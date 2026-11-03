"""Actual historical Gate/Router + own unprivileged Nginx, never installed services."""
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid

import httpx
import pytest

from .test_admission_deploy import guard_module  # noqa: F401


def free_port():
    with socket.socket() as available:
        available.bind(('127.0.0.1',0))
        return available.getsockname()[1]


def stop_owned(process,nginx=False):
    if process is not None and process.poll() is None:
        process.send_signal(signal.SIGQUIT if nginx else signal.SIGTERM)
        try:process.wait(timeout=8)
        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)


@pytest.mark.skipif(os.name!='posix' or not shutil.which('nginx'),reason='requires unprivileged isolated Linux Nginx')
def test_real_old_gate_router_cannot_receive_cached_strong_intent_after_rollback(tmp_path,monkeypatch,guard_module):
    if os.geteuid()==0:pytest.skip('requires unprivileged UID and own Nginx')
    roots=[os.environ.get(name) for name in ('ATLAS_TEST_LEGACY_GATE_ROOT','ATLAS_TEST_LEGACY_ROUTER_ROOT')]
    if not all(roots):pytest.skip('requires pinned legacy Gate/Router source checkouts')
    gate_root,router_root=map(lambda p:Path(p).resolve(),roots)
    gate_port,router_port,ingress_port=(free_port() for _ in range(3))
    env={k:v for k,v in os.environ.items() if k not in ('SCRIPTED_OPENAI_KEY','SCRIPTED_ANTHROPIC_KEY')}
    env['PYTHONPATH']=os.pathsep.join(map(str,(gate_root,router_root)))
    fixture=Path(__file__).parent/'fixtures/legacy_steer_runtime.py'
    legacy=None;nginx=None
    log=tmp_path/'legacy.log'
    try:
        with log.open('wb') as output:
            legacy=subprocess.Popen([sys.executable,str(fixture),str(tmp_path),str(gate_port),str(router_port)],
                cwd=tmp_path,env=env,stdout=output,stderr=subprocess.STDOUT)
        with httpx.Client(base_url=f'http://127.0.0.1:{gate_port}',trust_env=False,timeout=5) as direct:
            for _ in range(200):
                try:
                    if direct.get('/harness/health').status_code==200:break
                except httpx.HTTPError:pass
                if legacy.poll() is not None:pytest.fail(log.read_text(errors='replace'))
                time.sleep(.05)
            else:pytest.fail('legacy runtime startup timeout: '+log.read_text(errors='replace'))
            start=direct.post('/harness/enroll/start',json={'device_name':'rollback-qa','platform':'test','app_version':'1'}).json()
            assert direct.post('/harness/enroll/approve',headers={'X-Atlas-Token':'admin'},json={
                'user_code':start['user_code'],'org':'test-org','user':{'id':'qa','email':'qa@test.invalid','name':'qa'}}).status_code==200
            token=direct.post('/harness/enroll/poll',json={'device_code':start['device_code']}).json()['device_token']
            auth={'Authorization':'Bearer '+token}
            created=direct.post('/harness/agent/sessions',headers=auth,json={'model':'stub-fast','system':'test',
                'tools':[{'name':'echo','description':'echo','parameters':{'type':'object'}}]})
            assert created.status_code==200,created.text
            prefix='/harness/agent/sessions/'+created.json()['id']
            started=direct.post(prefix+'/prompt?mode=async',headers=auth,json={'id':'different-current-turn','text':'call:echo'})
            assert started.status_code==202,started.text
            for _ in range(30):
                events=direct.get(prefix+'/turns/different-current-turn/events',headers=auth,params={'wait':200}).json()['events']
                if any(e['type']=='tool_call' for e in events):break
            else:pytest.fail('scripted legacy SDK did not reach tool wait')
            flags={name:tmp_path/name for name in ('maintenance','admission','api-v1','steer-v1')}
            source=(Path(__file__).parents[1]/'deploy/nginx-gate-maintenance.conf').read_text()
            for actual,name in (('/run/atlas-gate-maintenance','maintenance'),('/run/atlas-gate-admission','admission'),
                ('/run/atlas-gate-api-attempt-v1','api-v1'),('/run/atlas-gate-steer-v1','steer-v1')):
                source=source.replace(actual,str(flags[name]))
            include=tmp_path/'policy.conf';include.write_text(source)
            for name in ('client-body','proxy','fastcgi','uwsgi','scgi'):(tmp_path/name).mkdir(mode=0o700)
            config=tmp_path/'nginx.conf'
            config.write_text(f'''daemon off;
pid "{tmp_path/'nginx.pid'}";
error_log "{tmp_path/'nginx.log'}";
events {{ worker_connections 64; }}
http {{ access_log off;
 client_body_temp_path "{tmp_path/'client-body'}";
 proxy_temp_path "{tmp_path/'proxy'}";
 fastcgi_temp_path "{tmp_path/'fastcgi'}";
 uwsgi_temp_path "{tmp_path/'uwsgi'}";
 scgi_temp_path "{tmp_path/'scgi'}";
 server {{ listen 127.0.0.1:{ingress_port}; include "{include}";
  location / {{ proxy_pass http://127.0.0.1:{gate_port}; proxy_http_version 1.1; }}
 }}
}}
''')
            with (tmp_path/'nginx-startup.log').open('wb') as output:
                nginx=subprocess.Popen([shutil.which('nginx'),'-p',str(tmp_path),'-c',str(config)],
                                        stdout=output,stderr=subprocess.STDOUT)
            with httpx.Client(base_url=f'http://127.0.0.1:{ingress_port}',trust_env=False,timeout=5) as client, \
                 httpx.Client(base_url=f'http://127.0.0.1:{router_port}',trust_env=False,timeout=5) as native:
                for _ in range(100):
                    try:
                        if client.get('/harness/health').status_code==200:break
                    except httpx.HTTPError:pass
                    if nginx.poll() is not None:pytest.fail((tmp_path/'nginx.log').read_text())
                    time.sleep(.05)
                else:pytest.fail('isolated nginx startup timeout')
                def evidence():return native.get('/qa/legacy-steer').json()
                proof=evidence()
                assert proof['gateVersion']=='0.4.21' and proof['routerVersion']=='0.8.21'
                assert Path(proof['gateSource']).is_relative_to(gate_root)
                assert Path(proof['routerSource']).is_relative_to(router_root)
                # Actual startup probe on old Gate clears even stale positive/pending markers.
                guard=guard_module
                monkeypatch.setattr(guard,'API_FLAG',flags['api-v1']);monkeypatch.setattr(guard,'STEER_FLAG',flags['steer-v1'])
                monkeypatch.setattr(guard,'health',lambda:direct.get('/harness/health').json())
                monkeypatch.setattr(guard,'gate_runtime',lambda:direct.get('/harness/admin/runtime',headers={'X-Atlas-Token':'admin'}).json())
                flags['steer-v1'].write_text('new-runtime-stale')
                flags['steer-v1'].with_name('steer-v1.pending').write_text('crash')
                assert guard.refresh_api_contract(attempts=1)['steerIngressAllowed'] is False
                body={'expectedTurnId':'old-intended-turn','clientMessageId':str(uuid.uuid4()),'text':'cached intent','attachments':[]}
                strong={**auth,'Idempotency-Key':'cached-original','X-Atlas-Steer-Contract':'1',
                        'X-Atlas-Steer-Target-Boot':'captured-old-boot'}
                before=len(evidence()['gateHits'])
                for endpoint in (prefix+'/steer',prefix+'/commands?requestId=cached-original',prefix+'/commands/cached'):
                    response=client.post(endpoint,json=body,headers=strong) if endpoint.endswith('/steer') else client.get(endpoint,headers=strong)
                    assert response.status_code==503 and response.json()['error']['code']=='steer_contract_unavailable'
                    assert response.json()['error']['state']=='unknown' and 'X-Atlas-Command-State' not in response.headers
                assert len(evidence()['gateHits'])==before and evidence()['records']==[]
                # Positive control proves the actual historical runtimes drop strong IDs
                # and steer the different current turn when no opt-in fence is requested.
                unsafe=client.post(prefix+'/steer',json=body,headers={**auth,'Idempotency-Key':'unfenced-control'})
                assert unsafe.status_code==200,unsafe.text
                assert evidence()['records']==[{'turnId':'different-current-turn','text':'cached intent'}]
                legacy_text=client.post(prefix+'/steer',json={'text':'old client'},headers=auth)
                assert legacy_text.status_code==200
                assert len(evidence()['records'])==2
                assert direct.post(prefix+'/interrupt',json={},headers=auth).status_code==200
    finally:
        stop_owned(nginx,nginx=True)
        stop_owned(legacy)
