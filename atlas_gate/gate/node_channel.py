"""Исходящий HTTP long-poll Router: ограниченная дисковая очередь, lease и безопасный replay."""
from __future__ import annotations
import asyncio
import base64
import json
import time
import uuid
import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from ..auth import require_token
from .errors import GateError, GateRoute

BODY_LIMIT = 64 * 1024 * 1024
RESULT_LIMIT = 96 * 1024 * 1024


class Channel:
    def __init__(self, store):
        self.store = store
        store._exec("""CREATE TABLE IF NOT EXISTS node_work(id TEXT PRIMARY KEY, node_id TEXT NOT NULL,
            method TEXT NOT NULL, path TEXT NOT NULL, request TEXT NOT NULL,
            state TEXT NOT NULL, deadline REAL NOT NULL, lease REAL NOT NULL DEFAULT 0,
            status INTEGER, response BLOB, created REAL NOT NULL)""")
        store._exec("CREATE INDEX IF NOT EXISTS node_work_pending ON node_work(node_id,state,created)")

    async def request(self, node_id, method, path, *, content=b"", params=None, headers=None):
        if len(content) > BODY_LIMIT:
            raise GateError(413, "payload_too_large", "команда превышает предел канала")
        if method != "GET" and not (headers or {}).get("Idempotency-Key"):
            raise GateError(422, "invalid_request", "изменяющая команда требует request_id")
        count = self.store._one("SELECT COUNT(*) n FROM node_work WHERE node_id=? AND state IN ('queued','leased') AND deadline>?", (node_id, time.time()))["n"]
        if count >= 64:
            raise GateError(429, "capacity_exceeded", "очередь канала заполнена")
        used = self.store._one("SELECT COALESCE(SUM(length(request)),0) n FROM node_work WHERE node_id=? AND state IN ('queued','leased') AND deadline>?", (node_id, time.time()))["n"]
        if used + len(content) * 4 // 3 + 2048 > 256 * 1024 * 1024:
            raise GateError(429, "capacity_exceeded", "предел объёма очереди канала")
        rid = str(uuid.uuid4())
        payload = json.dumps({"headers": headers or {}, "params": list(params.multi_items()) if hasattr(params, "multi_items") else params, "body": base64.b64encode(content).decode()})
        now = time.time()
        self.store._exec("INSERT INTO node_work(id,node_id,method,path,request,state,deadline,created) VALUES (?,?,?,?,?,'queued',?,?)", (rid, node_id, method, path, payload, now + 70, now))
        while time.time() < now + 70:
            row = self.store._one("SELECT state,status,response FROM node_work WHERE id=?", (rid,))
            if row["state"] == "complete":
                return httpx.Response(row["status"], content=row["response"], headers={"content-type": "application/json"}, request=httpx.Request(method, "http://router" + path))
            await asyncio.sleep(0.02)
        raise GateError(503, "command_outcome_unknown" if method != "GET" else "node_unavailable", "канал Router не подтвердил ответ")

    async def poll(self, node_id, wait=25, disconnected=None, authorized=None):
        end = time.monotonic() + wait
        while True:
            if authorized and not authorized():
                raise GateError(401, "unauthorized", "удостоверение Router отозвано")
            now = time.time()
            with self.store._lock:
                db = self.store._db
                db.execute("BEGIN IMMEDIATE")
                try:
                    db.execute("UPDATE node_work SET state='expired' WHERE deadline<? AND state IN ('queued','leased')", (now,))
                    db.execute("DELETE FROM node_work WHERE created<? AND state IN ('complete','expired')", (now - 86400,))
                    row = db.execute("SELECT * FROM node_work WHERE node_id=? AND deadline>? AND (state='queued' OR (state='leased' AND lease<?)) ORDER BY created LIMIT 1", (node_id, now, now)).fetchone()
                    if row:
                        db.execute("UPDATE node_work SET state='leased',lease=? WHERE id=?", (now + 40, row["id"]))
                    db.execute("COMMIT")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
            if row:
                return dict(json.loads(row["request"]), id=row["id"], method=row["method"], path=row["path"], deadline=row["deadline"])
            if time.monotonic() >= end:
                return None
            if disconnected and await disconnected():
                return None
            await asyncio.sleep(0.05)

    def complete(self, node_id, rid, status, body):
        row = self.store._one("SELECT * FROM node_work WHERE id=? AND node_id=?", (rid, node_id))
        if not row:
            raise GateError(404, "not_found", "команда принадлежит другому узлу или отсутствует")
        if row["state"] == "complete":
            if row["status"] != status or row["response"] != body:
                raise GateError(409, "idempotency_conflict", "ответ уже зафиксирован")
            return
        if row["state"] != "leased" or row["deadline"] < time.time():
            raise GateError(410, "expired", "срок команды истёк; сверить журнал Router")
        self.store._exec("UPDATE node_work SET state='complete',status=?,response=? WHERE id=?", (status, body, rid))


class ChannelClient:
    def __init__(self, channel, node_id):
        self.channel, self.node_id = channel, node_id

    async def request(self, method, path, **kwargs):
        return await self.channel.request(self.node_id, method, path, **kwargs)

    async def get(self, path):
        return await self.request("GET", path)

    async def aclose(self):
        pass


