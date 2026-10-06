"""Opt-in ChatGPT: независимые Gate/Router, compact и восстановление публичного SID."""
import asyncio
import json
import os
from pathlib import Path
import sys
import httpx
import pytest
from atlas_gate.main import create_app
from atlas_gate.settings import Settings
from .test_network_gate import serve

pytestmark = pytest.mark.skipif(os.environ.get("ATLAS_LIVE_CODEX") != "1", reason="нужен явный ATLAS_LIVE_CODEX=1")
MODEL = "gpt-6.1-sol"
TOOL = {"name": "read_fact", "description": "Получает контрольный факт из клиента. Вызови для получения факта.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}
SYSTEM = "Ты агент EDT. Отвечай кратко. Все действия исполняются только инструментами клиента. Используй atlas_list_tools для актуального каталога."


async def ask(client, sid, turn, text):
    prefix = "/harness/agent/sessions/" + sid
    payload = {"id": turn, "text": text}
    reply = await client.post(prefix + "/prompt?mode=async", json=payload)
    assert reply.status_code == 202, reply.text
    answered = set()
    deadline = asyncio.get_running_loop().time() + 180
    while asyncio.get_running_loop().time() < deadline:
        r = await client.get(prefix + "/turns/" + turn + "/events", params={"wait": 1000})
        assert r.status_code == 200, r.text
        doc = r.json()
        for event in doc["events"]:
            if event["type"] == "tool_call" and event["callId"] not in answered:
                delivered = await client.post(prefix + "/tool_result", json={"callId": event["callId"], "content": "Контрольный факт: КЕДР-729", "isError": False})
                assert delivered.status_code == 200, delivered.text
                answered.add(event["callId"])
        if doc["done"]:
            result = next(e for e in doc["events"] if e["type"] == "result")
            assert result["ok"], result
            return result, doc["events"]
    raise TimeoutError("реальный ход Codex не завершился")


@pytest.mark.asyncio
async def test_real_subscription_compact_router_and_gate_restart(tmp_path, monkeypatch):
    root = os.environ["ATLAS_TEST_ROUTER_ROOT"]
    sys.path.insert(0, root)
    from atlas_router.main import create_app as make_router
    from atlas_router.settings import Settings as RouterSettings
    router_settings = RouterSettings(_env_file=None, ATLAS_BACKEND="codex", ATLAS_GATE_ENABLED=False,
        ATLAS_TRUST_LOCAL=False, ATLAS_TOKEN="test-node", ATLAS_NODE_ID="live-node",
        ATLAS_NODE_DB=str(tmp_path / "node.db"), ATLAS_NODE_SUBAGENTS=0)
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    org = json.loads((Path(__file__).parents[1] / "atlas_gate/gate/orgs/test-org.json").read_text(encoding="utf-8"))
    org["routes"] = [r for r in org["routes"] if r["kind"] == "router-agent"]
    org["models"] = [{"route_id": "claude-sub", "model": MODEL, "display_name": "Codex subscription"}]
    org["pricing"] = []
    org["agents"] = []
    org["mcp"] = []
    org["policy"]["routes_by_class"] = {k: (["claude-sub"] if k in ("public", "internal") else []) for k in org["policy"]["data_classes"]}
    (orgs / "test-org.json").write_text(json.dumps(org), encoding="utf-8")
    nodes_file = tmp_path / "nodes.json"
    monkeypatch.setenv("LIVE_NODE_TEST_TOKEN", "test-node")
    def nodes(url):
        nodes_file.write_text(json.dumps([{"node_id": "live-node", "url": url, "token_env": "LIVE_NODE_TEST_TOKEN", "orgs": ["test-org"], "routes": ["claude-sub"], "account_group": "same-chatgpt-account"}]))
    gate_settings = Settings(_env_file=None, ATLAS_TOKEN="test-admin", ATLAS_GATE_TEST=False,
        ATLAS_GATE_DB=str(tmp_path / "gate.db"), ATLAS_GATE_KEY_PATH=str(tmp_path / "gate-key.pem"),
        ATLAS_GATE_KEYRING_SECRET="test-keyring", ATLAS_GATE_ORGS_DIR=str(orgs),
        ATLAS_GATE_NODES_FILE=str(nodes_file), ATLAS_GATE_KEYS_FILE=str(tmp_path / "no-keys"), ATLAS_GATE_NODE_PKI_DIR=str(tmp_path / "pki"))
    router = make_router(router_settings)
    async with serve(router) as router_url:
        nodes(router_url)
        gate = create_app(gate_settings)
        async with serve(gate) as url, httpx.AsyncClient(base_url=url, timeout=240) as client:
            await gate.state.gate.ready.wait()
            assert MODEL in gate.state.gate.nodes.model_ids("test-org", ["claude-sub"])
            start = (await client.post("/harness/enroll/start", json={"device_name": "isolated-live-test", "platform": "test", "app_version": "1.0"})).json()
            approved = await client.post("/harness/enroll/approve", headers={"X-Atlas-Token": "test-admin"}, json={"user_code": start["user_code"], "org": "test-org", "user": {"id": "tester", "email": "", "name": "test"}})
            assert approved.status_code == 200, approved.text
            credentials = (await client.post("/harness/enroll/poll", json={"device_code": start["device_code"]})).json()
            client.headers["Authorization"] = "Bearer " + credentials["device_token"]
            reply = await client.post("/harness/agent/sessions", json={"system": SYSTEM, "model": MODEL, "tools": [TOOL]}, headers={"Idempotency-Key": "live-create"})
            assert reply.status_code == 200, reply.text
            sid = reply.json()["id"]
            result, events = await ask(client, sid, "fact", "Вызови read_fact и запомни точный контрольный факт. Ответь им.")
            assert "КЕДР-729" in result["text"] and any(e["type"] == "tool_call" for e in events)
            replay = await client.post(f"/harness/agent/sessions/{sid}/prompt?mode=async", json={"id": "fact", "text": "Вызови read_fact и запомни точный контрольный факт. Ответь им."})
            assert replay.status_code == 202
            assert gate.state.gate.store._one("SELECT COUNT(*) n FROM router_usage_receipts")["n"] == 1
            filler = "\n".join(f"Архив {n}: obsolete-{n}-" + "abcdef0123456789" * 4 for n in range(800))
            await ask(client, sid, "archive", "Прочитай архив. Он не нужен для дальнейшей работы. Ответь: принято.\n" + filler)
            compact = await client.post(f"/harness/agent/sessions/{sid}/compact", json={"instructions": "Сохрани точный контрольный факт, полученный read_fact."}, headers={"Idempotency-Key": "live-compact"})
            assert compact.status_code == 200, compact.text
            assert "КЕДР-729" in compact.json()["summary"]
            assert 0 < compact.json()["postTokens"] < compact.json()["preTokens"]
            binding = gate.state.gate.store._one("SELECT * FROM router_bindings WHERE id=?", (sid,))
            native = binding["sdk_id"]
            old_boot = binding["boot_id"]
    # Закрыты оба сервера/SDK; Router восстанавливает по диску без нового клиентского create.
    restarted = make_router(router_settings)
    async with serve(restarted) as router_url:
        nodes(router_url)
        gate = create_app(gate_settings)
        async with serve(gate) as url, httpx.AsyncClient(base_url=url, timeout=240, headers={"Authorization": "Bearer " + credentials["device_token"]}) as client:
            await gate.state.gate.ready.wait()
            context = await client.get(f"/harness/agent/sessions/{sid}/context")
            assert context.status_code == 200, context.text
            restored = gate.state.gate.store._one("SELECT * FROM router_bindings WHERE id=?", (sid,))
            assert restored["node_id"] == "live-node" and restored["boot_id"] != old_boot and restored["sdk_id"] == native
            result, events = await ask(client, sid, "remember", "Какой точный контрольный факт ты получил раньше? Ответь из памяти, без вызова инструментов.")
            assert "КЕДР-729" in result["text"]
            usage = gate.state.gate.store._all("SELECT prompt_tokens,completion_tokens,cost FROM usage WHERE kind='agent'")
            assert len(usage) == 3 and all(u["prompt_tokens"] > 0 and u["completion_tokens"] > 0 and u["cost"] is None for u in usage)
