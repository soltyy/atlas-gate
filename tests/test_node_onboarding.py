import json

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException
from cryptography.hazmat.primitives.asymmetric import ed25519

from atlas_gate.gate.nodes import Nodes
from .test_discovery import configured  # noqa: F401 — изолированный Gate через настоящий HTTP
from .test_network_gate import serve
from .test_node_security import csr

AUTH = {"X-Atlas-Token": "test-admin-secret"}


async def body(http, **changes):
    opts = (await http.get('/harness/admin/node-options', headers=AUTH)).json()
    return dict(revision=opts['revision'], node_id='office-a', backend='codex',
                transport='connector', org='first', routes=['private-route'],
                account_group='new-account', account_turn_capacity=0, **changes)


async def test_connector_create_approval_restart_conflicts_and_no_secrets(configured):
    http, state, _ = configured
    doc = await body(http)
    assert (await http.post('/harness/admin/nodes', json=doc)).status_code == 401
    assert (await http.post('/harness/admin/nodes', headers=AUTH, json=doc)).status_code == 201
    stale = dict(doc, node_id='b')
    assert (await http.post('/harness/admin/nodes', headers=AUTH, json=stale)).status_code == 409
    duplicate = await body(http)
    assert (await http.post('/harness/admin/nodes', headers=AUTH, json=duplicate)).status_code == 409
    same_group = await body(http)
    same_group['node_id'] = 'office-b'
    assert (await http.post('/harness/admin/nodes', headers=AUTH, json=same_group)).status_code == 409
    same_group.update(share_account=True, account_turn_capacity=8)
    assert (await http.post('/harness/admin/nodes', headers=AUTH, json=same_group)).status_code == 422
    same_group['account_turn_capacity'] = 0
    assert (await http.post('/harness/admin/nodes', headers=AUTH, json=same_group)).status_code == 201
    invite = (await http.post('/harness/admin/nodes/office-a/invite', headers=AUTH)).json()['invitation']
    pem = csr(ed25519.Ed25519PrivateKey.generate())
    assert (await http.post('/node/enroll', json={'invitation': invite, 'csr': pem})).status_code == 200
    pending = (await http.get('/harness/admin/node-enrollments', headers=AUTH)).json()['enrollments']
    assert pending[0]['fingerprint']
    assert (await http.post('/harness/admin/node-enrollments/'+pending[0]['id']+'/approve', headers=AUTH)).status_code == 200
    assert (await http.post('/node/enroll/poll', json={'invitation': invite})).json()['state'] == 'approved'
    public = await http.get('/harness/admin/nodes', headers=AUTH)
    assert invite not in public.text and pem not in public.text
    assert public.json()['nodes'][0]['check_error'].startswith('Ожидается')
    restarted = Nodes(state.settings.ATLAS_GATE_NODES_FILE, state.store, state.channel)
    assert restarted.config['office-a'].account_turn_capacity == 0
    await restarted.close()


async def test_direct_checks_identity_auth_provider_and_persists(configured):
    http, state, _ = configured
    node = FastAPI()
    identity = {'nodeId': 'office-a', 'backend': 'codex', 'protocolVersion': 1,
                'bootId': 'boot', 'capacity': 0, 'turnCapacity': 0, 'ready': True,
                'sessions': 8, 'reservedUnits': 6, 'models': [{'value': 'gpt-6.1-sol'}],
                'capabilities': ['durable_commands', 'durable_events']}
    @node.get('/v1/node')
    async def info(x_atlas_token: str = Header('')):
        if x_atlas_token != 'new-private-secret':
            raise HTTPException(401)
        return identity
    async with serve(node) as url:
        doc = await body(http)
        doc.update(transport='direct', url=url, token='wrong')
        bad = await http.post('/harness/admin/nodes', headers=AUTH, json=doc)
        assert bad.status_code == 422 and 'ключ' in bad.text
        assert not state.nodes.config
        doc['token'] = 'new-private-secret'
        identity['nodeId'] = 'different'
        assert (await http.post('/harness/admin/nodes', headers=AUTH, json=doc)).status_code == 422
        identity['nodeId'] = 'office-a'; identity['backend'] = 'claude'
        assert (await http.post('/harness/admin/nodes', headers=AUTH, json=doc)).status_code == 422
        identity['backend'] = 'codex'
        added = await http.post('/harness/admin/nodes', headers=AUTH, json=doc)
        assert added.status_code == 201, added.text
        assert added.json()['status']['sessions'] == 8
        raw = (await http.get('/harness/admin/nodes', headers=AUTH)).text
        assert 'new-private-secret' not in raw
        from atlas_gate.gate.state import read_keys_file
        recovered = Nodes(state.settings.ATLAS_GATE_NODES_FILE,
                          tokens=read_keys_file(state.settings.ATLAS_GATE_NODE_ENV_FILE))
        assert (await recovered.probe('office-a'))['reservedUnits'] == 6
        await recovered.close()
        external = json.loads(open(state.settings.ATLAS_GATE_NODES_FILE).read())
        external[0]['enabled'] = False
        from pathlib import Path
        Path(state.settings.ATLAS_GATE_NODES_FILE).write_text(json.dumps(external))
        assert (await http.get('/harness/admin/node-options', headers=AUTH)).status_code == 409
        assert (await http.post('/harness/admin/nodes', headers=AUTH, json=doc)).status_code == 409


@pytest.mark.parametrize('change', [
    {'routes': ['private-api']}, {'org': 'archived'}, {'account_group': '../escape'},
    {'transport': 'direct', 'url': 'http://other-host:8765', 'token': 'secret'},
    {'transport': 'connector', 'token': 'unexpected'},
])
async def test_invalid_registration_leaves_existing_state_untouched(configured, change):
    http, state, _ = configured
    doc = await body(http)
    doc.update(change)
    assert (await http.post('/harness/admin/nodes', headers=AUTH, json=doc)).status_code == 422
    assert not state.nodes.config
