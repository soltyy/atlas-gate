"""Сетевой агентный Gate: публичные IDs, владение, async producer и polling/SSE."""
from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote
from fastapi import Depends, Request
from fastapi.responses import Response, JSONResponse
from sse_starlette.sse import EventSourceResponse
from ..schemas import CompactRequest, DocumentUploadRequest, PromptRequest, SessionCreated, SessionList, SteerRequest, ToolResultRequest, ToolsUpdate
from .api import check_data_class, gate, org_of, ready_gate, require_device, router
from .errors import GateError, GateFreshNodeError, STATUS_CODES
from .schemas import GateAgentSessionCreate, PromptAdmissionResolve
from ..sdk_tools import SdkTools, permitted
from .state import Device
from .attachments import node_attachments, validate_prompt, validate_tool_result
from .reasoning import model_policy, validate_new_prompt
from .operations import body_sha256, tool_result_not_sent
from .token_usage import accountable_terminal

log = logging.getLogger("atlas_gate")


def _response(response):
    raw = response.content
    headers = {"Retry-After": response.headers["Retry-After"]} if "Retry-After" in response.headers else {}
    if response.status_code >= 400:
        try:
            doc = response.json()
        except ValueError:
            doc = {}
        if not isinstance(doc, dict) or "error" not in doc:
            detail = doc.get("detail", "Router отказал") if isinstance(doc, dict) else doc
            if isinstance(detail, dict) and isinstance(detail.get('error'), dict):
                return Response(content=json.dumps({'error':detail['error']}, ensure_ascii=False).encode(),
                                status_code=response.status_code, media_type='application/json', headers=headers)
            code = STATUS_CODES.get(response.status_code, "agent_error")
            if str(detail).startswith("session_suspended"):
                code = "session_suspended"
            elif str(detail) == "router_identity_mismatch":
                code = "router_identity_mismatch"
            elif str(detail).startswith('resume_unavailable'):
                code = 'sdk_session_recovery_unavailable'
            elif isinstance(detail, str) and detail.startswith('adaptive_file'):
                code = detail
            raw = json.dumps({"error": {"code": code, "message": str(detail)}}, ensure_ascii=False).encode()
    return Response(content=raw, status_code=response.status_code, media_type="application/json", headers=headers)


def _selection(state, device, model=None, route_id=None):
    org = org_of(state, device)
    allowed = {r.id for r in org.routes if r.kind == "router-agent" and r.enabled and (route_id is None or r.id == route_id)}
    models = (state.bodies.get(device.org) or {}).get("models", [])
    selected = next((m for m in models if m.get("route_id") in allowed and m.get("enabled", True) and (not model or m["model"] == model)), None)
    if selected is None:
        raise GateError(403, "policy_denied", "маршрут/модель не разрешены профилем")
    return str(selected["route_id"]), str(selected["model"])


def _policy(state, device, binding, request, require_model=True, require_sdk=True):
    if require_model:
        _selection(state, device, binding["model"], binding["route"])
        for role in json.loads(binding["roles"]):
            if role.get("model") not in (None, "", "inherit"):
                _selection(state, device, role["model"], binding["route"])
    accepted = json.loads(binding.get('response') or '{}').get('sdkTools') or {}
    if require_sdk and not permitted(SdkTools.model_validate(accepted), org_of(state, device).sdk_tools):
        raise GateError(403, 'policy_denied', 'SDK tools больше не разрешены организацией')
    check_data_class(state, device, request, binding["route"])
    node = state.nodes.config.get(binding["node_id"])
    if node is None or not node.enabled or device.org not in node.orgs or binding["route"] not in node.routes:
        raise GateError(403, "policy_denied", "узел больше не разрешён организации")


def _path(binding, suffix=""):
    return "/v1/sessions/" + quote(binding["local_id"], safe="") + suffix


async def _events(state, binding, turn, device, after=0, wait=0):
    binding = await state.routing.recover_boot(binding)
    context_order = time.time_ns()
    response = await state.nodes.request(binding["node_id"], "GET", _path(binding, f"/turns/{quote(turn, safe='')}/events"), params={"after": after, "wait": wait}, expected_boot=binding["boot_id"])
    if response.status_code == 410:
        receipt = await state.nodes.request(binding["node_id"], "GET", _path(binding, f"/turns/{quote(turn, safe='')}/receipt"), expected_boot=binding["boot_id"])
        if receipt.status_code == 200:
            terminal = receipt.json()
            if accountable_terminal(terminal):
                state.routing.usage(binding, turn, terminal, device)
            if terminal.get("type") == "result":
                op = state.store._one('SELECT created FROM router_operations WHERE session_id=? AND turn_id=?',(binding['id'],turn))
                if state.context_windows.observe(binding,terminal.get('context'),context_order,(op or {}).get('created',0)):
                    await state.publish_contexts()
            state.store._exec("UPDATE router_operations SET finished=1,accounted=1 WHERE session_id=? AND turn_id=?", (binding["id"], turn))
    if response.status_code >= 400:
        return response, None
    doc = response.json()
    for event in doc.get('events', []):
        state.operations.absorbed(binding, event)
    doc["events"] = [state.routing.translate(binding, ev) for ev in doc.get("events", [])]
    from .reasoning import confirm
    for event in doc['events']:
        if isinstance(event.get('configuredReasoning'), dict):
            try:
                confirm(json.loads(binding.get('reasoning_snapshot') or '{}'), event)
            except ValueError as failed:
                raise GateError(502, 'reasoning_confirmation_missing', str(failed)) from failed
    for ev in doc["events"]:
        state.nodes.observe_event(binding["node_id"], ev)
        if accountable_terminal(ev):
            state.routing.usage(binding, turn, ev, device)
    if doc.get("done"):
        state.store._exec("UPDATE router_operations SET finished=1,accounted=1 WHERE session_id=? AND turn_id=?", (binding["id"], turn))
    for event in doc.get('events',[]):
        if event.get('type') == 'result':
            op = state.store._one('SELECT created FROM router_operations WHERE session_id=? AND turn_id=?',(binding['id'],turn))
            if state.context_windows.observe(binding,event.get('context'),context_order,(op or {}).get('created',0)):
                await state.publish_contexts()
    return response, doc


async def _follow_event_page(state, binding, turn, device, after, wait=1000, *, retry_window=None):
    """Retry the same journal read. JSON polling bounds recovery within one request."""
    delay = 0.25
    loop = asyncio.get_running_loop()
    deadline = None
    failure = None
    while True:
        try:
            async with asyncio.timeout_at(deadline):
                response, doc = await _events(state, binding, turn, device, after, wait)
        except TimeoutError:
            if failure is None:
                raise
            break
        except GateError as failed:
            if failed.code != "node_unavailable" or (
                    isinstance(failed, GateFreshNodeError) and not failed.retryable):
                raise
            failure = failed
            reason = failed.code
        else:
            if response.status_code not in (502, 503, 504):
                return response, doc
            if retry_window is not None and "Retry-After" in response.headers:
                return response, doc
            failure = (response, doc)
            reason = f"HTTP {response.status_code}"
        if delay == 0.25:
            log.warning("Чтение событий Router прервано: node=%s turn=%s after=%s reason=%s",
                        binding["node_id"], turn, after, reason)
        if retry_window is not None:
            if deadline is None:
                deadline = loop.time() + retry_window
            if loop.time() + delay >= deadline:
                break
            # After a failed long poll, fetch the saved page immediately when the link returns.
            wait = 0
        await asyncio.sleep(delay)
        delay = min(delay * 2, 5)
    if isinstance(failure, GateError):
        raise failure
    return failure


