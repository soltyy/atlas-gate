import asyncio
import json
from urllib.parse import urlsplit

import httpx
import pytest

from .test_network_gate import create, network  # noqa: F401


@pytest.mark.parametrize("mode", ["sse", "async"])
async def test_router_disconnect_resumes_same_event_cursor(network, monkeypatch, mode):
    from atlas_router.backends.stub import StubSession

    client, app, routers, *_ = network
    state = app.state.gate
    state.recovery_task.cancel()
    await asyncio.gather(state.recovery_task, return_exceptions=True)
    if mode == "sse":
        async def no_watch(*args):
            pass
        monkeypatch.setattr("atlas_gate.gate.agent._watch", no_watch)

    sid = await create(client)
    binding = state.store._one("SELECT * FROM router_bindings WHERE id=?", (sid,))
    node_id, local_id = binding["node_id"], binding["local_id"]
    owner = routers[0 if node_id == "a" else 1]
    prefix = f"/harness/agent/sessions/{sid}"
    turn = "disconnected"
    event_path = f"/v1/sessions/{local_id}/turns/{turn}/events"
    release_tool = asyncio.Event()
    actual_request = state.nodes.clients[node_id].request
    original_turn = StubSession._turn
    prompts, cursors, failures, mutations = [], [], [], []

    async def producer(self, turn_id, text, attachments=None):
        prompts.append(turn_id)
        async for event in original_turn(self, turn_id, text, attachments):
            yield event
            if event["type"] == "session":
                await release_tool.wait()

    async def interrupted_request(method, url, **kwargs):
        if method == "POST":
            mutations.append(url.rsplit("/", 1)[-1])
        if url == event_path:
            cursor = kwargs["params"]["after"]
            cursors.append(cursor)
            if cursor == 1 and not failures:
                failures.append("disconnect")
                state.nodes.snapshots.pop(node_id)
                release_tool.set()
                raise httpx.ReadError("link cut after first event")
            if failures == ["disconnect", "probe offline"]:
                failures.append("relay unavailable")
                return httpx.Response(503)
        if url == "/v1/node" and failures == ["disconnect"]:
            failures.append("probe offline")
            raise httpx.ConnectError("link not yet available")
        return await actual_request(method, url, **kwargs)

    monkeypatch.setattr(StubSession, "_turn", producer)
    monkeypatch.setattr(state.nodes.clients[node_id], "request", interrupted_request)
    payload = {"id": turn, "text": "call:echo"}
    delivered = []
    if mode == "sse":
        async with client.stream("POST", prefix + "/prompt", json=payload) as stream:
            assert stream.status_code == 200
            async for line in stream.aiter_lines():
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    delivered.append(event)
                    if event["type"] == "tool_call":
                        assert mutations == ["prompt"]
                        reply = await client.post(prefix + "/tool_result", json={
                            "callId": event["callId"], "content": "sent exactly once",
                        })
                        assert reply.status_code == 200
        assert [event["type"] for event in delivered] == [
            "session", "assistant", "tool_call", "assistant", "result",
        ]
        assert delivered[-1]["text"].endswith("sent exactly once")
    else:
        assert (await client.post(prefix + "/prompt?mode=async", json=payload)).status_code == 202
        async with asyncio.timeout(8):
            while len(failures) < 3 or cursors.count(1) < 3:
                await asyncio.sleep(0.01)
            assert mutations == ["prompt"]
            call = next(event for event in owner.state.journal.events(local_id, turn, 1)
                        if event["type"] == "tool_call")
            reply = await client.post(prefix + "/tool_result", json={
                "callId": call["callId"], "content": "sent exactly once",
            })
            assert reply.status_code == 200
            while not state.store._one("SELECT 1 FROM router_usage_receipts"):
                await asyncio.sleep(0.01)

    assert failures == ["disconnect", "probe offline", "relay unavailable"]
    assert cursors[:4] == [0, 1, 1, 1]
    assert mutations == ["prompt", "tool_result"]
    assert prompts == [turn]
    assert state.store._one("SELECT COUNT(*) n FROM router_bindings")["n"] == 1
    assert state.store._one("SELECT COUNT(*) n FROM router_usage_receipts")["n"] == 1


