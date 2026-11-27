import asyncio
import json
import logging
import re
import time
from types import SimpleNamespace

import pytest

from atlas_gate.gate.activity import GateActivity
from atlas_gate.gate.create_trace import ingress_id, trace_binding
from atlas_gate.gate.errors import GateError
from atlas_gate.gate.nodes import Nodes


@pytest.mark.asyncio
async def test_create_on_warm_and_fresh_connections_reports_the_wire_failure(tmp_path, caplog):
    attempts = []
    connections = []

    async def server(reader, writer):
        try:
            while True:
                request = await reader.readuntil(b"\r\n\r\n")
                lines = request.split(b"\r\n")
                headers = dict(line.lower().split(b": ", 1) for line in lines[1:] if line)
                body = await reader.readexactly(int(headers[b"content-length"]))
                assert b"private-payload" in body
                assert lines[0] == b"POST /v1/sessions HTTP/1.1"
                wire_id = headers[b"x-atlas-trace-id"].decode()
                assert re.fullmatch("[0-9a-f]{32}", wire_id)
                attempts.append(wire_id)
                connections.append(writer.get_extra_info("peername")[1])
                if len(attempts) > 1:
                    return  # Commit observed by fixture; response headers never written.
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    listener = await asyncio.start_server(server, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    cfg = tmp_path / "nodes.json"
    cfg.write_text(json.dumps([dict(node_id="node", url=f"http://127.0.0.1:{port}", token_env="TEST",
                                    account_group="group", orgs=["org"], routes=["route"])]))
    pool = Nodes(str(cfg), tokens={"TEST": "private-credential"})
    caplog.set_level(logging.INFO, logger="atlas_gate")
    try:
        async with listener:
            async def create():
                pool.snapshots["node"] = {"observed": time.monotonic(), "bootId": "node-boot"}
                return await pool.request("node", "POST", "/v1/sessions", content=b'{"text":"private-payload"}',
                                          expected_boot="node-boot", request_id="private-command-key")
            assert (await create()).status_code == 200
            for _ in range(2):
                with pytest.raises(GateError) as error:
                    await create()
                assert error.value.code == "command_outcome_unknown"
                assert error.value.extra["correlationId"] == attempts[-1]
    finally:
        await pool.close()
    assert len(set(attempts)) == 3
    assert connections[0] == connections[1] != connections[2]
    records = [r.getMessage() for r in caplog.records if "session_create wire_end" in r.msg]
    assert len(records) == 3
    for record, trace in zip(records, attempts):
        assert "traceId=" + trace in record
    assert f"connection=fresh localPort={connections[0]} peerPort={port}" in records[0]
    # httpcore has no stream extension on a failed reused connection.
    assert "connection=reused localPort=None peerPort=None" in records[1]
    assert f"connection=fresh localPort={connections[2]} peerPort={port}" in records[2]
    assert all("phase=receive_response_headers.failed" in r and "detail=eof_before_headers" in r
               and "send_request_body.complete:" in r for r in records[1:])
    assert "private-" not in caplog.text


@pytest.mark.asyncio
async def test_incoming_attempts_separate_cached_create_and_cancel_without_changing_activity(caplog):
    attempts = []
    key = "durable-key-hidden"

    async def app(scope, receive, send):
        assert scope["app"].state.active_gate_requests == 1
        attempts.append(ingress_id.get())
        trace_binding(key, {"status": "ready"})
        if len(attempts) == 1:
            raise asyncio.CancelledError
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"private-ack"})

    sent = []
    async def send(message):
        sent.append(message["type"])

    scope = dict(type="http", method="POST", path="/harness/agent/sessions",
                 app=SimpleNamespace(state=SimpleNamespace()))
    gate = GateActivity(app)
    caplog.set_level(logging.INFO, logger="atlas_gate")
    with pytest.raises(asyncio.CancelledError):
        await gate(scope, None, send)
    await gate(scope, None, send)
    assert len(set(attempts)) == 2 and all(re.fullmatch("[a-f0-9]{32}", item) for item in attempts)
    assert ingress_id.get() == "-" and scope["app"].state.active_gate_requests == 0
    assert sent == ["http.response.start", "http.response.body"]
    assert "outcome=interrupted" in caplog.text and "status=200 outcome=asgi_returned" in caplog.text
    assert len(set(re.findall(r"keyTag=([a-f0-9]{16}) prior=ready", caplog.text))) == 1
    assert key not in caplog.text and "private-ack" not in caplog.text
