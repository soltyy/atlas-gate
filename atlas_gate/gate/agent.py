"""Сетевой агентный Gate: публичные IDs, владение, async producer и polling/SSE."""
from __future__ import annotations
import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from urllib.parse import quote
from fastapi import Depends, Request
from fastapi.responses import Response, JSONResponse
from sse_starlette.sse import EventSourceResponse
from ..schemas import CompactRequest, DocumentUploadRequest, PromptRequest, SteerRequest, ToolResultRequest, ToolsUpdate
from .api import check_data_class, gate, org_of, ready_gate, require_device, router
from .errors import GateError, STATUS_CODES
from .schemas import GateAgentSessionCreate
from ..sdk_tools import SdkTools, permitted
from .state import Device
from .attachments import node_attachments, validate_prompt, validate_tool_result

log = logging.getLogger("atlas_gate")


def _response(response):
    raw = response.content
    if response.status_code >= 400:
        try:
            doc = response.json()
        except ValueError:
            doc = {}
        if not isinstance(doc, dict) or "error" not in doc:
            detail = doc.get("detail", "Router отказал") if isinstance(doc, dict) else doc
            code = STATUS_CODES.get(response.status_code, "agent_error")
            if str(detail).startswith("session_suspended"):
                code = "session_suspended"
            elif str(detail) == "router_identity_mismatch":
                code = "router_identity_mismatch"
            raw = json.dumps({"error": {"code": code, "message": str(detail)}}, ensure_ascii=False).encode()
    return Response(content=raw, status_code=response.status_code, media_type="application/json")


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
            if terminal.get("type") == "result":
                state.routing.usage(binding, turn, terminal, device)
                op = state.store._one('SELECT created FROM router_operations WHERE session_id=? AND turn_id=?',(binding['id'],turn))
                if state.context_windows.observe(binding,terminal.get('context'),context_order,(op or {}).get('created',0)):
                    await state.publish_contexts()
            state.store._exec("UPDATE router_operations SET finished=1,accounted=1 WHERE session_id=? AND turn_id=?", (binding["id"], turn))
    if response.status_code >= 400:
        return response, None
    doc = response.json()
    doc["events"] = [state.routing.translate(binding, ev) for ev in doc.get("events", [])]
    for ev in doc["events"]:
        state.nodes.observe_event(binding["node_id"], ev)
        if ev.get("type") == "result":
            state.routing.usage(binding, turn, ev, device)
    if doc.get("done"):
        state.store._exec("UPDATE router_operations SET finished=1,accounted=1 WHERE session_id=? AND turn_id=?", (binding["id"], turn))
    for event in doc.get('events',[]):
        if event.get('type') == 'result':
            op = state.store._one('SELECT created FROM router_operations WHERE session_id=? AND turn_id=?',(binding['id'],turn))
            if state.context_windows.observe(binding,event.get('context'),context_order,(op or {}).get('created',0)):
                await state.publish_contexts()
    return response, doc


async def _watch(state, binding, turn, device):
    after = 0
    try:
        for _ in range(3600):
            _, doc = await _events(state, binding, turn, device, after, 1000)
            if doc is None:
                return
            after = doc.get("nextAfter", after)
            if doc.get("done"):
                return
            await asyncio.sleep(0.01)
    except GateError as failed:
        log.warning("usage watcher %s остановлен: %s", turn, failed.code)


@router.get("/harness/agent/sessions")
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


