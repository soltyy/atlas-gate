"""Run with Linux root inside a disposable network namespace; no working endpoints are used."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

class Fixture(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(201)
        self.end_headers()
    def log_message(self, *args):
        pass


def wait_for(check):
    limit = time.monotonic() + 10
    while time.monotonic() < limit:
        if check():
            return
        time.sleep(.05)
    raise AssertionError("observer proof missing before test deadline")


def exercise(role, folder, script):
    proc = subprocess.Popen([sys.executable, "-I", script, role], env=dict(os.environ, STATE_DIRECTORY=str(folder)))
    events = folder/"events.jsonl"
    def rows():
        return [json.loads(line) for line in events.read_text().splitlines()] if events.exists() else []
    srv = ThreadingHTTPServer(("127.0.0.1", 8765), Fixture)
    worker = threading.Thread(target=srv.serve_forever, daemon=True)
    worker.start()
    try:
        wait_for(lambda: len(Path("/proc/net/packet").read_text().splitlines()) >= 5)
        for route in ("/v1/sessions/ignore/files", "/v1/sessions"):
            client = http.client.HTTPConnection("127.0.0.1", 8765)
            client.request("POST", route, body=b"TEST_SECRET_MUST_NEVER_BE_STORED",
                           headers={"Authorization": "TEST_CREDENTIAL_MUST_NEVER_BE_STORED"})
            reply = client.getresponse()
            assert reply.status == 201
            reply.read()
            client.close()
        s=socket.create_connection(("127.0.0.1",8765))
        rst_port=s.getsockname()[1]
        s.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack("ii",1,0))
        s.close()
        wait_for(lambda: any(r.get("event")=="response_headers" for r in rows())
                 and any(r.get("rst") and r["sport"]==rst_port for r in rows()))
        log=rows(); starts=[r for r in log if r.get("event")=="create"]
        assert len(starts)==1 and starts[0]["dport"]==8765
        assert any(r.get("create_us")==starts[0]["utc_us"] for r in log)
        assert any(r.get("fin") for r in log)
        assert "TEST_" not in events.read_text()
        print(role,"one CREATE, response, FIN, directed client RST; unrelated POST and secrets excluded")
    finally:
        srv.shutdown()
        srv.server_close()
        proc.send_signal(signal.SIGTERM)
        proc.wait(8)


if __name__=="__main__":
    if os.readlink("/proc/self/ns/net")==os.readlink("/proc/1/ns/net"):
        raise SystemExit("Run in a private network namespace")
    subprocess.run(["ip","link","set","lo","up"], check=True)
    subprocess.run(["ip","route","add","default","dev","lo"], check=True)
    script=Path(__file__).with_name("observe.py")
    with tempfile.TemporaryDirectory() as root:
        for role in ("gate","router"):
            exercise(role,Path(root)/role,script)
