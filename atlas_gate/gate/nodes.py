"""Одобренные endpoint, capabilities и HTTP-клиенты; не проксирует произвольные пути."""
from __future__ import annotations
import asyncio
import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import time
import ssl
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, quote, unquote
from typing import Literal
from uuid import uuid4
import httpx
from pydantic import BaseModel, Field, model_validator
from .errors import GateError, GateFreshNodeError


log = logging.getLogger("atlas_gate")

# Frequency of rechecking a failed direct link. Only a new, valid /v1/node
# response can allow dispatch, regardless of how much time has elapsed.
PROBE_RECHECK_INTERVAL = 1.0


def _http_cause(failed: httpx.HTTPError) -> str:
    name = type(failed).__name__
    return name if getattr(httpx, name, None) is type(failed) else "HTTPError"


@dataclass(frozen=True)
class ProbeFailure:
    reason: str
    cause: str
    correlation_id: str
    retryable: bool
    retry_at: float

    def diagnostics(self):
        return dict(reason=self.reason, phase='probe', cause=self.cause, correlationId=self.correlation_id)


class Node(BaseModel):
    node_id: str = Field(min_length=1)
    url: str = ""
    file_url: str = ""
    token_env: str = ""
    transport: Literal["direct", "connector"] = "direct"
    orgs: list[str]
    routes: list[str]
    account_group: str = Field(min_length=1)
    enabled: bool = True
    ca_file: str | None = None
    cert_file: str | None = None
    key_file: str | None = None
    account_turn_capacity: int = Field(default=0, ge=0)
    backend: Literal["", "claude", "codex"] = ""

    @model_validator(mode="after")
    def endpoint(self):
        if self.transport == "connector":
            if self.url or self.file_url or self.token_env or self.cert_file or self.key_file:
                raise ValueError("connector не использует входящий endpoint/секрет Router")
            return self
        if bool(self.cert_file) != bool(self.key_file):
            raise ValueError("для mTLS нужны cert_file и key_file вместе")
        self._validate_origin(self.url)
        if self.file_url:
            self._validate_origin(self.file_url)
            if httpx.URL(self.file_url).copy_with(path="/") == httpx.URL(self.url).copy_with(path="/"):
                raise ValueError("file_url должен указывать на отдельный endpoint загрузок")
        return self

    def _validate_origin(self, origin):
        url = urlsplit(origin)
        if not url.hostname or (url.port is not None and not 1 <= url.port <= 65535):
            raise ValueError("нужны корректные hostname и port")
        try:
            loopback = ipaddress.ip_address(url.hostname or "").is_loopback
        except ValueError:
            loopback = url.hostname == "localhost"
        if url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
            raise ValueError("endpoint должен быть origin без credentials/path/query")
        if url.scheme != "https" and not (url.scheme == "http" and loopback):
            raise ValueError("Router требует HTTPS; HTTP допустим только для loopback")
        if self.cert_file and url.scheme != "https":
            raise ValueError("mTLS требует HTTPS")


