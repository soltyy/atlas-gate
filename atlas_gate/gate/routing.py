"""Постоянные публичные IDs и резерв создания; один активный Gate/SQLite."""
from __future__ import annotations
import asyncio
import json
import time
import uuid
import hashlib
from .errors import GateError
from ..sdk_tools import SdkTools, supports, enabled, defaults

DDL = """
CREATE TABLE IF NOT EXISTS router_bindings (
 id TEXT PRIMARY KEY, device_id TEXT NOT NULL, org TEXT NOT NULL, route TEXT NOT NULL,
 node_id TEXT NOT NULL, boot_id TEXT NOT NULL, local_id TEXT, model TEXT NOT NULL, roles TEXT NOT NULL,
 status TEXT NOT NULL, created REAL NOT NULL, sdk_alias TEXT NOT NULL UNIQUE,
 sdk_id TEXT, UNIQUE(node_id,local_id)
);
CREATE TABLE IF NOT EXISTS router_usage_receipts (
 session_id TEXT NOT NULL, turn_id TEXT NOT NULL, PRIMARY KEY(session_id,turn_id)
);
CREATE TABLE IF NOT EXISTS legacy_router_history(sdk_id TEXT PRIMARY KEY,node_id TEXT NOT NULL,device_id TEXT NOT NULL,org TEXT NOT NULL);
"""


