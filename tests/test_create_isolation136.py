import asyncio
import json

import httpx
import pytest

from atlas_gate.gate.errors import GateError
from atlas_gate.gate.routing import Routing
from atlas_gate.gate.store import Store
from .test_routing import FakeNodes, device


class CreateNodes(FakeNodes):
    def __init__(self):
        super().__init__(capacity=40)
        self.config['a'].transport = 'direct'
        self.config['b'].transport = 'direct'
        self.config['a'].routes.append('other')
        for info in self.snapshots.values():
            info['capabilities'] = ['durable_commands', 'durable_events']
        self.fail_once = False
        self.hold = None
        self.dispatched = []
        self.receipts = {}
        self.history = {}

    async def probe(self, node):
        return self.snapshots[node]

    async def request(self, node, method, path, **kwargs):
        if method == 'GET':
            if path.endswith('/reconcile'):
                local_id = path.split('/')[-2]
                return httpx.Response(200, json=self.history[local_id])
            key = path.rsplit('/', 1)[1]
            if key not in self.receipts:
                return httpx.Response(404, json={'detail': 'command_not_found'})
            return httpx.Response(200, json=self.receipts[key])
        key = kwargs['request_id']
        self.dispatched.append((node, key, kwargs['expected_boot'], kwargs['content']))
        if self.fail_once:
            self.fail_once = False
            raise GateError(503, 'command_outcome_unknown', 'disconnected before admission')
        if self.hold:
            entered, release = self.hold
            entered.set()
            await release.wait()
        if key not in self.receipts:
            body = json.loads(kwargs['content'])
            response = {'id': 'new-' + key[-8:], 'tools': 0, 'resumed': bool(body.get('resumeSessionId'))}
            self.receipts[key] = {'state': 'complete', 'status': 200, 'response': response, 'requestId': key}
        return httpx.Response(200, json=self.receipts[key]['response'])


def scoped(chat, agent='same'):
    return {'sessionScope': {'version': 1, 'agentId': agent, 'conversationId': chat}}


async def test_same_chat_waits_and_other_chats_continue_after_gate_restart(tmp_path):
    path = str(tmp_path/'gate.db')
    store, nodes = Store(path), CreateNodes()
    router = Routing(store, nodes)
    nodes.snapshots['b']['draining'] = True
    payload = scoped('source')
    nodes.fail_once = True
    with pytest.raises(GateError):
        await router.create(device('d'), 'sub', 'claude', payload, 8, 'original')
    store.close()
    store = Store(path)
    router = Routing(store, nodes)
    try:
        for route, key, value in (
            ('sub', 'same-router-other-agent', scoped('source', 'different')),
            ('sub', 'same-agent-other-chat', scoped('another')),
            ('other', 'other-route-legacy', {}),
        ):
            assert (await router.create(device('d'), route, 'claude', value, 8, key))[0] == 200
        for route in ('sub', 'other'):
            with pytest.raises(GateError, match='сверки'):
                await router.create(device('d'), route, 'claude', payload, 8, 'competing')
        with pytest.raises(GateError) as mismatch:
            await router.create(device('d'), 'sub', 'claude', scoped('changed'), 8, 'original')
        assert mismatch.value.code == 'idempotency_conflict'

        nodes.hold = asyncio.Event(), asyncio.Event()
        active = asyncio.create_task(router.create(device('d'), 'sub', 'claude', payload, 8, 'original'))
        await nodes.hold[0].wait()
        with pytest.raises(GateError, match='сверки'):
            await router.create(device('d'), 'sub', 'claude', payload, 8, 'original')
        nodes.hold[1].set()
        result = await active
        assert result[0] == 200
        assert await router.create(device('d'), 'sub', 'claude', payload, 8, 'original') == result
        assert nodes.dispatched[0] == nodes.dispatched[-1]
        assert 'sessionScope' not in json.loads(nodes.dispatched[-1][-1])
        with pytest.raises(GateError) as limit:
            await router.create(device('d'), 'sub', 'claude', {}, 4, 'over-limit')
        assert limit.value.code == 'agent_sessions_limit'
    finally:
        store.close()


async def test_legacy_resume_only_retries_original_command_on_pinned_boot(tmp_path):
    store, nodes = Store(str(tmp_path/'gate.db')), CreateNodes()
    router = Routing(store, nodes)
    nodes.snapshots['b']['draining'] = True
    try:
        _, body = await router.create(device('d'), 'sub', 'claude', {}, 8, 'old')
        sid = json.loads(body)['id']
        old = router.get(sid, device('d'))
        store._exec("UPDATE router_bindings SET sdk_id='native',status='closed' WHERE id=?", (sid,))
        nodes.history[old['local_id']] = {'id': old['local_id'], 'nodeId': 'a', 'bootId': 'a',
            'sdkSessionId': 'native', 'closed': True,
            'sessionRecovery': {'busy': False, 'pendingTurnId': None}}
        payload = {'resumeSessionId': old['sdk_alias'], 'allowHistoryFallback': True}
        nodes.fail_once = True
        with pytest.raises(GateError):
            await router.create(device('d'), 'sub', 'claude', payload, 8, 'resume')
        store._exec("UPDATE router_bindings SET resume_sdk_id=NULL,create_body_sha256=NULL WHERE status='unknown'")
        with pytest.raises(GateError, match='сверки'):
            await router.create(device('d'), 'sub', 'claude', payload, 8, 'competing-legacy-key')
        with pytest.raises(GateError) as foreign:
            await router.create(device('someone'), 'sub', 'claude', payload, 8, 'resume')
        assert foreign.value.status == 404
        nodes.snapshots['a']['bootId'] = 'changed'
        with pytest.raises(GateError, match='сверки'):
            await router.create(device('d'), 'sub', 'claude', payload, 8, 'resume')
        assert len(nodes.dispatched) == 2
        nodes.snapshots['a']['bootId'] = 'a'
        result = await router.create(device('d'), 'sub', 'claude', payload, 8, 'resume')
        assert json.loads(result[1])['sessionRecovery']['mode'] == 'resumed'
        assert nodes.dispatched[-2] == nodes.dispatched[-1]
    finally:
        store.close()