@router.post("/harness/agent/sessions")
async def agent_session_create(body: GateAgentSessionCreate, request: Request, device: Device = Depends(require_device)):
    state = await ready_gate(request)
    if body.sdkTools is not None and not permitted(body.sdkTools, org_of(state, device).sdk_tools):
        raise GateError(403, 'policy_denied', 'SDK tools не разрешены организацией')
    route, model = _selection(state, device, body.model)
    for role in body.agents:
        if role.model and role.model != "inherit":
            _selection(state, device, role.model, route)
    check_data_class(state, device, request, route)
    status, raw = await state.routing.create(device, route, model, body.model_dump(exclude_none=True), state.settings.ATLAS_GATE_AGENT_SESSIONS_PER_DEVICE, request.headers.get("idempotency-key"), sdk_policy=org_of(state, device).sdk_tools)
    import httpx
    return _response(httpx.Response(status, content=raw))


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
    operation, fresh = state.operations.begin(device, binding, "POST", _path(binding, "/prompt"), body.model_dump(),
        request.headers.get("idempotency-key"), body.id, day, org_of(state, device).quota.agent_turns_per_day, state.nodes)
    if not fresh:
        if operation["state"] != "complete":
            await state.operations.reconcile(state.nodes)
            operation = state.store._one("SELECT * FROM router_operations WHERE id=?", (operation["id"],))
        if operation["state"] != "complete":
            raise GateError(409, "command_outcome_unknown", "ход требует сверки; повторное исполнение запрещено")
        import httpx
        response = httpx.Response(operation["status"], content=operation["response"].encode())
        if response.status_code >= 400 or mode == "async":
            return _response(response)
    else:
        try:
            response = await state.nodes.request(binding["node_id"], "POST", _path(binding, "/prompt"), content=body.model_dump_json().encode(), params={"mode": "async"}, expected_boot=binding["boot_id"], request_id=operation["id"])
        except GateError as failed:
            if failed.code == "node_unavailable":
                import httpx
                state.operations.complete(operation["id"], httpx.Response(503, json={"error": {"code": failed.code, "message": failed.message}}))
            else:
                state.operations.unknown(operation["id"])
            raise
        state.operations.complete(operation["id"], response)
    if response.status_code >= 400:
        return _response(response)
    task = asyncio.create_task(_watch(state, binding, body.id, device))
    state.watchers.add(task)
    task.add_done_callback(state.watchers.discard)
    if mode == "async":
        return _response(response)

    async def stream():
        after = 0
        try:
            while True:
                _, doc = await _events(state, binding, body.id, device, after, 1000)
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
    if current["state"] != "complete":
        current["receiptState"] = "unavailable"
        try:
            response = await state.nodes.request(op["node_id"], "GET", "/v1/commands/" + op["id"])
            if response.status_code == 200:
                current["receiptState"] = response.json().get("state", "unknown")
            if response.status_code == 200 and response.json().get("state") == "complete":
                import httpx
                saved = response.json()
                state.operations.complete(op["id"], httpx.Response(saved["status"], json=saved["response"]))
                current = state.store._one("SELECT * FROM router_operations WHERE id=?", (op["id"],))
        except (GateError, ValueError, KeyError):
            pass
    return current


