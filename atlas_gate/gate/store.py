"""Хранилище гейта — SQLite из стандартной библиотеки, все записи через этот модуль.

Токены хранятся только хэшами sha256; сравнение — постоянное по времени (`hmac.compare_digest`
при проверке хэша не нужно: поиск идёт по хэшу-ключу, сам секрет в БД не лежит). Virtual key
device_secret хранится зашифрованным (`crypto.Keyring`).

Один процесс, одна нить событий: соединение одно, `check_same_thread=False`, запись под замком
потока — асинхронные обработчики не держат его дольше одной транзакции.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
# Миграции «вперёд»: версия → SQL. Колонки не удаляются (GW-DIRECT-01: devices.litellm_key* остаются пустыми).
MIGRATIONS = {
    2: [
        "ALTER TABLE usage ADD COLUMN route TEXT",
        "ALTER TABLE usage ADD COLUMN session_id TEXT",
        "ALTER TABLE usage ADD COLUMN model_call TEXT",
        "CREATE INDEX IF NOT EXISTS usage_user_ts ON usage(user_id, ts)",
    ],
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS enrollments (
    device_code TEXT PRIMARY KEY,
    user_code TEXT NOT NULL UNIQUE,
    device_name TEXT NOT NULL,
    platform TEXT NOT NULL,
    app_version TEXT NOT NULL,
    status TEXT NOT NULL,              -- pending | approved | denied | expired | consumed
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    user_json TEXT,
    org TEXT,
    device_id TEXT
);
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    org TEXT NOT NULL,
    user_id TEXT NOT NULL,
    user_email TEXT NOT NULL,
    user_name TEXT NOT NULL,
    device_name TEXT NOT NULL,
    platform TEXT NOT NULL,
    app_version TEXT NOT NULL DEFAULT '',
    device_secret TEXT NOT NULL,       -- зашифрован
    litellm_key TEXT,                  -- не используется с v0.3.0 (LiteLLM снят), колонка оставлена
    litellm_key_id TEXT,
    created_at REAL NOT NULL,
    revoked_at REAL
);
CREATE TABLE IF NOT EXISTS tokens (
    device_id TEXT PRIMARY KEY,
    device_token_hash TEXT NOT NULL UNIQUE,
    expires_at REAL NOT NULL,
    refresh_token_hash TEXT NOT NULL UNIQUE,
    refresh_expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    device_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    received_at REAL NOT NULL,
    event_json TEXT NOT NULL,
    PRIMARY KEY (device_id, session_id, seq)
);
CREATE TABLE IF NOT EXISTS usage (
    org TEXT NOT NULL,
    user_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    kind TEXT NOT NULL,                -- gateway | agent
    model TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    cached_tokens INTEGER,
    cost REAL,
    ts REAL NOT NULL,
    route TEXT,
    session_id TEXT,
    model_call TEXT
);
CREATE TABLE IF NOT EXISTS agent_turns (
    user_id TEXT NOT NULL,
    day TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (user_id, day)
);
CREATE TABLE IF NOT EXISTS agent_sessions (
    sdk_session_id TEXT PRIMARY KEY,   -- чья сессия SDK: resumeSessionId только своей
    device_id TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS profiles (
    org TEXT PRIMARY KEY,
    profile_version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    jws TEXT NOT NULL,
    etag TEXT NOT NULL,
    issued_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS enrollments_user_code ON enrollments(user_code);
CREATE INDEX IF NOT EXISTS usage_user_ts ON usage(user_id, ts);
"""


