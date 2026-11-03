import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
import zipfile
import httpx
import pytest


@pytest.fixture
def guard_module(monkeypatch,tmp_path):
    if os.name=='nt':
        monkeypatch.setitem(sys.modules,'fcntl',SimpleNamespace(LOCK_EX=1,LOCK_NB=2,flock=lambda *a:None))
    spec=importlib.util.spec_from_file_location('gate_guard',Path(__file__).parents[1]/'deploy/gate_activate.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.API_FLAG=tmp_path/'api-v1'
    module.STEER_FLAG=tmp_path/'steer-v1'
    return module


@pytest.mark.parametrize('failure', [None,'busy','qualification','maintenance','startup','rollback',
                                     'drain_ack','stop_ack','contract','undrain_ack','legacy_rollback'])
@pytest.mark.skipif(os.name=='nt',reason='Linux systemd/symlink deployment guard')
def test_guard_failure_boundaries_and_rollback(tmp_path,monkeypatch,failure,guard_module):
    m=guard_module
    root=tmp_path/'atlas';releases=root/'releases';releases.mkdir(parents=True)
    old=releases/'gate-old';old.mkdir();candidate=releases/'gate-new';candidate.mkdir()
    (root/'gate').symlink_to(old,target_is_directory=True)
    package=candidate/'.venv/lib/python3.12/site-packages/atlas_gate';package.mkdir(parents=True)
    (package/'__init__.py').write_bytes(b'test')
    wheel=tmp_path/'candidate.whl'
    with zipfile.ZipFile(wheel,'w') as z:z.writestr('atlas_gate/__init__.py',b'test')
    sha=hashlib.sha256(wheel.read_bytes()).hexdigest();tests=tmp_path/'tests.json'
    tests.write_text(json.dumps({'status':'passed','wheelSha256':sha}))
    site=tmp_path/'site';site.write_text('include /etc/nginx/snippets/atlas-gate-maintenance.conf;')
    backups=tmp_path/'backups';backups.mkdir();database=tmp_path/'gate.db';sqlite3.connect(database).close()
    for name,value in dict(ROOT=root,FLAG=tmp_path/'maintenance',ADMISSION_FLAG=tmp_path/'admission',API_FLAG=tmp_path/'api-v1',SITE=site,LOCK=tmp_path/'lock',BACKUPS=backups,DATABASE=database).items():monkeypatch.setattr(m,name,value)
    monkeypatch.setattr(m.os,'geteuid',lambda:0,raising=False)
    monkeypatch.setattr(sys,'argv',['guard','--candidate',str(candidate),'--wheel',str(wheel),'--sha256',sha,'--tests-proof',str(tests)])
    events=[];draining=set();failed=False
    def states():return {k:{'ready':True,'draining':k in draining,
        'bootId':k+('-changed' if failure=='legacy_rollback' and (root/'gate').resolve()==candidate else ''),
        'activeTurns':int(failure=='busy'),'activeSubagents':0,'reservedUnits':0} for k in ('router','router-claude')}
    def node(kind,drain=None):
        nonlocal failed
        if drain:draining.add(kind)
        else:draining.discard(kind)
        events.append(('drain',kind,drain))
        if not failed and (failure=='drain_ack' and drain or failure=='undrain_ack' and drain is False):
            failed=True;raise RuntimeError('lost drain ACK after applying control')
    def run(args,**kw):
        nonlocal failed
        args=list(map(str,args));events.append(tuple(args))
        if 'atlas_gate.admission_check' in args:
            if failure=='qualification':raise RuntimeError('failed qualification')
            return SimpleNamespace(stdout=json.dumps({'status':'passed','version':'0.4.17'}))
        if args==['systemctl','start','atlas-gate'] and failure in ('startup','rollback') and (not failed or failure=='rollback'):
            failed=True;raise RuntimeError('failed startup')
        if args==['systemctl','stop','atlas-gate'] and failure=='stop_ack' and not failed:
            failed=True;raise RuntimeError('lost stop ACK after stopping service')
        return SimpleNamespace()
    class Client:
        def __init__(self,*a,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def get(self,*a,**kw):
            assert m.FLAG.exists()
            return SimpleNamespace(status_code=200 if failure=='maintenance' else 503,
                json=lambda:{'error':{'code':'gate_maintenance'}},headers={'Retry-After':'1'})
        def post(self,*a,**kw):
            assert m.ADMISSION_FLAG.exists() and not m.FLAG.exists()
            return SimpleNamespace(status_code=503,json=lambda:{'error':{'code':'gate_maintenance'}})
    monkeypatch.setattr(m,'states',states);monkeypatch.setattr(m,'node',node);monkeypatch.setattr(m,'run',run)
    monkeypatch.setattr(m,'health',lambda:{'version':'0.4.16'})
    monkeypatch.setattr(m,'ready',lambda version:events.append(('ready',version)))
    monkeypatch.setattr(m,'assert_gate_idle',lambda:None)
    monkeypatch.setattr(m,'assert_contract_lifecycle',lambda:None)
    monkeypatch.setattr(m,'assert_steer_ingress',lambda:None)
    monkeypatch.setattr(m,'sync_steer_contract',lambda expected_version=None: expected_version!='0.4.16')
    real_sync=m.sync_api_contract
    if failure=='legacy_rollback':
        m.API_FLAG.write_text('orphan-positive')
        m.API_FLAG.with_name(m.API_FLAG.name+'.pending').write_text('orphan-staging')
        monkeypatch.setattr(m,'gate_runtime',lambda: {'gateVersion':'0.4.17','apiAttemptContractVersion':1}
                            if (root/'gate').resolve()==candidate else None)
    def sync_contract(expected_version=None):
        events.append(('api-contract',expected_version))
        if failure=='legacy_rollback':
            result=real_sync(expected_version)
            if expected_version=='0.4.17':assert result and m.API_FLAG.read_text()=='0.4.17'
            return result
        return not (failure=='contract' and expected_version=='0.4.17')
    monkeypatch.setattr(m,'sync_api_contract',sync_contract)
    monkeypatch.setattr(m,'drain_ingress',lambda:events.append(('ingress-drain',m.FLAG.exists())))
    def wait_idle():
        assert m.ADMISSION_FLAG.exists() and not m.FLAG.exists()
        events.append(('wait-idle',False))
    monkeypatch.setattr(m,'wait_idle',wait_idle)
    monkeypatch.setattr(m.httpx,'Client',Client)
    if failure:
        with pytest.raises((RuntimeError,AssertionError)):m.main()
        assert (root/'gate').resolve()==(candidate if failure=='undrain_ack' else old)
    else:
        m.main();assert (root/'gate').resolve()==candidate
    if failure in ('rollback','undrain_ack'):
        assert m.FLAG.exists() and m.ADMISSION_FLAG.exists()
        assert draining==({'router','router-claude'} if failure=='rollback' else {'router-claude'})
    else:assert not m.FLAG.exists() and not m.ADMISSION_FLAG.exists() and not draining
    if failure in ('busy','qualification','maintenance','drain_ack'):
        assert not any(e[:1]==('systemctl',) for e in events)
    if failure in ('startup','stop_ack','contract'):assert ('ready','0.4.16') in events
    if failure=='legacy_rollback':
        assert ('api-contract','0.4.17') in events and ('api-contract','0.4.16') in events
        assert not m.API_FLAG.exists() and not m.API_FLAG.with_name(m.API_FLAG.name+'.pending').exists()
    # nginx -t intentionally precedes the deployment lock. Capability sync must
    # precede node admission checks, not that side-effect-free syntax check.
    assert events.index(('api-contract',None)) < next(
        (i for i,e in enumerate(events) if e[0] in ('drain','ingress-drain') or 'atlas_gate.admission_check' in e), len(events))
    if failure is None:
        assert [('ingress-drain',False),('wait-idle',False),('ingress-drain',True)] == [
            event for event in events if event[0] in ('ingress-drain','wait-idle')]


@pytest.mark.parametrize('runtime', [None, {'gateVersion':'legacy'},
    {'gateVersion':'0.4.20','apiAttemptContractVersion':True},
    {'gateVersion':'0.4.20','apiAttemptContractVersion':'1'}])
def test_stale_positive_and_pending_do_not_authorize_unsupported_runtime(tmp_path,monkeypatch,guard_module,runtime):
    m=guard_module;flag=tmp_path/'api-v1';pending=tmp_path/'api-v1.pending'
    flag.write_text('stale-positive');pending.write_text('stale-staging')
    monkeypatch.setattr(m,'API_FLAG',flag);monkeypatch.setattr(m,'gate_runtime',lambda:runtime)
    assert m.sync_api_contract() is False
    assert not flag.exists() and not pending.exists()


def test_contract_probe_failure_leaves_managed_ingress_fenced(tmp_path,monkeypatch,guard_module):
    m=guard_module;flag=tmp_path/'api-v1';flag.write_text('stale-positive')
    monkeypatch.setattr(m,'API_FLAG',flag)
    def failed():
        assert not flag.exists()  # no fail-open window while waiting for the probe
        raise RuntimeError('runtime probe unavailable')
    monkeypatch.setattr(m,'gate_runtime',failed)
    with pytest.raises(RuntimeError,match='probe unavailable'):m.sync_api_contract()
    assert not flag.exists()


def test_contract_sync_recovers_orphan_pending_and_binds_ready_version(tmp_path,monkeypatch,guard_module):
    m=guard_module;flag=tmp_path/'api-v1';pending=tmp_path/'api-v1.pending'
    pending.write_text('crashed-before-publish')
    monkeypatch.setattr(m,'API_FLAG',flag)
    monkeypatch.setattr(m,'gate_runtime',lambda:{'gateVersion':'0.4.20','apiAttemptContractVersion':1})
    assert m.sync_api_contract('0.4.20') is True
    assert flag.read_text()=='0.4.20' and not pending.exists()
    with pytest.raises(RuntimeError,match='identity'):m.sync_api_contract('0.4.21')
    assert not flag.exists() and not pending.exists()


def test_contract_publish_failure_cleans_staging_and_never_leaves_positive_marker(tmp_path,monkeypatch,guard_module):
    m=guard_module;flag=tmp_path/'api-v1';pending=tmp_path/'api-v1.pending'
    monkeypatch.setattr(m,'API_FLAG',flag)
    monkeypatch.setattr(m,'gate_runtime',lambda:{'gateVersion':'0.4.20','apiAttemptContractVersion':1})
    original=m.os.replace
    def fail(source,target):
        if target==flag:raise OSError('inert publish failure')
        return original(source,target)
    monkeypatch.setattr(m.os,'replace',fail)
    with pytest.raises(OSError,match='publish failure'):m.sync_api_contract('0.4.20')
    assert not flag.exists() and not pending.exists()


def test_startup_refresh_rebuilds_run_marker_without_deploy_lock_or_activation(tmp_path,monkeypatch,guard_module):
    m=guard_module;flag=tmp_path/'api-v1';pending=tmp_path/'api-v1.pending'
    flag.write_text('stale');pending.write_text('interrupted-startup')
    monkeypatch.setattr(m,'API_FLAG',flag);monkeypatch.setattr(m,'LOCK',tmp_path/'must-not-lock')
    probes=[]
    def health():
        assert not flag.exists() and not pending.exists()
        probes.append(True)
        if len(probes)==1:raise httpx.ConnectError('inert startup')
        return {'version':'0.4.20'}
    def forbidden(*a,**kw):raise AssertionError('refresh must not activate or call Router')
    monkeypatch.setattr(m,'health',health)
    monkeypatch.setattr(m,'gate_runtime',lambda:{'gateVersion':'0.4.20','apiAttemptContractVersion':1})
    monkeypatch.setattr(m.time,'sleep',lambda seconds:None)
    monkeypatch.setattr(m,'run',forbidden);monkeypatch.setattr(m,'node',forbidden)
    result=m.refresh_api_contract(attempts=2)
    assert result=={'status':'passed','gateVersion':'0.4.20','managedApiIngressAllowed':True,'steerIngressAllowed':False}
    assert len(probes)==2 and flag.read_text()=='0.4.20' and not m.LOCK.exists()


def test_startup_legacy_runtime_cannot_reenable_managed_posts(tmp_path,monkeypatch,guard_module):
    m=guard_module;flag=tmp_path/'api-v1';flag.write_text('old-positive')
    monkeypatch.setattr(m,'API_FLAG',flag);monkeypatch.setattr(m,'health',lambda:{'version':'0.4.19'})
    monkeypatch.setattr(m,'gate_runtime',lambda:None)
    assert m.refresh_api_contract(attempts=1)=={
        'status':'unsupported','gateVersion':'0.4.19','managedApiIngressAllowed':False,'steerIngressAllowed':False}
    assert not flag.exists()


@pytest.mark.parametrize('runtime',[None,{'gateVersion':'0.4.21','apiAttemptContractVersion':1},
    {'gateVersion':'0.4.22','steerContractVersion':True},
    {'gateVersion':'0.4.22','steerContractVersion':'1'}])
def test_steer_contract_never_trusts_stale_marker_or_legacy_runtime(guard_module,monkeypatch,runtime):
    m=guard_module;flag=m.STEER_FLAG;pending=flag.with_name(flag.name+'.pending')
    flag.write_text('new-runtime-stale');pending.write_text('crashed')
    monkeypatch.setattr(m,'gate_runtime',lambda:runtime)
    assert m.sync_steer_contract() is False
    assert not flag.exists() and not pending.exists()


def test_startup_refresh_contracts_handles_reboot_then_old_gate_rollback(guard_module,monkeypatch):
    m=guard_module
    runtime={'gateVersion':'0.4.22','apiAttemptContractVersion':1,'steerContractVersion':1}
    monkeypatch.setattr(m,'gate_runtime',lambda:runtime)
    def health():
        assert not m.STEER_FLAG.exists() and not m.STEER_FLAG.with_name(m.STEER_FLAG.name+'.pending').exists()
        return {'version':runtime['gateVersion']}
    monkeypatch.setattr(m,'health',health)
    m.STEER_FLAG.write_text('stale');m.STEER_FLAG.with_name(m.STEER_FLAG.name+'.pending').write_text('crash')
    assert m.refresh_api_contract(attempts=1)['steerIngressAllowed'] is True
    assert m.STEER_FLAG.read_text()=='0.4.22'
    runtime={'gateVersion':'0.4.21','apiAttemptContractVersion':1}
    result=m.refresh_api_contract(attempts=1)
    assert result['managedApiIngressAllowed'] is True and result['steerIngressAllowed'] is False
    assert not m.STEER_FLAG.exists()


def test_activation_requires_persistent_startup_marker_cleanup(guard_module,monkeypatch):
    m=guard_module
    pre='/usr/bin/rm -f '+ ' '.join(str(f)+suffix for f in (m.API_FLAG,m.STEER_FLAG) for suffix in ('','.pending'))
    unit='ExecStartPre='+pre+'\nExecStartPost=/usr/bin/systemctl --no-block start atlas-gate-api-contract-refresh.service'
    monkeypatch.setattr(m,'run',lambda *a,**k:SimpleNamespace(stdout=unit))
    m.assert_contract_lifecycle()
    unit=unit.replace(str(m.STEER_FLAG)+'.pending','')
    with pytest.raises(RuntimeError,match='startup hooks'):m.assert_contract_lifecycle()
    template=(Path(__file__).parents[1]/'deploy/atlas-gate-api-contract.conf').read_text()
    assert '/run/atlas-gate-steer-v1.pending' in template and 'ExecStartPre=+' in template


@pytest.mark.parametrize('failure',['health','runtime','invalid_runtime'])
def test_startup_probe_failure_is_nonfatal_after_fencing_healthy_io(tmp_path,monkeypatch,guard_module,failure):
    m=guard_module;flag=tmp_path/'api-v1';flag.write_text('old-positive')
    monkeypatch.setattr(m,'API_FLAG',flag)
    def unavailable():raise httpx.ConnectError('inert failed probe')
    monkeypatch.setattr(m,'health',unavailable if failure=='health' else lambda:{'version':'0.4.20'})
    monkeypatch.setattr(m,'gate_runtime',(lambda:['invalid runtime']) if failure=='invalid_runtime' else unavailable)
    result=m.refresh_api_contract(attempts=1)
    assert result['status']=='blocked' and result['managedApiIngressAllowed'] is False
    assert not flag.exists()


def test_refresh_cli_has_no_activation_arguments_or_side_effects(monkeypatch,guard_module,capsys):
    m=guard_module;calls=[]
    monkeypatch.setattr(m.os,'geteuid',lambda:0,raising=False)
    monkeypatch.setattr(sys,'argv',['guard','--refresh-api-contract'])
    monkeypatch.setattr(m,'refresh_api_contract',lambda:calls.append('refresh') or {
        'status':'blocked','managedApiIngressAllowed':False})
    def forbidden(*a,**kw):raise AssertionError('refresh CLI must not activate')
    monkeypatch.setattr(m,'states',forbidden);monkeypatch.setattr(m,'run',forbidden)
    m.main()
    assert calls==['refresh'] and json.loads(capsys.readouterr().out)['status']=='blocked'
    monkeypatch.setattr(sys,'argv',['guard','--refresh-api-contract','--candidate','unused'])
    with pytest.raises(SystemExit) as denied:m.main()
    assert denied.value.code==2 and calls==['refresh']


def test_refresh_cli_requires_root_before_touching_markers(monkeypatch,guard_module):
    m=guard_module
    monkeypatch.setattr(m.os,'geteuid',lambda:1000,raising=False)
    monkeypatch.setattr(sys,'argv',['guard','--refresh-api-contract'])
    def forbidden():raise AssertionError('non-root refresh must not mutate markers')
    monkeypatch.setattr(m,'refresh_api_contract',forbidden)
    with pytest.raises(PermissionError):m.main()


@pytest.mark.skipif(os.name!='posix' or not shutil.which('nginx'),reason='requires isolated Linux Nginx; unavailable on Windows')
def test_unprivileged_nginx_contract_fence_and_existing_stream_callbacks(tmp_path):
    """Own high ports, own PID/config/markers; never signals an installed Nginx."""
    if os.geteuid()==0:
        pytest.skip('run this isolated Nginx check as an unprivileged user')
    done=threading.Event();calls=[]
    class Upstream(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def do_GET(self):self.reply()
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length','0')))
            if self.path.endswith('/tool_result'):done.set()
            if self.path.endswith('/prompt'):
                self.send_response(200);self.send_header('Content-Type','text/event-stream')
                self.send_header('Transfer-Encoding','chunked');self.end_headers()
                def chunk(value):
                    self.wfile.write(('%x\r\n'%len(value)).encode()+value+b'\r\n');self.wfile.flush()
                chunk(b'data: waiting-tool\n\n')
                if done.wait(5):chunk(b'data: terminal\n\n')
                self.wfile.write(b'0\r\n\r\n');self.wfile.flush()
                return
            self.reply()
        def reply(self):
            calls.append((self.command,self.path))
            body=b'{"upstream":true}'
            self.send_response(200);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    upstream=ThreadingHTTPServer(('127.0.0.1',0),Upstream)
    thread=threading.Thread(target=upstream.serve_forever,daemon=True);thread.start()
    with socket.socket() as available:
        available.bind(('127.0.0.1',0));port=available.getsockname()[1]
    maintenance=tmp_path/'maintenance';admission=tmp_path/'admission';api=tmp_path/'api-v1';steer=tmp_path/'steer-v1'
    snippet=(Path(__file__).parents[1]/'deploy/nginx-gate-maintenance.conf').read_text()
    snippet=snippet.replace('/run/atlas-gate-maintenance',str(maintenance)).replace('/run/atlas-gate-admission',str(admission)).replace('/run/atlas-gate-api-attempt-v1',str(api)).replace('/run/atlas-gate-steer-v1',str(steer))
    include=tmp_path/'policy.conf';include.write_text(snippet)
    for directory in ('client-body','proxy','fastcgi','uwsgi','scgi'):
        (tmp_path/directory).mkdir(mode=0o700)
    config=tmp_path/'nginx.conf'
    config.write_text(f'''daemon off;
pid "{tmp_path/'nginx.pid'}";
error_log "{tmp_path/'error.log'}";
events {{ worker_connections 64; }}
http {{ access_log off;
 client_body_temp_path "{tmp_path/'client-body'}";
 proxy_temp_path "{tmp_path/'proxy'}";
 fastcgi_temp_path "{tmp_path/'fastcgi'}";
 uwsgi_temp_path "{tmp_path/'uwsgi'}";
 scgi_temp_path "{tmp_path/'scgi'}";
 server {{ listen 127.0.0.1:{port}; include "{include}";
  location / {{ proxy_pass http://127.0.0.1:{upstream.server_port}; proxy_http_version 1.1;
   proxy_buffering off; proxy_read_timeout 10s; }}
 }}
}}
''')
    command=[shutil.which('nginx'),'-p',str(tmp_path),'-c',str(config)]
    process=None
    try:
        with (tmp_path/'startup.log').open('wb') as startup:
            process=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=startup)
        with httpx.Client(base_url=f'http://127.0.0.1:{port}',trust_env=False,timeout=3) as client:
            for _ in range(50):
                try:
                    if client.get('/harness/health').status_code==200:break
                except httpx.HTTPError:pass
                if process.poll() is not None:
                    errors=tmp_path/'error.log'
                    pytest.fail((errors if errors.exists() else tmp_path/'startup.log').read_text())
                time.sleep(.05)
            else:pytest.fail('isolated Nginx not ready')
            path='/harness/llm/v1/chat/completions';headers={'X-Atlas-API-Attempt-Contract':'1'}
            before=len(calls)
            assert client.post(path,json={},headers=headers).json()['error']['code']=='api_attempt_contract_unavailable'
            api.with_name(api.name+'.pending').touch()
            assert client.post(path,json={},headers=headers).status_code==503 and len(calls)==before
            assert client.post(path,json={}).status_code==200  # legacy behavior remains explicit
            api.write_text('0.4.20')
            assert client.post(path,json={},headers=headers).status_code==200
            steer_path='/harness/agent/sessions/s/steer'
            strong={'X-Atlas-Steer-Contract':'1','X-Atlas-Steer-Target-Boot':'captured-boot'}
            before=len(calls)
            for endpoint in (steer_path,'/harness/agent/sessions/s/commands','/harness/agent/sessions/s/commands/c'):
                response=client.post(endpoint,json={},headers=strong) if endpoint==steer_path else client.get(endpoint,headers=strong)
                assert response.status_code==503 and response.json()['error']['code']=='steer_contract_unavailable'
                assert 'X-Atlas-Command-State' not in response.headers
            assert len(calls)==before
            steer.with_name(steer.name+'.pending').touch()
            assert client.post(steer_path,json={},headers=strong).status_code==503 and len(calls)==before
            assert client.post(steer_path,json={'text':'legacy'}).status_code==200
            steer.write_text('new-runtime')
            assert client.post(steer_path,json={},headers=strong).status_code==200
            steer.unlink()  # immediately visible without nginx reload/stat cache
            before=len(calls)
            assert client.post(steer_path,json={},headers=strong).status_code==503 and len(calls)==before
            with client.stream('POST','/harness/agent/sessions/s/prompt',json={}) as stream:
                iterator=stream.iter_text();assert 'waiting-tool' in next(iterator)
                admission.touch()
                subprocess.run(command+['-s','reload'],check=True,capture_output=True)
                # A separate pool guarantees a new downstream TCP connection;
                # the old worker still owns the streaming request above.
                with httpx.Client(base_url=f'http://127.0.0.1:{port}',trust_env=False,timeout=3) as callbacks:
                    assert callbacks.post('/harness/agent/sessions',json={}).status_code==503
                    assert callbacks.post(path,json={},headers=headers).json()['error']['code']=='gate_maintenance'
                    assert callbacks.post('/harness/agent/sessions/s/tool_result',json={}).status_code==200
                assert 'terminal' in ''.join(iterator)
            assert client.post('/harness/agent/sessions/s/interrupt',json={}).status_code==200
            assert client.get('/harness/agent/sessions/s/turns/t/receipt').status_code==200
            maintenance.touch()
            full=client.get('/harness/health')
            assert full.status_code==503 and full.headers['Retry-After']=='1'
            assert 'X-Atlas-Command-State' not in full.headers
            maintenance.unlink();admission.unlink();api.unlink()  # legacy rollback, contract remains blocked
            assert client.post(path,json={},headers=headers).json()['error']['code']=='api_attempt_contract_unavailable'
    finally:
        done.set()
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGQUIT)
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        upstream.shutdown();upstream.server_close();thread.join(timeout=2)


@pytest.mark.asyncio
async def test_direct_maintenance_blocks_admission_without_fabricating_receipt(network,tmp_path,monkeypatch):
    from .test_network_gate import create
    from atlas_gate.gate import maintenance
    c,app,*_=network;sid=await create(c)
    flag=tmp_path/'maintenance';flag.touch();monkeypatch.setattr(maintenance,'FLAG',flag)
    r=await c.post(f'/harness/agent/sessions/{sid}/prompt?mode=async',json={'id':'t','text':'work'})
    assert r.status_code==503 and r.json()['error']['code']=='gate_maintenance'
    assert r.headers['Retry-After']=='1' and 'X-Atlas-Command-State' not in r.headers
    assert app.state.gate.store._one('SELECT COUNT(*) n FROM router_operations')['n']==0
    assert (await c.get('/harness/health')).status_code==200


from .test_network_gate import network
