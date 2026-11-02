"""Real Gate HTTP + temporary SQLite + inert MockTransport, never a provider account."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from atlas_gate.gate.api_attempts import ApiAttempts, ResponseFacts
from atlas_gate.gate.errors import GateError
from atlas_gate.gate.store import Store
from .test_discovery import configured, enroll


@pytest.fixture(autouse=True)
def provider_available(monkeypatch):
    monkeypatch.setenv('PRIVATE_KEY_NAME', 'inert-key')
    async def probe(self, route, key):
        return 'ok'
    monkeypatch.setattr('atlas_gate.gate.state.GateState._probe', probe)


@pytest.fixture
async def managed(configured, monkeypatch):
    client, state, _ = configured
    if not hasattr(state, 'api_attempts'):
        state.api_attempts = ApiAttempts(state.store)
    creds = await enroll(client)
    client.headers['Authorization'] = 'Bearer ' + creds['device_token']
    monkeypatch.setattr(state, 'key_for', lambda route: 'provider-secret')
    yield client, state, creds
    await state.api_attempts.aclose()


HEADERS = {'X-Atlas-API-Attempt-Contract': '1', 'Idempotency-Key': 'attempt-one'}
BODY = {'model': 'api-model', 'messages': [{'role': 'user', 'content': 'inert'}]}
CHAT = '/harness/llm/v1/chat/completions'
USAGE = {'prompt_tokens': 7, 'completion_tokens': 3, 'prompt_tokens_details': {'cached_tokens': 2}}


def completion():
    return {'id': 'chat-one', 'choices': [{'index': 0, 'message': {'content': 'saved'}, 'finish_reason': 'stop'}],
            'usage': USAGE}


async def provider(state, handler):
    await state.http.aclose()
    state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))


def snapshot(state):
    row = state.store._one('SELECT * FROM api_attempts')
    return row, json.loads(row['summary'])


async def test_completed_post_replay_and_get_do_not_call_or_bill_again(managed):
    client, state, creds = managed
    calls = []
    def upstream(request):
        calls.append(request)
        assert request.headers['authorization'] == 'Bearer provider-secret'
        assert request.headers['idempotency-key'].startswith('atlas-gate-')
        assert request.headers['idempotency-key'] != HEADERS['Idempotency-Key']
        assert not any(k.startswith('x-atlas-api-attempt-') for k in request.headers)
        assert not any(k.startswith('x-atlas-recovery-') for k in request.headers)
        return httpx.Response(200, json=completion())
    await provider(state, upstream)
    first = await client.post(CHAT, json=BODY, headers=HEADERS)
    assert first.status_code == 200, first.text
    aid = first.headers['X-Atlas-API-Attempt-Id']
    # Existing saved output needs neither fresh quota nor an upstream credential.
    state.orgs['first'].quota.limit = 0
    state.key_for = lambda route: None
    again = await client.post(CHAT, json=BODY, headers=HEADERS)
    assert again.status_code == 200 and again.content == first.content
    receipt = await client.get(f'/harness/llm/attempts/{aid}')
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()['terminalKnown'] and receipt.json()['usageStatus'] == 'known'
    from atlas_gate.gate.operations import body_sha256
    assert receipt.json()['bodySha256'] == body_sha256(BODY)
    result = await client.get(f'/harness/llm/attempts/{aid}/result')
    assert result.status_code == 200 and result.content == first.content
    assert len(calls) == 1
    assert len(state.store._all("SELECT * FROM usage WHERE kind='gateway'")) == 1
    other = await enroll(client)
    foreign = await client.get(f'/harness/llm/attempts/{aid}/result',
        headers={'Authorization': 'Bearer ' + other['device_token']})
    assert foreign.status_code == 404


async def test_concurrent_duplicate_observes_original_producer(managed):
    client, state, _ = managed
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def upstream(request):
        calls.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(200, json=completion())
    await provider(state, upstream)
    first = asyncio.create_task(client.post(CHAT, json=BODY, headers=HEADERS))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        again = await client.post(CHAT, json=BODY, headers=HEADERS)
        assert again.status_code == 202 and again.json()['state'] == 'running'
    finally:
        release.set()
    assert (await first).status_code == 200
    assert len(calls) == 1


class Bytes(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


def sse(doc):
    return ('data: ' + json.dumps(doc) + '\n\n').encode()


async def test_finished_chat_calls_survive_missing_usage_tail_and_done(managed):
    client, state, _ = managed
    chunks = [sse({'choices': [{'index': 0, 'delta': {'tool_calls': [
        {'index': 0, 'id': 'call1', 'function': {'name': 'inert', 'arguments': '{'}}]}}]}),
        sse({'choices': [{'index': 0, 'delta': {'tool_calls': [
            {'index': 0, 'function': {'arguments': '"x":1}'}}]}, 'finish_reason': 'tool_calls'}]})]
    calls = []
    def upstream(request):
        calls.append(request)
        return httpx.Response(200, stream=Bytes(chunks), headers={'content-type': 'text/event-stream'})
    await provider(state, upstream)
    result = await client.post(CHAT, json={**BODY, 'stream': True}, headers=HEADERS)
    assert result.content == b''.join(chunks)
    row, saved = snapshot(state)
    assert row['state'] == 'complete' and saved['runState'] == 'waiting-tools'
    assert saved['toolCallsComplete'] and saved['usageStatus'] == 'unknown'
    replay = await client.post(CHAT, json={**BODY, 'stream': True}, headers=HEADERS)
    assert replay.content == result.content and len(calls) == 1
    assert not state.store._all("SELECT * FROM usage WHERE kind='gateway'")


async def test_consumer_disconnect_does_not_cancel_managed_producer(managed):
    client, state, _ = managed
    release = asyncio.Event()
    first = sse({'choices': [{'index': 0, 'delta': {'content': 'saved'}}]})
    last = sse({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}], 'usage': USAGE})
    class Controlled(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield first
            await release.wait()
            yield last
    await provider(state, lambda request: httpx.Response(200, stream=Controlled(),
                                                        headers={'content-type': 'text/event-stream'}))
    async with client.stream('POST', CHAT, json={**BODY, 'stream': True}, headers=HEADERS) as response:
        aid = response.headers['X-Atlas-API-Attempt-Id']
        await anext(response.aiter_raw())
    assert state.api_attempts.tasks
    release.set()
    await asyncio.wait_for(asyncio.gather(*tuple(state.api_attempts.tasks)), 3)
    receipt = await client.get(f'/harness/llm/attempts/{aid}')
    assert receipt.json()['state'] == 'complete'
    result = await client.get(f'/harness/llm/attempts/{aid}/result')
    assert result.content == first + last


async def test_lost_provider_outcome_is_durable_and_same_key_never_retries(managed):
    client, state, _ = managed
    calls = []
    def upstream(request):
        calls.append(request)
        raise httpx.ReadError('inert disconnect', request=request)
    await provider(state, upstream)
    first = await client.post(CHAT, json=BODY, headers=HEADERS)
    assert first.status_code == 409 and first.json()['error']['code'] == 'api_attempt_outcome_unknown'
    again = await client.post(CHAT, json=BODY, headers=HEADERS)
    assert again.status_code == 409 and len(calls) == 1
    row, saved = snapshot(state)
    assert row['state'] == 'outcome_unknown' and not saved['retry']['allowed']
    assert saved['usageStatus'] == 'unknown'


async def test_changed_target_body_or_branch_conflicts_without_dispatch(managed):
    client, state, _ = managed
    calls = []
    def upstream(request):
        calls.append(request)
        return httpx.Response(200, json=completion())
    await provider(state, upstream)
    assert (await client.post(CHAT, json=BODY, headers=HEADERS)).status_code == 200
    changed = await client.post(CHAT, json={**BODY, 'messages': []}, headers=HEADERS)
    assert changed.status_code == 409 and 'idempotency_conflict' in changed.text
    changed_branch = await client.post(CHAT, json=BODY, headers={**HEADERS, 'X-Atlas-Branch-Id': 'different'})
    assert changed_branch.status_code == 409 and len(calls) == 1


async def test_same_client_key_from_two_devices_has_distinct_provider_identity(managed):
    client, state, _ = managed
    vendor_cache = {}
    def upstream(request):
        key = request.headers['idempotency-key']
        doc = completion()
        doc['choices'][0]['message']['content'] = json.loads(request.content)['messages'][0]['content']
        return httpx.Response(200, json=vendor_cache.setdefault(key, doc))
    await provider(state, upstream)
    first = await client.post(CHAT, json=BODY, headers=HEADERS)
    other = await enroll(client)
    second = await client.post(CHAT, json={**BODY, 'messages': [{'role': 'user', 'content': 'second-device'}]},
        headers={**HEADERS, 'Authorization': 'Bearer ' + other['device_token']})
    assert first.json()['choices'][0]['message']['content'] == 'inert'
    assert second.json()['choices'][0]['message']['content'] == 'second-device'
    assert len(vendor_cache) == 2


async def test_branch_admission_rejection_is_not_provider_dispatch(managed):
    client, state, _ = managed
    calls, admissions = [], []
    def barrier(request, device, route, aid, *, db):
        admissions.append(aid)
        raise GateError(409, 'recovery_pending', 'inert branch barrier', deliveryState='not_sent')
    state.api_attempts.admission = barrier
    await provider(state, lambda request: calls.append(request) or httpx.Response(200, json=completion()))
    first = await client.post(CHAT, json=BODY, headers=HEADERS)
    again = await client.post(CHAT, json=BODY, headers=HEADERS)
    assert first.status_code == again.status_code == 409
    assert not calls and len(admissions) == 1
    assert snapshot(state)[0]['state'] == 'rejected_not_sent'


async def branch_headers(client):
    response = await client.post('/harness/recovery/inventories', json={
        'version': 1, 'conversationId': 'chat', 'branchId': 'branch', 'clientStoreEpoch': 'inert-store',
        'checkpointInventoryVersion': 1, 'sources': []}, headers={'Idempotency-Key': 'inventory'})
    assert response.status_code == 200, response.text
    inventory = response.json()
    sealed = await client.post('/harness/recovery/inventories/' + inventory['inventoryId'] + '/seal',
                               json={'version': 1}, headers={'Idempotency-Key': 'seal'})
    assert sealed.status_code == 200, sealed.text
    fence = inventory['branchFence']
    return {**HEADERS, 'X-Atlas-Conversation-Id': 'chat', 'X-Atlas-Branch-Id': 'branch',
            'X-Atlas-Recovery-Inventory': inventory['inventoryId'],
            'X-Atlas-Recovery-Generation': str(fence['recoveryGeneration']),
            'X-Atlas-Control-Epoch': str(fence['controlEpoch'])}


async def test_ready_branch_does_not_block_its_own_new_prepared_attempt(managed):
    client, state, _ = managed
    headers = await branch_headers(client)
    calls = []
    def upstream(request):
        calls.append(request)
        assert not any(k.startswith('x-atlas-recovery-') for k in request.headers)
        assert 'x-atlas-conversation-id' not in request.headers and 'x-atlas-branch-id' not in request.headers
        return httpx.Response(200, json=completion())
    await provider(state, upstream)
    response = await client.post(CHAT, json=BODY, headers=headers)
    assert response.status_code == 200, response.text
    assert len(calls) == 1


async def test_unknown_attempt_blocks_new_key_on_same_ready_branch(managed):
    client, state, _ = managed
    headers = await branch_headers(client)
    calls = []
    def upstream(request):
        calls.append(request)
        raise httpx.ReadError('inert unknown', request=request)
    await provider(state, upstream)
    first = await client.post(CHAT, json=BODY, headers=headers)
    assert first.status_code == 409 and first.json()['error']['code'] == 'api_attempt_outcome_unknown'
    second = await client.post(CHAT, json=BODY, headers={**headers, 'Idempotency-Key': 'new-attempt'})
    assert second.status_code == 409 and second.json()['error']['code'] == 'recovery_required'
    assert len(calls) == 1


async def test_legacy_same_key_still_has_legacy_no_dedup_semantics(managed):
    client, state, _ = managed
    calls = []
    await provider(state, lambda request: calls.append(request) or httpx.Response(200, json=completion()))
    for _ in range(2):
        assert (await client.post(CHAT, json=BODY, headers={'Idempotency-Key': 'legacy'})).status_code == 200
    assert len(calls) == 2 and not state.store._all('SELECT * FROM api_attempts')


@pytest.mark.parametrize('endpoint,dialect,doc,expected,run', [
    ('responses', 'openai', {'id': 'resp', 'status': 'completed', 'output': [
        {'type': 'function_call', 'call_id': 'call', 'name': 'inert', 'arguments': '{}'}],
        'usage': {'input_tokens': 7, 'output_tokens': 3, 'input_tokens_details': {'cached_tokens': 2}}},
     'tool_calls', 'waiting-tools'),
    ('responses', 'deepseek', {'id': 'resp', 'status': 'incomplete', 'store': False,
        'incomplete_details': {'reason': 'max_output_tokens'}, 'output': []}, 'incomplete', 'response-ready'),
    ('messages', 'anthropic', {'id': 'msg', 'stop_reason': 'pause_turn',
        'content': [{'type': 'server_tool_use', 'id': 'srvtool1', 'name': 'web_search', 'input': {'query': 'inert'}}],
        'usage': {'input_tokens': 7, 'output_tokens': 3, 'cache_creation_input_tokens': 2}},
     'paused', 'awaiting-continuation')])
async def test_endpoint_native_result_is_persisted_through_real_gate(managed, endpoint, dialect, doc, expected, run):
    client, state, _ = managed
    org = state.orgs['first']
    org.route('private-api').protocol = 'anthropic' if endpoint == 'messages' else 'openai'
    next(m for m in org.models if m.model == 'api-model').reasoningDialect = dialect
    calls = []
    await provider(state, lambda request: calls.append(request) or httpx.Response(200, json=doc))
    path = '/harness/llm/v1/' + endpoint
    first = await client.post(path, json=BODY, headers=HEADERS)
    assert first.status_code == 200, first.text
    _, saved = snapshot(state)
    assert saved['outcome'] == expected and saved['runState'] == run
    again = await client.post(path, json=BODY, headers=HEADERS)
    assert again.content == first.content and len(calls) == 1
    if endpoint == 'messages':
        assert saved['rawUsage']['cache_creation_input_tokens'] == 2
        assert state.store._one("SELECT cost FROM usage WHERE kind='gateway'")['cost'] is None


async def test_typed_provider_overload_is_saved_and_only_new_key_can_dispatch(managed):
    client, state, _ = managed
    next(m for m in state.orgs['first'].models if m.model == 'api-model').reasoningDialect = 'deepseek'
    calls = []
    await provider(state, lambda request: calls.append(request) or httpx.Response(503,
        json={'error': {'code': 'server_is_overloaded'}}, headers={'Retry-After': '2'}))
    first = await client.post(CHAT, json=BODY, headers=HEADERS)
    again = await client.post(CHAT, json=BODY, headers=HEADERS)
    assert first.status_code == again.status_code == 503 and len(calls) == 1
    assert snapshot(state)[1]['retry']['allowed']
    other = await client.post(CHAT, json=BODY, headers={**HEADERS, 'Idempotency-Key': 'attempt-two'})
    assert other.status_code == 503 and len(calls) == 2


async def test_missing_cache_usage_is_partial_not_invented_zero(managed):
    client, state, _ = managed
    doc = completion()
    doc['usage'] = {'prompt_tokens': 7, 'completion_tokens': 3}
    await provider(state, lambda request: httpx.Response(200, json=doc))
    response = await client.post(CHAT, json=BODY, headers=HEADERS)
    assert response.status_code == 200
    _, saved = snapshot(state)
    assert saved['usageStatus'] == 'partial' and saved['usage'] is None
    assert saved['costStatus'] == 'unknown'
    assert not state.store._all("SELECT * FROM usage WHERE kind='gateway'")


async def test_failed_terminal_commit_has_visible_unknown_and_no_endless_consumer(managed):
    client, state, _ = managed
    state.store._exec("CREATE TRIGGER fail_terminal BEFORE UPDATE OF summary ON api_attempts "
                      "BEGIN SELECT RAISE(ABORT,'inert persistence failure'); END")
    calls = []
    await provider(state, lambda request: calls.append(request) or httpx.Response(200, json=completion()))
    response = await asyncio.wait_for(client.post(CHAT, json=BODY, headers=HEADERS), 3)
    assert response.status_code == 409
    saved = response.json()['attempt']
    assert saved['state'] == 'outcome_unknown' and saved['durableState'] == 'running'
    assert saved['producerAlive'] is False and not saved['retry']['allowed']
    again = await client.post(CHAT, json=BODY, headers=HEADERS)
    assert again.status_code == 409 and len(calls) == 1
    assert not state.store._all("SELECT * FROM usage WHERE kind='gateway'")


@pytest.mark.parametrize('headers', [
    {'X-Atlas-API-Attempt-Contract': '1'},
    {'X-Atlas-API-Attempt-Contract': '2', 'Idempotency-Key': 'inert'}])
async def test_unnegotiated_attempt_never_reaches_provider(managed, headers):
    client, state, _ = managed
    calls = []
    await provider(state, lambda request: calls.append(request) or httpx.Response(200, json=completion()))
    response = await client.post(CHAT, json=BODY, headers=headers)
    assert response.status_code == 422 and not calls


def test_restart_seals_interrupted_attempt_and_keeps_partial_output(tmp_path):
    path = str(tmp_path / 'attempt.db')
    store = Store(path)
    manager = ApiAttempts(store)
    device = SimpleNamespace(org='org', device_id='device', user_id='user')
    row, fresh = manager.claim(device, SimpleNamespace(id='route'), 'model', '/responses', b'{"model":"model"}', httpx.Headers(HEADERS))
    assert fresh
    manager.dispatched(row['id'])
    manager.response_headers(row['id'], 200, {'content-type': 'text/event-stream'})
    manager.append(row['id'], b'data: partial\n\n')
    store.close()
    reopened = Store(path)
    try:
        restored = ApiAttempts(reopened)
        saved = restored.snapshot(row['id'], device)
        assert saved['state'] == 'outcome_unknown' and saved['resultBytes'] > 0
        assert not saved['retry']['allowed']
        assert restored.replay(restored.owned(row['id'], device), device).status_code == 409
        assert restored.replay(restored.owned(row['id'], device), device, result=True).body == b'data: partial\n\n'
    finally:
        reopened.close()


def test_restart_before_dispatch_proves_not_sent(tmp_path):
    path = str(tmp_path / 'prepared.db')
    device = SimpleNamespace(org='org', device_id='device', user_id='user')
    store = Store(path)
    manager = ApiAttempts(store)
    row, _ = manager.claim(device, SimpleNamespace(id='route'), 'model', '/responses', b'{}', httpx.Headers(HEADERS))
    store.close()
    reopened = Store(path)
    try:
        saved = ApiAttempts(reopened).snapshot(row['id'], device)
        assert saved['state'] == 'rejected_not_sent' and saved['deliveryState'] == 'not_sent'
    finally:
        reopened.close()


def test_terminal_and_usage_commit_or_rollback_together(tmp_path):
    store = Store(str(tmp_path / 'attempt.db'))
    try:
        manager = ApiAttempts(store)
        device = SimpleNamespace(org='org', device_id='device', user_id='user')
        row, _ = manager.claim(device, SimpleNamespace(id='route'), 'model', '/responses', b'{}', httpx.Headers(HEADERS))
        manager.dispatched(row['id'])
        usage = dict(org='org', user_id='user', device_id='device', kind='gateway', model='model',
                     prompt_tokens=7, completion_tokens=3, cached_tokens=2, cost=None, route='route')
        store._exec("CREATE TRIGGER fail_receipt BEFORE UPDATE OF summary ON api_attempts "
                    "BEGIN SELECT RAISE(ABORT,'inert failed receipt'); END")
        with pytest.raises(Exception, match='inert failed receipt'):
            manager.finish(row['id'], {'outcome': 'completed'}, usage_row=usage)
        assert not store._all('SELECT * FROM usage')
        assert manager.owned(row['id'], device)['state'] == 'running'
        store._exec('DROP TRIGGER fail_receipt')
        manager.finish(row['id'], {'outcome': 'completed'}, usage_row=usage)
        manager.finish(row['id'], {'outcome': 'completed'}, usage_row=usage)
        assert len(store._all('SELECT * FROM usage')) == 1
    finally:
        store.close()


@pytest.mark.parametrize('reason,outcome,run', [
    ('end_turn', 'completed', 'response-ready'), ('tool_use', 'tool_calls', 'waiting-tools'),
    ('pause_turn', 'paused', 'awaiting-continuation'), ('max_tokens', 'incomplete', 'response-ready'),
    ('refusal', 'refused', 'response-ready'), ('model_context_window_exceeded', 'incomplete', 'response-ready')])
def test_messages_stop_reason_is_not_run_success(reason, outcome, run):
    facts = ResponseFacts('/messages', 'anthropic')
    facts.nonstream(json.dumps({'id': 'msg', 'stop_reason': reason,
        'content': [{'type': 'tool_use', 'id': 'tool1', 'name': 'inert', 'input': {}}],
        'usage': {'input_tokens': 2, 'output_tokens': 3}}).encode())
    summary = facts.summary(200, {}, {'prompt': 2, 'completion': 3, 'cached': 0})
    assert summary['terminalKnown'] and summary['outcome'] == outcome and summary['runState'] == run
    assert not summary['hostExecutionAllowed']
    if reason == 'max_tokens':
        assert not summary['toolCallsComplete']


def test_messages_partial_tool_and_sse_error_never_become_complete_call():
    facts = ResponseFacts('/messages', 'anthropic')
    for doc in [{'type': 'message_start', 'message': {'id': 'msg', 'usage': {'input_tokens': 2, 'output_tokens': 1}}},
                {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'tool_use', 'id': 'tool', 'name': 'inert', 'input': {}}},
                {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json': '{"x":'}},
                {'type': 'error', 'error': {'type': 'overloaded_error'}}]:
        facts.feed(sse(doc))
    summary = facts.summary(200, {})
    assert not summary['terminalKnown'] and not summary['toolCallsComplete']
    assert not summary['retry']['allowed'] and summary['usageStatus'] == 'partial'


def test_malformed_stream_cannot_be_healed_by_a_finish_marker():
    facts = ResponseFacts('/chat/completions', 'openai')
    facts.feed(b'data: not-json\n\n')
    facts.feed(sse({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}))
    summary = facts.summary(200, {})
    assert not summary['terminalKnown'] and summary['error']['category'] == 'provider_protocol_error'


def test_finished_call_with_invalid_arguments_is_never_ready_for_tools():
    facts = ResponseFacts('/chat/completions', 'openai')
    facts.nonstream(json.dumps({'choices': [{'index': 0, 'message': {'tool_calls': [
        {'id': 'call', 'function': {'name': 'inert', 'arguments': '{'}}]}, 'finish_reason': 'tool_calls'}]}).encode())
    summary = facts.summary(200, {})
    assert summary['terminalKnown'] and not summary['toolCallsComplete']
    assert summary['runState'] == 'waiting-recovery'
    assert summary['error']['category'] == 'incomplete_tool_call'


@pytest.mark.parametrize('status,output,reason,outcome,run', [
    ('completed', [{'type': 'function_call', 'call_id': 'call', 'name': 'inert', 'arguments': '{}'}], None, 'tool_calls', 'waiting-tools'),
    ('completed', [{'type': 'message', 'content': [{'type': 'refusal', 'refusal': 'no'}]}], None, 'refused', 'response-ready'),
    ('incomplete', [], 'max_output_tokens', 'incomplete', 'response-ready'),
    ('in_progress', [], None, 'accepted-running', 'waiting-provider')])
def test_responses_status_content_and_run_are_separate(status, output, reason, outcome, run):
    facts = ResponseFacts('/responses', 'openai')
    facts.nonstream(json.dumps({'id': 'response', 'status': status, 'output': output,
                              'incomplete_details': {'reason': reason}}).encode())
    summary = facts.summary(200, {})
    assert summary['outcome'] == outcome and summary['runState'] == run
    assert summary['finishReason'] == reason


@pytest.mark.parametrize('dialect,status,error,eligible', [
    ('anthropic', 529, {'type': 'overloaded_error'}, True),
    ('deepseek', 503, {'code': 'server_is_overloaded'}, True),
    ('compatible', 503, {'code': 'server_is_overloaded'}, False),
    ('openai', 503, {'code': 'unknown'}, False),
    ('compatible', 408, {'code': 'timeout'}, False),
    ('anthropic', 200, {'type': 'overloaded_error'}, False)])
def test_retry_boundary_requires_attested_provider_and_typed_preinference_rejection(dialect, status, error, eligible):
    facts = ResponseFacts('/messages' if dialect == 'anthropic' else '/chat/completions', dialect)
    facts.nonstream(json.dumps({'error': error}).encode())
    summary = facts.summary(status, {'retry-after': '2'})
    assert summary['retry']['allowed'] is eligible
    assert summary['retry']['scope'] == ('new_attempt_with_policy' if eligible else 'reconcile')
    if status == 408:
        assert not summary['terminalKnown'] and summary['error']['category'] == 'provider_timeout'
