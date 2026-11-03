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


@pytest.mark.parametrize('extra,usage', [
    ({}, {'input_tokens': 10}),
    ({'usageIncomplete': True, 'usageSource': 'estimated'}, {'input_tokens': 10}),
    ({'usageIncomplete': 1, 'usageSource': 'completed_sdk_results'}, {'input_tokens': 10}),
    ({'usageIncomplete': True, 'usageSource': 'completed_sdk_results'}, {'input_tokens': True}),
    ({'usageIncomplete': True, 'usageSource': 'completed_sdk_results'}, {'input_tokens': -1}),
    ({'usageIncomplete': True, 'usageSource': 'completed_sdk_results'}, {'input_tokens': '10'}),
    ({'usageIncomplete': True, 'usageSource': 'completed_sdk_results'}, {'costUsd': 0.1}),
    ({'usageIncomplete': True, 'usageSource': 'completed_sdk_results'}, None),
])
def test_error_usage_requires_typed_completed_sdk_measurements(tmp_path, extra, usage):
    store = Store(str(tmp_path / 'usage.db'))
    try:
        record(store, 'unknown', usage, type='error', **extra)
        assert report(store)['summary']['records'] == 0
        assert store._one('SELECT COUNT(*) n FROM router_usage_receipts')['n'] == 0
    finally:
        store.close()


def test_known_partial_error_preserves_measures_and_incompleteness_after_restart(tmp_path):
    path = str(tmp_path / 'usage.db')
    store = Store(path)
    event = {'type': 'error', 'usageIncomplete': True, 'usageSource': 'completed_sdk_results',
             'outcomeUnknown': True, 'costUsd': 42}
    record(store, 'partial', {'input_tokens': 10, 'output_tokens': 5}, **event)
    store.close()
    store = Store(path)
    try:
        record(store, 'partial', {'input_tokens': 10, 'output_tokens': 5}, **event)
        data = report(store)
        row = data['rows'][0]
        assert data['summary']['records'] == 1 and data['summary']['incompleteRecords'] == 1
        assert row['uncachedInputTokens'] == 10 and row['outputTokens'] == 5
        assert row['totalInputTokens'] is None and row['cacheReadTokens'] is None
        assert row['usageIncomplete'] is True and row['usageSource'] == 'completed_sdk_results'
        assert row['status'] == 'unknown'
        assert store._one('SELECT cost FROM usage')['cost'] is None
    finally:
        store.close()


def test_partial_receipt_details_and_accounted_flag_commit_together(tmp_path):
    from atlas_gate.gate.operations import Operations
    store = Store(str(tmp_path / 'usage.db'))
    device = SimpleNamespace(org='first', user_id='user', device_id='device')
    binding = {'id': 'session', 'model': 'native', 'route': 'sub', 'node_id': 'node', 'boot_id': 'boot'}
    try:
        operation, _ = Operations(store).begin(device, binding, 'POST', '/v1/sessions/s/prompt',
            {'id': 'partial'}, 'original', turn_id='partial')
        event = {'type': 'error', 'usage': {'input_tokens': 10, 'output_tokens': 5},
                 'usageIncomplete': True, 'usageSource': 'completed_sdk_results'}
        store._exec("CREATE TRIGGER break_details BEFORE INSERT ON agent_usage_details BEGIN SELECT RAISE(ABORT, 'test'); END")
        routing = Routing(store, None)
        with pytest.raises(Exception):
            routing.usage(binding, 'partial', event, device)
        assert store._one('SELECT accounted FROM router_operations WHERE id=?', (operation['id'],))['accounted'] == 0
        assert store._one('SELECT COUNT(*) n FROM router_usage_receipts')['n'] == 0
        store._exec('DROP TRIGGER break_details')
        routing.usage(binding, 'partial', event, device)
        routing.usage(binding, 'partial', event, device)
        assert store._one('SELECT finished,accounted FROM router_operations WHERE id=?',
                          (operation['id'],)) == {'finished': 1, 'accounted': 1}
        assert store._one('SELECT COUNT(*) n FROM usage')['n'] == 1
    finally:
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
