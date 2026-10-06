"""Одобренные endpoint, capabilities и HTTP-клиенты; не проксирует произвольные пути."""
from __future__ import annotations
import asyncio
import ipaddress
import json
import os
import time
import ssl
from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal
import httpx
from pydantic import BaseModel, Field, model_validator
from .errors import GateError


class Node(BaseModel):
    node_id: str = Field(min_length=1)
    url: str = ""
    token_env: str = ""
    transport: Literal["direct", "connector"] = "direct"
    orgs: list[str]
    routes: list[str]
    account_group: str = Field(min_length=1)
    enabled: bool = True
    ca_file: str | None = None
    cert_file: str | None = None
    key_file: str | None = None
    account_turn_capacity: int = Field(default=4, ge=1)

    @model_validator(mode="after")
    def endpoint(self):
        if self.transport == "connector":
            if self.url or self.token_env or self.cert_file or self.key_file:
                raise ValueError("connector не использует входящий endpoint/секрет Router")
            return self
        url = urlsplit(self.url)
        try:
            loopback = ipaddress.ip_address(url.hostname or "").is_loopback
        except ValueError:
            loopback = url.hostname == "localhost"
        if url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
            raise ValueError("endpoint должен быть origin без credentials/path/query")
        if url.scheme != "https" and not (url.scheme == "http" and loopback):
            raise ValueError("Router требует HTTPS; HTTP допустим только для loopback")
        if bool(self.cert_file) != bool(self.key_file):
            raise ValueError("для mTLS нужны cert_file и key_file вместе")
        if self.cert_file and url.scheme != "https":
            raise ValueError("mTLS требует HTTPS")
        return self