async def _watch(state, binding, turn, device):
    after = 0
    try:
        for _ in range(3600):
            _, doc = await _follow_event_page(state, binding, turn, device, after)
            if doc is None:
                return
            after = doc.get("nextAfter", after)
            if doc.get("done"):
                return
            await asyncio.sleep(0.01)
    except GateError as failed:
        log.warning("usage watcher %s остановлен: %s", turn, failed.code)


@router.get("/harness/agent/sessions", response_model=SessionList, response_model_exclude_unset=True)
async def agent_sessions(request: Request, device: Device = Depends(require_device)):
    state, result, cache = gate(request), [], {}
    for binding in state.routing.list(device):
        node = binding["node_id"]
        if node not in cache:
            try:
                response = await state.nodes.request(node, "GET", "/v1/sessions")
                cache[node] = response.json().get("sessions", []) if response.status_code == 200 else []
            except GateError:
                cache[node] = []
        info = next((s for s in cache[node] if s["id"] == binding["local_id"]), None)
        if info is not None:
            if info.get("sdkSessionId"):
                state.store._exec("UPDATE router_bindings SET sdk_id=? WHERE id=?", (info["sdkSessionId"], binding["id"]))
            result.append(dict(info, id=binding["id"], sdkSessionId=binding["sdk_alias"] if info.get("sdkSessionId") else None))
        else:
            result.append({"id": binding["id"], "model": binding["model"], "state": "session_suspended", "sdkSessionId": binding["sdk_alias"] if binding["sdk_id"] else None})
    return {"sessions": result}


@router.post("/harness/agent/sessions", response_model=SessionCreated,
             description="Create ACK confirms resume only when resumed is explicitly true. "
                         "sdkSessionId is returned by session listing/events, not by creation.")
async def agent_session_create(body: GateAgentSessionCreate, request: Request, device: Device = Depends(require_device)):
    state = await ready_gate(request)
    if body.sdkTools is not None and not permitted(body.sdkTools, org_of(state, device).sdk_tools):
        raise GateError(403, 'policy_denied', 'SDK tools не разрешены организацией')
    route, model = _selection(state, device, body.model)
    for role in body.agents:
        if role.model and role.model != "inherit":
            _selection(state, device, role.model, route)
    check_data_class(state, device, request, route)
    status, raw = await state.routing.create(device, route, model, body.model_dump(exclude_none=True), state.settings.ATLAS_GATE_AGENT_SESSIONS_PER_DEVICE, request.headers.get("idempotency-key"), sdk_policy=org_of(state, device).sdk_tools,
        reasoning_policy=model_policy(org_of(state, device), route, model))
    import httpx
    response = _response(httpx.Response(status, content=raw))
    response.headers.update(state.routing.creation_headers(
        device, request.headers.get("idempotency-key"), status, raw))
    # Keep the exact persisted success/rejection bytes and headers on replay.
    # Response intentionally bypasses response_model serialization/default injection.
    return response


@router.post("/harness/agent/sessions/{sid}/prompt")
async def agent_prompt(sid: str, body: PromptRequest, request: Request, device: Device = Depends(require_device)):
    state = gate(request)
    binding = state.routing.get(sid, device)
    _policy(state, device, binding, request)
    binding = await state.routing.recover_boot(binding)
    mode = request.query_params.get("mode", "sse")
    if mode not in ("", "sse", "async"):
        raise GateError(422, "invalid_request", "неизвестный mode")
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    await state.nodes.refresh()
    validate_prompt(body, node_attachments(state.nodes.snapshots.get(binding['node_id'])))
    def validate_admission():
        _policy(state, device, binding, request)
        if body.adaptiveFiles and not _adaptive_offer(binding):
            raise GateError(409, 'adaptive_files_not_negotiated', 'Ход не согласовал адаптивные файлы')
        if body.adaptiveFiles and body.adaptiveFiles.bootId != binding['boot_id']:
            raise GateError(409, 'adaptive_files_reconfirmation_required', 'Привязка Router изменилась')
        if body.adaptiveFiles and not state.routing.adaptive_transport(binding['node_id']):
            raise GateError(409, 'adaptive_files_not_negotiated', 'Connector не подтвердил адаптивные файлы')
        validate_new_prompt(state, binding, device)
        def integer(name):
            value = request.headers.get(name)
            try:
                return None if value is None else int(value)
            except ValueError as error:
                raise GateError(422, 'recovery_fence_invalid', 'неверный recovery fence') from error
        state.recovery.check_admission(device, binding,
            conversation_id=request.headers.get('x-atlas-conversation-id'),
            branch_id=request.headers.get('x-atlas-branch-id'),
            inventory_id=request.headers.get('x-atlas-recovery-inventory'),
            generation=integer('x-atlas-recovery-generation'),
            control_epoch=integer('x-atlas-control-epoch'), db=state.store._db)
    operation, fresh = state.operations.begin(device, binding, "POST", _path(binding, "/prompt"), body.model_dump(),
        request.headers.get("idempotency-key"), body.id, day, org_of(state, device).quota.agent_turns_per_day, state.nodes,
        validate_new=validate_admission,
        body_digest=body_sha256(await request.json()))
    response = await _deliver_operation(state, binding, operation, fresh,
                                       body.model_dump_json().encode(), {"mode": "async"})
    if response.status_code >= 400:
        return _operation_response(response, operation)
    task = asyncio.create_task(_watch(state, binding, body.id, device))
    state.watchers.add(task)
    task.add_done_callback(state.watchers.discard)
    if mode == "async":
        return _operation_response(response, operation)

    async def stream():
        after = 0
        try:
            while True:
                _, doc = await _follow_event_page(state, binding, body.id, device, after)
                if doc is None:
                    yield {"data": json.dumps({"type": "error", "id": body.id, "kind": "session_suspended", "message": "события Router недоступны"})}
                    return
                for ev in doc["events"]:
                    yield {"data": json.dumps({k: v for k, v in ev.items() if k != "seq"}, ensure_ascii=False)}
                after = doc["nextAfter"]
                if doc["done"]:
                    return
        except GateError as failed:
            yield {"data": json.dumps({"type": "error", "id": body.id, "kind": failed.code, "message": failed.message})}
    return EventSourceResponse(stream())


