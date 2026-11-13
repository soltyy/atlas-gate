"""Gate88: real HTTP, temporary ledger, controlled Router; no SDK or inference."""
import copy
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from atlas_gate.gate.crypto import sha256_hex
from atlas_gate.gate.errors import GateError
from atlas_gate.gate.operations import Operations, body_sha256
from atlas_gate.gate.orgs import OrgConfig
from atlas_gate.gate.recovery import Recovery, router
from atlas_gate.gate.routing import Routing
from atlas_gate.gate.state import Device
from atlas_gate.gate.store import Store
from .test_network_gate import serve


class ControlledNodes:
    def __init__(self, client):
        self.client = client
        self.config = {"node": SimpleNamespace(enabled=True, orgs=["org"], routes=["sub"], transport="direct")}
        self.snapshots = {"node": {"bootId": "boot", "observed": time.monotonic(), "ready": True}}
        self.calls, self.turns, self.commands, self.cases = [], {}, {}, {}
        self.native_available, self.native_resumed, self.native_count = False, False, 0
        self.native_receipts, self.native_steps = {}, {}
        self.native_denied, self.native_failure = False, False
        self.native_busy, self.native_revision = False, 1

    def revoked(self, node):
        return False

    async def probe(self, node):
        self.snapshots[node]["observed"] = time.monotonic()
        return self.snapshots[node]

    async def request(self, node, method, path, *, content=b"", params=None, expected_boot=None, request_id=None):
        assert node == "node"
        self.calls.append((method, path, request_id))
        return await self.client.request(method, path, content=content, params=params,
                                         headers={"Idempotency-Key": request_id} if request_id else {})


@pytest.fixture
async def recovery_http(tmp_path):
    backend = FastAPI()
    nodes = None

    @backend.api_route("/{path:path}", methods=["GET", "POST"])
    async def controlled(path: str, request: Request):
        path = "/" + path
        if nodes.native_denied and path.startswith(("/v1/recovery/", "/v1/commands/")):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        if path.startswith("/v1/commands/"):
            return JSONResponse(nodes.commands.get(path.rsplit("/", 1)[1], {"detail": "command_not_found"}),
                                status_code=200 if path.rsplit("/", 1)[1] in nodes.commands else 404)
        if "/turns/" in path:
            tid = path.split("/turns/", 1)[1].split("/")[0]
            events = nodes.turns.get(tid)
            if not events:
                return JSONResponse({"detail": "turn_not_found"}, status_code=404)
            if path.endswith("/receipt"):
                return events[-1]
            if path.endswith("/events"):
                after = int(request.query_params.get("after", 0))
                selected = [event for event in events if event["seq"] > after][:2]
                cursor = selected[-1]["seq"] if selected else after
                return {"turnId": tid, "events": selected, "nextAfter": cursor, "state": "done", "done": cursor == len(events)}
            return {"turnId": tid, "state": "done", "done": True, "seq": len(events)}
        if path == "/v1/recovery/cases" and request.method == "POST":
            body = await request.json()
            nodes.cases.setdefault(body["id"], body)
            return {"id": body["id"]}
        if path.startswith("/v1/recovery/cases/"):
            rid = path.split("/v1/recovery/cases/", 1)[1].split("/")[0]
            saved = nodes.cases[rid]
            if path.endswith("/apply"):
                key = request.headers["idempotency-key"]
                body = await request.json()
                if key not in nodes.native_receipts:
                    nodes.native_count += 1
                    nodes.native_resumed = not (nodes.native_failure or nodes.native_busy)
                    result = {"sessionId": saved["sessionId"], "actionsReplayed": False}
                    if nodes.native_busy:
                        result.update(outcome="rejected_not_sent", scope="transport_only", code="sdk_writer_busy", retryable=True)
                    nodes.native_steps.setdefault(rid, {})[body["actionId"]] = {"request": body,
                        "state": "outcome_unknown" if nodes.native_failure else "complete",
                        "result": result}
                    nodes.native_receipts[key] = {"version": 1, "state": "outcome_unknown" if nodes.native_failure else "completed", "actionsReplayed": False}
                    nodes.commands[key] = {"requestId": key, "state": "complete", "status": 409 if nodes.native_failure else 200,
                                           "response": nodes.native_receipts[key]}
                    if nodes.native_busy:
                        nodes.native_revision += 1
                if nodes.native_failure:
                    return JSONResponse(nodes.native_receipts[key], status_code=409)
                return nodes.native_receipts[key]
            return {"version": 1, "id": rid, "revision": nodes.native_revision, "state": "ready", "sessionId": saved["sessionId"],
                    "nodeId": "node", "bootId": "boot", "history": {"state": "recorded", "sdkSessionId": "native-" + saved["sessionId"]},
                    "turn": None, "receipt": None,
                    "capabilities": {"readReceipts": True, "resumeTransport": nodes.native_available},
                    "steps": nodes.native_steps.get(rid, {}),
                    "actions": [{"id": "resume-" + str(nodes.native_revision), "kind": "resume_transport"}] if nodes.native_available and not nodes.native_resumed else []}
        return JSONResponse({"detail": "not_found"}, status_code=404)

    async with serve(backend) as router_url:
        async with httpx.AsyncClient(base_url=router_url) as connection:
            nodes = ControlledNodes(connection)
            store = Store(str(tmp_path / "gate.db"))
            org = OrgConfig(id="org", name="Test", routes=[{"id": "sub", "kind": "router-agent", "protocol": "atlas-agent"}],
                            models=[{"route_id": "sub", "model": "model"}])
            state = SimpleNamespace(store=store, nodes=nodes, orgs={"org": org}, org_errors={}, public_url="http://gate",
                                    bodies={"org": {"models": [{"route_id": "sub", "model": "model"}]}})
            state.operations, state.routing = Operations(store), Routing(store, nodes)
            state.recovery = Recovery(state)
            for name in ("owner", "other"):
                store.device_create({"device_id": name, "org": "org", "user_id": name, "user_email": "", "user_name": name,
                                     "device_name": name, "platform": "test", "device_secret": "test", "created_at": 0})
                store.tokens_set(name, sha256_hex(name), time.time() + 600, sha256_hex("refresh-" + name), time.time() + 1200)
            app = FastAPI()
            app.state.gate = state
            app.include_router(router)
            async with serve(app) as url:
                async with httpx.AsyncClient(base_url=url, headers={"Authorization": "Bearer owner"}) as client:
                    yield client, state, nodes
            state.store.close()


def owner(state):
    row = state.store.device("owner")
    return Device("owner", "org", "owner", "", "owner", row)


def admit(state, sid=None, **kwargs):
    branch = state.store._one("SELECT * FROM recovery_branches WHERE device_id='owner' AND conversation_id='chat' AND branch_id='branch'")
    return state.recovery.check_admission(owner(state), {"id": sid} if sid else None,
        conversation_id="chat", branch_id="branch", inventory_id=branch["inventory_id"],
        generation=branch["generation"], control_epoch=branch["control_epoch"], **kwargs)


