#!/usr/bin/env python3
"""Single-writer Gate activation with an ingress admission barrier. Run as root."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import uuid
import zipfile
import httpx
from dotenv import dotenv_values

ROOT=Path('/opt/atlas');FLAG=Path('/run/atlas-gate-maintenance')
ADMISSION_FLAG=Path('/run/atlas-gate-admission')
API_FLAG=Path('/run/atlas-gate-api-attempt-v1')
SITE=Path('/etc/nginx/sites-enabled/atlas-gate');LOCK=Path('/run/lock/atlas-deploy.lock')
BACKUPS=Path('/var/backups/atlas');DATABASE=Path('/var/lib/atlas-gate/gate.db')


def run(args, **kwargs):
    return subprocess.run(list(map(str,args)),check=True,**kwargs)


def node(kind, drain=None):
    e=dotenv_values('/etc/atlas/'+kind+'.env');port=8765 if kind=='router' else 8768
    with httpx.Client(trust_env=False,timeout=75) as c:
        response=c.request('GET' if drain is None else 'POST',f'http://127.0.0.1:{port}/v1/node'+('' if drain is None else '/drain'),
            headers={'X-Atlas-Token':e['ATLAS_TOKEN']},json=None if drain is None else {'enabled':drain})
        response.raise_for_status();return response.json()


def states():
    return {k:node(k) for k in ('router','router-claude')}


def assert_idle(s):
    if not all(d['ready'] and not any(d[k] for k in ('activeTurns','activeSubagents','reservedUnits')) for d in s.values()):
        raise RuntimeError('Busy or unready Router; no restart')


def health():
    with httpx.Client(trust_env=False,timeout=15) as c:
        r=c.get('http://127.0.0.1:8766/harness/health');r.raise_for_status();return r.json()


def gate_runtime():
    env=dotenv_values('/etc/atlas/gate.env')
    with httpx.Client(trust_env=False,timeout=15) as c:
        r=c.get('http://127.0.0.1:8766/harness/admin/runtime',headers={'X-Atlas-Token':env['ATLAS_TOKEN']})
        if r.status_code==404:
            # Legacy Gate has no detached producers. Its already accepted HTTP
            # requests are drained by retiring Nginx workers, never by socket age.
            return None
        r.raise_for_status();return r.json()


def assert_gate_idle():
    state=gate_runtime()
    if state:
        if state.get('activeRequests') or state.get('activeApiAttempts'):
            raise RuntimeError('Busy Gate I/O or detached API producer; no restart')


def sync_api_contract(expected_version=None):
    """Publish capability only after a fresh probe during serialized cutover/startup.

    Absence is deliberately fail-closed in Nginx. An old positive marker or an
    orphaned staging file must never authorize inference on a legacy rollback.
    """
    pending=API_FLAG.with_name(API_FLAG.name+'.pending')
    API_FLAG.unlink(missing_ok=True)
    # The activation lock or systemd's serialized ExecStartPost excludes another
    # lifecycle writer. .pending is staging, never capability evidence.
    pending.unlink(missing_ok=True)
    state=gate_runtime()
    if state is not None and not isinstance(state,dict):
        raise RuntimeError('Invalid API contract runtime response')
    if not state or type(state.get('apiAttemptContractVersion')) is not int or state['apiAttemptContractVersion']!=1:
        return False
    version=state.get('gateVersion')
    if not isinstance(version,str) or not version or expected_version is not None and version!=expected_version:
        raise RuntimeError('API contract runtime identity does not match ready Gate')
    try:
        fd=os.open(pending,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o644)
        with os.fdopen(fd,'w') as stream:
            stream.write(version);stream.flush();os.fsync(stream.fileno())
        os.replace(pending,API_FLAG)
    finally:
        pending.unlink(missing_ok=True)
    return True


def refresh_api_contract(attempts=45):
    """ExecStartPost mode: no deploy lock, activation, Router calls or service stop.

    systemctl start in the activator already holds LOCK. A probe failure after
    fencing is reported without failing ExecStartPost and killing healthy I/O.
    Activation itself still requires a confirmed contract before opening ingress.
    """
    API_FLAG.unlink(missing_ok=True)
    API_FLAG.with_name(API_FLAG.name+'.pending').unlink(missing_ok=True)
    version=None
    for attempt in range(attempts):
        try:
            state=health()
            if not isinstance(state,dict) or not isinstance(state.get('version'),str) or not state['version']:
                return {'status':'blocked','reason':'invalid_startup_health','managedApiIngressAllowed':False}
            version=state['version'];break
        except httpx.HTTPError:
            if attempt+1<attempts:time.sleep(1)
    if version is None:
        return {'status':'blocked','reason':'startup_health_unavailable','managedApiIngressAllowed':False}
    try:
        enabled=sync_api_contract(version)
    except (httpx.HTTPError,OSError,RuntimeError,ValueError) as failed:
        API_FLAG.unlink(missing_ok=True)
        return {'status':'blocked','gateVersion':version,'reason':type(failed).__name__,
                'managedApiIngressAllowed':False}
    return {'status':'passed' if enabled else 'unsupported','gateVersion':version,
            'managedApiIngressAllowed':enabled}


def drain_ingress():
    """Graceful Nginx reload closes idle keep-alive but lets old requests finish."""
    master=Path('/run/nginx.pid').read_text().strip()
    if not master.isdigit() or b'nginx: master process' not in (Path('/proc')/master/'cmdline').read_bytes():
        raise RuntimeError('Cannot prove Nginx master identity')
    workers={}
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():continue
        try:
            fields=(path/'stat').read_text().split(') ',1)[1].split()
            if fields[1]==master and b'nginx: worker process' in (path/'cmdline').read_bytes():
                workers[path.name]=fields[19]
        except (OSError,IndexError):pass
    if not workers:raise RuntimeError('Cannot prove Nginx ingress ownership')
    run(['nginx','-s','reload'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    while workers:
        for pid,started in list(workers.items()):
            try:
                current=(Path('/proc')/pid/'stat').read_text().split(') ',1)[1].split()[19]
                if current!=started:workers.pop(pid)
            except (OSError,IndexError):workers.pop(pid)
        if workers:time.sleep(1)  # no wall-clock abort of a healthy existing request


def marker(path, token):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o644)
    with os.fdopen(fd,'w') as f:f.write(token)


def wait_idle():
    while True:
        try:
            assert_idle(states());assert_gate_idle();return
        except RuntimeError:
            time.sleep(1)


def ready(version):
    for _ in range(45):
        try:
            if health()['version']==version:return
        except httpx.HTTPError:pass
        time.sleep(1)
    raise RuntimeError('Gate readiness failed')


def link(target):
    p=ROOT/'gate.admission81-new';assert not p.exists() and not p.is_symlink()
    p.symlink_to(target);os.replace(p,ROOT/'gate')


def main():
    p=argparse.ArgumentParser();p.add_argument('--candidate',type=Path)
    p.add_argument('--wheel',type=Path);p.add_argument('--sha256')
    p.add_argument('--tests-proof',type=Path);p.add_argument('--refresh-api-contract',action='store_true');a=p.parse_args()
    if os.geteuid()!=0:raise PermissionError('deployment and startup refresh require root')
    activation=(a.candidate,a.wheel,a.sha256,a.tests_proof)
    if a.refresh_api_contract:
        if any(value is not None for value in activation):p.error('startup refresh cannot activate a release')
        print(json.dumps(refresh_api_contract()),flush=True);return
    if any(value is None for value in activation):p.error('activation requires candidate, wheel, sha256 and tests-proof')
    assert a.candidate.resolve()==a.candidate and a.candidate.parent==ROOT/'releases'
    assert hashlib.sha256(a.wheel.read_bytes()).hexdigest()==a.sha256
    package=a.candidate/'.venv/lib/python3.12/site-packages'
    with zipfile.ZipFile(a.wheel) as z:
        for name in z.namelist():
            if name.startswith('atlas_gate/') and not name.endswith('/'):
                assert (package/name).read_bytes()==z.read(name),'Installed wheel mismatch'
    tests=json.loads(a.tests_proof.read_text());assert tests['status']=='passed' and tests['wheelSha256']==a.sha256
    assert 'include /etc/nginx/snippets/atlas-gate-maintenance.conf;' in SITE.read_text()
    run(['nginx','-t'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    with open(LOCK,'a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        # Close any stale positive marker before busy/qualification checks can
        # abort this rollout. A failed probe leaves managed requests fenced.
        sync_api_contract()
        before=states();assert_idle(before);assert_gate_idle();assert not any(d['draining'] for d in before.values())
        env={k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME','VIRTUAL_ENV')}
        check=run([a.candidate/'.venv/bin/python','-m','atlas_gate.admission_check'],cwd='/tmp',env=env,capture_output=True,text=True)
        qualification=json.loads(check.stdout);assert qualification['status']=='passed'
        old=(ROOT/'gate').resolve();oldversion=health()['version'];version=qualification['version']
        assert tuple(map(int,version.split('.')))>tuple(map(int,oldversion.split('.'))),'Never downgrade/reinstall'
        backup=BACKUPS/('gate81-'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime()));backup.mkdir(mode=0o700)
        (backup/'qualification.json').write_text(json.dumps(qualification,indent=2));(backup/'qualification.json').chmod(0o600)
        token=str(uuid.uuid4());assert not FLAG.exists()
        marker(ADMISSION_FLAG,token)
        drained=[];stopped=False;service_ready=True
        try:
            # Phase 1 blocks NEW admission only. Already accepted SDK work can
            # still deliver tool_result/Stop/receipts while old HTTP streams drain.
            with httpx.Client(trust_env=False,timeout=20) as c:
                response=c.post('https://gate.atlcon.ru/harness/agent/sessions',json={})
                assert response.status_code==503 and response.json()['error']['code']=='gate_maintenance'
            for k in before:
                drained.append(k)  # persist/control intent before potentially lost ACK
                node(k,True)
            drain_ingress()
            wait_idle()
            # Phase 2 cuts ingress only after producers have settled. Retiring
            # workers also finish any read/context request admitted during phase1.
            marker(FLAG,token)
            with httpx.Client(trust_env=False,timeout=20) as c:
                response=c.get('https://gate.atlcon.ru/harness/health')
                assert response.status_code==503 and response.json()['error']['code']=='gate_maintenance'
                assert response.headers.get('Retry-After') and 'X-Atlas-Command-State' not in response.headers
            drain_ingress()
            assert_idle(states());assert_gate_idle()
            stopped=True;service_ready=False
            API_FLAG.unlink(missing_ok=True)
            run(['systemctl','stop','atlas-gate'])
            with sqlite3.connect('file:'+str(DATABASE)+'?mode=ro',uri=True) as src,sqlite3.connect(backup/'gate.db') as dst:src.backup(dst)
            (backup/'gate.db').chmod(0o600)
            link(a.candidate);run(['systemctl','start','atlas-gate']);ready(version)
            if not sync_api_contract(version):
                raise RuntimeError('Candidate did not confirm managed API attempt contract1')
            service_ready=True
            after=states();assert all(after[k]['bootId']==before[k]['bootId'] for k in before)
        except BaseException:
            if stopped:
                service_ready=False
                run(['systemctl','stop','atlas-gate'])
                if (ROOT/'gate').resolve()!=old:link(old)
                service_ready=False
                run(['systemctl','start','atlas-gate']);ready(oldversion);sync_api_contract(oldversion);service_ready=True
            raise
        finally:
            if service_ready:
                for k in drained:node(k,False)
                for flag in (FLAG,ADMISSION_FLAG):
                    if flag.exists():
                        assert flag.read_text()==token,'Maintenance ownership changed; do not clear another rollout'
                        flag.unlink()
            else:
                print(json.dumps({'status':'recovery_required','maintenanceRetained':True,'backup':str(backup)}),flush=True)
        final=states();assert all(d['ready'] and not d['draining'] for d in final.values())
        proof={'status':'passed','version':version,'old':str(old),'new':str(a.candidate),'wheelSha256':a.sha256,
               'qualification':qualification,'backup':str(backup),'ingressMaintenanceVerified':True,
               'routerBootsUnchanged':all(final[k]['bootId']==before[k]['bootId'] for k in before),
               'maintenanceCleared':not FLAG.exists() and not ADMISSION_FLAG.exists(),
               'twoPhaseIngressDrain':True,'detachedApiDrain':True,'managedApiIngressAllowed':API_FLAG.exists()}
        (backup/'deployment.json').write_text(json.dumps(proof,indent=2));(backup/'deployment.json').chmod(0o600)
        print(json.dumps(proof),flush=True)


if __name__=='__main__':main()
