import json
from types import SimpleNamespace

from fastapi import FastAPI, Request
from httpcore._async import http11

from atlas_gate.gate.nodes import Nodes
from tests.test_network_gate import serve


async def test_direct_router_reuses_fresh_connection_and_retires_it_before_server_idle(tmp_path, monkeypatch):
    app = FastAPI()

    @app.get("/peer")
    async def peer(request: Request):
        return {"port": request.client.port}

    # Advance only the client's pool clock. The listening socket stays open on the server.
    now = 0.0
    monkeypatch.setattr(http11, "time", SimpleNamespace(monotonic=lambda: now))
    async with serve(app, timeout_keep_alive=5) as url:
        config = tmp_path / "nodes.json"
        config.write_text(json.dumps([{
            "node_id": "test", "url": url, "token_env": "NODE_TOKEN", "orgs": ["test"],
            "routes": ["claude-sub"], "account_group": "test",
        }]), encoding="utf-8")
        nodes = Nodes(str(config), tokens={"NODE_TOKEN": "test-token"})
        try:
            client = nodes.clients["test"]
            first = (await client.get("/peer")).json()
            now += 2.0
            fresh = (await client.get("/peer")).json()
            now += 4.0
            retired = (await client.get("/peer")).json()
            assert fresh == first
            assert retired != fresh
        finally:
            await nodes.close()