def source(state, nodes, sid="old", *, tool=True, terminal=True):
    store = state.store
    store._exec("INSERT INTO router_bindings(id,device_id,org,route,node_id,boot_id,local_id,model,roles,status,created,sdk_alias,sdk_id,response) "
                "VALUES (?,'owner','org','sub','node','boot',?,'model','[]','closed',0,?,?,?)",
                (sid, "local-" + sid, "alias-" + sid, "native-local-" + sid, '{"sdkTools":{}}'))
    binding = store._one("SELECT * FROM router_bindings WHERE id=?", (sid,))
    prompt = {"id": "turn-" + sid, "text": "old work"}
    op, _ = state.operations.begin(owner(state), binding, "POST", "/v1/sessions/local-" + sid + "/prompt", prompt,
                                   request_id="prompt-" + sid, turn_id=prompt["id"])
    state.operations.complete(op["id"], httpx.Response(202, json={"turnId": prompt["id"], "seq": 0}))
    operations = [{"kind": "prompt", "requestId": "prompt-" + sid, "turnId": prompt["id"], "bodySha256": body_sha256(prompt)}]
    if tool:
        output = {"callId": "call-" + sid, "content": "counter=1"}
        command, _ = state.operations.begin(owner(state), binding, "POST", "/v1/sessions/local-" + sid + "/tool_result", output,
                                             request_id="result-" + sid)
        state.operations.complete(command["id"], httpx.Response(200, json={"ok": True, "callId": output["callId"], "status": "accepted"}))
        operations.append({"kind": "tool_result", "requestId": "result-" + sid, "turnId": prompt["id"], "callId": output["callId"],
                           "bodySha256": body_sha256(output), "localState": "output_durable", "outputRef": "local-saved-output"})
    if terminal:
        nodes.turns[prompt["id"]] = [{"type": "delta", "id": prompt["id"], "seq": i, "text": str(i)} for i in range(1, 5)] + [
            {"type": "result", "id": prompt["id"], "seq": 5, "ok": True, "text": "saved complete answer"}]
    return {"sourceRef": sid, "sourceRunId": "run-" + sid, "sessionId": sid}, operations


async def post(client, path, body, key):
    response = await client.post(path, json=body, headers={"Idempotency-Key": key})
    assert response.status_code == 200, response.text
    return response.json()


async def inventory(client, refs, version=1):
    return await post(client, "/harness/recovery/inventories", {"version": 1, "conversationId": "chat", "branchId": "branch",
        "clientStoreEpoch": "client-store", "checkpointInventoryVersion": version, "sources": refs}, "inventory-" + str(version))


async def case(client, inv, ref, operations, key="case", checkpoint=3):
    body = {"version": 1, "inventoryId": inv["inventoryId"], "sourceRef": ref["sourceRef"], "conversationId": "chat", "branchId": "branch",
            "sourceRunId": ref["sourceRunId"], "sourceCheckpointVersion": checkpoint,
            "source": {"gateRoot": "http://gate", "sessionId": ref["sessionId"], "historyAlias": "alias-" + ref["sessionId"], "routeId": "sub", "model": "model"},
            "operations": operations, "observation": {"lastAppliedSeq": 0},
            "permissions": {"allowedRecovery": ["read_receipt", "apply_saved_ack", "resolve_source_checkpoint", "resume_transport"], "businessReplayAllowed": False}}
    return await post(client, "/harness/recovery/cases", body, key), body


def ack_body(snapshot, key="ack"):
    local = [a for a in snapshot["actions"] if a["executor"] == "harness"]
    source_field = "sourceAttemptId" if "sourceAttemptId" in local[0] else "sourceSessionId"
    return {"version": 1, "ackKey": key, "planId": snapshot["planId"], "planRevision": snapshot["revision"],
            "sourceManifestSha256": snapshot["sourceManifestSha256"], "clientStoreEpoch": "client-store",
            "sourceRunId": local[0]["sourceRunId"], source_field: local[0][source_field], "sourceCheckpointVersion": local[0]["sourceCheckpointVersion"],
            "appliedActions": [{"actionId": a["id"], "evidenceSha256": a["evidenceSha256"]} for a in local],
            "localCommitRef": "commit-" + key, "localCommitSha256": "f" * 64}


def cas(snapshot):
    return {"version": 1, "expectedRevision": snapshot["revision"], "sourceManifestSha256": snapshot["sourceManifestSha256"], "branchFence": snapshot["branchFence"]}


@pytest.mark.parametrize("blocker", [None, "no_stop", "wrong_call", "admitted_unknown", "alternate_accepted"])
async def test_stopped_turn_reconciles_unsent_durable_outputs(recovery_http, blocker, monkeypatch):
    client, state, nodes = recovery_http
    ref, operations = source(state, nodes, tool=False)
    sid, tid = ref["sessionId"], operations[0]["turnId"]
    for number in range(4):
        call_id = f"child-{number}"
        output = {"callId": call_id, "content": "saved result"}
        operations.append({"kind": "tool_result", "requestId": f"tool-result:{sid}:{call_id}",
                           "turnId": tid, "callId": call_id, "bodySha256": body_sha256(output),
                           "localState": "output_durable", "outputRef": f"saved-{number}"})
        if number == 0 and blocker in ("admitted_unknown", "alternate_accepted"):
            binding = state.store._one("SELECT * FROM router_bindings WHERE id=?", (sid,))
            command, _ = state.operations.begin(owner(state), binding, "POST",
                f"/v1/sessions/local-{sid}/tool_result", output,
                request_id=operations[-1]["requestId"] if blocker == "admitted_unknown" else "different-key")
            if blocker == "alternate_accepted":
                state.operations.complete(command["id"], httpx.Response(200, json={"callId": call_id, "status": "accepted"}))

    # Claude emits stopped parent-agent events under different call IDs.
    nodes.turns[tid] = [
        {"type": "tool_call", "id": tid, "seq": i + 1,
         "callId": "other-child" if i == 0 and blocker == "wrong_call" else f"child-{i}"}
        for i in range(4)
    ] + [
        {"type": "tool_done", "id": tid, "seq": 5, "callId": "parent-agent", "isError": True},
        {"type": "result", "id": tid, "seq": 6, "ok": False,
         "code": "provider_failed" if blocker == "no_stop" else "turn_interrupted"}
    ]
    inv = await inventory(client, [ref])
    if blocker is None:
        async def old_observer(*args):
            return None

        with monkeypatch.context() as old:
            old.setattr(state.recovery, "_stopped_tool_delivery", old_observer)
            snapshot, _ = await case(client, inv, ref, operations)
            path = "/harness/recovery/cases/" + snapshot["recoveryId"]
            assert [o["outcome"] for o in snapshot["operations"][1:]] == ["outcome_unknown"] * 4
            await post(client, "/harness/recovery/inventories/" + inv["inventoryId"] + "/seal", {"version": 1}, "seal")
            assert (await client.post(path + "/ack", json=ack_body(snapshot, "old-ack"))).status_code == 200
        snapshot = (await client.get(path)).json()
    else:
        snapshot, _ = await case(client, inv, ref, operations)
        await post(client, "/harness/recovery/inventories/" + inv["inventoryId"] + "/seal", {"version": 1}, "seal")
        path = "/harness/recovery/cases/" + snapshot["recoveryId"]
    outcomes = [o["outcome"] for o in snapshot["operations"]]
    assert outcomes[0] == "terminal_error"
    if blocker is None:
        assert outcomes[1:] == ["rejected"] * 4
        proofs = [e["body"]["payload"] for e in snapshot["evidence"] if e["body"]["kind"] == "tool_result"]
        assert [proof["operation"] for proof in proofs] == operations[1:]
        assert all(proof["response"]["error"]["deliveryState"] == "closed" for proof in proofs)
    else:
        assert outcomes[1] == "outcome_unknown"
    ack = await client.post(path + "/ack", json=ack_body(snapshot))
    assert ack.status_code == 200, ack.text
    final = (await client.get(path)).json()
    assert final["next"]["businessDispatchAllowed"] is (blocker is None)
    assert not any(method == "POST" and path.endswith(("/prompt", "/tool_result")) for method, path, _ in nodes.calls)