class Nodes:
    def __init__(self, path: str, store=None, channel=None):
        raw = json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).is_file() else []
        nodes = [Node.model_validate(n) for n in raw]
        if len({n.node_id for n in nodes}) != len(nodes):
            raise ValueError("повторяющийся node_id")
        groups = {}
        for n in nodes:
            if n.account_group in groups and groups[n.account_group] != n.account_turn_capacity:
                raise ValueError("account_turn_capacity должен совпадать у общего account_group")
            groups[n.account_group] = n.account_turn_capacity
        self.config = {n.node_id: n for n in nodes}
        self.clients = {}
        for n in nodes:
            if n.transport == 'connector':
                if channel is None:
                    raise ValueError('connector требует дисковый канал Gate')
                from .node_channel import ChannelClient
                self.clients[n.node_id] = ChannelClient(channel, n.node_id)
                continue
            token = os.environ.get(n.token_env)
            if not token:
                raise ValueError(f"нет секрета endpoint {n.node_id}: {n.token_env}")
            context = ssl.create_default_context(cafile=n.ca_file)
            if n.cert_file:
                context.load_cert_chain(n.cert_file, n.key_file)
            self.clients[n.node_id] = httpx.AsyncClient(base_url=n.url.rstrip("/"),
                headers={"X-Atlas-Token": token}, follow_redirects=False, trust_env=False,
                verify=context, timeout=httpx.Timeout(70, connect=5))
        self.snapshots: dict[str, dict] = {}
        self.cooldown: dict[str, float] = {}
        self.store = store
        self.catalog: dict[str, list] = {}
        if store is not None:
            store._exec("CREATE TABLE IF NOT EXISTS router_catalogs(node_id TEXT PRIMARY KEY, models TEXT NOT NULL)")
            self.catalog = {r["node_id"]: json.loads(r["models"]) for r in store._all("SELECT * FROM router_catalogs")}
            store._exec("CREATE TABLE IF NOT EXISTS router_account_cooldowns(account_group TEXT PRIMARY KEY, until REAL NOT NULL)")
            self.cooldown = {r["account_group"]: time.monotonic() + max(0, r["until"] - time.time()) for r in store._all("SELECT * FROM router_account_cooldowns WHERE until>?", (time.time(),))}

    async def close(self):
        await asyncio.gather(*(c.aclose() for c in self.clients.values()))

    async def probe(self, node_id: str) -> dict | None:
        if self.revoked(node_id):
            self.snapshots.pop(node_id, None)
            return None
        try:
            response = await self.clients[node_id].get("/v1/node")
            response.raise_for_status()
            info = response.json()
            if info.get("nodeId") != node_id or info.get("protocolVersion") != 1 or not info.get("bootId"):
                raise ValueError("identity/protocol mismatch")
            if not isinstance(info.get("capacity"), int) or info["capacity"] < 1:
                raise ValueError("invalid capacity")
            if not isinstance(info.get("models"), list):
                raise ValueError("invalid catalog")
            if not {"durable_commands", "durable_events"}.issubset(info.get("capabilities", [])):
                raise ValueError("Gate требует дисковый журнал Router")
            self.snapshots[node_id] = dict(info, observed=time.monotonic())
            if isinstance(info.get("quota"), dict):
                self.observe_event(node_id, dict(info["quota"], type="rate_limit"))
            if info["models"]:
                self.catalog[node_id] = info["models"]
                if self.store is not None:
                    self.store._exec("INSERT INTO router_catalogs VALUES (?,?) ON CONFLICT(node_id) DO UPDATE SET models=excluded.models", (node_id, json.dumps(info["models"])))
            return self.snapshots[node_id]
        except (httpx.HTTPError, ValueError, TypeError, KeyError, GateError):
            self.snapshots.pop(node_id, None)
            return None

    async def refresh(self):
        await asyncio.gather(*(self.probe(n.node_id) for n in self.config.values() if n.enabled))

    async def request(self, node_id: str, method: str, path: str, *, content=b"", params=None, expected_boot=None, request_id=None):
        node = self.config.get(node_id)
        if node is None or not node.enabled or self.revoked(node_id):
            raise GateError(503, "node_unavailable", "Router не подключён", node_id=node_id)
        info = self.snapshots.get(node_id)
        if not info or time.monotonic() - info["observed"] > 5:
            info = await self.probe(node_id)
        if info is None:
            raise GateError(503, "node_unavailable", "идентичность Router не подтверждена", node_id=node_id)
        headers = {"Content-Type": "application/json", "X-Atlas-Node": node_id,
                   "X-Atlas-Boot": expected_boot or info["bootId"]}
        if request_id:
            headers["Idempotency-Key"] = request_id
        try:
            response = await self.clients[node_id].request(method, path, content=content,
                params=params, headers=headers)
        except httpx.HTTPError as failed:
            raise GateError(503, "command_outcome_unknown" if method != "GET" else "node_unavailable",
                            "ответ Router не получен; повтор на другом узле запрещён", node_id=node_id) from failed
        if response.status_code == 429 and "capacity_exceeded" not in response.text:
            self.observe_event(node_id, {"type": "rate_limit"})
        if response.status_code == 409 and "command_outcome_unknown" in response.text:
            raise GateError(503, "command_outcome_unknown", "Router ещё не подтвердил итог команды; требуется сверка", node_id=node_id)
        if 300 <= response.status_code < 400:
            raise GateError(502, "node_protocol_error", "redirect Router запрещён")
        return response

    def observe_event(self, node_id, event):
        if event.get("type") == "rate_limit" and event.get("status") in ("allowed", "allowed_warning"):
            return
        if event.get("type") == "rate_limit" or (event.get("type") == "error" and event.get("kind") in ("rate_limit", "rate_limit_exceeded", "usage_limit")):
            retry = event.get("retryAfterSeconds", 60)
            retry = max(1, min(float(retry) if isinstance(retry, (int, float)) else 60, 86400))
            group = self.config[node_id].account_group
            self.cooldown[group] = time.monotonic() + retry
            if self.store:
                self.store._exec("CREATE TABLE IF NOT EXISTS router_account_cooldowns(account_group TEXT PRIMARY KEY, until REAL NOT NULL)")
                self.store._exec("INSERT INTO router_account_cooldowns VALUES (?,?) ON CONFLICT(account_group) DO UPDATE SET until=MAX(until,excluded.until)", (group, time.time() + retry))

    async def models(self):
        await self.refresh()
        return self.model_ids()

    def reserved(self):
        counts = {}
        if self.store and self.store._one("SELECT name FROM sqlite_master WHERE type='table' AND name='router_operations'"):
            counts = {r["node_id"]: r["units"] for r in self.store._all("SELECT node_id,SUM(units) units FROM router_operations WHERE (turn_id IS NOT NULL OR path LIKE '%/compact') AND finished=0 AND (state IN ('prepared','unknown') OR (state='complete' AND status<400)) GROUP BY node_id")}
        return {n: max(counts.get(n, 0), self.snapshots.get(n, {}).get("reservedUnits", self.snapshots.get(n, {}).get("activeTurns", 0))) for n in self.config}

    def revoked(self, node_id):
        return bool(self.store and self.store._one("SELECT name FROM sqlite_master WHERE type='table' AND name='node_revocations'") and self.store._one("SELECT node_id FROM node_revocations WHERE node_id=?", (node_id,)))

    def model_ids(self, org=None, routes=None):
        return {str(m["value"]) for node_id, models in self.catalog.items()
                if node_id in self.config and self.config[node_id].enabled
                and (org is None or org in self.config[node_id].orgs)
                and (routes is None or set(routes).intersection(self.config[node_id].routes))
                for m in models if isinstance(m, dict) and m.get("value")}
