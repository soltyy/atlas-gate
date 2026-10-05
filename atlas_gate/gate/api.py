"""Маршруты гейта `/harness/*`: control plane, прямой прокси к провайдерам, админка.

Агентный ход на подписке (`/harness/agent/*`) — в `agent.py`, он же подключается к этому роутеру.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import FileResponse, Response
from fastapi.security import HTTPBearer

from .. import __version__
from ..auth import require_token
from .crypto import hmac_hex, new_token, new_user_code, sha256_hex
from .errors import GateError, GateRoute, error_response
from .schemas import (
    GateAuditAccepted,
    GateAuditBatch,
    GateEnrollApprove,
    GateEnrollDeny,
    GateEnrollPoll,
    GateEnrollResult,
    GateEnrollStart,
    GateEnrollStarted,
    GateErrorResponse,
    GateHealth,
    GateManifest,
    GateOk,
    GateQuota,
    GateRefresh,
    GateReloaded,
    GateRevoked,
    GateTokens,
)
from .state import Device, GateState
from .discovery import Discovery, HarnessSettings, public_catalog, client_settings, json_snapshot
from .upstream import check_quota, month_bounds, pick_route, proxy, with_usage_option

log = logging.getLogger("atlas_gate")

ENROLL_TTL_SEC = 900
POLL_INTERVAL_SEC = 5
LLM_BODY_LIMIT = 10 * 1024 * 1024
AUDIT_BODY_LIMIT = 1024 * 1024
AUDIT_MAX_EVENTS = 1000
STRIPPED_AUDIT_KEYS = {"text", "content", "args", "result"}
STATIC_DIR = Path(__file__).parent / "static"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    state = GateState(app)
    app.state.gate = state

    async def first_reload() -> None:
        try:
            await state.reload()
        except Exception:  # noqa: BLE001
            log.exception("первая сборка профилей гейта не удалась")
            state.ready.set()

    task = asyncio.create_task(first_reload(), name="atlas-gate-reload")
    async def recovery():
        from .agent import _watch
        await state.ready.wait()
        watched = set()
        synchronized = 0
        while True:
            await state.routing.reconcile()
            await state.operations.reconcile(state.nodes)
            if time.monotonic() - synchronized > 30:
                await state.routing.sync_bindings()
                synchronized = time.monotonic()
            rows = state.store._all("SELECT o.*, b.id binding_id FROM router_operations o JOIN router_bindings b ON b.id=o.session_id LEFT JOIN router_usage_receipts r ON r.session_id=o.session_id AND r.turn_id=o.turn_id WHERE o.state='complete' AND o.status<400 AND o.turn_id IS NOT NULL AND o.accounted=0 AND r.turn_id IS NULL")
            from .state import Device
            for row in rows:
                key = (row["session_id"], row["turn_id"])
                if key in watched:
                    continue
                binding = state.store._one("SELECT * FROM router_bindings WHERE id=?", (row["session_id"],))
                dev = state.store.device(row["device_id"])
                if not binding or not dev:
                    continue
                device = Device(dev["device_id"], dev["org"], dev["user_id"], dev["user_email"], dev["user_name"], dev)
                watched.add(key)
                watcher = asyncio.create_task(_watch(state, binding, row["turn_id"], device))
                state.watchers.add(watcher)
                watcher.add_done_callback(state.watchers.discard)
                watcher.add_done_callback(lambda done, key=key: watched.discard(key))
            await asyncio.sleep(2)
    recovery_task = asyncio.create_task(recovery(), name="atlas-gate-recovery")
    state.recovery_task = recovery_task
    log.info("гейт включён: БД %s, ключ подписи kid %s…, ключи провайдеров %s", state.settings.ATLAS_GATE_DB,
             state.key.kid[:12], state.settings.ATLAS_GATE_KEYS_FILE)
    try:
        yield
    finally:
        task.cancel()
        recovery_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await recovery_task
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        await state.close()


ERRORS = {code: {"model": GateErrorResponse} for code in (400, 401, 402, 403, 404, 409, 410, 413, 422, 428, 503)}
router = APIRouter(route_class=GateRoute, lifespan=lifespan, tags=["gate"], responses=ERRORS)
admin = [Depends(require_token)]


def gate(request: Request) -> GateState:
    return request.app.state.gate


async def ready_gate(request: Request) -> GateState:
    state = gate(request)
    if not state.ready.is_set():
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(state.ready.wait(), 60)
    return state


def _token_from(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    # Anthropic SDK шлёт ключ в x-api-key — для /harness/llm/v1/messages это и есть device_token.
    return (request.headers.get("x-api-key") or "").strip() or None


# Архивная организация (#7): устройствам закрыты профиль, LLM-прокси и ход на подписке.
ARCHIVED_BLOCKS = ("/harness/profile", "/harness/settings", "/harness/llm/", "/harness/agent/")


async def require_device(request: Request) -> Device:
    state = gate(request)
    token = _token_from(request)
    if not token:
        raise GateError(401, "unauthorized", "нужен Authorization: Bearer <device_token>")
    row = state.store.token_by_hash(sha256_hex(token))
    if row is None:
        raise GateError(401, "unauthorized", "device_token не распознан")
    dev = state.store.device(row["device_id"])
    if dev is None:
        raise GateError(401, "unauthorized", "устройство не найдено")
    if dev["revoked_at"] is not None:
        raise GateError(401, "device_revoked", "устройство отозвано администратором — нужна повторная регистрация")
    if row["expires_at"] < time.time():
        raise GateError(401, "token_expired", "срок device_token истёк — обновите его POST /harness/token/refresh")
    org = state.orgs.get(dev["org"])
    if org is not None and org.archived and request.url.path.startswith(ARCHIVED_BLOCKS):
        raise GateError(403, "org_archived", f"организация {dev['org']!r} в архиве — профиль, модели и агент недоступны")
    return Device(dev["device_id"], dev["org"], dev["user_id"], dev["user_email"], dev["user_name"], dev)


def org_of(state: GateState, device: Device):
    org = state.orgs.get(device.org)
    if org is None:
        raise GateError(503, "profile_invalid", f"конфигурация организации {device.org!r} не загружена: "
                        + state.org_errors.get(device.org, "нет файла"))
    return org


def check_data_class(state: GateState, device: Device, request: Request, route_id: str) -> None:
    """X-Harness-Data-Class: класс не выше потолка маршрута и маршрут разрешён матрицей класса."""
    cls = request.headers.get("x-harness-data-class")
    if not cls:
        return
    org = org_of(state, device)
    classes = org.policy.data_classes
    route = org.route(route_id)
    if cls not in classes or route is None:
        raise GateError(403, "policy_denied", f"класс данных {cls!r} неизвестен", reason="unknown_data_class",
                        data_class=cls, route=route_id)
    allowed = org.policy.routes_by_class.get(cls)
    ceiling = route.max_data_class or classes[-1]
    if classes.index(cls) > classes.index(ceiling) or (allowed is not None and route_id not in allowed):
        raise GateError(403, "policy_denied", f"маршрут {route_id} не допускает данные класса {cls}",
                        reason="data_class_not_allowed", data_class=cls, route=route_id)


# --- регистрация устройства ---------------------------------------------------------------------


@router.post("/harness/enroll/start", response_model=GateEnrollStarted)
async def enroll_start(body: GateEnrollStart, request: Request) -> Any:
    state = gate(request)
    for _ in range(10):
        user_code = new_user_code()
        if state.store.enrollment_by_user_code(user_code) is None:
            break
    device_code = new_token("hdc_")
    state.store.enrollment_create(device_code, user_code, body.device_name, body.platform, body.app_version, ENROLL_TTL_SEC)
    log.info("регистрация начата: %s (%s, %s) — код %s", body.device_name, body.platform, body.app_version, user_code)
    return {"device_code": device_code, "user_code": user_code,
            "verification_url": f"{state.public_url}/harness/admin/?user_code={user_code}",
            "interval": POLL_INTERVAL_SEC, "expires_in": ENROLL_TTL_SEC}


def _issue_tokens(state: GateState, device_id: str) -> tuple[str, str, int]:
    s = state.settings
    ttl = min(s.ATLAS_GATE_DEVICE_TOKEN_TTL_SEC, 86400)
    device_token, refresh_token = new_token("hdt_"), new_token("hrt_")
    now = time.time()
    state.store.tokens_set(device_id, sha256_hex(device_token), now + ttl,
                           sha256_hex(refresh_token), now + s.ATLAS_GATE_REFRESH_TOKEN_TTL_SEC)
    return device_token, refresh_token, ttl


@router.post("/harness/enroll/poll", response_model=GateEnrollResult)
async def enroll_poll(body: GateEnrollPoll, request: Request) -> Any:
    state = gate(request)
    row = state.store.enrollment_by_device_code(body.device_code)
    if row is None:
        raise GateError(404, "not_found", "device_code неизвестен")
    status = row["status"]
    if status == "pending" and row["expires_at"] < time.time():
        state.store.enrollment_set(body.device_code, "expired")
        status = "expired"
    if status == "pending":
        return error_response(428, "enroll_pending", "администратор ещё не одобрил код",
                              headers={"Retry-After": str(POLL_INTERVAL_SEC)})
    if status == "denied":
        raise GateError(403, "enroll_denied", "администратор отклонил регистрацию")
    if status in ("expired", "consumed"):
        raise GateError(410, "enroll_expired", "срок кода истёк" if status == "expired" else "код уже использован")
    if not state.store.enrollment_consume(body.device_code):
        raise GateError(410, "enroll_expired", "код уже использован")
    dev = state.store.device(row["device_id"])
    org = state.orgs.get(dev["org"])
    device_token, refresh_token, ttl = _issue_tokens(state, dev["device_id"])
    log.info("устройство %s получило токены (%s, %s)", dev["device_id"], dev["user_email"], dev["org"])
    return {"device_id": dev["device_id"], "device_token": device_token, "expires_in": ttl,
            "refresh_token": refresh_token, "device_secret": state.keyring.open(dev["device_secret"]),
            "profile_signing_key": state.key.jwk(),
            "user": {"id": dev["user_id"], "email": dev["user_email"], "name": dev["user_name"]},
            "org": {"id": dev["org"], "name": org.name if org else dev["org"]}}


@router.post("/harness/enroll/approve", dependencies=admin, response_model=GateOk)
async def enroll_approve(body: GateEnrollApprove, request: Request) -> Any:
    state = await ready_gate(request)
    row = state.store.enrollment_by_user_code(body.user_code.strip().upper())
    if row is None:
        raise GateError(404, "not_found", "user_code неизвестен")
    if row["status"] != "pending" or row["expires_at"] < time.time():
        raise GateError(410, "enroll_expired", f"регистрация не ожидает одобрения (состояние {row['status']})")
    org = state.orgs.get(body.org)
    if org is None:
        raise GateError(422, "profile_invalid", f"организация {body.org!r} не загружена")
    if org.archived:
        raise GateError(403, "org_archived", f"организация {body.org!r} в архиве — регистрации в неё не одобряются")
    device_id = str(uuid.uuid4())
    state.store.device_create({
        "device_id": device_id, "org": org.id, "user_id": body.user.id, "user_email": body.user.email,
        "user_name": body.user.name, "device_name": row["device_name"], "platform": row["platform"],
        "app_version": row["app_version"], "device_secret": state.keyring.seal(secrets.token_urlsafe(32)),
        "litellm_key": None, "litellm_key_id": None,
        "created_at": time.time(), "revoked_at": None,
    })
    state.store.enrollment_set(row["device_code"], "approved", user=body.user.model_dump(), org=org.id, device_id=device_id)
    log.info("регистрация %s одобрена: устройство %s → %s (%s)", body.user_code, device_id, body.user.email, org.id)
    return {"ok": True}


@router.post("/harness/enroll/deny", dependencies=admin, response_model=GateOk)
async def enroll_deny(body: GateEnrollDeny, request: Request) -> Any:
    state = gate(request)
    row = state.store.enrollment_by_user_code(body.user_code.strip().upper())
    if row is None or row["status"] != "pending":
        raise GateError(404, "not_found", "ожидающей регистрации с таким кодом нет")
    state.store.enrollment_set(row["device_code"], "denied")
    return {"ok": True}


# --- токены и отзыв ---------------------------------------------------------------------------


@router.post("/harness/token/refresh", response_model=GateTokens)
async def token_refresh(body: GateRefresh, request: Request) -> Any:
    state = gate(request)
    row = state.store.token_by_refresh_hash(sha256_hex(body.refresh_token))
    if row is None:
        raise GateError(401, "unauthorized", "refresh_token не распознан")
    dev = state.store.device(row["device_id"])
    if dev is None or dev["revoked_at"] is not None:
        raise GateError(401, "device_revoked", "устройство отозвано")
    if row["refresh_expires_at"] < time.time():
        raise GateError(401, "token_expired", "срок refresh_token истёк — нужна повторная регистрация")
    device_token, refresh_token, ttl = _issue_tokens(state, dev["device_id"])
    return {"device_token": device_token, "expires_in": ttl, "refresh_token": refresh_token}


async def revoke_device(state: GateState, device_id: str) -> dict[str, Any]:
    """Отзыв: revoked_at и закрытие агентных сессий устройства (следующий запрос — 401 device_revoked)."""
    dev = state.store.device(device_id)
    if dev is None:
        raise GateError(404, "not_found", "устройство не найдено")
    state.store.device_revoke(device_id)
    closed = 0
    bindings = state.store._all("SELECT * FROM router_bindings WHERE device_id=? AND status='ready'", (device_id,))
    from urllib.parse import quote
    for binding in bindings:
        state.store._exec("UPDATE router_bindings SET status='closing' WHERE id=?", (binding["id"],))
        try:
            binding = await state.routing.recover_boot(binding)
            response = await state.nodes.request(binding["node_id"], "DELETE", "/v1/sessions/" + quote(binding["local_id"], safe=""), expected_boot=binding["boot_id"], request_id="revoke:" + device_id + ":" + binding["id"])
            if response.status_code < 400 or response.status_code == 404:
                state.store._exec("UPDATE router_bindings SET status='closed' WHERE id=?", (binding["id"],))
                closed += 1
        except GateError:
            log.warning("отзыв устройства: закрытие беседы %s требует сверки", binding["id"])
    log.info("устройство %s отозвано: агентных сессий закрыто %d", device_id, closed)
    return {"ok": True, "device_id": device_id, "agent_sessions_closed": closed}


@router.post("/harness/token/revoke", response_model=GateRevoked)
async def token_revoke(request: Request, device: Device = Depends(require_device)) -> Any:
    return await revoke_device(gate(request), device.device_id)


@router.post("/harness/devices/{device_id}/revoke", dependencies=admin, response_model=GateRevoked)
async def device_revoke(device_id: str, request: Request) -> Any:
    return await revoke_device(gate(request), device_id)


# --- профиль, квота, обновление ----------------------------------------------------------------


@router.get('/harness/discovery', response_model=Discovery, responses={304: {'description': 'каталог не изменился'}})
async def discovery(request: Request):
    """Без авторизации: все настроенные модели и контракт подключения, без настроек организаций."""
    state = await ready_gate(request)
    if not state.ready.is_set():
        raise GateError(503, 'not_ready', 'каталог Gate ещё не загружен')
    return json_snapshot(request, public_catalog(state))


@router.get('/harness/settings', response_model=HarnessSettings,
            dependencies=[Depends(HTTPBearer(auto_error=False, scheme_name='DeviceToken'))],
            responses={304: {'description': 'настройки не изменились'}})
async def settings(request: Request, device: Device = Depends(require_device)):
    """Полный клиентский JSON профиль своей организации, с исходной JWS подписью."""
    state = await ready_gate(request)
    org = org_of(state, device)
    if org.archived:
        raise GateError(403, 'org_archived', 'организация в архиве — настройки недоступны')
    row = state.store.profile(device.org)
    if row is None:
        raise GateError(503, 'profile_invalid', 'профиль организации ещё не подписан')
    return json_snapshot(request, client_settings(state, row), private=True)


@router.get("/harness/profile", responses={200: {"content": {"application/jose": {}}, "description": "JWS EdDSA"},
                                            304: {"description": "не изменился (If-None-Match)"}})
async def profile(request: Request, device: Device = Depends(require_device)) -> Any:
    state = await ready_gate(request)
    org_of(state, device)
    row = state.store.profile(device.org)
    if row is None:
        raise GateError(503, "profile_invalid", "профиль организации ещё не подписан")
    headers = {"ETag": row["etag"], "X-Harness-Profile-Version": str(row["profile_version"]), "Cache-Control": "no-cache"}
    wanted = [t.strip().removeprefix("W/") for t in (request.headers.get("if-none-match") or "").split(",")]
    if row["etag"] in wanted or "*" in wanted:
        return Response(status_code=304, headers=headers)
    return Response(content=row["jws"], media_type="application/jose", headers=headers)


@router.get("/harness/quota", response_model=GateQuota)
async def quota(request: Request, device: Device = Depends(require_device)) -> Any:
    """Деньги за календарный месяц UTC по пользователю — из таблицы usage гейта (GW-DIRECT-01)."""
    state = gate(request)
    org = org_of(state, device)
    since, until, reset_at = month_bounds()
    used = state.store.spend(device.user_id, since, until)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    turns = {"day": day, "limit": org.quota.agent_turns_per_day, "used": state.store.agent_turns(device.user_id, day)}
    limit = org.quota.limit
    return {"limit": limit, "used": round(used, 8), "remaining": max(limit - used, 0.0) if limit is not None else None,
            "currency": org.quota.currency, "reset_at": reset_at, "agent_turns": turns}


@router.get("/harness/update/manifest", response_model=GateManifest)
async def update_manifest(request: Request, device: Device = Depends(require_device)) -> Any:
    org = org_of(gate(request), device)
    if org.update_manifest is None:
        raise GateError(404, "not_found", "манифест обновления для организации не задан")
    return org.update_manifest.model_dump()


# --- аудит ------------------------------------------------------------------------------------


def _strip(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if k not in STRIPPED_AUDIT_KEYS}
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


@router.post("/harness/audit", response_model=GateAuditAccepted,
             openapi_extra={"requestBody": {"content": {"application/json": {"schema": GateAuditBatch.model_json_schema()}}}})
async def audit(request: Request, device: Device = Depends(require_device)) -> Any:
    state = gate(request)
    raw = await request.body()
    if len(raw) > AUDIT_BODY_LIMIT:
        raise GateError(413, "payload_too_large", f"батч аудита больше {AUDIT_BODY_LIMIT} байт")
    try:
        doc = json.loads(raw)
        batch = GateAuditBatch.model_validate(doc)
    except Exception as bad:  # noqa: BLE001
        raise GateError(422, "invalid_request", f"батч аудита не разобран: {bad}") from bad
    if len(batch.events) > AUDIT_MAX_EVENTS:
        raise GateError(413, "payload_too_large", f"в батче больше {AUDIT_MAX_EVENTS} событий")
    # ПОДПИСЬ СЧИТАЕТСЯ ПО ТОМУ, ЧТО ПРИСЛАЛ КЛИЕНТ (разобранный JSON, не нормализованный pydantic).
    expected = hmac_hex(state.keyring.open(device.row["device_secret"]),
                        {"session_id": doc["session_id"], "events": doc["events"]})
    if not secrets.compare_digest(expected, str(batch.hmac).lower()):
        raise GateError(401, "bad_signature", "HMAC батча не совпал с device_secret устройства")
    include = org_of(state, device).audit.include_messages
    rows = []
    for ev in doc["events"]:
        stored = dict(ev)
        if not include:
            stored["payload"] = _strip(stored.get("payload") or {})
        rows.append((int(ev["seq"]), json.dumps(stored, ensure_ascii=False, sort_keys=True)))
    state.store.audit_add(device.device_id, batch.session_id, rows)
    return {"accepted_up_to": state.store.audit_max_seq(device.device_id, batch.session_id)}


# --- здоровье ---------------------------------------------------------------------------------


@router.get("/harness/health", response_model=GateHealth)
async def health(request: Request) -> Any:
    state = gate(request)
    agent_ok = await state.agent_backend_ok()
    return {"ok": bool(state.orgs) and not state.org_errors, "version": __version__,
            "profile_version": state.profile_version(), "upstreams": dict(state.upstreams),
            "agent_backend": "ok" if agent_ok else "down"}


# --- /harness/llm/* — напрямую к апстримам ----------------------------------------------------


async def _read_limited(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise GateError(413, "payload_too_large", f"тело больше {limit} байт")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise GateError(413, "payload_too_large", f"тело больше {limit} байт")
        chunks.append(chunk)
    return b"".join(chunks)


LLM_BODY = {"requestBody": {"content": {"application/json": {"schema": {"type": "object", "required": ["model"],
                                                                         "properties": {"model": {"type": "string"}}}}}}}


@router.post("/harness/llm/v1/chat/completions", openapi_extra=LLM_BODY)
async def llm_chat(request: Request, device: Device = Depends(require_device)) -> Any:
    return await _llm_post(request, device, "openai", "/chat/completions")


@router.post("/harness/llm/v1/messages", openapi_extra=LLM_BODY)
async def llm_messages(request: Request, device: Device = Depends(require_device)) -> Any:
    return await _llm_post(request, device, "anthropic", "/messages")


async def _llm_post(request: Request, device: Device, protocol: str, suffix: str) -> Response:
    state = await ready_gate(request)
    org = org_of(state, device)
    raw = await _read_limited(request, LLM_BODY_LIMIT)
    try:
        doc = json.loads(raw)
        model = doc.get("model")
    except Exception as bad:  # noqa: BLE001
        raise GateError(400, "invalid_request", "тело не JSON-объект с полем model") from bad
    route = pick_route(state, device, org, model, protocol)
    check_data_class(state, device, request, route.id)
    check_quota(state, device, org)
    body = with_usage_option(raw, doc) if protocol == "openai" else raw
    return await proxy(request, state, device, org, route, str(model), suffix, body, doc.get("stream") is True)


@router.get("/harness/llm/v1/models")
async def llm_models(request: Request, device: Device = Depends(require_device)) -> Any:
    """Модели включённых gateway-маршрутов из профиля (формат OpenAI)."""
    state = await ready_gate(request)
    org = org_of(state, device)
    data = []
    for m in (state.bodies.get(device.org) or {}).get("models", []):
        route = org.route(m["route_id"])
        if route is not None and route.kind == "gateway" and m.get("enabled", True):
            data.append({"id": m["model"], "object": "model", "owned_by": route.id})
    return {"object": "list", "data": data}


# --- админка ----------------------------------------------------------------------------------


def _public_device(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if k not in ("device_secret", "litellm_key")}


@router.get("/harness/admin/enrollments", dependencies=admin)
async def admin_enrollments(request: Request, status: str | None = None) -> Any:
    state = gate(request)
    now = time.time()
    out = []
    for e in state.store.enrollments(status):
        if e["status"] == "pending" and e["expires_at"] < now:
            e["status"] = "expired"
            if status == "pending":
                continue
        out.append({k: v for k, v in e.items() if k != "device_code"})
    # Архивные организации при одобрении не предлагаются (#7).
    return {"enrollments": out, "orgs": sorted(o for o, cfg in state.orgs.items() if not cfg.archived)}


@router.get("/harness/admin/devices", dependencies=admin)
async def admin_devices(request: Request) -> Any:
    return {"devices": [_public_device(d) for d in gate(request).store.devices()]}


@router.get("/harness/admin/nodes", dependencies=admin)
async def admin_nodes(request: Request) -> Any:
    state = gate(request)
    await state.nodes.refresh()
    return {"nodes": [{"node_id": n.node_id, "enabled": n.enabled, "orgs": n.orgs,
                        "routes": n.routes, "account_group": n.account_group,
                        "transport": n.transport, "revoked": state.nodes.revoked(n.node_id),
                        "account_turn_capacity": n.account_turn_capacity,
                        "cooldown_seconds": max(0, state.nodes.cooldown.get(n.account_group, 0) - time.monotonic()),
                        "credential": state.store._one("SELECT expires,revoked FROM node_credentials WHERE node_id=?", (n.node_id,)),
                        "status": state.nodes.snapshots.get(n.node_id),
                          "bindings": state.store._all("SELECT id,device_id,route,status FROM router_bindings WHERE node_id=? AND status IN ('ready','creating','unknown','closing')", (n.node_id,))}
                       for n in state.nodes.config.values()]}


@router.post("/harness/admin/nodes/{node_id}/drain", dependencies=admin)
async def admin_node_drain(node_id: str, request: Request) -> Any:
    from .agent import _response
    body = await request.json()
    if not isinstance(body.get("enabled"), bool):
        raise GateError(422, "invalid_request", "enabled должен быть boolean")
    return _response(await gate(request).nodes.request(node_id, "POST", "/v1/node/drain",
        content=json.dumps({"enabled": body["enabled"]}).encode(), request_id="drain:" + str(uuid.uuid4())))


@router.post("/harness/admin/reload", dependencies=admin, response_model=GateReloaded)
async def admin_reload(request: Request) -> Any:
    result = await gate(request).reload()
    if result["errors"]:
        return error_response(422, "profile_invalid", "; ".join(f"{k}: {v}" for k, v in result["errors"].items()),
                              **result)
    return result


async def loopback_socket(request: Request) -> None:
    """Страница админки гейта — только с машины роутера (по адресу сокета, а не ATLAS_TRUST_LOCAL).

    В проде доверие к loopback выключено (#2), и `local_only` роутера закрыл бы страницу совсем.
    Сама страница секретов не несёт: её запросы идут под X-Atlas-Token, который вводит администратор.
    Через обратный прокси на этой же машине запрос тоже приходит с loopback — поэтому Caddyfile
    закрывает /harness/admin* снаружи.
    """
    host = (request.client.host if request.client else "") or ""
    if host.removeprefix("::ffff:") not in ("127.0.0.1", "::1"):
        raise GateError(403, "forbidden", "админка гейта доступна только с машины роутера")


@router.get("/harness/admin/", dependencies=[Depends(loopback_socket)], include_in_schema=False)
async def admin_page() -> Any:
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        raise GateError(404, "not_found", "админка гейта не собрана (ui/src/gate)")
    # Страница ссылается на ассеты с хешем в имени: её саму браузер обязан перепроверять, иначе после
    # выкатки он показывает прежнюю админку из кэша (#12, найдено живым прогоном).
    return FileResponse(index, headers={"Cache-Control": "no-cache"})


@router.get("/harness/admin/assets/{path:path}", dependencies=[Depends(loopback_socket)], include_in_schema=False)
async def admin_asset(path: str) -> Any:
    root = (STATIC_DIR / "assets").resolve()
    target = (root / path).resolve()
    if target != root and root in target.parents and target.is_file():
        return FileResponse(target)
    raise GateError(404, "not_found", "нет файла")
