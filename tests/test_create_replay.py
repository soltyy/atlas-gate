"""#52: известный отказ создания не становится неизвестным и не исполняется дважды."""
import asyncio
import json
import httpx
import pytest
from atlas_gate.gate.errors import GateError
from atlas_gate.gate.routing import Routing, DDL
from atlas_gate.gate.store import Store
from .test_routing import FakeNodes, device


class RejectedNodes(FakeNodes):
    def __init__(self, body=b'{"detail":"resume timeout"}', lost_ack=False):
        super().__init__(capacity=4)
        self.body, self.lost_ack = body, lost_ack
        self.posts = []
        self.gets = []
        self.receipt = httpx.Response(200, json={'state':'complete', 'status':502,
            'response':{'detail':'resume timeout'}})

    async def probe(self, node):
        return self.snapshots[node]

    async def request(self, node_id, method, path, **kwargs):
        if method == 'GET':
            self.gets.append((node_id, path))
            return self.receipt
        self.posts.append((node_id, path, kwargs))
        await asyncio.sleep(.01)
        if self.lost_ack:
            raise GateError(503, 'command_outcome_unknown', 'lost ACK after rejection')
        return httpx.Response(502, content=self.body)


@pytest.mark.parametrize('body', [b'', b'{"detail":"resume timeout"}', b'opaque\xff'])
async def test_rejected_response_replays_exactly_after_restart(tmp_path, body):
    path = str(tmp_path/'gate.db')
    store, nodes = Store(path), RejectedNodes(body)
    routing = Routing(store, nodes)
    try:
        first = await routing.create(device('d'), 'sub', 'claude', {}, 10, request_id='one')
        assert first == (502, body)
        store.close()
        store = Store(path)
        routing = Routing(store, nodes)
        assert await routing.create(device('d'), 'sub', 'claude', {}, 10, request_id='one') == first
        assert len(nodes.posts) == 1 and not nodes.gets
        with pytest.raises(GateError) as conflict:
            await routing.create(device('d'), 'sub', 'claude', {'system':'changed'}, 10, request_id='one')
        assert conflict.value.code == 'idempotency_conflict'
        await routing.create(device('other-device'), 'sub', 'claude', {}, 10, request_id='one')
        assert len(nodes.posts) == 2  # иной владелец не получает чужой результат
    finally:
        store.close()


async def test_lost_negative_ack_reconciles_router_receipt_without_new_create(tmp_path):
    store, nodes = Store(str(tmp_path/'gate.db')), RejectedNodes(lost_ack=True)
    routing = Routing(store, nodes)
    try:
        with pytest.raises(GateError):
            await routing.create(device('d'), 'sub', 'claude', {}, 10, request_id='one')
        status, body = await routing.create(device('d'), 'sub', 'claude', {}, 10, request_id='one')
        assert status == 502 and json.loads(body) == {'detail':'resume timeout'}
        assert len(nodes.posts) == 1 and len(nodes.gets) == 1
        assert await routing.create(device('d'), 'sub', 'claude', {}, 10, request_id='one') == (status, body)
        assert len(nodes.gets) == 1
    finally:
        store.close()


@pytest.mark.parametrize('receipt', [
    httpx.Response(404), httpx.Response(410),
    httpx.Response(200, json={'state':'unknown','status':None,'response':None}),
    httpx.Response(200, json={'state':'complete','status':502,'response':None}),
    httpx.Response(200, json={'state':'expired','status':502,'response':None}),
])
async def test_legacy_rejected_without_proof_never_reexecutes(tmp_path, receipt):
    store, nodes = Store(str(tmp_path/'gate.db')), RejectedNodes()
    routing = Routing(store, nodes)
    try:
        await routing.create(device('d'), 'sub', 'claude', {}, 10, request_id='one')
        store._exec('UPDATE router_bindings SET rejection_status=NULL,rejection_body=NULL')
        nodes.receipt = receipt
        with pytest.raises(GateError) as unknown:
            await routing.create(device('d'), 'sub', 'claude', {}, 10, request_id='one')
        assert unknown.value.code == 'command_outcome_unknown'
        assert len(nodes.posts) == 1
        assert store._one('SELECT rejection_status FROM router_bindings')['rejection_status'] is None
    finally:
        store.close()


async def test_legacy_rejected_receipt_is_cached_only_on_explicit_retry(tmp_path):
    store, nodes = Store(str(tmp_path/'gate.db')), RejectedNodes()
    routing = Routing(store, nodes)
    try:
        await routing.create(device('d'), 'sub', 'claude', {}, 10, request_id='one')
        store._exec('UPDATE router_bindings SET rejection_status=NULL,rejection_body=NULL')
        await routing.reconcile()
        assert not nodes.gets
        status, body = await routing.create(device('d'), 'sub', 'claude', {}, 10, request_id='one')
        assert status == 502 and json.loads(body)['detail'] == 'resume timeout'
        assert len(nodes.posts) == 1 and len(nodes.gets) == 1
    finally:
        store.close()


def test_nullable_columns_migrate_existing_schema_without_changing_state(tmp_path):
    store = Store(str(tmp_path/'old.db'))
    try:
        store._db.executescript(DDL)
        for name in ('request_key', 'fingerprint', 'response', 'sdk_tools'):
            store._db.execute(f'ALTER TABLE router_bindings ADD COLUMN {name} TEXT')
        store._db.execute("INSERT INTO router_bindings(id,device_id,org,route,node_id,boot_id,model,roles,status,created,sdk_alias) VALUES ('old','d','org','sub','a','boot','claude','[]','rejected',0,'alias')")
        Routing(store, RejectedNodes())
        row = store._one("SELECT * FROM router_bindings WHERE id='old'")
        assert row['status'] == 'rejected' and row['rejection_status'] is None and row['rejection_body'] is None
    finally:
        store.close()


async def test_terminal_creation_headers_require_persisted_matching_receipt(tmp_path):
    store, nodes = Store(str(tmp_path/'gate.db')), RejectedNodes()
    routing = Routing(store, nodes)
    try:
        status, body = await routing.create(device('d'), 'sub', 'claude', {}, 10, request_id='one')
        expected = {'X-Atlas-Create-Outcome':'rejected', 'X-Atlas-Create-Key':'one'}
        assert routing.creation_headers(device('d'), 'one', status, body) == expected
        assert routing.creation_headers(device('other'), 'one', status, body) == {}
        other_org = device('d')
        other_org.org = 'other'
        assert routing.creation_headers(other_org, 'one', status, body) == {}
        assert routing.creation_headers(device('d'), 'other', status, body) == {}
        assert routing.creation_headers(device('d'), None, status, body) == {}
        assert routing.creation_headers(device('d'), 'one', 503, body) == {}
        assert routing.creation_headers(device('d'), 'one', status, b'changed') == {}
        store._exec("UPDATE router_bindings SET status='unknown'")
        assert routing.creation_headers(device('d'), 'one', status, body) == {}
    finally:
        store.close()
