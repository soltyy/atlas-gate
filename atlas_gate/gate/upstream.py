"""Прямой прокси `/harness/llm/*` к апстримам провайдеров (GW-DIRECT-01, тикет #5).

LiteLLM снят: гейт сам подставляет ключ провайдера, пересылает тело байт-в-байт (единственная правка —
`stream_options.include_usage` у потокового OpenAI-запроса, иначе usage в потоке не придёт), отдаёт
ответ как есть — поток чанк-в-чанк — и попутно снимает usage для учёта и квоты в SQLite гейта.

Ключ провайдера не попадает ни в журнал, ни в ответ: он живёт только в заголовке исходящего запроса.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from .errors import GateError
from .orgs import OrgConfig, Route
from .state import Device, GateState

log = logging.getLogger("atlas_gate")

# Заголовки клиента, которые к провайдеру не идут: транспорт, авторизация к гейту, внутренние метки.
DROP_REQUEST = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "te", "trailer",
                "upgrade", "proxy-authorization", "proxy-authenticate", "authorization", "x-api-key", "cookie",
                "accept-encoding"}
DROP_RESPONSE = {"content-length", "connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade",
                 "content-encoding", "set-cookie"}


def pick_route(state: GateState, device: Device, org: OrgConfig, model: Any, protocol: str) -> Route:
    """Маршрут по модели из профиля. Модель маршрута без ключа — 503 route_down; неизвестная — 403."""
    body = state.bodies.get(device.org) or {}
    for m in body.get("models", []):
        if m.get("model") == model and m.get("enabled", True):
            route = org.route(m["route_id"])
            if route is not None and route.kind == "gateway":
                if route.protocol != protocol:
                    raise GateError(400, "invalid_request",
                                    f"модель {model!r} идёт по протоколу {route.protocol}, а не {protocol}")
                return route
    for m in org.models:
        route = org.route(m.route_id)
        if m.model == model and route is not None and route.kind == "gateway" and not route.enabled:
            raise GateError(403, "policy_denied", f"маршрут {route.id} выключен в профиле организации",
                            reason="route_disabled", route=route.id)
        if m.model == model and route is not None and route.kind == "gateway":
            raise GateError(503, "route_down", f"маршрут {route.id} не поднят: нет ключа {route.key_env}",
                            route=route.id)
    raise GateError(403, "policy_denied", f"модель {model!r} не разрешена профилем организации",
                    reason="model_not_in_profile", route=None)


def with_usage_option(raw: bytes, doc: dict[str, Any]) -> bytes:
    """Потоковый OpenAI-запрос без include_usage — добавить; иначе тело не трогается (байт-в-байт)."""
    if doc.get("stream") is not True:
        return raw
    opts = doc.get("stream_options")
    if isinstance(opts, dict) and opts.get("include_usage") is True:
        return raw
    patched = dict(doc)
    patched["stream_options"] = {**(opts if isinstance(opts, dict) else {}), "include_usage": True}
    return json.dumps(patched, ensure_ascii=False).encode("utf-8")


def openai_usage(u: dict[str, Any] | None) -> dict[str, int] | None:
    if not isinstance(u, dict):
        return None
    details = u.get("prompt_tokens_details") if isinstance(u.get("prompt_tokens_details"), dict) else {}
    cached = int(details.get("cached_tokens") or u.get("prompt_cache_hit_tokens") or 0)
    return {"prompt": int(u.get("prompt_tokens") or 0), "completion": int(u.get("completion_tokens") or 0),
            "cached": cached}


def anthropic_usage(start: dict[str, Any] | None, delta: dict[str, Any] | None) -> dict[str, int] | None:
    """input_tokens — некэшированный вход; к нему добавляются запись и чтение кэша."""
    if not isinstance(start, dict) and not isinstance(delta, dict):
        return None
    s, d = start or {}, delta or {}
    read = int(s.get("cache_read_input_tokens") or d.get("cache_read_input_tokens") or 0)
    write = int(s.get("cache_creation_input_tokens") or d.get("cache_creation_input_tokens") or 0)
    inp = int(s.get("input_tokens") or d.get("input_tokens") or 0)
    out = int(d.get("output_tokens") or s.get("output_tokens") or 0)
    return {"prompt": inp + read + write, "completion": out, "cached": read}


def cost_of(org: OrgConfig, route_id: str, model: str, u: dict[str, int]) -> float | None:
    price = next((p for p in org.pricing if p.route_id == route_id and p.model == model), None)
    if price is None:
        return None
    uncached = max(u["prompt"] - u["cached"], 0)
    return (uncached * price.input_uncached + u["cached"] * price.input_cached
            + u["completion"] * price.output) / 1_000_000


def month_bounds(now: float | None = None) -> tuple[float, float, str]:
    """Начало текущего и следующего календарного месяца UTC (секунды) и ISO сброса."""
    import datetime as dt

    t = dt.datetime.fromtimestamp(now if now is not None else time.time(), tz=dt.timezone.utc)
    start = t.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = (start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1))
    return start.timestamp(), nxt.timestamp(), nxt.isoformat()


def check_quota(state: GateState, device: Device, org: OrgConfig) -> None:
    if org.quota.limit is None:
        return
    since, until, _ = month_bounds()
    used = state.store.spend(device.user_id, since, until)
    if used >= org.quota.limit:
        raise GateError(402, "quota_exceeded", "месячный лимит пользователя исчерпан",
                        limit=org.quota.limit, used=round(used, 6), currency=org.quota.currency)


class UsageTap:
    """Снимает usage с потока SSE, не задерживая байты клиенту."""

    def __init__(self, protocol: str) -> None:
        self.protocol = protocol
        self._buf = b""
        self.usage: dict[str, int] | None = None
        self._start: dict[str, Any] | None = None
        self._delta: dict[str, Any] | None = None

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == b"[DONE]":
                continue
            try:
                ev = json.loads(payload)
            except ValueError:
                continue
            if self.protocol == "openai":
                u = openai_usage(ev.get("usage"))
                if u is not None:
                    self.usage = u
            elif ev.get("type") == "message_start":
                self._start = (ev.get("message") or {}).get("usage")
                self.usage = anthropic_usage(self._start, self._delta)
            elif ev.get("type") == "message_delta":
                self._delta = ev.get("usage")
                self.usage = anthropic_usage(self._start, self._delta)


async def proxy(request: Request, state: GateState, device: Device, org: OrgConfig, route: Route,
                model: str, suffix: str, body: bytes, stream: bool) -> Response:
    key = state.key_for(route)
    if key is None:
        raise GateError(503, "route_down", f"маршрут {route.id} не поднят: нет ключа {route.key_env}", route=route.id)
    headers = [(k, v) for k, v in request.headers.items()
               if k.lower() not in DROP_REQUEST and not k.lower().startswith("x-harness-")]
    if route.protocol == "anthropic":
        headers.append(("x-api-key", key))
        if not request.headers.get("anthropic-version"):
            headers.append(("anthropic-version", state.settings.ATLAS_GATE_ANTHROPIC_VERSION))
    else:
        headers.append(("authorization", f"Bearer {key}"))
    headers.append(("accept-encoding", "identity"))  # usage снимается с потока — без сжатия
    url = route.upstream() + suffix
    timeout = httpx.Timeout(route.timeout_s, connect=min(route.timeout_s, 10.0))
    upstream_req = state.http.build_request("POST", url, headers=headers, content=body, timeout=timeout)
    try:
        resp = await state.http.send(upstream_req, stream=True)
    except httpx.TimeoutException as failed:
        raise GateError(504, "upstream_timeout", f"апстрим {route.id} не ответил за {route.timeout_s:g} с",
                        route=route.id) from failed
    except httpx.HTTPError as failed:
        raise GateError(502, "upstream_error", f"апстрим {route.id} недоступен: {type(failed).__name__}",
                        route=route.id) from failed
    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in DROP_RESPONSE}
    session = request.headers.get("x-harness-session")
    model_call = request.headers.get("x-harness-model-call")

    def record(u: dict[str, int] | None) -> None:
        if u is None:
            return
        cost = cost_of(org, route.id, model, u)
        if cost is None:
            state.warn_unpriced_once(route.id, model)
        state.store.usage_add(org=org.id, user_id=device.user_id, device_id=device.device_id, kind="gateway",
                              model=model, prompt_tokens=u["prompt"], completion_tokens=u["completion"],
                              cached_tokens=u["cached"], cost=cost, route=route.id, session_id=session,
                              model_call=model_call)

    if resp.status_code >= 400 or not stream:
        # Ошибка апстрима — как есть; нестримовый ответ читается целиком ради usage.
        try:
            raw = await resp.aread()
        except httpx.HTTPError as failed:
            await resp.aclose()
            raise GateError(502, "upstream_error", f"апстрим {route.id} оборвал ответ: {type(failed).__name__}",
                            route=route.id) from failed
        await resp.aclose()
        if resp.status_code < 400:
            try:
                doc = json.loads(raw)
                record(openai_usage(doc.get("usage")) if route.protocol == "openai"
                       else anthropic_usage(doc.get("usage"), None))
            except (ValueError, AttributeError):
                log.warning("ответ апстрима %s без разбираемого usage", route.id)
        return Response(content=raw, status_code=resp.status_code, headers=out_headers)

    tap = UsageTap(route.protocol)

    async def relay() -> AsyncIterator[bytes]:
        try:
            async for chunk in resp.aiter_raw():
                tap.feed(chunk)
                yield chunk
        except httpx.HTTPError as failed:
            # Поток уже начат — код ответа не сменить; обрыв фиксируется в журнале, учёт — по снятому.
            log.warning("поток апстрима %s оборван: %s", route.id, type(failed).__name__)
        finally:
            await resp.aclose()
            record(tap.usage)

    return StreamingResponse(relay(), status_code=resp.status_code, headers=out_headers)
