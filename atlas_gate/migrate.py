"""Копия embedded Gate через SQLite backup; исходные файлы/службы не меняются."""
import argparse
import json
from pathlib import Path
import shutil
import sqlite3
from .gate.store import Store


def migrate(source_db, source_orgs, source_key, destination, legacy_node=None):
    source, orgs, key, target = map(lambda p: Path(p).resolve(), (source_db, source_orgs, source_key, destination))
    if target.exists():
        raise ValueError("destination должен быть новым каталогом; существующие данные не затираются")
    if not source.is_file() or not orgs.is_dir() or not key.is_file():
        raise ValueError("нужны существующие DB, каталог организаций и ключ подписи")
    if orgs == target or orgs in target.parents:
        raise ValueError("destination не может находиться внутри source-orgs")
    target.mkdir(parents=True, mode=0o700)
    # Backup API включает зафиксированный WAL; простое Copy-Item gate.db этого не делает.
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src, sqlite3.connect(target / "gate.db") as dst:
        if src.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("исходная SQLite повреждена")
        src.backup(dst)
        if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("копия SQLite не прошла проверку")
    shutil.copy2(key, target / "signing-key.pem")
    (target / "signing-key.pem").chmod(0o600)
    (target / "orgs").mkdir()
    for profile in orgs.rglob("*.json"):
        if profile.is_symlink() or orgs not in profile.resolve().parents:
            raise ValueError("внешние ссылки в source-orgs не переносятся")
        dest = target / "orgs" / profile.relative_to(orgs)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(profile, dest)
    store = Store(str(target / "gate.db"))
    try:
        store._exec("CREATE TABLE IF NOT EXISTS legacy_router_history(sdk_id TEXT PRIMARY KEY,node_id TEXT NOT NULL,device_id TEXT NOT NULL,org TEXT NOT NULL)")
        count = 0
        if legacy_node:
            count = store._exec("INSERT OR IGNORE INTO legacy_router_history SELECT s.sdk_session_id,?,s.device_id,d.org FROM agent_sessions s JOIN devices d USING(device_id)", (legacy_node,))
        devices = store._one("SELECT COUNT(*) n FROM devices")["n"]
        manifest = {"schema": 1, "devices": devices, "legacyHistories": count, "legacyNode": legacy_node,
                    "sdkCredentialsCopied": False, "servicesChanged": False}
        (target / "migration.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return manifest
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--source-orgs", required=True)
    parser.add_argument("--source-signing-key", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--legacy-node", help="только Router с прежним SDK profile/history")
    args = parser.parse_args()
    print(json.dumps(migrate(args.source_db, args.source_orgs, args.source_signing_key, args.destination, args.legacy_node), ensure_ascii=False))


if __name__ == "__main__":
    main()