async def _bound_attachments(state, binding):
    response = await state.nodes.request(binding['node_id'], 'GET', _path(binding, '/attachments'), expected_boot=binding['boot_id'])
    if response.status_code == 200:
        try:
            caps = node_attachments({'attachments': response.json().get('capabilities')})
        except (ValueError, AttributeError):
            caps = None
        if caps is not None:
            return caps
    # Older Router has no negotiated endpoint. Keep its other known flags, never
    # promote node-wide support into a per-session tool-result acknowledgement.
    caps = node_attachments(state.nodes.snapshots.get(binding['node_id']))
    return dict(caps, toolResultImages=False) if caps is not None else None


async def _control_receipt(state, op):
    current = state.store._one("SELECT * FROM router_operations WHERE id=?", (op["id"],))
    pending_steer = current.get('steer') and json.loads(current['steer'])['state'] in ('accepted', 'unknown')
    if tool_result_not_sent(current):
        # Project an old local refusal as pending while looking for Router's receipt.
        current["state"] = "unknown"
    if current["state"] != "complete" or pending_steer:
        current["receiptState"] = "unavailable"
        try:
            response = await state.nodes.request(op["node_id"], "GET", "/v1/commands/" + op["id"])
            if response.status_code == 200:
                from .receipts import completed_response
                saved = response.json()
                current['receiptState'] = saved.get('state', 'unknown') if isinstance(saved, dict) else 'malformed'
                if current.get('steer') and isinstance(saved, dict) and saved.get('steer') is not None:
                    state.operations.observe_steer(op['id'], saved['steer'])
                confirmed = completed_response(saved, op['id'])
                if confirmed is not None:
                    state.operations.complete(op['id'], confirmed, steer_receipt=saved.get('steer'))
                    current = state.store._one('SELECT * FROM router_operations WHERE id=?', (op['id'],))
                elif current['receiptState'] == 'complete':
                    current['receiptState'] = 'malformed'
            elif response.status_code == 404:
                current['receiptState'] = 'missing'
        except (GateError, ValueError, KeyError, TypeError):
            pass
    return {**current, **(state.store._one('SELECT steer FROM router_operations WHERE id=?', (op['id'],)) or {})}


def _pending_control(op):
    body = {"commandId": op["id"], "state": "pending",
                         "sessionId": op["session_id"],
                         "receiptState": op.get('receiptState', 'unconfirmed')}
    if op.get('steer'):
        from .steering import public
        body['steer'] = public(json.loads(op['steer']), op['session_id'])
    return JSONResponse(body, status_code=202,
                        headers={"Retry-After": "1"})


async def _execute_control(state, binding, op, raw):
    try:
        response = await state.nodes.request(binding["node_id"], "POST", op["path"],
                    content=raw, expected_boot=binding["boot_id"], request_id=op["id"])
    except Exception:
        state.operations.unknown(op["id"])
    else:
        if response.status_code >= 500:
            # A connector can lose its own HTTP reply while Router's durable
            # command is still running. Its receipt is authoritative.
            state.operations.unknown(op["id"])
            await _control_receipt(state, op)
        else:
            state.operations.complete(op["id"], response)


async def _compact_control(request, device, binding, raw, request_id):
    state = gate(request)
    await state.nodes.refresh()
    op, fresh = state.operations.begin(device, binding, "POST", _path(binding, "/compact"),
                                      json.loads(raw) if raw else {}, request_id, nodes=state.nodes)
    if fresh:
        task = asyncio.create_task(_execute_control(state, binding, op, raw))
        state.watchers.add(task)
        task.add_done_callback(state.watchers.discard)
    if "respond-async" in request.headers.get("prefer", "").lower():
        return _pending_control(op)
    # Legacy callers still receive the final response. A lost internal HTTP response
    # starts receipt observation, never another inference or a failover.
    while True:
        current = await _control_receipt(state, op)
        if current["state"] == "complete":
            import httpx
            return _response(httpx.Response(current["status"], content=current["response"].encode()))
        if current["state"] == "unknown" and current.get("receiptState") in ("unavailable", "unknown"):
            raise GateError(503, "command_outcome_unknown", "итог сжатия пока неизвестен; сессия сохранена", command_id=op["id"])
        if await request.is_disconnected():
            return _pending_control(op)  # The tracked command remains alive.
        await asyncio.sleep(0.2)


@router.get("/harness/agent/sessions/{sid}/commands/{command_id}")
async def control_receipt(sid: str, command_id: str, request: Request,
                          device: Device = Depends(require_device)):
    return await _read_command_receipt(sid, command_id, request, device)


async def _read_command_receipt(sid, command_id, request, device, *, request_key=None):
    state = gate(request)
    binding = state.store._one("SELECT * FROM router_bindings WHERE id=? AND device_id=? AND org=?", (sid, device.device_id, device.org))
    if binding is None:
        raise GateError(404, "not_found", "сессия не найдена")
    _policy(state, device, binding, request, require_model=False, require_sdk=False)
    if state.nodes.revoked(binding['node_id']):
        raise GateError(503, 'node_unavailable', 'Router отозван')
    op = state.store._one("SELECT * FROM router_operations WHERE id=? AND device_id=? AND session_id=?",
                         (command_id, device.device_id, sid))
    if op is None:
        sealed = state.store._one('SELECT response FROM router_admission_fences WHERE id=? AND device_id=? AND org=? AND session_id=?',
                                 (command_id, device.device_id, device.org, sid))
        if sealed:
            return _admission_negative(json.loads(sealed['response']))
        raise GateError(404, "not_found", "команда не найдена")
    from .steering import CONTRACT_HEADER, TARGET_HEADER
    if request.headers.get(CONTRACT_HEADER) is not None or request.headers.get(TARGET_HEADER) is not None:
        expected = json.loads(op.get('steer') or '{}').get('_targetBoot')
        if (request.headers.get(CONTRACT_HEADER) != '1' or not expected or
                request.headers.get(TARGET_HEADER) != expected):
            raise GateError(409, 'steer_contract_mismatch', 'Сверка требует исходные заголовки intent', state='unknown')
    if request_key and state.operations.closed_prompt_candidate(op, binding):
        proof, current = await _resolve_closed_prompt(
            state, device, binding, request_key, op["turn_id"], op["body_sha256"])
        if proof["outcome"] == "not_sent":
            return _admission_negative(proof)
    else:
        current = await _control_receipt(state, op)
    if binding['status'] == 'closed' and op['path'].endswith('/prompt') and current.get("receiptState") == "missing":
        current["receiptState"] = "unavailable"  # A closed binding cannot accept a transport retry.
    if current["state"] != "complete":
        if current.get('steer'):
            return _pending_control(current)  # Keep durable input evidence even if HTTP receipt is unavailable.
        if current.get("receiptState") in ('unknown', 'expired', 'malformed'):
            raise GateError(503, "command_outcome_unknown", "результат команды не подтверждён; требуется сверка",
                            command_id=op["id"], receiptState=current['receiptState'])
        return _pending_control(current)
    import httpx
    return _operation_response(httpx.Response(current["status"], content=current["response"].encode()), current)


@router.get('/harness/agent/sessions/{sid}/commands')
async def receipt_by_key(sid: str, request: Request, requestId: str,
                         device: Device = Depends(require_device)):
    """Lost ACK: клиент знает исходный Idempotency-Key, но ещё не знает commandId."""
    state = gate(request)
    return await _read_command_receipt(sid, state.operations.key(device, requestId), request, device,
                                       request_key=requestId)


