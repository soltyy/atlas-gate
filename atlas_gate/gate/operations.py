"""Собственный журнал Gate: квота, владельцы, результат и восстановление watchers."""
import asyncio
import hashlib
import json
import time
import uuid
import weakref
from .errors import GateError


def body_sha256(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


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
                    result = {'version': 1, 'outcome': 'unknown', 'commandId': rid,
                              'requestId': request_id, 'sessionId': binding['id'], 'turnId': turn_id,
                              'receiptState': old['state'], 'reason': 'observe_existing_command'}
                    db.execute('COMMIT')
                    return result
                sealed = db.execute('SELECT * FROM router_admission_fences WHERE id=? OR (device_id=? AND session_id=? AND turn_id=?)',
                                    (rid, device.device_id, binding['id'], turn_id)).fetchone()
                if sealed:
                    if (sealed['id'] != rid or sealed['org'] != device.org or sealed['device_id'] != device.device_id or
                            sealed['session_id'] != binding['id'] or sealed['turn_id'] != turn_id or sealed['body_sha256'] != digest):
                        raise GateError(409, 'idempotency_conflict', 'Отрицательная квитанция относится к другому intent')
                    result = json.loads(sealed['response'])
                elif db.execute('SELECT id FROM router_operations WHERE device_id=? AND session_id=? AND turn_id=?',
                                (device.device_id, binding['id'], turn_id)).fetchone():
                    result = {'version': 1, 'outcome': 'unknown', 'commandId': rid,
                              'reason': 'turn_exists_under_another_key'}
                else:
                    result = {'version': 1, 'outcome': 'not_sent', 'proofType': 'admission_fence',
                              'proofId': str(uuid.uuid4()), 'commandId': rid, 'requestId': request_id,
                              'deviceId': device.device_id, 'org': device.org, 'sessionId': binding['id'],
                              'turnId': turn_id, 'bodySha256': digest, 'nodeId': binding['node_id'],
                              'bootId': binding['boot_id'], 'route': binding['route'], 'model': binding['model'],
                              'sealed': True, 'requiresNewIntent': True}
                    db.execute('INSERT INTO router_admission_fences VALUES (?,?,?,?,?,?,?,?)',
                               (rid, device.device_id, device.org, binding['id'], turn_id, digest,
                                json.dumps(result, ensure_ascii=False), time.time()))
                db.execute('COMMIT')
                return result
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

    def begin(self, device, binding, method, path, body, request_id=None, turn_id=None, quota_day=None, quota_limit=None, nodes=None, validate_new=None, body_digest=None):
        # Ходы имеют стабильный ID протокола, поэтому даже старый клиент не запускает дубль prompt.
        rid = request_id or (f"prompt:{binding['id']}:{turn_id}" if turn_id else str(uuid.uuid4()))
        rid = self.key(device, rid)
        fingerprint = hashlib.sha256((method + path + json.dumps(body, sort_keys=True)).encode()).hexdigest()
        with self.store._lock:
            db = self.store._db
            db.execute("BEGIN IMMEDIATE")
            try:
                old = db.execute('SELECT * FROM router_operations WHERE id=?', (rid,)).fetchone()
                if old:
                    if (old['fingerprint'] != fingerprint or old['session_id'] != binding['id'] or
                            (body_digest and old['body_sha256'] and body_digest != old['body_sha256'])):
                        raise GateError(409, 'idempotency_conflict', 'request_id использован с другими параметрами')
                    db.execute('COMMIT')
                    return dict(old), False
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
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return self.store._one("SELECT * FROM router_operations WHERE id=?", (rid,)), True

    def complete(self, rid, response):
        with self.store._lock:
            db = self.store._db
            db.execute("BEGIN IMMEDIATE")
            try:
                old = db.execute("SELECT * FROM router_operations WHERE id=?", (rid,)).fetchone()
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

    async def reconcile(self, nodes):
        rows = self.store._all("SELECT * FROM router_operations WHERE state IN ('prepared','unknown')")
        for row in rows:
            try:
                # Новое boot допускается только при чтении журнала, не исполнении старой команды.
                await nodes.probe(row["node_id"])
                response = await nodes.request(row["node_id"], "GET", "/v1/commands/" + row["id"])
                if response.status_code != 200:
                    continue
                from .receipts import completed_response
                confirmed = completed_response(response.json(), row['id'])
                if confirmed is not None:
                    self.complete(row['id'], confirmed)
            except (GateError, ValueError, KeyError, TypeError):
                continue
