import asyncio
import copy
import pytest
from atlas_gate.gate.transport_recovery import decorate
from .test_network_gate import network, create

EXPIRY = {'stage': 'websocket', 'httpStatusCode': 400, 'code': 'websocket_connection_limit_reached'}


def test_policy_stop_overrides_stale_snapshot_without_rewriting_history():
    old = {'type': 'result', 'ok': False, 'subtype': 'interrupted', 'providerError': EXPIRY}
    before = copy.deepcopy(old)
    result = decorate(old)
    assert result['transportRecovery'] == {'version': 1, 'state': 'stopped', 'action': 'none', 'replayAllowed': False}
    assert old == before and result['providerError'] == EXPIRY
    assert decorate({'type': 'error', 'providerError': {'code': 'other_400'}}) == {'type': 'error', 'providerError': {'code': 'other_400'}}


@pytest.mark.parametrize('stop', [False, True])
async def test_http_gate_retry_and_closed_receipt_no_replay_or_quota(network, monkeypatch, stop):
    from atlas_router.backends.stub import StubSession
    client, app, routers, _, creds, other = network
    calls = []
    async def prompt(self, turn_id, text, attachments=None):
        calls.append(turn_id)
        yield {'type': 'provider_retry', 'id': turn_id, 'willRetry': True, 'providerError': EXPIRY}
        yield {'type': 'result', 'id': turn_id, 'ok': False, 'subtype': 'interrupted' if stop else 'failed',
               'providerError': EXPIRY, 'usage': {'input_tokens': 7, 'output_tokens': 2}}
    monkeypatch.setattr(StubSession, 'prompt', prompt)
    sid = await create(client);prefix = '/harness/agent/sessions/'+sid
    assert (await client.post(prefix+'/prompt?mode=async', json={'id': 'once', 'text': 'go'})).status_code == 202
    for _ in range(30):
        page = (await client.get(prefix+'/turns/once/events', params={'wait': 100})).json()
        if page['done']:break
        await asyncio.sleep(.01)
    assert page['done']
    assert page['events'][0]['transportRecovery']['action'] == 'read_same_turn'
    expected = 'none' if stop else 'explicit_continuation_after_reconcile'
    terminal = page['events'][-1]
    assert terminal['transportRecovery']['action'] == expected
    assert not app.state.gate.nodes.cooldown
    assert (await client.delete(prefix)).status_code == 200
    for _ in range(2):
        response = await client.get(prefix+'/turns/once/receipt')
        assert response.status_code == 200 and response.json() == {k:v for k,v in terminal.items() if k != 'seq'}
    assert len(app.state.gate.store._all('SELECT * FROM router_usage_receipts')) == 1
    assert (await client.get(prefix+'/turns/once/receipt', headers={'Authorization':'Bearer '+other['device_token']})).status_code == 404
    assert (await client.get(prefix+'/turns/foreign/receipt')).status_code == 404
    assert calls == ['once']