def _admission_negative(proof):
    return JSONResponse({'error': {'code': 'command_not_sent', 'message': 'Исходный intent закрыт до допуска'},
                         'recovery': proof}, status_code=409, headers={
        'X-Atlas-Command-State': 'complete', 'X-Atlas-Command-Id': proof['commandId'],
        'X-Atlas-Command-Outcome': 'not_sent', 'X-Atlas-Recovery-Version': '1',
        'Cache-Control': 'private, no-store'})


async def _closed_prompt_absent(state, binding, op):
    """Router 0.8.24 keeps closed sessions and turn identities across journal purge."""
    node, boot = op["node_id"], op["boot_id"]
    try:
        info = await state.nodes.probe(node)
        if (not info or info.get("bootId") != boot or info.get("routerVersion") != "0.8.24"
                or not {"durable_commands", "durable_events"}.issubset(info.get("capabilities", []))):
            return False
        response = await state.nodes.request(node, "GET", _path(binding, "/reconcile"), expected_boot=boot)
        if response.status_code != 200:
            return False
        source = response.json()
        lifecycle = source.get("sessionRecovery", {})
        if (source.get("id") != binding["local_id"] or source.get("nodeId") != node
                or source.get("bootId") != boot or source.get("sdkSessionId") != binding["sdk_id"]
                or source.get("closed") is not True or source.get("recoverable") is not False
                or lifecycle.get("version") != 1 or lifecycle.get("busy") is not False
                or lifecycle.get("replacementRequired") is not True
                or "pendingTurnId" not in lifecycle or lifecycle["pendingTurnId"] is not None):
            return False
        turn = _path(binding, "/turns/" + quote(op["turn_id"], safe=""))
        checks = (
            ("/v1/commands/" + op["id"], None, "command_not_found"),
            (turn + "/receipt", None, "receipt_not_found"),
            (turn + "/events", {"after": 0, "wait": 0}, f"сессия {binding['local_id']} не найдена"),
        )
        for path, params, missing in checks:
            evidence = await state.nodes.request(node, "GET", path, params=params, expected_boot=boot)
            if evidence.status_code != 404 or evidence.json() != {"detail": missing}:
                return False
        latest = await state.nodes.probe(node)
        return bool(latest and latest.get("bootId") == boot and not state.nodes.revoked(node))
    except (GateError, ValueError, TypeError, AttributeError):
        return False


async def _resolve_closed_prompt(state, device, binding, key, turn_id, digest):
    rid = state.operations.key(device, key)
    async with state.operations.delivery_lock(rid):
        proof = state.operations.resolve_prompt(device, binding, key, turn_id, digest)
        op = state.store._one("SELECT * FROM router_operations WHERE id=?", (rid,))
        if proof["outcome"] == "not_sent" or not op or not state.operations.closed_prompt_candidate(op, binding):
            return proof, op
        current = await _control_receipt(state, op)
        if current.get("receiptState") == "missing" and await _closed_prompt_absent(state, binding, current):
            proof = state.operations.resolve_prompt(device, binding, key, turn_id, digest, closed_journal=True)
        return proof, current


@router.post('/harness/agent/sessions/{sid}/commands/reconcile')
async def reconcile_prompt_admission(sid: str, body: PromptAdmissionResolve, request: Request,
                                     device: Device = Depends(require_device)):
    state = gate(request)
    binding = state.store._one('SELECT * FROM router_bindings WHERE id=? AND device_id=? AND org=?',
                              (sid, device.device_id, device.org))
    if binding is None:
        raise GateError(404, 'not_found', 'Сессия не найдена')
    _policy(state, device, binding, request, require_model=False, require_sdk=False)
    if state.nodes.revoked(binding['node_id']):
        raise GateError(503, 'node_unavailable', 'Router отозван')
    if binding['status'] == 'closed':
        proof, current = await _resolve_closed_prompt(
            state, device, binding, body.requestId, body.turnId, body.bodySha256)
        if proof['outcome'] != 'not_sent' and current and current['state'] == 'complete' and current['status'] == 202:
            try:
                ack = json.loads(current['response'])
            except (TypeError, ValueError):
                ack = None
            if isinstance(ack, dict) and ack.get('turnId') == body.turnId:
                import httpx
                return _operation_response(httpx.Response(202, content=current['response'].encode()), current)
    else:
        proof = state.operations.resolve_prompt(device, binding, body.requestId, body.turnId, body.bodySha256)
    if proof['outcome'] == 'not_sent':
        return _admission_negative(proof)
    return JSONResponse({'recovery': proof}, status_code=202, headers={'Retry-After': '1'})


def _operation_response(response, op):
    result = _response(response)
    if op.get('steer'):
        try:
            doc = json.loads(result.body)
            if isinstance(doc, dict):
                from .steering import public
                doc['steer'] = public(json.loads(op['steer']), op['session_id'])
                result = JSONResponse(doc, status_code=result.status_code,
                                      headers={k: v for k, v in result.headers.items() if k != 'content-length'})
        except (ValueError, TypeError):
            pass
    result.headers['X-Atlas-Command-State'] = 'complete'
    result.headers['X-Atlas-Command-Id'] = op['id']
    return result


def _tool_result_pending(op):
    return GateError(503, "command_outcome_unknown",
                     "доставка команды ещё не подтверждена; требуется сверка той же операции",
                     command_id=op["id"])