async def test_exact_closed_terminal_ack_output_pagination_and_no_business_dispatch(recovery_http):
    client, state, nodes = recovery_http
    ref, operations = source(state, nodes)
    inv = await inventory(client, [ref])
    snapshot, manifest = await case(client, inv, ref, operations)
    assert snapshot["sourceManifestSha256"] == body_sha256(manifest)
    assert [o["outcome"] for o in snapshot["operations"]] == ["terminal_success", "accepted"]
    terminal = next(e["body"]["payload"] for e in snapshot["evidence"] if e["body"]["kind"] == "prompt")
    assert len(terminal["events"]) == 5 and terminal["events"][-1]["text"] == "saved complete answer"
    assert operations[-1]["outputRef"] == "local-saved-output"
    await post(client, "/harness/recovery/inventories/" + inv["inventoryId"] + "/seal", {"version": 1}, "seal")
    result = await client.post("/harness/recovery/cases/" + snapshot["recoveryId"] + "/ack", json=ack_body(snapshot))
    assert result.status_code == 200 and result.json()["state"] == "client-applied"
    # No extra GET is needed to release the admission barrier after durable ACK.
    admit(state, "old")
    assert not any(path.endswith(("/prompt", "/tool_result")) for _, path, _ in nodes.calls)
    assert not state.store._all("SELECT * FROM usage")


async def test_existing_unknown_effect_is_resolved_by_a_later_exact_result(recovery_http):
    client, state, nodes = recovery_http
    ref, operations = source(state, nodes, tool=False)
    call, turn = "late-call", operations[0]["turnId"]
    nodes.turns[turn][0] = {"type": "tool_call", "seq": 1, "id": turn, "callId": call, "name": "echo", "input": {}}
    key = f"tool-result:{ref['sessionId']}:{call}"
    operations.append({"kind": "tool_effect", "requestId": key, "turnId": turn, "callId": call,
                       "bodySha256": body_sha256({"name": "echo", "arguments": {}}), "localState": "effect_unknown"})
    inv = await inventory(client, [ref])
    snapshot, _ = await case(client, inv, ref, operations)
    assert snapshot["operations"][-1]["outcome"] == "effect_unknown"
    path = "/harness/recovery/cases/" + snapshot["recoveryId"]
    binding = state.store._one("SELECT * FROM router_bindings WHERE id=?", (ref["sessionId"],))
    output = {"callId": call, "content": "verified output"}
    command, _ = state.operations.begin(owner(state), binding, "POST", "/v1/sessions/local-old/tool_result",
                                         output, request_id=key)
    state.operations.complete(command["id"], httpx.Response(200, json={"ok": True, "callId": call, "status": "accepted"}))
    assert body_sha256(output) != operations[-1]["bodySha256"]
    updated = await client.get(path)
    assert updated.status_code == 200, updated.text
    updated = updated.json()
    assert [op["outcome"] for op in updated["operations"]] == ["terminal_success", "accepted"]
    evidence = next(e["body"]["payload"] for e in updated["evidence"] if e["body"]["kind"] == "tool_effect")
    assert evidence["resultBodySha256"] == body_sha256(output)
    assert evidence["operation"]["bodySha256"] == operations[-1]["bodySha256"]
    assert "content" not in evidence["receipt"]
    await post(client, "/harness/recovery/inventories/" + inv["inventoryId"] + "/seal", {"version": 1}, "seal")
    ack = await client.post(path + "/ack", json=ack_body(updated))
    assert ack.status_code == 200, ack.text
    admit(state, "old")
    assert not any(method == "POST" and path.endswith(("/prompt", "/tool_result")) for method, path, _ in nodes.calls)


@pytest.mark.parametrize("change", [None, "different_turn", "different_call", "same_version", "wrong_hash", "no_receipt"])
async def test_new_checkpoint_advances_unknown_effect_without_dropping_the_old_case(recovery_http, change):
    client, state, nodes = recovery_http
    ref, operations = source(state, nodes, tool=False)
    call, turn, session = "late-call", operations[0]["turnId"], ref["sessionId"]
    nodes.turns[turn][0] = {"type": "tool_call", "seq": 1, "id": turn, "callId": call, "name": "echo", "input": {}}
    key = f"tool-result:{session}:{call}"
    effect = {"kind": "tool_effect", "requestId": key, "turnId": turn, "callId": call,
              "bodySha256": body_sha256({"name": "echo", "arguments": {}}), "localState": "effect_unknown"}
    first = await inventory(client, [ref], version=60)
    old, body = await case(client, first, ref, [*operations, effect], key="case-60", checkpoint=60)
    await post(client, f"/harness/recovery/inventories/{first['inventoryId']}/seal", {"version": 1}, "seal-60")
    current = await inventory(client, [ref], version=62)
    output = {"callId": call, "content": "saved locally"}
    if change != "no_receipt":
        binding = state.store._one("SELECT * FROM router_bindings WHERE id=?", (session,))
        command, _ = state.operations.begin(owner(state), binding, "POST", "/v1/sessions/local-old/tool_result",
                                            output, request_id=key)
        state.operations.complete(command["id"], httpx.Response(200, json={"callId": call, "status": "accepted"}))
    durable = {**effect, "kind": "tool_result", "bodySha256": body_sha256(output),
               "localState": "output_durable", "outputRef": "local:old:late-call"}
    if change == "different_turn":
        durable["turnId"] = "other-turn"
    if change == "different_call":
        durable["callId"] = "other-call"
    if change == "wrong_hash":
        durable["bodySha256"] = "f" * 64
    body.update(inventoryId=current["inventoryId"], sourceCheckpointVersion=60 if change == "same_version" else 62,
                operations=[*operations, durable])
    response = await client.post("/harness/recovery/cases", json=body, headers={"Idempotency-Key": "case-62"})
    if change not in (None, "no_receipt"):
        assert response.status_code == 409, response.text
        return
    assert response.status_code == 200, response.text
    await post(client, f"/harness/recovery/inventories/{current['inventoryId']}/seal", {"version": 1}, "seal-62")
    path = "/harness/recovery/cases/" + response.json()["recoveryId"]
    plan = (await client.get(path)).json()
    assert plan["operations"][-1]["outcome"] == ("outcome_unknown" if change == "no_receipt" else "accepted")
    ack = await client.post(path + "/ack", json=ack_body(plan, "ack-62"))
    assert ack.status_code == 200, ack.text
    old_resolved = state.store._one("SELECT resolved FROM recovery_cases WHERE id=?", (old["recoveryId"],))["resolved"]
    assert bool(old_resolved) == (change is None)
    if change is None:
        assert (await client.get(path)).json()["next"]["businessDispatchAllowed"] is True
        admit(state, session)
    assert not any(method == "POST" and url.endswith(("/prompt", "/tool_result")) for method, url, _ in nodes.calls)


