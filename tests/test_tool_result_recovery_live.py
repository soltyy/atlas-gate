"""Real Gate and Router HTTP with a scripted producer and a cut response."""

import asyncio

import httpx
import pytest

from atlas_gate.gate.operations import body_sha256
from .test_network_gate import create, network  # noqa: F401


@pytest.mark.parametrize("failure", ["before", "after", "stop", "started", "stop_race", "context_error"])
async def test_recover_exact_parallel_tool_result(network, monkeypatch, failure):
    from atlas_router.backends.stub import StubSession

    client, app, routers, _, _, other = network
    state = app.state.gate
    sid = await create(client)
    binding = state.store._one("SELECT * FROM router_bindings WHERE id=?", (sid,))
    node = binding["node_id"]
    router = routers[0 if node == "a" else 1]
    prefix, turn = f"/harness/agent/sessions/{sid}", "parallel"
    call, sibling = "child-a", "child-b"
    body = {"callId": call, "content": "durable output", "isError": False}
    key = f"tool-result:{sid}:{call}"
    recovery = f"{prefix}/turns/{turn}/tool-result-recovery"
    recovery_query = {"callId": call, "bodySha256": body_sha256(body)}
    retry_headers = {"Idempotency-Key": key, "X-Atlas-Tool-Result-Turn": turn}
    effects, prompts = [], []
    actual_request = state.nodes.clients[node].request
    actual_tool_result = StubSession.tool_result
    cut = False
    send_count = 0

    async def scripted(self, turn_id, text, attachments=None):
        prompts.append(turn_id)
        yield {"type": "session", "id": turn_id, "sessionId": self.session_id, "model": self.model}
        pending = [self._pending.open(call), self._pending.open(sibling)]
        for call_id in (call, sibling):
            yield {"type": "tool_call", "id": turn_id, "callId": call_id, "name": "echo", "input": {}}
        await asyncio.gather(*pending)
        if self._interrupt.is_set():
            yield {"type": "result", "id": turn_id, "ok": False, "subtype": "interrupted",
                   "code": "turn_interrupted", "text": ""}
        elif failure == "context_error":
            yield {"type": "result", "id": turn_id, "ok": False, "code": "context_length_exceeded",
                   "context": {"totalTokens": 998_000, "maxTokens": 1_000_000, "percentage": 99.8,
                               "model": "stub-fast"}, "text": "",
                   "usage": {"input_tokens": 10, "output_tokens": 2}}
        else:
            yield {"type": "result", "id": turn_id, "ok": True, "text": "done",
                   "usage": {"input_tokens": 10, "output_tokens": 2}}

    async def tool_result(self, call_id, content, is_error):
        effects.append(call_id)
        return await actual_tool_result(self, call_id, content, is_error)

    async def broken_request(method, url, **kwargs):
        nonlocal cut, send_count
        if method == "POST" and url.endswith("/tool_result") and call.encode() in kwargs["content"]:
            send_count += 1
            if not cut:
                cut = True
                if failure == "after":
                    assert (await actual_request(method, url, **kwargs)).status_code == 200
                if failure == "started":
                    router.state.journal.accept(kwargs["headers"]["Idempotency-Key"], "fixture ambiguous admission")
                raise httpx.RemoteProtocolError("response unavailable")
            if failure == "stop_race":
                interrupt = await actual_request("POST", url.removesuffix("/tool_result") + "/interrupt")
                assert interrupt.json()["settled"]
        return await actual_request(method, url, **kwargs)

    monkeypatch.setattr(StubSession, "_turn", scripted)
    monkeypatch.setattr(StubSession, "tool_result", tool_result)
    monkeypatch.setattr(state.nodes.clients[node], "request", broken_request)
    assert (await client.post(prefix + "/prompt?mode=async", json={"id": turn, "text": "parallel"})).status_code == 202
    async with asyncio.timeout(8):
        while True:
            page = await client.get(f"{prefix}/turns/{turn}/events", params={"wait": 100})
            if len([e for e in page.json()["events"] if e["type"] == "tool_call"]) == 2:
                break
            await asyncio.sleep(.01)
    settings = (await client.get("/harness/settings")).json()["profile"]
    assert settings["toolResultRecovery"]["version"] == 1
    turn_status = (await client.get(f"{prefix}/turns/{turn}")).json()
    assert turn_status["toolCallbacks"] == {
        "version": 1, "sessionId": sid, "turnId": turn, "seq": turn_status["seq"],
        "nodeId": node, "bootId": binding["boot_id"], "pendingCallIds": [call, sibling],
    }
    assert (await client.post(prefix + "/tool_result", json={"callId": sibling, "content": "other"})).status_code == 200
    first = await client.post(prefix + "/tool_result", json=body, headers={"Idempotency-Key": key})
    assert first.status_code == 503 and first.json()["error"]["code"] == "command_outcome_unknown"
    if failure == "stop":
        assert (await client.post(prefix + "/interrupt")).json()["settled"]
    turn_status = (await client.get(f"{prefix}/turns/{turn}")).json()
    if failure in ("before", "started", "stop_race", "context_error"):
        assert turn_status["toolCallbacks"]["pendingCallIds"] == [call]
    else:
        assert turn_status["done"] and "toolCallbacks" not in turn_status

    result = (await client.get(recovery, params=recovery_query)).json()
    assert result["requestId"] == key and result["bodySha256"] == body_sha256(body)
    assert result["outcome"] == ("accepted" if failure == "after" else "unknown")
    assert result["retryAllowed"] is (failure in ("before", "stop_race", "context_error"))
    wrong = await client.get(recovery.replace(f"/{turn}/", "/wrong/"), params=recovery_query)
    assert wrong.json()["outcome"] == "unknown" and wrong.json()["retryAllowed"] is False
    foreign = await client.get(recovery, params=recovery_query,
                               headers={"Authorization": "Bearer " + other["device_token"]})
    assert foreign.status_code == 404
    changed = await client.post(prefix + "/tool_result", json=dict(body, content="changed"), headers=retry_headers)
    assert changed.status_code == 409

    if failure == "before":
        fake_boot = dict(binding, boot_id="wrong-boot")
        from atlas_gate.gate.agent import _tool_recovery_state
        op = state.store._one("SELECT * FROM router_operations WHERE id=?", (result["commandId"],))
        assert await _tool_recovery_state(state, fake_boot, op, turn, call) is None
    replay = await client.post(prefix + "/tool_result", json=body, headers=retry_headers)
    if failure in ("before", "after", "context_error"):
        assert replay.status_code == 200 and replay.json()["status"] == "accepted"
        again = await client.post(prefix + "/tool_result", json=body, headers=retry_headers)
        assert again.content == replay.content
        assert effects.count(call) == 1 and effects.count(sibling) == 1
        async with asyncio.timeout(8):
            while not (await client.get(f"{prefix}/turns/{turn}/events")).json()["done"]:
                await asyncio.sleep(.01)
        if failure == "context_error":
            terminal = (await client.get(f"{prefix}/turns/{turn}/receipt")).json()
            assert terminal["code"] == "context_length_exceeded" and terminal["ok"] is False
            assert (await client.get(recovery, params=recovery_query)).json()["outcome"] == "accepted"
        assert state.store._one("SELECT COUNT(*) n FROM router_usage_receipts")["n"] == 1
    elif failure == "stop_race":
        assert replay.status_code == 409 and replay.json()["status"] == "recovery_unconfirmed"
        assert (await client.get(recovery, params=recovery_query)).json()["outcome"] == "unknown"
        assert effects == [sibling]
    else:
        assert replay.status_code == 503
        assert effects == [sibling]
        if failure == "started":
            await client.post(prefix + "/interrupt")
    assert send_count == (2 if failure in ("before", "stop_race", "context_error") else 1)
    assert prompts == [turn]
