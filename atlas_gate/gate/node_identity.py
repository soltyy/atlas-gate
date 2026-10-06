"""Приглашение → подписанный CSR → одобрение; proof привязан к запросу и сертификату."""
from __future__ import annotations
import hashlib
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import jwt
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from .errors import GateError


def digest(data):
    return hashlib.sha256(data).hexdigest()


class NodeIdentity:
    def __init__(self, store, nodes, directory, audience):
        self.store, self.nodes, self.audience = store, nodes, audience
        self.directory = Path(directory)
        store._exec("""CREATE TABLE IF NOT EXISTS node_invites(token_hash TEXT PRIMARY KEY,
            node_id TEXT NOT NULL, expires REAL NOT NULL, csr TEXT, state TEXT NOT NULL,
            certificate TEXT)""")
        store._exec("""CREATE TABLE IF NOT EXISTS node_credentials(node_id TEXT PRIMARY KEY,
            public_key TEXT NOT NULL, certificate TEXT NOT NULL, serial TEXT NOT NULL,
            expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)""")
        store._exec("CREATE TABLE IF NOT EXISTS node_nonces(id TEXT PRIMARY KEY, expires REAL NOT NULL)")
        store._exec("CREATE TABLE IF NOT EXISTS node_revocations(node_id TEXT PRIMARY KEY, revoked REAL NOT NULL)")
        columns = {r["name"] for r in store._all("PRAGMA table_info(node_credentials)")}
        for name, kind in (("previous_serial", "TEXT"), ("previous_until", "REAL")):
            if name not in columns:
                store._exec(f"ALTER TABLE node_credentials ADD COLUMN {name} {kind}")

    def ca(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        key_path, cert_path = self.directory / "ca-key.pem", self.directory / "ca.pem"
        if not key_path.exists() and not cert_path.exists():
            key = ed25519.Ed25519PrivateKey.generate()
            now = datetime.now(timezone.utc)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Atlas node CA")])
            cert = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=3650)).add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True).add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False).add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True).sign(key, algorithm=None)
            # Конфигурация Gate имеет единственного владельца; права Windows задаёт установка.
            with key_path.open("xb") as f:
                f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            key_path.chmod(0o600)
            with cert_path.open("xb") as f:
                f.write(cert.public_bytes(serialization.Encoding.PEM))
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        if key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw) != cert.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw):
            raise ValueError("CA key/certificate mismatch")
        return key, cert

    def invite(self, node_id):
        node = self.nodes.config.get(node_id)
        if not node or not node.enabled:
            raise GateError(404, "not_found", "Router не одобрен в конфигурации Gate")
        token = secrets.token_urlsafe(32)
        self.store._exec("INSERT INTO node_invites VALUES (?,?,?,NULL,'invited',NULL)", (digest(token.encode()), node_id, time.time() + 900))
        return {"invitation": token, "expiresIn": 900, "nodeId": node_id}

    def invitation(self, token):
        row = self.store._one("SELECT * FROM node_invites WHERE token_hash=?", (digest(token.encode()),))
        if not row or row["expires"] < time.time() or row["state"] in ("revoked", "consumed"):
            raise GateError(401, "unauthorized", "приглашение недействительно")
        return row

    def enroll(self, token, pem):
        row = self.invitation(token)
        if row["state"] != "invited":
            if row["csr"] == pem:
                return {"nodeId": row["node_id"], "state": row["state"]}
            raise GateError(409, "conflict", "приглашение уже связано с другим ключом")
        try:
            csr = x509.load_pem_x509_csr(pem.encode())
            if not csr.is_signature_valid or not isinstance(csr.public_key(), ed25519.Ed25519PublicKey):
                raise ValueError("CSR signature/key")
        except (ValueError, TypeError):
            raise GateError(422, "invalid_request", "нужен подписанный Ed25519 CSR")
        self.store._exec("UPDATE node_invites SET csr=?,state='pending' WHERE token_hash=? AND state='invited'", (pem, row["token_hash"]))
        return {"nodeId": row["node_id"], "state": "pending"}

    def issue(self, node_id, pem, rotation=False):
        csr = x509.load_pem_x509_csr(pem.encode())
        public = csr.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        old = self.store._one("SELECT * FROM node_credentials WHERE node_id=?", (node_id,))
        if rotation and old and public == old["public_key"] and old["expires"] > time.time() + 29 * 86400:
            return old["certificate"]
        if rotation and old and public != old["public_key"]:
            raise GateError(422, "invalid_request", "смена ключа требует нового одобрения администратора")
        key, ca = self.ca()
        now = datetime.now(timezone.utc)
        cert = x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_id)])).issuer_name(ca.subject).public_key(csr.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=30)).add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True).add_extension(x509.SubjectKeyIdentifier.from_public_key(csr.public_key()), critical=False).add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.public_key()), critical=False).add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=True).add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier("spiffe://atlas.local/router/" + node_id)]), critical=False).sign(key, algorithm=None)
        certificate = cert.public_bytes(serialization.Encoding.PEM).decode()
        self.store._exec("INSERT INTO node_credentials(node_id,public_key,certificate,serial,expires,revoked) VALUES (?,?,?,?,?,0) ON CONFLICT(node_id) DO UPDATE SET public_key=excluded.public_key,certificate=excluded.certificate,serial=excluded.serial,expires=excluded.expires,revoked=0,previous_serial=NULL,previous_until=NULL", (node_id, public, certificate, str(cert.serial_number), cert.not_valid_after_utc.timestamp()))
        if rotation and old:
            self.store._exec("UPDATE node_credentials SET previous_serial=?,previous_until=? WHERE node_id=?", (old["serial"], time.time() + 3600, node_id))
        return certificate

    def approve(self, token_hash):
        row = self.store._one("SELECT * FROM node_invites WHERE token_hash=? AND state='pending' AND expires>?", (token_hash, time.time()))
        if not row or row["node_id"] not in self.nodes.config or not self.nodes.config[row["node_id"]].enabled:
            raise GateError(404, "not_found", "заявка недоступна")
        cert = self.issue(row["node_id"], row["csr"])
        self.store._exec("DELETE FROM node_revocations WHERE node_id=?", (row["node_id"],))
        self.store._exec("UPDATE node_invites SET state='approved',certificate=? WHERE token_hash=?", (cert, token_hash))
        return {"ok": True}

    def poll(self, token):
        row = self.invitation(token)
        out = {"nodeId": row["node_id"], "state": row["state"]}
        if row["state"] == "approved":
            _, ca = self.ca()
            out.update(certificate=row["certificate"], ca=ca.public_bytes(serialization.Encoding.PEM).decode(), audience=self.audience)
        return out

    def revoke(self, node_id):
        self.store._exec("INSERT INTO node_revocations VALUES (?,?) ON CONFLICT(node_id) DO UPDATE SET revoked=excluded.revoked", (node_id, time.time()))
        self.store._exec("UPDATE node_credentials SET revoked=1 WHERE node_id=?", (node_id,))
        self.store._exec("UPDATE node_invites SET state='revoked' WHERE node_id=?", (node_id,))
        self.nodes.snapshots.pop(node_id, None)

    async def authenticate(self, request):
        node_id = request.headers.get("x-atlas-node", "")
        node = self.nodes.config.get(node_id)
        saved = self.store._one("SELECT * FROM node_credentials WHERE node_id=? AND revoked=0 AND expires>?", (node_id, time.time()))
        if not node or not node.enabled or not saved:
            raise GateError(401, "unauthorized", "удостоверение Router недействительно")
        try:
            body = await request.body()
            target = request.url.path + ("?" + request.url.query if request.url.query else "")
            claims = jwt.decode(request.headers.get("x-atlas-proof", ""), saved["public_key"], algorithms=["EdDSA"], audience=self.audience, issuer=node_id, options={"require": ["iss", "sub", "aud", "iat", "exp", "jti", "request", "serial"]})
            serial_ok = claims["serial"] == saved["serial"] or (claims["serial"] == saved["previous_serial"] and (saved["previous_until"] or 0) > time.time())
            if claims["sub"] != node_id or not serial_ok or not 0 < claims["exp"] - claims["iat"] <= 60 or claims["request"] != digest(request.method.encode() + target.encode() + body):
                raise ValueError("request binding")
            if not isinstance(claims["jti"], str) or len(claims["jti"]) > 100:
                raise ValueError("nonce")
            self.store._exec("DELETE FROM node_nonces WHERE expires<?", (time.time(),))
            import sqlite3
            try:
                self.store._exec("INSERT INTO node_nonces VALUES (?,?)", (node_id + ":" + claims["jti"], claims["exp"]))
            except sqlite3.IntegrityError:
                raise ValueError("replayed proof")
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            raise GateError(401, "unauthorized", "proof Router отвергнут")
        return node_id
