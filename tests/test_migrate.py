import sqlite3
import json
import pytest
from atlas_gate.gate.crypto import Keyring, SigningKey
from atlas_gate.gate.store import Store
from atlas_gate.gate.routing import Routing
from atlas_gate.migrate import migrate
from .test_routing import FakeNodes, device


@pytest.mark.asyncio
async def test_wal_backup_preserves_owners_key_and_legacy_resume_node(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    orgs = source / "orgs"
    orgs.mkdir()
    (orgs / "org.json").write_text('{"id":"org"}')
    key = SigningKey.load_or_create(str(source / "key.pem"))
    db = Store(str(source / "gate.db"))
    encrypted = Keyring("same-keyring-secret").seal("test-device-secret")
    db._exec("INSERT INTO devices(device_id,org,user_id,user_email,user_name,device_name,platform,device_secret,created_at) VALUES ('owner','org','owner','','','test','test',?,0)", (encrypted,))
    db.agent_session_own("old-sdk", "owner")
    target = tmp_path / "new-gate"
    result = migrate(source / "gate.db", orgs, source / "key.pem", target, legacy_node="b")
    assert result["devices"] == 1 and result["legacyHistories"] == 1
    assert result["servicesChanged"] is False and result["sdkCredentialsCopied"] is False
    assert SigningKey.load_or_create(str(target / "signing-key.pem")).kid == key.kid
    copied = Store(str(target / "gate.db"))
    assert Keyring("same-keyring-secret").open(copied.device("owner")["device_secret"]) == "test-device-secret"
    routing = Routing(copied, FakeNodes(capacity=4))
    status, raw = await routing.create(device("owner"), "sub", "claude", {"resumeSessionId": "old-sdk"}, 10)
    assert status == 200
    assert routing.get(json.loads(raw)["id"], device("owner"))["node_id"] == "b"
    with pytest.raises(Exception) as denied:
        await routing.create(device("other"), "sub", "claude", {"resumeSessionId": "old-sdk"}, 10)
    assert denied.value.status == 404
    assert db.agent_session_owner("old-sdk") == "owner"
    copied.close()
    db.close()
    with pytest.raises(ValueError):
        migrate(source / "gate.db", orgs, source / "key.pem", target)
