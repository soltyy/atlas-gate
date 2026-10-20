import base64
import hashlib
import io
import json

import httpx

import pytest

from atlas_gate.sdk_tools import SdkTools, defaults, permitted, supports
from .test_network_gate import network, events
from .test_tool_result_images import image_network


def test_image_policy_and_legacy_node_do_not_promote_support():
    requested = SdkTools(imageGeneration=True)
    assert not permitted(requested, SdkTools())
    assert permitted(requested, requested)
    assert not supports({}, requested)
    assert supports({'imageGeneration': {'supported':True}}, requested)
    assert defaults({}).imageGeneration is False
    assert defaults({'imageGeneration':{'supported':True}}, requested).imageGeneration
    assert 'imageGeneration' not in SdkTools().model_dump()


def test_responses_stream_usage_is_read_from_terminal_response():
    from atlas_gate.gate.upstream import UsageTap
    tap=UsageTap('openai')
    event={'type':'response.completed','response':{'usage':{'input_tokens':42,'output_tokens':17,
        'input_tokens_details':{'cached_tokens':10}}}}
    raw=('data: '+json.dumps(event)+'\n\n').encode()
    tap.feed(raw[:20]);tap.feed(raw[20:])
    assert tap.usage=={'prompt':42,'completion':17,'cached':10}


async def test_generated_media_events_and_bytes_through_owned_binding(image_network, monkeypatch):
    from PIL import Image
    from atlas_router.backends.stub import StubSession
    client, app, routers, settings, creds, other = image_network
    buf=io.BytesIO(); Image.new('RGB',(8,8),'green').save(buf,format='PNG')
    encoded=base64.b64encode(buf.getvalue()).decode()

    async def generated(self, turn_id, text, attachments=None):
        doc=self.media.save(self._media_scope, encoded, call_id='image-call', turn_id=turn_id, source='test')
        yield {'type':'media_artifact','id':turn_id,'artifact':doc}
        yield {'type':'sdk_tool_completed','id':turn_id,'callId':'image-call','name':'image_generation','executor':'sdk','artifact':doc}
        yield {'type':'result','id':turn_id,'ok':True,'text':'image ready'}
    monkeypatch.setattr(StubSession,'prompt',generated)
    response=await client.post('/harness/agent/sessions',json={'model':'stub-fast','sdkTools':{}})
    assert response.status_code==200,response.text
    sid=response.json()['id'];path='/harness/agent/sessions/'+sid
    response=await client.post(path+'/prompt?mode=async',json={'id':'draw','text':'draw'})
    assert response.status_code==202,response.text
    received=await events(client,sid,'draw',True)
    artifact=next(e['artifact'] for e in received if e['type']=='media_artifact')
    app.state.gate.store._exec('UPDATE router_bindings SET boot_id=? WHERE id=?',('previous-boot',sid))
    assert not any(e['type']=='tool_call' for e in received)
    assert (await client.get(path+'/media')).json()['artifacts']==[artifact]
    result=await client.get(path+'/media/'+artifact['id'])
    assert result.status_code==200,result.text
    assert hashlib.sha256(base64.b64decode(result.json()['data'])).hexdigest()==artifact['sha256']
    denied=await client.get(path+'/media/'+artifact['id'],headers={'Authorization':'Bearer '+other['device_token']})
    assert denied.status_code==404
    other_session=(await client.post('/harness/agent/sessions',json={'model':'stub-fast','sdkTools':{}})).json()['id']
    assert (await client.get('/harness/agent/sessions/'+other_session+'/media/'+artifact['id'])).status_code==404
    replay=await events(client,sid,'draw',True)
    assert next(e['artifact'] for e in replay if e['type']=='media_artifact')==artifact


async def test_images_and_responses_api_use_server_key_and_policy(network, monkeypatch):
    client, app, routers, settings, creds, other = network
    state=app.state.gate
    monkeypatch.setenv('SCRIPTED_OPENAI_KEY','image-provider-key')
    state._keys['SCRIPTED_OPENAI_KEY']='image-provider-key'
    requests=[]
    async def upstream(request):
        requests.append(request)
        assert request.headers['authorization']=='Bearer image-provider-key'
        return httpx.Response(200,json={'data':[{'b64_json':'test-image'}],
            'usage':{'input_tokens':10,'output_tokens':20}})
    await state.http.aclose()
    state.http=httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    for model in state.bodies['test-org']['models']:
        if model['model']=='scripted-openai': model['enabled']=True
    state.route_keyed[('test-org','scripted-openai')]=True
    if not any(m['model']=='scripted-openai' for m in state.bodies['test-org']['models']):
        state.bodies['test-org']['models'].append({'model':'scripted-openai','route_id':'scripted-openai','enabled':True})
    next(m for m in state.orgs['test-org'].models if m.model=='scripted-openai').image_generation=True
    for suffix, extra in [('images/generations',{'prompt':'draw'}),('responses',{'input':'draw','tools':[{'type':'image_generation'}]})]:
        payload={'model':'scripted-openai',**extra}
        response=await client.post('/harness/llm/v1/'+suffix,json=payload)
        assert response.status_code==200,response.text
        assert requests[-1].url.path.endswith('/'+suffix)
        assert json.loads(requests[-1].content)==payload
    usage=state.store._all("SELECT * FROM usage WHERE kind='gateway'")
    assert len(usage)==2
    assert all(row['prompt_tokens']==10 and row['completion_tokens']==20 and row['cost'] is None for row in usage)
    denied=await client.post('/harness/llm/v1/images/generations',json={'model':'not-allowed','prompt':'draw'})
    assert denied.status_code==403 and len(requests)==2
    state.orgs['test-org'].sdk_tools=SdkTools()
    denied=await client.post('/harness/llm/v1/images/generations',json={'model':'scripted-openai','prompt':'draw'})
    assert denied.status_code==403 and len(requests)==2
