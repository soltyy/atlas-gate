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
    p=argparse.ArgumentParser();p.add_argument('--candidate',type=Path,required=True)
    p.add_argument('--wheel',type=Path,required=True);p.add_argument('--sha256',required=True)
    p.add_argument('--tests-proof',type=Path,required=True);a=p.parse_args()
    assert os.geteuid()==0 and a.candidate.resolve()==a.candidate and a.candidate.parent==ROOT/'releases'
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
        before=states();assert_idle(before);assert not any(d['draining'] for d in before.values())
        env={k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME','VIRTUAL_ENV')}
        check=run([a.candidate/'.venv/bin/python','-m','atlas_gate.admission_check'],cwd='/tmp',env=env,capture_output=True,text=True)
        qualification=json.loads(check.stdout);assert qualification['status']=='passed'
        old=(ROOT/'gate').resolve();oldversion=health()['version'];version=qualification['version']
        assert tuple(map(int,version.split('.')))>tuple(map(int,oldversion.split('.'))),'Never downgrade/reinstall'
        backup=BACKUPS/('gate81-'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime()));backup.mkdir(mode=0o700)
        (backup/'qualification.json').write_text(json.dumps(qualification,indent=2));(backup/'qualification.json').chmod(0o600)
        token=str(uuid.uuid4());fd=os.open(FLAG,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o644)
        with os.fdopen(fd,'w') as f:f.write(token)
        drained=[];stopped=False
        try:
            # The public listener proves the marker is active BEFORE stopping Gate.
            with httpx.Client(trust_env=False,timeout=20) as c:
                response=c.get('https://gate.atlcon.ru/harness/health')
                assert response.status_code==503 and response.json()['error']['code']=='gate_maintenance'
                assert response.headers.get('Retry-After') and 'X-Atlas-Command-State' not in response.headers
            for k in before:node(k,True);drained.append(k)
            assert_idle(states())
            run(['systemctl','stop','atlas-gate']);stopped=True
            with sqlite3.connect('file:'+str(DATABASE)+'?mode=ro',uri=True) as src,sqlite3.connect(backup/'gate.db') as dst:src.backup(dst)
            (backup/'gate.db').chmod(0o600)
            link(a.candidate);run(['systemctl','start','atlas-gate']);ready(version)
            after=states();assert all(after[k]['bootId']==before[k]['bootId'] for k in before)
        except BaseException:
            if stopped:
                run(['systemctl','stop','atlas-gate'])
                if (ROOT/'gate').resolve()!=old:link(old)
                run(['systemctl','start','atlas-gate']);ready(oldversion)
            raise
        finally:
            for k in drained:node(k,False)
            assert FLAG.read_text()==token,'Maintenance ownership changed; do not clear another rollout'
            FLAG.unlink()
        final=states();assert all(d['ready'] and not d['draining'] for d in final.values())
        proof={'status':'passed','version':version,'old':str(old),'new':str(a.candidate),'wheelSha256':a.sha256,
               'qualification':qualification,'backup':str(backup),'ingressMaintenanceVerified':True,
               'routerBootsUnchanged':all(final[k]['bootId']==before[k]['bootId'] for k in before),'maintenanceCleared':not FLAG.exists()}
        (backup/'deployment.json').write_text(json.dumps(proof,indent=2));(backup/'deployment.json').chmod(0o600)
        print(json.dumps(proof),flush=True)


if __name__=='__main__':main()
