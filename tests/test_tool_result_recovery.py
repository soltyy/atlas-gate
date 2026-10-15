"""Gate #50: настоящий HTTP Gate → два Router, SDK — сценарный stub."""
import asyncio
import httpx
import pytest

from tests.test_network_gate import create, events, network  # noqa: F401 — pytest fixture


async def waiting_tool(client):
    sid = await create(client)
    prefix = f"/harness/agent/sessions/{sid}"
    accepted = await client.post(prefix + "/prompt?mode=async", json={"id": "delivery", "text": "call:echo"})
    assert accepted.status_code == 202, accepted.text
    call = next(e for e in await events(client, sid, "delivery") if e["type"] == "tool_call")
    return sid, prefix, {"callId": call["callId"], "content": "результат сохранён"}


def lose_delivery(state, binding, monkeypatch, after_acceptance=False):
    client = state.nodes.clients[binding["node_id"]]
    original = client.request
    attempts = []
    accepted = []

    async def request(method, path, **kwargs):
        if method == "POST" and path.endswith("/tool_result"):
            attempts.append(kwargs["headers"]["Idempotency-Key"])
            if len(attempts) == 1:
                if after_acceptance:
                    result = await original(method, path, **kwargs)
                    assert result.status_code == 200
                    accepted.append(result)
                raise httpx.ReadError("fault injection: lost transport ACK", request=httpx.Request(method, "http://router" + path))
            accepted.append(await original(method, path, **kwargs))
            return accepted[-1]
        return await original(method, path, **kwargs)

    monkeypatch.setattr(client, "request", request)
    return attempts, accepted


@pytest.mark.parametrize("after_acceptance", [False, True])
async def test_tool_result_recovers_missing_command_or_lost_ack_without_duplicate(network, monkeypatch, after_acceptance):
    client, app, routers, _, _, other = network
    state = app.state.gate
    sid, prefix, payload = await waiting_tool(client)
    binding = state.store._one("SELECT * FROM router_bindings WHERE id=?", (sid,))
    attempts, accepted = lose_delivery(state, binding, monkeypatch, after_acceptance)
    first = await client.post(prefix + "/tool_result", json=payload)
    assert first.status_code == 503, first.text
    assert first.json()["error"]["code"] == "command_outcome_unknown"
    op = state.store._one("SELECT * FROM router_operations WHERE session_id=? AND path LIKE '%/tool_result'", (sid,))
    assert op["boot_id"] == binding["boot_id"]
    denied = await client.post(prefix + "/tool_result", json=payload, headers={"Authorization": "Bearer " + other["device_token"]})
    assert denied.status_code == 404
    conflict = await client.post(prefix + "/tool_result", json=dict(payload, content="other"))
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    replies = await asyncio.gather(*(client.post(prefix + "/tool_result", json=payload) for _ in range(4)))
    assert [r.status_code for r in replies] == [200] * 4
    assert len(accepted) == 1
    assert len(attempts) == (1 if after_acceptance else 2)
    assert set(attempts) == {op["id"]}
    assert state.store._one("SELECT state FROM router_operations WHERE id=?", (op["id"],))["state"] == "complete"
    final = next(e for e in await events(client, sid, "delivery", True) if e["type"] == "result")
    assert final["ok"] and final["text"].endswith(payload["content"])
    assert state.store._one("SELECT node_id,status FROM router_bindings WHERE id=?", (sid,)) == {"node_id": binding["node_id"], "status": "ready"}


@pytest.mark.parametrize("receipt_state", ["running", "unavailable", "malformed"])
async def test_tool_result_unconfirmed_receipt_is_retryable_without_redelivery(network, monkeypatch, receipt_state):
    client, app, *_ = network
    state = app.state.gate
    sid, prefix, payload = await waiting_tool(client)
    binding = state.store._one("SELECT * FROM router_bindings WHERE id=?", (sid,))
    attempts, _ = lose_delivery(state, binding, monkeypatch)
    assert (await client.post(prefix + "/tool_result", json=payload)).status_code == 503
    upstream = state.nodes.clients[binding["node_id"]]
    original = upstream.request

    async def pending(method, path, **kwargs):
        if path.startswith("/v1/commands/"):
            if receipt_state == "unavailable":
                return httpx.Response(503, json={})
            return httpx.Response(200, json=["bad"] if receipt_state == "malformed" else {"state": "running"})
        return await original(method, path, **kwargs)

    monkeypatch.setattr(upstream, "request", pending)
    response = await client.post(prefix + "/tool_result", json=payload)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "command_outcome_unknown"
    assert len(attempts) == 1
    monkeypatch.setattr(upstream, "request", original)
    assert (await client.post(prefix + "/tool_result", json=payload)).status_code == 200


@pytest.mark.parametrize("guard", ["changed_boot", "legacy_boot", "no_durable_commands"])
async def test_tool_result_missing_receipt_never_replays_without_original_boot_evidence(network, monkeypatch, guard):
    client, app, *_ = network
    state = app.state.gate
    sid, prefix, payload = await waiting_tool(client)
    binding = state.store._one("SELECT * FROM router_bindings WHERE id=?", (sid,))
    attempts, _ = lose_delivery(state, binding, monkeypatch)
    assert (await client.post(prefix + "/tool_result", json=payload)).status_code == 503
    if guard == "legacy_boot":
        state.store._exec("UPDATE router_operations SET boot_id=NULL WHERE session_id=? AND path LIKE '%/tool_result'", (sid,))
    original_probe = state.nodes.probe

    async def probe(node):
        info = await original_probe(node)
        if guard == "changed_boot":
            return dict(info, bootId="other-boot")
        if guard == "no_durable_commands":
            return dict(info, capabilities=[])
        return info

    monkeypatch.setattr(state.nodes, "probe", probe)
    response = await client.post(prefix + "/tool_result", json=payload)
    assert response.status_code == 503, response.text
    assert len(attempts) == 1
