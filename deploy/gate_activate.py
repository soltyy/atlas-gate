#!/usr/bin/env python3
"""Single-writer Gate activation with an ingress admission barrier. Run as root."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import tempfile
import time
from typing import NamedTuple
import uuid
import zipfile
import httpx
from dotenv import dotenv_values

ROOT=Path('/opt/atlas');FLAG=Path('/run/atlas-gate-maintenance')
ADMISSION_FLAG=Path('/run/atlas-gate-admission')
API_FLAG=Path('/run/atlas-gate-api-attempt-v1')
STEER_FLAG=Path('/run/atlas-gate-steer-v1')
CONTRACT_LOCK=Path('/run/lock/atlas-gate-contract.lock')
CONTRACT_EPOCH=Path('/run/atlas-gate-contract-generation')
SITE=Path('/etc/nginx/sites-enabled/atlas-gate');LOCK=Path('/run/lock/atlas-deploy.lock')
BACKUPS=Path('/var/backups/atlas');DATABASE=Path('/var/lib/atlas-gate/gate.db')


def run(args, **kwargs):
    return subprocess.run(list(map(str,args)),check=True,**kwargs)


def node(kind, drain=None, *, port=None, expected=None):
    e=dotenv_values('/etc/atlas/'+kind+'.env')
    if port is None:port=8765 if kind=='router' else 8768
    headers={'X-Atlas-Token':e['ATLAS_TOKEN']}
    if expected is not None:
        headers.update({'X-Atlas-Node':expected['nodeId'], 'X-Atlas-Boot':expected['bootId']})
    with httpx.Client(trust_env=False,timeout=15 if expected is not None else 75) as c:
        response=c.request('GET' if drain is None else 'POST',f'http://127.0.0.1:{port}/v1/node'+('' if drain is None else '/drain'),
            headers=headers,json=None if drain is None else {'enabled':drain})
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


class ObsoleteContractGeneration(RuntimeError):
    pass


@contextmanager
def contract_lock():
    # Never the deployment lock: ExecStartPre runs while activation holds it.
    with open(CONTRACT_LOCK,'a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        yield


def _write_generation(value):
    with CONTRACT_EPOCH.open('w') as stream:
        stream.write(value);stream.flush();os.fsync(stream.fileno())


def _generation_locked():
    value=CONTRACT_EPOCH.read_text() if CONTRACT_EPOCH.exists() else ''
    if not value:
        value=uuid.uuid4().hex
        _write_generation(value)
    return value


def contract_generation():
    with contract_lock():return _generation_locked()


def _clear_contracts_locked():
    for flag in (API_FLAG, STEER_FLAG):
        flag.unlink(missing_ok=True)
        flag.with_name(flag.name+'.pending').unlink(missing_ok=True)


def clear_contracts(generation=None):
    with contract_lock():
        if generation is not None and _generation_locked()!=generation:
            raise ObsoleteContractGeneration('Gate restarted during contract probe')
        _clear_contracts_locked()


def reset_contracts():
    """Persistent ExecStartPre: linearizes against every old publisher, no I/O."""
    with contract_lock():
        generation=uuid.uuid4().hex
        _write_generation(generation)
        _clear_contracts_locked()
        return generation


def _sync_contract(flag, field, expected_version=None, generation=None):
    """Publish capability only after a fresh probe during serialized cutover/startup.

    Absence is deliberately fail-closed in Nginx. An old positive marker or an
    orphaned staging file must never authorize inference on a legacy rollback.
    """
    pending=flag.with_name(flag.name+'.pending')
    with contract_lock():
        current=_generation_locked()
        if generation is not None and generation!=current:
            raise ObsoleteContractGeneration('Gate restarted before contract probe')
        generation=current
        flag.unlink(missing_ok=True);pending.unlink(missing_ok=True)
    state=gate_runtime()
    if state is not None and not isinstance(state,dict):
        raise RuntimeError('Invalid API contract runtime response')
    with contract_lock():
        if _generation_locked()!=generation:
            raise ObsoleteContractGeneration('Gate restarted during contract probe')
        if not state or type(state.get(field)) is not int or state[field]!=1:
            return False
        version=state.get('gateVersion')
        if not isinstance(version,str) or not version or expected_version is not None and version!=expected_version:
            raise RuntimeError('API contract runtime identity does not match ready Gate')
        try:
            fd=os.open(pending,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o644)
            with os.fdopen(fd,'w') as stream:
                stream.write(version);stream.flush();os.fsync(stream.fileno())
            os.replace(pending,flag)
        finally:
            pending.unlink(missing_ok=True)
    return True


def sync_api_contract(expected_version=None, generation=None):
    return _sync_contract(API_FLAG, 'apiAttemptContractVersion', expected_version, generation)


def sync_steer_contract(expected_version=None, generation=None):
    return _sync_contract(STEER_FLAG, 'steerContractVersion', expected_version, generation)


def assert_contract_lifecycle():
    """The external fence must survive starting an older wheel after rollback."""
    unit=run(['systemctl','show','atlas-gate','-p','ExecStartPre','-p','ExecStartPost'],
             capture_output=True,text=True).stdout
    required=('/usr/local/sbin/atlas-gate-deploy', '--reset-runtime-contracts',
              'atlas-gate-api-contract-refresh.service')
    if not isinstance(unit,str) or not all(value in unit for value in required):
        raise RuntimeError('Persistent contract startup hooks are missing; no activation')


def assert_steer_ingress():
    # An unauthenticated invalid-session request cannot invoke an SDK even on
    # legacy Gate. It proves the installed external policy, not source files.
    with httpx.Client(trust_env=False,timeout=20) as client:
        for method,path in (('POST','/steer'),('GET','/commands?requestId=contract-probe')):
            response=client.request(method,'https://gate.atlcon.ru/harness/agent/sessions/contract-probe'+path,
                headers={'X-Atlas-Steer-Contract':'1','X-Atlas-Steer-Target-Boot':'contract-probe'})
            if (response.status_code!=503 or response.json().get('error',{}).get('code')!='steer_contract_unavailable'
                    or 'X-Atlas-Command-State' in response.headers):
                raise RuntimeError('Exact steering ingress fence not installed; no activation')


def refresh_api_contract(attempts=45):
    """ExecStartPost mode: no deploy lock, activation, Router calls or service stop.

    systemctl start in the activator already holds LOCK. A probe failure after
    fencing is reported without failing ExecStartPost and killing healthy I/O.
    Activation itself still requires a confirmed contract before opening ingress.
    """
    clear_contracts()
    version=None
    for attempt in range(attempts):
        generation=contract_generation()
        try:
            state=health()
            if not isinstance(state,dict) or not isinstance(state.get('version'),str) or not state['version']:
                return {'status':'blocked','reason':'invalid_startup_health','managedApiIngressAllowed':False,'steerIngressAllowed':False}
            version=state['version']
            enabled=sync_api_contract(version,generation)
            steering=sync_steer_contract(version,generation)
            return {'status':'passed' if enabled else 'unsupported','gateVersion':version,
                    'managedApiIngressAllowed':enabled,'steerIngressAllowed':steering}
        except ObsoleteContractGeneration:
            # ExecStartPost may have targeted this already active oneshot. Retry
            # the NEW epoch instead of exiting and losing its startup refresh.
            continue
        except httpx.HTTPError:
            if attempt+1<attempts:time.sleep(1)
        except (OSError,RuntimeError,ValueError) as failed:
            try:clear_contracts(generation)
            except ObsoleteContractGeneration:continue
            return {'status':'blocked','gateVersion':version,'reason':type(failed).__name__,
                    'managedApiIngressAllowed':False,'steerIngressAllowed':False}
    try:clear_contracts(generation)
    except ObsoleteContractGeneration:pass
    return {'status':'blocked','reason':'startup_health_unavailable','managedApiIngressAllowed':False,'steerIngressAllowed':False}


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


def wait_idle(reader=None):
    while True:
        # Losing all verified paths aborts the switch and releases our drains.
        observed = reader() if reader else None
        try:
            assert_idle(observed if reader else states());assert_gate_idle();return
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


class StandbyTarget(NamedTuple):
    node_id: str
    pin: str
    direct: int
    selector: int
    via_fi: int


STANDBY_TARGETS = {
    'router': StandbyTarget('vdska-codex', 'CODEX_BOOT_ID', 8765, 38765, 28765),
    'router-claude': StandbyTarget('vdska-claude', 'CLAUDE_BOOT_ID', 8768, 38768, 28768),
}
STANDBY_PORTS = {t.node_id: (t.direct, t.selector) for t in STANDBY_TARGETS.values()}


def standby_nodes(original):
    nodes = json.loads(original)
    changed = set()
    for node_config in nodes:
        name = node_config['node_id']
        if name not in STANDBY_PORTS:
            continue
        direct, selector = STANDBY_PORTS[name]
        if (name in changed or node_config.get('transport', 'direct') != 'direct'
                or node_config['url'] != f'http://127.0.0.1:{direct}'):
            raise RuntimeError(f'Unexpected current configuration for {name}')
        node_config['url'] = f'http://127.0.0.1:{selector}'
        changed.add(name)
    if changed != STANDBY_PORTS.keys():
        raise RuntimeError('Expected both existing direct nodes')
    return (json.dumps(nodes, ensure_ascii=False, indent=2) + '\n').encode()


FILE_LANE_ENV = Path('/etc/atlas/router-standby/file-lanes.env')


def file_lane_ports():
    env = dotenv_values(FILE_LANE_ENV)
    if env.get('ATLAS_FILE_LANES') != '1':
        raise RuntimeError('File transport is not enabled on the selector')
    return {kind: int(env[target.pin.replace('BOOT_ID', 'FILE_LISTEN_PORT')])
            for kind, target in STANDBY_TARGETS.items()}


def file_lane_nodes(original, ports):
    check_selector_registry(original)
    entries = json.loads(original)
    for entry in entries:
        kind = next(kind for kind, t in STANDBY_TARGETS.items() if t.node_id == entry['node_id'])
        url = f'http://127.0.0.1:{ports[kind]}'
        if entry.get('file_url') not in (None, '', url):
            raise RuntimeError('Refusing to overwrite a different file transport')
        entry['file_url'] = url
    return (json.dumps(entries, ensure_ascii=False, indent=2) + '\n').encode()


def check_file_selectors(before, ports):
    for kind, port in ports.items():
        target = STANDBY_TARGETS[kind]
        token = dotenv_values('/etc/atlas/' + kind + '.env')['ATLAS_TOKEN']
        expected = {'nodeId': target.node_id, 'bootId': before[kind]['bootId'], 'protocolVersion': 1}
        with httpx.Client(trust_env=False) as client:
            response = client.get(f'http://127.0.0.1:{port}/v1/node/identity',
                                  headers={'X-Atlas-Token': token, 'X-Atlas-Node': target.node_id,
                                           'X-Atlas-Boot': expected['bootId']})
        response.raise_for_status()
        if response.json() != expected:
            raise RuntimeError(f'{kind} file selector returned a different identity')


def replace_nodes(path, expected, updated):
    # Only called with Gate stopped and the deployment lock held. Preserve
    # updates made through the node admin API before the admission fence.
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.read_bytes() != expected:
        raise RuntimeError('Node configuration changed; refusing cutover')
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.nodes-cutover-')
    try:
        with os.fdopen(fd, 'wb') as out:
            os.fchown(out.fileno(), info.st_uid, info.st_gid)
            os.fchmod(out.fileno(), stat.S_IMODE(info.st_mode))
            out.write(updated)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def selector_boots():
    """Read only the two public boot pins of the running HAProxy, including after re-enrol."""
    pid = run(['systemctl', 'show', 'atlas-router-selector.service', '-p', 'MainPID', '--value'],
              text=True, capture_output=True).stdout.strip()
    if not pid.isdigit() or int(pid) <= 1:
        raise RuntimeError('Router selector is not running')
    wanted = {t.pin.encode('ascii'): kind for kind, t in STANDBY_TARGETS.items()}
    pins = {}
    for entry in (Path('/proc') / pid / 'environ').read_bytes().split(b'\0'):
        key, _, value = entry.partition(b'=')
        if key in wanted:
            pins[wanted[key]] = value.decode('ascii')
    return pid, pins


def verified_state(kind, reference, port):
    target = STANDBY_TARGETS[kind]
    if (reference.get('nodeId') != target.node_id or not reference.get('bootId')
            or type(reference.get('protocolVersion')) is not int or reference['protocolVersion'] != 1):
        raise RuntimeError(f'Unexpected {kind} reference identity')
    result = node(kind, port=port, expected=reference)
    if (not isinstance(result, dict) or result.get('nodeId') != target.node_id
            or result.get('bootId') != reference['bootId'] or type(result.get('protocolVersion')) is not int
            or result['protocolVersion'] != 1 or type(result.get('draining')) is not bool):
        raise RuntimeError(f'{kind} on port {port} did not confirm the original Router runtime')
    return result


def check_selector(before, *, require_direct=True):
    process, pins = selector_boots()
    for kind, target in STANDBY_TARGETS.items():
        if pins.get(kind) != before[kind].get('bootId'):
            raise RuntimeError(f'Active selector has a different {kind} boot pin')
        ports = (target.direct, target.via_fi, target.selector) if require_direct else (target.via_fi, target.selector)
        for port in ports:
            verified_state(kind, before[kind], port)
    if selector_boots() != (process, pins):
        raise RuntimeError('Active selector changed while verifying paths')


def check_staged_pins(before, *, require_idle=False):
    staged = dotenv_values('/var/lib/atlas-router-standby/verified-boots.env')
    expected = {t.pin: before[kind]['bootId'] for kind, t in STANDBY_TARGETS.items()}
    if staged != expected:
        raise RuntimeError('Enrol both direct and FI paths for the current Router boots before activation')
    for kind, target in STANDBY_TARGETS.items():
        for port in (target.direct, target.via_fi):
            state = verified_state(kind, before[kind], port)
            if require_idle:
                if not state['draining']:
                    raise RuntimeError(f'{kind} drain lost before selector restart')
                assert_idle({kind: state})


SELECTOR_DIR = Path('/usr/local/lib/atlas-router-standby')
SELECTOR_FILES = ('haproxy.cfg', 'path_checks.lua', 'preflight.py')


def selector_files(directory, *, allow_missing=False):
    if directory.resolve() != directory or not directory.is_dir():
        raise RuntimeError('Unexpected selector bundle directory')
    files = {}
    for name in SELECTOR_FILES:
        source = directory / name
        if source.is_symlink() or (source.exists() and not source.is_file()):
            raise RuntimeError('Unexpected selector file: ' + name)
        files[name] = None if allow_missing and not source.exists() else source.read_bytes()
    return files


def write_selector_files(directory, files):
    for name in SELECTOR_FILES:
        target = directory / name
        if target.is_symlink():
            raise RuntimeError('Unexpected selector symlink: ' + name)
        if files[name] is None:
            target.unlink(missing_ok=True)
            continue
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as output:
            try:
                output.write(files[name])
                output.flush()
                os.fchmod(output.fileno(), 0o644)
                os.replace(output.name, target)
            finally:
                Path(output.name).unlink(missing_ok=True)


def qualify_selector_bundle(directory, expected_hashes, before):
    files = selector_files(directory)
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    if expected_hashes != hashes:
        raise RuntimeError('Selector bundle differs from the qualified artifacts')
    # Execute only the exact bytes whose hashes were checked, even if staging moves.
    with tempfile.TemporaryDirectory() as snapshot:
        root = Path(snapshot)
        write_selector_files(root, files)
        env = dict(os.environ, **dotenv_values('/etc/atlas/router-standby/runtime.env'))
        env.update(dotenv_values('/var/lib/atlas-router-standby/verified-boots.env'))
        if FILE_LANE_ENV.is_file():
            env.update(dotenv_values(FILE_LANE_ENV))
        env['ROUTER_PATH_WATCHER'] = str(root / 'path_checks.lua')
        run(['/usr/sbin/haproxy', '-c', '-f', root / 'haproxy.cfg'], env=env, capture_output=True)
        result = run(['/usr/bin/python3', root / 'preflight.py'], env=env, capture_output=True, text=True)
        pins = dict(line.split('=', 1) for line in result.stdout.splitlines())
        if pins != {target.pin: before[kind]['bootId'] for kind, target in STANDBY_TARGETS.items()}:
            raise RuntimeError('Lightweight identity disagrees with current Router pins')
    return files


def refresh_standby_pins(before, install=None, *, file_ports=None):
    """The caller owns the admission fences and has stopped the Gate service."""
    check_staged_pins(before, require_idle=True)
    _, live_pins = selector_boots()
    expected = {kind: state['bootId'] for kind, state in before.items()}
    if install or live_pins != expected:
        gate = run(['systemctl', 'show', 'atlas-gate', '-p', 'ActiveState', '--value'],
                   text=True, capture_output=True)
        if gate.stdout.strip() != 'inactive':
            raise RuntimeError('Gate still running; selector restart refused')
        ports = [t.selector for t in STANDBY_TARGETS.values()]
        if FILE_LANE_ENV.is_file() and dotenv_values(FILE_LANE_ENV).get('ATLAS_FILE_LANES') == '1':
            ports.extend(file_lane_ports().values())
        sockets = '( ' + ' or '.join(f'sport = :{p}' for p in ports) + ' )'
        clients = run(['ss', '-Hnt', 'state', 'established', sockets],
                      text=True, capture_output=True)
        if clients.stdout.strip():
            raise RuntimeError('Selector still has client connections; restart refused')
        if install:
            install()
        run(['systemctl', 'restart', 'atlas-router-selector.service'])
    # HAProxy accepts TCP before its initial health checks publish the two backends.
    # This deadline bounds the infrastructure check while ingress is fenced.
    deadline = time.monotonic() + 45
    while True:
        try:
            check_selector(before, require_direct=False)
            if file_ports:
                check_file_selectors(before, file_ports)
            return
        except httpx.TransportError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.5)


def check_selector_registry(original):
    entries = json.loads(original)
    expected = {t.node_id: f'http://127.0.0.1:{t.selector}' for t in STANDBY_TARGETS.values()}
    actual = {entry['node_id']: entry['url'] for entry in entries
              if entry.get('transport', 'direct') == 'direct' and entry.get('enabled', True)}
    if len(entries) != len(expected) or actual != expected:
        raise RuntimeError('Pin refresh requires the existing two selector endpoints')


def standby_control(kind, reference, drain=None):
    """Observe through any verified path. Reconcile a lost drain ACK before another write."""
    t = STANDBY_TARGETS[kind]
    ports = (t.direct, t.selector, t.via_fi)
    attempted_write = False
    for port in ports:
        try:
            state = verified_state(kind, reference, port)
            if drain is None or state['draining'] is drain:
                return state
            attempted_write = True
            node(kind, drain, port=port, expected=reference)
            confirmed = verified_state(kind, reference, port)
            if confirmed['draining'] is drain:
                return confirmed
        except (httpx.HTTPError, RuntimeError, ValueError):
            continue  # Every alternative begins with an identity-pinned GET.
    if attempted_write:
        # The last write may have taken effect even if its final observation failed.
        actual = standby_control(kind, reference)
        if actual['draining'] is drain:
            return actual
    raise RuntimeError(f'Could not confirm {kind} runtime or drain state over any verified path')


def restore_drains(drained, control):
    remaining = []
    for kind in drained:
        try:
            control(kind, False)
        except (httpx.HTTPError, RuntimeError, ValueError, OSError):
            remaining.append(kind)
    if remaining:
        raise RuntimeError('Could not confirm drain release: ' + ', '.join(remaining))


def main():
    p=argparse.ArgumentParser();p.add_argument('--candidate',type=Path)
    p.add_argument('--wheel',type=Path);p.add_argument('--sha256')
    p.add_argument('--tests-proof',type=Path);p.add_argument('--refresh-api-contract',action='store_true')
    p.add_argument('--reset-runtime-contracts',action='store_true')
    transport=p.add_mutually_exclusive_group()
    transport.add_argument('--router-standby',action='store_true')
    transport.add_argument('--refresh-standby-pins',action='store_true')
    p.add_argument('--selector-bundle',type=Path)
    p.add_argument('--file-lanes',action='store_true')
    a=p.parse_args()
    if a.file_lanes and not (a.selector_bundle and a.refresh_standby_pins):
        p.error('--file-lanes requires --selector-bundle and --refresh-standby-pins')
    if a.selector_bundle and not a.refresh_standby_pins:
        p.error('--selector-bundle requires --refresh-standby-pins')
    use_standby=a.router_standby or a.refresh_standby_pins
    if use_standby and (a.reset_runtime_contracts or a.refresh_api_contract):
        p.error('standby options require release activation')
    if os.geteuid()!=0:raise PermissionError('deployment and startup refresh require root')
    activation=(a.candidate,a.wheel,a.sha256,a.tests_proof)
    if a.reset_runtime_contracts:
        if a.refresh_api_contract or any(value is not None for value in activation):p.error('startup reset cannot activate or refresh')
        print(json.dumps({'status':'reset','generation':reset_contracts()}),flush=True);return
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
        clear_contracts()
        assert_contract_lifecycle()
        assert_steer_ingress()
        # Close any stale positive marker before busy/qualification checks can
        # abort this rollout. A failed probe leaves managed requests fenced.
        sync_api_contract()
        sync_steer_contract()
        before=states()
        bundle = qualify_selector_bundle(a.selector_bundle, tests.get('selectorBundleSha256'), before) if a.selector_bundle else None
        control=node
        read_states=states
        file_ports=None
        previous_file_ports=None
        if use_standby:
            settings=dotenv_values('/etc/atlas/gate.env')
            nodes_path=Path(settings['ATLAS_GATE_NODES_FILE'])
            if nodes_path != DATABASE.parent/'nodes.json' or nodes_path.is_symlink():
                raise RuntimeError('Unexpected node registry path')
            original_nodes=nodes_path.read_bytes()
            if a.refresh_standby_pins:
                check_selector_registry(original_nodes)
                if any(e.get('file_url') for e in json.loads(original_nodes)):
                    previous_file_ports=file_lane_ports()
                if a.file_lanes or previous_file_ports:
                    file_ports=file_lane_ports()
                    selector_nodes=file_lane_nodes(original_nodes, file_ports)
                check_staged_pins(before)
            else:
                selector_nodes=standby_nodes(original_nodes)
                check_selector(before)
            def control(kind, drain=None):
                return standby_control(kind, before[kind], drain)
            def read_states():
                return {kind: control(kind) for kind in before}
            def drained_states():
                current = read_states()
                if not all(entry['draining'] for entry in current.values()):
                    raise RuntimeError('Router drain changed before cutover')
                return current
        if any(not d['ready'] or d['draining'] for d in before.values()):
            raise RuntimeError('Unready Router or another drain owner; no activation')
        # Busy SDK work may finish behind phase-1 admission fencing. Checking
        # idle before establishing that fence starves rollout under new traffic.
        # wait_idle and the final phase-2 check still prohibit a busy restart.
        env={k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME','VIRTUAL_ENV')}
        check=run([a.candidate/'.venv/bin/python','-m','atlas_gate.admission_check'],cwd='/tmp',env=env,capture_output=True,text=True)
        qualification=json.loads(check.stdout);assert qualification['status']=='passed'
        old=(ROOT/'gate').resolve()
        runtime=gate_runtime()
        # Liveness/version is independent of downstream SDK discovery. The old
        # /health itself can be queued behind the node-probe convoy being fixed.
        oldversion=runtime['gateVersion'] if runtime is not None else health()['version']
        version=qualification['version']
        assert tuple(map(int,version.split('.')))>tuple(map(int,oldversion.split('.'))),'Never downgrade/reinstall'
        backup=BACKUPS/('gate81-'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime()));backup.mkdir(mode=0o700)
        (backup/'qualification.json').write_text(json.dumps(qualification,indent=2));(backup/'qualification.json').chmod(0o600)
        token=str(uuid.uuid4());assert not FLAG.exists()
        marker(ADMISSION_FLAG,token)
        drained=[];stopped=False;service_ready=True
        nodes_changed=False
        previous_selector = None
        try:
            # Phase 1 blocks NEW admission only. Already accepted SDK work can
            # still deliver tool_result/Stop/receipts while old HTTP streams drain.
            with httpx.Client(trust_env=False,timeout=20) as c:
                response=c.post('https://gate.atlcon.ru/harness/agent/sessions',json={})
                assert response.status_code==503 and response.json()['error']['code']=='gate_maintenance'
            for k in before:
                drained.append(k)  # persist/control intent before potentially lost ACK
                control(k,True)
            drain_ingress()
            if use_standby:wait_idle(drained_states)
            else:wait_idle()
            # Phase 2 cuts ingress only after producers have settled. Retiring
            # workers also finish any read/context request admitted during phase1.
            marker(FLAG,token)
            with httpx.Client(trust_env=False,timeout=20) as c:
                response=c.get('https://gate.atlcon.ru/harness/health')
                assert response.status_code==503 and response.json()['error']['code']=='gate_maintenance'
                assert response.headers.get('Retry-After') and 'X-Atlas-Command-State' not in response.headers
            drain_ingress()
            assert_idle(drained_states() if use_standby else states());assert_gate_idle()
            stopped=True;service_ready=False
            clear_contracts()
            run(['systemctl','stop','atlas-gate'])
            with sqlite3.connect('file:'+str(DATABASE)+'?mode=ro',uri=True) as src,sqlite3.connect(backup/'gate.db') as dst:src.backup(dst)
            (backup/'gate.db').chmod(0o600)
            if a.refresh_standby_pins:
                if nodes_path.is_symlink() or nodes_path.read_bytes() != original_nodes:
                    raise RuntimeError('Node registry changed before selector restart')
                if bundle:
                    def install():
                        nonlocal previous_selector
                        previous = selector_files(SELECTOR_DIR, allow_missing=True)
                        snap = backup / 'selector-before'
                        snap.mkdir(mode=0o700)
                        write_selector_files(snap, previous)
                        previous_selector = previous
                        write_selector_files(SELECTOR_DIR, bundle)
                    if file_ports:
                        refresh_standby_pins(before, install, file_ports=file_ports)
                    else:
                        refresh_standby_pins(before, install)
                elif file_ports:
                    refresh_standby_pins(before, file_ports=file_ports)
                else:
                    refresh_standby_pins(before)
            if a.router_standby or a.file_lanes:
                check_selector(before, require_direct=False)
                (backup/'nodes.json').write_bytes(original_nodes)
                replace_nodes(nodes_path, original_nodes, selector_nodes)
                nodes_changed=True
            link(a.candidate);run(['systemctl','start','atlas-gate']);ready(version)
            if not sync_api_contract(version):
                raise RuntimeError('Candidate did not confirm managed API attempt contract1')
            if not sync_steer_contract(version):
                raise RuntimeError('Candidate did not confirm exact steering contract1')
            service_ready=True
            after=read_states();assert all(after[k]['bootId']==before[k]['bootId'] for k in before)
            if use_standby:
                check_selector(after, require_direct=False)
            if file_ports:
                check_file_selectors(before, file_ports)
        except BaseException:
            if stopped:
                service_ready=False
                run(['systemctl','stop','atlas-gate'])
                if previous_selector is not None:
                    write_selector_files(SELECTOR_DIR, previous_selector)
                    run(['systemctl', 'restart', 'atlas-router-selector.service'])
                    if previous_file_ports:
                        refresh_standby_pins(before, file_ports=previous_file_ports)
                    else:
                        refresh_standby_pins(before)
                if nodes_changed:
                    replace_nodes(nodes_path, selector_nodes, original_nodes)
                if (ROOT/'gate').resolve()!=old:link(old)
                service_ready=False
                clear_contracts()
                run(['systemctl','start','atlas-gate']);ready(oldversion)
                sync_api_contract(oldversion);sync_steer_contract(oldversion);service_ready=True
            raise
        finally:
            if service_ready:
                def release_ingress():
                    for flag in (FLAG,ADMISSION_FLAG):
                        if flag.exists():
                            assert flag.read_text()==token,'Maintenance ownership changed; do not clear another rollout'
                            flag.unlink()
                if use_standby:
                    # Once Gate is ready, a failed standby undrain is reported
                    # per node. Our global ingress fence must not block healthy peers.
                    try:restore_drains(drained, control)
                    finally:release_ingress()
                else:
                    for k in drained:node(k,False)
                    release_ingress()
            else:
                print(json.dumps({'status':'recovery_required','maintenanceRetained':True,'backup':str(backup)}),flush=True)
        final=read_states();assert all(d['ready'] and not d['draining'] for d in final.values())
        proof={'status':'passed','version':version,'old':str(old),'new':str(a.candidate),'wheelSha256':a.sha256,
               'qualification':qualification,'backup':str(backup),'ingressMaintenanceVerified':True,
               'routerBootsUnchanged':all(final[k]['bootId']==before[k]['bootId'] for k in before),
               'maintenanceCleared':not FLAG.exists() and not ADMISSION_FLAG.exists(),
               'twoPhaseIngressDrain':True,'detachedApiDrain':True,'managedApiIngressAllowed':API_FLAG.exists(),
               'steerIngressAllowed':STEER_FLAG.exists()}
        if bundle:
            proof['selectorBundleSha256'] = tests['selectorBundleSha256']
        if file_ports:
            proof['fileSelectors'] = file_ports
        if use_standby:
            proof['routerStandby'] = {kind: {'nodeId': s['nodeId'], 'bootId': s['bootId'],
                                             'selectorPort': STANDBY_TARGETS[kind].selector}
                                      for kind, s in final.items()}
        (backup/'deployment.json').write_text(json.dumps(proof,indent=2));(backup/'deployment.json').chmod(0o600)
        print(json.dumps(proof),flush=True)


if __name__=='__main__':main()
