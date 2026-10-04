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
async def serve(app, **options):
    options.setdefault("timeout_graceful_shutdown", 5)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", **options))
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
        yield f"{'https' if options.get('ssl_certfile') else 'http'}://127.0.0.1:{port}"
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
                             ATLAS_TRUST_LOCAL=False, ATLAS_TOKEN="test-router", ATLAS_NODE_ID=name, ATLAS_NODE_CAPACITY=4,
                             ATLAS_NODE_DB=str(tmp_path / (name + "-node.db"))))
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
        settings.ATLAS_GATE_NODE_PKI_DIR = str(tmp_path / "pki")
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


@pytest.mark.asyncio
async def test_idle_close_releases_device_slot_and_offline_revoke_retries(network, monkeypatch):
    from atlas_gate.gate.api import revoke_device
    from atlas_gate.gate.errors import GateError
    client, app, routers, settings, creds, other = network
    state = app.state.gate
    settings.ATLAS_GATE_AGENT_SESSIONS_PER_DEVICE = 1
    sid = await create(client)
    binding = state.store._one("SELECT * FROM router_bindings WHERE id=?", (sid,))
    native = routers[0 if binding["node_id"] == "a" else 1]
    # Штатное удаление в Router (например idle sweeper), Gate ещё считает сессию живой.
    await native.state.registry.remove(binding["local_id"])
    replacement = await create(client)
    assert replacement != sid
    assert state.store._one("SELECT status FROM router_bindings WHERE id=?", (sid,))["status"] == "closed"
    original = state.nodes.request
    async def offline_delete(node, method, path, **kwargs):
        if method == "DELETE":
            raise GateError(503, "node_unavailable", "test disconnected node")
        return await original(node, method, path, **kwargs)
    monkeypatch.setattr(state.nodes, "request", offline_delete)
    await revoke_device(state, creds["device_id"])
    assert (await client.get("/harness/profile")).status_code == 401
    assert state.store._one("SELECT status FROM router_bindings WHERE id=?", (replacement,))["status"] == "closing"
    monkeypatch.setattr(state.nodes, "request", original)
    await state.routing.sync_bindings()
    assert state.store._one("SELECT status FROM router_bindings WHERE id=?", (replacement,))["status"] == "closed"
    assert sum(len(r.state.registry.list()) for r in routers) == 0


@pytest.mark.asyncio
async def test_usage_recovery_after_event_retention(network, monkeypatch):
    from atlas_gate.gate import agent
    client, app, routers, settings, creds, other = network
    state = app.state.gate
    async def offline_watcher(*args):
        return
    monkeypatch.setattr(agent, "_watch", offline_watcher)
    sid = await create(client)
    binding = state.store._one("SELECT * FROM router_bindings WHERE id=?", (sid,))
    started = await client.post(f"/harness/agent/sessions/{sid}/prompt?mode=async", json={"id": "retained", "text": "hello"})
    assert started.status_code == 202
    async with httpx.AsyncClient(base_url=state.nodes.config[binding["node_id"]].url, headers={"X-Atlas-Token": "test-router"}) as native_client:
        for _ in range(100):
            answer = await native_client.get(f"/v1/sessions/{binding['local_id']}/turns/retained/events", params={"wait": 100})
            if answer.json()["done"]:
                break
        else:
            raise AssertionError("native terminal event missing")
    assert state.store._one("SELECT COUNT(*) n FROM usage WHERE kind='agent'")["n"] == 0
    native = routers[0 if binding["node_id"] == "a" else 1]
    native.state.journal.purge(-1)
    for _ in range(2):
        expired = await client.get(f"/harness/agent/sessions/{sid}/turns/retained/events")
        assert expired.status_code == 410, expired.text
    assert state.store._one("SELECT COUNT(*) n FROM usage WHERE kind='agent'")["n"] == 1
    operation = state.store._one("SELECT finished,accounted FROM router_operations WHERE session_id=? AND turn_id='retained'", (sid,))
    assert operation["finished"] == operation["accounted"] == 1