async def test_historical_tool_preflight_is_pending_until_router_receipt(recovery_http):
    client, state, nodes = recovery_http
    ref, operations = source(state, nodes, terminal=False)
    callback = operations[-1]
    command_id = state.operations.key(owner(state), callback["requestId"])
    state.store._exec("UPDATE router_operations SET status=503,response=? WHERE id=?",
        (json.dumps({"error": {"code": "node_unavailable", "deliveryState": "not_sent"}}), command_id))
    inv = await inventory(client, [ref])
    snapshot, _ = await case(client, inv, ref, operations)
    assert snapshot["operations"][-1]["outcome"] == "outcome_unknown"
    assert not any(e["body"]["kind"] == "tool_result" for e in snapshot["evidence"])

    nodes.commands[command_id] = {"requestId": command_id, "state": "complete", "status": 200,
                                 "response": {"ok": True, "callId": callback["callId"], "status": "accepted"}}
    settled = (await client.get("/harness/recovery/cases/" + snapshot["recoveryId"])).json()
    assert settled["operations"][-1]["outcome"] == "accepted"
    assert not any(method == "POST" and path.endswith("/tool_result") for method, path, _ in nodes.calls)


async def test_first_late_ack_survives_revision_stop_and_restart(recovery_http):
    client, state, nodes = recovery_http
    ref, operations = source(state, nodes)
    inv = await inventory(client, [ref])
    original, _ = await case(client, inv, ref, operations)
    path = "/harness/recovery/cases/" + original["recoveryId"]
    receipt = ack_body(original)
    await post(client, path + "/evidence", {"version": 1, "kind": "executor_claim", "claim": {"saved": True}}, "claim")
    advanced = (await client.get(path)).json()
    await post(client, path + "/cancel", cas(advanced), "cancel")
    first = await client.post(path + "/ack", json=receipt)
    assert first.status_code == 200 and first.json()["state"] == "historical-applied"
    filename = state.store._one("PRAGMA database_list")["file"]
    state.store.close()
    state.store = Store(filename)
    state.operations, state.routing = Operations(state.store), Routing(state.store, nodes)
    state.recovery = Recovery(state)
    assert (await client.post(path + "/ack", json=receipt)).json() == first.json()
    changed = copy.deepcopy(receipt)
    changed["localCommitSha256"] = "e" * 64
    assert (await client.post(path + "/ack", json=changed)).status_code == 409
    cancelled = (await client.get(path)).json()
    assert cancelled["state"] == "cancelled" and not cancelled["actions"]
    with pytest.raises(GateError, match="Сначала"):
        admit(state, "old")
    assert nodes.native_count == 0


async def test_inventory_barrier_missing_source_and_new_snapshot_fences_admission(recovery_http):
    client, state, nodes = recovery_http
    ref_a, ops_a = source(state, nodes, "a")
    ref_b, ops_b = source(state, nodes, "b")
    inv = await inventory(client, [ref_a, ref_b])
    a, _ = await case(client, inv, ref_a, ops_a, "case-a")
    assert (await client.post("/harness/recovery/cases/" + a["recoveryId"] + "/ack", json=ack_body(a, "ack-a"))).status_code == 200
    seal_path = "/harness/recovery/inventories/" + inv["inventoryId"] + "/seal"
    assert (await client.post(seal_path, json={"version": 1}, headers={"Idempotency-Key": "seal"})).status_code == 409
    state.recovery = Recovery(state)  # crash/restart before B registration and seal
    with pytest.raises(GateError):
        admit(state, "a")
    b, _ = await case(client, inv, ref_b, ops_b, "case-b")
    await post(client, seal_path, {"version": 1}, "seal")
    assert (await client.post("/harness/recovery/cases/" + b["recoveryId"] + "/ack", json=ack_body(b, "ack-b"))).status_code == 200
    admit(state, "a")
    await inventory(client, [ref_a, ref_b], 2)
    with pytest.raises(GateError):
        admit(state, "a")


async def test_owner_policy_body_conflict_and_untrusted_claim(recovery_http):
    client, state, nodes = recovery_http
    ref, operations = source(state, nodes, terminal=False)
    inv = await inventory(client, [ref])
    snapshot, body = await case(client, inv, ref, operations)
    path = "/harness/recovery/cases/" + snapshot["recoveryId"]
    assert (await client.get(path, headers={"Authorization": "Bearer other"})).status_code == 404
    changed = copy.deepcopy(body)
    changed["operations"][0]["bodySha256"] = "0" * 64
    assert (await client.post("/harness/recovery/cases", json=changed, headers={"Idempotency-Key": "case"})).status_code == 409
    await post(client, path + "/evidence", {"version": 1, "kind": "external_success", "claim": {"terminal": True}}, "forged-proof")
    observed = (await client.get(path)).json()
    assert observed["operations"][0]["outcome"] == "accepted_running" and observed["claims"]
    state.orgs["org"].archived = True
    assert (await client.get(path)).status_code == 403
    state.orgs["org"].archived = False
    nodes.config["node"].enabled = False
    assert (await client.get(path)).status_code == 403
    nodes.config["node"].enabled = True
    state.store.device_revoke("owner")
    assert (await client.get(path)).status_code == 401


