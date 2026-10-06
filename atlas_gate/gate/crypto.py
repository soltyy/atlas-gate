"""Криптография гейта: ключ подписи профиля (Ed25519, JWS EdDSA), шифрование секретов в БД, токены."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

import jwt
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

log = logging.getLogger("atlas_gate")

# user_code: 8 знаков без двусмысленных 0/O/1/I.
USER_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sha256_hex(data: bytes | str) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def new_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def new_user_code() -> str:
    return "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(8))


def canonical_json(obj: Any) -> bytes:
    """Каноническая форма для подписи HMAC аудита: ключи по алфавиту, без пробелов, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def hmac_hex(secret: str, obj: Any) -> str:
    return hmac.new(secret.encode("utf-8"), canonical_json(obj), hashlib.sha256).hexdigest()


def _restrict_acl(path: Path) -> None:
    """Файл ключа — только учётной записи службы (Windows ACL); на POSIX — 0600."""
    try:
        if sys.platform == "win32":
            # По SID, а не по имени: на русской Windows `whoami` у LocalSystem отвечает
            # «nt authority\система», и icacls такое имя не принимал (выкатка 28.09.2026).
            row = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True,
                                 timeout=10).stdout.strip()
            sid = row.rsplit(",", 1)[-1].strip().strip('"') if row else ""
            if not sid.startswith("S-1-"):
                raise RuntimeError(f"SID учётной записи не получен: {row!r}")
            subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:F", "*S-1-5-32-544:F"],
                           capture_output=True, text=True, timeout=10, check=True)
        else:
            os.chmod(path, 0o600)
    except Exception as failed:  # noqa: BLE001 — ключ создан; права — предупреждение, не отказ
        log.warning("права на %s не ограничены: %s", path, failed)


class SigningKey:
    """Ed25519: приватный ключ в PEM на диске, `kid` = sha256 сырого публичного ключа."""

    def __init__(self, private: Ed25519PrivateKey) -> None:
        self.private = private
        self.public: Ed25519PublicKey = private.public_key()
        raw = self.public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.kid = sha256_hex(raw)
        self._raw_public = raw

    @classmethod
    def load_or_create(cls, path: str) -> "SigningKey":
        p = Path(path)
        if p.is_file():
            key = serialization.load_pem_private_key(p.read_bytes(), password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise RuntimeError(f"{p}: ключ подписи профиля обязан быть Ed25519")
            return cls(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        key = Ed25519PrivateKey.generate()
        p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
        _restrict_acl(p)
        log.info("создан ключ подписи профиля %s", p)
        return cls(key)

    def jwk(self) -> dict[str, str]:
        return {"kty": "OKP", "crv": "Ed25519", "x": b64url(self._raw_public), "kid": self.kid,
                "alg": "EdDSA", "use": "sig"}

    def sign(self, payload: dict[str, Any]) -> str:
        return jwt.encode(payload, self.private, algorithm="EdDSA",
                          headers={"kid": self.kid, "typ": "harness-profile+jws"})


def verify_profile(jws: str, jwk: dict[str, str]) -> dict[str, Any]:
    """Проверка подписи профиля публичным ключом из ответа poll — то, что делает клиент."""
    public = Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(jwk["x"] + "=" * (-len(jwk["x"]) % 4)))
    header = jwt.get_unverified_header(jws)
    if header.get("kid") != jwk["kid"]:
        raise jwt.InvalidSignatureError("kid профиля не совпадает с ключом")
    return jwt.decode(jws, public, algorithms=["EdDSA"], options={"verify_exp": False, "verify_iat": False})


class Keyring:
    """Шифрование device_secret в БД; ключ Fernet выводится из секрета окружения."""

    def __init__(self, secret: str) -> None:
        if not secret:
            raise RuntimeError("ATLAS_GATE_KEYRING_SECRET пуст — device_secret нечем шифровать")
        self._f = Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest()))

    def seal(self, value: str) -> str:
        return self._f.encrypt(value.encode("utf-8")).decode("ascii")

    def open(self, value: str) -> str:
        try:
            return self._f.decrypt(value.encode("ascii")).decode("utf-8")
        except InvalidToken as bad:
            raise RuntimeError("секрет в БД гейта не расшифрован: сменился ATLAS_GATE_KEYRING_SECRET?") from bad
