"""#65: actual session HTTP wire, OpenAPI, replay and history aliases, no provider calls."""
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from atlas_gate.schemas import SessionCreated, SessionInfo, SessionList
from atlas_gate.sdk_tools import SdkTools
from atlas_gate.gate.errors import GateError
from .test_network_gate import network
from .test_tool_result_images import image_network


WIRE = json.loads((Path(__file__).parent / 'fixtures/contracts/session-wire-v085.json').read_text())
PREFIX = '/harness/agent/sessions'


def test_captured_production_ack_and_info_have_distinct_identity_contracts():
    ack = WIRE['gateCreateAck']
    assert ack['resumed'] is True and 'sdkSessionId' not in ack
    assert SessionCreated.model_validate(ack).model_dump(exclude_unset=True) == ack
    assert SessionInfo.model_validate(WIRE['routerSessionInfo']).model_dump(exclude_unset=True) == WIRE['routerSessionInfo']
    # Neither an absent flag nor a non-boolean value can prove strict resume.
    assert SessionCreated.model_validate({'id': 'fresh', 'tools': 0}).resumed is False
    assert SessionCreated(id='fresh', tools=0, resumed=False).resumed is False
    for invalid in ('true', 1, None):
        with pytest.raises(ValidationError):
            SessionCreated(id='bad', tools=0, resumed=invalid)


def _shape(schema):
    """Compare structural contracts, allowing Gate-specific explanatory descriptions."""
    if isinstance(schema, dict):
        return {k: _shape(v) for k, v in schema.items() if k not in ('title', 'description', 'additionalProperties')}
    if isinstance(schema, list):
        return [_shape(v) for v in schema]
    return schema


def _legacy_shape(schema):
    import copy
    schema=copy.deepcopy(schema)
    if 'configuredReasoning' in schema.get('properties',{}):
        extra=schema['properties'].pop('configuredReasoning')
        assert extra['anyOf'][0]['type']=='object'
        assert 'configuredReasoning' not in schema.get('required',[])
    return _shape(schema)


async def test_openapi_matches_captured_router_and_preserves_suspended_variant(network):
    client, app, routers, *_ = network
    gate = (await client.get('/openapi.json')).json()
    schemas = gate['components']['schemas']
    for method, name in (('post', 'SessionCreated'), ('get', 'SessionList')):
        response = gate['paths'][PREFIX][method]['responses']['200']['content']['application/json']['schema']
        assert response == {'$ref': '#/components/schemas/' + name}
    for name in ('SessionCreated', 'SessionInfo'):
        assert _legacy_shape(schemas[name]) == _shape(WIRE['routerSchemas'][name])
        for router in routers:
            actual = router.openapi()['components']['schemas'][name]
            assert _legacy_shape(actual) == _shape(WIRE['routerSchemas'][name])
    assert schemas['SessionCreated']['additionalProperties'] is True
    assert 'sdkSessionId' not in schemas['SessionCreated']['properties']
    assert schemas['SuspendedSessionInfo']['required'] == ['id', 'model', 'state']
    variants = schemas['SessionList']['properties']['sessions']['items']['anyOf']
    assert {'$ref': '#/components/schemas/SessionInfo'} in variants
    assert {'$ref': '#/components/schemas/SuspendedSessionInfo'} in variants


