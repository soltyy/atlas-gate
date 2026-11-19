"""A stale negotiation result cannot negate an already dispatched answer."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from atlas_gate.gate.question_answers import AnswerRequest, Answers
from atlas_gate.gate.store import Store
from .test_network_gate import network  # noqa: F401
from .test_question_answers135 import prepare, active, body, raw, observe


async def test_concurrent_retry_cannot_create_false_negative(network, monkeypatch):
    from atlas_router.backends.stub import StubSession
    queued, technical = await prepare(network, monkeypatch)
    client, app, *_ = network
    state = app.state.gate
    sid, prefix, headers = await active(client)
    value = body(); data = raw(value)
    held, release_probe, release_native, native_queued = (asyncio.Event() for _ in range(4))
    native = StubSession.steer_v1

    async def delayed_native(self, *args, **kwargs):
        native_queued.set()
        await release_native.wait()
        return await native(self, *args, **kwargs)

    monkeypatch.setattr(StubSession, 'steer_v1', delayed_native)
    original = state.nodes.request
    first_probe = True

    async def reordered(node, method, path, **kwargs):
        nonlocal first_probe
        if method == 'GET' and path.endswith('/sdk-tools') and first_probe:
            first_probe = False
            held.set()
            await release_probe.wait()
            return httpx.Response(200, json={'questionAnswers':{'version':1,'supported':False}})
        return await original(node, method, path, **kwargs)

    monkeypatch.setattr(state.nodes, 'request', reordered)
    send = {**headers, 'Idempotency-Key':'racing-question', 'Content-Type':'application/json'}
    first = asyncio.create_task(client.post(prefix+'/question-answer', content=data, headers=send))
    try:
        await asyncio.wait_for(held.wait(), 5)
        second = await client.post(prefix+'/question-answer', content=data, headers=send)
        assert second.status_code == 202, second.text
        await asyncio.wait_for(native_queued.wait(), 5)
        release_probe.set()
        late = await first
        assert late.status_code == 202
        assert late.json()['questionAnswer']['state'] != 'not_applied'
        release_native.set()
        async with asyncio.timeout(5):
            while True:
                current = await observe(client, prefix, value, data)
                receipt = current.json()['questionAnswer']
                if receipt['state'] == 'applied':
                    break
                assert receipt['state'] != 'not_applied'
                await asyncio.sleep(.01)
        assert receipt['admission'] == 'accepted' and receipt['callback'] == 'released'
        assert len(technical) == len(queued) == 1
        assert (await client.post(prefix+'/question-answer', content=data, headers=send)).json() == current.json()
    finally:
        release_probe.set(); release_native.set()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.parametrize('legacy', [False, True])
def test_dispatch_and_preflight_rejection_are_durable_opposing_fences(tmp_path, legacy):
    store = Store(str(tmp_path/'gate.db'))
    try:
        answers = Answers(store)
        owner = SimpleNamespace(device_id='d', org='o')
        binding = {'id':'s', 'node_id':'node'}
        value = body()
        row, fresh = answers.admit('rid', owner, binding, 'key', 'boot', raw(value), AnswerRequest(**value))
        assert fresh
        original = json.loads(row['receipt'])
        negative = {**original, 'state':'not_applied', 'admission':'rejected',
            'callback':'not_released', 'application':'not_applied'}
        if legacy:
            store._exec('DELETE FROM gate_question_answer_dispatches WHERE id=?', (row['id'],))
            answers = Answers(store)
        else:
            assert answers.dispatch(row)[1]
        # Reload from SQLite, not an in-memory dispatch flag.
        answers = Answers(store)
        assert answers.save(row, negative, pre_dispatch_rejection=True) == original
        assert len(store._all('PRAGMA table_info(gate_question_answers)')) == 10
        legacy_value = body()
        # Exact ten-value INSERT used by the previous published Gate still works.
        store._exec('INSERT INTO gate_question_answers VALUES (?,?,?,?,?,?,?,?,?,?)',
            ('legacy-insert','d','o','s',legacy_value['clientMessageId'],'old-fingerprint',
             'old-key','boot',raw(legacy_value),json.dumps(original)))
        legacy_row = answers.get('legacy-insert')
        assert answers.save(legacy_row, negative, pre_dispatch_rejection=True) == original
        value2 = body()
        row2, _ = answers.admit('other', owner, binding, 'key2', 'boot', raw(value2), AnswerRequest(**value2))
        negative2 = {**json.loads(row2['receipt']), 'state':'not_applied', 'admission':'rejected',
            'callback':'not_released', 'application':'not_applied'}
        assert answers.save(row2, negative2, pre_dispatch_rejection=True) == negative2
        # The opposing interleaving must not send after a proven rejection.
        assert answers.dispatch(row2) == (negative2, False)
    finally:
        store.close()
