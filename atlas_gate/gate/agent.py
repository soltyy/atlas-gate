"""Сетевой агентный Gate: публичные IDs, владение, async producer и polling/SSE."""
from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime, timezone
from urllib.parse import quote
from fastapi import Depends, Request
from fastapi.responses import Response, JSONResponse
from sse_starlette.sse import EventSourceResponse
from ..schemas import CompactRequest, PromptRequest, SteerRequest, ToolResultRequest, ToolsUpdate
from .api import check_data_class, gate, org_of, ready_gate, require_device, router
from .errors import GateError, STATUS_CODES
from .schemas import GateAgentSessionCreate
from .state import Device

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


def _policy(state, device, binding, request):
    _selection(state, device, binding["model"], binding["route"])
    for role in json.loads(binding["roles"]):
        if role.get("model") not in (None, "", "inherit"):
            _selection(state, device, role["model"], binding["route"])
    check_data_class(state, device, request, binding["route"])
    node = state.nodes.config.get(binding["node_id"])
    if node is None or not node.enabled or device.org not in node.orgs or binding["route"] not in node.routes:
        raise GateError(403, "policy_denied", "узел больше не разрешён организации")


def _path(binding, suffix=""):
    return "/v1/sessions/" + quote(binding["local_id"], safe="") + suffix


async def _events(state, binding, turn, device, after=0, wait=0):
    response = await state.nodes.request(binding["node_id"], "GET", _path(binding, f"/turns/{quote(turn, safe='')}/events"), params={"after": after, "wait": wait}, expected_boot=binding["boot_id"])
    if response.status_code >= 400:
        return response, None
    doc = response.json()
    doc["events"] = [state.routing.translate(binding, ev) for ev in doc.get("events", [])]
    for ev in doc["events"]:
        if ev.get("type") == "result":
            state.routing.usage(binding, turn, ev, device)
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
    route, model = _selection(state, device, body.model)
    for role in body.agents:
        if role.model and role.model != "inherit":
            _selection(state, device, role.model, route)
    check_data_class(state, device, request, route)
    status, raw = await state.routing.create(device, route, model, body.model_dump(exclude_none=True), state.settings.ATLAS_GATE_AGENT_SESSIONS_PER_DEVICE)
    import httpx
    return _response(httpx.Response(status, content=raw))


@router.post("/harness/agent/sessions/{sid}/prompt")
async def agent_prompt(sid: str, body: PromptRequest, request: Request, device: Device = Depends(require_device)):
    state = gate(request)
    binding = state.routing.get(sid, device)
    _policy(state, device, binding, request)
    mode = request.query_params.get("mode", "sse")
    if mode not in ("", "sse", "async"):
        raise GateError(422, "invalid_request", "неизвестный mode")
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not state.store.agent_turns_reserve(device.user_id, day, org_of(state, device).quota.agent_turns_per_day):
        raise GateError(402, "quota_exceeded", "дневной лимит исчерпан")
    response = await state.nodes.request(binding["node_id"], "POST", _path(binding, "/prompt"), content=body.model_dump_json().encode(), params={"mode": "async"}, expected_boot=binding["boot_id"])
    if response.status_code >= 400:
        state.store.agent_turns_release(device.user_id, day)
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


async def _delegate(request, device, sid, method, suffix):
    state = gate(request)
    binding = state.routing.get(sid, device)
    _policy(state, device, binding, request)
    response = await state.nodes.request(binding["node_id"], method, _path(binding, suffix), content=await request.body(), params=request.query_params, expected_boot=binding["boot_id"])
    return _response(response)


@router.post("/harness/agent/sessions/{sid}/tool_result")
async def agent_tool_result(sid: str, body: ToolResultRequest, request: Request, device: Device = Depends(require_device)):
    return await _delegate(request, device, sid, "POST", "/tool_result")


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
    _policy(state, device, binding, request)
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
    binding = state.routing.get(sid, device)
    response = await state.nodes.request(binding["node_id"], "DELETE", _path(binding), expected_boot=binding["boot_id"])
    if response.status_code < 400 or response.status_code == 404:
        state.store._exec("UPDATE router_bindings SET status='closed' WHERE id=?", (sid,))
    return _response(response)