@pytest.mark.parametrize('provider', ['codex', 'claude'])
async def test_create_list_resume_and_replay_direct_and_connector(image_network, monkeypatch, provider):
    # Real Gate, Router, journal, HTTP and (for connector) channel queue/executor.
    # Only the vendor backend is deterministic; no credentials or paid inference.
    from atlas_router.media import ImageApi
    from atlas_router.backends.stub import StubBackend

    client, app, routers, _, creds, other = image_network
    state = app.state.gate
    async def authenticated():
        return {'loggedIn': True}
    async def image_capable():
        return True
    monkeypatch.setattr(ImageApi, 'ready', property(lambda self: True))
    for router in routers:
        monkeypatch.setattr(router.state.backend, 'name', provider)
        monkeypatch.setattr(router.state.backend, 'image_capabilities', image_capable, raising=False)
        monkeypatch.setattr(router.state.cli, 'auth_status', authenticated)
        monkeypatch.setattr(router.state.files, 'available', lambda: True)
    state.orgs['test-org'].sdk_tools = SdkTools(webSearch='live', files=True, imageGeneration=True)
    base_tools = {'version': 1, 'webSearch': 'live', 'files': True}
    payload = {'model': 'stub-fast', 'tools': [], 'sdkTools': base_tools}
    sent_creates = []
    request = state.nodes.request

    async def extensions(node, method, path, **kwargs):
        response = await request(node, method, path, **kwargs)
        if method == 'POST' and path == '/v1/sessions':
            sent_creates.append(json.loads(kwargs['content']))
            if response.status_code == 200:
                doc = response.json()
                doc['futureExtension'] = {'keep': [1, None, False]}
                return httpx.Response(200, json=doc)
        if method == 'GET' and path == '/v1/sessions' and response.status_code == 200:
            doc = response.json()
            for info in doc['sessions']:
                info['futureInfo'] = {'keep': True}
            return httpx.Response(200, json=doc)
        return response

    monkeypatch.setattr(state.nodes, 'request', extensions)
    first = await client.post(PREFIX, json=payload, headers={'Idempotency-Key': 'fresh'})
    assert first.status_code == 200, first.text
    assert first.json()['resumed'] is False and 'sdkSessionId' not in first.json()
    assert first.json()['sdkTools'] == base_tools
    assert SessionCreated.model_validate(first.json()).model_dump(exclude_unset=True) == first.json()
    assert (await client.post(PREFIX, json=payload, headers={'Idempotency-Key': 'fresh'})).content == first.content
    assert len(sent_creates) == 1
    old_sid = first.json()['id']
    listing = await client.get(PREFIX)
    assert listing.status_code == 200, listing.text
    old_info = next(x for x in listing.json()['sessions'] if x['id'] == old_sid)
    assert old_info['futureInfo'] == {'keep': True}
    binding = state.store._one('SELECT * FROM router_bindings WHERE id=?', (old_sid,))
    native = binding['sdk_id']
    assert old_info['sdkSessionId'] == binding['sdk_alias'] != native
    translated = state.routing.translate(binding, {'type': 'session', 'sessionId': native, 'id': 'test-event'})
    assert translated['sessionId'] == binding['sdk_alias']
    assert native not in listing.text
    assert (await client.get(PREFIX, headers={'Authorization': 'Bearer ' + other['device_token']})).json() == {'sessions': []}

    # Upgrade tools using the same native SDK history, as the EDT #209 incident.
    assert (await client.delete(PREFIX + '/' + old_sid, headers={'Idempotency-Key': 'close-old'})).status_code == 200
    changed_tools = dict(base_tools, imageGeneration=True)
    upgraded = dict(payload, sdkTools=changed_tools, resumeSessionId=old_info['sdkSessionId'])
    resumed = await client.post(PREFIX, json=upgraded, headers={'Idempotency-Key': 'upgrade'})
    assert resumed.status_code == 200, resumed.text
    ack = resumed.json()
    assert ack['resumed'] is True and ack['resumeError'] is None
    assert 'sdkSessionId' not in ack and ack['sdkTools'] == changed_tools
    assert ack['futureExtension'] == {'keep': [1, None, False]}
    assert sent_creates[-1]['resumeSessionId'] == native
    again = await client.post(PREFIX, json=upgraded, headers={'Idempotency-Key': 'upgrade'})
    assert again.content == resumed.content and len(sent_creates) == 2
    conflict = await client.post(PREFIX, json=dict(upgraded, system='changed'), headers={'Idempotency-Key': 'upgrade'})
    assert conflict.status_code == 409 and conflict.json()['error']['code'] == 'idempotency_conflict'
    listing = await client.get(PREFIX)
    new_binding = state.store._one('SELECT * FROM router_bindings WHERE id=?', (ack['id'],))
    info = next(x for x in listing.json()['sessions'] if x['id'] == ack['id'])
    assert info['sdkSessionId'] == new_binding['sdk_alias'] != native
    assert new_binding['sdk_id'] == native and info['resumed'] is True and info['turns'] == 0
    assert SessionList.model_validate(listing.json()).model_dump(exclude_unset=True) == listing.json()

    async def unavailable(node, method, path, **kwargs):
        if method == 'GET' and path == '/v1/sessions':
            raise GateError(503, 'node_unavailable', 'test offline')
        return await extensions(node, method, path, **kwargs)
    monkeypatch.setattr(state.nodes, 'request', unavailable)
    suspended = await client.get(PREFIX)
    assert suspended.status_code == 200, suspended.text
    assert suspended.json() == {'sessions': [{'id': ack['id'], 'model': 'stub-fast',
        'state': 'session_suspended', 'sdkSessionId': new_binding['sdk_alias']}]}
    # No inferred live fields and no added inference, including all retries.
    assert native not in suspended.text
    assert state.store._one('SELECT count(*) n FROM router_operations WHERE turn_id IS NOT NULL')['n'] == 0
    assert all(not e.buffers for router in routers for e in router.state.registry._entries.values())
    assert StubBackend.memories[native] == []


