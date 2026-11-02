"""Прямой прокси `/harness/llm/*` к апстримам провайдеров (GW-DIRECT-01, тикет #5).

LiteLLM снят: гейт сам подставляет ключ провайдера, пересылает тело байт-в-байт (единственная правка —
`stream_options.include_usage` у потокового OpenAI-запроса, иначе usage в потоке не придёт), отдаёт
ответ как есть — поток чанк-в-чанк — и попутно снимает usage для учёта и квоты в SQLite гейта.

Ключ провайдера не попадает ни в журнал, ни в ответ: он живёт только в заголовке исходящего запроса.
"""

from __future__ import annotations

import json
import asyncio
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
from .api_attempts import ApiAttempts, ResponseFacts, opted_in, selected_choice

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


def openai_usage(u: dict[str, Any] | None, strict: bool = False) -> dict[str, int] | None:
    if not isinstance(u, dict):
        return None
    details = u.get('input_tokens_details') or u.get("prompt_tokens_details") or {}
    if not isinstance(details, dict):
        details = {}
    if strict:
        inp = u.get('prompt_tokens', u.get('input_tokens'))
        out = u.get('completion_tokens', u.get('output_tokens'))
        cache = details.get('cached_tokens', u.get('prompt_cache_hit_tokens'))
        if any(type(v) is not int or v < 0 for v in (inp, out, cache)) or cache > inp:
            return None
        if 'cached_tokens' in details and 'prompt_cache_hit_tokens' in u and details['cached_tokens'] != u['prompt_cache_hit_tokens']:
            return None
        return {'prompt': inp, 'completion': out, 'cached': cache}
    cached = int(details.get("cached_tokens") or u.get("prompt_cache_hit_tokens") or 0)
    return {"prompt": int(u.get("prompt_tokens") or u.get('input_tokens') or 0), "completion": int(u.get("completion_tokens") or u.get('output_tokens') or 0),
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

    def __init__(self, protocol: str, strict: bool = False) -> None:
        self.protocol = protocol
        self.strict = strict
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
                u = openai_usage(ev.get("usage") or (ev.get('response') or {}).get('usage'), strict=self.strict)
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
    if opted_in(request.headers):
        return await _managed_proxy(request, state, device, org, route, model, suffix, body, stream)
    return await _legacy_proxy(request, state, device, org, route, model, suffix, body, stream)


def _prepare_request(request, state, device, org, route, model, suffix, body):
    # Capture once before provider IO. Reload/time-window changes cannot reprice an active call.
    from datetime import datetime, timezone
    from ..pricing import selected
    schedule = next((p for p in org.pricing_v1 if (p.route_id, p.model) == (route.id, model)), None)
    price_decision, period = None, None
    if schedule:
        if request.headers.get('x-atlas-pricing-contract') != '1':
            raise GateError(422, 'pricing_contract_required', 'маршрут требует pricing v1; обновите профиль/клиент', deliveryState='not_sent')
        period = schedule.select(datetime.now(timezone.utc))
        if period is None:
            raise GateError(409, 'pricing_expired', 'нет действующего тарифного интервала; обновите прайс', deliveryState='not_sent')
        try:
            wanted = json.loads(request.headers.get('x-atlas-pricing-selection', 'null'))
        except ValueError:
            wanted = None
        if wanted != {'pricing_version': schedule.pricing_version, 'revision': schedule.revision, 'tariff_id': period.tariff_id,
                      'starts_at': period.starts_at.isoformat(), 'ends_at': period.ends_at.isoformat()}:
            raise GateError(409, 'pricing_requote_required', 'тариф изменился до отправки; пересчитайте резерв', deliveryState='not_sent')
        if suffix.startswith('/images/'):
            raise GateError(422, 'pricing_usage_unsupported', 'token pricing не покрывает Images API', deliveryState='not_sent')
        price_decision = selected(schedule, period)
        from .api_reasoning import digest
        request_key = request.headers.get('idempotency-key') or request.headers.get('x-harness-model-call')
        if not request_key or len(request_key) > 500:
            raise GateError(422, 'pricing_request_key_required', 'pricing v1 требует устойчивый model-call ID', deliveryState='not_sent')
        identity = digest([device.org, device.device_id, route.id, model, request_key])
        fingerprint = digest([suffix, json.loads(body), wanted])
        state.store._exec('INSERT OR IGNORE INTO gateway_price_requests VALUES (?,?,?)',
                          (identity, fingerprint, json.dumps(price_decision)))
        pinned = state.store._one('SELECT * FROM gateway_price_requests WHERE id=?', (identity,))
        if pinned['fingerprint'] != fingerprint or json.loads(pinned['decision']) != price_decision:
            raise GateError(409, 'idempotency_conflict', 'принятый model-call изменил тело или pricing snapshot', deliveryState='not_sent')
    key = state.key_for(route)
    if key is None:
        raise GateError(503, "route_down", f"маршрут {route.id} не поднят: нет ключа {route.key_env}", route=route.id)
    headers = [(k, v) for k, v in request.headers.items()
                 if k.lower() not in DROP_REQUEST and not k.lower().startswith("x-harness-")
                 and k.lower() != 'x-atlas-reasoning-scope'
                 and not k.lower().startswith('x-atlas-api-attempt-')
                 and not k.lower().startswith('x-atlas-recovery-')
                 and k.lower() not in ('x-atlas-choice-index', 'x-atlas-conversation-id', 'x-atlas-branch-id',
                                       'x-atlas-generation', 'x-atlas-control-epoch')
                 and not k.lower().startswith('x-atlas-pricing-')]
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
    return upstream_req, price_decision, period, schedule


async def _legacy_proxy(request, state, device, org, route, model, suffix, body, stream):
    upstream_req, price_decision, period, schedule = _prepare_request(
        request, state, device, org, route, model, suffix, body)
    try:
        resp = await state.http.send(upstream_req, stream=True)
    except httpx.TimeoutException as failed:
        raise GateError(504, "upstream_timeout", f"апстрим {route.id} не ответил за {route.timeout_s:g} с",
                        route=route.id) from failed
    except httpx.HTTPError as failed:
        raise GateError(502, "upstream_error", f"апстрим {route.id} недоступен: {type(failed).__name__}",
                        route=route.id) from failed
    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in DROP_RESPONSE}
    if price_decision:
        out_headers['X-Atlas-Pricing-Decision'] = json.dumps(price_decision, separators=(',', ':'), ensure_ascii=True)
    session = request.headers.get("x-harness-session")
    model_call = request.headers.get("x-harness-model-call")

    def record(u: dict[str, int] | None) -> None:
        if u is None:
            return
        # Existing pricing has text token rates only. Do not misprice image tokens.
        image_billing = suffix.startswith('/images/') or (suffix == '/responses' and
            any(t.get('type') == 'image_generation' for t in (json.loads(body).get('tools') or []) if isinstance(t, dict)))
        cost = None if image_billing else (period.rates.cost(u) if period else cost_of(org, route.id, model, u))
        if cost is None:
            state.warn_unpriced_once(route.id, model)
        state.store.usage_add(org=org.id, user_id=device.user_id, device_id=device.device_id, kind="gateway",
                              model=model, prompt_tokens=u["prompt"], completion_tokens=u["completion"],
                              cached_tokens=u["cached"], cost=cost, route=route.id, session_id=session,
                              model_call=model_call, pricing_decision=price_decision)

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
                record(openai_usage(doc.get("usage"), strict=bool(schedule)) if route.protocol == "openai"
                       else anthropic_usage(doc.get("usage"), None))
            except (ValueError, AttributeError):
                log.warning("ответ апстрима %s без разбираемого usage", route.id)
        return Response(content=raw, status_code=resp.status_code, headers=out_headers)

    tap = UsageTap(route.protocol, strict=bool(schedule))

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


