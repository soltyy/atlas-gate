import json
import httpx
import pytest
from atlas_gate.reasoning import ReasoningCapabilities, ReasoningPolicy
from .test_discovery import configured, enroll


@pytest.fixture(autouse=True)
def provider_available(monkeypatch):
    monkeypatch.setenv('PRIVATE_KEY_NAME','test-provider-key')
    async def probe(self,route,key):return 'ok'
    monkeypatch.setattr('atlas_gate.gate.state.GateState._probe',probe)


@pytest.mark.parametrize('stream',[False,True])
async def test_actual_api_policy_proxy_headers_usage_and_rejection(configured,monkeypatch,stream):
    client,state,_=configured
    credentials=await enroll(client)
    client.headers['Authorization']='Bearer '+credentials['device_token']
    model=next(m for m in state.orgs['first'].models if m.model=='api-model')
    model.reasoningDialect='openai'
    model.reasoningCapabilities=ReasoningCapabilities(status='supported',efforts=['low','high'],defaultEffort='low',source='test')
    model.reasoningPolicy=ReasoningPolicy(default={'effort':'low'})
    monkeypatch.setattr(state,'key_for',lambda route:'test-provider-key')
    captured=[]
    usage={'prompt_tokens':17,'completion_tokens':3,'prompt_tokens_details':{'cached_tokens':10}}
    def upstream(request):
        captured.append(request)
        if stream:
            class Stream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield ('data: '+json.dumps({'usage':usage})+'\n\ndata: [DONE]\n\n').encode()
            return httpx.Response(200,stream=Stream(),headers={'Content-Type':'text/event-stream'})
        return httpx.Response(200,json={'choices':[],'usage':usage})
    await state.http.aclose()
    state.http=httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    request={'model':'api-model','messages':[{'role':'user','content':'opaque'}],'stream':stream}
    headers={'Idempotency-Key':'model-call','X-Atlas-Reasoning-Scope':'loop','X-Harness-Agent':'agent'}
    result=await client.post('/harness/llm/v1/chat/completions',json=request,headers=headers)
    assert result.status_code==200,result.text
    assert json.loads(result.headers['X-Atlas-Configured-Reasoning'])['configured']['effort']=='low'
    sent=json.loads(captured[0].content)
    assert sent['messages']==request['messages'] and sent['reasoning_effort']=='low'
    assert not any(k.startswith('x-harness-') or k=='x-atlas-reasoning-scope' for k in captured[0].headers)
    rows=state.store._all("SELECT * FROM usage WHERE kind='gateway'")
    assert len(rows)==1 and rows[0]['prompt_tokens']==17 and rows[0]['cached_tokens']==10
    # New model-call in an accepted loop keeps the original decision after a policy change.
    model.reasoningPolicy=ReasoningPolicy(default={'effort':'high'})
    again=await client.post('/harness/llm/v1/chat/completions',json=request,
        headers={**headers,'Idempotency-Key':'next-call'})
    assert again.status_code==200,again.text
    assert json.loads(captured[-1].content)['reasoning_effort']=='low'
    before=len(captured)
    invalid=await client.post('/harness/llm/v1/chat/completions',json={**request,'atlas_reasoning':{'effort':'max'}},
        headers={'Idempotency-Key':'unsupported'})
    assert invalid.status_code==422 and len(captured)==before
