"""Atomic managed API admission fences; temporary SQLite and inert HTTP only."""
import asyncio
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest

from atlas_gate.gate.api_attempts import ApiAttempts, api_attempt_contract
from atlas_gate.gate.errors import GateError
from atlas_gate.gate.operations import body_sha256
from atlas_gate.gate.store import Store
from .test_api_attempts import managed, configured, provider_available, provider, completion, BODY, HEADERS, CHAT


@pytest.fixture
def ledger(tmp_path):
    store = Store(str(tmp_path / 'fences.db'))
    manager = ApiAttempts(store)
    device = SimpleNamespace(org='org', device_id='device', user_id='user')
    route = SimpleNamespace(id='route')
    yield manager, device, route
    store.close()


def seal(manager, device, route, *, key='original', model='model', suffix='/responses', body=None):
    return manager.reconcile(device, route, model, suffix, key, body_sha256(body or {'model': model}))


def claim(manager, device, route, *, key='original', model='model', suffix='/responses', body=None, headers=None):
    return manager.claim(device, route, model, suffix, json.dumps(body or {'model': model}).encode(),
                         httpx.Headers({'Idempotency-Key': key, **(headers or {})}))


def test_absent_reconcile_is_stable_negative_receipt_and_late_claim_is_fenced(ledger):
    manager, device, route = ledger
    first = seal(manager, device, route)
    row = manager.owned(first['attemptId'], device)
    raw = manager.replay(row, device).body
    assert first['state'] == 'rejected_not_sent' and first['outcome'] == 'sealed-not-sent'
    assert first['notSentProven'] and first['providerDispatch'] is False
    assert first['scope'] == 'managed_api_admission_fence'
    assert first['bodySha256'] == body_sha256({'model': 'model'})
    assert first['resultSha256'] == hashlib.sha256(raw).hexdigest()
    assert seal(manager, device, route) == first
    # Reconcile cannot know the original optional transport/semantic headers.
    saved, fresh = claim(manager, device, route, headers={
        'Anthropic-Beta': 'unknown-original', 'X-Atlas-Conversation-Id': 'chat',
        'X-Atlas-Branch-Id': 'branch', 'X-Atlas-Choice-Index': 'invalid-but-never-dispatched'})
    assert not fresh and saved['id'] == first['attemptId']
    assert manager.replay(saved, device).body == raw
    assert manager.dispatched(saved['id']) is False
    assert not manager.store._all('SELECT * FROM usage')


def test_reconcile_seals_prepared_attempt_before_queued_producer_can_dispatch(ledger):
    manager, device, route = ledger
    row, fresh = claim(manager, device, route)
    assert fresh
    saved = seal(manager, device, route)
    assert saved['attemptId'] == row['id'] and saved['notSentProven']
    before = manager.owned(row['id'], device)
    raw = manager.replay(before, device).body
    assert manager.dispatched(row['id']) is False
    manager.add_response_headers(row['id'], {'X-Atlas-Configured-Reasoning': 'late'})
    assert manager.owned(row['id'], device) == before
    assert manager.replay(before, device).body == raw


@pytest.mark.parametrize('state', ['running', 'outcome_unknown', 'complete'])
def test_existing_dispatched_attempt_is_observed_never_sealed(ledger, state):
    manager, device, route = ledger
    row, _ = claim(manager, device, route)
    assert manager.dispatched(row['id'])
    if state != 'running':
        manager.response_headers(row['id'], 200, {'content-type': 'application/json'})
        manager.append(row['id'], b'{"saved":true}')
        manager.finish(row['id'], {'outcome': 'completed' if state == 'complete' else 'unknown',
                                  'terminalKnown': state == 'complete'}, state=state)
    before = manager.owned(row['id'], device)
    observed = seal(manager, device, route)
    assert observed['attemptId'] == row['id'] and observed['state'] == state
    assert not observed.get('notSentProven', False)
    assert manager.owned(row['id'], device) == before