async def test_fence_absent_prompt_and_late_original_cannot_execute(recovery_http):
    client, state, nodes = recovery_http
    ref, _ = source(state, nodes, tool=False)
    # This checkpoint was never admitted. Existing old work remains in server
    # reconciliation and cannot be disguised as a negative outcome.
    payload = {"id": "never-sent", "text": "not submitted"}
    op = {"kind": "prompt", "requestId": "never-key", "turnId": payload["id"], "bodySha256": body_sha256(payload)}
    inv = await inventory(client, [ref])
    snapshot, _ = await case(client, inv, ref, [op])
    action = next(a for a in snapshot["actions"] if a["kind"] == "seal_prompt")
    applied = await post(client, "/harness/recovery/cases/" + snapshot["recoveryId"] + "/apply",
                         {**cas(snapshot), "planId": snapshot["planId"], "actionIds": [action["id"]]}, "apply-fence")
    assert applied["steps"][0]["outcome"] == "not_sent"
    binding = state.store._one("SELECT * FROM router_bindings WHERE id='old'")
    with pytest.raises(GateError) as blocked:
        state.operations.begin(owner(state), binding, "POST", "/v1/sessions/local-old/prompt", payload, "never-key", payload["id"])
    assert blocked.value.code == "command_not_sent"


async def test_native_transport_after_client_apply_is_idempotent_and_not_prompt(recovery_http):
    client, state, nodes = recovery_http
    ref, operations = source(state, nodes)
    state.store._exec("UPDATE router_bindings SET status='ready' WHERE id='old'")
    nodes.native_available = True
    inv = await inventory(client, [ref])
    snapshot, _ = await case(client, inv, ref, operations)
    assert not [a for a in snapshot["actions"] if a["kind"] == "resume_transport"]
    await post(client, "/harness/recovery/inventories/" + inv["inventoryId"] + "/seal", {"version": 1}, "seal")
    path = "/harness/recovery/cases/" + snapshot["recoveryId"]
    assert (await client.post(path + "/ack", json=ack_body(snapshot))).status_code == 200
    with pytest.raises(GateError) as blocked:
        admit(state, "old")
    assert blocked.value.extra.get("reason") == "transport_recovery_required"
    ready = (await client.get(path)).json()
    action = next(a for a in ready["actions"] if a["kind"] == "resume_transport")
    body = {**cas(ready), "planId": ready["planId"], "actionIds": [action["id"]]}
    first = await post(client, path + "/apply", body, "native-apply")
    await client.get(path)  # snapshot revision can advance after native completion
    assert await post(client, path + "/apply", body, "native-apply") == first
    assert nodes.native_count == 1
    assert not any(path.endswith(("/prompt", "/tool_result")) for _, path, _ in nodes.calls)


async def test_inventory_cannot_drop_unknown_source_and_old_case_cannot_use_new_fence(recovery_http):
    client, state, nodes = recovery_http
    ref, operations = source(state, nodes, tool=False, terminal=False)
    old_inventory = await inventory(client, [ref])
    old, _ = await case(client, old_inventory, ref, operations)
    missing = await client.post("/harness/recovery/inventories", json={"version": 1, "conversationId": "chat", "branchId": "branch",
        "clientStoreEpoch": "client-store", "checkpointInventoryVersion": 2, "sources": []}, headers={"Idempotency-Key": "drop-source"})
    assert missing.status_code == 409 and missing.json()["error"]["code"] == "recovery_inventory_incomplete"
    new_inventory = await inventory(client, [ref], 2)
    nodes.turns["turn-old"] = [{"type": "result", "id": "turn-old", "seq": 1, "ok": True, "text": "finished"}]
    new, _ = await case(client, new_inventory, ref, operations, "new-case")
    await post(client, "/harness/recovery/inventories/" + new_inventory["inventoryId"] + "/seal", {"version": 1}, "new-seal")
    assert (await client.post("/harness/recovery/cases/" + new["recoveryId"] + "/ack", json=ack_body(new))).status_code == 200
    admit(state, "old")
    old_path = "/harness/recovery/cases/" + old["recoveryId"]
    historical = (await client.get(old_path)).json()
    assert not historical["next"]["businessDispatchAllowed"]
    assert not [a for a in historical["actions"] if a["executor"] == "gate"]
    assert (await client.post(old_path + "/cancel", json=cas(historical), headers={"Idempotency-Key": "late-cancel"})).status_code == 409


async def test_two_cases_same_source_share_applied_fact_and_never_duplicate_execution(recovery_http):
    import asyncio
    client, state, nodes = recovery_http
    ref, operations = source(state, nodes)
    inv = await inventory(client, [ref])
    (a, _), (b, _) = await asyncio.gather(case(client, inv, ref, operations, "case-a"), case(client, inv, ref, operations, "case-b"))
    assert a["recoveryId"] != b["recoveryId"]
    await post(client, "/harness/recovery/inventories/" + inv["inventoryId"] + "/seal", {"version": 1}, "seal")
    assert (await client.post("/harness/recovery/cases/" + a["recoveryId"] + "/ack", json=ack_body(a))).status_code == 200
    # Equivalent immutable source facts settle the other case without redoing
    # the client transaction or allocating a successor/native producer.
    admit(state, "old")
    assert (await client.get("/harness/recovery/cases/" + b["recoveryId"])).json()["state"] == "completed"
    assert nodes.native_count == 0


async def test_known_server_operation_omitted_by_client_blocks_seal(recovery_http):
    client, state, nodes = recovery_http
    ref, operations = source(state, nodes)
    inv = await inventory(client, [ref])
    snapshot, _ = await case(client, inv, ref, [operations[-1]])
    await client.post("/harness/recovery/cases/" + snapshot["recoveryId"] + "/ack", json=ack_body(snapshot))
    missing = await client.post("/harness/recovery/inventories/" + inv["inventoryId"] + "/seal", json={"version": 1}, headers={"Idempotency-Key": "seal"})
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "recovery_server_sources_missing"
    assert missing.json()["error"]["serverOperations"][0]["turnId"] == "turn-old"


async def test_native_lost_ack_reconciles_receipt_without_second_action(recovery_http, monkeypatch):
    client, state, nodes = recovery_http
    ref, operations = source(state, nodes)
    state.store._exec("UPDATE router_bindings SET status='ready' WHERE id='old'")
    nodes.native_available = True
    inv = await inventory(client, [ref])
    initial, _ = await case(client, inv, ref, operations)
    await post(client, "/harness/recovery/inventories/" + inv["inventoryId"] + "/seal", {"version": 1}, "seal")
    path = "/harness/recovery/cases/" + initial["recoveryId"]
    await client.post(path + "/ack", json=ack_body(initial))
    ready = (await client.get(path)).json()
    action = next(a for a in ready["actions"] if a["kind"] == "resume_transport")
    body = {**cas(ready), "planId": ready["planId"], "actionIds": [action["id"]]}
    original = nodes.request

    async def lose_after_execution(node, method, target, **kwargs):
        response = await original(node, method, target, **kwargs)
        if method == "POST" and target.endswith("/apply"):
            rid = kwargs["request_id"]
            nodes.commands[rid] = {"requestId": rid, "state": "complete", "status": response.status_code, "response": response.json()}
            raise GateError(503, "command_outcome_unknown", "lost ACK")
        return response

    monkeypatch.setattr(nodes, "request", lose_after_execution)
    assert (await client.post(path + "/apply", json=body, headers={"Idempotency-Key": "native"})).status_code == 503
    changed = dict(body, actionIds=["different"])
    assert (await client.post(path + "/apply", json=changed, headers={"Idempotency-Key": "native"})).status_code == 409
    recovered = await post(client, path + "/apply", body, "native")
    assert recovered["steps"][0]["outcome"] == "accepted" and nodes.native_count == 1
    assert len([entry for entry in nodes.calls if entry[0] == "POST" and entry[1].endswith("/apply")]) == 1