class Store:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
            row = self._db.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                self._db.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
            elif row["version"] > SCHEMA_VERSION:
                raise RuntimeError(f"схема БД гейта {row['version']} новее кода ({SCHEMA_VERSION})")
            else:
                for version in range(row["version"] + 1, SCHEMA_VERSION + 1):
                    for sql in MIGRATIONS.get(version, []):
                        self._db.execute(sql)
                    self._db.execute("UPDATE schema_version SET version=?", (version,))

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _one(self, sql: str, args: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(sql, args).fetchone()
        return dict(row) if row is not None else None

    def _all(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def _exec(self, sql: str, args: tuple[Any, ...] = ()) -> int:
        with self._lock:
            return self._db.execute(sql, args).rowcount

    # --- регистрации --------------------------------------------------------------------------

    def enrollment_create(self, device_code: str, user_code: str, device_name: str, platform: str,
                          app_version: str, ttl: float) -> None:
        now = time.time()
        self._exec(
            "INSERT INTO enrollments(device_code,user_code,device_name,platform,app_version,status,created_at,expires_at)"
            " VALUES (?,?,?,?,?,'pending',?,?)",
            (device_code, user_code, device_name, platform, app_version, now, now + ttl))

    def enrollment_by_device_code(self, device_code: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM enrollments WHERE device_code=?", (device_code,))

    def enrollment_by_user_code(self, user_code: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM enrollments WHERE user_code=?", (user_code,))

    def enrollments(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            return self._all("SELECT * FROM enrollments WHERE status=? ORDER BY created_at", (status,))
        return self._all("SELECT * FROM enrollments ORDER BY created_at")

    def enrollment_set(self, device_code: str, status: str, *, user: dict[str, Any] | None = None,
                       org: str | None = None, device_id: str | None = None) -> None:
        self._exec("UPDATE enrollments SET status=?, user_json=COALESCE(?,user_json), org=COALESCE(?,org),"
                   " device_id=COALESCE(?,device_id) WHERE device_code=?",
                   (status, json.dumps(user, ensure_ascii=False) if user else None, org, device_id, device_code))

    def enrollment_consume(self, device_code: str) -> bool:
        """approved → consumed ровно один раз (ответ poll с токенами одноразовый)."""
        return self._exec("UPDATE enrollments SET status='consumed' WHERE device_code=? AND status='approved'",
                          (device_code,)) == 1

    # --- устройства ---------------------------------------------------------------------------

    def device_create(self, row: dict[str, Any]) -> None:
        cols = ",".join(row)
        self._exec(f"INSERT INTO devices({cols}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))

    def device(self, device_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM devices WHERE device_id=?", (device_id,))

    def devices(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM devices ORDER BY created_at")

    def device_revoke(self, device_id: str) -> bool:
        return self._exec("UPDATE devices SET revoked_at=? WHERE device_id=? AND revoked_at IS NULL",
                          (time.time(), device_id)) == 1

    # --- токены -------------------------------------------------------------------------------

    def tokens_set(self, device_id: str, token_hash: str, expires_at: float,
                   refresh_hash: str, refresh_expires_at: float) -> None:
        self._exec("INSERT INTO tokens(device_id,device_token_hash,expires_at,refresh_token_hash,refresh_expires_at)"
                   " VALUES (?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET device_token_hash=excluded.device_token_hash,"
                   " expires_at=excluded.expires_at, refresh_token_hash=excluded.refresh_token_hash,"
                   " refresh_expires_at=excluded.refresh_expires_at",
                   (device_id, token_hash, expires_at, refresh_hash, refresh_expires_at))

    def token_by_hash(self, token_hash: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM tokens WHERE device_token_hash=?", (token_hash,))

    def token_by_refresh_hash(self, refresh_hash: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM tokens WHERE refresh_token_hash=?", (refresh_hash,))

    # --- аудит --------------------------------------------------------------------------------

    def audit_add(self, device_id: str, session_id: str, events: list[tuple[int, str]]) -> None:
        now = time.time()
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._db.executemany(
                    "INSERT OR IGNORE INTO audit(device_id,session_id,seq,received_at,event_json) VALUES (?,?,?,?,?)",
                    [(device_id, session_id, seq, now, body) for seq, body in events])
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def audit_max_seq(self, device_id: str, session_id: str) -> int:
        row = self._one("SELECT MAX(seq) AS m FROM audit WHERE device_id=? AND session_id=?", (device_id, session_id))
        return int(row["m"]) if row and row["m"] is not None else 0

    def audit_events(self, device_id: str, session_id: str) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM audit WHERE device_id=? AND session_id=? ORDER BY seq", (device_id, session_id))

    # --- учёт ---------------------------------------------------------------------------------

    def usage_add(self, **row: Any) -> None:
        row.setdefault("ts", time.time())
        cols = ",".join(row)
        self._exec(f"INSERT INTO usage({cols}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))

    def usage(self, device_id: str | None = None) -> list[dict[str, Any]]:
        if device_id:
            return self._all("SELECT * FROM usage WHERE device_id=? ORDER BY ts", (device_id,))
        return self._all("SELECT * FROM usage ORDER BY ts")

    def usage_groups(self, org: str) -> list[dict[str, Any]]:
        """Вызовы организации по (маршрут, вид, модель). Маршрут пуст у ходов на подписке (kind=agent)
        и у расходов, импортированных из LiteLLM — их относит к маршруту вызывающий, по модели."""
        return self._all("SELECT route, kind, model, COUNT(*) AS n FROM usage WHERE org=? GROUP BY route, kind, model",
                         (org,))

    def spend(self, user_id: str, since: float, until: float) -> float:
        """Сумма стоимости вызовов gateway пользователя за период (cost null — не считается)."""
        row = self._one("SELECT COALESCE(SUM(cost), 0) AS s FROM usage WHERE user_id=? AND kind='gateway'"
                        " AND ts>=? AND ts<?", (user_id, since, until))
        return float(row["s"]) if row else 0.0

    def schema_version(self) -> int:
        row = self._one("SELECT version FROM schema_version")
        return int(row["version"]) if row else 0

    def agent_turns(self, user_id: str, day: str) -> int:
        row = self._one("SELECT count FROM agent_turns WHERE user_id=? AND day=?", (user_id, day))
        return int(row["count"]) if row else 0

    def agent_turns_inc(self, user_id: str, day: str) -> int:
        self._exec("INSERT INTO agent_turns(user_id,day,count) VALUES (?,?,1)"
                   " ON CONFLICT(user_id,day) DO UPDATE SET count=count+1", (user_id, day))
        return self.agent_turns(user_id, day)

    def agent_turns_reserve(self, user_id: str, day: str, limit: int | None) -> bool:
        """Атомарно занять единицу лимита; непринятый prompt обязан вернуть её."""
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO agent_turns(user_id,day,count) SELECT ?,?,1 WHERE ? IS NULL OR ?>0"
                " ON CONFLICT(user_id,day) DO UPDATE SET count=count+1 WHERE ? IS NULL OR count<?",
                (user_id, day, limit, limit, limit, limit))
            return cursor.rowcount == 1

    def agent_turns_release(self, user_id: str, day: str) -> None:
        self._exec("UPDATE agent_turns SET count=count-1 WHERE user_id=? AND day=? AND count>0",
                   (user_id, day))

    def agent_session_own(self, sdk_session_id: str, device_id: str) -> None:
        self._exec("INSERT OR IGNORE INTO agent_sessions(sdk_session_id,device_id,created_at) VALUES (?,?,?)",
                   (sdk_session_id, device_id, time.time()))

    def agent_session_owner(self, sdk_session_id: str) -> str | None:
        row = self._one("SELECT device_id FROM agent_sessions WHERE sdk_session_id=?", (sdk_session_id,))
        return row["device_id"] if row else None

    # --- профили ------------------------------------------------------------------------------

    def profile(self, org: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM profiles WHERE org=?", (org,))

    def profile_put(self, org: str, version: int, content_hash: str, jws: str, etag: str, issued_at: float) -> None:
        self._exec("INSERT INTO profiles(org,profile_version,content_hash,jws,etag,issued_at) VALUES (?,?,?,?,?,?)"
                   " ON CONFLICT(org) DO UPDATE SET profile_version=excluded.profile_version,"
                   " content_hash=excluded.content_hash, jws=excluded.jws, etag=excluded.etag,"
                   " issued_at=excluded.issued_at", (org, version, content_hash, jws, etag, issued_at))
