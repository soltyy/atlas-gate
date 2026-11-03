"""Durable recovery coordinator. SDK execution and business replay never belong to Gate.

Local transactions contain only ledger operations. Router observations and native
transport actions run outside SQLite transactions and have durable action identities.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import uuid
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .api import gate, org_of, require_device
from .errors import GateError, GateRoute
from .operations import body_sha256
from .receipts import completed_response
from .history_identity import CAPABILITY as SOURCE_HISTORY_IDENTITY

CAPABILITY = {
    "version": 1, "supported": True, "protocol": "session-recovery-coordinator-v1",
    "inventoriesPath": "/harness/recovery/inventories",
    "casesPath": "/harness/recovery/cases",
    "operations": ["inventory", "seal", "read_receipts", "seal_prompt", "client_apply", "apply_api_receipt", "cancel", "transport"],
    "sourceKinds": ["managed_session", "api_attempt"],
    "businessReplayAllowed": False, "clientAppliedRequired": True,
    "retention": {"caseReceipts": "until_explicit_retirement", "proofPayload": "source_retention"},
    "canonicalBody": "json-sort-keys-compact-utf8-v1",
    "exactPinnedPath": True,
    "exactPinnedPathMeaning": "native actions require exact node/boot/transport proof; not a union resume guarantee",
    "continuation": {"supported": False, "reason": "terminal_continuation_adapter_not_qualified"},
    "sourceHistoryIdentity": dict(SOURCE_HISTORY_IDENTITY),
}


class Versioned(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1]

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int:
            raise ValueError("version must be integer 1")
        return value


class SourceRef(Versioned):
    version: Literal[1] = 1
    sourceRef: str = Field(min_length=1, max_length=200)
    sourceRunId: str = Field(min_length=1, max_length=200)
    kind: Literal["managed_session", "api_attempt"] = "managed_session"
    sessionId: str | None = Field(default=None, min_length=1, max_length=200)
    attemptId: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def exact_source(self):
        if self.kind == "managed_session" and (not self.sessionId or self.attemptId):
            raise ValueError("managed_session requires sessionId only")
        if self.kind == "api_attempt" and (not self.attemptId or self.sessionId):
            raise ValueError("api_attempt requires attemptId only")
        return self


class InventoryCreate(Versioned):
    conversationId: str = Field(min_length=1, max_length=200)
    branchId: str = Field(min_length=1, max_length=200)
    clientStoreEpoch: str = Field(min_length=1, max_length=200)
    checkpointInventoryVersion: int = Field(ge=0)
    sources: list[SourceRef]


class CaseSource(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    gateRoot: str | None = None
    sessionId: str = Field(min_length=1, max_length=200)
    historyAlias: str = Field(min_length=1, max_length=200)
    routeId: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=200)
    kind: Literal["managed_session"] = "managed_session"


class ApiSource(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["api_attempt"]
    gateRoot: str | None = None
    attemptId: str = Field(min_length=1, max_length=200)
    routeId: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=200)


class Operation(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    kind: Literal["prompt", "tool_result", "compact", "tools", "interrupt", "tool_effect", "api_attempt"]
    requestId: str = Field(min_length=1, max_length=200)
    bodySha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    turnId: str | None = Field(default=None, min_length=1, max_length=200)
    callId: str | None = Field(default=None, min_length=1, max_length=200)


class CaseCreate(Versioned):
    model_config = ConfigDict(extra="allow")
    inventoryId: str = Field(min_length=1, max_length=200)
    sourceRef: str = Field(min_length=1, max_length=200)
    conversationId: str = Field(min_length=1, max_length=200)
    branchId: str = Field(min_length=1, max_length=200)
    sourceRunId: str = Field(min_length=1, max_length=200)
    source: CaseSource | ApiSource
    operations: list[Operation] = Field(min_length=1)
    sourceCheckpointVersion: int = Field(default=0, ge=0)
    observation: dict[str, Any] = Field(default_factory=dict)
    control: dict[str, Any] = Field(default_factory=dict)
    permissions: dict[str, Any] = Field(default_factory=dict)
    transportGoal: Literal["ready", "released", "none"] = "ready"


class Fence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    recoveryGeneration: int = Field(ge=1)
    controlEpoch: int = Field(ge=0)


class Apply(Versioned):
    planId: str
    expectedRevision: int = Field(ge=1)
    sourceManifestSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    branchFence: Fence
    actionIds: list[str] = Field(min_length=1)


class AppliedAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    actionId: str
    evidenceSha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Ack(Versioned):
    ackKey: str = Field(min_length=1, max_length=200)
    planId: str
    planRevision: int = Field(ge=1)
    sourceManifestSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    clientStoreEpoch: str = Field(min_length=1, max_length=200)
    sourceRunId: str
    sourceSessionId: str | None = None
    sourceAttemptId: str | None = None
    sourceCheckpointVersion: int = Field(ge=0)
    appliedActions: list[AppliedAction] = Field(min_length=1)
    localCommitRef: str = Field(min_length=1, max_length=200)
    localCommitSha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def exact_source(self):
        if bool(self.sourceSessionId) == bool(self.sourceAttemptId):
            raise ValueError("ACK requires exactly one sourceSessionId or sourceAttemptId")
        return self


class Evidence(Versioned):
    kind: str = Field(min_length=1, max_length=100)
    claim: dict[str, Any]


class Cancel(Versioned):
    expectedRevision: int = Field(ge=1)
    sourceManifestSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    branchFence: Fence


DDL = """
CREATE TABLE IF NOT EXISTS recovery_branches(
 id TEXT PRIMARY KEY, device_id TEXT NOT NULL, org TEXT NOT NULL,
 conversation_id TEXT NOT NULL, branch_id TEXT NOT NULL,
 generation INTEGER NOT NULL, control_epoch INTEGER NOT NULL, inventory_id TEXT,
 UNIQUE(device_id,org,conversation_id,branch_id));
CREATE TABLE IF NOT EXISTS recovery_inventories(
 id TEXT PRIMARY KEY, branch_id TEXT NOT NULL, body TEXT NOT NULL,
 version INTEGER NOT NULL, client_epoch TEXT NOT NULL, generation INTEGER NOT NULL,
 sealed INTEGER NOT NULL DEFAULT 0, seal_hash TEXT);
CREATE TABLE IF NOT EXISTS recovery_sources(
 inventory_id TEXT NOT NULL, source_ref TEXT NOT NULL, run_id TEXT NOT NULL,
 session_id TEXT NOT NULL, PRIMARY KEY(inventory_id,source_ref));