class Nodes:
    def __init__(self, path: str, store=None, channel=None, *, tokens=None):
        self.config_revision = hashlib.sha256(Path(path).read_bytes() if Path(path).is_file() else b"[]").hexdigest()
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
        self.file_clients = {}
        for n in nodes:
            if n.transport == 'connector':
                if channel is None:
                    raise ValueError('connector требует дисковый канал Gate')
                from .node_channel import ChannelClient
                self.clients[n.node_id] = ChannelClient(channel, n.node_id)
                continue
            token = (tokens or {}).get(n.token_env) or os.environ.get(n.token_env)
            if not token:
                raise ValueError(f"нет секрета endpoint {n.node_id}: {n.token_env}")
            context = ssl.create_default_context(cafile=n.ca_file)
            if n.cert_file:
                context.load_cert_chain(n.cert_file, n.key_file)
            # Retire idle sockets before Router's 5 s keep-alive, allowing time for tunnel latency.
            for url, pool in ((n.url, self.clients), (n.file_url, self.file_clients)):
                if url:
                    pool[n.node_id] = httpx.AsyncClient(base_url=url.rstrip("/"),
                        headers={"X-Atlas-Token": token}, follow_redirects=False, trust_env=False,
                        verify=context, timeout=httpx.Timeout(70, connect=5),
                        limits=httpx.Limits(keepalive_expiry=3.0))
        self._probes: dict[str, asyncio.Task] = {}
        self.snapshots: dict[str, dict] = {}
        self.probe_errors: dict[str, str] = {}
        self._probe_failures: dict[str, ProbeFailure] = {}
        self.cooldown: dict[str, float] = {}
        self.quota_seen: dict[str, float] = {}
        self.store = store
        self.catalog: dict[str, list] = {}
        if store is not None:
            store._exec("CREATE TABLE IF NOT EXISTS router_catalogs(node_id TEXT PRIMARY KEY, models TEXT NOT NULL)")
            self.catalog = {r["node_id"]: json.loads(r["models"]) for r in store._all("SELECT * FROM router_catalogs")}
            store._exec("CREATE TABLE IF NOT EXISTS router_account_cooldowns(account_group TEXT PRIMARY KEY, until REAL NOT NULL)")
            self.cooldown = {r["account_group"]: time.monotonic() + max(0, r["until"] - time.time()) for r in store._all("SELECT * FROM router_account_cooldowns WHERE until>?", (time.time(),))}
            store._exec("CREATE TABLE IF NOT EXISTS router_quota_observations(key TEXT PRIMARY KEY, value REAL NOT NULL)")
            self.quota_seen = {r["key"]: r["value"] for r in store._all("SELECT * FROM router_quota_observations")}


    async def close(self):
        pending = list(self._probes.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.gather(*(c.aclose() for c in (*self.clients.values(), *self.file_clients.values())))

    async def probe(self, node_id: str) -> dict | None:
        # Concurrent observers need the same fresh identity, not N serial SDK
        # auth/catalog requests. A timed-out observer must not cancel that check
        # for event delivery, another CREATE, or the background reconciler.
        task = self._probes.get(node_id)
        if task is None or task.done():
            failure = self._probe_failures.get(node_id)
            if failure and time.monotonic() < failure.retry_at and not self.revoked(node_id):
                return None
            task = asyncio.create_task(self._probe(node_id), name='atlas-node-probe-' + node_id)
            self._probes[node_id] = task
        verified = await asyncio.shield(task)
        # A later check or a failed request may have invalidated this result
        # before this waiter was scheduled again.
        return self.snapshots.get(node_id) if verified is not None else None

    def probe_retryable(self, node_id: str) -> bool:
        failure = self._probe_failures.get(node_id)
        return bool(failure and failure.retryable)

    def _record_probe_failure(self, node_id, reason, cause, message, *, retryable=False):
        retry_at = 0.0
        if reason == 'transport_unavailable' and self.config[node_id].transport == "direct":
            retry_at = time.monotonic() + PROBE_RECHECK_INTERVAL
        failure = ProbeFailure(reason, cause, uuid4().hex, retryable, retry_at)
        self.snapshots.pop(node_id, None)
        self.probe_errors[node_id] = message
        self._probe_failures[node_id] = failure
        log.warning("Router probe failed: node=%s phase=probe reason=%s cause=%s correlationId=%s",
                    node_id, reason, cause, failure.correlation_id)

    async def _probe(self, node_id: str) -> dict | None:
        if not self.config[node_id].enabled or self.revoked(node_id):
            self.snapshots.pop(node_id, None)
            self._probe_failures.pop(node_id, None)
            return None
        client = self.clients[node_id]
        if self.config[node_id].transport == "connector" and time.monotonic() - getattr(client.channel, "last_poll", {}).get(node_id, 0) > 60:
            # Одобрение и первый poll могут прийти одновременно с созданием беседы.
            # Короткая grace-пауза не превращает список узлов в ожидание 70-секундной очереди.
            credential = self.store and self.store._one("SELECT node_id FROM node_credentials WHERE node_id=? AND revoked=0 AND expires>?", (node_id, time.time()))
            deadline = time.monotonic() + (2 if credential else 0)
            while time.monotonic() < deadline and time.monotonic() - client.channel.last_poll.get(node_id, 0) > 60:
                await asyncio.sleep(0.02)
        if self.config[node_id].transport == "connector" and time.monotonic() - getattr(client.channel, "last_poll", {}).get(node_id, 0) > 60:
            self._record_probe_failure(node_id, 'transport_unavailable', 'ConnectorOffline',
                "Ожидается исходящее подключение. Выполните регистрацию, одобрите заявку и запустите atlas-node run.",
                retryable=True)
            return None
        try:
            response = await self.clients[node_id].get("/v1/node")
            response.raise_for_status()
            info = response.json()
            if not isinstance(info, dict) or not isinstance(info.get("nodeId"), str) or not info["nodeId"]:
                raise ValueError("missing identity")
            if info["nodeId"] != node_id:
                self._record_probe_failure(node_id, 'node_id_mismatch', 'UnexpectedNodeId',
                                           "Router сообщил другой NODE_ID. Проверьте привязку узла.")
                return None
            info['connectorProtocol'] = (2 if response.headers.get('x-atlas-transport-contract') == '2' else 1) if self.config[node_id].transport == 'connector' else None
            if self.config[node_id].backend and info.get("backend") != self.config[node_id].backend:
                self._record_probe_failure(node_id, 'backend_mismatch', 'UnexpectedBackend',
                                           "Router использует другого поставщика. Проверьте ATLAS_BACKEND.")
                return None
            if info.get("protocolVersion") != 1 or not info.get("bootId"):
                raise ValueError("identity/protocol mismatch")
            if type(info.get("capacity")) is not int or info["capacity"] < 0:
                raise ValueError("invalid capacity")
            if not isinstance(info.get("models"), list):
                raise ValueError("invalid catalog")
            if not {"durable_commands", "durable_events"}.issubset(info.get("capabilities", [])):
                raise ValueError("Gate требует дисковый журнал Router")
            if not self.config[node_id].enabled or self.revoked(node_id):
                self.snapshots.pop(node_id, None)
                self._probe_failures.pop(node_id, None)
                return None
            self.snapshots[node_id] = dict(info, observed=time.monotonic())
            self._probe_failures.pop(node_id, None)
            self.probe_errors.pop(node_id, None)
            if isinstance(info.get("quota"), dict):
                self.observe_event(node_id, dict(info["quota"], type="rate_limit"))
            if info["models"]:
                self.catalog[node_id] = info["models"]
                if self.store is not None:
                    self.store._exec("INSERT INTO router_catalogs VALUES (?,?) ON CONFLICT(node_id) DO UPDATE SET models=excluded.models", (node_id, json.dumps(info["models"])))
            return self.snapshots[node_id]
        except (httpx.HTTPError, ValueError, TypeError, KeyError, GateError) as failed:
            if isinstance(failed, httpx.TransportError) or isinstance(failed, GateError) and failed.code == 'node_unavailable':
                cause = _http_cause(failed) if isinstance(failed, httpx.TransportError) else "ConnectorUnavailable"
                self._record_probe_failure(node_id, 'transport_unavailable', cause,
                    "Gate не получил ответ при проверке доступности Router.", retryable=True)
            elif isinstance(failed, httpx.HTTPStatusError):
                status = failed.response.status_code
                message = ("Router отверг ключ доступа. Проверьте ATLAS_TOKEN." if status in (401, 403)
                           else "Проверка доступности Router вернула ошибку HTTP.")
                self._record_probe_failure(node_id, 'probe_http_error', f"HTTP{status}", message,
                                           retryable=status in (502, 503, 504))
            else:
                self._record_probe_failure(node_id, 'identity_unconfirmed', 'InvalidNodeMetadata',
                    "Router не подтвердил NODE_ID или протокол с дисковым журналом. Проверьте NODE_ID и NODE_DB.")
            return None

    async def refresh(self):
        await asyncio.gather(*(self.probe(n.node_id) for n in self.config.values() if n.enabled))

    async def refresh_for(self, org, route, pinned_node=None):
        await asyncio.gather(*(self.probe(n.node_id) for n in self.config.values()
            if n.enabled and org in n.orgs and route in n.routes
            and (pinned_node is None or n.node_id == pinned_node)))

    async def _transport_identity(self, client, node_id, expected_boot, *, legacy_probe=False):
        """Read-only Router identity. Never publishes SDK readiness or changes boot pins."""
        headers = {"X-Atlas-Node": node_id}
        if expected_boot:
            headers["X-Atlas-Boot"] = expected_boot
        try:
            response = await client.request("GET", "/v1/node/identity", headers=headers)
            if legacy_probe and response.status_code == 404 and response.json() == {"detail": "Not Found"}:
                # A pre-0.8.37 direct Router has only the original identity contract.
                info = await self.probe(node_id)
                boot = info["bootId"] if info else None
            else:
                response.raise_for_status()
                info = response.json()
                if (not isinstance(info, dict) or set(info) != {"nodeId", "bootId", "protocolVersion"}
                        or info["nodeId"] != node_id or type(info["protocolVersion"]) is not int
                        or info["protocolVersion"] != 1):
                    raise ValueError("invalid journal identity")
                boot = info["bootId"]
            if (not isinstance(boot, str) or not boot or not boot.isascii()
                    or not all(c.isalnum() or c in "_-" for c in boot) or len(boot) > 128
                    or expected_boot is not None and boot != expected_boot):
                raise ValueError("unconfirmed journal boot")
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            raise GateError(503, "node_unavailable", "Router не подтвердил идентичность на выбранном канале",
                            node_id=node_id) from error
        if not self.config[node_id].enabled or self.revoked(node_id):
            raise GateError(503, "node_unavailable", "Router не подключён", node_id=node_id)
        return boot

    async def request(self, node_id: str, method: str, path: str, *, content=b"", params=None,
                      expected_boot=None, request_id=None, tool_result_turn=None):
        node = self.config.get(node_id)
        if node is None or not node.enabled or self.revoked(node_id):
            raise GateFreshNodeError('Router не подключён', node_id=node_id, method=method,
                                     path=path, reason='node_unavailable')
        # The durable command journal is independent of SDK discovery. Connector
        # transports keep their own incoming-poll and capability verification.
        journal_path = path.split("/")
        journal = (node.transport == "direct" and method == "GET" and len(journal_path) == 4
                   and journal_path[:3] == ["", "v1", "commands"] and bool(journal_path[3]))
        client = self.clients[node_id]
        info = self.snapshots.get(node_id)
        if journal:
            boot = await self._transport_identity(client, node_id, expected_boot, legacy_probe=True)
            info = {"bootId": boot}
        elif not info or time.monotonic() - info["observed"] > 5:
            info = await self.probe(node_id)
            if not node.enabled or self.revoked(node_id):
                raise GateFreshNodeError('Router не подключён', node_id=node_id, method=method,
                                         path=path, reason='node_unavailable')
        if info is None:
            failure = self._probe_failures.get(node_id)
            # Stored pre-dispatch command receipts have a byte-stable legacy message.
            message = 'идентичность Router не подтверждена'
            if method == 'GET':
                message = self.probe_errors.get(node_id, message)
            raise GateFreshNodeError(message, node_id=node_id,
                                     method=method, path=path, reason='identity_unconfirmed',
                                     retryable=self.probe_retryable(node_id),
                                     diagnostics=failure.diagnostics() if failure else None)
        if node.file_url and method == "POST" and re.fullmatch(
                r"/v1/sessions/[^/]+/(?:documents|files|files/adaptive/[^/]+)", path):
            if not expected_boot or info["bootId"] != expected_boot:
                raise GateFreshNodeError("Основной канал не подтвердил boot привязанной сессии", node_id=node_id,
                                         method=method, path=path, reason="identity_unconfirmed")
            client = self.file_clients[node_id]
            try:
                # A control probe cannot attest the separately configured file transport.
                await self._transport_identity(client, node_id, expected_boot)
            except GateError as failed:
                raise GateFreshNodeError(failed.message, node_id=node_id, method=method, path=path,
                                         reason="identity_unconfirmed") from failed
        headers = {"Content-Type": "application/json", "X-Atlas-Node": node_id,
                   "X-Atlas-Boot": expected_boot or info["bootId"]}
        if journal:
            headers["X-Atlas-Journal-Read"] = "1"
        question_probe = method == 'GET' and path.endswith('/sdk-tools') and dict(params or {}).get('questionAnswers') == '1'
        if method == 'POST' and path.endswith('/question-answer') or question_probe:
            if not expected_boot:
                raise GateError(422, 'question_answer_target_boot_required', 'Нужен исходный boot для ответа')
            headers['X-Atlas-Question-Answer-Contract'] = '1'
        steering_probe = (method == 'GET' and path.endswith('/sdk-tools')
                          and dict(params or {}).get('steering') == '1')
        if method == 'POST' and path.endswith('/steer') or steering_probe:
            try:
                strong = json.loads(content).get('expectedTurnId') is not None
            except (ValueError, AttributeError):
                strong = False
            if strong or steering_probe:
                if not expected_boot:
                    raise GateError(422, 'steer_target_boot_required', 'Strong steer требует исходный boot')
                headers['X-Atlas-Steer-Contract'] = '1'
        if method == "GET" and path.endswith(("/files", "/sdk-tools")):
            headers["X-Atlas-Input-Recovery"] = "1"
        if request_id:
            headers["Idempotency-Key"] = request_id
        if tool_result_turn is not None:
            params = {"toolResultTurn": tool_result_turn}
        try:
            options = {"timeout": httpx.Timeout(connect=5, read=None, write=70, pool=5)} if path.endswith("/compact") and self.config[node_id].transport != 'connector' else {}
            response = await client.request(method, path, content=content,
                params=params, headers=headers, **options)
        except httpx.HTTPError as failed:
            correlation_id = uuid4().hex
            cause = _http_cause(failed)
            # A lost reply cannot change the identity established by /v1/node.
            # In particular, preserve a newly observed boot for steer fencing.
            log.warning("Router transport failed: node=%s phase=request method=%s cause=%s correlationId=%s",
                        node_id, method, cause, correlation_id)
            raise GateError(503, "command_outcome_unknown" if method != "GET" else "node_unavailable",
                            "ответ Router не получен; повтор на другом узле запрещён", node_id=node_id,
                            reason='transport_unavailable', phase='request', cause=cause,
                            correlationId=correlation_id) from failed
        if journal:
            if not node.enabled or self.revoked(node_id):
                raise GateError(503, "node_unavailable", "Router не подключён", node_id=node_id)
            if response.status_code in (200, 404):
                # Only the responding journal can attribute its own result. Two
                # identity probes could both use A while the receipt came from B.
                proof = tuple(response.headers.get(k) for k in
                              ("X-Atlas-Journal-Read", "X-Atlas-Node", "X-Atlas-Boot"))
                attributed = proof == ("1", node_id, info["bootId"])
                try:
                    saved = response.json()
                except ValueError:
                    saved = None
                if response.status_code == 404:
                    valid = attributed and saved == {"detail": "command_not_found"}
                else:
                    # Older Routers can still return an immutable result for the
                    # exact command, including after a legitimate restart.
                    legacy = expected_boot is None and proof == (None, None, None)
                    valid = ((attributed or legacy) and isinstance(saved, dict)
                             and saved.get("requestId") == unquote(journal_path[3]))
                if not valid:
                    raise GateError(503, "command_outcome_unknown", "Router не подтвердил источник квитанции",
                                    node_id=node_id)
        if response.status_code == 429 and "capacity_exceeded" not in response.text:
            self.observe_event(node_id, {"type": "rate_limit", "observationId": request_id or str(time.time_ns())})
        if response.status_code == 409 and "command_outcome_unknown" in response.text:
            raise GateError(503, "command_outcome_unknown", "Router ещё не подтвердил итог команды; требуется сверка", node_id=node_id)
        if 300 <= response.status_code < 400:
            raise GateError(502, "node_protocol_error", "redirect Router запрещён")
        if (method != 'GET' and request_id and response.status_code >= 500
                and '/files/adaptive/' not in path and not path.endswith('/question-answer')):
            # 5xx может принадлежать relay, а команда уже принята Router.
            # Только его дисковая квитанция подтверждает итог, включая настоящий 5xx.
            from .receipts import completed_response
            confirmed = None
            try:
                receipt = await self.request(node_id, 'GET', '/v1/commands/' + quote(request_id, safe=''))
                if receipt.status_code == 200:
                    confirmed = completed_response(receipt.json(), request_id)
            except (GateError, ValueError, TypeError):
                pass
            if confirmed is None:
                raise GateError(503, 'command_outcome_unknown',
                                'ответ канала не подтверждён журналом Router; требуется сверка',
                                command_id=request_id)
            return confirmed
        return response

    def observe_event(self, node_id, event):
        kind = event.get("type")
        if kind != "rate_limit" and not (kind == "error" and event.get("kind") in ("rate_limit", "rate_limit_exceeded", "usage_limit")):
            return
        group = self.config[node_id].account_group
        allowed = kind == "rate_limit" and event.get("status") in ("allowed", "allowed_warning")
        reset = event.get("resetsAt")
        reset = float(reset) if type(reset) in (int, float) and math.isfinite(reset) and reset > 0 else None
        stream = f"event:{node_id}:{event['id']}" if event.get("id") and type(event.get("seq")) is int else None
        key = None
        value = 0
        if stream:
            value = event["seq"]
            if self.quota_seen.get(stream, -1) >= value:
                return
            key = stream
        elif not allowed and reset is None:
            # Cached quota and HTTP/event replay must not renew a relative interval.
            key = "relative:" + node_id + ":" + hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
            if key in self.quota_seen:
                return
        now = time.time()
        retry = event.get("retryAfterSeconds", 60)
        retry = float(retry) if type(retry) in (int, float) and math.isfinite(retry) else 60
        until = reset if reset is not None else now + max(1, min(retry, 86400))
        if reset is None and not allowed:
            until = max(until, now + max(0, self.cooldown.get(group, 0) - time.monotonic()))
        if not allowed and until <= now:
            # Expired historical rejection cannot create a new block or clear a newer one.
            if key:
                self._quota_update(group, None, key, value, change=False)
            return
        self._quota_update(group, 0 if allowed else until, key, value)

    def _quota_update(self, group, until, key, value, *, change=True):
        if self.store:
            with self.store._lock:
                db = self.store._db
                db.execute("BEGIN IMMEDIATE")
                try:
                    if key:
                        db.execute("INSERT INTO router_quota_observations VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
                    if change:
                        if until == 0:
                            db.execute("DELETE FROM router_account_cooldowns WHERE account_group=?", (group,))
                        else:
                            db.execute("INSERT INTO router_account_cooldowns VALUES (?,?) ON CONFLICT(account_group) DO UPDATE SET until=excluded.until", (group, until))
                    db.execute("COMMIT")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
        if key:
            self.quota_seen[key] = value
        if change:
            if until == 0:
                self.cooldown.pop(group, None)
            else:
                self.cooldown[group] = time.monotonic() + max(0, until - time.time())

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

    def route_catalog(self, org, route):
        """Approved cached model names for one organisation and route, including outages."""
        entries = {}
        for node_id, models in sorted(self.catalog.items()):
            node = self.config.get(node_id)
            if not node or not node.enabled or self.revoked(node_id) or org not in node.orgs or route not in node.routes:
                continue
            for model in models:
                if isinstance(model, dict) and model.get("value"):
                    value = str(model["value"])
                    entries.setdefault(value, {"value": value, "name": str(model.get("name") or value)})
        return [entries[value] for value in sorted(entries)]