class Routing:
    def __init__(self, store, nodes):
        self.store, self.nodes = store, nodes
        self.lock = asyncio.Lock()
        with store._lock:
            store._db.executescript(DDL)
            columns = {r[1] for r in store._db.execute("PRAGMA table_info(router_bindings)")}
            for name in ("request_key", "fingerprint", "response", "sdk_tools"):
                if name not in columns:
                    store._db.execute(f"ALTER TABLE router_bindings ADD COLUMN {name} TEXT")
            store._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS router_binding_request ON router_bindings(request_key)")

    def get(self, sid, device):
        row = self.store._one("SELECT * FROM router_bindings WHERE id=? AND device_id=? AND org=? AND status='ready'",
                              (sid, device.device_id, device.org))
        if row is None:
            raise GateError(404, "not_found", "сессия не найдена")
        return row

    def list(self, device):
        return self.store._all("SELECT * FROM router_bindings WHERE device_id=? AND org=? AND status='ready' ORDER BY created",
                               (device.device_id, device.org))

    async def create(self, device, route, model, payload, limit, request_id=None, sdk_policy=None):
        if request_id is not None and (not request_id or len(request_id) > 200):
            raise GateError(422, "invalid_request", "request_id должен содержать 1..200 знаков")
        key = hashlib.sha256((device.device_id + ":" + (request_id or str(uuid.uuid4()))).encode()).hexdigest()
        fingerprint = hashlib.sha256(json.dumps({"route": route, "model": model, "payload": payload}, sort_keys=True).encode()).hexdigest()
        # Замок покрывает выбор+резерв, но не медленное open_session.
        async with self.lock:
            await self.sync_bindings(device.device_id)
            old = self.store._one("SELECT * FROM router_bindings WHERE request_key=?", (key,))
            if old:
                if old["fingerprint"] != fingerprint:
                    raise GateError(409, "idempotency_conflict", "создание с другими параметрами")
                if old["response"] and old["status"] == "ready":
                    return 200, old["response"].encode()
                await self.reconcile()
                old = self.store._one("SELECT * FROM router_bindings WHERE request_key=?", (key,))
                if old["response"] and old["status"] == "ready":
                    return 200, old["response"].encode()
                raise GateError(409, "command_outcome_unknown", "создание требует сверки")
            unresolved = self.store._one("SELECT id FROM router_bindings WHERE device_id=? AND status='unknown'", (device.device_id,))
            if unresolved:
                raise GateError(409, "command_outcome_unknown", "предыдущее создание требует сверки")
            mine = self.store._one("SELECT COUNT(*) n FROM router_bindings WHERE device_id=? AND status IN ('creating','ready','unknown')", (device.device_id,))["n"]
            if limit and mine >= limit:
                raise GateError(409, "agent_sessions_limit", "достигнут предел сессий устройства")
            await self.nodes.refresh()
            resume = None
            if payload.get("resumeSessionId"):
                resume = self.store._one("SELECT * FROM router_bindings WHERE sdk_alias=? AND device_id=? AND org=? AND sdk_id IS NOT NULL",
                    (payload["resumeSessionId"], device.device_id, device.org))
                if resume is None:
                    legacy = self.store._one("SELECT * FROM legacy_router_history WHERE sdk_id=? AND device_id=? AND org=?", (payload["resumeSessionId"], device.device_id, device.org))
                    if legacy:
                        resume = dict(legacy, route=route, model=model)
                if resume is None:
                    raise GateError(404, "not_found", "история SDK для возобновления не найдена")
                if resume["route"] != route or resume["model"] != model:
                    raise GateError(403, "policy_denied", "resume требует прежнего маршрута и модели")
            candidates = []
            loads = self.nodes.reserved() if hasattr(self.nodes, "reserved") else {n: i.get("activeTurns", 0) for n, i in self.nodes.snapshots.items()}
            for node_id, info in self.nodes.snapshots.items():
                n = self.nodes.config[node_id]
                if not supports(info.get('sdkTools'), SdkTools.model_validate(payload.get('sdkTools') or {})):
                    continue
                if not n.enabled or device.org not in n.orgs or route not in n.routes:
                    continue
                if resume and node_id != resume["node_id"]:
                    continue
                if not info.get("ready") or info.get("draining") or self.nodes.cooldown.get(n.account_group, 0) > time.monotonic():
                    continue
                if model not in {m.get("value") for m in info["models"]}:
                    continue
                role_models = {r.get("model") for r in payload.get("agents", []) if r.get("model") not in (None, "", "inherit")}
                if not role_models.issubset({m.get("value") for m in info["models"]}):
                    continue
                counts = self.store._one("SELECT SUM(status='ready') ready, SUM(status IN ('creating','unknown')) reserved FROM router_bindings WHERE node_id=?", (node_id,))
                reserved = counts["reserved"] or 0
                sessions = max(info.get("sessions", 0), counts["ready"] or 0) + reserved
                if info["capacity"] and sessions >= info["capacity"]:
                    continue
                required = 1 + (info.get("maxSubagents", 0) if payload.get("agents") else 0)
                group_load = sum(loads.get(other_id, 0) for other_id, other in self.nodes.config.items() if other.account_group == n.account_group)
                turn_limit = info.get("turnCapacity", info["capacity"])
                group_limit = getattr(n, "account_turn_capacity", 0)
                if (turn_limit and loads.get(node_id, 0) + required > turn_limit) or (group_limit and group_load + required > group_limit):
                    continue
                # Активные ходы приоритетнее неактивных историй; последние расходуют SDK sessions.
                quota = info.get("quota") or {}
                known = [w["usedPercent"] / 100 for w in quota.get("windows", []) if isinstance(w.get("usedPercent"), (int, float)) and (not w.get("resetsAt") or w["resetsAt"] > time.time())]
                if isinstance(quota.get("utilization"), (int, float)):
                    known.append(quota["utilization"])
                pressure = max(known) if known else 1.0  # null не считается свободной подпиской
                candidates.append(((loads.get(node_id, 0) + reserved) / (turn_limit or 1),
                                   pressure, sessions / (info["capacity"] or 1), node_id))
            if not candidates:
                raise GateError(503, "capacity_exceeded", "нет доступного совместимого Router")
            best = min((a, b, c) for a, b, c, _ in candidates)
            ties = [n for a, b, c, n in candidates if (a, b, c) == best]
            import secrets
            node_id = secrets.choice(ties)
            body = dict(payload, model=model)
            if body.get('sdkTools') is None:
                body['sdkTools'] = defaults(self.nodes.snapshots[node_id].get('sdkTools'), sdk_policy).model_dump()
            sid, alias = str(uuid.uuid4()), str(uuid.uuid4())
            boot_id = self.nodes.snapshots[node_id]["bootId"]
            self.store._exec("INSERT INTO router_bindings(id,device_id,org,route,node_id,boot_id,model,roles,status,created,sdk_alias,request_key,fingerprint,sdk_tools) VALUES (?,?,?,?,?,?,?,?,'creating',?,?,?,?,?)",
                (sid, device.device_id, device.org, route, node_id, boot_id, model, json.dumps(payload.get("agents", [])), time.time(), alias, key, fingerprint, json.dumps(body['sdkTools'])))
            if resume:
                body["resumeSessionId"] = resume["sdk_id"]
        try:
            response = await self.nodes.request(node_id, "POST", "/v1/sessions", content=json.dumps(body).encode(), expected_boot=boot_id, request_id="create:" + key)
        except GateError:
            self.store._exec("UPDATE router_bindings SET status='unknown' WHERE id=?", (sid,))
            raise
        if response.status_code >= 400:
            self.store._exec("UPDATE router_bindings SET status='rejected' WHERE id=?", (sid,))
            return response.status_code, response.content
        try:
            created = response.json()
            requested = SdkTools.model_validate(body.get('sdkTools') or {})
            if enabled(requested) and created.get('sdkTools') != requested.model_dump():
                raise ValueError('SDK tools acknowledgement missing')
            local_id = created["id"]
            if not isinstance(local_id, str) or not local_id:
                raise ValueError("invalid session id")
            self.store._exec("UPDATE router_bindings SET status='ready',local_id=? WHERE id=?", (local_id, sid))
        except (ValueError, KeyError, TypeError):
            self.store._exec("UPDATE router_bindings SET status='unknown' WHERE id=?", (sid,))
            raise GateError(502, "command_outcome_unknown", "Router вернул неверный ответ создания")
        created["id"] = sid
        self.store._exec("UPDATE router_bindings SET response=? WHERE id=?", (json.dumps(created), sid))
        return response.status_code, json.dumps(created).encode()

    async def reconcile(self):
        for binding in self.store._all("SELECT * FROM router_bindings WHERE status IN ('creating','unknown') AND request_key IS NOT NULL"):
            try:
                await self.nodes.probe(binding["node_id"])
                response = await self.nodes.request(binding["node_id"], "GET", "/v1/commands/create:" + binding["request_key"])
                if response.status_code != 200:
                    continue
                command = response.json()
                if command["state"] != "complete":
                    continue
                if command["status"] >= 400:
                    self.store._exec("UPDATE router_bindings SET status='rejected' WHERE id=?", (binding["id"],))
                    continue
                created = command["response"]
                requested = SdkTools.model_validate(json.loads(binding['sdk_tools'] or '{}'))
                if enabled(requested) and created.get('sdkTools') != requested.model_dump():
                    continue
                local = created["id"]
                if not isinstance(local, str) or not local:
                    continue
                created = dict(created, id=binding["id"])
                self.store._exec("UPDATE router_bindings SET status='ready',local_id=?,response=? WHERE id=?", (local, json.dumps(created), binding["id"]))
            except (GateError, ValueError, KeyError, TypeError):
                continue

    async def recover_boot(self, binding):
        info = self.nodes.snapshots.get(binding["node_id"])
        if not info or time.monotonic() - info.get("observed", 0) > 5:
            info = await self.nodes.probe(binding["node_id"])
        if not info or info["bootId"] == binding["boot_id"]:
            return binding
        response = await self.nodes.request(binding["node_id"], "GET", "/v1/sessions/" + binding["local_id"] + "/reconcile")
        if response.status_code != 200:
            raise GateError(409, "resume_unavailable", "Router не подтвердил постоянную сессию")
        saved = response.json()
        if saved.get("nodeId") != binding["node_id"] or saved.get("id") != binding["local_id"] or saved.get("bootId") != info["bootId"]:
            raise GateError(409, "router_identity_mismatch", "привязка Router не подтверждена")
        if binding["sdk_id"] and saved.get("sdkSessionId") != binding["sdk_id"]:
            raise GateError(409, "resume_unavailable", "история SDK не совпадает")
        self.store._exec("UPDATE router_bindings SET boot_id=? WHERE id=?", (info["bootId"], binding["id"]))
        return dict(binding, boot_id=info["bootId"])

    async def sync_bindings(self, device_id=None):
        # Проверять journal metadata, а не пустой memory registry после reboot.
        if not hasattr(self.nodes, "probe"):
            return
        sql = "SELECT b.*,d.revoked_at FROM router_bindings b JOIN devices d USING(device_id) WHERE b.status IN ('ready','closing')"
        rows = self.store._all(sql + (" AND b.device_id=?" if device_id else ""), (device_id,) if device_id else ())
        for binding in rows:
            try:
                response = await self.nodes.request(binding["node_id"], "GET", "/v1/sessions/" + binding["local_id"] + "/reconcile")
                if response.status_code == 404:
                    self.store._exec("UPDATE router_bindings SET status='closed' WHERE id=?", (binding["id"],))
                    self.store._exec("UPDATE router_operations SET finished=1 WHERE session_id=?", (binding["id"],))
                    continue
                if response.status_code != 200:
                    continue
                saved = response.json()
                if saved.get("id") != binding["local_id"] or saved.get("nodeId") != binding["node_id"]:
                    continue
                if binding["sdk_id"] and binding["sdk_id"] != saved.get("sdkSessionId"):
                    continue
                if saved.get("closed"):
                    self.store._exec("UPDATE router_bindings SET status='closed' WHERE id=?", (binding["id"],))
                    self.store._exec("UPDATE router_operations SET finished=1 WHERE session_id=?", (binding["id"],))
                    continue
                if saved.get("sdkSessionId"):
                    self.store._exec("UPDATE router_bindings SET sdk_id=? WHERE id=?", (saved["sdkSessionId"], binding["id"]))
                if binding["status"] == "closing" or binding["revoked_at"] is not None:
                    closed = await self.nodes.request(binding["node_id"], "DELETE", "/v1/sessions/" + binding["local_id"], expected_boot=saved["bootId"], request_id="revoke:" + binding["device_id"] + ":" + binding["id"])
                    if closed.status_code < 400 or closed.status_code == 404:
                        self.store._exec("UPDATE router_bindings SET status='closed' WHERE id=?", (binding["id"],))
                        self.store._exec("UPDATE router_operations SET finished=1 WHERE session_id=?", (binding["id"],))
            except GateError:
                continue

    def translate(self, binding, data):
        # SDK IDs из session event — отдельное непрозрачное пространство Gate.
        if data.get("type") == "session" and data.get("sessionId"):
            self.store._exec("UPDATE router_bindings SET sdk_id=? WHERE id=?", (str(data["sessionId"]), binding["id"]))
            data = dict(data, sessionId=binding["sdk_alias"])
        return data

    def usage(self, binding, turn, result, device):
        from .token_usage import sdk_usage
        detail = sdk_usage(result)
        cached = detail["cacheReadTokens"]
        prompt = detail["totalInputTokens"]
        with self.store._lock:
            db = self.store._db
            db.execute("BEGIN IMMEDIATE")
            try:
                added = db.execute("INSERT OR IGNORE INTO router_usage_receipts VALUES (?,?)", (binding["id"], turn)).rowcount
                if added:
                    db.execute("INSERT INTO usage(org,user_id,device_id,kind,model,prompt_tokens,completion_tokens,cached_tokens,cost,ts,route,session_id,model_call) VALUES (?,?,?,'agent',?,?,?,?,NULL,?,?,?,?)",
                        (device.org, device.user_id, device.device_id, binding["model"], prompt, detail["outputTokens"], cached,
                         time.time(), binding["route"], binding["id"], turn))
                    db.execute("INSERT INTO agent_usage_details VALUES (?,?,?)",
                        (binding["id"], turn, json.dumps(detail)))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
