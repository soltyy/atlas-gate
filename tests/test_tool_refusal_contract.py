"""Keep the Router callback outcome for both public client parsers."""
import json

import httpx
import pytest

from atlas_gate.gate.agent import _response
from .test_network_gate import create, network  # noqa: F401


async def test_http_tool_refusal_and_command_receipt_keep_structured_outcome(network):
    client, app, *_ = network
    sid = await create(client)
    prefix = '/harness/agent/sessions/' + sid
    response = await client.post(prefix+'/tool_result', json={'callId':'unknown-call','content':'saved'},
                                 headers={'Idempotency-Key':'original-result'})
    assert response.status_code == 409
    doc = response.json()
    assert doc['status'] == doc['error']['status'] == 'unknown_call'
    assert doc['callId'] == doc['error']['callId'] == 'unknown-call'
    receipt = await client.get(prefix+'/commands', params={'requestId':'original-result'})
    assert receipt.content == response.content
    assert receipt.headers['X-Atlas-Command-State'] == 'complete'


@pytest.mark.parametrize('status', ['answered', 'unknown_call', 'turn_not_waiting'])
def test_known_callback_status_is_additive_for_both_legacy_clients(status):
    result = _response(httpx.Response(409, json={'detail':'refused', 'callId':'call',
        'status':status, 'untrustedExtra':'discard'}))
    doc = json.loads(result.body)
    assert doc == {'error':{'code':'conflict','message':'refused','callId':'call','status':status},
                   'status':status,'callId':'call'}


@pytest.mark.parametrize('fields', [
    {'callId':'call','status':'accepted'}, {'callId':None,'status':'answered'},
    {'callId':'','status':'answered'}, {'callId':'x'*201,'status':'answered'},
])
def test_unknown_or_invalid_callback_metadata_does_not_invent_outcome(fields):
    doc = json.loads(_response(httpx.Response(409,json={'detail':'refused',**fields})).body)
    assert doc == {'error':{'code':'conflict','message':'refused'}}