def _pending_control(op):
    return JSONResponse({"commandId": op["id"], "state": "pending",
                         "sessionId": op["session_id"]}, status_code=202,
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
    state = gate(request)
    binding = state.store._one("SELECT * FROM router_bindings WHERE id=? AND device_id=? AND org=?", (sid, device.device_id, device.org))
    if binding is None:
        raise GateError(404, "not_found", "сессия не найдена")
    op = state.store._one("SELECT * FROM router_operations WHERE id=? AND device_id=? AND session_id=? AND path LIKE '%/compact'",
                         (command_id, device.device_id, sid))
    if op is None:
        raise GateError(404, "not_found", "команда не найдена")
    current = await _control_receipt(state, op)
    if current["state"] != "complete":
        if current.get("receiptState") == "unknown":
            raise GateError(503, "command_outcome_unknown", "Router перезапущен; требуется сверка результата сжатия", command_id=op["id"])
        return _pending_control(op)
    import httpx
    result = _response(httpx.Response(current["status"], content=current["response"].encode()))
    result.headers["X-Atlas-Command-State"] = "complete"
    return result


def _tool_result_pending(op):
    return GateError(503, "command_outcome_unknown",
                     "доставка результата ещё не подтверждена; повторите ту же команду",
                     command_id=op["id"])


async def _deliver_tool_result(state, binding, op, fresh, raw, params):
    """Сверка ACK и доставка того же результата, без повторного запуска инструмента (#50)."""
    import httpx

    async with state.operations.delivery_lock(op["id"]):
        op = state.store._one("SELECT * FROM router_operations WHERE id=?", (op["id"],))
        if op["state"] == "complete":
            return _response(httpx.Response(op["status"], content=op["response"].encode()))
        try:
            if not fresh:
                receipt = await state.nodes.request(binding["node_id"], "GET", "/v1/commands/" + op["id"])
                if receipt.status_code == 200:
                    saved = receipt.json()
                    if (saved.get("state") == "complete" and type(saved.get("status")) is int
                            and 100 <= saved["status"] <= 599 and "response" in saved):
                        response = httpx.Response(saved["status"], json=saved["response"])
                        state.operations.complete(op["id"], response)
                        return _response(response)
                    raise _tool_result_pending(op)
                if receipt.status_code != 404:
                    raise _tool_result_pending(op)
                # 404 разрешает повтор только при прежнем boot и durable дедупликации.
                # Для старой записи без boot свидетельств недостаточно: ничего не посылаем.
                info = await state.nodes.probe(binding["node_id"])
                if (not info or not op["boot_id"] or info["bootId"] != op["boot_id"]
                        or binding["boot_id"] != op["boot_id"]
                        or "durable_commands" not in info.get("capabilities", [])):
                    raise _tool_result_pending(op)
            response = await state.nodes.request(
                binding["node_id"], "POST", op["path"], content=raw, params=params,
                expected_boot=op["boot_id"] or binding["boot_id"], request_id=op["id"])
        except GateError as failed:
            state.operations.unknown(op["id"])
            log.warning("tool_result pending: command=%s node=%s code=%s cause=%s",
                        op["id"], binding["node_id"], failed.code,
                        type(failed.__cause__).__name__ if failed.__cause__ else "none")
            if failed.status >= 500 or failed.code == "command_outcome_unknown":
                raise _tool_result_pending(op) from failed
            raise
        except (ValueError, KeyError, TypeError, AttributeError) as failed:
            state.operations.unknown(op["id"])
            raise _tool_result_pending(op) from failed
        state.operations.complete(op["id"], response)
        return _response(response)


async def _delegate(request, device, sid, method, suffix, *, tool_result=None):
    state = gate(request)
    binding = state.routing.get(sid, device)
    # Revocation blocks further work, not observing/stopping/deleting owned work.
    control = method in ('GET', 'DELETE') or suffix == '/interrupt' or suffix.endswith('/stop')
    _policy(state, device, binding, request, require_model=suffix in ("/steer", "/tools", "/compact"), require_sdk=not control)
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
        if suffix == "/compact":
            await state.nodes.refresh()
        op, fresh = state.operations.begin(device, binding, method, _path(binding, suffix), json.loads(raw) if raw else {}, request_id, nodes=state.nodes)
        if suffix == "/tool_result":
            return await _deliver_tool_result(state, binding, op, fresh, raw, request.query_params)
        if not fresh:
            if op["state"] != "complete":
                await state.operations.reconcile(state.nodes)
                op = state.store._one("SELECT * FROM router_operations WHERE id=?", (op["id"],))
            if op["state"] == "complete":
                import httpx
                return _response(httpx.Response(op["status"], content=op["response"].encode()))
            raise GateError(409, "command_outcome_unknown", "управляющая команда требует сверки")
    context_order = time.time_ns()
    try:
        response = await state.nodes.request(binding["node_id"], method, _path(binding, suffix), content=raw, params=request.query_params, expected_boot=binding["boot_id"], request_id=op["id"] if op else None)
    except GateError:
        if op:
            state.operations.unknown(op["id"])
        raise
    if op:
        state.operations.complete(op["id"], response)
    if method == "GET" and suffix == "/context" and response.status_code == 200:
        if state.context_windows.observe(binding,response.json(),context_order):
            await state.publish_contexts()
    return _response(response)


@router.post("/harness/agent/sessions/{sid}/tool_result")
async def agent_tool_result(sid: str, body: ToolResultRequest, request: Request, device: Device = Depends(require_device)):
    return await _delegate(request, device, sid, "POST", "/tool_result", tool_result=body)


@router.post("/harness/agent/sessions/{sid}/interrupt")
async def agent_interrupt(sid: str, request: Request, device: Device = Depends(require_device)):
    return await _delegate(request, device, sid, "POST", "/interrupt")


@router.post("/harness/agent/sessions/{sid}/steer")
async def agent_steer(sid: str, body: SteerRequest, request: Request, device: Device = Depends(require_device)):
    return await _delegate(request, device, sid, "POST", "/steer")


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
    return await _delegate(request, device, sid, "GET", f"/turns/{quote(turn_id, safe='')}")


@router.get("/harness/agent/sessions/{sid}/turns/{turn_id}/events")
async def agent_events(sid: str, turn_id: str, request: Request, device: Device = Depends(require_device)):
    state = gate(request)
    binding = state.routing.get(sid, device)
    _policy(state, device, binding, request, require_model=False, require_sdk=False)
    try:
        after, wait = int(request.query_params.get("after", 0)), int(request.query_params.get("wait", 0))
        if after < 0 or wait < 0:
            raise ValueError
    except ValueError:
        raise GateError(422, "invalid_request", "after/wait должны быть неотрицательными числами")
    response, doc = await _events(state, binding, turn_id, device, after, min(wait, 30000))
    return JSONResponse(doc) if doc is not None else _response(response)


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

@router.post('/harness/agent/sessions/{sid}/files')
async def agent_upload_files(sid: str, request: Request, device: Device = Depends(require_device)):
    return await _delegate(request, device, sid, 'POST', '/files')

@router.get('/harness/agent/sessions/{sid}/files')
async def agent_list_files(sid: str, request: Request, device: Device = Depends(require_device)):
    return await _delegate(request, device, sid, 'GET', '/files')

@router.delete('/harness/agent/sessions/{sid}/files/{file_id}')
async def agent_delete_file(sid: str, file_id: str, request: Request, device: Device = Depends(require_device)):
    return await _delegate(request, device, sid, 'DELETE', '/files/'+quote(file_id, safe=''))
