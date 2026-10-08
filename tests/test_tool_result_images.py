"""Negotiated image tool results through real Gate/Router HTTP, no provider inference."""
import asyncio
import base64
import json
import httpx
from types import SimpleNamespace

import pytest

from atlas_gate.gate.attachments import node_attachments, route_attachments, validate_tool_result
from atlas_gate.gate.schemas import GateAgentSessionCreate
from atlas_gate.schemas import SessionCreate, ToolResultRequest
from atlas_gate.gate.errors import GateError
from .test_network_gate import network, events


def test_tool_result_images_negotiation_is_strict_and_preserved():
    for schema in (SessionCreate, GateAgentSessionCreate):
        assert schema.model_validate({}).toolResultImages is False
        assert schema.model_validate({'toolResultImages': True}).toolResultImages is True
        for invalid in ('true', 1, None):
            with pytest.raises(ValueError):
                schema.model_validate({'toolResultImages': invalid})
    source = SessionCreate(toolResultImages=True, model='model')
    assert GateAgentSessionCreate.model_validate(source.model_dump()).toolResultImages is True


def test_node_and_signed_route_intersection_do_not_promote_unknown_support():
    caps = dict(schemaVersion=1, text=True, image=True, pdfNative=True,
                documentPages=True, agentDocumentTools=False, maxFiles=8,
                maxRawBytes=15000000, maxTotalBytes=20000000,
                maxTextChars=None, maxPdfPages=500, maxPagePixels=4000000)
    assert node_attachments({'attachments': caps})['toolResultImages'] is None
    state = SimpleNamespace(nodes=SimpleNamespace(
        config={n: SimpleNamespace(enabled=True, orgs=['org'], routes=['route'], node_id=n) for n in ('a', 'b')},
        revoked=lambda _: False, catalog={n: [{'value': 'model'}] for n in ('a', 'b')},
        snapshots={n: {'attachments': dict(caps, toolResultImages=True)} for n in ('a', 'b')}))
    assert route_attachments(state, 'org', 'route', 'model')['toolResultImages'] is True
    state.nodes.snapshots['b']['attachments'].pop('toolResultImages')
    assert route_attachments(state, 'org', 'route', 'model')['toolResultImages'] is None
    state.nodes.snapshots['b']['attachments']['toolResultImages'] = False
    assert route_attachments(state, 'org', 'route', 'model')['toolResultImages'] is False


def test_missing_capability_refuses_media_but_preserves_text():
    image = ToolResultRequest(callId='call', attachments=[{'kind': 'image', 'mediaType': 'image/png', 'data': 'YQ=='}])
    for caps in (None, {}, {'toolResultImages': None}, {'toolResultImages': False}):
        with pytest.raises(GateError) as failed:
            validate_tool_result(image, caps)
        assert failed.value.code == 'unsupported_media'
        validate_tool_result(ToolResultRequest(callId='call', content='legacy'), caps)


@pytest.fixture(params=['direct', 'connector'])
async def image_network(network, request):
    if request.param == 'direct':
        yield network
        return
    from atlas_gate.gate.node_channel import ChannelClient
    from atlas_router.connector import Connector
    state = network[1].state.gate
    workers, connections = [], []
    try:
        for node_id, node in state.nodes.config.items():
            connector = object.__new__(Connector)
            connector.identity = {'nodeId': node_id}
            connector.router = httpx.AsyncClient(base_url=node.url, headers={'X-Atlas-Token': 'test-router'})
            connections.append(connector.router)
            await state.nodes.clients[node_id].aclose()
            state.nodes.clients[node_id] = ChannelClient(state.channel, node_id)
            async def pump(n=node_id, c=connector):
                while True:
                    work = await state.channel.poll(n, wait=.1)
                    if work:
                        result = await c.execute(work)
                        state.channel.complete(n, work['id'], result['status'], base64.b64decode(result['body']))
            workers.append(asyncio.create_task(pump()))
        yield network
        for task in workers:
            if task.done():
                task.result()
    finally:
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        for client in connections:
            await client.aclose()