@pytest.mark.parametrize('changed', ['route', 'model', 'suffix', 'body'])
def test_fence_conflicts_on_changed_exact_target_or_body(ledger, changed):
    manager, device, route = ledger
    original = seal(manager, device, route)
    arguments = dict(key='original', model='model', suffix='/responses', body={'model': 'model'})
    if changed == 'route':
        route = SimpleNamespace(id='other-route')
    elif changed == 'model':
        arguments['model'] = 'other'
    elif changed == 'suffix':
        arguments['suffix'] = '/chat/completions'
    else:
        arguments['body'] = {'model': 'model', 'input': 'changed'}
    with pytest.raises(GateError) as caught:
        claim(manager, device, route, **arguments)
    assert caught.value.code == 'idempotency_conflict'
    assert manager.snapshot(original['attemptId'], device)['notSentProven']


def test_fence_survives_restart_and_is_owner_scoped(tmp_path):
    path = str(tmp_path / 'fences.db')
    device = SimpleNamespace(org='org', device_id='device', user_id='user')
    route = SimpleNamespace(id='route')
    first_store = Store(path)
    first = seal(ApiAttempts(first_store), device, route)
    first_store.close()
    reopened = Store(path)
    try:
        manager = ApiAttempts(reopened)
        assert seal(manager, device, route) == first
        assert claim(manager, device, route)[1] is False
        other = SimpleNamespace(org='org', device_id='other', user_id='user')
        with pytest.raises(GateError) as caught:
            manager.owned(first['attemptId'], other)
        assert caught.value.status == 404
        assert claim(manager, other, route)[1] is True
    finally:
        reopened.close()


def test_legacy_missing_original_body_hash_is_not_backfilled_or_sealed(ledger):
    manager, device, route = ledger
    row, _ = claim(manager, device, route)
    manager.store._exec('UPDATE api_attempts SET body_sha256=NULL WHERE id=?', (row['id'],))
    observed = seal(manager, device, route)
    assert observed['state'] == 'prepared' and observed['identityConfirmed'] is False
    assert not observed.get('notSentProven') and observed['bodySha256'] is None


def test_claim_dispatch_vs_reconcile_race_has_one_atomic_winner(ledger):
    manager, device, route = ledger
    for index in range(16):
        start = threading.Barrier(2)
        key = 'race-' + str(index)
        def request():
            start.wait()
            row, fresh = claim(manager, device, route, key=key)
            sent = fresh and manager.dispatched(row['id'])
            return row['id'], bool(sent)
        def reconcile():
            start.wait()
            return seal(manager, device, route, key=key)
        with ThreadPoolExecutor(max_workers=2) as pool:
            attempt = pool.submit(request)
            negative = pool.submit(reconcile)
            aid, sent = attempt.result(timeout=3)
            observed = negative.result(timeout=3)
        assert aid == observed['attemptId']
        if observed.get('notSentProven'):
            assert not sent and manager.owned(aid, device)['state'] == 'rejected_not_sent'
        else:
            assert sent and manager.owned(aid, device)['state'] == 'running'


async def test_late_original_managed_post_after_reconcile_never_calls_provider(managed):
    client, state, creds = managed
    calls = []
    await provider(state, lambda request: calls.append(request) or httpx.Response(200, json=completion()))
    route = state.orgs['first'].route('private-api')
    # The root HTTP endpoint supplies authenticated Device; use its real stored owner here.
    attempt_owner = state.store.device(creds['device_id'])
    device = SimpleNamespace(org=attempt_owner['org'], device_id=attempt_owner['device_id'], user_id=attempt_owner['user_id'])
    saved = state.api_attempts.reconcile(device, route, 'api-model', '/chat/completions',
                                         HEADERS['Idempotency-Key'], body_sha256(BODY))
    response = await client.post(CHAT, json=BODY, headers={**HEADERS, 'Anthropic-Beta': 'late-original'})
    assert response.status_code == 409 and response.json()['admissionFence']['notSentProven']
    assert response.headers['X-Atlas-API-Attempt-Id'] == saved['attemptId']
    assert not calls
    assert api_attempt_contract()['admissionFence']['protectsLegacy'] is False