async def _deliver_operation(state, binding, op, fresh, raw, params, *, tool_result_turn=None):
    """Сверка ACK и повтор только с доказательством прежнего boot + durable dedup (#50/#60)."""
    import httpx

    async with state.operations.delivery_lock(op["id"]):
        op = state.store._one("SELECT * FROM router_operations WHERE id=?", (op["id"],))
        not_sent = tool_result_not_sent(op)
        fresh = fresh and op["state"] == "prepared"
        if op["state"] == "complete" and not not_sent:
            if op.get('steer'):
                op = await _control_receipt(state, op)
            return httpx.Response(op["status"], content=op["response"].encode())
        try:
            if not fresh:
                receipt = await state.nodes.request(binding["node_id"], "GET", "/v1/commands/" + op["id"])
                if receipt.status_code == 200:
                    from .receipts import completed_response
                    saved = receipt.json()
                    if op.get('steer') and isinstance(saved, dict) and saved.get('steer') is not None:
                        state.operations.observe_steer(op['id'], saved['steer'])
                    response = completed_response(saved, op['id'])
                    if response is not None:
                        state.operations.complete(op["id"], response, steer_receipt=saved.get('steer'))
                        return response
                    raise _tool_result_pending(op)
                if receipt.status_code != 404:
                    raise _tool_result_pending(op)
                if op.get('steer') or (not_sent and tool_result_turn is None):
                    # Absence is not proof of non-admission, even on the same boot.
                    raise _tool_result_pending(op)
                # 404 разрешает повтор только при прежнем boot и durable дедупликации.
                # Для старой записи без boot свидетельств недостаточно: ничего не посылаем.
                info = await state.nodes.probe(binding["node_id"])
                if (not info or not op["boot_id"] or info["bootId"] != op["boot_id"]
                        or binding["boot_id"] != op["boot_id"]
                        or "durable_commands" not in info.get("capabilities", [])):
                    raise _tool_result_pending(op)
                if tool_result_turn is not None:
                    proof = await _tool_recovery_state(state, binding, op, tool_result_turn, json.loads(raw)["callId"])
                    if not (proof and proof.get("waiting") is True and proof.get("retryAllowed") is True
                            and proof.get("commandState") == "missing"):
                        raise _tool_result_pending(op)
            extra = {"tool_result_turn": tool_result_turn} if tool_result_turn is not None else {}
            if op['path'].endswith('/prompt'):
                source = state.store._one('SELECT status FROM router_bindings WHERE id=?', (op['session_id'],))
                if not source or source['status'] != 'ready':
                    raise _tool_result_pending(op)
            response = await state.nodes.request(
                binding["node_id"], op['method'], op["path"], content=raw, params=params,
                expected_boot=op["boot_id"] or binding["boot_id"], request_id=op["id"], **extra)
        except GateError as failed:
            if fresh and isinstance(failed, GateFreshNodeError):
                # Nodes проверил identity/доступность ДО отправки. Это известный
                # отказ доставки, в отличие от потерянного ответа принятой команды.
                response = httpx.Response(503, json={'error':{
                    'code':failed.code, 'message':failed.message, 'deliveryState':'not_sent'}})
                if op.get('steer'):
                    state.operations.observe_steer(op['id'], {**json.loads(op['steer']), 'state': 'not_applied'})
                state.operations.complete(op['id'], response, pre_dispatch=(binding, failed))
                if op['method'] == 'POST' and op['path'].endswith('/tool_result'):
                    raise _tool_result_pending(op) from failed
                return response
            state.operations.unknown(op["id"])
            log.warning("command pending: command=%s node=%s code=%s cause=%s",
                        op["id"], binding["node_id"], failed.code,
                        type(failed.__cause__).__name__ if failed.__cause__ else "none")
            if failed.status >= 500 or failed.code == "command_outcome_unknown":
                raise _tool_result_pending(op) from failed
            raise
        except (ValueError, KeyError, TypeError, AttributeError) as failed:
            state.operations.unknown(op["id"])
            raise _tool_result_pending(op) from failed
        try:
            state.operations.complete(op["id"], response)
        except (ValueError, KeyError, TypeError, AttributeError) as failed:
            state.operations.unknown(op['id'])
            raise _tool_result_pending(op) from failed
        return response


async def _delegate(request, device, sid, method, suffix, *, tool_result=None):
    state = gate(request)
    media_read = method == 'GET' and (suffix == '/media' or suffix.startswith('/media/'))
    binding = state.routing.get(sid, device, include_closed=media_read)
    # Revocation blocks further work, not observing/stopping/deleting owned work.
    control = method in ('GET', 'DELETE') or suffix == '/interrupt' or suffix.endswith('/stop')
    _policy(state, device, binding, request, require_model=suffix in ("/steer", "/tools", "/compact"), require_sdk=not control)
    if not media_read or binding['status'] == 'closed':
        binding = await state.routing.recover_boot(binding)
    if tool_result is not None and tool_result.attachments:
        validate_tool_result(tool_result, await _bound_attachments(state, binding))
    raw = await request.body()
    request_id = request.headers.get("idempotency-key")
    if not request_id and suffix == "/tool_result":
        request_id = "tool-result:" + sid + ":" + str(json.loads(raw).get("callId", ""))
    if method == "POST" and suffix == "/compact":
        return await _compact_control(request, device, binding, raw, request_id)
    if method == "DELETE":
        # Cleanup after an uncertain HTTP outcome must not cancel an active SDK command.
        for pending in state.store._all("SELECT * FROM router_operations WHERE session_id=? AND path LIKE '%/compact' AND finished=0", (sid,)):
            current = await _control_receipt(state, pending)
            if current["state"] != "complete":
                raise GateError(409, "compact_pending", "сжатие ещё выполняется; закрытие сессии требует явной отмены")
    op = None
    if method != "GET":
        op, fresh = state.operations.begin(device, binding, method, _path(binding, suffix), json.loads(raw) if raw else {}, request_id, nodes=state.nodes)
        response = await _deliver_operation(state, binding, op, fresh, raw, request.query_params)
        return _operation_response(response, op)
    context_order = time.time_ns()
    response = await state.nodes.request(binding["node_id"], method, _path(binding, suffix),
                                         content=raw, params=request.query_params,
                                         expected_boot=None if media_read and binding['status'] != 'closed' else binding["boot_id"])
    if method == "GET" and suffix == "/context" and response.status_code == 200:
        if state.context_windows.observe(binding,response.json(),context_order):
            await state.publish_contexts()
    if method == 'GET' and suffix in ('/files', '/sdk-tools') and response.status_code == 200:
        from .input_recovery import validate, public
        doc = response.json()
        if suffix == '/sdk-tools' and 'steering' in doc:
            from .steering import bound_capability, capability, negotiate
            if (request.query_params.get('steering') != '1'
                    and capability(doc['steering']).get('supported') is not False):
                doc['steering'] = await negotiate(state.nodes, binding)
            else:
                doc['steering'] = bound_capability(doc['steering'], binding)
        proof = doc.get('inputRecovery')
        sdk_id = binding.get('sdk_id')
        if proof is not None and not sdk_id:
            saved = await state.nodes.request(binding['node_id'], 'GET', _path(binding, '/reconcile'),
                                              expected_boot=binding['boot_id'])
            if saved.status_code != 200:
                raise GateError(409, 'input_recovery_unavailable', 'Привязка файловой сессии не подтверждена')
            metadata = saved.json()
            if (metadata.get('id') != binding['local_id'] or metadata.get('nodeId') != binding['node_id']
                    or metadata.get('bootId') != binding['boot_id']):
                raise GateError(409, 'input_recovery_mismatch', 'Идентичность файловой сессии не подтверждена')
            sdk_id = metadata.get('sdkSessionId')
        validate(proof, binding['local_id'], sdk_id)
        if proof is not None:
            doc['inputRecovery'] = public(proof, binding)
        return JSONResponse(doc)
    return _response(response)


@router.post("/harness/agent/sessions/{sid}/tool_result")
async def agent_tool_result(sid: str, body: ToolResultRequest, request: Request, device: Device = Depends(require_device)):
    turn_id = request.headers.get("x-atlas-tool-result-turn")
    if turn_id is not None:
        state = gate(request)
        binding = state.routing.get(sid, device)
        _policy(state, device, binding, request, require_model=False)
        key = f"tool-result:{sid}:{body.callId}"
        if request.headers.get("idempotency-key") != key or request.query_params:
            raise GateError(409, "tool_result_recovery_mismatch", "Требуется исходный ключ результата")
        raw = await request.body()
        op = _original_tool_operation(state, device, binding, turn_id, body.callId, body_sha256(json.loads(raw)))
        if op is None:
            raise GateError(404, "not_found", "Исходная доставка не найдена")
        try:
            proof = await _tool_recovery_state(state, binding, op, turn_id, body.callId)
        except (GateError, ValueError):
            proof = None
        if proof is None:
            raise _tool_result_pending(op)
        return _operation_response(await _deliver_operation(
            state, binding, op, False, raw, None, tool_result_turn=turn_id), op)
    return await _delegate(request, device, sid, "POST", "/tool_result", tool_result=body)