@pytest.mark.asyncio
async def test_shared_account_capacity_and_drain(network):
    client, app, routers, settings, creds, other = network
    # Сессии открываются до нагрузки; аккаунт общий, поэтому другой узел не обходит лимит.
    sids = [await create(client) for _ in range(2)]
    state = app.state.gate
    for node in state.nodes.config.values():
        node.account_group = "shared"
        node.account_turn_capacity = 1
    first = await client.post(f"/harness/agent/sessions/{sids[0]}/prompt?mode=async", json={"id": "busy", "text": "call:echo"})
    assert first.status_code == 202, first.text
    call = next(e for e in await events(client, sids[0], "busy") if e["type"] == "tool_call")
    denied = await client.post(f"/harness/agent/sessions/{sids[1]}/prompt?mode=async", json={"id": "later", "text": "hello"})
    assert denied.status_code == 429, denied.text
    assert (await client.post("/harness/agent/sessions", json={"system": "test", "model": "stub-fast", "tools": []})).status_code == 503
    await client.post(f"/harness/agent/sessions/{sids[0]}/tool_result", json={"callId": call["callId"], "content": "ok"})
    await events(client, sids[0], "busy", True)
    accepted = await client.post(f"/harness/agent/sessions/{sids[1]}/prompt?mode=async", json={"id": "later", "text": "hello"})
    assert accepted.status_code == 202, accepted.text


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


@pytest.mark.asyncio
async def test_lost_create_and_prompt_ack_reconciles_without_duplicates(network, monkeypatch):
    from atlas_gate.gate.errors import GateError
    from datetime import datetime, timezone
    client, app, routers, settings, creds, other = network
    state = app.state.gate
    original = state.nodes.request
    lost = {"create": False, "prompt": False}
    async def lose_ack(node, method, path, **kwargs):
        response = await original(node, method, path, **kwargs)
        kind = "create" if path == "/v1/sessions" and method == "POST" else "prompt" if path.endswith("/prompt") else None
        if kind and not lost[kind]:
            lost[kind] = True
            raise GateError(503, "command_outcome_unknown", "lost response after acceptance")
        return response
    monkeypatch.setattr(state.nodes, "request", lose_ack)
    payload = {"system": "test", "model": "stub-fast", "tools": []}
    initial = await client.post("/harness/agent/sessions", json=payload, headers={"Idempotency-Key": "create-once"})
    assert initial.status_code == 503
    retry = await client.post("/harness/agent/sessions", json=payload, headers={"Idempotency-Key": "create-once"})
    assert retry.status_code == 200, retry.text
    sid = retry.json()["id"]
    assert sum(len(r.state.registry.list()) for r in routers) == 1
    first = await client.post(f"/harness/agent/sessions/{sid}/prompt?mode=async", json={"id": "once", "text": "one"})
    assert first.status_code == 503
    repeated = await client.post(f"/harness/agent/sessions/{sid}/prompt?mode=async", json={"id": "once", "text": "one"})
    assert repeated.status_code == 202, repeated.text
    await events(client, sid, "once", True)
    assert sum(e.turns for r in routers for e in r.state.registry._entries.values()) == 1
    assert state.store.agent_turns("first", datetime.now(timezone.utc).strftime("%Y-%m-%d")) == 1
    assert state.store._one("SELECT COUNT(*) n FROM usage WHERE kind='agent'")["n"] == 1


