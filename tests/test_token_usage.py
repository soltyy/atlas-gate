import json
import time
from types import SimpleNamespace

import pytest
from atlas_gate.gate.routing import Routing
from atlas_gate.gate.store import Store
from atlas_gate.gate.token_usage import sdk_usage, usage_report
from .test_discovery import configured, enroll


def record(store, turn, usage, **extra):
    Routing(store, None).usage({"id": "session", "model": "gpt-6.1-sol", "route": "sub"}, turn,
        {"usage": usage, **extra}, SimpleNamespace(org="first", user_id="user", device_id="device"))


def report(store, **kw):
    return usage_report(store, since=0, until=time.time() + 10, **kw)


@pytest.mark.parametrize("invalid", [None, -1, True, "100", 1.5])
def test_missing_and_invalid_values_are_not_zero(invalid):
    detail = sdk_usage({"usage": {"input_tokens": 0, "output_tokens": invalid}})
    assert detail["uncachedInputTokens"] == 0
    assert detail["outputTokens"] is None
    assert detail["cacheReadTokens"] is None and detail["totalInputTokens"] is None


def test_sdk_details_dedup_restart_and_model_breakdown(tmp_path):
    path = str(tmp_path / "usage.db")
    store = Store(path)
    usage = {"input_tokens": 10, "cache_read_input_tokens": 90, "cache_creation_input_tokens": 20, "output_tokens": 5}
    record(store, "claude", usage, durationMs=3000, ok=True,
           modelUsage={"opus": {"inputTokens": 10, "outputTokens": 5, "costUSD": 99, "secret": "hidden"}})
    record(store, "claude", usage)
    store.close()
    store = Store(path)
    record(store, "claude", usage)
    # Canonical Codex aggregate supplied by Router; no additional modelUsage to sum.
    record(store, "codex", {"input_tokens": 100, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 0}, ok=False)
    data = report(store, limit=1)
    assert data["summary"]["records"] == 2
    assert data["summary"]["totalInputTokens"] == 220
    assert data["summary"]["cacheWriteTokens"] == 20
    assert data["summary"]["cacheReadPercent"] == pytest.approx(90 / 220 * 100)
    assert data["summary"]["cacheComparableRecords"] == 2
    assert data["rows"][0]["status"] == "failed"
    second = report(store, limit=1, before=data["nextBefore"])
    assert second["summary"] == data["summary"]
    row = second["rows"][0]
    assert row["durationMs"] == 3000 and row["status"] == "success"
    assert row["modelUsage"]["opus"]["inputTokens"] == 10
    assert "hidden" not in json.dumps(data) + json.dumps(second)
    assert second["nextBefore"] is None
    store.close()


def test_legacy_unknown_coverage_and_filters(tmp_path):
    store = Store(str(tmp_path / "usage.db"))
    store.usage_add(**{"org": "first", "device_id": "device", "user_id": "u", "kind": "agent", "model": "opus",
                     "prompt_tokens": 100, "completion_tokens": 4, "cached_tokens": 0})
    record(store, "missing", {})
    data = report(store)
    assert data["summary"]["totalInputTokens"] == 100
    assert data["summary"]["totalInputTokensRecords"] == 1
    assert data["summary"]["cacheReadTokens"] is None
    assert data["summary"]["cacheReadPercent"] is None
    assert data["summary"]["legacyRecords"] == 1
    assert len(data["groups"]) == 2
    assert report(store, model="opus")["summary"]["records"] == 1
    assert report(store, model="opus' OR 1=1 --")["summary"]["records"] == 0
    assert report(store, org="other")["rows"] == []
    assert report(store, device="other")["rows"] == []
    assert report(store, kind="gateway")["rows"] == []
    store.close()


def test_receipt_and_details_are_one_transaction(tmp_path):
    store = Store(str(tmp_path / "usage.db"))
    store._exec("CREATE TRIGGER break_details BEFORE INSERT ON agent_usage_details BEGIN SELECT RAISE(ABORT, 'test'); END")
    with pytest.raises(Exception):
        record(store, "turn", {})
    assert store._one("SELECT COUNT(*) n FROM usage")["n"] == 0
    assert store._one("SELECT COUNT(*) n FROM router_usage_receipts")["n"] == 0
    store.close()


@pytest.mark.asyncio
async def test_admin_api_authorization_validation_and_filters(configured):
    http, state, _ = configured
    record(state.store, "one", {"input_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 0})
    assert (await http.get('/harness/admin/usage')).status_code == 401
    credentials = await enroll(http)
    assert (await http.get('/harness/admin/usage', headers={'X-Atlas-Token': credentials['device_token']})).status_code == 401
    admin = {'X-Atlas-Token': 'test-admin-secret'}
    data = (await http.get('/harness/admin/usage?model=gpt-6.1-sol', headers=admin)).json()
    assert data['summary']['records'] == 1 and data['summary']['cacheReadTokens'] == 0
    assert (await http.get('/harness/admin/usage?model=other', headers=admin)).json()['rows'] == []
    for query in ('since=4&until=3', 'since=nan', 'until=inf', 'limit=0', 'limit=201', 'before=-1'):
        assert (await http.get('/harness/admin/usage?' + query, headers=admin)).status_code in (400, 422)
