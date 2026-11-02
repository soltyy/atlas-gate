"""Installed-wheel qualification; ephemeral SQLite only, no Router/SDK/config."""
import concurrent.futures
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
from . import __version__
from .gate.operations import Operations, body_sha256
from .gate.store import Store
from .gate.errors import GateError


def check():
    with tempfile.TemporaryDirectory(prefix='atlas-admission-check-') as root:
        store=Store(str(Path(root)/'gate.db'));ops=Operations(store)
        device=SimpleNamespace(device_id='own-device',org='own-org',user_id='own-user')
        binding={'id':'own-session','node_id':'own-node','boot_id':'own-boot','route':'sub','model':'model'}
        for i in range(20):
            body={'id':str(i),'text':'inert'};key='own-'+str(i);barrier=threading.Barrier(2)
            def admit():
                barrier.wait()
                try:ops.begin(device,binding,'POST','/v1/sessions/local/prompt',body,key,str(i));return 'admitted'
                except GateError as e:return e.code
            def seal():
                barrier.wait();return ops.resolve_prompt(device,binding,key,str(i),body_sha256(body))
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                a=pool.submit(admit);b=pool.submit(seal);first=a.result();proof=b.result()
            assert (proof['outcome']=='not_sent' and first=='command_not_sent') or (proof['outcome']=='unknown' and first=='admitted')
        digest=body_sha256({'id':'missing','text':'inert'})
        proof=ops.resolve_prompt(device,binding,'missing-key','missing',digest)
        store.close();store=Store(str(Path(root)/'gate.db'));ops=Operations(store)
        assert ops.resolve_prompt(device,binding,'missing-key','missing',digest)==proof
        try:
            store._exec("INSERT INTO router_operations(id,device_id,session_id,turn_id,fingerprint,node_id,method,path,state,created) VALUES (?,?,?,?,?,'node','POST','/prompt','prepared',0)",
                        (proof['commandId'],device.device_id,binding['id'],'missing','legacy'))
        except sqlite3.IntegrityError:pass
        else:raise AssertionError('legacy admission bypassed fence')
        store.close()
    package=Path(__file__).parent
    digest=hashlib.sha256()
    for f in sorted(package.rglob('*')):
        if f.is_file() and '__pycache__' not in f.parts and f.suffix!='.pyc':
            digest.update(str(f.relative_to(package)).replace('\\','/').encode()+b'\0'+f.read_bytes())
    return {'status':'passed','version':__version__,'packageSha256':digest.hexdigest(),
            'races':20,'restartProofExact':True,'legacyAdmissionBlocked':True,'sdkActions':0}


if __name__=='__main__':print(json.dumps(check()))