def _original_tool_operation(state, device, binding, turn_id, call_id, digest):
    key = f"tool-result:{binding['id']}:{call_id}"
    op = state.store._one(
        "SELECT * FROM router_operations WHERE id=? AND device_id=? AND session_id=? AND method='POST' AND path=?",
        (state.operations.key(device, key), device.device_id, binding["id"], _path(binding, "/tool_result")))
    if op and (op["body_sha256"] != digest or not turn_id or op["turn_id"] not in (None, turn_id)):
        raise GateError(409, "tool_result_recovery_mismatch", "Ход или хеш исходного результата не совпадает")
    return op


async def _tool_recovery_state(state, binding, op, turn_id, call_id):
    if op["node_id"] != binding["node_id"] or not op["boot_id"] or op["boot_id"] != binding["boot_id"]:
        return None
    info = state.nodes.snapshots.get(binding["node_id"])
    if not info or time.monotonic() - info.get("observed", 0) > 5:
        info = await state.nodes.probe(binding["node_id"])
    if (not info or info.get("bootId") != op["boot_id"]
            or not {"tool_result_recovery_v1", "durable_commands"}.issubset(info.get("capabilities", []))):
        return None
    response = await state.nodes.request(
        binding["node_id"], "GET", _path(binding, f"/turns/{quote(turn_id, safe='')}/tool-result-recovery"),
        params={"callId": call_id, "commandId": op["id"]}, expected_boot=op["boot_id"])
    if response.status_code != 200:
        return None
    proof = response.json()
    if isinstance(proof, dict) and proof.get("callKnown") is True and all(proof.get(key) == value for key, value in {
        "version": 1, "nodeId": binding["node_id"], "bootId": op["boot_id"],
        "sessionId": binding["local_id"], "turnId": turn_id, "callId": call_id, "commandId": op["id"],
    }.items()):
        return proof
    return None


@router.get("/harness/agent/sessions/{sid}/turns/{turn_id}/tool-result-recovery")
async def tool_result_recovery(sid: str, turn_id: str, callId: str, bodySha256: str,
                               request: Request, device: Device = Depends(require_device)):
    state = gate(request)
    binding = state.routing.get(sid, device)
    _policy(state, device, binding, request, require_model=False)
    op = _original_tool_operation(state, device, binding, turn_id, callId, bodySha256)
    if op is None:
        raise GateError(404, "not_found", "Исходная доставка не найдена")
    async with state.operations.delivery_lock(op["id"]):
        current = await _control_receipt(state, op)
        try:
            proof = await _tool_recovery_state(state, binding, op, turn_id, callId)
        except (GateError, ValueError):
            proof = None
    outcome, retry = "unknown", False
    if proof and current["state"] == "complete":
        try:
            receipt = json.loads(current["response"])
        except (ValueError, TypeError):
            receipt = None
        if isinstance(receipt, dict) and receipt.get("callId") == callId:
            if current["status"] == 200 and receipt.get("status") == "accepted":
                outcome = "accepted"
            elif current["status"] == 409 and receipt.get("status") == "turn_not_waiting":
                outcome = "closed"
    elif proof and current.get("receiptState") == "missing":
        retry = (proof.get("waiting") is True and proof.get("retryAllowed") is True
                 and proof.get("commandState") == "missing")
    return JSONResponse({"version": 1, "sessionId": sid, "turnId": turn_id, "callId": callId,
                         "requestId": f"tool-result:{sid}:{callId}", "commandId": op["id"],
                         "bodySha256": bodySha256, "outcome": outcome, "retryAllowed": retry},
                        headers={"Cache-Control": "private, no-store"})


@router.post("/harness/agent/sessions/{sid}/interrupt")
async def agent_interrupt(sid: str, request: Request, device: Device = Depends(require_device)):
    return await _delegate(request, device, sid, "POST", "/interrupt")


@router.post("/harness/agent/sessions/{sid}/steer")
async def agent_steer(sid: str, body: SteerRequest, request: Request, device: Device = Depends(require_device)):
    from .steering import capability, validate, CONTRACT_HEADER, TARGET_HEADER
    contract, target_boot = request.headers.get(CONTRACT_HEADER), request.headers.get(TARGET_HEADER)
    if body.expectedTurnId is None and contract is None and target_boot is None:
        return await _delegate(request, device, sid, "POST", "/steer")
    if contract != '1' or body.expectedTurnId is None or not target_boot or len(target_boot) > 200:
        raise GateError(422, 'steer_contract_required', 'Точный steer требует contract1, target boot и paired IDs',
                        state='unknown')  # A malformed retry cannot negate an already admitted key.
    state = gate(request)
    binding = state.routing.get(sid, device)
    _policy(state, device, binding, request)
    key = request.headers.get('idempotency-key')
    rid = state.operations.key(device, key)  # strong v1 requires the original stable key
    raw = await request.body()
    original = json.loads(raw)
    # Steering identity is over immutable outbox HTTP bytes, including omission
    # of defaults and Unicode encoding; prompt recovery keeps its own old rule.
    digest = hashlib.sha256(raw).hexdigest()
    existing = state.store._one('SELECT id FROM router_operations WHERE id=?', (rid,))
    info, preflight_error = None, None
    if existing is None:
        try:
            # The target was captured by the client before admission. A restart
            # cannot make a fresh steer target a newly restored SDK transport.
            if binding['boot_id'] != target_boot:
                raise GateError(409, 'steer_target_boot_mismatch', 'Исходный runtime сессии изменился')
            info = await state.nodes.probe(binding['node_id'])
            if not info or info.get('bootId') != target_boot:
                raise GateError(409, 'steer_target_boot_mismatch', 'Исходный runtime Router изменился')
        except GateError as failed:
            preflight_error = failed
    metadata = {'version': 1, 'sessionId': binding['local_id'], 'turnId': body.expectedTurnId,
                'clientMessageId': body.clientMessageId, 'uuid': None, 'state': 'unknown',
                'bodySha256': digest, '_targetBoot': target_boot, '_contract': 1, '_org': device.org}
    op, fresh = state.operations.begin(device, binding, 'POST', _path(binding, '/steer'), original, key,
                                      body_digest=digest, steer=metadata)
    if fresh:
        # Persist preflight negatives under the original key too: a later
        # capability/profile change must not admit the same rejected intent.
        try:
            if preflight_error is not None:
                raise preflight_error
            validate(body, capability((info or {}).get('steering')))
            if (info['steering'].get('targetNodeId') != binding['node_id'] or
                    info['steering'].get('targetBootId') != target_boot):
                raise GateError(409, 'steer_target_boot_mismatch', 'Capability относится к другому runtime')
            negotiated = await state.nodes.request(binding['node_id'], 'GET', _path(binding, '/sdk-tools'),
                                                   params={'steering': '1'}, expected_boot=binding['boot_id'])
            if negotiated.status_code != 200:
                error = json.loads(_response(negotiated).body).get('error') or {}
                error = error if isinstance(error, dict) else {}
                raise GateError(negotiated.status_code, error.get('code', 'steering_unavailable'),
                                error.get('message', 'Router не подтвердил активный runtime'))
            try:
                caps = capability(negotiated.json().get('steering'))
            except (ValueError, AttributeError):
                caps = capability(None)
            validate(body, caps)
            if caps['targetBootId'] != target_boot or caps['targetNodeId'] != binding['node_id']:
                raise GateError(409, 'steer_target_boot_mismatch', 'Сессия подтвердила другой runtime')
            validate_prompt(PromptRequest(id=body.clientMessageId, text=body.text, attachments=body.attachments),
                            None)  # Legacy prompt/upload ceilings are not strong SDK input limits.
        except GateError as failed:
            import httpx
            response = httpx.Response(failed.status, json={
                'error': {'code': failed.code, 'message': failed.message, **failed.extra,
                          'state': 'not_applied', 'deliveryState': 'not_sent'},
                'steer': {**metadata, 'state': 'not_applied'}})
            state.operations.complete(op['id'], response)
            return _operation_response(response, state.store._one('SELECT * FROM router_operations WHERE id=?', (op['id'],)))
    response = await _deliver_operation(state, binding, op, fresh, raw, request.query_params)
    current = state.store._one('SELECT * FROM router_operations WHERE id=?', (op['id'],))
    return _operation_response(response, current)


