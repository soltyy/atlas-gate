"""Собственный журнал Gate: квота, владельцы, результат и восстановление watchers."""
import hashlib
import json
import time
import uuid
from .errors import GateError


class Operations:
    def __init__(self, store):
        self.store = store
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

    def begin(self, device, binding, method, path, body, request_id=None, turn_id=None, quota_day=None, quota_limit=None, nodes=None):
        # Ходы имеют стабильный ID протокола, поэтому даже старый клиент не запускает дубль prompt.
        rid = request_id or (f"prompt:{binding['id']}:{turn_id}" if turn_id else str(uuid.uuid4()))
        if len(rid) > 200:
            raise GateError(422, "invalid_request", "слишком длинный request_id")
        rid = hashlib.sha256((device.device_id + ":" + rid).encode()).hexdigest()
        fingerprint = hashlib.sha256((method + path + json.dumps(body, sort_keys=True)).encode()).hexdigest()
        old = self.store._one("SELECT * FROM router_operations WHERE id=?", (rid,))
        if old:
            if old["fingerprint"] != fingerprint or old["session_id"] != binding["id"]:
                raise GateError(409, "idempotency_conflict", "request_id использован с другими параметрами")
            return old, False
        with self.store._lock:
            db = self.store._db
            db.execute("BEGIN IMMEDIATE")
            try:
                units = 1
                if (turn_id or path.endswith("/compact")) and nodes:
                    node = nodes.config[binding["node_id"]]
                    info = nodes.snapshots.get(binding["node_id"], {})
                    units += info.get("maxSubagents", 0) if turn_id and json.loads(binding["roles"]) else 0
                    if info.get("draining"):
                        raise GateError(429, "capacity_exceeded", "Router готовится к обновлению")
                    if nodes.cooldown.get(node.account_group, 0) > time.monotonic():
                        raise GateError(429, "rate_limit", "подписка временно не принимает новые ходы")
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
                db.execute("UPDATE router_operations SET units=? WHERE id=?", (units, rid))
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
        self.store._exec("UPDATE router_operations SET state='unknown' WHERE id=?", (rid,))

    async def reconcile(self, nodes):
        rows = self.store._all("SELECT * FROM router_operations WHERE state IN ('prepared','unknown')")
        for row in rows:
            try:
                # Новое boot допускается только при чтении журнала, не исполнении старой команды.
                await nodes.probe(row["node_id"])
                response = await nodes.request(row["node_id"], "GET", "/v1/commands/" + row["id"])
                if response.status_code != 200:
                    continue
                saved = response.json()
                if saved["state"] == "complete":
                    import httpx
                    self.complete(row["id"], httpx.Response(saved["status"], json=saved["response"]))
            except GateError:
                continue
