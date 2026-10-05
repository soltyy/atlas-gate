"""Настоящий HTTP Gate без Router/SDK: discovery, enrollment, подписанные settings и reload."""
import json

import httpx
import pytest

from atlas_gate.main import create_app
from atlas_gate.settings import Settings
from atlas_gate.gate.crypto import verify_profile
from .test_network_gate import serve


@pytest.fixture
async def configured(tmp_path, monkeypatch):
    async def unavailable_models(self):
        return None
    monkeypatch.setattr('atlas_gate.gate.state.GateState._agent_model_ids', unavailable_models)
    orgs = tmp_path / 'orgs'
    orgs.mkdir()
    first = {'id': 'first', 'name': 'Private first org',
             'routes': [{'id': 'private-route', 'kind': 'router-agent', 'protocol': 'atlas-agent'},
                        {'id': 'private-api', 'kind': 'gateway', 'protocol': 'openai',
                         'upstream_url': 'https://private-upstream.invalid/v1', 'key_env': 'PRIVATE_KEY_NAME'}],
             'models': [{'route_id': 'private-route', 'model': 'gpt-6.1-sol', 'context_window': 272000},
                        {'route_id': 'private-api', 'model': 'api-model', 'max_output': 128000}],
             'agents': [{'id': 'assistant', 'name': 'Private agent', 'version': '1',
                         'system_prompt': 'Private prompt', 'default': True, 'settings': {'temperature': .2},
                         'extension_setting': {'retained': True}}],
             'mcp': [{'id': 'private-mcp', 'url': 'https://private-mcp.invalid', 'headers_ref': 'MCP_HEADER_NAME'}],
             'feature_flags': {'private_feature': True}}
    second = dict(first, id='second', name='Private second org', models=[
        {'route_id': 'private-route', 'model': 'gpt-6.1-sol', 'context_window': 872000},
        {'route_id': 'private-route', 'model': 'disabled-model', 'enabled': False}])
    archived = dict(second, id='archived', archived=True, models=[
        {'route_id': 'private-route', 'model': 'archived-model'}])
    for org in (first, second, archived):
        (orgs / (org['id'] + '.json')).write_text(json.dumps(org), encoding='utf-8')
    settings = Settings(_env_file=None, ATLAS_TOKEN='test-admin-secret',
        ATLAS_GATE_KEYRING_SECRET='test-keyring-secret', ATLAS_GATE_DB=str(tmp_path/'gate.db'),
        ATLAS_GATE_KEY_PATH=str(tmp_path/'key.pem'), ATLAS_GATE_ORGS_DIR=str(orgs),
        ATLAS_GATE_KEYS_FILE=str(tmp_path/'no-keys'), ATLAS_GATE_NODES_FILE=str(tmp_path/'no-nodes'),
        ATLAS_GATE_NODE_ENV_FILE=str(tmp_path/'no-node-env'), ATLAS_GATE_NODE_PKI_DIR=str(tmp_path/'pki'))
    app = create_app(settings)
    async with serve(app) as url, httpx.AsyncClient(base_url=url, timeout=10) as http:
        await app.state.gate.ready.wait()
        yield http, app.state.gate, orgs


async def enroll(http, org='first'):
    start = (await http.post('/harness/enroll/start', json={
        'device_name': 'test', 'platform': 'test', 'app_version': '1'})).json()
    approve = await http.post('/harness/enroll/approve', headers={'X-Atlas-Token':'test-admin-secret'},
        json={'user_code':start['user_code'], 'org':org, 'user':{'id':'test-user'}})
    assert approve.status_code == 200, approve.text
    credentials = (await http.post('/harness/enroll/poll', json={'device_code':start['device_code']})).json()
    return credentials


async def test_public_catalog_all_models_variants_and_no_private_settings(configured):
    http, state, orgs = configured
    response = await http.get('/harness/discovery')
    assert response.status_code == 200, response.text
    doc = response.json()
    assert doc['schema_version'] == 1
    assert sorted(m['context_window'] for m in doc['models'] if m['model']=='gpt-6.1-sol') == [272000, 872000]
    assert len({m['catalog_id'] for m in doc['models']}) == len(doc['models'])
    assert any(m['model']=='api-model' and m['max_output']==128000 for m in doc['models'])
    assert any(m['model']=='disabled-model' and not m['enabled'] and m['context_window'] is None for m in doc['models'])
    assert all(m['model']!='archived-model' for m in doc['models'])
    for forbidden in ('private-', 'Private ', 'PRIVATE_KEY_NAME', 'MCP_HEADER_NAME', 'test-admin-secret', 'test-keyring-secret'):
        assert forbidden not in response.text
    assert (await http.get(doc['endpoints']['settings'])).status_code == 401
    cached = await http.get('/harness/discovery', headers={'If-None-Match':'"other", W/'+response.headers['etag']})
    assert cached.status_code == 304 and not cached.content
    openapi = (await http.get('/openapi.json')).json()
    schemas = openapi['components']['schemas']
    assert 'Discovery' in schemas and 'HarnessSettings' in schemas and 'ClientProfile' in schemas
    assert openapi['paths']['/harness/settings']['get']['security'] == [{'DeviceToken':[]}]
    assert not openapi['paths']['/harness/discovery']['get'].get('security')