@router.post("/harness/agent/sessions/{sid}/tools")
async def agent_tools(sid: str, body: ToolsUpdate, request: Request, device: Device = Depends(require_device)):
    return await _delegate(request, device, sid, "POST", "/tools")


@router.post("/harness/agent/sessions/{sid}/compact")
async def agent_compact(sid: str, request: Request, body: CompactRequest | None = None, device: Device = Depends(require_device)):
    return await _delegate(request, device, sid, "POST", "/compact")


@router.get("/harness/agent/sessions/{sid}/context")
async def agent_context(sid: str, request: Request, device: Device = Depends(require_device)):
    return await _delegate(request, device, sid, "GET", "/context")


@router.get('/harness/agent/sessions/{sid}/attachments')
async def agent_attachments(sid: str, request: Request, device: Device = Depends(require_device)):
    state = await ready_gate(request)
    binding = state.routing.get(sid, device)
    _policy(state, device, binding, request)
    binding = await state.routing.recover_boot(binding)
    return {'attachments': await _bound_attachments(state, binding)}


@router.post('/harness/agent/sessions/{sid}/documents')
async def agent_upload_documents(sid: str, body: DocumentUploadRequest, request: Request, device: Device = Depends(require_device)):
    state = gate(request)
    binding = state.routing.get(sid, device)
    _policy(state, device, binding, request, require_model=False)
    validate_prompt(body, node_attachments(state.nodes.snapshots.get(binding['node_id'])))
    return await _delegate(request, device, sid, 'POST', '/documents')


@router.get('/harness/agent/sessions/{sid}/documents')
async def agent_documents(sid: str, request: Request, device: Device = Depends(require_device)):
    return await _delegate(request, device, sid, 'GET', '/documents')


@router.get('/harness/agent/sessions/{sid}/documents/{document_id}')
async def agent_document_page(sid: str, document_id: str, request: Request, device: Device = Depends(require_device)):
    return await _delegate(request, device, sid, 'GET', '/documents/'+quote(document_id, safe=''))


@router.delete('/harness/agent/sessions/{sid}/documents/{document_id}')
async def agent_delete_document(sid: str, document_id: str, request: Request, device: Device = Depends(require_device)):
    return await _delegate(request, device, sid, 'DELETE', '/documents/'+quote(document_id, safe=''))


@router.post("/harness/agent/sessions/{sid}/tasks/{task_id}/stop")
async def agent_stop(sid: str, task_id: str, request: Request, device: Device = Depends(require_device)):
    return await _delegate(request, device, sid, "POST", f"/tasks/{quote(task_id, safe='')}/stop")


@router.get("/harness/agent/sessions/{sid}/turns/{turn_id}")
async def agent_turn(sid: str, turn_id: str, request: Request, device: Device = Depends(require_device)):
    state = gate(request)
    binding = state.routing.get(sid, device)
    _policy(state, device, binding, request, require_model=False, require_sdk=False)
    binding = await state.routing.recover_boot(binding)
    response = await state.nodes.request(binding["node_id"], "GET", _path(binding, f"/turns/{quote(turn_id, safe='')}"),
                                         params={"toolResultRecovery": "1"},
                                         expected_boot=binding["boot_id"])
    if response.status_code != 200:
        return _response(response)
    status = response.json()
    callbacks = status.pop("toolCallbacks", None)
    if isinstance(callbacks, dict) and all(callbacks.get(key) == value for key, value in {
        "version": 1, "sessionId": binding["local_id"], "nodeId": binding["node_id"],
        "bootId": binding["boot_id"], "turnId": turn_id, "seq": status.get("seq"),
    }.items()) and status.get("turnId") == turn_id and status.get("done") is False:
        pending = callbacks.get("pendingCallIds")
        if isinstance(pending, list) and all(isinstance(call, str) and call for call in pending):
            status["toolCallbacks"] = dict(callbacks, sessionId=sid)
    return JSONResponse(status, headers={"Cache-Control": "private, no-store"})


@router.get("/harness/agent/sessions/{sid}/turns/{turn_id}/events")
async def agent_events(sid: str, turn_id: str, request: Request, device: Device = Depends(require_device)):
    state = gate(request)
    binding = state.routing.get(sid, device, include_closed=True)
    _policy(state, device, binding, request, require_model=False, require_sdk=False)
    try:
        after, wait = int(request.query_params.get("after", 0)), int(request.query_params.get("wait", 0))
        if after < 0 or wait < 0:
            raise ValueError
    except ValueError:
        raise GateError(422, "invalid_request", "after/wait должны быть неотрицательными числами")
    response, doc = await _follow_event_page(state, binding, turn_id, device, after, min(wait, 30000),
                                            retry_window=3)
    return JSONResponse(doc) if doc is not None else _response(response)


@router.get('/harness/agent/sessions/{sid}/turns/{turn_id}/receipt')
async def agent_turn_receipt(sid: str, turn_id: str, request: Request, device: Device = Depends(require_device)):
    state = gate(request)
    binding = state.routing.get(sid, device, include_closed=True)
    _policy(state, device, binding, request, require_model=False, require_sdk=False)
    binding = await state.routing.recover_boot(binding)
    response = await state.nodes.request(binding['node_id'], 'GET',
        _path(binding, f'/turns/{quote(turn_id, safe="")}/receipt'), expected_boot=binding['boot_id'])
    if response.status_code != 200:
        return _response(response)
    terminal = state.routing.translate(binding, response.json())
    if accountable_terminal(terminal):
        state.routing.usage(binding, turn_id, terminal, device)
    if terminal.get('type') in ('result', 'error'):
        state.store._exec('UPDATE router_operations SET finished=1,accounted=1 WHERE session_id=? AND turn_id=?',
                         (binding['id'], turn_id))
    return JSONResponse(terminal, headers={'Cache-Control': 'private, no-store'})


