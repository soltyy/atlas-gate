"""Watch CREATE and TCP closes between Gate and Router, retaining only transport metadata."""
import argparse
import collections
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import signal
import socket
import struct
import subprocess
import threading
import time

# Both deployments use IPv4 loopback with Ethernet framing (DLT_EN10MB).
# The kernel tests the full packet. 54 captured bytes are Ethernet + minimum
# IPv4 + minimum TCP. No application bytes can be copied to user space.
SNAPLEN = 54
PORTS = {
    "gate": (8766, 38765, 38768, 8765, 8768, 28765, 28768),
    "router": (8765, 8768),
}
PEERS = {"gate": ("185.224.208.221", "185.9.26.113"),
         "router": ("217.177.32.205", "185.9.26.113")}


def prefix(data):
    """Generate a classic BPF test at the start of TCP payload."""
    terms = []
    offset = 0
    for size in (4, 2, 1):
        while len(data) - offset >= size:
            value = int.from_bytes(data[offset:offset + size], "big")
            terms.append(f"tcp[((tcp[12] & 0xf0) >> 2) + {offset}:{size}] = {value}")
            offset += size
    return "(" + " and ".join(terms) + ")"


def filters(role, external):
    ports = PORTS[role]
    def portset(direction):
        return "(" + " or ".join(f"{direction} port {p}" for p in ports) + ")"
    loop = "ip and tcp and src host 127.0.0.1 and dst host 127.0.0.1 and "
    routes = [b"POST /v1/sessions HTTP/"]
    if role == "gate":
        routes.append(b"POST /harness/agent/sessions HTTP/")
    path = "(" + " or ".join(prefix(route) for route in routes) + ")"
    flags = "(tcp[tcpflags] & (tcp-fin|tcp-rst) != 0)"
    yield "create", "lo", loop + portset("dst") + " and " + path
    yield "response", "lo", loop + portset("src") + " and " + prefix(b"HTTP/1.") + " and tcp[((tcp[12] & 0xf0) >> 2)+9] >= 50"
    yield "close", "lo", loop + portset("") + " and " + flags
    for dev, peers in external.items():
        yield "ssh_close", dev, "ip and tcp and port 22 and (" + " or ".join("host " + p for p in peers) + ") and " + flags


def packets(stream, ready):
    header = stream.read(24)
    if len(header) != 24 or header[:4] not in (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4"):
        raise ValueError("unrecognised capture format")
    endian = "<" if header[:4] == b"\xd4\xc3\xb2\xa1" else ">"
    snap, link = struct.unpack_from(endian + "II", header, 16)
    if snap != SNAPLEN or link != 1:
        raise ValueError("unexpected capture size or link type")
    ready()
    while record := stream.read(16):
        sec, us, length, _ = struct.unpack(endian + "IIII", record)
        if length > SNAPLEN:
            raise ValueError("unexpected packet size")
        p = stream.read(length)
        if len(p) != length:
            raise ValueError("incomplete packet")
        if len(p) < 48 or p[12:14] != b"\x08\x00" or p[23] != 6:
            continue
        off = 14 + (p[14] & 0xf) * 4
        if len(p) < off + 14:
            continue
        sport, dport, seq, ack = struct.unpack_from("!HHII", p, off)
        yield (sec * 1_000_000 + us, socket.inet_ntoa(p[26:30]), sport,
               socket.inet_ntoa(p[30:34]), dport, seq, ack, p[off+13])


def run(role, directory):
    directory.mkdir(exist_ok=True)
    logger = logging.getLogger("socket_trace")
    logger.setLevel(logging.INFO)
    logger.addHandler(RotatingFileHandler(directory / "events.jsonl",
                                         maxBytes=2*1024*1024, backupCount=4))
    def record(**fields):
        logger.info(json.dumps(fields, separators=(",", ":")))
    external = {}
    for peer in PEERS[role]:
        route = json.loads(subprocess.check_output(["ip", "-j", "-4", "route", "get", peer]))[0]
        external.setdefault(route["dev"], []).append(peer)
    events = queue.Queue(maxsize=2048)
    ended = threading.Event()
    processes = []
    def reader(kind, dev, proc):
        try:
            for packet in packets(proc.stdout, lambda: events.put(('reader_ready', dev, kind))):
                events.put((kind, dev, packet))
        except (ValueError, OSError, struct.error) as exc:
            logging.error("capture %s: %s", kind, type(exc).__name__)
        finally:
            ended.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: ended.set())
    try:
        for kind, dev, expression in filters(role, external):
            proc = subprocess.Popen(["/usr/bin/tcpdump", "-p", "-nn", "-i", dev, "-y", "EN10MB",
                                     "-s", str(SNAPLEN), "-B", "256", "--immediate-mode",
                                     "-U", "-w", "-", expression], stdout=subprocess.PIPE)
            processes.append(proc)
            threading.Thread(target=reader, args=(kind, dev, proc), daemon=True).start()
        record(event="observer_start", role=role, utc_us=time.time_ns()//1000)
        # tcpdump streams are independent; keep a small header-time index to
        # handle a response reaching this queue before its short local request.
        creates = collections.OrderedDict()
        replies = collections.OrderedDict()
        def remember(table, key, packet):
            table[key] = packet
            table.move_to_end(key)
            if len(table) > 2048:
                table.popitem(last=False)
        def response(start, reply):
            ts,src,sp,dst,dp,seq,ack,_ = reply
            if ts >= start[0] and ((ack-start[5]) & 0xffffffff) < 0x80000000:
                record(event="response_headers", utc_us=ts, src=src, sport=sp, dst=dst, dport=dp,
                       seq=seq, ack=ack, create_us=start[0])
                return True
            return False
        ready = set()
        while not ended.is_set():
            try:
                kind, dev, p = events.get(timeout=1)
            except queue.Empty:
                continue
            if kind == 'reader_ready':
                ready.add((dev, p))
                if len(ready) == len(processes):
                    record(event='observer_ready', role=role, utc_us=time.time_ns()//1000)
                continue
            ts,src,sp,dst,dp,seq,ack,flags = p
            if kind == "create":
                key = (src, sp, dst, dp)
                if key in creates and creates[key][5] == seq:
                    continue
                record(event="create", utc_us=ts, src=src, sport=sp, dst=dst, dport=dp, seq=seq, ack=ack)
                buffered = replies.pop(key, None)
                if buffered and response(p, buffered):
                    creates.pop(key, None)
                else:
                    remember(creates, key, p)
            elif kind == "response":
                key = (dst, dp, src, sp)
                start = creates.get(key)
                if start and response(start, p):
                    creates.pop(key, None)
                else:
                    remember(replies, key, p)
            else:
                record(event=kind, utc_us=ts, dev=dev, src=src, sport=sp, dst=dst,
                       dport=dp, fin=bool(flags & 1), rst=bool(flags & 4), seq=seq, ack=ack)
        if any(proc.poll() is not None for proc in processes):
            raise RuntimeError("capture process exited")
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
        for proc in processes:
            proc.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=PORTS)
    args = parser.parse_args()
    os.umask(0o077)
    run(args.role, Path(os.environ["STATE_DIRECTORY"]))