async def test_settings_are_exact_signed_profile_scoped_and_reloadable(configured):
    http, state, orgs = configured
    first = await enroll(http)
    second = await enroll(http, 'second')
    auth = {'Authorization':'Bearer '+first['device_token']}
    response = await http.get('/harness/settings', headers=auth)
    assert response.status_code == 200, response.text
    doc = response.json()
    payload = verify_profile(doc['profile_jws'], first['profile_signing_key'])
    assert doc['profile'] == payload
    legacy = await http.get('/harness/profile', headers=auth)
    assert doc['profile_jws'] == legacy.text
    assert doc['profile']['agents'][0]['extension_setting'] == {'retained':True}
    assert doc['profile']['org']['id'] == 'first'
    assert doc['profile']['models'][0]['context_window'] == 272000
    assert doc['profile']['mcp'][0]['id'] == 'private-mcp'
    assert 'private, no-cache' == response.headers['cache-control']
    assert 'Authorization' in response.headers['vary']
    assert 'PRIVATE_KEY_NAME' not in response.text and 'private-upstream' not in response.text
    cached = await http.get('/harness/settings', headers=dict(auth, **{'If-None-Match':response.headers['etag']}))
    assert cached.status_code == 304
    other = await http.get('/harness/settings?org=first', headers={
        'Authorization':'Bearer '+second['device_token'], 'If-None-Match':response.headers['etag']})
    assert other.status_code == 200 and other.json()['profile']['org']['id'] == 'second'
    before = (await http.get('/harness/discovery')).headers['etag']
    file = orgs/'first.json'
    changed = json.loads(file.read_text())
    changed['models'][0]['context_window'] = 400000
    changed['agents'][0]['settings']['temperature'] = .7
    file.write_text(json.dumps(changed), encoding='utf-8')
    assert (await http.post('/harness/admin/reload', headers={'X-Atlas-Token':'test-admin-secret'})).status_code == 200
    updated = await http.get('/harness/settings', headers=dict(auth, **{'If-None-Match':response.headers['etag']}))
    assert updated.status_code == 200
    assert updated.json()['profile']['profile_version'] > payload['profile_version']
    assert updated.json()['profile']['agents'][0]['settings']['temperature'] == .7
    assert (await http.get('/harness/discovery', headers={'If-None-Match':before})).status_code == 200
    await state.reload()
    assert (await http.get('/harness/settings', headers=dict(auth, **{'If-None-Match':updated.headers['etag']}))).status_code == 304


@pytest.mark.parametrize('denial', ['invalid', 'expired', 'revoked', 'archived'])
async def test_settings_reject_invalid_access(configured, denial):
    http, state, orgs = configured
    credentials = await enroll(http)
    token = credentials['device_token']
    if denial == 'invalid':
        token = 'invalid'
    elif denial == 'expired':
        state.store._exec('UPDATE tokens SET expires_at=0 WHERE device_id=?', (credentials['device_id'],))
    elif denial == 'revoked':
        state.store._exec('UPDATE devices SET revoked_at=1 WHERE device_id=?', (credentials['device_id'],))
    else:
        state.orgs['first'].archived = True
    response = await http.get('/harness/settings', headers={'Authorization':'Bearer '+token, 'If-None-Match':'*'})
    assert response.status_code == (403 if denial=='archived' else 401), response.text


async def test_settings_and_etag_survive_gate_restart(configured):
    http, state, orgs = configured
    credentials = await enroll(http)
    auth = {'Authorization':'Bearer '+credentials['device_token']}
    before = await http.get('/harness/settings', headers=auth)
    public_before = await http.get('/harness/discovery')
    await state.close()
    restarted = create_app(state.settings)
    async with serve(restarted) as url, httpx.AsyncClient(base_url=url, headers=auth) as client:
        await restarted.state.gate.ready.wait()
        after = await client.get('/harness/settings')
        assert after.json() == before.json()
        assert after.headers['etag'] == before.headers['etag']
        assert (await client.get('/harness/discovery')).headers['etag'] == public_before.headers['etag']
