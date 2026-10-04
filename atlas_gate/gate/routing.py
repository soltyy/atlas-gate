"""Постоянные публичные IDs и резерв создания; один активный Gate/SQLite."""
from __future__ import annotations
import asyncio
import json
import time
import uuid
from .errors import GateError

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
"""


class Routing:
    def __init__(self, store, nodes):
        self.store, self.nodes = store, nodes
        self.lock = asyncio.Lock()
        with store._lock:
            store._db.executescript(DDL)

    def get(self, sid, device):
        row = self.store._one("SELECT * FROM router_bindings WHERE id=? AND device_id=? AND org=? AND status='ready'",
                              (sid, device.device_id, device.org))
        if row is None:
            raise GateError(404, "not_found", "сессия не найдена")
        return row

    def list(self, device):
        return self.store._all("SELECT * FROM router_bindings WHERE device_id=? AND org=? AND status='ready' ORDER BY created",
                               (device.device_id, device.org))

    async def create(self, device, route, model, payload, limit):
        # Замок покрывает выбор+резерв, но не медленное open_session.
        async with self.lock:
            unresolved = self.store._one("SELECT id FROM router_bindings WHERE device_id=? AND status='unknown'", (device.device_id,))
            if unresolved:
                raise GateError(409, "command_outcome_unknown", "предыдущее создание требует сверки")
            mine = self.store._one("SELECT COUNT(*) n FROM router_bindings WHERE device_id=? AND status IN ('creating','ready','unknown')", (device.device_id,))["n"]
            if mine >= limit:
                raise GateError(409, "agent_sessions_limit", "достигнут предел сессий устройства")
            await self.nodes.refresh()
            resume = None
            if payload.get("resumeSessionId"):
                resume = self.store._one("SELECT * FROM router_bindings WHERE sdk_alias=? AND device_id=? AND org=? AND sdk_id IS NOT NULL",
                    (payload["resumeSessionId"], device.device_id, device.org))
                if resume is None:
                    raise GateError(404, "not_found", "история SDK для возобновления не найдена")
                if resume["route"] != route or resume["model"] != model:
                    raise GateError(403, "policy_denied", "resume требует прежнего маршрута и модели")
            candidates = []
            for node_id, info in self.nodes.snapshots.items():
                n = self.nodes.config[node_id]
                if not n.enabled or device.org not in n.orgs or route not in n.routes:
                    continue
                if resume and node_id != resume["node_id"]:
                    continue
                if not info.get("ready") or self.nodes.cooldown.get(n.account_group, 0) > time.monotonic():
                    continue
                if model not in {m.get("value") for m in info["models"]}:
                    continue
                role_models = {r.get("model") for r in payload.get("agents", []) if r.get("model") not in (None, "", "inherit")}
                if not role_models.issubset({m.get("value") for m in info["models"]}):
                    continue
                counts = self.store._one("SELECT SUM(status='ready') ready, SUM(status IN ('creating','unknown')) reserved FROM router_bindings WHERE node_id=?", (node_id,))
                reserved = counts["reserved"] or 0
                sessions = max(info.get("sessions", 0), counts["ready"] or 0) + reserved
                if sessions >= info["capacity"]:
                    continue
                # Активные ходы приоритетнее неактивных историй; последние расходуют SDK sessions.
                candidates.append(((info.get("activeTurns", 0) + reserved) / info["capacity"],
                                   sessions / info["capacity"], node_id))
            if not candidates:
                raise GateError(503, "capacity_exceeded", "нет доступного совместимого Router")
            best = min((a, b) for a, b, _ in candidates)
            ties = [n for a, b, n in candidates if (a, b) == best]
            import secrets
            node_id = secrets.choice(ties)
            sid, alias = str(uuid.uuid4()), str(uuid.uuid4())
            boot_id = self.nodes.snapshots[node_id]["bootId"]
            self.store._exec("INSERT INTO router_bindings(id,device_id,org,route,node_id,boot_id,model,roles,status,created,sdk_alias) VALUES (?,?,?,?,?,?,?,?,'creating',?,?)",
                (sid, device.device_id, device.org, route, node_id, boot_id, model, json.dumps(payload.get("agents", [])), time.time(), alias))
            body = dict(payload, model=model)
            if resume:
                body["resumeSessionId"] = resume["sdk_id"]
        try:
            response = await self.nodes.request(node_id, "POST", "/v1/sessions", content=json.dumps(body).encode(), expected_boot=boot_id)
        except GateError:
            self.store._exec("UPDATE router_bindings SET status='unknown' WHERE id=?", (sid,))
            raise
        if response.status_code >= 400:
            self.store._exec("UPDATE router_bindings SET status='rejected' WHERE id=?", (sid,))
            return response.status_code, response.content
        try:
            created = response.json()
            local_id = created["id"]
            if not isinstance(local_id, str) or not local_id:
                raise ValueError("invalid session id")
            self.store._exec("UPDATE router_bindings SET status='ready',local_id=? WHERE id=?", (local_id, sid))
        except (ValueError, KeyError, TypeError):
            self.store._exec("UPDATE router_bindings SET status='unknown' WHERE id=?", (sid,))
            raise GateError(502, "command_outcome_unknown", "Router вернул неверный ответ создания")
        created["id"] = sid
        return response.status_code, json.dumps(created).encode()

    def translate(self, binding, data):
        # SDK IDs из session event — отдельное непрозрачное пространство Gate.
        if data.get("type") == "session" and data.get("sessionId"):
            self.store._exec("UPDATE router_bindings SET sdk_id=? WHERE id=?", (str(data["sessionId"]), binding["id"]))
            data = dict(data, sessionId=binding["sdk_alias"])
        return data

    def usage(self, binding, turn, result, device):
        u = result.get("usage") or {}
        cached = int(u.get("cache_read_input_tokens") or 0)
        prompt = int(u.get("input_tokens") or 0) + cached + int(u.get("cache_creation_input_tokens") or 0)
        with self.store._lock:
            db = self.store._db
            db.execute("BEGIN IMMEDIATE")
            try:
                added = db.execute("INSERT OR IGNORE INTO router_usage_receipts VALUES (?,?)", (binding["id"], turn)).rowcount
                if added:
                    db.execute("INSERT INTO usage(org,user_id,device_id,kind,model,prompt_tokens,completion_tokens,cached_tokens,cost,ts,route,session_id,model_call) VALUES (?,?,?,'agent',?,?,?,?,NULL,?,?,?,?)",
                        (device.org, device.user_id, device.device_id, binding["model"], prompt, int(u.get("output_tokens") or 0), cached,
                         time.time(), binding["route"], binding["id"], turn))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
