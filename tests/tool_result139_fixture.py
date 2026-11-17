"""Loopback Gate and two Router processes for EDT delivery acceptance.

Run with ATLAS_TEST_ROUTER_ROOT and python -m tests.tool_result139_fixture.
All credentials, databases and scripted tool calls belong to this temporary fixture.
"""
import asyncio
import json
from pathlib import Path
import tempfile

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
import pytest

from .test_network_gate import create, network


async def main():
    from atlas_router.backends.stub import StubSession

    with tempfile.TemporaryDirectory(prefix="gate-tool-result139-") as directory:
        patch = pytest.MonkeyPatch()
        fixture = network.__wrapped__(Path(directory), patch)
        client, app, routers, _, credentials, _ = await anext(fixture)
        try:
            cases = {}
            accepted = []
            shutdown = asyncio.Event()
            state = app.state.gate
            state.settings.ATLAS_GATE_PUBLIC_URL = str(client.base_url).rstrip("/")
            org = state.orgs["test-org"].model_dump(mode="json")
            org["agents"] = [dict(id="stub-agent", name="Delivery acceptance", version="1", default=True,
                                  model="inherit", allowed_routes=["claude-sub"],
                                  tools_allowlist=["*"], system_prompt="test")]
            folder = Path(state.settings.ATLAS_GATE_ORGS_DIR)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "test-org.json").write_text(json.dumps(org), encoding="utf-8")
            await state.reload()
            original_result = StubSession.tool_result

            async def producer(self, turn_id, text, attachments=None):
                calls = [turn_id + "-a", turn_id + "-b"]
                futures = [self._pending.open(call) for call in calls]
                for call in calls:
                    yield {"type": "tool_call", "id": turn_id, "callId": call,
                           "name": "echo", "input": {"value": call},
                           "agent": {"id": call, "name": "executor", "parent": "parent-" + call}}
                await asyncio.gather(*futures)
                stopped = self._interrupt.is_set()
                yield {"type": "result", "id": turn_id, "ok": not stopped,
                       "subtype": "interrupted" if stopped else "success",
                       "code": "turn_interrupted" if stopped else None,
                       "text": "fixture result", "usage": {"input_tokens": 1, "output_tokens": 1}}

            async def result(self, call_id, content, is_error):
                accepted.append(call_id)
                return await original_result(self, call_id, content, is_error)

            patch.setattr(StubSession, "prompt", producer)
            patch.setattr(StubSession, "tool_result", result)
            from atlas_gate.gate import agent
            from atlas_gate.gate.errors import GateError
            original_begin = state.operations.begin
            original_response = agent._operation_response

            def begin(device, binding, method, path, body, *args, **kwargs):
                case = cases.get(binding["id"])
                if (case and case["mode"] == "lost_post" and not case["injected"]
                        and path.endswith("/tool_result") and body["callId"].endswith("-a")):
                    case["injected"] = True
                    raise GateError(503, "fixture_lost_post", "fixture before admission")
                return original_begin(device, binding, method, path, body, *args, **kwargs)

            def response(value, op):
                case = cases.get(op["session_id"])
                if (case and case["mode"] == "lost_ack" and not case["injected"]
                        and op["path"].endswith("/tool_result") and value.status_code == 200
                        and value.json().get("callId", "").endswith("-a")):
                    case["injected"] = True
                    return JSONResponse({"error": {"code": "fixture_lost_ack"}}, status_code=503)
                return original_response(value, op)

            patch.setattr(state.operations, "begin", begin)
            patch.setattr(agent, "_operation_response", response)

            def authorize(request):
                if request.headers.get("X-Test-Key") != "isolated-tool-result139":
                    raise HTTPException(403)

            @app.post("/__test/tool-result139/setup")
            async def setup(request: Request):
                authorize(request)
                body = await request.json()
                mode = body.get("mode", "normal")
                if mode not in ("normal", "lost_post", "lost_ack"):
                    raise HTTPException(422, "unsupported fixture mode")
                sid = await create(client)
                cases[sid] = {"mode": mode, "injected": False}
                return {"url": str(client.base_url), "session_id": sid, "credentials": credentials}

            @app.get("/__test/tool-result139/state")
            async def inspect(request: Request):
                authorize(request)
                return {"acceptedCalls": accepted, "cases": cases}

            @app.post("/__test/tool-result139/shutdown")
            async def stop(request: Request):
                authorize(request)
                asyncio.get_running_loop().call_later(.1, shutdown.set)
                return {"ok": True}

            print(json.dumps({"url": str(client.base_url), "testKey": "isolated-tool-result139"}), flush=True)
            await shutdown.wait()
        finally:
            await fixture.aclose()
            for router in routers:
                router.state.journal.db.close()
            patch.undo()


if __name__ == "__main__":
    asyncio.run(main())
