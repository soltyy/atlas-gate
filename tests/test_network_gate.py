"""Два настоящих Router через HTTP; тестовая зависимость на их исходники, без SDK входа."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import socket
import sys
import httpx
import pytest
import uvicorn
from atlas_gate.main import create_app
from atlas_gate.settings import Settings


@contextlib.asynccontextmanager
async def serve(app):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(500):
            if server.started:
                break
            if task.done():
                task.result()
                raise RuntimeError("server exited")
            await asyncio.sleep(0.01)
        else:
            raise TimeoutError("server startup")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 15)


@pytest.fixture
async def network(tmp_path, monkeypatch):
    root = os.environ.get("ATLAS_TEST_ROUTER_ROOT")
    if not root:
        pytest.skip("ATLAS_TEST_ROUTER_ROOT: path to Router checkout with /v1/node")
    sys.path.insert(0, root)
    from atlas_router.main import create_app as router_app
    from atlas_router.settings import Settings as RouterSettings
    nodes = []
    apps = []
    async with contextlib.AsyncExitStack() as stack:
        for name in ("a", "b"):
            app = router_app(RouterSettings(_env_file=None, ATLAS_BACKEND="stub", ATLAS_GATE_ENABLED=False,
                             ATLAS_TRUST_LOCAL=False, ATLAS_TOKEN="test-router", ATLAS_NODE_ID=name, ATLAS_NODE_CAPACITY=4))
            apps.append(app)
            url = await stack.enter_async_context(serve(app))
            nodes.append({"node_id": name, "url": url, "token_env": "NODE_TOKEN", "orgs": ["test-org"], "routes": ["claude-sub"], "account_group": name})
        monkeypatch.setenv("NODE_TOKEN", "test-router")
        file = tmp_path / "nodes.json"
        file.write_text(json.dumps(nodes))
        settings = Settings(_env_file=None, ATLAS_TOKEN="admin", ATLAS_GATE_TEST=True,
            ATLAS_GATE_DB=str(tmp_path / "gate.db"), ATLAS_GATE_KEY_PATH=str(tmp_path / "key.pem"),
            ATLAS_GATE_KEYRING_SECRET="test-secret", ATLAS_GATE_ORGS_DIR=str(tmp_path / "orgs"),
            ATLAS_GATE_KEYS_FILE=str(tmp_path / "no-keys"), ATLAS_GATE_NODES_FILE=str(file), ATLAS_GATE_AGENT_SESSIONS_PER_DEVICE=10)
        gate_app = create_app(settings)
        url = await stack.enter_async_context(serve(gate_app))
        client = await stack.enter_async_context(httpx.AsyncClient(base_url=url, timeout=30))
        await gate_app.state.gate.ready.wait()
        async def enroll(name):
            start = (await client.post("/harness/enroll/start", json={"device_name": name, "platform": "test", "app_version": "1.0"})).json()
            approved = await client.post("/harness/enroll/approve", headers={"X-Atlas-Token": "admin"}, json={"user_code": start["user_code"], "org": "test-org", "user": {"id": name, "email": name + "@test.invalid", "name": name}})
            assert approved.status_code == 200, approved.text
            poll = await client.post("/harness/enroll/poll", json={"device_code": start["device_code"]})
            assert poll.status_code == 200, poll.text
            return poll.json()
        creds = await enroll("first")
        other = await enroll("other")
        client.headers["Authorization"] = "Bearer " + creds["device_token"]
        yield client, gate_app, apps, settings, creds, other


async def create(client):
    response = await client.post("/harness/agent/sessions", json={"system": "test", "model": "stub-fast", "tools": [{"name": "echo", "description": "echo", "parameters": {"type": "object"}}]})
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def events(client, sid, turn, terminal=False):
    for _ in range(100):
        response = await client.get(f"/harness/agent/sessions/{sid}/turns/{turn}/events", params={"wait": 100})
        assert response.status_code == 200, response.text
        doc = response.json()
        if doc["done"] if terminal else any(e["type"] == "tool_call" for e in doc["events"]):
            return doc["events"]
        await asyncio.sleep(0.01)
    raise AssertionError("events timeout")


@pytest.mark.asyncio
async def test_http_tools_affinity_usage_and_gate_restart(network):
    client, app, routers, settings, creds, other = network
    profile = await client.get("/harness/profile")
    assert profile.status_code == 200
    sid1 = await create(client)
    first = app.state.gate.store._one("SELECT * FROM router_bindings WHERE id=?", (sid1,))
    response = await client.post(f"/harness/agent/sessions/{sid1}/prompt?mode=async", json={"id": "turn1", "text": "call:echo"})
    assert response.status_code == 202, response.text
    call = next(e for e in await events(client, sid1, "turn1") if e["type"] == "tool_call")
    # Новый запрос выбирает другой, свободный Router; прежняя беседа остаётся на первом.
    sid2 = await create(client)
    second = app.state.gate.store._one("SELECT * FROM router_bindings WHERE id=?", (sid2,))
    assert first["node_id"] != second["node_id"]
    wrong = await client.post(f"/harness/agent/sessions/{sid1}/tool_result", headers={"Authorization": "Bearer " + other["device_token"]}, json={"callId": call["callId"], "content": "wrong"})
    assert wrong.status_code == 404
    ok = await client.post(f"/harness/agent/sessions/{sid1}/tool_result", json={"callId": call["callId"], "content": "correct"})
    assert ok.status_code == 200, ok.text
    result = await events(client, sid1, "turn1", True)
    assert next(e for e in result if e["type"] == "result")["text"].endswith("correct")
    alias = next(e for e in result if e["type"] == "session")["sessionId"]
    assert alias == first["sdk_alias"]
    await events(client, sid1, "turn1", True)
    assert app.state.gate.store._one("SELECT COUNT(*) n FROM usage WHERE kind='agent'")["n"] == 1
    second_turn = await client.post(f"/harness/agent/sessions/{sid1}/prompt?mode=async", json={"id": "turn2", "text": "second message"})
    assert second_turn.status_code == 202
    await events(client, sid1, "turn2", True)
    compact = await client.post(f"/harness/agent/sessions/{sid1}/compact", json={})
    assert compact.status_code == 200, compact.text
    # Второй Gate поверх той же БД после закрытия первого state; текущие Router продолжают жить.
    await app.state.gate.close()
    # Сервер теста остаётся, но к закрытому state больше не обращаемся.
    reopened = create_app(settings)
    async with serve(reopened) as url, httpx.AsyncClient(base_url=url, headers={"Authorization": "Bearer " + creds["device_token"]}) as restored:
        await reopened.state.gate.ready.wait()
        status = await restored.get(f"/harness/agent/sessions/{sid1}/context")
        assert status.status_code == 200, status.text
        assert reopened.state.gate.store._one("SELECT node_id FROM router_bindings WHERE id=?", (sid1,))["node_id"] == first["node_id"]
        close = await restored.delete(f"/harness/agent/sessions/{sid1}")
        assert close.status_code == 200
        resume = await restored.post("/harness/agent/sessions", json={"system": "test", "model": "stub-fast", "tools": [], "resumeSessionId": alias})
        assert resume.status_code == 200 and resume.json()["resumed"], resume.text
        assert reopened.state.gate.store._one("SELECT node_id FROM router_bindings WHERE id=?", (resume.json()["id"],))["node_id"] == first["node_id"]


@pytest.mark.asyncio
async def test_sse_and_control_mirrors(network):
    client, app, routers, settings, creds, other = network
    sid = await create(client)
    response = await client.post(f"/harness/agent/sessions/{sid}/prompt", json={"id": "plain", "text": "hello"})
    assert response.status_code == 200 and "text/event-stream" in response.headers["content-type"], response.text
    assert '"type": "result"' in response.text
    for suffix, body in (("tools", {"tools": []}), ("interrupt", {}), ("steer", {"text": "hint"})):
        reply = await client.post(f"/harness/agent/sessions/{sid}/{suffix}", json=body)
        assert reply.status_code not in (404, 500), reply.text


@pytest.mark.asyncio
async def test_stop_after_route_disabled_and_quota_before_send(network, monkeypatch):
    from datetime import datetime, timezone
    from atlas_gate.gate.errors import GateError
    client, app, routers, settings, creds, other = network
    sid = await create(client)
    state = app.state.gate
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    original = state.nodes.request
    async def unavailable(*args, **kwargs):
        raise GateError(503, "node_unavailable", "discovery failed before POST")
    monkeypatch.setattr(state.nodes, "request", unavailable)
    refused = await client.post(f"/harness/agent/sessions/{sid}/prompt?mode=async", json={"id": "refused", "text": "hello"})
    assert refused.status_code == 503
    assert state.store.agent_turns("first", day) == 0
    monkeypatch.setattr(state.nodes, "request", original)
    accepted = await client.post(f"/harness/agent/sessions/{sid}/prompt?mode=async", json={"id": "active", "text": "call:echo"})
    assert accepted.status_code == 202
    await events(client, sid, "active")
    state.orgs["test-org"].route("claude-sub").enabled = False
    stopped = await client.post(f"/harness/agent/sessions/{sid}/interrupt", json={})
    assert stopped.status_code == 200, stopped.text
    terminal = await events(client, sid, "active", True)
    assert next(e for e in terminal if e["type"] == "result")["subtype"] == "interrupted"
