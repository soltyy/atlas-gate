"""Одобренные endpoint, capabilities и HTTP-клиенты; не проксирует произвольные пути."""
from __future__ import annotations
import asyncio
import ipaddress
import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from pydantic import BaseModel, Field, model_validator
from .errors import GateError


class Node(BaseModel):
    node_id: str = Field(min_length=1)
    url: str
    token_env: str
    orgs: list[str]
    routes: list[str]
    account_group: str = Field(min_length=1)
    enabled: bool = True
    ca_file: str | None = None

    @model_validator(mode="after")
    def endpoint(self):
        url = urlsplit(self.url)
        try:
            loopback = ipaddress.ip_address(url.hostname or "").is_loopback
        except ValueError:
            loopback = url.hostname == "localhost"
        if url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
            raise ValueError("endpoint должен быть origin без credentials/path/query")
        if url.scheme != "https" and not (url.scheme == "http" and loopback):
            raise ValueError("Router требует HTTPS; HTTP допустим только для loopback")
        return self


class Nodes:
    def __init__(self, path: str, store=None):
        raw = json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).is_file() else []
        nodes = [Node.model_validate(n) for n in raw]
        if len({n.node_id for n in nodes}) != len(nodes):
            raise ValueError("повторяющийся node_id")
        self.config = {n.node_id: n for n in nodes}
        self.clients = {}
        for n in nodes:
            token = os.environ.get(n.token_env)
            if not token:
                raise ValueError(f"нет секрета endpoint {n.node_id}: {n.token_env}")
            self.clients[n.node_id] = httpx.AsyncClient(base_url=n.url.rstrip("/"),
                headers={"X-Atlas-Token": token}, follow_redirects=False, trust_env=False,
                verify=n.ca_file or True, timeout=httpx.Timeout(70, connect=5))
        self.snapshots: dict[str, dict] = {}
        self.cooldown: dict[str, float] = {}
        self.store = store
        self.catalog: dict[str, list] = {}
        if store is not None:
            store._exec("CREATE TABLE IF NOT EXISTS router_catalogs(node_id TEXT PRIMARY KEY, models TEXT NOT NULL)")
            self.catalog = {r["node_id"]: json.loads(r["models"]) for r in store._all("SELECT * FROM router_catalogs")}

    async def close(self):
        await asyncio.gather(*(c.aclose() for c in self.clients.values()))

    async def probe(self, node_id: str) -> dict | None:
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
            self.snapshots[node_id] = dict(info, observed=time.monotonic())
            if info["models"]:
                self.catalog[node_id] = info["models"]
                if self.store is not None:
                    self.store._exec("INSERT INTO router_catalogs VALUES (?,?) ON CONFLICT(node_id) DO UPDATE SET models=excluded.models", (node_id, json.dumps(info["models"])))
            return self.snapshots[node_id]
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            self.snapshots.pop(node_id, None)
            return None

    async def refresh(self):
        await asyncio.gather(*(self.probe(n.node_id) for n in self.config.values() if n.enabled))

    async def request(self, node_id: str, method: str, path: str, *, content=b"", params=None, expected_boot=None):
        node = self.config.get(node_id)
        if node is None or not node.enabled:
            raise GateError(503, "node_unavailable", "Router не подключён", node_id=node_id)
        info = self.snapshots.get(node_id)
        if not info or time.monotonic() - info["observed"] > 5:
            info = await self.probe(node_id)
        if info is None:
            raise GateError(503, "node_unavailable", "идентичность Router не подтверждена", node_id=node_id)
        headers = {"Content-Type": "application/json", "X-Atlas-Node": node_id,
                   "X-Atlas-Boot": expected_boot or info["bootId"]}
        try:
            response = await self.clients[node_id].request(method, path, content=content,
                params=params, headers=headers)
        except httpx.HTTPError as failed:
            raise GateError(503, "command_outcome_unknown" if method != "GET" else "node_unavailable",
                            "ответ Router не получен; повтор на другом узле запрещён", node_id=node_id) from failed
        if response.status_code == 429:
            self.cooldown[node.account_group] = time.monotonic() + 60
        if 300 <= response.status_code < 400:
            raise GateError(502, "node_protocol_error", "redirect Router запрещён")
        return response

    async def models(self):
        await self.refresh()
        return self.model_ids()

    def model_ids(self, org=None, routes=None):
        return {str(m["value"]) for node_id, models in self.catalog.items()
                if node_id in self.config and self.config[node_id].enabled
                and (org is None or org in self.config[node_id].orgs)
                and (routes is None or set(routes).intersection(self.config[node_id].routes))
                for m in models if isinstance(m, dict) and m.get("value")}
