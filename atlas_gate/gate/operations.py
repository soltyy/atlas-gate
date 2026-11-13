"""Собственный журнал Gate: квота, владельцы, результат и восстановление watchers."""
import asyncio
import hashlib
import json
import math
import time
import uuid
import weakref
from .errors import GateError, GateFreshNodeError


def body_sha256(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def tool_result_not_sent(op):
    """Recognize the Gate preflight response, including rows written before #124."""
    if op['method'] != 'POST' or not op['path'].endswith('/tool_result') or op['status'] != 503:
        return False
    try:
        error = json.loads(op['response'])['error']
        return error['code'] == 'node_unavailable' and error['deliveryState'] == 'not_sent'
    except (KeyError, TypeError, ValueError):
        return False


class Operations:
    def __init__(self, store):
        self.store = store
        self.delivery_locks = weakref.WeakValueDictionary()
        store._exec("""CREATE TABLE IF NOT EXISTS router_operations(
            id TEXT PRIMARY KEY, device_id TEXT NOT NULL, session_id TEXT NOT NULL,
            turn_id TEXT, fingerprint TEXT NOT NULL, node_id TEXT NOT NULL,
            method TEXT NOT NULL, path TEXT NOT NULL, state TEXT NOT NULL,
            quota_day TEXT, status INTEGER, response TEXT, created REAL NOT NULL)""")
        columns = {r["name"] for r in store._all("PRAGMA table_info(router_operations)")}
        if "finished" not in columns:
            store._exec("ALTER TABLE router_operations ADD COLUMN finished INTEGER NOT NULL DEFAULT 0")
        if "units" not in columns:
            store._exec("ALTER TABLE router_operations ADD COLUMN units INTEGER NOT NULL DEFAULT 1")
        if "accounted" not in columns:
            store._exec("ALTER TABLE router_operations ADD COLUMN accounted INTEGER NOT NULL DEFAULT 0")
        if "boot_id" not in columns:
            store._exec("ALTER TABLE router_operations ADD COLUMN boot_id TEXT")
        if 'body_sha256' not in columns:
            store._exec('ALTER TABLE router_operations ADD COLUMN body_sha256 TEXT')
        if 'steer' not in columns:
            store._exec('ALTER TABLE router_operations ADD COLUMN steer TEXT')
        if 'admission_rejection' not in columns:
            store._exec('ALTER TABLE router_operations ADD COLUMN admission_rejection TEXT')
        store._exec("""CREATE INDEX IF NOT EXISTS router_steer_identity ON router_operations(
            device_id,session_id,json_extract(steer,'$.clientMessageId')) WHERE steer IS NOT NULL""")
        store._exec("""CREATE INDEX IF NOT EXISTS router_steer_owner_identity ON router_operations(
            device_id,json_extract(steer,'$._org'),json_extract(steer,'$.clientMessageId')) WHERE steer IS NOT NULL""")
        store._exec('''CREATE TABLE IF NOT EXISTS router_admission_fences(
            id TEXT PRIMARY KEY, device_id TEXT NOT NULL, org TEXT NOT NULL,
            session_id TEXT NOT NULL, turn_id TEXT NOT NULL, body_sha256 TEXT NOT NULL,
            response TEXT NOT NULL, created REAL NOT NULL,
            UNIQUE(device_id,session_id,turn_id))''')
        # Preserve the safety invariant even if an older Gate is rolled back onto
        # this database: its admission INSERT must fail before any forwarding.
        store._exec('''CREATE TRIGGER IF NOT EXISTS router_operation_admission_fence
            BEFORE INSERT ON router_operations WHEN EXISTS (
              SELECT 1 FROM router_admission_fences f WHERE f.id=NEW.id OR
              (f.device_id=NEW.device_id AND f.session_id=NEW.session_id AND f.turn_id=NEW.turn_id))
            BEGIN SELECT RAISE(ABORT,'admission_fenced'); END''')

    def fence(self, device, binding, request_id):
        return self.store._one('SELECT * FROM router_admission_fences WHERE id=? AND device_id=? AND org=? AND session_id=?',
                              (self.key(device, request_id), device.device_id, device.org, binding['id']))

    def resolve_prompt(self, device, binding, request_id, turn_id, digest):
        """Linearization point shared with begin; never calls Router or SDK."""
        rid = self.key(device, request_id)
        with self.store._lock:
            db = self.store._db
            db.execute('BEGIN IMMEDIATE')
            try:
                old = db.execute('SELECT * FROM router_operations WHERE id=?', (rid,)).fetchone()
                if old:
                    if (old['device_id'] != device.device_id or old['session_id'] != binding['id'] or
                            old['turn_id'] != turn_id or not old['path'].endswith('/prompt')):
                        raise GateError(409, 'idempotency_conflict', 'Команда относится к другому запросу')
                    if old['body_sha256'] and old['body_sha256'] != digest:
                        raise GateError(409, 'idempotency_conflict', 'SHA тела команды не совпадает')
                sealed = db.execute('SELECT * FROM router_admission_fences WHERE id=? OR (device_id=? AND session_id=? AND turn_id=?)',
                                    (rid, device.device_id, binding['id'], turn_id)).fetchone()
                if sealed:
                    if (sealed['id'] != rid or sealed['org'] != device.org or sealed['device_id'] != device.device_id or
                            sealed['session_id'] != binding['id'] or sealed['turn_id'] != turn_id or sealed['body_sha256'] != digest):
                        raise GateError(409, 'idempotency_conflict', 'Отрицательная квитанция относится к другому intent')
                    result = json.loads(sealed['response'])
                elif old and not self._prompt_rejection(old, device, binding, digest):
                    result = {'version': 1, 'outcome': 'unknown', 'commandId': rid,
                              'requestId': request_id, 'sessionId': binding['id'], 'turnId': turn_id,
                              'receiptState': old['state'], 'reason': 'observe_existing_command'}
                elif db.execute('SELECT id FROM router_operations WHERE device_id=? AND session_id=? AND turn_id=? AND id!=?',
                                (device.device_id, binding['id'], turn_id, rid)).fetchone():
                    result = {'version': 1, 'outcome': 'unknown', 'commandId': rid,
                              'reason': 'turn_exists_under_another_key'}
                else:
                    # Existing proven rejection uses its original target, even if
                    # a later read-only recovery has advanced the binding's boot.
                    target = json.loads(old['admission_rejection']) if old else {
                        'nodeId': binding['node_id'], 'bootId': binding['boot_id'],
                        'route': binding['route'], 'model': binding['model']}
                    result = {'version': 1, 'outcome': 'not_sent', 'proofType': 'admission_fence',
                              'proofId': str(uuid.uuid4()), 'commandId': rid, 'requestId': request_id,
                              'deviceId': device.device_id, 'org': device.org, 'sessionId': binding['id'],
                              'turnId': turn_id, 'bodySha256': digest, 'nodeId': target['nodeId'],
                              'bootId': target['bootId'], 'route': target['route'], 'model': target['model'],
                              'sealed': True, 'requiresNewIntent': True}
                    db.execute('INSERT INTO router_admission_fences VALUES (?,?,?,?,?,?,?,?)',
                               (rid, device.device_id, device.org, binding['id'], turn_id, digest,
                                json.dumps(result, ensure_ascii=False), time.time()))
                db.execute('COMMIT')
                return result
            except BaseException:
                db.execute('ROLLBACK')
                raise

    @staticmethod
    def _prompt_rejection(old, device, binding, digest):
        """Only durable internal provenance authorizes upgrading a complete 503.

        Older rows, including the #98 incident, have no such evidence. Public
        error code/message/deliveryState can also come from Router or a relay.
        """
        if (old['state'] != 'complete' or old['status'] != 503 or old['method'] != 'POST'
                or not old['body_sha256'] or old['body_sha256'] != digest or not old['boot_id']):
            return False
        try:
            saved = json.loads(old['admission_rejection'] or 'null')
            common = (isinstance(saved, dict) and saved.get('version') == 1
                and saved.get('reason') in ('node_unavailable', 'identity_unconfirmed')
                and saved.get('dispatchStarted') is False
                and all(saved.get(k) == v for k, v in {
                    'deviceId': device.device_id, 'org': device.org, 'sessionId': binding['id'],
                    'turnId': old['turn_id'], 'bodySha256': digest, 'nodeId': old['node_id'],
                    'bootId': old['boot_id'], 'method': old['method'], 'path': old['path'],
                    'route': binding['route'], 'model': binding['model']}.items()))
            return common and (saved.get('source') == 'gate_node_preflight' or
                saved.get('source') == 'operator_process_down' and Operations._operator_witness(old, saved))
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _operator_witness(old, saved):
        """Positive audited process-down interval, never an absence/404 inference."""
        times = [saved.get(k) for k in ('stoppedAt', 'created', 'rejectedAt', 'startedAt')]
        audit_hash = saved.get('auditSha256')
        return (all(type(t) in (int, float) and math.isfinite(t) for t in times)
            and times[0] <= times[1] == old['created'] <= times[2] < times[3]
            and saved.get('commandId') == old['id'] and saved.get('httpStatus') == old['status']
            and saved.get('responseSha256') == hashlib.sha256((old['response'] or '').encode()).hexdigest()
            and isinstance(saved.get('auditRef'), str) and saved['auditRef'].startswith('/')
            and isinstance(audit_hash, str) and len(audit_hash) == 64
            and all(c in '0123456789abcdef' for c in audit_hash))

    def import_prompt_rejection(self, rid, witness):
        """Privileged offline operator action, deliberately absent from HTTP.

        The operator must independently verify positive process-lifecycle/log
        evidence and retain the exact audit document at auditRef (root:0600),
        whose raw bytes hash is auditSha256. This method does not inspect a
        Router journal, make network calls, or infer proof from a missing row.
        It only adds provenance; status/body/state/quota/binding stay unchanged.
        """
        with self.store._lock:
            db = self.store._db
            db.execute('BEGIN IMMEDIATE')
            try:
                old = db.execute('SELECT * FROM router_operations WHERE id=?', (rid,)).fetchone()
                binding = db.execute('SELECT * FROM router_bindings WHERE id=?',
                                     (old['session_id'],)).fetchone() if old else None
                owner = db.execute('SELECT org FROM devices WHERE device_id=?',
                                   (old['device_id'],)).fetchone() if old else None
                if (not old or not binding or not owner or old['state'] != 'complete' or old['status'] != 503
                        or old['method'] != 'POST' or not old['turn_id'] or not old['body_sha256'] or not old['boot_id']
                        or not isinstance(binding['local_id'], str) or not binding['local_id']
                        or old['device_id'] != binding['device_id'] or owner['org'] != binding['org']):
                    raise GateError(409, 'admission_witness_unsupported', 'Исходная complete503/identity не подтверждена')
                from urllib.parse import quote
                expected = {'version': 1, 'source': 'operator_process_down', 'dispatchStarted': False,
                    'deviceId': old['device_id'], 'org': binding['org'], 'sessionId': old['session_id'],
                    'turnId': old['turn_id'], 'bodySha256': old['body_sha256'], 'nodeId': old['node_id'],
                    'bootId': old['boot_id'], 'method': old['method'], 'path': old['path'],
                    'route': binding['route'], 'model': binding['model']}
                if (not isinstance(witness, dict) or any(witness.get(k) != v for k, v in expected.items())
                        or old['node_id'] != binding['node_id']
                        or old['path'] != '/v1/sessions/' + quote(binding['local_id'], safe='') + '/prompt'
                        or not self._operator_witness(old, witness)):
                    raise GateError(409, 'admission_witness_conflict', 'Свидетельство не связано с исходной операцией')
                try:
                    error = json.loads(old['response'])['error']
                    reason = {'идентичность Router не подтверждена': 'identity_unconfirmed',
                              'Router не подключён': 'node_unavailable'}[error['message']]
                    if (error['code'] != 'node_unavailable' or error['deliveryState'] != 'not_sent'
                            or witness.get('reason') != reason):
                        raise ValueError('unsupported rejection')
                except (ValueError, TypeError, KeyError):
                    raise GateError(409, 'admission_witness_unsupported', 'Исходный HTTP отказ не совпадает') from None
                if old['admission_rejection']:
                    if json.loads(old['admission_rejection']) != witness:
                        raise GateError(409, 'admission_witness_conflict', 'Сохранённое свидетельство неизменно')
                else:
                    db.execute('UPDATE router_operations SET admission_rejection=? WHERE id=?',
                               (json.dumps(witness, ensure_ascii=False), rid))
                db.execute('COMMIT')
                return witness
            except BaseException:
                db.execute('ROLLBACK')
                raise

    def delivery_lock(self, rid):
        # Один Gate/SQLite; Router дополнительно защищает durable Idempotency-Key.
        return self.delivery_locks.setdefault(rid, asyncio.Lock())

    @staticmethod
    def key(device, request_id):
        if not request_id or len(request_id) > 200:
            raise GateError(422, 'invalid_request', 'request_id должен содержать 1..200 знаков')
        return hashlib.sha256((device.device_id + ':' + request_id).encode()).hexdigest()

    def begin(self, device, binding, method, path, body, request_id=None, turn_id=None, quota_day=None, quota_limit=None, nodes=None, validate_new=None, body_digest=None, steer=None):
        # Ходы имеют стабильный ID протокола, поэтому даже старый клиент не запускает дубль prompt.
        rid = request_id or (f"prompt:{binding['id']}:{turn_id}" if turn_id else str(uuid.uuid4()))
        rid = self.key(device, rid)
        fingerprint = hashlib.sha256((method + path + json.dumps(body, sort_keys=True)).encode()).hexdigest()
        if steer is not None:
            fingerprint = body_sha256({'request': fingerprint, 'contract': steer.get('_contract'),
                                       'targetBoot': steer.get('_targetBoot')})
        with self.store._lock:
            db = self.store._db
            db.execute("BEGIN IMMEDIATE")
            try:
                old = db.execute('SELECT * FROM router_operations WHERE id=?', (rid,)).fetchone()
                if old:
                    if (old['fingerprint'] != fingerprint or old['session_id'] != binding['id'] or
                            (body_digest and old['body_sha256'] and body_digest != old['body_sha256'])):
                        raise GateError(409, 'idempotency_conflict', 'request_id использован с другими параметрами')
                    if steer is not None and (not old['steer'] or
                            any(json.loads(old['steer']).get(k) != steer.get(k) for k in ('_targetBoot', '_contract'))):
                        raise GateError(409, 'idempotency_conflict', 'Исходный target boot команды изменён')
                    db.execute('COMMIT')
                    return dict(old), False
                if steer is not None:
                    original = db.execute("""SELECT id,session_id,steer FROM router_operations WHERE device_id=?
                        AND steer IS NOT NULL AND json_extract(steer,'$.clientMessageId')=?
                        AND (json_extract(steer,'$._org')=? OR
                             (json_extract(steer,'$._org') IS NULL AND session_id=?))""",
                        (device.device_id, steer['clientMessageId'], device.org, binding['id'])).fetchone()
                    if original:
                        from .steering import public
                        saved = json.loads(original['steer'])
                        raise GateError(409, 'steer_message_identity_conflict',
                            'Реплика уже принадлежит исходной команде; требуется сверка прежнего ключа',
                            state=saved['state'], originalCommandId=original['id'],
                            steer=public(saved, original['session_id']))
                sealed = db.execute('SELECT * FROM router_admission_fences WHERE id=? OR (device_id=? AND session_id=? AND turn_id=?)',
                                    (rid, device.device_id, binding['id'], turn_id)).fetchone()
                if sealed:
                    if (sealed['id'] != rid or sealed['device_id'] != device.device_id or sealed['org'] != device.org or
                            sealed['session_id'] != binding['id'] or sealed['turn_id'] != turn_id or
                            sealed['body_sha256'] != (body_digest or body_sha256(body))):
                        raise GateError(409, 'idempotency_conflict', 'Intent закрыт для других параметров')
                    raise GateError(409, 'command_not_sent', 'Intent закрыт до допуска; старый запрос исполняться не будет',
                                    recovery=json.loads(sealed['response']))
                if validate_new is not None:
                    validate_new()
                units = 1
                if (turn_id or path.endswith("/compact")) and nodes:
                    node = nodes.config[binding["node_id"]]
                    info = nodes.snapshots.get(binding["node_id"], {})
                    units += info.get("maxSubagents", 0) if turn_id and json.loads(binding["roles"]) else 0
                    if info.get("draining"):
                        raise GateError(429, "capacity_exceeded", "Router готовится к обновлению")
                    # A cached provider rejection must not veto a new explicit command.
                    # Real concurrency/drain checks and durable replay remain enforced.
                    group_nodes = [n.node_id for n in nodes.config.values() if n.account_group == node.account_group]
                    placeholders = ",".join("?" for _ in group_nodes)
                    active = db.execute(f"SELECT node_id,SUM(units) units FROM router_operations WHERE node_id IN ({placeholders}) AND (turn_id IS NOT NULL OR path LIKE '%/compact') AND finished=0 AND (state IN ('prepared','unknown') OR (state='complete' AND status<400)) GROUP BY node_id", group_nodes).fetchall()
                    reserved = {r["node_id"]: r["units"] for r in active}
                    load = max(reserved.get(node.node_id, 0), info.get("reservedUnits", 0))
                    group_load = sum(max(reserved.get(n, 0), nodes.snapshots.get(n, {}).get("reservedUnits", 0)) for n in group_nodes)
                    turn_limit = info.get("turnCapacity", 0)
                    if (turn_limit and load + units > turn_limit) or (node.account_turn_capacity and group_load + units > node.account_turn_capacity):
                        raise GateError(429, "capacity_exceeded", "нет свободных ресурсов Router/аккаунта")
                if quota_day:
                    reserved = db.execute("INSERT INTO agent_turns(user_id,day,count) SELECT ?,?,1 WHERE ? IS NULL OR ?>0 ON CONFLICT(user_id,day) DO UPDATE SET count=count+1 WHERE ? IS NULL OR count<?",
                        (device.user_id, quota_day, quota_limit, quota_limit, quota_limit, quota_limit)).rowcount
                    if not reserved:
                        raise GateError(402, "quota_exceeded", "дневной лимит исчерпан")
                db.execute("INSERT INTO router_operations(id,device_id,session_id,turn_id,fingerprint,node_id,method,path,state,quota_day,created) VALUES (?,?,?,?,?,?,?,?,'prepared',?,?)",
                    (rid, device.device_id, binding["id"], turn_id, fingerprint, binding["node_id"], method, path, quota_day, time.time()))
                db.execute("UPDATE router_operations SET units=?,boot_id=? WHERE id=?", (units, binding.get("boot_id"), rid))
                db.execute('UPDATE router_operations SET body_sha256=? WHERE id=?', (body_digest or body_sha256(body), rid))
                if steer is not None:
                    db.execute('UPDATE router_operations SET steer=? WHERE id=?', (json.dumps(steer), rid))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return self.store._one("SELECT * FROM router_operations WHERE id=?", (rid,)), True

    def complete(self, rid, response, *, steer_receipt=None, pre_dispatch=None):
        with self.store._lock:
            db = self.store._db
            db.execute("BEGIN IMMEDIATE")
            try:
                old = db.execute("SELECT * FROM router_operations WHERE id=?", (rid,)).fetchone()
                if old and old['state'] == 'complete' and old['admission_rejection']:
                    db.execute('COMMIT')
                    return  # The original negative HTTP receipt is immutable.
                if pre_dispatch is not None:
                    binding, failed = pre_dispatch
                    if (not old or old['state'] != 'prepared' or not isinstance(failed, GateFreshNodeError)
                            or failed.node_id != old['node_id'] or failed.method != old['method']
                            or failed.path != old['path'] or binding['id'] != old['session_id']
                            or binding['device_id'] != old['device_id'] or response.status_code != 503):
                        raise ValueError('invalid pre-dispatch provenance')
                    if old['method'] == 'POST' and old['path'].endswith('/tool_result'):
                        # The client still owns the saved result. Keep its original key and
                        # digest open for exact recovery, even if this Gate restarts.
                        db.execute("UPDATE router_operations SET state='unknown',status=?,response=? WHERE id=?",
                                   (response.status_code, response.text, rid))
                        db.execute('COMMIT')
                        return
                    if old['method'] == 'POST' and old['path'].endswith('/prompt'):
                        rejection = {'version': 1, 'source': 'gate_node_preflight', 'reason': failed.reason,
                            'dispatchStarted': False, 'deviceId': old['device_id'], 'org': binding['org'],
                            'sessionId': old['session_id'], 'turnId': old['turn_id'],
                            'bodySha256': old['body_sha256'], 'nodeId': old['node_id'], 'bootId': old['boot_id'],
                            'method': old['method'], 'path': old['path'], 'route': binding['route'], 'model': binding['model']}
                        db.execute('UPDATE router_operations SET admission_rejection=? WHERE id=?',
                                   (json.dumps(rejection, ensure_ascii=False), rid))
                if old and old['steer']:
                    saved = json.loads(old['steer'])
                    try:
                        reported = response.json().get('steer')
                    except (ValueError, AttributeError):
                        reported = None
                    if steer_receipt is not None:
                        # GET's current projection outranks the immutable old
                        # HTTP ACK body, especially accepted -> unknown on reboot.
                        reported = steer_receipt
                    if reported is not None:
                        from .steering import merge
                        saved = merge(saved, reported)
                        if steer_receipt is not None and reported.get('state') == 'unknown' and saved['state'] == 'unknown':
                            saved['_lookupUnknown'] = True
                    db.execute('UPDATE router_operations SET steer=? WHERE id=?', (json.dumps(saved), rid))
                db.execute("UPDATE router_operations SET state='complete',status=?,response=?,finished=CASE WHEN path LIKE '%/compact' THEN 1 ELSE finished END WHERE id=?", (response.status_code, response.text, rid))
                if old and old["state"] != "complete" and old["quota_day"] and response.status_code >= 400:
                    user = db.execute("SELECT user_id FROM devices WHERE device_id=?", (old["device_id"],)).fetchone()
                    if user:
                        db.execute("UPDATE agent_turns SET count=count-1 WHERE user_id=? AND day=? AND count>0", (user[0], old["quota_day"]))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def unknown(self, rid):
        self.store._exec("UPDATE router_operations SET state='unknown' WHERE id=? AND state!='complete'", (rid,))

    def observe_steer(self, rid, receipt):
        with self.store._lock:
            db = self.store._db
            old = db.execute('SELECT steer FROM router_operations WHERE id=?', (rid,)).fetchone()
            if old and old['steer']:
                from .steering import merge
                updated = merge(json.loads(old['steer']), receipt)
                if receipt.get('state') == 'unknown' and updated['state'] == 'unknown':
                    updated['_lookupUnknown'] = True
                db.execute('UPDATE router_operations SET steer=? WHERE id=?', (json.dumps(updated), rid))

    def absorbed(self, binding, event):
        """Persist before delivering the event/cursor, including ACK/event reordering."""
        if event.get('type') != 'steer_absorbed' or not event.get('uuid'):
            return
        for row in self.store._all('SELECT id,steer FROM router_operations WHERE session_id=? AND steer IS NOT NULL', (binding['id'],)):
            saved = json.loads(row['steer'])
            if saved['turnId'] == event.get('id') and saved['clientMessageId'] == event.get('clientMessageId'):
                try:
                    self.observe_steer(row['id'], {**saved, 'uuid': event['uuid'], 'state': 'absorbed'})
                except ValueError:
                    continue

    async def reconcile(self, nodes):
        rows = self.store._all("""SELECT * FROM router_operations WHERE state IN ('prepared','unknown')
            OR (steer IS NOT NULL AND json_extract(steer,'$.state') IN ('accepted','unknown'))""")
        for row in rows:
            try:
                # Новое boot допускается только при чтении журнала, не исполнении старой команды.
                await nodes.probe(row["node_id"])
                response = await nodes.request(row["node_id"], "GET", "/v1/commands/" + row["id"])
                if response.status_code != 200:
                    continue
                from .receipts import completed_response
                saved = response.json()
                if row.get('steer') and isinstance(saved, dict) and saved.get('steer') is not None:
                    self.observe_steer(row['id'], saved['steer'])
                confirmed = completed_response(saved, row['id'])
                if confirmed is not None:
                    self.complete(row['id'], confirmed, steer_receipt=saved.get('steer'))
            except (GateError, ValueError, KeyError, TypeError):
                continue