async def _managed_proxy(request, state, device, org, route, model, suffix, body, stream):
    """One producer per admitted attempt; disconnection only detaches its consumer."""
    manager: ApiAttempts = state.api_attempts
    # Early API replay uses this exact original input, before mutable defaults,
    # reasoning policy normalization or Chat include_usage injection.
    original = await request.body()
    row, fresh = manager.claim(device, route, model, suffix, original, request.headers,
                               branch=request.headers.get('x-atlas-branch-id'))
    if not fresh:
        return manager.replay(row, device)
    aid = row['id']
    try:
        check_quota(state, device, org)
        upstream_req, price_decision, period, schedule = _prepare_request(
            request, state, device, org, route, model, suffix, body)
        # A shared provider account must never deduplicate two different owners'
        # identical client keys. This does not assert provider retry guarantees.
        upstream_req.headers['Idempotency-Key'] = 'atlas-gate-' + aid
    except GateError as failed:
        raw = json.dumps({'error': {'code': failed.code, 'message': failed.message, **failed.extra}},
                         ensure_ascii=False).encode()
        manager.response_headers(aid, failed.status, {'content-type': 'application/json'})
        manager.append(aid, raw)
        manager.finish(aid, {'outcome': 'rejected-not-sent', 'terminalKnown': True,
                            'usageStatus': 'unknown', 'error': {'code': failed.code, 'category': 'admission'},
                            'retry': {'allowed': False, 'scope': 'new_intent_after_condition'}},
                       state='rejected_not_sent')
        return manager.replay(manager.owned(aid, device), device)

    ready = asyncio.Event()
    entry = next((m for m in org.models if m.route_id == route.id and m.model == model), None)
    # Compatibility alone does not attest provider admission-rejection semantics.
    dialect = getattr(entry, 'reasoningDialect', None) or 'compatible'
    facts = ResponseFacts(suffix, dialect, selected_choice(request.headers))
    static_price = next((p.model_dump() for p in org.pricing if p.route_id == route.id and p.model == model), None)

    def normalized_usage():
        if not facts.usage_complete:
            return None
        try:
            raw_usage = facts.usage or {}
            required = ('input_tokens', 'output_tokens') if suffix == '/messages' else (
                'prompt_tokens' if 'prompt_tokens' in raw_usage else 'input_tokens',
                'completion_tokens' if 'completion_tokens' in raw_usage else 'output_tokens')
            if any(type(raw_usage.get(k)) is not int or raw_usage[k] < 0 for k in required):
                return None
            if suffix == '/messages' and any(type(v) is not int or v < 0 for k, v in raw_usage.items()
                    if k in ('cache_read_input_tokens', 'cache_creation_input_tokens')):
                return None
            usage = (anthropic_usage(facts.usage, None) if suffix == '/messages'
                     else openai_usage(facts.usage, strict=True))
            if usage is None or any(type(v) is not int or v < 0 for v in usage.values()) or usage['cached'] > usage['prompt']:
                return None
            return usage
        except (ValueError, TypeError, AttributeError, OverflowError):
            return None

    async def produce():
        resp = None
        failure = None
        headers = {}
        admission_rejected = False
        try:
            # Fence and dispatch marker share one short SQLite transaction.
            # No await or upstream IO occurs while that transaction is open.
            if not manager.dispatched(aid, request=request, device=device, route=route):
                admission_rejected = True  # Reconcile sealed a queued attempt; preserve its receipt.
                return
            resp = await state.http.send(upstream_req, stream=True)
            headers = {k: v for k, v in resp.headers.items() if k.lower() not in DROP_RESPONSE
                       and not k.lower().startswith('x-atlas-api-attempt-')}
            if price_decision:
                headers['X-Atlas-Pricing-Decision'] = json.dumps(price_decision, separators=(',', ':'), ensure_ascii=True)
            manager.response_headers(aid, resp.status_code, headers)
            ready.set()
            raw = []
            def capture(chunk):
                manager.append(aid, chunk)  # No unsaved bytes reach the consumer.
                if stream and resp.status_code < 400:
                    facts.feed(chunk)
                else:
                    raw.append(chunk)
            if resp.is_stream_consumed:
                capture(resp.content)
            else:
                async for chunk in resp.aiter_raw():
                    capture(chunk)
            if not stream or resp.status_code >= 400:
                facts.nonstream(b''.join(raw))
        except GateError as failed:
            admission_rejected = True
            try:
                raw = json.dumps({'error': {'code': failed.code, 'message': failed.message, **failed.extra}},
                                 ensure_ascii=False).encode()
                manager.response_headers(aid, failed.status, {'content-type': 'application/json'})
                manager.append(aid, raw)
                manager.finish(aid, {'outcome': 'rejected-not-sent', 'terminalKnown': True, 'deliveryState': 'not_sent',
                                    'usageStatus': 'unknown', 'error': {'code': failed.code, 'category': 'admission'},
                                    'retry': {'allowed': False, 'scope': 'new_intent_after_condition'}},
                               state='rejected_not_sent')
            except Exception:
                manager.producer_failed(aid, 'admission_receipt_persistence_error')
        except asyncio.CancelledError:
            failure = 'producer_interrupted'
        except httpx.HTTPError as failed:
            failure = 'upstream_timeout' if isinstance(failed, httpx.TimeoutException) else 'upstream_transport_error'
        except Exception as failed:
            # A failed persistence operation is not permission to replay inference.
            failure = 'output_persistence_error'
            log.error('сохранение API attempt %s не завершено: %s', aid, type(failed).__name__)
        finally:
            try:
                if admission_rejected:
                    return
                if resp is not None:
                    await resp.aclose()
                current = manager.owned(aid, device)
                if current['status'] is None:
                    status = 504 if failure == 'upstream_timeout' else 502
                    manager.response_headers(aid, status, {'content-type': 'application/json'})
                    manager.append(aid, json.dumps({'error': {'code': failure or 'upstream_unknown',
                                   'message': 'исход API attempt неизвестен', 'attemptId': aid,
                                   'deliveryState': 'unknown'}}).encode())
                    current = manager.owned(aid, device)
                usage = normalized_usage()
                summary = facts.summary(current['status'], headers, usage)
                if failure:
                    summary['error'] = {'code': failure, 'category': 'transport_or_persistence'}
                    summary['retry'] = {'allowed': False, 'scope': 'reconcile', 'owner': 'agent_loop'}
                    # A durable native terminal is still valid if only the usage
                    # tail or connection close failed afterwards.
                    if not facts.terminal:
                        summary.update(outcome='partial' if current['status'] == 200 else 'unknown',
                                       terminalKnown=False, runState='waiting-recovery')
                usage_row = None
                cost = None
                if usage is not None:
                    image_billing = suffix.startswith('/images/') or (suffix == '/responses' and
                        any(t.get('type') == 'image_generation' for t in (json.loads(body).get('tools') or []) if isinstance(t, dict)))
                    cache_write = suffix == '/messages' and bool((facts.usage or {}).get('cache_creation_input_tokens'))
                    if not image_billing and not cache_write:
                        if period:
                            cost = period.rates.cost(usage)
                        elif static_price:
                            cost = ((usage['prompt'] - usage['cached']) * static_price['input_uncached']
                                    + usage['cached'] * static_price['input_cached']
                                    + usage['completion'] * static_price['output']) / 1_000_000
                    usage_row = dict(org=org.id, user_id=device.user_id, device_id=device.device_id, kind='gateway',
                                     model=model, prompt_tokens=usage['prompt'], completion_tokens=usage['completion'],
                                     cached_tokens=usage['cached'], cost=cost, route=route.id,
                                     session_id=request.headers.get('x-harness-session'), model_call=aid)
                summary.update(cost=cost, costKind='estimate', costStatus='estimate' if cost is not None else 'unknown',
                               costCoverage='model_tokens_only', pricingDecision=price_decision, staticPrice=static_price)
                terminal_state = 'complete' if summary['terminalKnown'] or summary['outcome'] == 'accepted-running' else 'outcome_unknown'
                manager.finish(aid, summary, state=terminal_state, usage_row=usage_row, pricing_decision=price_decision)
            except Exception as failed:
                log.error('квитанция API attempt %s недоступна: %s', aid, type(failed).__name__)
                manager.producer_failed(aid, 'receipt_persistence_error')
            finally:
                ready.set()

    producer = manager.start(produce())
    await ready.wait()
    current = manager.owned(aid, device)
    if not stream or current['status'] is None or current['status'] >= 400:
        await asyncio.shield(producer)
        return manager.replay(manager.owned(aid, device), device)
    return StreamingResponse(manager.stream(aid), status_code=current['status'], headers=manager.headers(current))
