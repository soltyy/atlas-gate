"""Compare shared and separate OpenSSH transports in an offline Linux network.

Run as root inside a fresh `unshare --net --pid --fork --mount-proc` process.
The outer namespace must contain only a disconnected loopback device. The script
prints JSON lines to stdout, including transaction timings and TCP counter deltas.
Requires iproute2, ethtool, OpenSSH client/server and a local login user.
"""

import argparse
import concurrent.futures
import contextlib
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import pwd
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


NODES = {"codex": 8041, "claude": 8042}
SSH_PORT = 2222
SIZE = 2 * 1024 * 1024
PAYLOAD = bytes(range(256)) * (SIZE // 256)
SHA = hashlib.sha256(PAYLOAD).hexdigest()
RECEIPT = "/v1/commands/bench-command"
UPLOAD = "/v1/sessions/bench-session/files"
LINKS = (("gate", "router", "gd", "rd", "10.161.1", 12),
         ("gate", "fi", "gf", "fg", "10.161.2", 7),
         ("fi", "router", "fr", "rf", "10.161.3", 8))


def output(*args):
    return subprocess.check_output(args, text=True).strip()


def emit(**values):
    print(json.dumps(values, separators=(",", ":")), flush=True)


def start(stack, args, stderr=None):
    process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=stderr,
                               start_new_session=True)

    def stop():
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()

    stack.callback(stop)
    return process


