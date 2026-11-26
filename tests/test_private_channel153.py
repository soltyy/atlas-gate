"""Accepted turn through a real disappearing TCP listener. Only the model SDK is scripted."""
import asyncio
from urllib.parse import urlsplit

import httpx
import pytest

from .test_network_gate import create, events, network, serve


@pytest.mark.parametrize("backend", ["codex", "claude"])
async def test_bound_turn_survives_private_channel_outage(network, monkeypatch, tmp_path, backend):
    client, app, routers, _, _, foreign = network
    state = app.state.gate

    async def signed_in():
        return {"loggedIn": True}

    node = "a" if backend == "codex" else "b"
    peer = "b" if node == "a" else "a"
    owner = routers[0 if node == "a" else 1]
    monkeypatch.setattr(owner.state.backend, "name", backend)
    monkeypatch.setattr(owner.state.cli, "auth_status", signed_in)
    state.nodes.config[node].backend = backend
    state.nodes.config[peer].enabled = False
    sid = await create(client)
    state.nodes.config[peer].enabled = True
    binding = state.store._one("SELECT * FROM router_bindings WHERE id=?", (sid,))
    assert binding["node_id"] == node
    sdk_session = owner.state.registry.get(binding["local_id"]).session
    sdk_id = sdk_session.session_id
    turn = "turn-channel-outage"
    prompt_key = "prompt-before-outage"
    prefix = f"/harness/agent/sessions/{sid}"
    event_url = f"{prefix}/turns/{turn}/events"
    result_ready = asyncio.Event()
    native_turn = sdk_session._turn
    upstream_commands = []

    async def count_business_commands(request):
        if request.method == "POST" and request.url.path.endswith(("/prompt", "/tool_result")):
            upstream_commands.append(request.url.path)

    for upstream in state.nodes.clients.values():
        upstream.event_hooks["request"].append(count_business_commands)

    async def pause_before_result(*args):
        async for event in native_turn(*args):
            if event["type"] == "result":
                await result_ready.wait()
            yield event

    monkeypatch.setattr(sdk_session, "_turn", pause_before_result)
    # Shut down only the private listener. The Router process, journal and actual
    # session stay alive, as they do when just the SSH channel goes away.
    async with serve(owner, lifespan="off", timeout_graceful_shutdown=0) as link:
        state.nodes.clients[node].base_url = link
        port = urlsplit(link).port

        async def lose_ack(response):
            assert response.status_code == 202
            await response.aread()
            await response.aclose()
            raise httpx.ReadError("client lost its admission ACK", request=response.request)

        client.event_hooks["response"].append(lose_ack)
        try:
            with pytest.raises(httpx.ReadError, match="lost its admission ACK"):
                await client.post(prefix + "/prompt?mode=async", json={"id": turn, "text": "call:echo"},
                                  headers={"Idempotency-Key": prompt_key})
        finally:
            client.event_hooks["response"].remove(lose_ack)

        first_events = await events(client, sid, turn)
        cursor = first_events[-1]["seq"]
        call, = [event for event in first_events if event["type"] == "tool_call"]
        alias, = [event["sessionId"] for event in first_events if event["type"] == "session"]
        assert alias == binding["sdk_alias"]
        executed_call_id = call["callId"]
        marker = tmp_path / "host-action"
        with marker.open("x", encoding="utf-8") as out:
            out.write(executed_call_id)
        reply = await client.post(prefix + "/tool_result",
                                  json={"callId": executed_call_id, "content": marker.read_text(encoding="utf-8")},
                                  headers={"Idempotency-Key": "result-before-outage"})
        assert reply.status_code == 200, reply.text
        healthy_sid = await create(client)
        assert state.store._one("SELECT node_id FROM router_bindings WHERE id=?", (healthy_sid,))["node_id"] == peer

    result_ready.set()
    async with asyncio.timeout(10):
        while not owner.state.journal.turn(binding["local_id"], turn)["done"]:
            await asyncio.sleep(0.01)
    assert await state.nodes.probe(node) is None
    interrupted = await client.get(event_url, params={"after": cursor, "wait": 0})
    assert interrupted.status_code == 503, interrupted.text
    error = interrupted.json()["error"]
    assert (error["code"], error["reason"], error["phase"], error["cause"]) == (
        "node_unavailable", "transport_unavailable", "probe", "ConnectError")
    assert error["correlationId"]
    assert (error["operation"], error["sessionId"], error["turnId"]) == ("agent_events", sid, turn)
    foreign_headers = {"Authorization": "Bearer " + foreign["device_token"]}
    assert (await client.get(event_url, headers=foreign_headers)).status_code == 404
    receipt = await client.get(prefix + "/commands", params={"requestId": prompt_key})
    assert receipt.status_code == 202, receipt.text
    assert receipt.headers["X-Atlas-Command-State"] == "complete"
    assert receipt.json()["turnId"] == turn
    assert (await client.get(f"/harness/agent/sessions/{healthy_sid}/context")).status_code == 200

    async with serve(owner, port=port, lifespan="off"):
        async with asyncio.timeout(15):
            while True:
                resumed = await client.get(event_url, params={"after": cursor, "wait": 0})
                if resumed.status_code == 200:
                    break
                assert resumed.status_code == 503, resumed.text
                await asyncio.sleep(0.05)
        end = resumed.json()
        assert end["done"] and end["state"] == "done"
        assert end["events"][-1]["text"].endswith(executed_call_id)
        combined = first_events + end["events"]
        assert [e["seq"] for e in combined] == list(range(1, end["nextAfter"] + 1))
        assert [e["callId"] for e in combined if e["type"] == "tool_call"] == [executed_call_id]
        assert (await client.get(event_url, params={"after": cursor})).json() == end

    current = state.store._one("SELECT * FROM router_bindings WHERE id=?", (sid,))
    assert (current["node_id"], current["local_id"], current["sdk_alias"]) == (node, binding["local_id"], alias)
    assert owner.state.registry.get(current["local_id"]).session.session_id == sdk_id
    assert sdk_session._turns == sdk_session._calls == 1
    assert upstream_commands == [f"/v1/sessions/{binding['local_id']}/{op}"
                                 for op in ("prompt", "tool_result")]
    assert state.store._one("SELECT COUNT(*) n FROM router_usage_receipts")["n"] == 1
    assert state.store._one("SELECT COUNT(*) n FROM usage WHERE kind='agent'")["n"] == 1
    operations = state.store._all("SELECT id, state FROM router_operations WHERE session_id=?", (sid,))
    assert len(operations) == 2 and all(op["state"] == "complete" for op in operations)
    assert receipt.headers["X-Atlas-Command-Id"] in {op["id"] for op in operations}