async def test_sse_does_not_retry_a_forbidden_router_probe(network, monkeypatch):
    client, app, *_ = network
    state = app.state.gate
    state.recovery_task.cancel()
    await asyncio.gather(state.recovery_task, return_exceptions=True)

    async def no_watch(*args):
        pass

    monkeypatch.setattr("atlas_gate.gate.agent._watch", no_watch)
    sid = await create(client)
    binding = state.store._one("SELECT * FROM router_bindings WHERE id=?", (sid,))
    node_id = binding["node_id"]
    actual_request = state.nodes.clients[node_id].request
    failed_probes = []
    disconnected = False

    async def request(method, url, **kwargs):
        nonlocal disconnected
        if url.endswith("/events"):
            disconnected = True
            state.nodes.snapshots.pop(node_id)
            raise httpx.ReadError("first event read failed")
        if url == "/v1/node" and disconnected:
            failed_probes.append(url)
            return httpx.Response(403, request=httpx.Request(method, "http://localhost" + url))
        return await actual_request(method, url, **kwargs)

    monkeypatch.setattr(state.nodes.clients[node_id], "request", request)
    async with asyncio.timeout(8):
        result = await client.post(f"/harness/agent/sessions/{sid}/prompt",
                                   json={"id": "forbidden", "text": "hello"})
    events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ")]
    assert len(failed_probes) in (1, 2)  # Routing and Nodes may both verify identity.
    assert len(events) == 1 and events[0]["type"] == "error" and events[0]["kind"] == "node_unavailable"


async def test_json_poll_recovers_lost_page_after_delivered_tool_result(network, monkeypatch):
    from atlas_router.backends.stub import StubSession

    client, app, _, _, _, other = network
    state = app.state.gate
    state.recovery_task.cancel()
    await asyncio.gather(state.recovery_task, return_exceptions=True)

    async def no_watch(*args):
        pass

    monkeypatch.setattr("atlas_gate.gate.agent._watch", no_watch)
    sid = await create(client)
    binding = state.store._one("SELECT * FROM router_bindings WHERE id=?", (sid,))
    node = binding["node_id"]
    turn = "json-reconnect"
    event_suffix = f"/turns/{turn}/events"
    prefix = f"/harness/agent/sessions/{sid}"
    events_url = prefix + event_suffix
    requests, producers, actions = [], [], []
    failures = asyncio.Queue()
    actual_turn = StubSession._turn
    transport = state.nodes.clients[node]._transport
    actual_send = transport.handle_async_request

    async def producer(self, turn_id, text, attachments=None):
        producers.append(turn_id)
        async for event in actual_turn(self, turn_id, text, attachments):
            yield event

    async def drop_response(request):
        path = urlsplit(str(request.url)).path
        if request.method == "POST" and path.startswith("/v1/sessions/"):
            requests.append(("POST", path.rsplit("/", 1)[-1]))
        if path.endswith(event_suffix):
            requests.append(("GET", int(request.url.params["after"])))
            if not failures.empty():
                action = failures.get_nowait()
                if action == "hang":
                    await asyncio.Event().wait()
                upstream = await actual_send(request)
                await upstream.aread()
                await upstream.aclose()
                raise httpx.RemoteProtocolError("lost upstream HTTP reply", request=request)
        return await actual_send(request)

    monkeypatch.setattr(StubSession, "_turn", producer)
    monkeypatch.setattr(transport, "handle_async_request", drop_response)
    assert (await client.post(prefix + "/prompt?mode=async", json={"id": turn, "text": "call:echo"})).status_code == 202
    cursor, seen = 0, []
    async with asyncio.timeout(12):
        while not actions:
            initial = await client.get(events_url, params={"after": cursor, "wait": 200})
            assert initial.status_code == 200
            page = initial.json()
            cursor = page["nextAfter"]
            seen.extend(event["seq"] for event in page["events"])
            for event in page["events"]:
                if event["type"] == "tool_call":
                    actions.append(event["callId"])
                    reply = await client.post(prefix + "/tool_result", json={
                        "callId": event["callId"], "content": "one local execution",
                    })
                    assert reply.status_code == 200
        failures.put_nowait("drop")
        failures.put_nowait("hang")
        before = len(requests)
        stalled = await client.get(events_url, params={"after": cursor, "wait": 200})
        assert stalled.status_code == 503 and stalled.json()["error"]["code"] == "node_unavailable"
        assert requests[before:] == [("GET", cursor)] * 2

        failures.put_nowait("drop")
        before = len(requests)
        recovered = await client.get(events_url, params={"after": cursor, "wait": 200})
        assert recovered.status_code == 200
        assert requests[before:] == [("GET", cursor)] * 2
        terminal = recovered.json()
        assert terminal["done"] and terminal["state"] == "done"
        assert all(event["type"] != "tool_call" for event in terminal["events"])
        assert terminal["events"][-1]["text"].endswith("one local execution")
        seen.extend(event["seq"] for event in terminal["events"])
        assert seen == list(range(1, terminal["nextAfter"] + 1))
        assert (await client.get(events_url, params={"after": cursor})).json() == terminal
        foreign = await client.get(events_url, headers={"Authorization": "Bearer " + other["device_token"]})
        assert foreign.status_code in (403, 404)
        missing = await client.get(prefix + "/turns/absent/events")
        assert missing.status_code == 404
    assert len(actions) == 1 and producers == [turn]
    assert [op for method, op in requests if method == "POST"] == ["prompt", "tool_result"]
    assert state.store._one("SELECT COUNT(*) n FROM router_usage_receipts")["n"] == 1