router = APIRouter(route_class=GateRoute)
admin = [Depends(require_token)]


def state(request):
    return request.app.state.gate


class Enroll(BaseModel):
    invitation: str = Field(min_length=20, max_length=100)
    csr: str = Field(max_length=16384)


class Invitation(BaseModel):
    invitation: str = Field(min_length=20, max_length=100)


@router.post("/harness/admin/nodes/{node_id}/invite", dependencies=admin)
async def invite(node_id: str, request: Request):
    return state(request).identity.invite(node_id)


@router.get("/harness/admin/node-enrollments", dependencies=admin)
async def enrollments(request: Request):
    rows = state(request).store._all("SELECT token_hash id,node_id,state,expires,csr FROM node_invites WHERE state='pending' AND expires>?", (time.time(),))
    from .node_identity import digest
    for row in rows:
        row["fingerprint"] = digest(row.pop("csr").encode())
    return {"enrollments": rows}


@router.post("/harness/admin/node-enrollments/{rid}/approve", dependencies=admin)
async def approve(rid: str, request: Request):
    return state(request).identity.approve(rid)


@router.post("/harness/admin/nodes/{node_id}/revoke", dependencies=admin)
async def revoke(node_id: str, request: Request):
    state(request).identity.revoke(node_id)
    return {"ok": True}


@router.post("/node/enroll")
async def enroll(body: Enroll, request: Request):
    return state(request).identity.enroll(body.invitation, body.csr)


@router.post("/node/enroll/poll")
async def enroll_poll(body: Invitation, request: Request):
    return state(request).identity.poll(body.invitation)


@router.post("/node/rotate")
async def rotate(request: Request):
    if state(request).settings.ATLAS_GATE_NODE_TLS_PORT and not request.app.state.node_mtls:
        raise GateError(403, "forbidden", "используйте отдельный node mTLS listener")
    node_id = await state(request).identity.authenticate(request)
    body = await request.json()
    csr = body.get("csr", "")
    if len(csr) > 16384:
        raise GateError(413, "payload_too_large", "CSR слишком велик")
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    try:
        parsed = x509.load_pem_x509_csr(csr.encode())
        if not parsed.is_signature_valid:
            raise ValueError("signature")
    except (ValueError, TypeError):
        raise GateError(422, "invalid_request", "CSR отвергнут")
    certificate = state(request).identity.issue(node_id, csr, rotation=True)
    _, ca = state(request).identity.ca()
    return {"nodeId": node_id, "certificate": certificate, "ca": ca.public_bytes(serialization.Encoding.PEM).decode(), "audience": state(request).identity.audience}


@router.get("/node/work")
async def work(request: Request):
    if state(request).settings.ATLAS_GATE_NODE_TLS_PORT and not request.app.state.node_mtls:
        raise GateError(403, "forbidden", "используйте отдельный node mTLS listener")
    node_id = await state(request).identity.authenticate(request)
    if state(request).nodes.config[node_id].transport != "connector":
        raise GateError(403, "forbidden", "исходящий канал не включён для узла")
    def authorized():
        credential = state(request).store._one("SELECT revoked,expires FROM node_credentials WHERE node_id=?", (node_id,))
        return bool(credential and not credential["revoked"] and credential["expires"] > time.time() and state(request).nodes.config[node_id].enabled)
    return {"work": await state(request).channel.poll(node_id, disconnected=request.is_disconnected, authorized=authorized)}


class NodeBodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/node/"):
            return await self.app(scope, receive, send)
        from starlette.exceptions import HTTPException
        from .errors import error_response
        limit = RESULT_LIMIT * 4 // 3 + 1024 if scope["path"].endswith("/result") else 32768
        count = 0
        async def bounded():
            nonlocal count
            chunk = await receive()
            count += len(chunk.get("body", b""))
            if count > limit:
                raise HTTPException(413, "тело запроса превышает предел канала")
            return chunk
        headers = dict(scope.get("headers", []))
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            length = limit + 1
        if length > limit:
            return await error_response(413, "payload_too_large", "тело запроса превышает предел канала")(scope, receive, send)
        await self.app(scope, bounded, send)


@router.post("/node/work/{rid}/result")
async def result(rid: str, request: Request):
    if state(request).settings.ATLAS_GATE_NODE_TLS_PORT and not request.app.state.node_mtls:
        raise GateError(403, "forbidden", "используйте отдельный node mTLS listener")
    if int(request.headers.get("content-length", "0")) > RESULT_LIMIT * 4 // 3 + 1024:
        raise GateError(413, "payload_too_large", "ответ превышает предел канала")
    node_id = await state(request).identity.authenticate(request)
    body = await request.json()
    try:
        raw = base64.b64decode(body["body"], validate=True)
        status = int(body["status"])
        if len(raw) > RESULT_LIMIT or not 100 <= status <= 599:
            raise ValueError("result limit")
    except (ValueError, KeyError, TypeError):
        raise GateError(422, "invalid_request", "неверный ответ команды")
    state(request).channel.complete(node_id, rid, status, raw)
    return {"ok": True}
