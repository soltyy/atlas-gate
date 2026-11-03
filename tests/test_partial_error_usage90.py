"""Real HTTP consumer regression; only the SDK producer is scripted."""
import asyncio
import json

import pytest

from atlas_gate.gate.routing import Routing
from .test_network_gate import create, network  # noqa: F401
from .test_steering90 import connector_node


async def no_watch(*args):
    pass


@pytest.mark.parametrize('transport', ['direct', 'connector'])
@pytest.mark.parametrize('consumer', ['poll', 'sse', 'receipt', 'expired'])
async def test_completed_held_error_usage_is_consumed_once_with_partial_evidence(network, monkeypatch, tmp_path, transport, consumer):
    from atlas_router.backends.stub import StubSession
    client, app, routers, *_ = network
    state = app.state.gate
    # A background watcher would mask a broken explicit receipt consumer.
    state.recovery_task.cancel()
    await asyncio.gather(state.recovery_task, return_exceptions=True)
    monkeypatch.setattr('atlas_gate.gate.agent._watch', no_watch)
    produced = []
    async def failed(self, turn_id, text, attachments=None):
        produced.append(turn_id)
        yield {'type': 'error', 'id': turn_id, 'kind': 'session_dead', 'code': 'sdk_stream_ended',
               'message': 'EOF after completed own SDK result', 'outcomeUnknown': True,
               'usage': {'input_tokens': 10, 'output_tokens': 5},
               'usageIncomplete': True, 'usageSource': 'completed_sdk_results', 'costUsd': 0.00007,
               'modelUsage': {'native': {'inputTokens': 10, 'outputTokens': 5, 'costUSD': 0.00007}}}
    monkeypatch.setattr(StubSession, 'prompt', failed)
    task = await connector_node(client, state, tmp_path) if transport == 'connector' else None
    try:
        sid = await create(client);turn = 'held-eof';prefix = '/harness/agent/sessions/' + sid
        binding = state.store._one('SELECT * FROM router_bindings WHERE id=?', (sid,))
        owner = routers[0 if binding['node_id'] == 'a' else 1]
        response = await client.post(prefix + '/prompt' + ('' if consumer == 'sse' else '?mode=async'),
                                     json={'id': turn, 'text': 'scripted held result then EOF'})
        assert response.status_code == (200 if consumer == 'sse' else 202), response.text
        for _ in range(100):
            saved = owner.state.journal.turn(binding['local_id'], turn)
            if saved and saved['done']:
                break
            await asyncio.sleep(.01)
        else:
            pytest.fail('scripted terminal did not finish')
        if consumer != 'sse':
            assert state.store._one('SELECT COUNT(*) n FROM usage')['n'] == 0
        if consumer == 'poll':
            response = await client.get(prefix + '/turns/' + turn + '/events')
            terminal = next(e for e in response.json()['events'] if e['type'] == 'error')
        elif consumer == 'sse':
            terminal = next(json.loads(line[6:]) for line in response.text.splitlines()
                            if line.startswith('data: ') and json.loads(line[6:]).get('type') == 'error')
        elif consumer == 'expired':
            owner.state.registry.get(binding['local_id']).buffers.pop(turn)
            owner.state.journal.purge(0)  # Real Router journal retains the terminal receipt.
            expired = await client.get(prefix + '/turns/' + turn + '/events')
            assert expired.status_code == 410, expired.text
            assert state.store._one('SELECT COUNT(*) n FROM usage')['n'] == 1
            terminal = (await client.get(prefix + '/turns/' + turn + '/receipt')).json()
        else:
            terminal = (await client.get(prefix + '/turns/' + turn + '/receipt')).json()
        assert terminal['type'] == 'error' and terminal['outcomeUnknown'] is True
        assert terminal['usage'] == {'input_tokens': 10, 'output_tokens': 5}
        assert terminal['usageIncomplete'] is True and terminal['usageSource'] == 'completed_sdk_results'
        # Concurrent historical reads/recreated accounting component cannot bill/count again.
        state.routing = Routing(state.store, state.nodes)
        replies = await asyncio.gather(*(client.get(prefix + '/turns/' + turn + '/receipt') for _ in range(3)))
        assert all(r.status_code == 200 and r.json()['usageIncomplete'] is True for r in replies)
        assert produced == [turn]
        assert state.store._one('SELECT COUNT(*) n FROM usage')['n'] == 1
        assert state.store._one('SELECT COUNT(*) n FROM router_usage_receipts')['n'] == 1
        assert state.store._one('SELECT COUNT(*) n FROM agent_usage_details')['n'] == 1
        assert state.store._one('SELECT finished,accounted FROM router_operations WHERE session_id=? AND turn_id=?',
                                (sid, turn)) == {'finished': 1, 'accounted': 1}
        assert state.store._one('SELECT cost FROM usage')['cost'] is None
        report = (await client.get('/harness/admin/usage', headers={'X-Atlas-Token': 'admin'})).json()
        row = report['rows'][0]
        assert row['uncachedInputTokens'] == 10 and row['outputTokens'] == 5
        assert row['totalInputTokens'] is None and row['cacheReadTokens'] is None
        assert row['usageIncomplete'] is True and row['usageSource'] == 'completed_sdk_results'
        assert row['status'] == 'unknown' and report['summary']['incompleteRecords'] == 1
        assert 'costUSD' not in json.dumps(row['modelUsage'])
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_untyped_error_metrics_are_not_guessed_even_when_terminal_is_accounted(network, monkeypatch):
    from atlas_router.backends.stub import StubSession
    client, app, routers, *_ = network
    state = app.state.gate
    async def unproven(self, turn_id, text, attachments=None):
        yield {'type': 'error', 'id': turn_id, 'outcomeUnknown': True,
               'usage': {'input_tokens': 99, 'output_tokens': 88}}
    monkeypatch.setattr(StubSession, 'prompt', unproven)
    sid = await create(client);prefix = '/harness/agent/sessions/' + sid
    assert (await client.post(prefix + '/prompt?mode=async', json={'id': 'unknown', 'text': 'unproven'})).status_code == 202
    for _ in range(30):
        response = await client.get(prefix + '/turns/unknown/events', params={'wait': 100})
        if response.json()['done']:
            break
    assert response.json()['events'][-1]['type'] == 'error'
    assert state.store._one('SELECT COUNT(*) n FROM usage')['n'] == 0
    assert state.store._one('SELECT COUNT(*) n FROM router_usage_receipts')['n'] == 0
    assert state.store._one('SELECT accounted FROM router_operations WHERE session_id=? AND turn_id=?',
                            (sid, 'unknown'))['accounted'] == 1
