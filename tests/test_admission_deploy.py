import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
import zipfile
import pytest


@pytest.mark.parametrize('failure', [None,'busy','qualification','maintenance','startup'])
@pytest.mark.skipif(os.name=='nt',reason='Linux systemd/symlink deployment guard')
def test_guard_failure_boundaries_and_rollback(tmp_path,monkeypatch,failure):
    if os.name=='nt':monkeypatch.setitem(sys.modules,'fcntl',SimpleNamespace(LOCK_EX=1,LOCK_NB=2,flock=lambda *a:None))
    spec=importlib.util.spec_from_file_location('gate_guard',Path(__file__).parents[1]/'deploy/gate_activate.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
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
    for name,value in dict(ROOT=root,FLAG=tmp_path/'maintenance',SITE=site,LOCK=tmp_path/'lock',BACKUPS=backups,DATABASE=database).items():monkeypatch.setattr(m,name,value)
    monkeypatch.setattr(m.os,'geteuid',lambda:0,raising=False)
    monkeypatch.setattr(sys,'argv',['guard','--candidate',str(candidate),'--wheel',str(wheel),'--sha256',sha,'--tests-proof',str(tests)])
    events=[];draining=set();failed=False
    def states():return {k:{'ready':True,'draining':k in draining,'bootId':k,'activeTurns':int(failure=='busy'),'activeSubagents':0,'reservedUnits':0} for k in ('router','router-claude')}
    def node(kind,drain=None):
        if drain:draining.add(kind)
        else:draining.discard(kind)
        events.append(('drain',kind,drain))
    def run(args,**kw):
        nonlocal failed
        args=list(map(str,args));events.append(tuple(args))
        if 'atlas_gate.admission_check' in args:
            if failure=='qualification':raise RuntimeError('failed qualification')
            return SimpleNamespace(stdout=json.dumps({'status':'passed','version':'0.4.17'}))
        if args==['systemctl','start','atlas-gate'] and failure=='startup' and not failed:
            failed=True;raise RuntimeError('failed startup')
        return SimpleNamespace()
    class Client:
        def __init__(self,*a,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def get(self,*a,**kw):
            assert m.FLAG.exists()
            return SimpleNamespace(status_code=200 if failure=='maintenance' else 503,
                json=lambda:{'error':{'code':'gate_maintenance'}},headers={'Retry-After':'1'})
    monkeypatch.setattr(m,'states',states);monkeypatch.setattr(m,'node',node);monkeypatch.setattr(m,'run',run)
    monkeypatch.setattr(m,'health',lambda:{'version':'0.4.16'})
    monkeypatch.setattr(m,'ready',lambda version:events.append(('ready',version)))
    monkeypatch.setattr(m.httpx,'Client',Client)
    if failure:
        with pytest.raises((RuntimeError,AssertionError)):m.main()
        assert (root/'gate').resolve()==old
    else:
        m.main();assert (root/'gate').resolve()==candidate
    assert not m.FLAG.exists() and not draining
    if failure in ('busy','qualification','maintenance'):
        assert not any(e[:1]==('systemctl',) for e in events)
    if failure=='startup':assert ('ready','0.4.16') in events


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
