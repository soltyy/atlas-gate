"""Durable credential rotation receipts; no session or business dispatch side effects."""
from __future__ import annotations

import hmac
import json
import time

from .crypto import hmac_hex, new_token, sha256_hex, verify_profile
from .errors import GateError


SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_current (
 device_id TEXT PRIMARY KEY, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_receipts (
 operation TEXT PRIMARY KEY, device_id TEXT NOT NULL, predecessor TEXT NOT NULL,
 payload TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS auth_predecessor ON auth_receipts(predecessor);
CREATE TABLE IF NOT EXISTS auth_access (
 token_hash TEXT PRIMARY KEY, device_id TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_enrollment (
 code_hash TEXT PRIMARY KEY, device_id TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_enrollment_intent (
 device_code TEXT PRIMARY KEY, expected_device_id TEXT
);
"""


def _device(state, device_id):
    dev = state.store._db.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
    if dev is None or dev['revoked_at'] is not None:
        raise GateError(401, 'device_revoked', 'устройство отозвано')
    return dev


def _result(state, payload, *, reveal_secret=False):
    result = json.loads(state.keyring.open(payload))
    result['expires_in'] = max(0, int(result.pop('access_expires_at') - time.time()))
    result.pop('refresh_expires_at', None)
    if not reveal_secret:
        result.pop('device_secret', None)
    return result


def _current(state, device_id):
    db = state.store._db
    row = db.execute('SELECT payload FROM auth_current WHERE device_id=?', (device_id,)).fetchone()
    tokens = db.execute('SELECT * FROM tokens WHERE device_id=?', (device_id,)).fetchone()
    if row is not None and tokens is not None:
        try:
            body = json.loads(state.keyring.open(row['payload']))
            if (body.get('device_id') == device_id
                and sha256_hex(body['device_token']) == tokens['device_token_hash']
                and sha256_hex(body['refresh_token']) == tokens['refresh_token_hash']
                and body.get('access_expires_at') == tokens['expires_at']
                and body.get('refresh_expires_at') == tokens['refresh_expires_at']):
                return row['payload']
        except (RuntimeError,ValueError,KeyError,TypeError):
            pass
    raise GateError(409, 'device_confirmation_required', 'реквизиты изменены вне журнала — подтвердите прежнее устройство')


def _issue(state, device_id):
    """Caller owns the SQLite transaction and store lock."""
    db = state.store._db
    dev = _device(state, device_id)
    now = time.time()
    ttl = min(state.settings.ATLAS_GATE_DEVICE_TOKEN_TTL_SEC, 86400)
    access, refresh = new_token('hdt_'), new_token('hrt_')
    # Concurrent clients retain their already-issued access until its original expiry.
    db.execute('INSERT OR IGNORE INTO auth_access SELECT device_token_hash,device_id,expires_at FROM tokens WHERE device_id=?', (device_id,))
    db.execute('DELETE FROM auth_access WHERE expires_at<?', (now,))
    # Keep permanent operation identity. GC must not turn terminal expired into a new request.
    db.execute("UPDATE auth_receipts SET payload='' WHERE expires_at<? AND payload!=''", (now,))
    db.execute('INSERT INTO tokens VALUES (?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET '
               'device_token_hash=excluded.device_token_hash,expires_at=excluded.expires_at,'
               'refresh_token_hash=excluded.refresh_token_hash,refresh_expires_at=excluded.refresh_expires_at',
               (device_id, sha256_hex(access), now+ttl, sha256_hex(refresh), now+state.settings.ATLAS_GATE_REFRESH_TOKEN_TTL_SEC))
    payload = state.keyring.seal(json.dumps(dict(device_id=device_id, device_token=access,
        refresh_token=refresh, device_secret=state.keyring.open(dev['device_secret']), access_expires_at=now+ttl,
        refresh_expires_at=now+state.settings.ATLAS_GATE_REFRESH_TOKEN_TTL_SEC)))
    db.execute('INSERT INTO auth_current VALUES (?,?) ON CONFLICT(device_id) DO UPDATE SET payload=excluded.payload', (device_id,payload))
    return payload


def _transaction(state, action, *, reveal_secret=False):
    with state.store._lock:
        db = state.store._db
        db.execute('BEGIN IMMEDIATE')
        try:
            payload = action(db)
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise
    return _result(state, payload, reveal_secret=reveal_secret)


def issue(state, device_id):
    return _transaction(state, lambda db: _issue(state, device_id))


def poll(state, device_code):
    def action(db):
        row = db.execute('SELECT * FROM enrollments WHERE device_code=?', (device_code,)).fetchone()
        if row is None or row['expires_at'] < time.time():
            raise GateError(410, 'enroll_expired', 'срок подтверждения истёк')
        _device(state, row['device_id'])
        key = sha256_hex(device_code)
        receipt = db.execute('SELECT payload FROM auth_enrollment WHERE code_hash=?', (key,)).fetchone()
        if row['status'] in ('consumed','recovery_consumed') and receipt:
            return receipt['payload']
        if row['status'] not in ('approved','recovery_approved'):
            raise GateError(410, 'enroll_expired', 'код уже использован')
        payload = _issue(state, row['device_id'])
        status='recovery_consumed' if row['status']=='recovery_approved' else 'consumed'
        db.execute('UPDATE enrollments SET status=? WHERE device_code=?', (status,device_code))
        db.execute('INSERT INTO auth_enrollment VALUES (?,?,?)', (key, row['device_id'], payload))
        return payload
    return _transaction(state, action, reveal_secret=True)


def refresh(state, refresh_token, rotation_id):
    predecessor = sha256_hex(refresh_token)
    operation = sha256_hex('refresh:' + predecessor + ':' + rotation_id) if rotation_id else None
    def action(db):
        current = db.execute('SELECT * FROM tokens WHERE refresh_token_hash=?', (predecessor,)).fetchone()
        if current is None:
            # Same operation returns the exact result, even after later rotations. A concurrent
            # different operation may converge only while this result is still the current pair.
            receipt = db.execute('SELECT * FROM auth_receipts WHERE operation=?', (operation,)).fetchone() if operation else None
            if receipt is None and operation:
                receipt = db.execute('SELECT r.* FROM auth_receipts r JOIN auth_current c '
                    'ON c.device_id=r.device_id AND c.payload=r.payload WHERE r.predecessor=? LIMIT 1', (predecessor,)).fetchone()
                if receipt is not None:
                    _current(state, receipt['device_id'])
            if receipt is None:
                raise GateError(401, 'unauthorized', 'refresh_token не распознан')
            _device(state, receipt['device_id'])
            if receipt['expires_at'] < time.time():
                raise GateError(401, 'token_expired', 'срок повтора refresh истёк')
            if operation:
                db.execute('INSERT OR IGNORE INTO auth_receipts VALUES (?,?,?,?,?)',
                    (operation, receipt['device_id'], predecessor, receipt['payload'], receipt['expires_at']))
            return receipt['payload']
        _device(state, current['device_id'])
        if current['refresh_expires_at'] < time.time():
            raise GateError(401, 'token_expired', 'срок refresh_token истёк — подтвердите прежнее устройство')
        payload = _issue(state, current['device_id'])
        if operation:
            db.execute('INSERT INTO auth_receipts VALUES (?,?,?,?,?)',
                (operation, current['device_id'], predecessor, payload, current['refresh_expires_at']))
        return payload
    return _transaction(state, action)


def challenge(state, device_id, request_id):
    now = int(time.time())
    body = dict(version=1,purpose='atlas-gate-token-challenge',device_id=device_id,
        request_id=request_id,nonce=new_token('challenge_'),issued_at=now,expires_at=now+300)
    return {'challenge':state.key.sign(body),'expires_in':300}


def recover(state, device_id, request_id, proof, challenge_token):
    try:
        scope = verify_profile(challenge_token,state.key.jwk())
        now = time.time()
        if (scope.get('purpose') != 'atlas-gate-token-challenge' or scope.get('version') != 1
            or scope.get('device_id') != device_id or scope.get('request_id') != request_id
            or scope.get('expires_at',0) <= now or scope.get('issued_at',now+100) > now+60
            or scope.get('expires_at',0)-scope.get('issued_at',0) != 300):
            raise ValueError('invalid scope')
    except Exception:
        raise GateError(401,'recovery_proof_expired','подтверждение восстановления истекло или не соответствует запросу') from None
    operation = sha256_hex('recover:' + device_id + ':' + request_id)
    def action(db):
        dev = _device(state, device_id)
        expected = hmac_hex(state.keyring.open(dev['device_secret']),
            {'version':1, 'purpose':'atlas-gate-token-recovery', 'device_id':device_id, 'request_id':request_id,
             'challenge':challenge_token})
        if not hmac.compare_digest(expected, proof):
            raise GateError(401, 'unauthorized', 'подтверждение устройства не распознано')
        tokens = db.execute('SELECT * FROM tokens WHERE device_id=?', (device_id,)).fetchone()
        if tokens is None or tokens['refresh_expires_at'] < time.time():
            raise GateError(401, 'token_expired', 'срок восстановления истёк — подтвердите прежнее устройство')
        payload = _current(state, device_id)
        receipt = db.execute('SELECT * FROM auth_receipts WHERE operation=?', (operation,)).fetchone()
        if receipt:
            if receipt['expires_at'] < time.time():
                raise GateError(401, 'token_expired', 'срок результата восстановления истёк')
            return receipt['payload']
        db.execute('INSERT INTO auth_receipts VALUES (?,?,?,?,?)',
            (operation, device_id, '', payload, tokens['refresh_expires_at']))
        return payload
    return _transaction(state, action)
