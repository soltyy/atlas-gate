import json
import time
from types import SimpleNamespace

import httpx
import pytest

from atlas_gate.gate.errors import GateError
from atlas_gate.gate.nodes import Nodes
from atlas_gate.gate.operations import Operations
from atlas_gate.gate.routing import Routing
from atlas_gate.gate.store import Store


def setup_nodes(tmp_path, store, groups=("account",)):
    path = tmp_path / "nodes.json"
    path.write_text(json.dumps([
        {"node_id": str(i), "url": "http://127.0.0.1:1", "token_env": "TEST_TOKEN",
         "orgs": ["org"], "routes": ["sub"], "account_group": group}
        for i, group in enumerate(groups)
    ]))
    return Nodes(str(path), store, tokens={"TEST_TOKEN": "test-only"})


@pytest.mark.parametrize("status", ["allowed", "allowed_warning"])
async def test_recovery_clears_shared_account_durably_only(tmp_path, status):
    store = Store(str(tmp_path / "gate.db"))
    nodes = setup_nodes(tmp_path, store, ("account", "account", "other"))
    try:
        for node in ("0", "2"):
            nodes.observe_event(node, {"type": "rate_limit", "status": "rejected", "retryAfterSeconds": 86400})
        await nodes.close()
        # Recovery must also remove a rejection loaded after Gate restart.
        nodes = setup_nodes(tmp_path, store, ("account", "account", "other"))
        assert nodes.cooldown["account"] > time.monotonic()
        for _ in range(2):
            nodes.observe_event("1", {"type": "rate_limit", "status": status})
        assert "account" not in nodes.cooldown
        assert nodes.cooldown["other"] > time.monotonic()
        assert [r["account_group"] for r in store._all("SELECT * FROM router_account_cooldowns")] == ["other"]
        await nodes.close()
        nodes = setup_nodes(tmp_path, store, ("account", "account", "other"))
        assert "account" not in nodes.cooldown
        assert "other" in nodes.cooldown
    finally:
        await nodes.close()
        store.close()


@pytest.mark.parametrize("event", [{}, {"type": "result", "status": "allowed"},
    {"type": "rate_limit", "status": "unknown"}, {"type": "rate_limit", "status": "rejected"}])
async def test_non_authoritative_status_does_not_clear(tmp_path, event):
    store = Store(str(tmp_path / "gate.db"))
    nodes = setup_nodes(tmp_path, store)
    try:
        nodes.observe_event("0", {"type": "rate_limit", "status": "rejected"})
        nodes.observe_event("0", event)
        assert nodes.cooldown["account"] > time.monotonic()
        assert store._one("SELECT * FROM router_account_cooldowns WHERE account_group='account'")
    finally:
        await nodes.close()
        store.close()


@pytest.mark.parametrize("status", ["allowed", "allowed_warning"])
async def test_probe_recovery_unblocks_creation_and_existing_turn(tmp_path, status):
    store = Store(str(tmp_path / "gate.db"))
    nodes = setup_nodes(tmp_path, store)
    quota = {"status": "rejected", "retryAfterSeconds": 86400}
    posts = []

    def router(request):
        if request.url.path == "/v1/node":
            return httpx.Response(200, json={"nodeId": "0", "bootId": "boot", "protocolVersion": 1,
                "capacity": 0, "ready": True, "sessions": 0, "activeTurns": 0,
                "models": [{"value": "codex"}], "capabilities": ["durable_commands", "durable_events"],
                "quota": quota})
        posts.append(request.url.path)
        return httpx.Response(200, json={"id": "local", "tools": 0})

    await nodes.clients["0"].aclose()
    nodes.clients["0"] = httpx.AsyncClient(transport=httpx.MockTransport(router), base_url="http://node")
    device = SimpleNamespace(device_id="device", org="org", user_id="user")
    routing = Routing(store, nodes)
    operations = Operations(store)
    try:
        _, body = await routing.create(device, "sub", "codex", {}, 0)
        binding = routing.get(json.loads(body)["id"], device)
        assert posts == ["/v1/sessions"]
        nodes.observe_event("0", {"type": "rate_limit", "status": "rejected"})
        _, fresh = operations.begin(device, binding, "POST", "/v1/sessions/local/prompt", {}, turn_id="turn", nodes=nodes)
        assert fresh
        # Same logical command remains a replay, even while old quota is rejected.
        _, fresh = operations.begin(device, binding, "POST", "/v1/sessions/local/prompt", {}, turn_id="turn", nodes=nodes)
        assert not fresh
        quota.update(status=status)
        await nodes.refresh()
        assert not store._all("SELECT * FROM router_account_cooldowns")
    finally:
        await nodes.close()
        store.close()


async def test_absolute_reset_replay_restart_and_expiry(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    store = Store(str(tmp_path / "gate.db"))
    nodes = setup_nodes(tmp_path, store)
    event = {"type": "rate_limit", "status": "rejected", "resetsAt": 1100, "retryAfterSeconds": 100}
    try:
        nodes.observe_event("0", event)
        clock[0] = 1099
        nodes.observe_event("0", event)
        assert nodes.cooldown["account"] == 1100
        assert store._one("SELECT until FROM router_account_cooldowns")["until"] == 1100
        await nodes.close()
        nodes = setup_nodes(tmp_path, store)
        clock[0] = 1101
        nodes.observe_event("0", event)
        assert nodes.cooldown.get("account", 0) <= clock[0]
        # A new real rejection with a later reset still blocks.
        nodes.observe_event("0", dict(event, resetsAt=1200))
        nodes.observe_event("0", event)
        assert nodes.cooldown["account"] == 1200
    finally:
        await nodes.close()
        store.close()


async def test_relative_event_and_probe_are_durable_once(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    store = Store(str(tmp_path / "gate.db"))
    nodes = setup_nodes(tmp_path, store)
    event = {"type": "error", "kind": "rate_limit", "id": "turn", "seq": 5, "retryAfterSeconds": 60}
    probe = {"type": "rate_limit", "status": "rejected", "retryAfterSeconds": 60}
    try:
        nodes.observe_event("0", event)
        nodes.observe_event("0", probe)
        clock[0] = 1040
        await nodes.close()
        nodes = setup_nodes(tmp_path, store)
        nodes.observe_event("0", event)
        nodes.observe_event("0", probe)
        assert nodes.cooldown["account"] == 1060
        # An out-of-order allowed event must not erase the later rejection.
        nodes.observe_event("0", {"type": "rate_limit", "status": "allowed", "id": "turn", "seq": 4})
        assert nodes.cooldown["account"] == 1060
        clock[0] = 1061
        nodes.observe_event("0", event)
        nodes.observe_event("0", probe)
        assert nodes.cooldown.get("account", 0) <= clock[0]
        nodes.observe_event("0", dict(event, seq=6))
        assert nodes.cooldown["account"] == 1121
    finally:
        await nodes.close()
        store.close()


async def test_absolute_rejection_not_shortened_by_terminal_error(tmp_path):
    store = Store(str(tmp_path / "gate.db"))
    nodes = setup_nodes(tmp_path, store)
    try:
        reset = time.time() + 600
        nodes.observe_event("0", {"type": "rate_limit", "status": "rejected", "resetsAt": reset, "id": "turn", "seq": 7})
        nodes.observe_event("0", {"type": "error", "kind": "rate_limit", "id": "turn", "seq": 8})
        assert store._one("SELECT until FROM router_account_cooldowns")["until"] == pytest.approx(reset, abs=.1)
    finally:
        await nodes.close()
        store.close()
