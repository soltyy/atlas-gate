"""A missing session-scoped Stop must never be retargeted to its successor."""
import httpx
import pytest

from .test_network_gate import create, events, network  # noqa: F401
from .test_retry_contract import delivery_network  # noqa: F401


@pytest.mark.parametrize('loss', ['before_dispatch', 'lost_ack'])
async def test_uncertain_stop_observes_original_receipt_without_stopping_successor(delivery_network, monkeypatch, loss):
    client, app, *_ = delivery_network
    sid = await create(client)
    prefix = f'/harness/agent/sessions/{sid}'
    state = app.state.gate
    binding = state.store._one('SELECT * FROM router_bindings WHERE id=?', (sid,))
    upstream = state.nodes.clients[binding['node_id']]
    original = upstream.request
    stops = []

    async def lose(method, path, **kw):
        if method == 'POST' and path.endswith('/interrupt'):
            stops.append(kw['headers']['Idempotency-Key'])
            if len(stops) == 1:
                if loss == 'lost_ack':
                    response = await original(method, path, **kw)
                    assert response.status_code == 200
                raise httpx.ReadTimeout('Stop outcome is unknown')
        return await original(method, path, **kw)

    assert (await client.post(prefix + '/prompt?mode=async', json={'id':'old','text':'call:echo'})).status_code == 202
    await events(client, sid, 'old')
    monkeypatch.setattr(upstream, 'request', lose)
    headers = {'Idempotency-Key':'original-stop'}
    assert (await client.post(prefix + '/interrupt', headers=headers)).status_code == 503
    if loss == 'before_dispatch':
        assert (await client.post(prefix + '/tool_result', json={
            'callId':'stub-call-1','content':'done','isError':False})).status_code == 200
    await events(client, sid, 'old', terminal=True)
    assert (await client.post(prefix + '/prompt?mode=async', json={'id':'next','text':'call:echo'})).status_code == 202
    next_events = await events(client, sid, 'next')
    next_call = next(e['callId'] for e in next_events if e['type'] == 'tool_call')
    repeated = await client.post(prefix + '/interrupt', headers=headers)
    assert repeated.status_code == (503 if loss == 'before_dispatch' else 200)
    assert len(stops) == 1
    observed = await client.get(prefix + '/commands', params={'requestId':'original-stop'})
    assert observed.status_code == (202 if loss == 'before_dispatch' else 200)
    status = (await client.get(prefix + '/turns/next')).json()
    assert status['done'] is False
    assert (await client.post(prefix + '/tool_result', json={
        'callId':next_call,'content':'done','isError':False})).status_code == 200
    await events(client, sid, 'next', terminal=True)