def wait_for(check, name, seconds=12):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if check():
            return
        time.sleep(0.03)
    raise RuntimeError(f"timed out waiting for {name}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def answer(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/v1/node/identity":
            return self.answer(200, self.server.identity)
        if self.path == RECEIPT:
            return self.answer(200, {"requestId": "bench-command", "state": "complete",
                                     **self.server.identity})
        self.answer(404, {})

    def do_POST(self):
        if self.path != UPLOAD or self.headers.get("Content-Length") != str(SIZE):
            return self.answer(400, {})
        digest = hashlib.sha256()
        first = self.rfile.read(4096)
        digest.update(first)
        (self.server.directory / "first").write_text(str(time.monotonic()))
        rest = SIZE - len(first)
        while rest:
            chunk = self.rfile.read(min(32768, rest))
            if not chunk:
                return self.answer(400, {})
            digest.update(chunk)
            rest -= len(chunk)
        (self.server.directory / "last").write_text(str(time.monotonic()))
        self.answer(200, {"sha256": digest.hexdigest(), **self.server.identity})


def serve(directory):
    with contextlib.ExitStack() as stack:
        for node, port in NODES.items():
            srv = stack.enter_context(ThreadingHTTPServer(("127.0.0.1", port), Handler))
            srv.identity = {"nodeId": node, "bootId": "fixture-" + node}
            srv.directory = directory
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        (directory / "ready").touch()
        signal.pause()


def transaction(port, path, method="GET", body=None, timeout=8):
    with contextlib.closing(http.client.HTTPConnection("127.0.0.1", port, timeout)) as conn:
        conn.request(method, path, body)
        response = conn.getresponse()
        data = json.loads(response.read())
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        return data


def load(directory, node, samples):
    expected = {"nodeId": node, "bootId": "fixture-" + node}
    for port in (8101, 8102):
        def ready():
            try:
                return transaction(port, "/v1/node/identity", timeout=.7) == expected
            except OSError:
                return False

        wait_for(ready, f"identity on {port}")
    for name in ("first", "last"):
        (directory / name).unlink(missing_ok=True)

    def read():
        began = time.monotonic()
        error = None
        try:
            result = transaction(8101, RECEIPT)
            if result != {"requestId": "bench-command", "state": "complete", **expected}:
                raise ValueError("wrong journal receipt")
        except (OSError, ValueError, RuntimeError, http.client.HTTPException) as exc:
            error = type(exc).__name__ + ": " + str(exc)
        return began, time.monotonic(), error

    # A blocked SSH channel must not delay the scheduled launch of later GETs.
    with concurrent.futures.ThreadPoolExecutor(max_workers=samples + 1) as pool:
        upload = pool.submit(transaction, 8102, UPLOAD, "POST", PAYLOAD, 55)
        wait_for((directory / "first").exists, "first upload bytes")
        schedule = time.monotonic() + .02
        futures = []
        for index in range(samples):
            due = schedule + 3.2 * index / samples
            time.sleep(max(0, due - time.monotonic()))
            futures.append(pool.submit(read))
        rows = [f.result() for f in futures]
        if upload.result() != {"sha256": SHA, **expected}:
            raise AssertionError("upload response")
    first, last = (float((directory / k).read_text()) for k in ("first", "last"))
    # Use server-observed bytes; an open HTTP request may outlive the upload.
    if not all(first <= begin < last for begin, _, _ in rows):
        raise AssertionError("GET started outside active upload")
    return {"upload_ms": round((last - first) * 1000, 1),
            "get_ms": [round((end - begin) * 1000, 2) for begin, end, _ in rows],
            "get_start_ms_after_first_upload_bytes": [round((b-first)*1000, 2) for b, _, _ in rows],
            "get_errors": {i: error for i, (_, _, error) in enumerate(rows) if error}}


def namespaces(stack):
    if {i["ifname"] for i in json.loads(output("ip", "-j", "link"))} != {"lo"}:
        raise RuntimeError("start under unshare --net; host-interface manipulation refused")
    if os.geteuid() != 0 or os.getpid() != 1:
        raise RuntimeError("run as root using unshare --net --pid --fork --mount-proc")
    ns = {}
    for name in ("gate", "fi", "router"):
        owner = start(stack, ["unshare", "--net", "sleep", "infinity"])
        flag = f"/proc/{owner.pid}/ns/net"
        wait_for(lambda: os.readlink(flag) != os.readlink("/proc/self/ns/net"), name)
        ns[name] = ["nsenter", "-t", str(owner.pid), "-n"]
        output(*ns[name], "ip", "link", "set", "lo", "up")
    for a, b, left, right, prefix, delay_ms in LINKS:
        output("ip", "link", "add", left, "type", "veth", "peer", "name", right)
        for k, (side, interface) in enumerate(((a, left), (b, right)), 1):
            output("ip", "link", "set", interface, "netns", ns[side][2])
            output(*ns[side], "ip", "addr", "add", prefix + f".{k}/30", "dev", interface)
            output(*ns[side], "ip", "link", "set", interface, "mtu", "1400", "up")
            output(*ns[side], "ethtool", "-K", interface, "tso", "off", "gso", "off", "gro", "off")
            # Each direction has ONE physical bottleneck shared by all SSH flows.
            output(*ns[side], "tc", "qdisc", "add", "dev", interface, "root", "netem",
                   "delay", f"{delay_ms}ms", "rate", "4mbit", "limit", "32")
    return ns


def configure(directory, user):
    directory.chmod(0o755)  # sshd reads the throwaway public key as the login user.
    for name in ("client", "host"):
        output("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(directory / name))
    hostkey = (directory / "host.pub").read_text().strip()
    known = directory / "known_hosts"
    hosts = {"direct": "10.161.1.2", "jump": "10.161.2.2", "standby": "10.161.3.2"}
    known.write_text("".join(f"[{addr}]:{SSH_PORT} {hostkey}\n" for addr in hosts.values()))
    cfg = "".join(f"Host {host}\n  HostName {addr}\n" for host, addr in hosts.items())
    cfg += (f"Host standby\n  ProxyJump jump\nHost *\n  Port {SSH_PORT}\n  User {user}\n"
            f"  IdentityFile {directory}/client\n  UserKnownHostsFile {known}\n"
            "  GlobalKnownHostsFile /dev/null\n  StrictHostKeyChecking yes\n  BatchMode yes\n"
            "  IdentitiesOnly yes\n  IdentityAgent none\n  Compression no\n  ControlMaster no\n"
            "  ControlPath none\n  ExitOnForwardFailure yes\n  LogLevel ERROR\n")
    (directory / "client.conf").write_text(cfg)
    for server in ("fi", "router"):
        permits = "10.161.3.2:2222" if server == "fi" else "127.0.0.1:8041 127.0.0.1:8042"
        (directory / (server + ".conf")).write_text(
            f"Port {SSH_PORT}\nListenAddress 0.0.0.0\nHostKey {directory}/host\n"
            f"PidFile {directory}/{server}.pid\nAuthorizedKeysFile {directory}/client.pub\n"
            f"AllowUsers {user}\nStrictModes no\nPasswordAuthentication no\n"
            "KbdInteractiveAuthentication no\nAuthenticationMethods publickey\nUsePAM yes\n"
            f"AllowTcpForwarding local\nPermitOpen {permits}\nDisableForwarding no\n"
            "AllowAgentForwarding no\nX11Forwarding no\nPermitTTY no\nForceCommand /bin/false\n")


def tcp_snapshot(ns, route, count):
    legs = LINKS[:1] if route == "direct" else LINKS[1:]
    snapshot = {}
    for a, b, left, right, prefix, _ in legs:
        for k, (side, interface) in enumerate(((a, left), (b, right)), 1):
            stats = output(*ns[side], "ss", "-HntOi", "state", "established")
            rows = {}
            for line in stats.splitlines():
                cols = line.split()
                if len(cols) < 4:
                    continue
                local, peer = cols[2:4]
                if local.split(":")[0] != prefix + f".{k}" or not peer.startswith(prefix + "."):
                    continue
                if SSH_PORT not in (int(local.rsplit(":", 1)[1]), int(peer.rsplit(":", 1)[1])):
                    continue
                rtx = re.search(r"\bretrans:\d+/(\d+)", line)
                size = re.search(r"\bbytes_retrans:(\d+)", line)
                sent = re.search(r"\bbytes_sent:(\d+)", line)
                rows[f"{local} > {peer}"] = [int(x.group(1)) if x else 0 for x in (rtx, size, sent)]
            if len(rows) != count:
                raise AssertionError(f"{side}/{interface}: {len(rows)} established SSH sockets; expected {count}")
            snapshot[f"{side}.{interface}"] = rows
    return snapshot


def experiment(ns, directory, node, route, mode, samples):
    with contextlib.ExitStack() as tunnels:
        ssh = [*ns["gate"], "ssh", "-F", str(directory / "client.conf"), "-N", "-T"]
        target = "direct" if route == "direct" else "standby"
        fwd = [["-L", f"127.0.0.1:{port}:127.0.0.1:{NODES[node]}"] for port in (8101, 8102)]
        groups = [fwd[0] + fwd[1]] if mode == "shared" else fwd
        for args in groups:
            start(tunnels, [*ssh, *args, target])

        def ready():
            try:
                tcp_snapshot(ns, route, len(groups))
            except AssertionError:
                return False
            return "127.0.0.1:8102" in output(*ns["gate"], "ss", "-HntOl", "state", "listening")

        wait_for(ready, "SSH forwards")
        before = tcp_snapshot(ns, route, len(groups))
        metrics = json.loads(output(*ns["gate"], sys.executable, __file__, "--load", node,
                                    "--dir", str(directory), "--samples", str(samples)))
        time.sleep(.12)  # allow the final TCP ACKs to settle before reading counters
        after = tcp_snapshot(ns, route, len(groups))
        delta = {}
        for side, rows in after.items():
            if rows.keys() != before[side].keys():
                raise AssertionError("SSH socket changed during measurement")
            delta[side] = {k: [v - old for v, old in zip(values, before[side][k])]
                           for k, values in rows.items()}
        return {**metrics, "tcp": delta}


def summary(runs):
    all_ms = sorted(ms for run in runs for i, ms in enumerate(run["get_ms"])
                    if str(i) not in run["get_errors"])
    errors = sum(len(run["get_errors"]) for run in runs)
    percentiles = [all_ms[math.ceil(q * len(all_ms)) - 1] if all_ms else None for q in (.5, .95, .99)]
    return dict(zip(("p50", "p95", "p99"), percentiles), success=len(all_ms), errors=errors)


def bench(args):
    user = os.environ.get("SUDO_USER") or "nobody"
    pwd.getpwnam(user)
    with tempfile.TemporaryDirectory(prefix="atlas-file-bench-") as tmp, contextlib.ExitStack() as stack:
        directory = Path(tmp)
        ns = namespaces(stack)
        configure(directory, user)
        stderr = stack.enter_context((directory / "sshd.log").open("w"))
        for server in ("router", "fi"):
            start(stack, [*ns[server], "/usr/sbin/sshd", "-De", "-f", str(directory / (server + ".conf"))], stderr)
        start(stack, [*ns["router"], sys.executable, __file__, "--serve", "--dir", str(directory)])
        wait_for((directory / "ready").exists, "Router fixture")
        emit(kind="setup", bytes=SIZE, sha256=SHA, ssh=output("ssh", "-Q", "protocol-version"),
             kernel=output("uname", "-r"), legs=LINKS, rate_mbit=4, qlimit=32, mtu=1400,
             tcp_fields=["retransmitted_segments", "retransmitted_bytes", "sent_bytes"],
             samples=args.samples, rounds=args.rounds)
        runs = {}
        for rep in range(args.rounds):
            for i, node in enumerate(NODES):
                for j, route in enumerate(("direct", "fi")):
                    modes = ("shared", "separate") if (rep+i+j) % 2 == 0 else ("separate", "shared")
                    for mode in modes:
                        result = experiment(ns, directory, node, route, mode, args.samples)
                        emit(kind="run", round=rep+1, node=node, route=route, mode=mode, **result)
                        runs.setdefault((node, route, mode), []).append(result)
        for (node, route, mode), data in runs.items():
            emit(kind="summary", node=node, route=route, mode=mode, **summary(data))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=64)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--load", choices=NODES)
    parser.add_argument("--dir", type=Path)
    args = parser.parse_args()
    if not 1 <= args.samples <= 100 or args.rounds < 1:
        parser.error("specify 1..100 GETs per round and at least one round")
    if args.serve:
        serve(args.dir)
    elif args.load:
        print(json.dumps(load(args.dir, args.load, args.samples)))
    else:
        bench(args)