@router.delete("/harness/agent/sessions/{sid}")
async def agent_delete(sid: str, request: Request, device: Device = Depends(require_device)):
    state = gate(request)
    binding = state.store._one("SELECT * FROM router_bindings WHERE id=? AND device_id=? AND org=? AND status IN ('ready','closed')", (sid, device.device_id, device.org))
    if not binding:
        raise GateError(404, "not_found", "сессия не найдена")
    if binding["status"] == "closed":
        return JSONResponse({"ok": True})
    response = await _delegate(request, device, sid, "DELETE", "")
    if response.status_code < 400 or response.status_code == 404:
        state.store._exec("UPDATE router_bindings SET status='closed' WHERE id=?", (sid,))
    return response


@router.get('/harness/agent/sessions/{sid}/sdk-tools')
async def agent_sdk_tools(sid: str, request: Request, device: Device = Depends(require_device)):
    return await _delegate(request, device, sid, 'GET', '/sdk-tools')


@router.get('/harness/agent/sessions/{sid}/reasoning')
async def agent_reasoning(sid: str, request: Request, device: Device = Depends(require_device)):
    state = gate(request)
    binding = state.routing.get(sid, device, include_closed=True)
    _policy(state, device, binding, request, require_model=False, require_sdk=False)
    binding = await state.routing.recover_boot(binding)
    response = await state.nodes.request(binding['node_id'], 'GET', _path(binding, '/reasoning'), expected_boot=binding['boot_id'])
    if response.status_code >= 400:
        return _response(response)
    doc = response.json()
    from .reasoning import confirm
    try:
        confirm(json.loads(binding.get('reasoning_snapshot') or '{}'), doc)
    except ValueError as failed:
        raise GateError(502, 'reasoning_confirmation_missing', str(failed)) from failed
    return JSONResponse(doc, headers={'Cache-Control':'private, no-store'})


@router.get('/harness/agent/sessions/{sid}/media')
async def agent_media(sid: str, request: Request, device: Device = Depends(require_device)):
    response = await _delegate(request, device, sid, 'GET', '/media')
    response.headers['Cache-Control'] = 'private, no-store'
    return response


@router.get('/harness/agent/sessions/{sid}/media/{artifact_id}')
async def agent_media_read(sid: str, artifact_id: str, request: Request, device: Device = Depends(require_device)):
    import re
    if not re.fullmatch(r'[a-f0-9]{64}', artifact_id):
        raise GateError(404, 'not_found', 'изображение не найдено')
    response = await _delegate(request, device, sid, 'GET', '/media/' + artifact_id)
    response.headers['Cache-Control'] = 'private, no-store'
    return response

@router.post('/harness/agent/sessions/{sid}/files')
async def agent_upload_files(sid: str, request: Request, device: Device = Depends(require_device)):
    return await _delegate(request, device, sid, 'POST', '/files')


def _adaptive_offer(binding):
    offer = json.loads(binding.get('response') or '{}').get('adaptiveFiles')
    if isinstance(offer, dict) and offer.get('version') == 1 and offer.get('branchId') and offer.get('bootId'):
        return offer
    return None


def _adaptive_binding(request, device, sid, *, receipt=False):
    state = gate(request)
    binding = state.routing.get(sid, device, include_closed=receipt)
    _policy(state, device, binding, request, require_model=not receipt, require_sdk=not receipt)
    if not receipt and not org_of(state, device).sdk_tools.files:
        raise GateError(403, 'policy_denied', 'Организация отключила доступ к оригиналам')
    if not _adaptive_offer(binding):
        raise GateError(409, 'adaptive_files_not_negotiated', 'Сессия не согласовала адаптивные файлы')
    return state, binding


@router.get('/harness/agent/sessions/{sid}/files/adaptive')
async def agent_adaptive_capability(sid: str, request: Request, device: Device = Depends(require_device)):
    state, binding = _adaptive_binding(request, device, sid)
    if not state.routing.adaptive_transport(binding['node_id']):
        raise GateError(409, 'adaptive_files_not_negotiated', 'Connector не подтвердил адаптивные файлы')
    binding = await state.routing.recover_boot(binding)
    response = await state.nodes.request(binding['node_id'], 'GET', _path(binding, '/files/adaptive'),
                                         expected_boot=binding['boot_id'])
    if response.status_code != 200:
        return _response(response)
    try:
        capability = response.json()['adaptiveFiles']
        expected = dict(_adaptive_offer(binding), bootId=binding['boot_id'])
        if capability != expected:
            raise ValueError('capability mismatch')
    except (ValueError, KeyError, TypeError) as failed:
        raise GateError(502, 'adaptive_files_ack_invalid', 'Router не подтвердил capability') from failed
    return JSONResponse({'adaptiveFiles': capability}, headers={'Cache-Control': 'private, no-store'})


@router.get('/harness/agent/sessions/{sid}/files/adaptive/{request_id}')
async def agent_adaptive_receipt(sid: str, request_id: uuid.UUID, request: Request, device: Device = Depends(require_device)):
    state, binding = _adaptive_binding(request, device, sid, receipt=True)
    response = await state.nodes.request(binding['node_id'], 'GET', _path(binding, '/files/adaptive/' + str(request_id)))
    result = _response(response)
    result.headers['Cache-Control'] = 'private, no-store'
    return result


@router.post('/harness/agent/sessions/{sid}/files/adaptive/{request_id}')
async def agent_adaptive_reply(sid: str, request_id: uuid.UUID, request: Request, device: Device = Depends(require_device)):
    state, binding = _adaptive_binding(request, device, sid)
    if request.headers.get('idempotency-key') != str(request_id):
        raise GateError(422, 'adaptive_file_idempotency_key_required', 'Ожидается исходный requestId')
    raw = await request.body()
    # Recheck policy and owner after the body read, before forwarding to the original boot.
    state, binding = _adaptive_binding(request, device, sid)
    response = await state.nodes.request(binding['node_id'], 'POST', _path(binding, '/files/adaptive/' + str(request_id)),
        expected_boot=binding['boot_id'], request_id=str(request_id), content=raw)
    return _response(response)

@router.get('/harness/agent/sessions/{sid}/files')
async def agent_list_files(sid: str, request: Request, device: Device = Depends(require_device)):
    return await _delegate(request, device, sid, 'GET', '/files')

@router.delete('/harness/agent/sessions/{sid}/files/{file_id}')
async def agent_delete_file(sid: str, file_id: str, request: Request, device: Device = Depends(require_device)):
    return await _delegate(request, device, sid, 'DELETE', '/files/'+quote(file_id, safe=''))