async def test_real_http_images_negotiation_proxy_and_text_compatibility(image_network, monkeypatch):
    from pydantic import BaseModel, Field
    from atlas_router.backends.stub import StubSession
    from atlas_gate.gate.crypto import verify_profile

    client, app, routers, settings, creds, other = image_network
    captured = []
    original = StubSession.tool_result

    async def receive(self, call_id, content, is_error=False, *, attachments=None):
        captured.append({'callId': call_id, 'content': content, 'isError': is_error, 'attachments': attachments})
        return await original(self, call_id, content, is_error)

    monkeypatch.setattr(StubSession, 'tool_result', receive)
    # Scenario backend keeps deterministic inference, reports the same fixed-node
    # media support as Claude; this tests actual HTTP schemas/proxy, not the SDK.
    async def logged_in():
        return {'loggedIn': True}

    for router in routers:
        monkeypatch.setattr(router.state.backend, 'name', 'claude')
        monkeypatch.setattr(router.state.cli, 'auth_status', logged_in)
    await app.state.gate.reload()
    config = (await client.get('/harness/settings')).json()
    profile = verify_profile(config['profile_jws'], creds['profile_signing_key'])
    assert any((m.get('attachments') or {}).get('toolResultImages') is True for m in profile['models'])

    payload = {'system': 'test', 'model': 'stub-fast', 'toolResultImages': True,
               'tools': [{'name': 'echo', 'description': 'echo', 'parameters': {'type': 'object'}}]}
    created = await client.post('/harness/agent/sessions', json=payload)
    assert created.status_code == 200, created.text
    assert created.json()['toolResultImages'] is True
    sid = created.json()['id']
    caps = (await client.get(f'/harness/agent/sessions/{sid}/attachments')).json()['attachments']
    assert caps['toolResultImages'] is True
    prompt = await client.post(f'/harness/agent/sessions/{sid}/prompt?mode=async', json={'id': 'image', 'text': 'call:echo'})
    assert prompt.status_code == 202, prompt.text
    call = next(e for e in await events(client, sid, 'image', False) if e['type'] == 'tool_call')
    image = {'kind': 'image', 'filename': 'page.png', 'mediaType': 'image/png', 'data': 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jMZkAAAAASUVORK5CYII=', 'sizeBytes': 1}
    valid = {'callId': call['callId'], 'content': 'page identity', 'isError': False, 'attachments': [image]}
    for attachment in (dict(image, kind='pdf'), dict(image, data='bad-base64'), dict(image, mediaType='text/html')):
        refused = await client.post(f'/harness/agent/sessions/{sid}/tool_result', json=dict(valid, attachments=[attachment]))
        assert refused.status_code == 422, refused.text
        assert refused.json()['error']['code'] == 'attachment_invalid'
        assert captured == []
    refused = await client.post(f'/harness/agent/sessions/{sid}/tool_result', json=dict(valid, attachments=[image] * (caps['maxFiles'] + 1)))
    assert refused.status_code == 422 and refused.json()['error']['code'] == 'attachment_limit'
    assert captured == []
    accepted = await client.post(f'/harness/agent/sessions/{sid}/tool_result', json=valid)
    assert accepted.status_code == 200, accepted.text
    assert captured == [valid]
    await events(client, sid, 'image', True)

    class OldMiddleCreate(BaseModel):
        system: str
        model: str
        tools: list[dict] = Field(default_factory=list)

    old_payload = OldMiddleCreate.model_validate(payload).model_dump()
    assert 'toolResultImages' not in old_payload
    old_created = await client.post('/harness/agent/sessions', json=old_payload)
    assert old_created.status_code == 200, old_created.text
    assert old_created.json()['toolResultImages'] is False
    old_sid = old_created.json()['id']
    assert (await client.get(f'/harness/agent/sessions/{old_sid}/attachments')).json()['attachments']['toolResultImages'] is False
    # Global node support is still true; the bound negotiation controls media.
    assert all(node_attachments(s)['toolResultImages'] for s in app.state.gate.nodes.snapshots.values())
    started = await client.post(f'/harness/agent/sessions/{old_sid}/prompt?mode=async', json={'id': 'old', 'text': 'call:echo'})
    assert started.status_code == 202
    old_call = next(e for e in await events(client, old_sid, 'old', False) if e['type'] == 'tool_call')
    refused = await client.post(f'/harness/agent/sessions/{old_sid}/tool_result', json=dict(valid, callId=old_call['callId']))
    assert refused.status_code == 422 and refused.json()['error']['code'] == 'unsupported_media'
    assert len(captured) == 1
    text = {'callId': old_call['callId'], 'content': 'exact legacy text', 'isError': True}
    accepted = await client.post(f'/harness/agent/sessions/{old_sid}/tool_result', content=json.dumps(text).encode(), headers={'Content-Type': 'application/json'})
    assert accepted.status_code == 200, accepted.text
    assert captured[-1] == dict(text, attachments=None)
    await events(client, old_sid, 'old', True)