async def test_legacy_create_without_resume_flag_is_not_promoted(image_network, monkeypatch):
    client, app, routers, *_ = image_network
    request = app.state.gate.nodes.request
    async def legacy_ack(node, method, path, **kwargs):
        response = await request(node, method, path, **kwargs)
        if method == 'POST' and path == '/v1/sessions' and response.status_code == 200:
            doc = response.json()
            del doc['resumed']
            return httpx.Response(200, json=doc)
        return response
    monkeypatch.setattr(app.state.gate.nodes, 'request', legacy_ack)
    payload = {'model': 'stub-fast', 'tools': [], 'sdkTools': {}}
    headers = {'Idempotency-Key': 'legacy-ack'}
    first = await client.post(PREFIX, json=payload, headers=headers)
    assert first.status_code == 200, first.text
    assert 'resumed' not in first.json() and 'sdkSessionId' not in first.json()
    assert SessionCreated.model_validate(first.json()).resumed is False
    assert (await client.post(PREFIX, json=payload, headers=headers)).content == first.content
    assert sum(len(router.state.registry) for router in routers) == 1
    assert all(not e.buffers for router in routers for e in router.state.registry._entries.values())


async def test_lost_create_ack_reconciles_without_another_open_or_prompt(image_network, monkeypatch):
    client, app, routers, *_ = image_network
    state = app.state.gate
    request = state.nodes.request
    creates = []
    async def lose_ack(node, method, path, **kwargs):
        response = await request(node, method, path, **kwargs)
        if method == 'POST' and path == '/v1/sessions':
            creates.append(path)
            raise GateError(503, 'command_outcome_unknown', 'test lost success ACK')
        return response
    monkeypatch.setattr(state.nodes, 'request', lose_ack)
    payload = {'model': 'stub-fast', 'tools': [], 'sdkTools': {}}
    headers = {'Idempotency-Key': 'lost-once'}
    lost = await client.post(PREFIX, json=payload, headers=headers)
    assert lost.status_code == 503 and lost.json()['error']['code'] == 'command_outcome_unknown'
    restored = await client.post(PREFIX, json=payload, headers=headers)
    assert restored.status_code == 200, restored.text
    assert 'sdkSessionId' not in restored.json() and restored.json()['resumed'] is False
    assert SessionCreated.model_validate(restored.json()).model_dump(exclude_unset=True) == restored.json()
    assert (await client.post(PREFIX, json=payload, headers=headers)).content == restored.content
    assert len(creates) == 1
    assert sum(len(router.state.registry) for router in routers) == 1
    assert all(not e.buffers for router in routers for e in router.state.registry._entries.values())
