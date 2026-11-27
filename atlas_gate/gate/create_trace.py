"""Timing and transport evidence for a single session-create attempt."""
from contextvars import ContextVar
import hashlib
import logging
import time
from uuid import uuid4


log = logging.getLogger("atlas_gate")
ingress_id = ContextVar("gate_create_ingress_id", default="-")


async def trace_ingress(app, scope, receive, send):
    attempt = uuid4().hex
    token = ingress_id.set(attempt)
    started = time.monotonic()
    status = None
    outcome = "interrupted"

    async def observe_send(message):
        nonlocal status
        await send(message)
        if message["type"] == "http.response.start":
            status = message["status"]

    try:
        log.info("session_create ingress_start attempt=%s", attempt)
        await app(scope, receive, observe_send)
        outcome = "asgi_returned"
    finally:
        ingress_id.reset(token)
        log.info("session_create ingress_end attempt=%s status=%s outcome=%s elapsedMs=%d",
                 attempt, status, outcome, (time.monotonic() - started) * 1000)


def trace_binding(key, old):
    attempt = ingress_id.get()
    if attempt != "-":
        # Hash the already device-scoped database key. Neither the client key nor
        # the Router command ID is written to the trace.
        tag = hashlib.sha256(key.encode("ascii")).hexdigest()[:16]
        log.info("session_create binding attempt=%s keyTag=%s prior=%s", attempt, tag,
                 old["status"] if old else "new")


class CreateWireTrace:
    def __init__(self):
        self.id = uuid4().hex
        self.start = time.monotonic()
        self.connection = "unknown"
        self.local_port = self.peer_port = None
        self.last_phase = "dispatch"
        self.marks = []

    def observe_ports(self, stream):
        if stream is None:
            return
        try:
            local = stream.get_extra_info("client_addr")
            peer = stream.get_extra_info("server_addr")
        except (AttributeError, OSError, ValueError, RuntimeError, LookupError):
            return
        if isinstance(local, tuple) and len(local) >= 2 and isinstance(local[1], int):
            self.local_port = local[1]
        if isinstance(peer, tuple) and len(peer) >= 2 and isinstance(peer[1], int):
            self.peer_port = peer[1]

    async def on_event(self, event, info):
        # httpcore passes headers, request objects and exception messages in info.
        # Only connection establishment is allowed to inspect a returned stream.
        protocol, _, stage = event.partition(".")
        phase, _, state = stage.partition(".")
        if protocol not in {"connection", "http11", "http2"} or phase not in {
            "connect_tcp", "start_tls", "send_request_headers", "send_request_body",
            "receive_response_headers", "receive_response_body",
        } or state not in {"started", "complete", "failed"}:
            return
        if phase == "connect_tcp":
            self.connection = "fresh"
            if state == "complete":
                self.observe_ports(info.get("return_value"))
        if phase == "send_request_headers" and self.connection == "unknown":
            self.connection = "reused"
        self.last_phase = stage
        self.marks.append(f"{stage}:{int((time.monotonic() - self.start) * 1000)}")

    def finish(self, *, response=None, error=None):
        if response is not None:
            self.observe_ports(response.extensions.get("network_stream"))
        chain = []
        detail = "-"
        current = error
        while current is not None and len(chain) < 5:
            chain.append(type(current).__name__)
            if current.args == ("Server disconnected without sending a response.",):
                detail = "eof_before_headers"
            if isinstance(current, OSError) and current.errno is not None:
                detail = f"errno_{current.errno}"
            current = current.__cause__
        log.info("session_create wire_end attempt=%s traceId=%s status=%s elapsedMs=%d phase=%s "
                 "connection=%s localPort=%s peerPort=%s errors=%s detail=%s marksMs=%s",
                 ingress_id.get(), self.id, response.status_code if response is not None else None,
                 (time.monotonic() - self.start) * 1000, self.last_phase, self.connection,
                 self.local_port, self.peer_port, "/".join(chain) or "-", detail, ",".join(self.marks) or "-")