async def test_generic_completed_500_is_not_negative_execution_proof(recovery_http):
    client, state, nodes = recovery_http
    ref, operations = source(state, nodes, terminal=False, tool=False)
    opid = state.operations.key(owner(state), "prompt-old")
    state.operations.complete(opid, httpx.Response(500, json={"detail": "failed after dispatch"}))
    inv = await inventory(client, [ref])
    result, _ = await case(client, inv, ref, operations)
    assert result["operations"][0]["outcome"] == "outcome_unknown"
    assert result["state"] == "needs_input" and not result["actions"]


def api_source(state, *, unknown=False, legacy=False):
    from atlas_gate.gate.api_attempts import ApiAttempts
    from atlas_gate.gate.orgs import Route
    route = Route(id="api", kind="gateway", protocol="openai", upstream_url="https://unused.invalid/v1", key_env="NO_TEST_API_KEY")
    state.orgs["org"].routes.append(route)
    state.api_attempts = ApiAttempts(state.store)
    payload = {"model": "api-model", "input": "saved work"}
    row, _ = state.api_attempts.claim(owner(state), route, "api-model", "/responses", json.dumps(payload).encode(),
        {"idempotency-key": "api-key", "x-atlas-conversation-id": "chat", "x-atlas-branch-id": "branch"}, branch="branch")
    state.api_attempts.dispatched(row["id"])
    state.api_attempts.response_headers(row["id"], 200, {"content-type": "application/json"})
    state.api_attempts.append(row["id"], b'{"status":"completed","output":[{"type":"message","content":"saved result"}]}')
    state.api_attempts.finish(row["id"], {"terminalKnown": not unknown, "outcome": "unknown" if unknown else "completed",
        "runState": "waiting-recovery" if unknown else "response-ready", "hostExecutionAllowed": False}, state="outcome_unknown" if unknown else "complete")
    if legacy:
        state.store._exec("UPDATE api_attempts SET body_sha256=NULL WHERE id=?", (row["id"],))
    return {"kind": "api_attempt", "attemptId": row["id"], "sourceRef": "api-source", "sourceRunId": "api-run"}, payload


async def api_case(client, inv, ref, payload, key="api-case", request_id="api-key"):
    return await post(client, "/harness/recovery/cases", {"version": 1, "inventoryId": inv["inventoryId"], "sourceRef": ref["sourceRef"],
        "sourceRunId": ref["sourceRunId"], "sourceCheckpointVersion": 3, "conversationId": "chat", "branchId": "branch",
        "source": {"kind": "api_attempt", "attemptId": ref["attemptId"], "routeId": "api", "model": "api-model"},
        "operations": [{"kind": "api_attempt", "requestId": request_id, "bodySha256": body_sha256(payload)}],
        "permissions": {"allowedRecovery": ["read_receipt", "apply_api_receipt", "resolve_source_checkpoint"], "businessReplayAllowed": False}}, key)


@pytest.mark.parametrize("unknown,legacy", [(False, False), (True, False), (False, True)])
async def test_api_source_result_unknown_and_legacy_body_identity(recovery_http, unknown, legacy):
    client, state, nodes = recovery_http
    ref, payload = api_source(state, unknown=unknown, legacy=legacy)
    inv = await inventory(client, [ref])
    snapshot = await api_case(client, inv, ref, payload)
    assert snapshot["sourceKind"] == "api_attempt" and not nodes.calls
    if unknown or legacy:
        assert not snapshot["actions"] and snapshot["state"] == "needs_input"
        assert snapshot["operations"][0]["outcome"] == ("evidence_unavailable" if legacy else "outcome_unknown")
        with pytest.raises(GateError):
            admit(state)
    else:
        assert {a["kind"] for a in snapshot["actions"]} == {"apply_api_receipt", "resolve_source_checkpoint"}
        proof = snapshot["evidence"][0]
        assert sha256_hex(proof["canonicalPayloadUTF8"]) == proof["evidenceSha256"]
        assert proof["body"]["payload"]["resultSha256"] == state.api_attempts.snapshot(ref["attemptId"], owner(state))["resultSha256"]
        await post(client, "/harness/recovery/inventories/" + inv["inventoryId"] + "/seal", {"version": 1}, "api-seal")
        response = await client.post("/harness/recovery/cases/" + snapshot["recoveryId"] + "/ack", json=ack_body(snapshot))
        assert response.status_code == 200, response.text
        admit(state)


async def native_ready(client, state, nodes):
    ref, operations = source(state, nodes)
    state.store._exec("UPDATE router_bindings SET status='ready' WHERE id='old'")
    nodes.native_available = True
    inv = await inventory(client, [ref])
    snapshot, _ = await case(client, inv, ref, operations)
    await post(client, "/harness/recovery/inventories/" + inv["inventoryId"] + "/seal", {"version": 1}, "seal")
    path = "/harness/recovery/cases/" + snapshot["recoveryId"]
    assert (await client.post(path + "/ack", json=ack_body(snapshot))).status_code == 200
    ready = (await client.get(path)).json()
    action = next(a for a in ready["actions"] if a["kind"] == "resume_transport")
    return path, ready, action


async def test_native_unknown_survives_complete_http409_and_auth_rejection(recovery_http):
    client, state, nodes = recovery_http
    path, ready, action = await native_ready(client, state, nodes)
    nodes.native_failure = True
    body = {**cas(ready), "planId": ready["planId"], "actionIds": [action["id"]]}
    first = await client.post(path + "/apply", json=body, headers={"Idempotency-Key": "native-unknown"})
    assert first.status_code == 503
    assert state.store._one("SELECT state FROM recovery_steps")["state"] == "unknown"
    nodes.native_denied = True
    repeated = await client.post(path + "/apply", json=body, headers={"Idempotency-Key": "native-unknown"})
    assert repeated.status_code == 503
    assert state.store._one("SELECT state FROM recovery_steps")["state"] == "unknown"
    current = (await client.get(path)).json()
    await post(client, path + "/cancel", cas(current), "cancel-unknown")
    assert state.store._one("SELECT state FROM recovery_steps")["state"] == "unknown"
    assert nodes.native_count == 1
    with pytest.raises(GateError):
        admit(state, "old")