CREATE TABLE IF NOT EXISTS recovery_cases(
 id TEXT PRIMARY KEY, branch_id TEXT NOT NULL, inventory_id TEXT NOT NULL,
 source_ref TEXT NOT NULL, device_id TEXT NOT NULL, org TEXT NOT NULL,
 session_id TEXT NOT NULL, run_id TEXT NOT NULL, manifest TEXT NOT NULL,
 manifest_hash TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 resolved INTEGER NOT NULL DEFAULT 0, cancelled INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL DEFAULT 'collecting', plan_id TEXT, snapshot TEXT,
 required_actions TEXT NOT NULL DEFAULT '[]', proof_ready INTEGER NOT NULL DEFAULT 0,
 transport_required INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS recovery_requests(
 device_id TEXT NOT NULL, org TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL,
 response TEXT NOT NULL, PRIMARY KEY(device_id,org,key));
CREATE TABLE IF NOT EXISTS recovery_evidence(
 id TEXT PRIMARY KEY, case_id TEXT NOT NULL, digest TEXT NOT NULL, body TEXT NOT NULL,
 UNIQUE(case_id,digest));
CREATE TABLE IF NOT EXISTS recovery_plans(
 id TEXT PRIMARY KEY, case_id TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recovery_applied(
 case_id TEXT NOT NULL, action_id TEXT NOT NULL, evidence_hash TEXT NOT NULL,
 receipt TEXT NOT NULL, action_kind TEXT NOT NULL DEFAULT '', PRIMARY KEY(case_id,action_id));
CREATE TABLE IF NOT EXISTS recovery_steps(
 id TEXT PRIMARY KEY, branch_id TEXT NOT NULL, case_id TEXT NOT NULL, action_id TEXT NOT NULL,
 state TEXT NOT NULL, result TEXT, descriptor TEXT NOT NULL DEFAULT '{}', UNIQUE(case_id,action_id));
CREATE TABLE IF NOT EXISTS recovery_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
"""


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        _fail("recovery_manifest_invalid", "Manifest должен содержать canonical JSON без NaN/Infinity", 422)


def _id(*parts):
    return hashlib.sha256(_json(parts).encode("utf-8")).hexdigest()


def _fail(code, message, status=409, **extra):
    raise GateError(status, code, message, **extra)


def _key(request):
    value = request.headers.get("idempotency-key")
    if not value or len(value) > 200:
        _fail("recovery_key_required", "Нужен устойчивый Idempotency-Key длиной 1..200", 422)
    return value


def _source_id(source):
    return "api:" + source["attemptId"] if source.get("kind") == "api_attempt" else source["sessionId"]


class Recovery:
    def __init__(self, state):
        self.state, self.store = state, state.store
        with self.store._lock:
            self.store._db.executescript(DDL)
            if "descriptor" not in {r[1] for r in self.store._db.execute("PRAGMA table_info(recovery_steps)")}:
                self.store._db.execute("ALTER TABLE recovery_steps ADD COLUMN descriptor TEXT NOT NULL DEFAULT '{}'")
            for table, name, definition in (("recovery_cases", "required_actions", "TEXT NOT NULL DEFAULT '[]'"),
                                             ("recovery_cases", "proof_ready", "INTEGER NOT NULL DEFAULT 0"),
                                             ("recovery_cases", "transport_required", "INTEGER NOT NULL DEFAULT 0"),
                                             ("recovery_applied", "action_kind", "TEXT NOT NULL DEFAULT ''")):
                if name not in {r[1] for r in self.store._db.execute(f"PRAGMA table_info({table})")}:
                    self.store._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            self.store._db.execute("INSERT OR IGNORE INTO recovery_meta VALUES ('store_epoch',?)", (str(uuid.uuid4()),))
        self.store_epoch = self.store._one("SELECT value FROM recovery_meta WHERE key='store_epoch'")["value"]

    @contextlib.contextmanager
    def transaction(self):
        with self.store._lock:
            db = self.store._db
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    with contextlib.suppress(sqlite3.Error):
                        db.execute("ROLLBACK")
                raise

    def authorize(self, device, request, session_id=None, *, native=False):
        current = self.store.device(device.device_id)
        if not current or current["org"] != device.org or current["revoked_at"] is not None:
            _fail("device_revoked", "Устройство отозвано или сменило владельца", 401)
        org = org_of(self.state, device)
        if org.archived:
            _fail("org_archived", "Организация в архиве", 403)
        if session_id is None:
            return None
        if session_id.startswith("api:"):
            if native:
                _fail("recovery_action_unsupported", "API attempt не имеет SDK transport", 422)
            manager = getattr(self.state, "api_attempts", None)
            if manager is None:
                _fail("recovery_source_unsupported", "API attempt ledger не поддержан", 422)
            row = manager.owned(session_id[4:], device)
            route = org.route(row["route"])
            if route is None or route.kind != "gateway":
                _fail("policy_denied", "Исходный API route больше не разрешён", 403)
            from .api import check_data_class
            check_data_class(self.state, device, request, route.id)
            return {"id": session_id, "route": row["route"], "model": row["model"], "status": row["state"], "api_attempt": row}
        binding = self.store._one("SELECT * FROM router_bindings WHERE id=? AND device_id=? AND org=?",
                                  (session_id, device.device_id, device.org))
        if not binding:
            _fail("not_found", "Исходная привязка не найдена", 404)
        from .agent import _policy
        _policy(self.state, device, binding, request, require_model=native, require_sdk=native)
        if self.state.nodes.revoked(binding["node_id"]):
            _fail("node_unavailable", "Исходный Router отозван", 403)
        return binding

    def _request(self, db, device, key, scope, body, response=None):
        fingerprint = _id(scope, body)
        old = db.execute("SELECT * FROM recovery_requests WHERE device_id=? AND org=? AND key=?",
                         (device.device_id, device.org, key)).fetchone()
        if old:
            if old["fingerprint"] != fingerprint:
                _fail("idempotency_conflict", "Recovery key уже связан с другим неизменяемым запросом")
            return json.loads(old["response"])
        if response is not None:
            db.execute("INSERT INTO recovery_requests VALUES (?,?,?,?,?)",
                       (device.device_id, device.org, key, fingerprint, _json(response)))
        return None

    def _inventory(self, inventory_id, device):
        row = self.store._one("SELECT i.*,b.device_id,b.org,b.conversation_id,b.branch_id logical_branch "
                              "FROM recovery_inventories i JOIN recovery_branches b ON b.id=i.branch_id "
                              "WHERE i.id=? AND b.device_id=? AND b.org=?", (inventory_id, device.device_id, device.org))
        if not row:
            _fail("not_found", "Inventory не найден", 404)
        return row

    def _case(self, case_id, device, request):
        row = self.store._one("SELECT * FROM recovery_cases WHERE id=? AND device_id=? AND org=?",
                              (case_id, device.device_id, device.org))
        if not row:
            _fail("not_found", "Recovery case не найден", 404)
        self.authorize(device, request, row["session_id"])
        return row

    def create_inventory(self, device, request, body, key):
        self.authorize(device, request)
        refs = body["sources"]
        if len({s["sourceRef"] for s in refs}) != len(refs):
            _fail("recovery_source_conflict", "sourceRef должен быть уникальным", 422)
        for source in refs:
            self.authorize(device, request, _source_id(source))
        bid = _id(device.org, device.device_id, body["conversationId"], body["branchId"])
        with self.transaction() as db:
            old = self._request(db, device, key, "inventory", body)
            if old:
                return old
            branch = db.execute("SELECT * FROM recovery_branches WHERE id=?", (bid,)).fetchone()
            declared = {(s["sourceRunId"], _source_id(s)) for s in refs}
            carry = db.execute("SELECT DISTINCT run_id,session_id FROM recovery_cases WHERE branch_id=? AND resolved=0", (bid,)).fetchall()
            omitted = [{"sourceRunId": r["run_id"], "sessionId": r["session_id"]} for r in carry if (r["run_id"], r["session_id"]) not in declared]
            if omitted:
                _fail("recovery_inventory_incomplete", "Новый snapshot не может отбросить прежние unresolved sources", missingSources=omitted)
            if branch and branch["inventory_id"]:
                previous = db.execute("SELECT * FROM recovery_inventories WHERE id=?", (branch["inventory_id"],)).fetchone()
                if previous["client_epoch"] != body["clientStoreEpoch"]:
                    _fail("recovery_store_epoch_changed", "Смена client store epoch требует явного handoff/quarantine")
                if body["checkpointInventoryVersion"] <= previous["version"]:
                    _fail("recovery_inventory_conflict", "Новый snapshot должен иметь более новую inventory version")
            generation = branch["generation"] + 1 if branch else 1
            iid = str(uuid.uuid4())
            if branch:
                self._settle_prepared(db, bid, "inventory_superseded")
                db.execute("UPDATE recovery_branches SET generation=?,inventory_id=? WHERE id=?", (generation, iid, bid))
            else:
                db.execute("INSERT INTO recovery_branches VALUES (?,?,?,?,?,?,0,?)",
                           (bid, device.device_id, device.org, body["conversationId"], body["branchId"], generation, iid))
            db.execute("INSERT INTO recovery_inventories(id,branch_id,body,version,client_epoch,generation) VALUES (?,?,?,?,?,?)",
                       (iid, bid, _json(body), body["checkpointInventoryVersion"], body["clientStoreEpoch"], generation))
            for source in refs:
                db.execute("INSERT INTO recovery_sources VALUES (?,?,?,?)",
                           (iid, source["sourceRef"], source["sourceRunId"], _source_id(source)))
            result = {"version": 1, "inventoryId": iid, "state": "collecting", "sealed": False,
                      "branchFence": {"recoveryGeneration": generation, "controlEpoch": branch["control_epoch"] if branch else 0}}
            self._request(db, device, key, "inventory", body, result)
            return result

    def seal(self, device, request, inventory_id, body, key):
        inventory = self._inventory(inventory_id, device)
        self.authorize(device, request)
        for source in self.store._all("SELECT * FROM recovery_sources WHERE inventory_id=?", (inventory_id,)):
            self.authorize(device, request, source["session_id"])
        with self.transaction() as db:
            old = self._request(db, device, key, "seal:" + inventory_id, body)
            if old:
                return old
            branch = db.execute("SELECT * FROM recovery_branches WHERE id=?", (inventory["branch_id"],)).fetchone()
            if branch["inventory_id"] != inventory_id:
                _fail("recovery_revision_conflict", "Inventory уже заменён новым snapshot")
            missing = db.execute("SELECT s.source_ref FROM recovery_sources s WHERE s.inventory_id=? AND NOT EXISTS "
                                 "(SELECT 1 FROM recovery_cases c WHERE c.inventory_id=s.inventory_id AND c.source_ref=s.source_ref)",
                                 (inventory_id,)).fetchall()
            if missing:
                _fail("recovery_inventory_incomplete", "Не все source checkpoints зарегистрированы",
                      missingSources=[r[0] for r in missing])
            covered = set()
            for row in db.execute("SELECT manifest FROM recovery_cases WHERE inventory_id=?", (inventory_id,)).fetchall():
                saved = json.loads(row[0])
                for operation in saved["operations"]:
                    covered.add(_source_id(saved["source"]) if operation["kind"] == "api_attempt" else self.state.operations.key(device, operation["requestId"]))
            omitted = [op for op in self._known_pending(db, inventory["branch_id"]) if op["commandId"] not in covered]
            if omitted:
                _fail("recovery_server_sources_missing", "Inventory не содержит известные серверу незакрытые операции",
                      serverOperations=omitted)
            digest = body_sha256(json.loads(inventory["body"]))
            db.execute("UPDATE recovery_inventories SET sealed=1,seal_hash=? WHERE id=?", (digest, inventory_id))
            result = {"version": 1, "inventoryId": inventory_id, "sealed": True, "sha256": digest,
                      "branchFence": {"recoveryGeneration": branch["generation"], "controlEpoch": branch["control_epoch"]}}
            self._request(db, device, key, "seal:" + inventory_id, body, result)
            return result

    def create_case(self, device, request, body, key):
        inventory = self._inventory(body["inventoryId"], device)
        source = body["source"]
        api = source.get("kind") == "api_attempt"
        binding = self.authorize(device, request, _source_id(source))
        if (body["conversationId"] != inventory["conversation_id"] or body["branchId"] != inventory["logical_branch"]
                or source.get("routeId") != binding["route"] or source.get("model") != binding["model"]
                or (not api and source.get("historyAlias") != binding["sdk_alias"])):
            _fail("recovery_source_conflict", "Manifest не соответствует исходной branch/binding")
        if api:
            row = binding["api_attempt"]
            if ((row["conversation"] is not None and row["conversation"] != body["conversationId"])
                    or (row["branch"] is not None and row["branch"] != body["branchId"])):
                _fail("recovery_source_conflict", "API attempt принадлежит другой conversation/branch")
        if (source.get("gateRoot") or self.state.public_url).rstrip("/") != self.state.public_url.rstrip("/"):
            _fail("recovery_root_unsupported", "Другой Gate требует отдельной авторизации/federation", 422)
        seen = set()
        for operation in body["operations"]:
            kind = operation.get("kind")
            if kind not in (("api_attempt",) if api else ("prompt", "tool_result", "compact", "tools", "interrupt", "tool_effect")):
                _fail("recovery_operation_unsupported", "Неподдержанный kind операции", 422)
            key_tuple = (kind, operation.get("requestId"))
            if not key_tuple[1] or key_tuple in seen:
                _fail("recovery_source_conflict", "Операции должны иметь уникальные исходные keys", 422)
            seen.add(key_tuple)
            digest = operation.get("bodySha256", "")
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                _fail("recovery_manifest_incomplete", "Нужен исходный canonical body SHA256", 422)
            if kind in ("prompt", "tool_result") and not operation.get("turnId"):
                _fail("recovery_manifest_incomplete", "Нужен exact source turn", 422)
            if kind == "tool_result" and not operation.get("callId"):
                _fail("recovery_manifest_incomplete", "Нужен exact source call", 422)
            if operation.get("sourceRunId", body["sourceRunId"]) != body["sourceRunId"] or operation.get("sessionId", binding["id"]) != binding["id"]:
                _fail("recovery_source_conflict", "Операция другого Run/binding не допускается")
            if api and (hashlib.sha256(operation["requestId"].encode()).hexdigest() != binding["api_attempt"]["key_hash"]
                        or operation.get("attemptId", binding["api_attempt"]["id"]) != binding["api_attempt"]["id"]
                        or (binding["api_attempt"].get("body_sha256") and binding["api_attempt"]["body_sha256"] != operation["bodySha256"])):
                _fail("recovery_source_conflict", "API attempt key/body не соответствуют manifest")
            old_op = None if api else self.store._one("SELECT * FROM router_operations WHERE id=?", (self.state.operations.key(device, operation["requestId"]),))
            if old_op and (old_op["session_id"] != binding["id"] or not old_op["path"].endswith("/" + kind)
                           or old_op["body_sha256"] and old_op["body_sha256"] != operation["bodySha256"]
                           or kind == "prompt" and old_op["turn_id"] != operation["turnId"]):
                _fail("recovery_source_conflict", "Исходный manifest не соответствует Gate operation ledger")
        with self.transaction() as db:
            old = self._request(db, device, key, "case", body)
            if old:
                return old
            branch = db.execute("SELECT * FROM recovery_branches WHERE id=?", (inventory["branch_id"],)).fetchone()
            if branch["inventory_id"] != inventory["id"]:
                _fail("recovery_revision_conflict", "Inventory уже заменён")
            ref = db.execute("SELECT * FROM recovery_sources WHERE inventory_id=? AND source_ref=?",
                             (inventory["id"], body["sourceRef"])).fetchone()
            if not ref or ref["run_id"] != body["sourceRunId"] or ref["session_id"] != binding["id"]:
                _fail("recovery_source_conflict", "Source отсутствует в inventory")
            supplied = {(o["kind"], o["requestId"], o["bodySha256"]) for o in body["operations"]}
            for prior in db.execute("SELECT manifest FROM recovery_cases WHERE branch_id=? AND run_id=? AND session_id=? AND resolved=0",
                                    (branch["id"], body["sourceRunId"], binding["id"])).fetchall():
                missing = [o["requestId"] for o in json.loads(prior["manifest"])["operations"]
                           if (o["kind"], o["requestId"], o["bodySha256"]) not in supplied]
                if missing:
                    _fail("recovery_manifest_incomplete", "Новый case не может отбросить прежние pending операции source", missingOperations=missing)
            cid = str(uuid.uuid4())
            db.execute("INSERT INTO recovery_cases(id,branch_id,inventory_id,source_ref,device_id,org,session_id,run_id,manifest,manifest_hash) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)", (cid, branch["id"], inventory["id"], body["sourceRef"], device.device_id, device.org,
                                                      binding["id"], body["sourceRunId"], _json(body), body_sha256(body)))
            result = {"version": 1, "recoveryId": cid, "state": "collecting", "revision": 1,
                      "sourceManifestSha256": body_sha256(body)}
            self._request(db, device, key, "case", body, result)
            return result

    def _known_pending(self, db, bid, admitting_api_attempt_id=None):
        rows = db.execute("SELECT o.id,o.session_id,o.turn_id,o.state,o.body_sha256 FROM router_operations o "
                          "WHERE o.session_id IN (SELECT s.session_id FROM recovery_sources s "
                          "JOIN recovery_inventories i ON i.id=s.inventory_id WHERE i.branch_id=?) "
                          "AND (o.state!='complete' OR (o.turn_id IS NOT NULL AND o.status<400 AND o.finished=0)) ORDER BY o.id", (bid,)).fetchall()
        result = [{"commandId": r["id"], "sessionId": r["session_id"], "turnId": r["turn_id"],
                   "state": r["state"], "bodySha256": r["body_sha256"]} for r in rows]
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='api_attempts'").fetchone():
            branch = db.execute("SELECT * FROM recovery_branches WHERE id=?", (bid,)).fetchone()
            attempts = db.execute("SELECT * FROM api_attempts WHERE device=? AND org=? AND conversation=? AND branch=?",
                                  (branch["device_id"], branch["org"], branch["conversation_id"], branch["branch_id"])).fetchall()
            for attempt in attempts:
                if attempt["id"] == admitting_api_attempt_id and attempt["state"] == "prepared":
                    continue  # Trusted dispatch callback, never a client exclusion.
                summary = json.loads(attempt["summary"])
                if attempt["state"] in ("prepared", "running", "outcome_unknown") or (attempt["state"] == "complete" and summary.get("terminalKnown") is not True):
                    result.append({"commandId": "api:" + attempt["id"], "attemptId": attempt["id"], "state": attempt["state"], "bodySha256": attempt["body_sha256"]})
        return result

    def _ready(self, db, bid, admitting_api_attempt_id=None, *, include_transport=True):
        branch = db.execute("SELECT * FROM recovery_branches WHERE id=?", (bid,)).fetchone()
        inventory = db.execute("SELECT * FROM recovery_inventories WHERE id=?", (branch["inventory_id"],)).fetchone()
        if not inventory or not inventory["sealed"]:
            return False, "inventory_unsealed"
        if db.execute("SELECT 1 FROM recovery_cases WHERE branch_id=? AND (resolved=0 OR (inventory_id=? AND cancelled=1)) LIMIT 1",
                      (bid, inventory["id"])).fetchone():
            return False, "source_unresolved"
        # Include known server operations even when a stale client omitted them.
        if self._known_pending(db, bid, admitting_api_attempt_id):
            return False, "server_operation_unresolved"
        if db.execute("SELECT 1 FROM recovery_steps WHERE branch_id=? AND state IN ('prepared','started','unknown') LIMIT 1", (bid,)).fetchone():
            return False, "action_outcome_unknown"
        if include_transport and db.execute("SELECT 1 FROM recovery_cases WHERE branch_id=? AND inventory_id=? AND transport_required=1 LIMIT 1",
                                           (bid, inventory["id"])).fetchone():
            return False, "transport_recovery_required"
        return True, None

    def check_admission(self, device, binding=None, *, conversation_id=None, branch_id=None,
                        inventory_id=None, generation=None, control_epoch=None, admitting_api_attempt_id=None, db=None):
        """Call inside admission validation, after immutable command replay lookup.

        Bindings already participating in recovery are fenced even without new
        client headers. Replacement bindings use explicit conversation/branch IDs.
        """
        with (self.transaction() if db is None else contextlib.nullcontext(db)) as db:
            current_device = db.execute("SELECT org,revoked_at FROM devices WHERE device_id=?", (device.device_id,)).fetchone()
            current_org = self.state.orgs.get(device.org)
            if not current_device or current_device["org"] != device.org or current_device["revoked_at"] is not None:
                _fail("device_revoked", "Устройство отозвано до dispatch", 401)
            if current_org is None or current_org.archived:
                _fail("policy_denied", "Организация больше не допускает dispatch", 403)
            if binding:
                rows = db.execute("SELECT DISTINCT b.* FROM recovery_branches b JOIN recovery_inventories i ON i.branch_id=b.id "
                                  "JOIN recovery_sources s ON s.inventory_id=i.id WHERE b.device_id=? AND b.org=? AND s.session_id=?",
                                  (device.device_id, device.org, binding["id"])).fetchall()
            else:
                rows = []
            if conversation_id is not None and branch_id is not None:
                extra = db.execute("SELECT * FROM recovery_branches WHERE device_id=? AND org=? AND conversation_id=? AND branch_id=?",
                                   (device.device_id, device.org, conversation_id, branch_id)).fetchone()
                if extra and all(r["id"] != extra["id"] for r in rows):
                    rows = [*rows, extra]
            if not rows and inventory_id is not None:
                extra = db.execute("SELECT b.* FROM recovery_branches b JOIN recovery_inventories i ON i.branch_id=b.id "
                                   "WHERE i.id=? AND b.device_id=? AND b.org=?", (inventory_id, device.device_id, device.org)).fetchone()
                if not extra:
                    _fail("recovery_revision_conflict", "Inventory допуска не найден")
                if ((conversation_id is not None and conversation_id != extra["conversation_id"])
                        or (branch_id is not None and branch_id != extra["branch_id"])):
                    _fail("recovery_revision_conflict", "Inventory принадлежит другой ветви")
                rows = [extra]
            for branch in rows:
                ready, reason = self._ready(db, branch["id"], admitting_api_attempt_id)
                if not ready:
                    _fail("recovery_required", "Сначала завершите сверку source checkpoints", reason=reason,
                          inventoryId=branch["inventory_id"], recoveryGeneration=branch["generation"], controlEpoch=branch["control_epoch"])
                if inventory_id is None or generation is None or control_epoch is None:
                    _fail("recovery_required", "Зарегистрированная recovery ветвь требует актуальный inventory/generation/control fence",
                          inventoryId=branch["inventory_id"], recoveryGeneration=branch["generation"], controlEpoch=branch["control_epoch"])
                if inventory_id != branch["inventory_id"] or generation != branch["generation"] or control_epoch != branch["control_epoch"]:
                    _fail("recovery_revision_conflict", "Допуск относится к прежней inventory/generation")

    async def _native_snapshot(self, case, binding, manifest, branch):
        """Router104 owns history proof and transport restoration, never Gate SDK code."""
        rid = _id(case["id"], binding["boot_id"], branch["generation"], branch["control_epoch"])
        turns = [o["turnId"] for o in manifest["operations"] if o.get("turnId")]
        body = {"version": 1, "id": rid, "sessionId": binding["local_id"],
                "expectedBootId": binding["boot_id"], "sourceManifestSha256": case["manifest_hash"],
                "generation": branch["generation"], "controlEpoch": branch["control_epoch"]}
        if turns:
            body["turnId"] = turns[-1]
        created = await self.state.nodes.request(binding["node_id"], "POST", "/v1/recovery/cases",
                    content=_json(body).encode(), expected_boot=binding["boot_id"], request_id="recovery:" + rid)
        if created.status_code not in (200, 201, 202):
            return None
        current = await self.state.nodes.request(binding["node_id"], "GET", "/v1/recovery/cases/" + rid,
                                                 expected_boot=binding["boot_id"])
        if current.status_code != 200:
            return None
        data = current.json()
        if isinstance(data, dict) and data.get("state") == "stale_lifecycle":
            fingerprint = data.get("lifecycleFingerprint")
            if not isinstance(fingerprint, str) or not fingerprint:
                return None
            rid = _id(rid, "lifecycle", fingerprint)
            body = dict(body, id=rid)
            created = await self.state.nodes.request(binding["node_id"], "POST", "/v1/recovery/cases",
                        content=_json(body).encode(), expected_boot=binding["boot_id"], request_id="recovery:" + rid)
            if created.status_code not in (200, 201, 202):
                return None
            current = await self.state.nodes.request(binding["node_id"], "GET", "/v1/recovery/cases/" + rid,
                                                     expected_boot=binding["boot_id"])
            if current.status_code != 200:
                return None
            data = current.json()
        if not isinstance(data, dict) or data.get("state") == "stale_lifecycle":
            return None
        if (data.get("version") != 1 or data.get("id") != rid or data.get("sessionId") != binding["local_id"]
                or data.get("nodeId") != binding["node_id"] or data.get("bootId") != binding["boot_id"]):
            _fail("recovery_source_conflict", "Router case не подтверждает исходную identity")
        native = (data.get("history") or {}).get("sdkSessionId")
        if binding.get("sdk_id") and native != binding["sdk_id"]:
            _fail("recovery_source_conflict", "Router case указывает другую SDK history")
        return data

    async def _reconcile_steps(self, branch_id, device, request):
        """A lost Gate HTTP response cannot strand a completed Router action."""
        rows = self.store._all("SELECT s.*,c.session_id FROM recovery_steps s JOIN recovery_cases c ON c.id=s.case_id "
                               "WHERE s.branch_id=? AND s.state IN ('started','unknown')", (branch_id,))
        for step in rows:
            try:
                binding = self.authorize(device, request, step["session_id"])
                descriptor = json.loads(step["descriptor"])
                if descriptor.get("kind") == "seal_prompt":
                    operation = descriptor["operation"]
                    fence = self.state.operations.fence(device, binding, operation["requestId"])
                    result = json.loads(fence["response"]) if fence else None
                else:
                    result = await self._native_step_outcome(binding, step)
                if result is None:
                    continue
                self.store._exec("UPDATE recovery_steps SET state='complete',result=? WHERE id=?", (_json(result), step["id"]))
                if result.get("outcome") == "accepted":
                    self.store._exec("UPDATE recovery_cases SET transport_required=0 WHERE id=?", (step["case_id"],))
            except (GateError, ValueError):
                continue

    def _settle_prepared(self, db, branch_id, reason):
        """Prepared is a local proof of no dispatch; started/unknown never is."""
        rows = db.execute("SELECT id,action_id FROM recovery_steps WHERE branch_id=? AND state='prepared'", (branch_id,)).fetchall()
        for row in rows:
            result = {"actionId": row["action_id"], "outcome": "rejected_not_sent", "reason": reason}
            db.execute("UPDATE recovery_steps SET state='complete',result=? WHERE id=? AND state='prepared'", (_json(result), row["id"]))

    async def _native_step_outcome(self, binding, step):
        descriptor = json.loads(step["descriptor"])
        if descriptor.get("kind") not in ("resume_transport", "release_idle_transport"):
            return None
        # A completed HTTP command may itself report an unknown native effect.
        # The native step ledger is authoritative even after command TTL expires.
        native = await self.state.nodes.request(binding["node_id"], "GET", "/v1/recovery/cases/" + descriptor["routerCaseId"])
        if native.status_code != 200:
            return None
        data = native.json()
        if (not isinstance(data, dict) or data.get("version") != 1 or data.get("id") != descriptor["routerCaseId"]
                or data.get("sessionId") != binding["local_id"] or data.get("nodeId") != binding["node_id"]):
            return None
        record = data.get("steps", {}).get(descriptor["routerActionId"])
        if record is not None:
            if (record.get("request") != descriptor["nativeRequest"] or record.get("state") != "complete"
                    or record.get("kind", descriptor["kind"]) != descriptor["kind"]):
                return None
            result = record.get("result")
            if not isinstance(result, dict) or result.get("actionsReplayed") is not False:
                return None
            if (result.get("outcome") == "rejected_not_sent" and result.get("scope") == "transport_only"
                    and result.get("code") in ("sdk_writer_busy", "capacity_exceeded") and result.get("retryable") is True):
                return {"actionId": step["action_id"], "status": 429 if result["code"] == "capacity_exceeded" else 409, "outcome": "rejected_not_sent",
                        "scope": "transport_only", "code": result["code"], "retryable": True}
            if result.get("sessionId") != binding["local_id"] or result.get("outcome") not in (None, "accepted"):
                return None
            return {"actionId": step["action_id"], "status": 200, "outcome": "accepted"}
        receipt = await self.state.nodes.request(binding["node_id"], "GET", "/v1/commands/recovery-step:" + step["id"])
        confirmed = completed_response(receipt.json(), "recovery-step:" + step["id"]) if receipt.status_code == 200 else None
        if confirmed is not None and confirmed.status_code >= 400:
            # Durable rejection plus the absence of a native step proves this
            # Router action stopped before native IO, not merely that HTTP ended.
            return {"actionId": step["action_id"], "status": confirmed.status_code, "outcome": "rejected"}
        return None

    def _mark_resolved(self, db, case, manifest):
        """A proof applied to a complete source also settles equivalent older cases.

        A later superset carrying another unknown operation remains unresolved.
        The cancellation facts and immutable receipts themselves are retained.
        """
        supplied = {(o["kind"], o["requestId"], o["bodySha256"]) for o in manifest["operations"]}
        siblings = db.execute("SELECT id,manifest FROM recovery_cases WHERE branch_id=? AND run_id=? AND session_id=?",
                              (case["branch_id"], case["run_id"], case["session_id"])).fetchall()
        for sibling in siblings:
            required = {(o["kind"], o["requestId"], o["bodySha256"]) for o in json.loads(sibling["manifest"])["operations"]}
            if required <= supplied:
                db.execute("UPDATE recovery_cases SET resolved=1 WHERE id=?", (sibling["id"],))

    def _applied_facts(self, rows, manifest, client_epoch):
        result = set()
        for row in rows:
            receipt = json.loads(row["receipt"])
            if (row["action_kind"] and receipt["clientStoreEpoch"] == client_epoch
                    and receipt["sourceCheckpointVersion"] == manifest.get("sourceCheckpointVersion", 0)):
                result.add((row["action_kind"], row["evidence_hash"]))
        return result

    def _proof(self, cid, kind, payload):
        evidence = {"version": 1, "issuer": "gate", "kind": kind, "storeEpoch": self.store_epoch, "payload": payload}
        digest = body_sha256(evidence)
        eid = _id(cid, digest)
        self.store._exec("INSERT OR IGNORE INTO recovery_evidence VALUES (?,?,?,?)", (eid, cid, digest, _json(evidence)))
        return {"evidenceId": eid, "evidenceSha256": digest}

    async def _terminal(self, binding, turn_id, after):
        path = "/v1/sessions/" + quote(binding["local_id"], safe="") + "/turns/" + quote(turn_id, safe="")
        receipt = await self.state.nodes.request(binding["node_id"], "GET", path + "/receipt", expected_boot=binding["boot_id"])
        if receipt.status_code != 200:
            return None
        saved = receipt.json()
        if not isinstance(saved, dict) or saved.get("id") != turn_id or saved.get("type") not in ("result", "error") or saved.get("willRetry") is True:
            return None
        # A receipt is written atomically with the exact turn row. This GET uses
        # that durable row; an absent receipt never probes a cold runtime.
        status = await self.state.nodes.request(binding["node_id"], "GET", path, expected_boot=binding["boot_id"])
        status_body = status.json() if status.status_code == 200 else None
        if not isinstance(status_body, dict) or status_body.get("done") is not True:
            return None
        end = status_body.get("seq")
        if type(end) is not int or type(after) is not int or not 0 <= after <= end:
            _fail("recovery_cursor_conflict", "Client cursor не соответствует exact terminal turn")
        events, cursor, unavailable = [], after, False
        while cursor < end:
            page = await self.state.nodes.request(binding["node_id"], "GET", path + "/events", params={"after": cursor, "wait": 0}, expected_boot=binding["boot_id"])
            if page.status_code == 410:
                unavailable = True
                break
            if page.status_code != 200:
                return None
            data = page.json()
            if not isinstance(data, dict) or data.get("turnId") != turn_id or type(data.get("nextAfter")) is not int or data["nextAfter"] <= cursor:
                _fail("recovery_protocol_error", "Router не подтвердил непрерывную страницу exact events", 503)
            for event in data.get("events", []):
                if event.get("seq") != cursor + 1:
                    _fail("recovery_event_gap", "В журнале Router обнаружен gap", 503)
                cursor += 1
                events.append(self._public_event(binding, event))
            if cursor != data["nextAfter"] or cursor > end:
                _fail("recovery_event_gap", "Cursor страницы не соответствует событиям", 503)
        terminal = self._public_event(binding, saved)
        return {"sessionId": binding["id"], "turnId": turn_id, "terminal": terminal,
                "seq": end, "events": events, "payloadUnavailable": unavailable}

    @staticmethod
    def _public_event(binding, event):
        # routing.translate also updates binding.sdk_id for session events. An
        # old journal read must never roll back the current resolved lineage.
        from .transport_recovery import decorate
        public = decorate(dict(event))
        if public.get("type") == "session" and public.get("sessionId"):
            public["sessionId"] = binding["sdk_alias"]
        return public

    async def observe(self, device, request, case_id):
        case = self._case(case_id, device, request)
        manifest = json.loads(case["manifest"])
        binding = self.authorize(device, request, case["session_id"])
        if "api_attempt" in binding:
            return await self._observe_api(device, request, case, manifest, binding)
        await self._reconcile_steps(case["branch_id"], device, request)
        outcomes, actions, proofs = [], [], []
        all_resolved = bool(manifest["operations"])
        allowed = manifest.get("permissions", {}).get("allowedRecovery", [])
        try:
            binding = await self.state.routing.recover_boot(binding)
        except GateError:
            pass  # Local complete receipts remain usable while Router is down.
        for operation in manifest["operations"]:
            kind, request_id = operation["kind"], operation["requestId"]
            oid = self.state.operations.key(device, request_id)
            op = self.store._one("SELECT * FROM router_operations WHERE id=?", (oid,))
            if op and (op["device_id"] != device.device_id or op["session_id"] != binding["id"]
                       or not op["path"].endswith("/" + kind) or (op["body_sha256"] and op["body_sha256"] != operation["bodySha256"])
                       or (kind == "prompt" and op["turn_id"] != operation["turnId"])):
                _fail("recovery_source_conflict", "Исходные key/body/turn не соответствуют command ledger")
            if op and op["state"] != "complete":
                try:
                    response = await self.state.nodes.request(binding["node_id"], "GET", "/v1/commands/" + quote(oid, safe=""))
                    confirmed = completed_response(response.json(), oid) if response.status_code == 200 else None
                    if confirmed is not None:
                        self.state.operations.complete(oid, confirmed)
                        op = self.store._one("SELECT * FROM router_operations WHERE id=?", (oid,))
                except (GateError, ValueError):
                    pass
            outcome = {"kind": kind, "requestId": request_id, "outcome": "outcome_unknown"}
            payload, action_kind = None, None
            if kind == "tool_effect":
                outcome["outcome"] = "effect_unknown"
            elif op and op["state"] == "complete":
                try:
                    response = json.loads(op["response"])
                except (TypeError, ValueError):
                    response = None
                if not op["body_sha256"]:
                    outcome["outcome"] = "evidence_unavailable"
                elif kind == "prompt":
                    outcome["outcome"] = "accepted_running" if op["status"] < 400 else "outcome_unknown"
                    try:
                        payload = await self._terminal(binding, operation["turnId"], manifest.get("observation", {}).get("lastAppliedSeq", 0))
                    except GateError as failed:
                        payload = None
                        outcome["recoveryError"] = {"code": failed.code, "status": failed.status}
                        if failed.code in ("recovery_event_gap", "recovery_cursor_conflict", "recovery_protocol_error"):
                            outcome["outcome"] = "outcome_unknown"
                    except ValueError:
                        payload = None
                        outcome["recoveryError"] = {"code": "recovery_protocol_error", "status": 503}
                        outcome["outcome"] = "outcome_unknown"
                    if payload:
                        terminal = payload["terminal"]
                        unknown = terminal.get("outcomeUnknown") is True or terminal.get("kind") == "command_outcome_unknown"
                        outcome["outcome"] = "effect_unknown" if unknown else "terminal_success" if terminal.get("ok") is True else "terminal_error"
                        action_kind = None if unknown else "resolve_source_checkpoint"
                        self.store._exec("UPDATE router_operations SET finished=1 WHERE id=?", (oid,))
                    elif self._not_sent(response):
                        outcome["outcome"] = "rejected"
                        payload, action_kind = {"operation": operation, "status": op["status"], "response": response}, "resolve_source_checkpoint"
                elif op["status"] >= 400 and (kind == "tool_result" or self._not_sent(response)):
                    outcome["outcome"] = "rejected"
                    payload, action_kind = {"operation": operation, "status": op["status"], "response": response}, "resolve_source_checkpoint"
                elif op["status"] >= 400 or not isinstance(response, dict):
                    outcome["outcome"] = "outcome_unknown"
                elif kind == "tool_result":
                    if response.get("callId") != operation["callId"] or response.get("status") != "accepted":
                        outcome["outcome"] = "outcome_unknown"
                    else:
                        outcome["outcome"] = "accepted"
                        payload, action_kind = {"operation": operation, "status": op["status"], "response": response}, "apply_saved_ack"
                elif kind != "prompt":
                    outcome["outcome"] = "accepted"
                    payload, action_kind = {"operation": operation, "status": op["status"], "response": response}, "resolve_source_checkpoint"
            elif not op and kind == "prompt":
                sealed = self.state.operations.fence(device, binding, request_id)
                if sealed:
                    payload = json.loads(sealed["response"])
                    if payload["bodySha256"] != operation["bodySha256"] or payload["turnId"] != operation["turnId"]:
                        _fail("recovery_source_conflict", "Admission fence принадлежит другому intent")
                    outcome["outcome"], action_kind = "sealed_not_sent", "resolve_source_checkpoint"
                elif "resolve_source_checkpoint" in allowed or "seal_prompt" in allowed:
                    actions.append({"id": _id(case_id, "seal", operation), "executor": "gate", "kind": "seal_prompt", "operation": operation})
            if payload is not None:
                proof = self._proof(case_id, kind, payload)
                proofs.append({**proof, **self.store._one("SELECT body FROM recovery_evidence WHERE id=?", (proof["evidenceId"],))})
                outcome.update(proof)
                if action_kind and action_kind in allowed:
                    actions.append({"id": _id(case_id, action_kind, request_id, proof["evidenceSha256"]), "executor": "harness", "kind": action_kind,
                                    **proof, "sourceRunId": case["run_id"], "sourceSessionId": case["session_id"],
                                    "sourceCheckpointVersion": manifest.get("sourceCheckpointVersion", 0)})
                else:
                    all_resolved = False
            else:
                all_resolved = False
            outcomes.append(outcome)
        return await self._publish(device, request, case, manifest, binding, outcomes, actions, proofs, all_resolved)

    @staticmethod
    def _not_sent(response):
        if not isinstance(response, dict):
            return False
        values = [response, response.get("error"), response.get("recovery")]
        detail = response.get("detail")
        if isinstance(detail, dict):
            values.extend([detail, detail.get("error")])
        return any(isinstance(value, dict) and (value.get("deliveryState") == "not_sent" or value.get("outcome") == "not_sent") for value in values)

    async def _observe_api(self, device, request, case, manifest, binding):
        manager = self.state.api_attempts
        row = binding["api_attempt"]
        receipt = manager.snapshot(row["id"], device)
        effective_state = receipt["state"]
        allowed = manifest.get("permissions", {}).get("allowedRecovery", [])
        outcomes, actions, proofs = [], [], []
        complete = bool(row.get("body_sha256")) and effective_state in ("complete", "rejected_not_sent") and receipt.get("terminalKnown") is True
        for operation in manifest["operations"]:
            outcome = {"kind": "api_attempt", "requestId": operation["requestId"], "outcome": "outcome_unknown", "attemptId": row["id"], "apiReceipt": receipt}
            if effective_state in ("running", "prepared") and receipt.get("producerAlive") is not False:
                outcome["outcome"] = "accepted_running"
            if not row.get("body_sha256"):
                outcome["outcome"] = "evidence_unavailable"
            elif complete:
                outcome["outcome"] = "sealed_not_sent" if effective_state == "rejected_not_sent" else "api_terminal"
                stable_receipt = {key: value for key, value in receipt.items() if key not in ("producerAlive", "durableState")}
                payload = {"attempt": stable_receipt, "operation": operation, "resultPath": receipt["resultPath"],
                           "resultSha256": receipt.get("resultSha256"), "hostExecutionAllowed": False, "taskVerified": False}
                proof = self._proof(case["id"], "api_attempt", payload)
                proofs.append({**proof, **self.store._one("SELECT body FROM recovery_evidence WHERE id=?", (proof["evidenceId"],))})
                outcome.update(proof)
                kinds = ["resolve_source_checkpoint"] if effective_state == "rejected_not_sent" else ["apply_api_receipt", "resolve_source_checkpoint"]
                for kind in kinds:
                    if kind not in allowed:
                        complete = False
                        continue
                    actions.append({"id": _id(case["id"], kind, proof["evidenceSha256"]), "executor": "harness", "kind": kind,
                                    **proof, "sourceRunId": case["run_id"], "sourceAttemptId": row["id"],
                                    "sourceCheckpointVersion": manifest.get("sourceCheckpointVersion", 0)})
            outcomes.append(outcome)
        return await self._publish(device, request, case, manifest, binding, outcomes, actions, proofs, complete)

    async def _publish(self, device, request, case, manifest, binding, outcomes, actions, proofs, all_resolved):
        case_id = case["id"]
        allowed = manifest.get("permissions", {}).get("allowedRecovery", [])
        applied = {r["action_id"] for r in self.store._all("SELECT action_id FROM recovery_applied WHERE case_id=?", (case_id,))}
        prior_applied = self.store._all("SELECT a.evidence_hash,a.receipt,a.action_kind FROM recovery_applied a JOIN recovery_cases c ON c.id=a.case_id "
                                        "WHERE c.branch_id=? AND c.run_id=? AND c.session_id=?", (case["branch_id"], case["run_id"], case["session_id"]))
        client_epoch = self.store._one("SELECT client_epoch FROM recovery_inventories WHERE id=?", (case["inventory_id"],))["client_epoch"]
        inherited = self._applied_facts(prior_applied, manifest, client_epoch)
        client_actions = [a for a in actions if a["executor"] == "harness"]
        resolved = all_resolved and bool(client_actions) and all(a["id"] in applied or (a["kind"], a["evidenceSha256"]) in inherited for a in client_actions)
        branch = self.store._one("SELECT * FROM recovery_branches WHERE id=?", (case["branch_id"],))
        native = None
        if "api_attempt" not in binding and not case["cancelled"] and branch["inventory_id"] == case["inventory_id"]:
            try:
                native = await self._native_snapshot(case, binding, manifest, branch)
            except (GateError, ValueError):
                pass
        native_policy_error = None
        if native:
            try:
                self.authorize(device, request, case["session_id"], native=True)
            except GateError as failed:
                native_policy_error = failed.code
        with self.transaction() as db:
            current = db.execute("SELECT * FROM recovery_cases WHERE id=?", (case_id,)).fetchone()
            resolved = bool(current["resolved"]) or resolved
            db.execute("UPDATE recovery_cases SET resolved=? WHERE id=?", (int(resolved), case_id))
            if not current["resolved"]:
                db.execute("UPDATE recovery_cases SET required_actions=?,proof_ready=? WHERE id=?", (_json(client_actions), int(all_resolved), case_id))
            if resolved:
                self._mark_resolved(db, case, manifest)
            branch = db.execute("SELECT * FROM recovery_branches WHERE id=?", (case["branch_id"],)).fetchone()
            inventory = db.execute("SELECT * FROM recovery_inventories WHERE id=?", (case["inventory_id"],)).fetchone()
            source_ready, _ = self._ready(db, case["branch_id"], include_transport=False)
            current_inventory = branch["inventory_id"] == case["inventory_id"]
            goal = manifest.get("transportGoal", "ready")
            if source_ready and current_inventory and native and not native_policy_error and binding["status"] != "closed" and not self.state.nodes.snapshots.get(binding.get("node_id"), {}).get("draining"):
                for action in native.get("actions", []):
                    if action.get("kind") not in ("resume_transport", "release_idle_transport") or not isinstance(action.get("id"), str):
                        continue
                    if action["kind"] not in allowed or (action["kind"] == "release_idle_transport" and goal != "released") or (action["kind"] == "resume_transport" and goal != "ready"):
                        continue
                    if action["kind"] == "resume_transport" and not native.get("capabilities", {}).get("resumeTransport"):
                        continue
                    base_id = _id(case_id, "native", native["id"], action["id"], native["revision"])
                    action_id, previous = base_id, None
                    while True:
                        prior = db.execute("SELECT id,result,state FROM recovery_steps WHERE case_id=? AND action_id=?", (case_id, action_id)).fetchone()
                        if not prior or prior["state"] != "complete" or json.loads(prior["result"]).get("outcome") not in ("rejected", "rejected_not_sent"):
                            break
                        previous = prior["id"]
                        action_id = _id(base_id, "after_rejected", previous)
                    descriptor = {"id": action_id, "executor": "gate", "kind": action["kind"],
                                  "routerCaseId": native["id"], "routerActionId": action["id"], "routerRevision": native["revision"]}
                    if previous:
                        descriptor["previousRejectedStepId"] = previous
                    actions.append(descriptor)
            native_history = (native.get("history") or {}).get("state") if native else None
            replacement_required = bool(native_policy_error) or binding["status"] == "closed" or native_history in ("reset", "unavailable") or bool(native and native.get("transport", {}).get("replacementRequired"))
            transport_required = bool(current["transport_required"])
            if binding["status"] == "closed" or goal == "none" or native_policy_error:
                transport_required = False
            if native and isinstance(native.get("transport"), dict):
                if (goal == "ready" and native["transport"].get("ready") is True) or (goal == "released" and native["transport"].get("ready") is False):
                    transport_required = False
            wanted = "resume_transport" if goal == "ready" else "release_idle_transport" if goal == "released" else None
            if (current_inventory and native and not native_policy_error and binding["status"] != "closed" and wanted in allowed
                    and any(action.get("kind") == wanted for action in native.get("actions", []))):
                # Install the admission barrier before client ACK. Otherwise a
                # prompt racing the first post-ACK GET could bypass history-only
                # transport restoration even though the plan requires it.
                transport_required = True
            if any(a["executor"] == "gate" and a["kind"] in ("resume_transport", "release_idle_transport") for a in actions):
                transport_required = True
            db.execute("UPDATE recovery_cases SET transport_required=? WHERE id=?", (int(transport_required), case_id))
            ready, reason = self._ready(db, case["branch_id"])
            uncertain = any(o["outcome"] in ("outcome_unknown", "effect_unknown", "evidence_unavailable") for o in outcomes)
            own_pending = db.execute("SELECT 1 FROM recovery_steps WHERE case_id=? AND state IN ('prepared','started','unknown') LIMIT 1", (case_id,)).fetchone()
            state = "cancelled" if current["cancelled"] else "waiting" if own_pending else "plan_ready" if transport_required else "completed" if resolved else "plan_ready" if actions else "needs_input" if uncertain else "waiting"
            snapshot = {"version": 1, "recoveryId": case_id, "sourceManifestSha256": case["manifest_hash"], "state": state,
                        "sourceKind": "api_attempt" if "api_attempt" in binding else "managed_session",
                        "branchFence": {"recoveryGeneration": branch["generation"], "controlEpoch": branch["control_epoch"]},
                        "inventory": {"id": inventory["id"], "version": inventory["version"], "sealed": bool(inventory["sealed"]), "sha256": inventory["seal_hash"]},
                        "operations": outcomes, "actions": [] if current["cancelled"] else [a for a in actions if current_inventory or a["executor"] == "harness"],
                        "resume": {"businessReplayAllowed": False, "history": (native.get("history") or {}).get("state", "unconfirmed") if native else "unconfirmed",
                                   "supported": bool(not replacement_required and native and native.get("capabilities", {}).get("resumeTransport")),
                                   "replacementRequired": replacement_required, "policyError": native_policy_error},
                        "next": {"state": "ready" if ready and current_inventory else "needs_input" if uncertain else reason or "historical_source",
                                 "reason": reason, "businessDispatchAllowed": ready and current_inventory,
                                 "independentBranchAllowed": uncertain, "effectReplayAllowed": False},
                        "evidence": [{**p, "canonicalPayloadUTF8": p["body"], "body": json.loads(p["body"])} for p in proofs],
                        "claims": [json.loads(r["body"]) for r in db.execute("SELECT body FROM recovery_evidence WHERE case_id=? ORDER BY id", (case_id,)).fetchall()
                                   if json.loads(r["body"]).get("issuer") == "client"],
                        "knownServerOperations": self._known_pending(db, case["branch_id"]),
                        "serverSteps": [{"actionId": r["action_id"], "state": r["state"], "receipt": json.loads(r["result"]) if r["result"] else None}
                                        for r in db.execute("SELECT * FROM recovery_steps WHERE case_id=? ORDER BY id", (case_id,)).fetchall()]}
            previous = json.loads(current["snapshot"]) if current["snapshot"] else None
            comparison = dict(previous or {})
            comparison.pop("revision", None)
            comparison.pop("planId", None)
            revision = current["revision"] + int(previous is not None and comparison != snapshot)
            plan_id = _id(case_id, revision, snapshot)
            snapshot.update(revision=revision, planId=plan_id)
            db.execute("INSERT OR IGNORE INTO recovery_plans VALUES (?,?,?,?)", (plan_id, case_id, revision, _json(snapshot)))
            db.execute("UPDATE recovery_cases SET revision=?,state=?,plan_id=?,snapshot=? WHERE id=?", (revision, state, plan_id, _json(snapshot), case_id))
        return snapshot

    def evidence(self, device, request, case_id, body, key):
        self._case(case_id, device, request)
        with self.transaction() as db:
            old = self._request(db, device, key, "evidence:" + case_id, body)
            if old:
                return old
            # Device claims never promote themselves to external technical proofs.
            digest = body_sha256(body)
            eid = _id(case_id, "claim", digest)
            db.execute("INSERT OR IGNORE INTO recovery_evidence VALUES (?,?,?,?)", (eid, case_id, digest, _json({"issuer": "client", "claim": body})))
            db.execute("UPDATE recovery_cases SET revision=revision+1 WHERE id=?", (case_id,))
            result = {"version": 1, "evidenceId": eid, "trustedOutcome": False}
            self._request(db, device, key, "evidence:" + case_id, body, result)
            return result

    def ack(self, device, request, case_id, body):
        case = self._case(case_id, device, request)
        with self.transaction() as db:
            old = self._request(db, device, body["ackKey"], "ack:" + case_id, body)
            if old:
                return old
            plan = db.execute("SELECT * FROM recovery_plans WHERE id=? AND case_id=?", (body["planId"], case_id)).fetchone()
            inventory = db.execute("SELECT * FROM recovery_inventories WHERE id=?", (case["inventory_id"],)).fetchone()
            manifest = json.loads(case["manifest"])
            if (not plan or plan["revision"] != body["planRevision"] or body["sourceManifestSha256"] != case["manifest_hash"]
                    or body["sourceRunId"] != case["run_id"] or (body.get("sourceSessionId") or "api:" + (body.get("sourceAttemptId") or "")) != case["session_id"]
                    or body["clientStoreEpoch"] != inventory["client_epoch"]
                    or body["sourceCheckpointVersion"] != manifest.get("sourceCheckpointVersion", 0)):
                _fail("recovery_ack_conflict", "ACK не соответствует immutable source/plan/client store")
            saved = json.loads(plan["body"])
            descriptors = {a["id"]: a for a in saved["actions"] if a["executor"] == "harness"}
            if len({a["actionId"] for a in body["appliedActions"]}) != len(body["appliedActions"]):
                _fail("recovery_ack_conflict", "Повтор action ID в ACK")
            for action in body["appliedActions"]:
                descriptor = descriptors.get(action["actionId"])
                if descriptor is None or descriptor.get("evidenceSha256") != action["evidenceSha256"]:
                    _fail("recovery_ack_conflict", "ACK подменяет action/evidence descriptor")
                prior = db.execute("SELECT evidence_hash FROM recovery_applied WHERE case_id=? AND action_id=?", (case_id, action["actionId"])).fetchone()
                if prior and prior["evidence_hash"] != action["evidenceSha256"]:
                    _fail("recovery_ack_conflict", "Применённый evidence hash неизменяем")
            branch = db.execute("SELECT * FROM recovery_branches WHERE id=?", (case["branch_id"],)).fetchone()
            historical = (case["cancelled"] or saved["branchFence"] != {"recoveryGeneration": branch["generation"], "controlEpoch": branch["control_epoch"]})
            result = {"version": 1, "recoveryId": case_id, "ackKey": body["ackKey"], "state": "historical-applied" if historical else "client-applied",
                      "planId": plan["id"], "businessDispatchAllowed": False}
            for action in body["appliedActions"]:
                db.execute("INSERT OR IGNORE INTO recovery_applied(case_id,action_id,evidence_hash,receipt,action_kind) VALUES (?,?,?,?,?)",
                           (case_id, action["actionId"], action["evidenceSha256"], _json(body), descriptors[action["actionId"]]["kind"]))
            # Recompute source completion in this transaction, even for a first
            # ACK of an older plan. New/unknown source facts cannot be discharged
            # merely by applying an old action set.
            current = db.execute("SELECT * FROM recovery_cases WHERE id=?", (case_id,)).fetchone()
            required = json.loads(current["required_actions"])
            facts = self._applied_facts(db.execute("SELECT a.evidence_hash,a.receipt,a.action_kind FROM recovery_applied a JOIN recovery_cases c ON c.id=a.case_id "
                                                  "WHERE c.branch_id=? AND c.run_id=? AND c.session_id=?",
                                                  (case["branch_id"], case["run_id"], case["session_id"])).fetchall(), manifest, inventory["client_epoch"])
            complete = bool(current["proof_ready"] and required) and all((action["kind"], action["evidenceSha256"]) in facts for action in required)
            complete = bool(current["resolved"]) or complete
            db.execute("UPDATE recovery_cases SET resolved=?,revision=revision+1 WHERE id=?", (int(complete), case_id))
            if complete:
                self._mark_resolved(db, case, manifest)
            self._request(db, device, body["ackKey"], "ack:" + case_id, body, result)
            return result

    def _validate_cas(self, db, case, body):
        current = db.execute("SELECT * FROM recovery_cases WHERE id=?", (case["id"],)).fetchone()
        branch = db.execute("SELECT * FROM recovery_branches WHERE id=?", (case["branch_id"],)).fetchone()
        if (body["sourceManifestSha256"] != current["manifest_hash"] or body["expectedRevision"] != current["revision"]
                or current["inventory_id"] != branch["inventory_id"]
                or body["branchFence"] != {"recoveryGeneration": branch["generation"], "controlEpoch": branch["control_epoch"]}):
            _fail("recovery_revision_conflict", "План или branch fence уже изменились")
        return current, branch

    def cancel(self, device, request, case_id, body, key):
        case = self._case(case_id, device, request)
        with self.transaction() as db:
            old = self._request(db, device, key, "cancel:" + case_id, body)
            if old:
                return old
            _, branch = self._validate_cas(db, case, body)
            self._settle_prepared(db, branch["id"], "control_fenced")
            db.execute("UPDATE recovery_cases SET cancelled=1,state='cancelled',revision=revision+1 WHERE id=?", (case_id,))
            db.execute("UPDATE recovery_branches SET control_epoch=control_epoch+1 WHERE id=?", (branch["id"],))
            result = {"version": 1, "recoveryId": case_id, "state": "cancelled", "controlEpoch": branch["control_epoch"] + 1}
            self._request(db, device, key, "cancel:" + case_id, body, result)
            return result

    async def apply(self, device, request, case_id, body, key):
        case = self._case(case_id, device, request)
        await self._reconcile_steps(case["branch_id"], device, request)
        with self.transaction() as db:
            old = self._request(db, device, key, "apply:" + case_id, body)
            if old and not old.get("pending"):
                return old
            if old:
                completed = [db.execute("SELECT * FROM recovery_steps WHERE id=?", (_id(case_id, aid),)).fetchone() for aid in body["actionIds"]]
                if all(s and s["state"] == "complete" for s in completed):
                    result = {"version": 1, "recoveryId": case_id, "steps": [json.loads(s["result"]) for s in completed], "businessDispatchAllowed": False}
                    db.execute("UPDATE recovery_requests SET response=? WHERE device_id=? AND org=? AND key=?",
                               (_json(result), device.device_id, device.org, key))
                    return result
                current = db.execute("SELECT * FROM recovery_cases WHERE id=?", (case_id,)).fetchone()
                branch = db.execute("SELECT * FROM recovery_branches WHERE id=?", (case["branch_id"],)).fetchone()
                if (current["cancelled"] or current["inventory_id"] != branch["inventory_id"]
                        or body["branchFence"] != {"recoveryGeneration": branch["generation"], "controlEpoch": branch["control_epoch"]}):
                    return {"version": 1, "recoveryId": case_id, "state": "waiting", "reason": "superseded_action_reconciliation", "businessDispatchAllowed": False}
            else:
                current, branch = self._validate_cas(db, case, body)
            plan = db.execute("SELECT * FROM recovery_plans WHERE id=? AND case_id=?", (body["planId"], case_id)).fetchone()
            if current["cancelled"] or not plan or (not old and plan["id"] != current["plan_id"]):
                _fail("recovery_revision_conflict", "План отменён или заменён")
            descriptors = {a["id"]: a for a in json.loads(plan["body"])["actions"]}
            actions = []
            for aid in body["actionIds"]:
                action = descriptors.get(aid)
                if action is None or action["executor"] != "gate":
                    _fail("recovery_action_forbidden", "Apply принимает только сохранённые server action IDs", 422)
                actions.append(action)
            if len({a["id"] for a in actions}) != len(actions):
                _fail("recovery_action_forbidden", "Повтор server action ID", 422)
            step_ids = {_id(case_id, action["id"]) for action in actions}
            if any(row["id"] not in step_ids for row in db.execute("SELECT id FROM recovery_steps WHERE branch_id=? AND state IN ('prepared','started','unknown')",
                                                                  (branch["id"],)).fetchall()):
                _fail("recovery_action_busy", "Другой case уже владеет native шагом этой ветви")
            for action in actions:
                sid = _id(case_id, action["id"])
                descriptor = dict(action, branchFence=body["branchFence"])
                if action["kind"] in ("resume_transport", "release_idle_transport"):
                    descriptor["nativeRequest"] = {"version": 1, "actionId": action["routerActionId"], "expectedRevision": action["routerRevision"],
                                                    "generation": body["branchFence"]["recoveryGeneration"], "controlEpoch": body["branchFence"]["controlEpoch"]}
                db.execute("INSERT OR IGNORE INTO recovery_steps(id,branch_id,case_id,action_id,state,result,descriptor) "
                           "VALUES (?,?,?,?, 'prepared',NULL,?)", (sid, branch["id"], case_id, action["id"], _json(descriptor)))
            if not old:
                self._request(db, device, key, "apply:" + case_id, body,
                              {"version": 1, "recoveryId": case_id, "state": "applying", "pending": True, "businessDispatchAllowed": False})
        results = []
        for action in actions:
            sid = _id(case_id, action["id"])
            step = self.store._one("SELECT * FROM recovery_steps WHERE id=?", (sid,))
            if step["state"] == "complete":
                results.append(json.loads(step["result"]))
                continue
            try:
                binding = self.authorize(device, request, case["session_id"], native=action["kind"] in ("resume_transport", "release_idle_transport"))
            except GateError as failed:
                if step["state"] == "prepared":
                    result = {"actionId": action["id"], "outcome": "rejected_not_sent",
                              "scope": "transport_only" if action["kind"] in ("resume_transport", "release_idle_transport") else "gate_recovery_action",
                              "code": failed.code, "status": failed.status}
                    self.store._exec("UPDATE recovery_steps SET state='complete',result=? WHERE id=? AND state='prepared'", (_json(result), sid))
                raise
            with self.transaction() as db:
                branch = db.execute("SELECT * FROM recovery_branches WHERE id=?", (case["branch_id"],)).fetchone()
                current = db.execute("SELECT * FROM recovery_cases WHERE id=?", (case_id,)).fetchone()
                if current["cancelled"] or current["inventory_id"] != branch["inventory_id"] or body["branchFence"] != {"recoveryGeneration": branch["generation"], "controlEpoch": branch["control_epoch"]}:
                    if step["state"] == "prepared":
                        result = {"actionId": action["id"], "outcome": "rejected_not_sent", "reason": "control_fenced"}
                        db.execute("UPDATE recovery_steps SET state='complete',result=? WHERE id=?", (_json(result), sid))
                        results.append(result)
                        continue
                    return {"version": 1, "recoveryId": case_id, "state": "waiting", "reason": "superseded_action_reconciliation", "businessDispatchAllowed": False}
                db.execute("UPDATE recovery_steps SET state='started' WHERE id=? AND state='prepared'", (sid,))
            if action["kind"] == "seal_prompt":
                operation = action["operation"]
                result = self.state.operations.resolve_prompt(device, binding, operation["requestId"], operation["turnId"], operation["bodySha256"])
            elif action["kind"] in ("resume_transport", "release_idle_transport"):
                native_body = {"version": 1, "actionId": action["routerActionId"], "expectedRevision": action["routerRevision"],
                               "generation": body["branchFence"]["recoveryGeneration"], "controlEpoch": body["branchFence"]["controlEpoch"]}
                try:
                    response = await self.state.nodes.request(binding["node_id"], "POST",
                                    "/v1/recovery/cases/" + action["routerCaseId"] + "/apply",
                                    content=_json(native_body).encode(), expected_boot=binding["boot_id"], request_id="recovery-step:" + sid)
                except GateError:
                    self.store._exec("UPDATE recovery_steps SET state='unknown' WHERE id=? AND state!='complete'", (sid,))
                    raise
                # Node transport confirms 5xx against the Router command receipt.
                # A missing response stays unknown and is never sent with a new key.
                result = await self._native_step_outcome(binding, self.store._one("SELECT * FROM recovery_steps WHERE id=?", (sid,)))
                if result is None:
                    self.store._exec("UPDATE recovery_steps SET state='unknown' WHERE id=? AND state!='complete'", (sid,))
                    _fail("recovery_action_outcome_unknown", "Router ещё не подтвердил native transport outcome", 503)
            else:
                _fail("recovery_action_unsupported", "Native действие не поддержано exact adapter", 422)
            self.store._exec("UPDATE recovery_steps SET state='complete',result=? WHERE id=?", (_json(result), sid))
            if result.get("outcome") == "accepted" and action["kind"] in ("resume_transport", "release_idle_transport"):
                self.store._exec("UPDATE recovery_cases SET transport_required=0 WHERE id=?", (case_id,))
            results.append(result)
        result = {"version": 1, "recoveryId": case_id, "steps": results, "businessDispatchAllowed": False}
        with self.transaction() as db:
            db.execute("UPDATE recovery_requests SET response=? WHERE device_id=? AND org=? AND key=?",
                       (_json(result), device.device_id, device.org, key))
        return result


router = APIRouter(route_class=GateRoute, tags=["recovery"])


def _response(value):
    return JSONResponse(value, headers={"Cache-Control": "private, no-store"})


@router.post("/harness/recovery/inventories")
async def inventories(body: InventoryCreate, request: Request, device=Depends(require_device)):
    return _response(gate(request).recovery.create_inventory(device, request, body.model_dump(exclude_unset=True), _key(request)))


@router.post("/harness/recovery/inventories/{inventory_id}/seal")
async def seal_inventory(inventory_id: str, body: Versioned, request: Request, device=Depends(require_device)):
    return _response(gate(request).recovery.seal(device, request, inventory_id, body.model_dump(), _key(request)))


@router.post("/harness/recovery/cases")
async def cases(body: CaseCreate, request: Request, device=Depends(require_device)):
    recovery = gate(request).recovery
    created = recovery.create_case(device, request, body.model_dump(exclude_unset=True), _key(request))
    return _response(await recovery.observe(device, request, created["recoveryId"]))


@router.get("/harness/recovery/cases/{case_id}")
async def snapshot(case_id: str, request: Request, device=Depends(require_device)):
    return _response(await gate(request).recovery.observe(device, request, case_id))


@router.get('/harness/agent/sessions/{sid}/history-identity')
def history_identity(sid: str, request: Request, previousAlias: str | None = None,
                     device=Depends(require_device)):
    from .history_identity import identity
    if previousAlias is not None and (not previousAlias or len(previousAlias) > 200):
        raise GateError(422, 'history_identity_invalid', 'Нужен непустой opaque alias')
    return JSONResponse(identity(gate(request).recovery, device, request, sid, previousAlias),
                        headers={'Cache-Control': 'private, no-store', 'Vary': 'Authorization'})


@router.post("/harness/recovery/cases/{case_id}/evidence")
async def append_evidence(case_id: str, body: Evidence, request: Request, device=Depends(require_device)):
    return _response(gate(request).recovery.evidence(device, request, case_id, body.model_dump(), _key(request)))


@router.post("/harness/recovery/cases/{case_id}/apply")
async def apply(case_id: str, body: Apply, request: Request, device=Depends(require_device)):
    return _response(await gate(request).recovery.apply(device, request, case_id, body.model_dump(), _key(request)))


@router.post("/harness/recovery/cases/{case_id}/ack")
async def ack(case_id: str, body: Ack, request: Request, device=Depends(require_device)):
    return _response(gate(request).recovery.ack(device, request, case_id, body.model_dump()))


@router.post("/harness/recovery/cases/{case_id}/cancel")
async def cancel(case_id: str, body: Cancel, request: Request, device=Depends(require_device)):
    return _response(gate(request).recovery.cancel(device, request, case_id, body.model_dump(), _key(request)))
