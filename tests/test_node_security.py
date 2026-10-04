import hashlib
import json
import time
from types import SimpleNamespace
import uuid
import httpx
import jwt
import pytest
from fastapi import FastAPI, Request
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID
from atlas_gate.gate.errors import GateError, GateRoute
from atlas_gate.gate.node_identity import NodeIdentity
from atlas_gate.gate.node_channel import Channel
from atlas_gate.gate.store import Store


def csr(key):
    return x509.CertificateSigningRequestBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "client claim ignored")])).sign(key, None).public_bytes(serialization.Encoding.PEM).decode()


@pytest.mark.asyncio
async def test_identity_approval_signed_request_replay_rotation_revocation(tmp_path):
    store = Store(str(tmp_path / "gate.db"))
    nodes = SimpleNamespace(config={"a": SimpleNamespace(enabled=True)}, snapshots={})
    identity = NodeIdentity(store, nodes, tmp_path / "pki", "gate-unique")
    key = ed25519.Ed25519PrivateKey.generate()
    token = identity.invite("a")["invitation"]
    with pytest.raises(GateError):
        identity.enroll(token, "bad CSR")
    identity.enroll(token, csr(key))
    assert identity.poll(token)["state"] == "pending"
    row = identity.invitation(token)
    identity.approve(row["token_hash"])
    result = identity.poll(token)
    cert = x509.load_pem_x509_certificate(result["certificate"].encode())
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "a"
    app = FastAPI()
    app.router.route_class = GateRoute
    @app.post("/proof")
    async def proof(request: Request):
        return {"node": await identity.authenticate(request)}
    def headers(path="/proof", raw=b"{}", nonce=None, signer=key, serial=str(cert.serial_number), audience="gate-unique"):
        now = int(time.time())
        claims = {"iss": "a", "sub": "a", "aud": audience, "iat": now, "exp": now + 60, "jti": nonce or str(uuid.uuid4()), "serial": serial, "request": hashlib.sha256(b"POST" + path.encode() + raw).hexdigest()}
        return {"X-Atlas-Node": "a", "X-Atlas-Proof": jwt.encode(claims, signer, algorithm="EdDSA")}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://gate") as c:
        h = headers()
        assert (await c.post("/proof", content=b"{}", headers=h)).status_code == 200
        assert (await c.post("/proof", content=b"{}", headers=h)).status_code == 401
        assert (await c.post("/proof", content=b'{"changed":true}', headers=headers())).status_code == 401
        assert (await c.post("/proof", content=b"{}", headers=headers(audience="other-gate"))).status_code == 401
        assert (await c.post("/proof", content=b"{}", headers=headers(signer=ed25519.Ed25519PrivateKey.generate()))).status_code == 401
        store._exec("UPDATE node_credentials SET expires=? WHERE node_id='a'", (time.time() + 10 * 86400,))
        changed = identity.issue("a", csr(key), rotation=True)
        new_cert = x509.load_pem_x509_certificate(changed.encode())
        assert cert.serial_number != new_cert.serial_number
        assert identity.issue("a", csr(key), rotation=True) == changed
        assert (await c.post("/proof", content=b"{}", headers=headers())).status_code == 200
        store._exec("UPDATE node_credentials SET previous_until=0 WHERE node_id='a'")
        assert (await c.post("/proof", content=b"{}", headers=headers())).status_code == 401
        new = headers(serial=str(new_cert.serial_number))
        identity.revoke("a")
        assert (await c.post("/proof", content=b"{}", headers=new)).status_code == 401
    store.close()


@pytest.mark.asyncio
async def test_channel_lease_ack_duplicates_node_owner_and_queue_bound(tmp_path):
    import asyncio
    store = Store(str(tmp_path / "gate.db"))
    channel = Channel(store)
    task = asyncio.create_task(channel.request("a", "POST", "/v1/sessions", content=b"{}", headers={"Idempotency-Key": "same"}))
    await asyncio.sleep(0)
    work = await channel.poll("a", wait=0)
    assert work["id"]
    assert await channel.poll("a", wait=0) is None
    with pytest.raises(GateError) as wrong:
        channel.complete("b", work["id"], 200, b"{}")
    assert wrong.value.status == 404
    store._exec("UPDATE node_work SET lease=0 WHERE id=?", (work["id"],))
    replay = await channel.poll("a", wait=0)
    assert replay == work
    channel.complete("a", work["id"], 200, b"{}")
    channel.complete("a", work["id"], 200, b"{}")
    assert (await task).status_code == 200
    with pytest.raises(GateError) as conflict:
        channel.complete("a", work["id"], 500, b"different")
    assert conflict.value.status == 409
    tasks = [asyncio.create_task(channel.request("a", "GET", "/v1/node")) for _ in range(64)]
    await asyncio.sleep(0)
    with pytest.raises(GateError) as full:
        await channel.request("a", "GET", "/v1/node")
    assert full.value.status == 429
    for pending in tasks:
        pending.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    store.close()
