import json
import time
from types import SimpleNamespace
import httpx
import pytest
from atlas_gate.gate.errors import GateError
from atlas_gate.gate.nodes import Node, Nodes
from atlas_gate.gate.routing import Routing
from atlas_gate.gate.store import Store


class FakeNodes:
    def __init__(self, capacity=1):
        self.config = {n: SimpleNamespace(enabled=True, orgs=["org"], routes=["sub"], account_group=n) for n in ("a", "b")}
        self.snapshots = {n: {"ready": True, "bootId": n, "models": [{"value": "claude"}], "capacity": capacity,
                                "sessions": 0, "activeTurns": 0} for n in self.config}
        self.cooldown = {}
        self.fail = False

    async def refresh(self):
        pass

    async def request(self, node_id, method, path, **kwargs):
        import asyncio
        await asyncio.sleep(0.01)
        if self.fail:
            raise GateError(503, "command_outcome_unknown", "lost ACK")
        return httpx.Response(200, json={"id": "same-local-id", "tools": 0})


def device(name):
    return SimpleNamespace(device_id=name, org="org", user_id=name)


@pytest.mark.asyncio
async def test_concurrent_reservations_and_duplicate_local_ids(tmp_path):
    import asyncio
    store = Store(str(tmp_path / "test.db"))
    routing = Routing(store, FakeNodes())
    rows = await asyncio.gather(*(routing.create(device(str(i)), "sub", "claude", {}, 10) for i in range(2)))
    ids = [json.loads(r[1])["id"] for r in rows]
    assert ids[0] != ids[1]
    assert {routing.get(sid, device(str(i)))["node_id"] for i, sid in enumerate(ids)} == {"a", "b"}
    with pytest.raises(GateError) as denied:
        routing.get(ids[0], device("other"))
    assert denied.value.status == 404
    with pytest.raises(GateError) as full:
        await routing.create(device("third"), "sub", "claude", {}, 10)
    assert full.value.code == "capacity_exceeded"
    store.close()
    reopened = Store(str(tmp_path / "test.db"))
    assert Routing(reopened, FakeNodes()).get(ids[0], device("0"))["local_id"] == "same-local-id"
    reopened.close()


@pytest.mark.asyncio
async def test_least_busy_cooldown_and_unknown_ack(tmp_path):
    store, nodes = Store(str(tmp_path / "test.db")), FakeNodes(capacity=4)
    nodes.snapshots["a"]["activeTurns"] = 3
    nodes.snapshots["a"]["sessions"] = 3
    routing = Routing(store, nodes)
    _, raw = await routing.create(device("first"), "sub", "claude", {}, 10)
    assert routing.get(json.loads(raw)["id"], device("first"))["node_id"] == "b"
    nodes.cooldown["b"] = time.monotonic() + 100
    _, raw = await routing.create(device("second"), "sub", "claude", {}, 10)
    assert routing.get(json.loads(raw)["id"], device("second"))["node_id"] == "a"
    nodes.fail = True
    nodes.snapshots["a"]["sessions"] = 0
    with pytest.raises(GateError):
        await routing.create(device("lost"), "sub", "claude", {}, 10)
    with pytest.raises(GateError) as repeated:
        await routing.create(device("lost"), "sub", "claude", {}, 10)
    assert repeated.value.code == "command_outcome_unknown"
    assert store._one("SELECT COUNT(*) n FROM router_bindings WHERE device_id='lost'")["n"] == 1
    store.close()


def test_usage_transaction_deduplicates_and_aliases(tmp_path):
    store = Store(str(tmp_path / "test.db"))
    routing = Routing(store, FakeNodes())
    binding = {"id": "gate-id", "model": "claude", "route": "sub"}
    for _ in range(3):
        routing.usage(binding, "turn", {"usage": {"input_tokens": 7, "output_tokens": 5}}, device("one"))
    assert store._one("SELECT COUNT(*) n FROM usage")["n"] == 1
    assert store._one("SELECT prompt_tokens FROM usage")["prompt_tokens"] == 7
    store.close()


@pytest.mark.parametrize("url", ["http://example.com", "https://example.com/v1", "https://user:pass@example.com", "https://example.com?target=x"])
def test_endpoint_validation(url):
    with pytest.raises(ValueError):
        Node(node_id="a", url=url, token_env="A", orgs=["org"], routes=["sub"], account_group="account")


@pytest.mark.asyncio
async def test_identity_mismatch_excludes_node(tmp_path, monkeypatch):
    monkeypatch.setenv("NODE_SECRET", "dummy")
    path = tmp_path / "nodes.json"
    path.write_text(json.dumps([{"node_id": "a", "url": "http://127.0.0.1:1", "token_env": "NODE_SECRET", "orgs": ["org"], "routes": ["sub"], "account_group": "account"}]))
    nodes = Nodes(str(path))
    await nodes.clients["a"].aclose()
    nodes.clients["a"] = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"nodeId": "b", "bootId": "boot", "protocolVersion": 1})), base_url="http://node")
    assert await nodes.probe("a") is None
    assert not nodes.snapshots
    await nodes.close()


@pytest.mark.asyncio
async def test_catalog_survives_outage_but_respects_org(tmp_path, monkeypatch):
    monkeypatch.setenv("NODE_SECRET", "dummy")
    path = tmp_path / "nodes.json"
    path.write_text(json.dumps([{"node_id": "a", "url": "http://127.0.0.1:1", "token_env": "NODE_SECRET", "orgs": ["org"], "routes": ["sub"], "account_group": "account"}]))
    store = Store(str(tmp_path / "test.db"))
    nodes = Nodes(str(path), store)
    await nodes.clients["a"].aclose()
    nodes.clients["a"] = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"nodeId": "a", "bootId": "boot", "protocolVersion": 1, "models": [{"value": "claude"}], "capacity": 1})), base_url="http://node")
    assert await nodes.probe("a")
    assert nodes.model_ids("org", ["sub"]) == {"claude"}
    assert nodes.model_ids("other", ["sub"]) == set()
    await nodes.close()
    restarted = Nodes(str(path), store)
    assert await restarted.probe("a") is None
    assert restarted.model_ids("org", ["sub"]) == {"claude"}
    assert not restarted.snapshots
    await restarted.close()
    store.close()