async def test_reconcile_queued_real_gate_producer_preserves_negative_bytes(managed):
    client, state, creds = managed
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    await provider(state, lambda request: calls.append(request) or httpx.Response(200, json=completion()))
    original_start = state.api_attempts.start
    def paused(coro):
        async def queued():
            entered.set()
            await release.wait()
            await coro
        return original_start(queued())
    state.api_attempts.start = paused
    request = asyncio.create_task(client.post(CHAT, json=BODY, headers=HEADERS))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        owner = state.store.device(creds['device_id'])
        device = SimpleNamespace(org=owner['org'], device_id=owner['device_id'], user_id=owner['user_id'])
        saved = state.api_attempts.reconcile(device, state.orgs['first'].route('private-api'), 'api-model',
                                             '/chat/completions', HEADERS['Idempotency-Key'], body_sha256(BODY))
        before = state.api_attempts.owned(saved['attemptId'], device)
        raw = state.api_attempts.replay(before, device).body
    finally:
        release.set()
    result = await asyncio.wait_for(request, 3)
    assert result.status_code == 409 and result.content == raw
    assert state.api_attempts.owned(saved['attemptId'], device) == before
    assert not calls


def reconcile_body(**changes):
    return {'version': 1, 'requestId': HEADERS['Idempotency-Key'], 'routeId': 'private-api',
            'model': BODY['model'], 'endpoint': '/chat/completions', 'bodySha256': body_sha256(BODY), **changes}


async def test_public_fence_lookup_and_late_post_use_one_stable_negative_receipt(managed):
    client, state, _ = managed
    calls = []
    await provider(state, lambda request: calls.append(request) or httpx.Response(200, json=completion()))
    first = await client.post('/harness/llm/attempts/reconcile', json=reconcile_body(), headers=HEADERS)
    assert first.status_code == 200, first.text
    saved = first.json()
    assert saved['notSentProven'] and saved['providerDispatch'] is False
    assert first.headers['Cache-Control'] == 'private, no-store'
    assert (await client.post('/harness/llm/attempts/reconcile', json=reconcile_body(), headers=HEADERS)).json() == saved
    read = await client.get('/harness/llm/attempts/lookup', params={
        'requestId': HEADERS['Idempotency-Key'], 'route_id': 'private-api', 'model': BODY['model'],
        'bodySha256': body_sha256(BODY)})
    assert read.status_code == 200 and read.json() == saved
    late = await client.post(CHAT, json=BODY, headers=HEADERS)
    assert late.status_code == 409 and late.json()['admissionFence']['attemptId'] == saved['attemptId']
    assert not calls and not state.store._all("SELECT * FROM usage WHERE kind='gateway'")


async def test_public_fence_requires_contract_and_exact_endpoint_and_rejects_conflicts(managed):
    client, state, _ = managed
    def forbidden(request):
        raise AssertionError('reconcile cannot invoke provider')
    await provider(state, forbidden)
    unnegotiated = await client.post('/harness/llm/attempts/reconcile', json=reconcile_body())
    assert unnegotiated.status_code == 422
    body = reconcile_body()
    body.pop('endpoint')
    assert (await client.post('/harness/llm/attempts/reconcile', json=body, headers=HEADERS)).status_code == 422
    first = await client.post('/harness/llm/attempts/reconcile', json=reconcile_body(), headers=HEADERS)
    assert first.status_code == 200
    changed = await client.post('/harness/llm/attempts/reconcile',
        json=reconcile_body(endpoint='/responses'), headers=HEADERS)
    assert changed.status_code == 409 and changed.json()['error']['code'] == 'idempotency_conflict'
    bad_hash = await client.post('/harness/llm/attempts/reconcile',
        json=reconcile_body(bodySha256='a' * 64), headers=HEADERS)
    assert bad_hash.status_code == 409
    assert state.store._one('SELECT COUNT(*) n FROM api_attempts')['n'] == 1