async def test_prepared_step_cancel_and_restart_settles_without_native_dispatch(recovery_http):
    from atlas_gate.gate.recovery import _id
    client, state, nodes = recovery_http
    path, ready, action = await native_ready(client, state, nodes)
    body = {**cas(ready), "planId": ready["planId"], "actionIds": [action["id"]]}
    cid, branch = ready["recoveryId"], state.store._one("SELECT * FROM recovery_branches")
    device = owner(state)
    with state.recovery.transaction() as db:
        db.execute("INSERT INTO recovery_steps(id,branch_id,case_id,action_id,state,result,descriptor) VALUES (?,?,?,?,'prepared',NULL,?)",
                   (_id(cid, action["id"]), branch["id"], cid, action["id"], json.dumps(action)))
        state.recovery._request(db, device, "crashed-apply", "apply:" + cid, body, {"pending": True})
    await post(client, path + "/cancel", cas(ready), "cancel-prepared")
    filename = state.store._one("PRAGMA database_list")["file"]
    state.store.close()
    state.store = Store(filename)
    state.operations, state.routing = Operations(state.store), Routing(state.store, nodes)
    state.recovery = Recovery(state)
    settled = await post(client, path + "/apply", body, "crashed-apply")
    assert settled["steps"][0]["outcome"] == "rejected_not_sent"
    assert nodes.native_count == 0
    # Resolved old source + explicit new inventory can proceed; the abandoned
    # prepared claim must not become a permanent global writer barrier.
    new = await inventory(client, [], 2)
    await post(client, "/harness/recovery/inventories/" + new["inventoryId"] + "/seal", {"version": 1}, "new-seal")
    admit(state, "old")


async def test_ready_registered_branch_rejects_late_unfenced_legacy_callback(recovery_http):
    client, state, nodes = recovery_http
    # A branch that has never registered recovery preserves legacy admission.
    state.recovery.check_admission(owner(state), {"id": "unregistered"})
    ref, operations = source(state, nodes)
    inv = await inventory(client, [ref])
    snapshot, _ = await case(client, inv, ref, operations)
    await post(client, "/harness/recovery/inventories/" + inv["inventoryId"] + "/seal", {"version": 1}, "seal")
    await client.post("/harness/recovery/cases/" + snapshot["recoveryId"] + "/ack", json=ack_body(snapshot))
    with pytest.raises(GateError) as blocked:
        state.recovery.check_admission(owner(state), {"id": "old"})
    assert blocked.value.code == "recovery_required"
    admit(state, "old")


async def test_api_prepared_dispatch_exclusion_never_hides_running_attempt(recovery_http):
    client, state, nodes = recovery_http
    ref, payload = api_source(state)
    inv = await inventory(client, [ref])
    snapshot = await api_case(client, inv, ref, payload)
    await post(client, "/harness/recovery/inventories/" + inv["inventoryId"] + "/seal", {"version": 1}, "seal")
    await client.post("/harness/recovery/cases/" + snapshot["recoveryId"] + "/ack", json=ack_body(snapshot))
    route = state.orgs["org"].route("api")
    row, _ = state.api_attempts.claim(owner(state), route, "api-model", "/responses", json.dumps(payload).encode(),
        {"idempotency-key": "new-api", "x-atlas-conversation-id": "chat", "x-atlas-branch-id": "branch"}, branch="branch")
    with pytest.raises(GateError):
        admit(state)
    admit(state, admitting_api_attempt_id=row["id"])
    state.api_attempts.dispatched(row["id"])
    with pytest.raises(GateError):
        admit(state, admitting_api_attempt_id=row["id"])


async def test_partial_historical_ack_after_cancel_does_not_drop_required_actions(recovery_http):
    client, state, nodes = recovery_http
    ref, operations = source(state, nodes)
    inv = await inventory(client, [ref])
    original, _ = await case(client, inv, ref, operations)
    path = "/harness/recovery/cases/" + original["recoveryId"]
    await post(client, path + "/cancel", cas(original), "cancel")
    hidden = (await client.get(path)).json()
    assert hidden["actions"] == []
    partial = ack_body(original)
    partial["appliedActions"] = partial["appliedActions"][:1]
    response = await client.post(path + "/ack", json=partial)
    assert response.status_code == 200 and response.json()["state"] == "historical-applied"
    stored = state.store._one("SELECT * FROM recovery_cases WHERE id=?", (original["recoveryId"],))
    assert not stored["resolved"] and len(json.loads(stored["required_actions"])) == 2


async def test_inherited_api_receipt_hash_does_not_substitute_checkpoint_resolution(recovery_http):
    client, state, nodes = recovery_http
    ref, payload = api_source(state)
    inv = await inventory(client, [ref])
    a = await api_case(client, inv, ref, payload, "case-a")
    b = await api_case(client, inv, ref, payload, "case-b")
    assert a["actions"][0]["evidenceSha256"] == a["actions"][1]["evidenceSha256"]
    receipt = ack_body(a)
    receipt["appliedActions"] = [entry for entry in receipt["appliedActions"] if entry["actionId"] == next(action["id"] for action in a["actions"] if action["kind"] == "apply_api_receipt")]
    assert (await client.post("/harness/recovery/cases/" + a["recoveryId"] + "/ack", json=receipt)).status_code == 200
    observed = (await client.get("/harness/recovery/cases/" + b["recoveryId"])).json()
    assert observed["state"] != "completed"
    assert all(not row["resolved"] for row in state.store._all("SELECT resolved FROM recovery_cases"))


async def test_restarted_router_terminal_unknown_is_not_completed_business_proof(recovery_http):
    client, state, nodes = recovery_http
    ref, operations = source(state, nodes, tool=False)
    nodes.turns["turn-old"] = [{"type": "error", "id": "turn-old", "seq": 1,
        "kind": "command_outcome_unknown", "message": "producer lost; effects may exist"}]
    inv = await inventory(client, [ref])
    snapshot, _ = await case(client, inv, ref, operations)
    assert snapshot["operations"][0]["outcome"] == "effect_unknown"
    assert snapshot["state"] == "needs_input" and not snapshot["actions"]
    assert snapshot["evidence"][0]["body"]["payload"]["terminal"]["kind"] == "command_outcome_unknown"


def test_machine_wire_fixture_schemas_canonical_vectors_and_references():
    from pathlib import Path
    from atlas_gate.gate.recovery import Ack, Apply, CAPABILITY, Cancel, CaseCreate, Evidence, InventoryCreate, Versioned, _json
    fixture = json.loads((Path(__file__).parents[1] / "docs/fixtures/session-recovery-v1.json").read_text(encoding="utf-8"))
    models = {"inventoryCreate": InventoryCreate, "inventorySeal": Versioned, "managedCaseCreate": CaseCreate,
              "apiCaseCreate": CaseCreate, "clientAck": Ack, "apiClientAck": Ack, "serverApply": Apply,
              "evidenceClaim": Evidence, "cancel": Cancel}
    for name, model in models.items():
        model.model_validate(fixture["requests"][name]["body"])
    assert fixture["capability"] == CAPABILITY
    for vector in fixture["canonicalVectors"]:
        assert _json(vector["input"]) == vector["canonicalUTF8"]
        assert sha256_hex(vector["canonicalUTF8"]) == vector["sha256"]
    for name, request in (("casePlan", "managedCaseCreate"), ("apiCasePlan", "apiCaseCreate")):
        plan = fixture["responses"][name]
        assert body_sha256(fixture["requests"][request]["body"]) == plan["sourceManifestSha256"]
        for evidence in plan["evidence"]:
            assert _json(evidence["body"]) == evidence["canonicalPayloadUTF8"]
            assert sha256_hex(evidence["canonicalPayloadUTF8"]) == evidence["evidenceSha256"]
    apply = fixture["requests"]["serverApply"]["body"]
    transport = fixture["responses"]["transportPlan"]
    assert apply["planId"] == transport["planId"] and apply["expectedRevision"] == transport["revision"]
    assert all(any(action["id"] == aid and action["executor"] == "gate" for action in transport["actions"]) for aid in apply["actionIds"])


