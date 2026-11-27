"""Offline HAProxy transport test. Run under unshare --net with only loopback."""

import argparse
import contextlib
import http.client
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


RESULT = "/v1/sessions/canary/tool_result"
EVENTS = "/v1/sessions/canary/turns/test-turn/events"
KEY = "tool-result:canary:call-1"
RECEIPT = "/v1/commands/" + KEY
BODY = b'{"callId":"call-1","content":"already executed","isError":false}'


class Node:
    def __init__(self, name):
        self.node_id = "canary-" + name
        self.boot_id = secrets.token_hex(16)
        self.token = secrets.token_hex(12)
        self.receipt = None
        self.posts = []
        self.events = [{"seq": 1, "type": "tool_call", "callId": "call-1"}]
        self.lock = threading.Lock()


class Route(ThreadingHTTPServer):
    def __init__(self, node, name):
        super().__init__(("127.0.0.1", 0), Handler)
        self.node = node
        self.name = name
        self.identity = (node.node_id, node.boot_id)
        self.catalog_bytes = 0
        self.delay_next = 0.0
        self.probe_times = []
        self.sdk_probes = 0
        self.business_reads = 0
        self.checked = threading.Event()
        self.health_probes = Counter()
        self.release_stream = threading.Event()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, value):
        body = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Canary-Path", self.server.name)
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def do_GET(self):
        route = self.server
        node = route.node
        target = urlsplit(self.path)
        if self.headers.get("X-Atlas-Token") != node.token:
            return self.reply(401, {})
        if target.path == "/v1/node":
            route.sdk_probes += 1
            return self.reply(500, {})
        if target.path == "/v1/node/identity":
            identity = route.identity
            route.probe_times.append(time.monotonic())
            route.health_probes[identity] += 1
            delay, route.delay_next = route.delay_next, 0
            if delay:
                time.sleep(delay)
            route.checked.set()
            if identity is None:
                return self.reply(503, {})
            value = {"nodeId": identity[0], "bootId": identity[1], "protocolVersion": 1}
            if route.catalog_bytes:
                value["catalog"] = "x" * route.catalog_bytes
            return self.reply(200, value)
        # Even a mismatched/unavailable test route answers ordinary requests.
        # A 503 observed by the client must come from the selector itself.
        route.business_reads += 1
        if target.path == "/canary/stream":
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.send_header("X-Canary-Path", route.name)
            self.end_headers()
            self.wfile.write(b"a")
            self.wfile.flush()
            if route.release_stream.wait(35):
                self.wfile.write(b"b")
            return
        if target.path == "/canary/route":
            return self.reply(200, {"path": route.name})
        if target.path == RECEIPT:
            return self.reply(200 if node.receipt else 404, node.receipt)
        if target.path == EVENTS:
            after = int(parse_qs(target.query)["after"][0])
            events = [e for e in node.events if e["seq"] > after]
            return self.reply(200, {"turnId": "test-turn", "events": events,
                                    "nextAfter": node.events[-1]["seq"]})
        self.reply(404, {})

    def do_POST(self):
        route = self.server
        node = route.node
        if self.headers.get("X-Atlas-Token") != node.token:
            return self.reply(401, {})
        if self.path != RESULT:
            return self.reply(404, {})
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        key = self.headers.get("Idempotency-Key")
        with node.lock:
            node.posts.append((route.name, key, raw))
            if node.receipt is None:
                node.receipt = {"requestId": key, "state": "complete", "status": 200,
                                "response": {"ok": True, "callId": "call-1",
                                             "status": "accepted"}, "headers": {}}
                node.events.append({"seq": 2, "type": "result", "callId": "call-1"})
        if route.name == "direct":
            route.identity = None
            self.close_connection = True   # committed, but connection closes before ACK
            return
        self.reply(200, node.receipt["response"])