@pytest.mark.asyncio
async def test_outbound_connector_enrollment_replay_tools_and_revocation(network, tmp_path, monkeypatch):
    from atlas_router.connector import Connector, private_key, csr_for, save_identity
    from atlas_gate.gate.node_channel import ChannelClient
    from atlas_gate.gate.nodes import Node
    client, app, routers, settings, creds, other = network
    state = app.state.gate
    node = state.nodes.config["a"]
    router_url = node.url
    await state.nodes.clients["a"].aclose()
    state.nodes.config["a"] = Node(node_id="a", transport="connector", orgs=node.orgs, routes=node.routes, account_group=node.account_group)
    state.nodes.clients["a"] = ChannelClient(state.channel, "a")
    state.nodes.config["b"].enabled = False
    admin = {"X-Atlas-Token": "admin"}
    assert (await client.post("/harness/admin/nodes/a/invite")).status_code == 401
    invite = (await client.post("/harness/admin/nodes/a/invite", headers=admin)).json()["invitation"]
    root = tmp_path / "connector"
    key = private_key(root)
    registered = await client.post("/node/enroll", json={"invitation": invite, "csr": csr_for(key, "a")})
    assert registered.status_code == 200, registered.text
    assert (await client.post("/node/enroll/poll", json={"invitation": invite})).json()["state"] == "pending"
    rid = (await client.get("/harness/admin/node-enrollments", headers=admin)).json()["enrollments"][0]["id"]
    approved = await client.post(f"/harness/admin/node-enrollments/{rid}/approve", headers=admin)
    assert approved.status_code == 200, approved.text
    identity = (await client.post("/node/enroll/poll", json={"invitation": invite})).json()
    save_identity(root, key, "a", str(client.base_url), identity)
    connector = Connector(root, router_url, "test-router")
    original_call = connector.call
    lost = False
    async def lose_result_ack(method, path, body=b""):
        nonlocal lost
        result = await original_call(method, path, body)
        if path.endswith("/result") and not lost:
            lost = True
            raise httpx.ReadError("ACK lost after Gate persisted reply")
        return result
    monkeypatch.setattr(connector, "call", lose_result_ack)
    running = asyncio.create_task(connector.run())
    try:
        sid = await create(client)
        assert state.routing.get(sid, type("D", (), {"device_id": creds["device_id"], "org": "test-org"})())["node_id"] == "a"
        started = await client.post(f"/harness/agent/sessions/{sid}/prompt?mode=async", json={"id": "nat", "text": "call:echo"})
        assert started.status_code == 202, started.text
        call = next(e for e in await events(client, sid, "nat") if e["type"] == "tool_call")
        delivered = await client.post(f"/harness/agent/sessions/{sid}/tool_result", json={"callId": call["callId"], "content": "NAT OK"})
        assert delivered.status_code == 200, delivered.text
        assert next(e for e in await events(client, sid, "nat", True) if e["type"] == "result")["text"].endswith("NAT OK")
        assert sum(e.turns for r in routers for e in r.state.registry._entries.values()) == 1
        assert lost
        # Политика не выдаётся вместе с credential: файл Gate остаётся источником прав.
        assert "orgs" not in identity and "routes" not in identity
        # Переключаем только тестовый канал на отдельный mTLS ingress того же Gate state.
        import ssl
        from atlas_gate.main import create_node_app
        from atlas_gate.node_pki import issue_server
        cert, server_key = issue_server(settings.ATLAS_GATE_NODE_PKI_DIR, tmp_path / "tls", ["127.0.0.1"])
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        settings.ATLAS_GATE_NODE_TLS_PORT = 8767
        async with serve(create_node_app(app), ssl_certfile=str(cert), ssl_keyfile=str(server_key), ssl_ca_certs=str(Path(settings.ATLAS_GATE_NODE_PKI_DIR) / "ca.pem"), ssl_cert_reqs=ssl.CERT_REQUIRED) as tls_url:
            context = ssl.create_default_context(cafile=str(root / "node-ca.pem"))
            async with httpx.AsyncClient(verify=context, timeout=3) as untrusted:
                with pytest.raises(httpx.HTTPError):
                    await untrusted.get(tls_url + "/node/work")
            connector = Connector(root, router_url, "test-router", ca_file=str(root / "node-ca.pem"), mtls=True, node_gate=tls_url)
            original_call = connector.call
            running = asyncio.create_task(connector.run())
            try:
                s2 = await create(client)
                accepted = await client.post(f"/harness/agent/sessions/{s2}/prompt?mode=async", json={"id": "tls", "text": "TLS works"})
                assert accepted.status_code == 202, accepted.text
                await events(client, s2, "tls", True)
                await client.post("/harness/admin/nodes/a/revoke", headers=admin)
                with pytest.raises(httpx.HTTPStatusError) as denied:
                    await original_call("GET", "/node/work")
                assert denied.value.response.status_code == 401
            finally:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
        settings.ATLAS_GATE_NODE_TLS_PORT = 0
        await client.post("/harness/admin/nodes/a/revoke", headers=admin)
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