async def test_old_session_event_replay_does_not_change_current_native_binding(recovery_http):
    client, state, nodes = recovery_http
    ref, operations = source(state, nodes, tool=False)
    nodes.turns["turn-old"] = [{"type": "session", "id": "turn-old", "seq": 1, "sessionId": "obsolete-native"},
                               {"type": "result", "id": "turn-old", "seq": 2, "ok": True, "text": "saved result"}]
    inv = await inventory(client, [ref])
    snapshot, _ = await case(client, inv, ref, operations)
    assert state.store._one("SELECT sdk_id FROM router_bindings WHERE id='old'")["sdk_id"] == "native-local-old"
    assert "obsolete-native" not in json.dumps(snapshot)
    assert snapshot["evidence"][0]["body"]["payload"]["events"][0]["sessionId"] == "alias-old"


async def test_api_producer_liveness_change_does_not_invalidate_immutable_apply_proof(recovery_http, monkeypatch):
    client, state, nodes = recovery_http
    ref, payload = api_source(state)
    original = state.api_attempts.snapshot
    alive = True

    def with_liveness(attempt_id, device):
        return {**original(attempt_id, device), "producerAlive": alive, "durableState": "complete"}

    monkeypatch.setattr(state.api_attempts, "snapshot", with_liveness)
    inv = await inventory(client, [ref])
    first = await api_case(client, inv, ref, payload)
    await post(client, "/harness/recovery/inventories/" + inv["inventoryId"] + "/seal", {"version": 1}, "seal")
    alive = False
    path = "/harness/recovery/cases/" + first["recoveryId"]
    latest = (await client.get(path)).json()
    assert latest["evidence"][0]["evidenceSha256"] == first["evidence"][0]["evidenceSha256"]
    assert (await client.post(path + "/ack", json=ack_body(first))).status_code == 200
    admit(state)


async def test_proven_writer_busy_can_retry_new_transport_action_without_model_replay(recovery_http):
    client, state, nodes = recovery_http
    path, ready, action = await native_ready(client, state, nodes)
    nodes.native_busy = True
    body = {**cas(ready), "planId": ready["planId"], "actionIds": [action["id"]]}
    rejected = await post(client, path + "/apply", body, "native-busy")
    assert rejected["steps"][0]["outcome"] == "rejected_not_sent"
    assert rejected["steps"][0]["scope"] == "transport_only"
    with pytest.raises(GateError):
        admit(state, "old")
    nodes.native_busy = False
    fresh = (await client.get(path)).json()
    next_action = next(a for a in fresh["actions"] if a["kind"] == "resume_transport")
    assert next_action["id"] != action["id"]
    accepted = await post(client, path + "/apply", {**cas(fresh), "planId": fresh["planId"], "actionIds": [next_action["id"]]}, "native-after-busy")
    assert accepted["steps"][0]["outcome"] == "accepted"
    admit(state, "old")
    assert nodes.native_count == 2
    assert not any(path.endswith(("/prompt", "/tool_result")) for _, path, _ in nodes.calls)


async def test_managed_api_admission_fence_settles_source_without_provider_dispatch(recovery_http):
    client, state, nodes = recovery_http
    _, payload = api_source(state)
    route = state.orgs["org"].route("api")
    proof = state.api_attempts.reconcile(owner(state), route, "api-model", "/responses", "unsubmitted-api",
        body_sha256(payload), conversation_id="chat", branch_id="branch")
    ref = {"kind": "api_attempt", "attemptId": proof["attemptId"], "sourceRef": "sealed-api", "sourceRunId": "old-api-run"}
    inv = await inventory(client, [ref])
    snapshot = await api_case(client, inv, ref, payload, request_id="unsubmitted-api")
    assert snapshot["operations"][0]["outcome"] == "sealed_not_sent"
    assert [a["kind"] for a in snapshot["actions"]] == ["resolve_source_checkpoint"]
    assert snapshot["evidence"][0]["body"]["payload"]["attempt"]["notSentProven"] is True
    assert snapshot["evidence"][0]["body"]["payload"]["attempt"]["providerDispatch"] is False
    await post(client, "/harness/recovery/inventories/" + inv["inventoryId"] + "/seal", {"version": 1}, "seal")
    assert (await client.post("/harness/recovery/cases/" + snapshot["recoveryId"] + "/ack", json=ack_body(snapshot))).status_code == 200
    row, fresh = state.api_attempts.claim(owner(state), route, "api-model", "/responses", json.dumps(payload).encode(),
        {"idempotency-key": "unsubmitted-api", "x-atlas-conversation-id": "chat", "x-atlas-branch-id": "branch"}, branch="branch")
    assert not fresh and row["id"] == proof["attemptId"] and row["state"] == "rejected_not_sent"
    admit(state)
    assert not nodes.calls


async def test_policy_revoked_after_plan_is_not_sent_and_fresh_plan_can_retry(recovery_http):
    client, state, nodes = recovery_http
    path, ready, action = await native_ready(client, state, nodes)
    state.bodies["org"]["models"][0]["enabled"] = False
    body = {**cas(ready), "planId": ready["planId"], "actionIds": [action["id"]]}
    denied = await client.post(path + "/apply", json=body, headers={"Idempotency-Key": "denied-native"})
    assert denied.status_code == 403 and nodes.native_count == 0
    step = state.store._one("SELECT * FROM recovery_steps")
    assert step["state"] == "complete" and json.loads(step["result"])["outcome"] == "rejected_not_sent"
    current = (await client.get(path)).json()
    assert current["resume"]["policyError"] == "policy_denied" and not [a for a in current["actions"] if a["executor"] == "gate"]
    state.bodies["org"]["models"][0]["enabled"] = True
    fresh = (await client.get(path)).json()
    retry = next(a for a in fresh["actions"] if a["kind"] == "resume_transport")
    assert retry["id"] != action["id"] and retry["previousRejectedStepId"] == step["id"]
    accepted = await post(client, path + "/apply", {**cas(fresh), "planId": fresh["planId"], "actionIds": [retry["id"]]}, "permitted-native")
    assert accepted["steps"][0]["outcome"] == "accepted" and nodes.native_count == 1
