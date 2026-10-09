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


@pytest.mark.asyncio
async def test_known_subscription_headroom_breaks_equal_load_tie(tmp_path):
    store, nodes = Store(str(tmp_path / "test.db")), FakeNodes(capacity=4)
    nodes.snapshots["a"]["quota"] = {"windows": [{"usedPercent": 90, "resetsAt": time.time() + 1000}]}
    nodes.snapshots["b"]["quota"] = {"windows": [{"usedPercent": 20, "resetsAt": time.time() + 1000}]}
    routing = Routing(store, nodes)
    _, response = await routing.create(device("d"), "sub", "claude", {}, 10)
    assert routing.get(json.loads(response)["id"], device("d"))["node_id"] == "b"
    store.close()


def device(name):
    return SimpleNamespace(device_id=name, org="org", user_id=name)


@pytest.mark.asyncio
async def test_unlimited_capacity_still_chooses_free_node_and_reserves_over_four(tmp_path):
    from atlas_gate.gate.operations import Operations
    store, nodes = Store(str(tmp_path / "unlimited.db")), FakeNodes(capacity=0)
    for name, n in nodes.config.items():
        n.node_id = name
        n.account_turn_capacity = 0
    for info in nodes.snapshots.values():
        info['turnCapacity'] = 0
    nodes.snapshots['a']['activeTurns'] = nodes.snapshots['a']['sessions'] = 8
    routing = Routing(store, nodes)
    _, response = await routing.create(device('free'), 'sub', 'claude', {}, 20)
    binding = routing.get(json.loads(response)['id'], device('free'))
    assert binding['node_id'] == 'b'
    operations = Operations(store)
    for i in range(7):
        operation, fresh = operations.begin(device('free'), binding, 'POST', '/v1/sessions/local/prompt', {}, request_id=str(i), turn_id=str(i), nodes=nodes)
        assert fresh and operation['units'] == 1
    nodes.cooldown['b'] = time.monotonic() + 60
    with pytest.raises(GateError) as limited:
        operations.begin(device('free'), binding, 'POST', '/v1/sessions/local/prompt', {}, request_id='blocked', turn_id='blocked', nodes=nodes)
    assert limited.value.code == 'rate_limit'
    store.close()


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
    assert store._one("SELECT prompt_tokens FROM usage")["prompt_tokens"] is None
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
    nodes.clients["a"] = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"nodeId": "a", "bootId": "boot", "protocolVersion": 1, "models": [{"value": "claude"}], "capacity": 1, "capabilities": ["durable_commands", "durable_events"]})), base_url="http://node")
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


@pytest.mark.asyncio
async def test_explicit_history_fallback_keeps_original_node_affinity(tmp_path):
    store, nodes = Store(str(tmp_path / 'fallback.db')), FakeNodes(capacity=4)
    nodes.snapshots['a']['quota'] = {'windows':[{'usedPercent':0}]}
    nodes.snapshots['b']['quota'] = {'windows':[{'usedPercent':80}]}
    routing = Routing(store, nodes)
    try:
        _, body = await routing.create(device('d'), 'sub', 'claude', {}, 10)
        first = routing.get(json.loads(body)['id'], device('d'))
        assert first['node_id'] == 'a'
        store._exec("UPDATE router_bindings SET sdk_id='native-sdk',status='closed' WHERE id=?", (first['id'],))
        nodes.snapshots['a']['quota']['windows'][0]['usedPercent'] = 80
        nodes.snapshots['b']['quota']['windows'][0]['usedPercent'] = 0
        sent=[]
        original=nodes.request
        async def request(node_id, method, path, **kwargs):
            sent.append((node_id,json.loads(kwargs['content'])))
            response = await original(node_id,method,path,**kwargs)
            return httpx.Response(response.status_code, json={**response.json(), "id": "fallback-local-id"})
        nodes.request=request
        _, body=await routing.create(device('d'),'sub','claude',
            {'resumeSessionId':first['sdk_alias'],'allowHistoryFallback':True},10)
        second=routing.get(json.loads(body)['id'],device('d'))
        assert second['node_id'] == 'a'  # b свободнее, но migration не разрешён.
        assert sent == [('a', {'model':'claude','resumeSessionId':'native-sdk','allowHistoryFallback':True,'sdkTools':{'version':1,'webSearch':'disabled','files':False}})]
        with pytest.raises(GateError) as denied:
            await routing.create(device('other'),'sub','claude',
                {'resumeSessionId':first['sdk_alias'],'allowHistoryFallback':True},10)
        assert denied.value.status == 404
    finally:
        store.close()
