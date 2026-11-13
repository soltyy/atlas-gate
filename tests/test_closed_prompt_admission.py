"""Exercise the closed-source fence through Gate and the installed Router wire."""
import asyncio
import time

import httpx
import pytest

from atlas_gate.gate.operations import body_sha256
from .test_network_gate import create, events, network  # noqa: F401


@pytest.mark.parametrize("evidence", ["missing", "lost_ack", "expired", "other_key", "other_boot", "unavailable"])
async def test_closed_prompt_admission(network, monkeypatch, evidence):
    client, app, routers, _, credentials, other = network
    state = app.state.gate
    sid = await create(client)
    path = f"/harness/agent/sessions/{sid}"
    binding = state.store._one("SELECT * FROM router_bindings WHERE id=?", (sid,))
    node = binding["node_id"]
    local = binding["local_id"]
    router = routers[0 if node == "a" else 1]
    journal = router.state.journal
    prior = await client.post(path + "/prompt?mode=async", json={"id": "history", "text": "remember this"})
    assert prior.status_code == 202
    history = await events(client, sid, "history", terminal=True)
    alias = next(event["sessionId"] for event in history if event["type"] == "session")
    body = {"id": "lost", "text": "continue with history"}
    key = "prompt:lost"
    rid = state.operations.key(type("Owner", (), {"device_id": credentials["device_id"]}), key)
    headers = {"Idempotency-Key": key}
    query = {"version": 1, "requestId": key, "turnId": body["id"], "bodySha256": body_sha256(body)}
    upstream = state.nodes.clients[node]
    original = upstream.request
    original_posts = []

    async def lose_request(method, target, **kwargs):
        if method == "POST" and target == f"/v1/sessions/{local}/prompt":
            original_posts.append(kwargs["headers"]["Idempotency-Key"])
            if evidence in ("lost_ack", "expired"):
                result = await original(method, target, **kwargs)
                assert result.status_code == 202
            raise httpx.RemoteProtocolError("connection lost")
        return await original(method, target, **kwargs)

    with monkeypatch.context() as outage:
        outage.setattr(upstream, "request", lose_request)
        assert (await client.post(path + "/prompt?mode=async", json=body, headers=headers)).status_code == 503
    if evidence in ("lost_ack", "expired"):
        async with asyncio.timeout(8):
            while not journal.receipt(local, body["id"]):
                await asyncio.sleep(.01)
    if evidence == "other_key":
        # Direct Router admission under a different command key still leaves the turn tombstone.
        await original("POST", f"/v1/sessions/{local}/prompt", params={"mode": "async"},
                       json=body, headers={"Idempotency-Key": "alternate-router-key"})
        async with asyncio.timeout(8):
            while not journal.receipt(local, body["id"]):
                await asyncio.sleep(.01)
    if evidence == "expired":
        journal.purge(-1)
        assert journal.command(rid)["state"] == "expired"
    assert local in await router.state.registry.sweep_idle(time.time() + 7200)
    await state.routing.sync_bindings()
    assert state.store._one("SELECT status FROM router_bindings WHERE id=?", (sid,))["status"] == "closed"
    initial = state.store._one("SELECT * FROM router_operations WHERE id=?", (rid,))
    assert initial["state"] == "unknown" and initial["finished"] == 1

    if evidence == "other_boot":
        probe = state.nodes.probe

        async def changed_boot(node_id):
            return dict(await probe(node_id), bootId="different")

        monkeypatch.setattr(state.nodes, "probe", changed_boot)
    if evidence == "unavailable":
        async def bad_read(method, target, **kwargs):
            if method == "GET" and target.endswith("/events"):
                return httpx.Response(503, json={"detail": "unavailable"})
            return await original(method, target, **kwargs)

        monkeypatch.setattr(upstream, "request", bad_read)

    async def lookup():
        return await client.get(path + "/commands", params={"requestId": key})

    async def reconcile(**changes):
        return await client.post(path + "/commands/reconcile", json={**query, **changes})

    if evidence == "missing":
        assert (await reconcile(bodySha256="f" * 64)).status_code == 409
        assert (await reconcile(turnId="another")).status_code == 409
        assert (await client.get(path + "/commands", params={"requestId": key}, headers={
            "Authorization": "Bearer " + other["device_token"]})).status_code == 404
        read, resolved, late = await asyncio.gather(
            lookup(), reconcile(), client.post(path + "/prompt?mode=async", json=body, headers=headers))
        assert read.status_code == resolved.status_code == 409
        assert read.content == resolved.content and read.headers["X-Atlas-Command-Outcome"] == "not_sent"
        proof = read.json()["recovery"]
        assert proof["bodySha256"] == query["bodySha256"] and proof["commandId"] == rid and proof["sealed"]
        assert proof["nodeId"] == node and proof["bootId"] == binding["boot_id"]
        assert late.status_code == 404
        assert (await lookup()).content == read.content
        assert state.store._one("SELECT * FROM router_operations WHERE id=?", (rid,)) == initial
        assert journal.command(rid) is None and journal.turn(local, body["id"]) is None

        resumed = await client.post("/harness/agent/sessions", json={
            "model": "stub-fast", "resumeSessionId": alias, "allowHistoryFallback": False})
        assert resumed.status_code == 200 and resumed.json()["resumed"] is True
        successor = resumed.json()["id"]
        assert (await client.post(f"/harness/agent/sessions/{successor}/prompt?mode=async",
                                  json=dict(body, id="successor"))).status_code == 202
        final = await events(client, successor, "successor", terminal=True)
        assert "[помню: 1]" in next(e["text"] for e in final if e["type"] == "result")
        await events(client, successor, "successor", terminal=True)
        assert state.store._one("SELECT COUNT(*) n FROM router_usage_receipts WHERE session_id=?", (successor,))["n"] == 1
    elif evidence == "lost_ack":
        read = await reconcile()
        assert read.status_code == 202 and read.json()["turnId"] == body["id"]
        assert read.headers["X-Atlas-Command-State"] == "complete"
        assert (await lookup()).content == read.content
    else:
        response = await reconcile()
        assert response.status_code == 202 and response.json()["recovery"]["outcome"] == "unknown"
        assert not state.store._one("SELECT * FROM router_admission_fences WHERE id=?", (rid,))
    assert original_posts == [rid]