def request(port, node, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    headers = {"X-Atlas-Token": node.token, "X-Atlas-Node": node.node_id,
               "X-Atlas-Boot": node.boot_id}
    if body is not None:
        headers["Idempotency-Key"] = KEY
        headers["Content-Type"] = "application/json"
    try:
        conn.request("GET" if body is None else "POST", path, body, headers)
        response = conn.getresponse()
        content = response.read()
        value = json.loads(content) if response.status == 200 else None
        return response.status, response.getheader("X-Canary-Path"), value
    except (OSError, http.client.RemoteDisconnected):
        return None, None, None
    finally:
        conn.close()


def wait_for(description, proc, predicate):
    # Only bounds a failed test. Pass requires observing the requested evidence.
    end = time.monotonic() + 25
    while time.monotonic() < end:
        if proc.poll() is not None:
            raise RuntimeError(f"HAProxy exited with {proc.returncode}")
        if predicate():
            return
        time.sleep(0.08)
    raise AssertionError("No proof before test deadline: " + description)


def check_preflight(script, env, nodes, routes):
    def verify(*args):
        return subprocess.run([sys.executable, str(script), *args], env=env,
                              capture_output=True, text=True, timeout=20)

    standby = routes["codex"]["standby"]
    original = standby.identity
    try:
        for identity in [(nodes["claude"].node_id, original[1]),
                         (original[0], "wrong-boot")]:
            standby.identity = identity
            result = verify()
            assert result.returncode != 0 and not result.stdout.strip(), result.stdout
    finally:
        standby.identity = original
    standby.catalog_bytes = 33000
    try:
        result = verify()
        assert result.returncode != 0 and not result.stdout.strip()
    finally:
        standby.catalog_bytes = 0
    result = verify()
    result.check_returncode()
    pins = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert pins == {name.upper() + "_BOOT_ID": node.boot_id for name, node in nodes.items()}
    env.update(pins)
    with tempfile.TemporaryDirectory() as state:
        pin_file = Path(state) / "verified-boots.env"
        verify("--write-pins", pin_file).check_returncode()
        previous = pin_file.read_bytes()
        assert previous.decode("ascii") == result.stdout
        assert pin_file.stat().st_mode & 0o077 == 0
        standby.identity = None
        try:
            assert verify("--write-pins", pin_file).returncode != 0
            assert pin_file.read_bytes() == previous
        finally:
            standby.identity = original
    print("PASS preflight: pins both real test boots; rejects a wrong node and mismatched boots")


def exercise(proc, ports, nodes, routes):
    def get(name, path="/canary/route"):
        return request(ports[name], nodes[name], path)

    def active(name, expected):
        wait_for(f"{name} on {expected}", proc,
                 lambda: get(name)[:2] == (200, expected))

    for name in nodes:
        active(name, "direct")
        for route in routes[name].values():
            wait_for(f"authenticated health check on {name}/{route.name}", proc,
                     route.checked.is_set)
    codex = nodes["codex"]
    direct, standby = routes["codex"]["direct"], routes["codex"]["standby"]
    before = get("codex", EVENTS + "?after=0")
    assert before[:2] == (200, "direct") and before[2]["nextAfter"] == 1
    ack = request(ports["codex"], codex, RESULT, BODY)
    assert ack[0] is None or ack[0] >= 500, f"unexpected ACK: {ack[:2]}"
    active("codex", "standby")
    status, via, receipt = get("codex", RECEIPT)
    assert (status, via) == (200, "standby")
    assert receipt == {"requestId": KEY, "state": "complete", "status": 200,
                       "response": {"ok": True, "callId": "call-1", "status": "accepted"},
                       "headers": {}}
    status, via, resumed = get("codex", EVENTS + "?after=" + str(before[2]["nextAfter"]))
    assert (status, via) == (200, "standby") and resumed["nextAfter"] == 2
    assert resumed["events"] == [{"seq": 2, "type": "result", "callId": "call-1"}]
    assert codex.posts == [("direct", KEY, BODY)], "the result POST was replayed"
    assert get("claude")[:2] == (200, "direct")
    print("PASS lost ACK: direct committed one POST, standby returned its receipt and seq=2")

    def rejected():
        return get("codex")[0] in (None, 502, 503, 504)

    standby.catalog_bytes = 33000
    wait_for("oversized node document rejected", proc, rejected)
    assert get("claude")[:2] == (200, "direct")
    standby.catalog_bytes = 0
    active("codex", "standby")
    print("PASS oversized health response rejected; standby recovers with a valid response")

    for scenario, identity in [
        ("wrong nodeId", (nodes["claude"].node_id, codex.boot_id)),
        ("wrong bootId", (codex.node_id, "incorrect-boot")),
        ("both paths unavailable", None),
    ]:
        standby.identity = identity
        wait_for(scenario, proc, rejected)
        previous = (direct.business_reads, standby.business_reads)
        assert rejected() and previous == (direct.business_reads, standby.business_reads)
        assert get("claude")[:2] == (200, "direct")
        standby.identity = (codex.node_id, codex.boot_id)
        active("codex", "standby")
        print(f"PASS {scenario}: Codex fails closed, Claude stays direct")
    assert codex.posts == [("direct", KEY, BODY)]
    assert routes["claude"]["standby"].business_reads == 0

    # A healthy direct Router may restart while the backup still advertises an
    # old process. Losing direct now must not reconnect to that stale process.
    new_boot = secrets.token_hex(16)
    direct.identity = (codex.node_id, new_boot)
    wait_for("replacement direct is fenced immediately", proc, rejected)
    direct.identity = None
    wait_for("new direct boot retires stale backup", proc, rejected)
    wrong_direct = (nodes["claude"].node_id, codex.boot_id)
    direct.identity = wrong_direct
    wait_for("wrong direct node examined", proc, lambda: direct.health_probes[wrong_direct] >= 2)
    assert rejected(), "wrong direct identity must not re-enable stale standby"
    direct.identity = None
    standby.identity = (codex.node_id, new_boot)
    wait_for("standby observed at its replacement boot", proc,
             lambda: standby.health_probes[(codex.node_id, new_boot)] >= 2)
    assert rejected(), "unverified replacement boot admitted"
    direct.identity = (codex.node_id, new_boot)
    wait_for("unverified direct remains fenced", proc, rejected)
    direct.identity = (codex.node_id, codex.boot_id)
    active("codex", "direct")
    print("PASS changed direct boot: blocks both the replacement and the retired standby")

    claude = nodes["claude"]
    routes["claude"]["direct"].identity = None
    active("claude", "standby")
    assert get("codex")[:2] == (200, "direct")
    via_fi = routes["claude"]["standby"]
    with contextlib.closing(http.client.HTTPConnection("127.0.0.1", ports["claude"],
                                                       timeout=5)) as conn:
        try:
            conn.request("GET", "/canary/stream", headers={"X-Atlas-Token": claude.token})
            stream = conn.getresponse()
            assert stream.status == 200 and stream.getheader("X-Canary-Path") == "standby"
            assert stream.read(1) == b"a"
            routes["claude"]["direct"].identity = (claude.node_id, claude.boot_id)
            active("claude", "direct")
            via_fi.release_stream.set()
            assert stream.read() == b"b"
        finally:
            via_fi.release_stream.set()
    print("PASS Claude failover and failback: existing standby stream completed intact")


def stop_process(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@contextlib.contextmanager
def selector(cmd, config, env):
    with tempfile.TemporaryFile(mode="w+b") as err:
        proc = subprocess.Popen([*cmd, "-db"], cwd=config.parent, env=env,
                                stdout=err, stderr=err)
        try:
            yield proc
        except BaseException:
            stop_process(proc)
            err.seek(0)
            print(err.read().decode(errors="replace")[-3500:])
            raise
        finally:
            stop_process(proc)


def cold_start(cmd, config, script, env, ports, nodes, routes):
    # A bound but non-listening port refuses actual TCP connects. Restart the
    # configured binary each time, simulating loss of either SSH service.
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        for absent, remaining in (("DIRECT", "standby"), ("STANDBY", "direct")):
            degraded = dict(env)
            for name in nodes:
                degraded[f"{name.upper()}_{absent}_PORT"] = str(closed.getsockname()[1])
            with selector(cmd, config, degraded) as proc:
                for name, node in nodes.items():
                    wait_for(f"restart {name} on {remaining}", proc,
                             lambda: request(ports[name], node, "/canary/route")[:2]
                             == (200, remaining))
                subprocess.run([sys.executable, str(script), "--check-selector"],
                               env=degraded, check=True, timeout=20)
                if absent == "DIRECT":
                    standby = routes["codex"]["standby"]
                    original = standby.identity
                    wrong = (original[0], "unverified-boot")
                    standby.identity = wrong
                    try:
                        wait_for("new boot examined", proc, lambda: standby.health_probes[wrong] >= 2)
                        reads = standby.business_reads
                        assert request(ports["codex"], nodes["codex"], "/canary/route")[0] != 200
                        assert standby.business_reads == reads
                    finally:
                        standby.identity = original
            print(f"PASS selector restart with {absent.lower()} unreachable for both Routers")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--haproxy", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--preflight", type=Path, help="defaults to preflight.py next to config")
    args = parser.parse_args()
    if not __debug__:
        parser.error("assertions must be enabled")
    if {name for _, name in socket.if_nameindex()} != {"lo"}:
        parser.error("a loopback-only network namespace is required")
    if not int(Path("/sys/class/net/lo/flags").read_text().strip(), 16) & 1:
        subprocess.run(["ip", "link", "set", "lo", "up"], check=True)
    config = args.config.resolve(strict=True)
    with contextlib.ExitStack() as stack:
        env = os.environ.copy()
        env["ROUTER_PATH_CHECK_INTERVAL"] = "1s"
        env["ROUTER_PATH_WATCHER"] = str(config.with_name("path_checks.lua"))
        nodes, ports, routes = {}, {}, {}
        # Reserve all frontend ports at once so the two kernel allocations differ.
        front_sockets = []
        for name in ("codex", "claude"):
            node = nodes[name] = Node(name)
            front = stack.enter_context(socket.socket())
            front.bind(("127.0.0.1", 0))
            front_sockets.append(front)
            ports[name] = front.getsockname()[1]
            settings = {"LISTEN_PORT": ports[name], "CHECK_TOKEN": node.token,
                        "NODE_ID": node.node_id}
            routes[name] = {}
            for path in ("direct", "standby"):
                route = routes[name][path] = Route(node, path)
                stack.callback(route.server_close)
                thread = threading.Thread(target=route.serve_forever, daemon=True)
                thread.start()
                stack.callback(thread.join)
                stack.callback(route.shutdown)
                settings[path.upper() + "_PORT"] = route.server_port
            env.update({name.upper() + "_" + k: str(v) for k, v in settings.items()})
        script = (args.preflight or config.with_name("preflight.py")).resolve(strict=True)
        check_preflight(script, env, nodes, routes)
        for paths in routes.values():
            for route in paths.values():
                route.checked.clear()
        for s in front_sockets:
            s.close()
        cmd = [args.haproxy, "-f", str(config)]
        subprocess.run([*cmd, "-c"], check=True, cwd=config.parent, env=env)
        cold_start(cmd, config, script, env, ports, nodes, routes)
        # Fixtures below count only reads/health observations in the lost-ACK case.
        for paths in routes.values():
            for route in paths.values():
                route.business_reads = 0
                route.checked.clear()
        with selector(cmd, config, env) as proc:
            exercise(proc, ports, nodes, routes)


if __name__ == "__main__":
    # Let ExitStack reap the foreground canary and close all fixture sockets.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    main()
