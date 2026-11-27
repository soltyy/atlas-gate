import base64
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from atlas_gate.gate.errors import GateFreshNodeError
from atlas_gate.gate.nodes import Node, Nodes
from .test_document_transport import codex_network
from .test_network_gate import network
from .test_probe_isolation144 import info


CONFIG = {"node_id": "codex", "url": "http://127.0.0.1:8001", "file_url": "http://127.0.0.1:8002",
          "token_env": "TEST", "orgs": ["test"], "routes": ["sub"], "account_group": "codex", "backend": "codex"}
IDENTITY = "/v1/node/identity"
SESSION = "/v1/sessions/bound"
BOOT = "boot-codex"


@pytest.fixture
async def nodes(tmp_path, monkeypatch):
    calls = []
    replies = {}

    def handler(req):
        lane = "file" if req.url.port == 8002 else "control"
        assert req.headers["X-Atlas-Token"] == "test-token"
        calls.append((lane, req.method, req.url.path))
        if req.url.path == "/v1/node":
            return httpx.Response(200, json={**info("codex"), "backend": "codex", **replies.get("control_info", {})})
        assert req.headers["X-Atlas-Node"] == "codex"
        assert req.headers["X-Atlas-Boot"] == BOOT
        if req.url.path == IDENTITY:
            return replies.get("identity", httpx.Response(200, json={"nodeId":"codex", "bootId":BOOT, "protocolVersion":1}))
        if req.method == "POST":
            assert req.headers["Idempotency-Key"] == "original" and req.content == b'{"id": "upload"}'
        return httpx.Response(200, json={})

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(**kw, transport=httpx.MockTransport(handler)))
    path = tmp_path / "nodes.json"
    path.write_text(json.dumps([CONFIG]))
    pool = Nodes(str(path), tokens={"TEST": "test-token"})
    try:
        yield pool, calls, replies
    finally:
        await pool.close()


@pytest.mark.parametrize("method,suffix,lane", [
    ("POST", "/files", "file"), ("POST", "/files/adaptive/id", "file"), ("POST", "/documents", "file"),
    ("GET", "/files", "control"), ("GET", "/files/adaptive/id", "control"), ("GET", "/documents/id", "control"),
    ("DELETE", "/files/id", "control"), ("POST", "/prompt", "control"), ("POST", "/tool_result", "control"),
    ("GET", "/turns/t/events", "control"), ("GET", "/turns/t/receipt", "control"),
])
async def test_only_bound_upload_posts_use_the_file_origin(nodes, method, suffix, lane):
    pool, calls, _ = nodes
    path = SESSION + suffix
    response = await pool.request("codex", method, path, request_id="original", expected_boot=BOOT,
                                  content=b'{"id": "upload"}')
    assert response.status_code == 200
    expected = [("control", "GET", "/v1/node")]
    if lane == "file":
        expected.append(("file", "GET", IDENTITY))
    assert calls == expected + [(lane, method, path)]


@pytest.mark.parametrize("status,identity", [
    (200, {"nodeId": "other", "bootId": BOOT, "protocolVersion": 1}),
    (200, {"nodeId": "codex", "bootId": "another", "protocolVersion": 1}),
    (404, {"detail": "Not Found"}), (503, {}),
])
async def test_rejected_file_identity_never_dispatches_or_falls_back(nodes, status, identity):
    pool, calls, replies = nodes
    replies["identity"] = httpx.Response(status, json=identity)
    with pytest.raises(GateFreshNodeError):
        await pool.request("codex", "POST", SESSION+"/files", expected_boot=BOOT, request_id="original")
    assert calls == [("control", "GET", "/v1/node"), ("file", "GET", IDENTITY)]


@pytest.mark.parametrize("control_info", [{"bootId": "other"}, {"backend": "claude"}, {"capabilities": []}])
async def test_file_identity_cannot_replace_control_admission(nodes, control_info):
    pool, calls, replies = nodes
    replies["control_info"] = control_info
    with pytest.raises(GateFreshNodeError):
        await pool.request("codex", "POST", SESSION+"/files/adaptive/id", expected_boot=BOOT)
    assert calls == [("control", "GET", "/v1/node")]


@pytest.mark.parametrize("update", [
    {"file_url": "http://remote.example"}, {"file_url": "https://token@host"},
    {"file_url": "https://example/path"}, {"file_url": CONFIG["url"]+"/"},
    {"transport": "connector", "url": "", "token_env": ""},
])
def test_file_origin_requires_a_distinct_approved_direct_endpoint(update):
    with pytest.raises(ValidationError):
        Node.model_validate({**CONFIG, **update})


@pytest.mark.parametrize("network", ["file_url"], indirect=True)
async def test_lost_upload_ack_recovers_through_control_without_another_post(codex_network, monkeypatch):
    client, gate, routers, *_ = codex_network
    state = gate.state.gate
    data = (Path(__file__).parent / "fixtures" / "mixed.pdf").read_bytes()
    doc_id = hashlib.sha256(data).hexdigest()
    created = await client.post("/harness/agent/sessions", json={
        "model": "stub-fast", "tools": [], "documentAccess": "client-authorized-v1", "documentIds": [doc_id]})
    assert created.status_code == 200, created.text
    sid = created.json()["id"]
    binding = state.store._one("SELECT * FROM router_bindings WHERE id=?", (sid,))
    node = binding["node_id"]
    file = state.nodes.file_clients[node]
    control = state.nodes.clients[node]
    assert file.base_url != control.base_url
    file_request, control_request = file.request, control.request
    writes, reads = [], []

    async def lose_ack(method, path, **kw):
        result = await file_request(method, path, **kw)
        if method == "POST":
            writes.append(kw["headers"]["Idempotency-Key"])
            assert path == f"/v1/sessions/{binding['local_id']}/documents"
            assert result.status_code == 200, result.text
            raise httpx.ReadError("lost reply after durable commit")
        assert path == IDENTITY
        return result

    async def observe_control(method, path, **kw):
        assert method == "GET", (method, path)
        reads.append(path)
        return await control_request(method, path, **kw)

    monkeypatch.setattr(file, "request", lose_ack)
    monkeypatch.setattr(control, "request", observe_control)
    prefix = f"/harness/agent/sessions/{sid}"
    body = {"id": "restore", "attachments": [{"kind":"pdf", "filename":"mixed.pdf",
                                               "data":base64.b64encode(data).decode()}]}
    headers = {"Idempotency-Key":"original-upload"}
    failed = await client.post(prefix+"/documents", json=body, headers=headers)
    assert failed.status_code == 503 and failed.json()["error"]["code"] == "command_outcome_unknown"
    receipt = await client.get(prefix+"/commands", params={"requestId": "original-upload"})
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["documents"][0]["id"] == doc_id
    replay = await client.post(prefix+"/documents", json=body, headers=headers)
    assert replay.status_code == 200 and replay.json() == receipt.json()
    assert writes == [receipt.headers["X-Atlas-Command-Id"]]
    assert "/v1/commands/"+writes[0] in reads
    saved = state.store._one("SELECT id,node_id,boot_id FROM router_operations WHERE id=?", (writes[0],))
    assert saved["node_id"] == node and saved["boot_id"] == binding["boot_id"]
    owner = routers[0 if node == "a" else 1]
    assert owner.state.journal.command(writes[0])["state"] == "complete"
