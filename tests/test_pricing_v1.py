import copy
import json
from datetime import datetime, timezone, timedelta

import httpx
import pytest
from pydantic import ValidationError

from atlas_gate.pricing import Schedule, Rates
from atlas_gate.gate.orgs import OrgConfig, dump, parse
from atlas_gate.gate.profile import build_body
from atlas_gate.gate.crypto import verify_profile
from .test_discovery import configured, enroll


def plan(at=None):
    at = at or datetime.now(timezone.utc)
    start, split, end = at - timedelta(days=1), at + timedelta(hours=1), at + timedelta(days=1)
    return dict(route_id='private-api', model='api-model', pricing_version='verified-test',
                source_url='https://example.invalid/prices', calendar_source_url='https://example.invalid/calendar',
                checked_at=start.isoformat(), valid_from=start.isoformat(), valid_until=end.isoformat(),
                periods=[dict(starts_at=start.isoformat(), ends_at=split.isoformat(), tariff_id='offpeak',
                              rates=dict(input_uncached=.15, input_cached=.003, output=.6)),
                         dict(starts_at=split.isoformat(), ends_at=end.isoformat(), tariff_id='peak',
                              rates=dict(input_uncached=.3, input_cached=.006, output=1.2))])


def selection(p, period=0):
    v=p.periods[period]
    return {'pricing_version':p.pricing_version,'tariff_id':v.tariff_id,
            'starts_at':v.starts_at.isoformat(),'ends_at':v.ends_at.isoformat()}


def test_interval_boundaries_reserve_and_exact_estimate():
    p=Schedule.model_validate(plan())
    assert p.select(p.valid_from)==p.periods[0]
    assert p.select(p.periods[0].ends_at)==p.periods[1]
    assert p.select(p.valid_until) is None
    assert p.public()['reserve_rates']==dict(input_uncached=.3,input_cached=.006,output=1.2)
    assert p.periods[0].rates.cost(dict(prompt=1_000_000,cached=500_000,completion=1_000_000))==.6765


@pytest.mark.parametrize('usage',[{}, {'prompt_tokens':1},
    {'prompt_tokens':1,'completion_tokens':2},
    {'prompt_tokens':1,'completion_tokens':2,'prompt_cache_hit_tokens':2},
    {'prompt_tokens':1,'completion_tokens':2,'prompt_tokens_details':{'cached_tokens':0},'prompt_cache_hit_tokens':1},
    {'prompt_tokens':True,'completion_tokens':2,'prompt_cache_hit_tokens':0}])
def test_incomplete_dynamic_usage_is_unknown_not_free(usage):
    from atlas_gate.gate.upstream import openai_usage
    assert openai_usage(usage,strict=True) is None


@pytest.mark.parametrize('mutation',['gap','overlap','unsorted','naive','nan','negative','currency','coverage','zero_interval'])
def test_invalid_schedules(mutation):
    p=plan()
    if mutation in ('gap','overlap'):
        p['periods'][1]['starts_at']=(datetime.fromisoformat(p['periods'][1]['starts_at'])+
            timedelta(seconds=1 if mutation=='gap' else -1)).isoformat()
    elif mutation=='unsorted':p['periods'].reverse()
    elif mutation=='naive':p['checked_at']='2026-01-01T00:00:00'
    elif mutation=='nan':p['periods'][0]['rates']['output']=float('nan')
    elif mutation=='negative':p['periods'][0]['rates']['output']=-1
    elif mutation=='currency':p['currency']='EUR'
    elif mutation=='coverage':p['periods'].pop()
    else:p['periods'][0]['ends_at']=p['periods'][0]['starts_at']
    with pytest.raises(ValidationError):Schedule.model_validate(p)


@pytest.fixture(autouse=True)
def provider_available(monkeypatch):
    monkeypatch.setenv('PRIVATE_KEY_NAME','test-key')
    async def probe(self,route,key):return 'ok'
    monkeypatch.setattr('atlas_gate.gate.state.GateState._probe',probe)


async def test_signed_profile_roundtrip_and_strict_org(configured):
    client,state,_=configured
    original=state.orgs['first']
    doc=original.model_dump(mode='json');doc['pricing_v1']=[plan()];doc['pricing']=[]
    org=OrgConfig.model_validate(doc)
    assert parse(dump(org))==org
    body,_=build_body(org,'https://gate.invalid',set(),None)
    assert body['pricing']==[]
    assert body['pricing_v1'][0]['reserve_rates']['output']==1.2
    # Real publication through the normal admin If-Match path.
    admin={'X-Atlas-Token':'test-admin-secret'}
    before=(await client.get('/harness/admin/orgs/first',headers=admin)).json()
    r=await client.put('/harness/admin/orgs/first',json=doc,headers={**admin,'If-Match':before['etag']})
    assert r.status_code==200,r.text
    creds=await enroll(client)
    response=await client.get('/harness/settings',headers={'Authorization':'Bearer '+creds['device_token']})
    assert response.status_code==200,response.text
    settings=response.json()
    signed=verify_profile(settings['profile_jws'],creds['profile_signing_key'])
    assert signed==settings['profile']
    assert signed['pricing_v1'][0]['pricing_version']=='verified-test'
    assert signed['pricing']==[]
    doc['pricing']=[dict(route_id='private-api',model='api-model')]
    with pytest.raises(ValidationError):OrgConfig.model_validate(doc)


@pytest.mark.parametrize('stream',[False,True])
async def test_real_proxy_admission_capture_header_strip_and_usage(configured,monkeypatch,stream):
    client,state,_=configured
    creds=await enroll(client);client.headers['Authorization']='Bearer '+creds['device_token']
    schedule=Schedule.model_validate(plan());state.orgs['first'].pricing_v1=[schedule]
    state.orgs['first'].pricing=[]
    monkeypatch.setattr(state,'key_for',lambda route:'private-key')
    captured=[]
    usage={'prompt_tokens':1_000_000,'completion_tokens':1_000_000,'prompt_tokens_details':{'cached_tokens':500_000}}
    def provider(request):
        captured.append(request)
        assert not any(k.startswith('x-atlas-pricing-') for k in request.headers)
        # Mutate catalog after dispatch: this call must retain its original rate.
        state.orgs['first'].pricing_v1=[]
        if stream:
            class Stream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield ('data: '+json.dumps({'usage':usage})+'\n\ndata: [DONE]\n\n').encode()
            return httpx.Response(200,stream=Stream(),headers={'Content-Type':'text/event-stream'})
        return httpx.Response(200,json={'choices':[],'usage':usage})
    await state.http.aclose();state.http=httpx.AsyncClient(transport=httpx.MockTransport(provider))
    body={'model':'api-model','messages':[],'stream':stream}
    refused=await client.post('/harness/llm/v1/chat/completions',json=body)
    assert refused.status_code==422 and 'pricing_contract_required' in refused.text and not captured
    headers={'X-Atlas-Pricing-Contract':'1','X-Atlas-Pricing-Selection':json.dumps(selection(schedule)), 'Idempotency-Key':'inert-call'}
    wrong=await client.post('/harness/llm/v1/chat/completions',json=body,headers={**headers,'X-Atlas-Pricing-Selection':'{}'})
    assert wrong.status_code==409 and 'not_sent' in wrong.text and not captured
    response=await client.post('/harness/llm/v1/chat/completions',json=body,headers=headers)
    assert response.status_code==200,response.text
    decision=json.loads(response.headers['X-Atlas-Pricing-Decision'])
    assert decision['rates']==schedule.periods[0].rates.model_dump()
    assert decision['provider_confirmed'] is False
    rows=state.store._all("SELECT * FROM usage WHERE kind='gateway'")
    assert len(rows)==1 and rows[0]['cost']==.6765
    saved=state.store._one('SELECT decision FROM gateway_usage_pricing')
    assert json.loads(saved['decision'])==decision


async def test_expired_schedule_never_calls_provider(configured):
    client,state,_=configured
    creds=await enroll(client);client.headers['Authorization']='Bearer '+creds['device_token']
    state.orgs['first'].pricing_v1=[Schedule.model_validate(plan(datetime(2020,1,1,tzinfo=timezone.utc)))]
    r=await client.post('/harness/llm/v1/chat/completions',json={'model':'api-model','messages':[]},headers={'X-Atlas-Pricing-Contract':'1'})
    assert r.status_code==409 and 'pricing_expired' in r.text and 'not_sent' in r.text


def test_reviewed_calendar_weekends_holiday_gap_and_boundaries():
    from tools.deepseek_pricing import generate
    start=datetime(2026,10,29,tzinfo=timezone.utc)
    end=datetime(2026,11,3,tzinfo=timezone.utc)
    schedules=generate(start,end,start,{start.date()},'https://example.invalid/calendar','test')
    p=schedules[0]
    assert p.select(start+timedelta(hours=2)).tariff_id=='offpeak'  # explicit holiday
    friday=start+timedelta(days=1)
    for hours,expected in [(0,'offpeak'),(1,'peak'),(4,'offpeak'),(6,'peak'),(10,'offpeak')]:
        assert p.select(friday+timedelta(hours=hours)).tariff_id==expected
    assert p.select(start+timedelta(days=2,hours=2)).tariff_id=='offpeak' # Saturday
    assert p.select(start+timedelta(days=4,hours=2)).tariff_id=='peak' # Monday
    assert p.select(end) is None


async def test_pinned_request_rejects_changed_body_and_has_no_io_retry(configured,monkeypatch):
    client,state,_=configured
    creds=await enroll(client);client.headers['Authorization']='Bearer '+creds['device_token']
    p=Schedule.model_validate(plan());state.orgs['first'].pricing_v1=[p]
    monkeypatch.setattr(state,'key_for',lambda route:'test-key')
    calls=[]
    def provider(request):
        calls.append(request)
        return httpx.Response(503,json={'error':{'code':'server_is_overloaded'}})
    await state.http.aclose();state.http=httpx.AsyncClient(transport=httpx.MockTransport(provider))
    headers={'Idempotency-Key':'immutable','X-Atlas-Pricing-Contract':'1',
             'X-Atlas-Pricing-Selection':json.dumps(selection(p))}
    body={'model':'api-model','messages':[]}
    r=await client.post('/harness/llm/v1/chat/completions',json=body,headers=headers)
    assert r.status_code==503 and len(calls)==1
    assert not state.store._all("SELECT * FROM usage WHERE kind='gateway'")
    r=await client.post('/harness/llm/v1/chat/completions',json={**body,'messages':[{'role':'user','content':'different'}]},headers=headers)
    assert r.status_code==409 and 'idempotency_conflict' in r.text and len(calls)==1
    assert len(state.store._all('SELECT * FROM gateway_price_requests'))==1
    read=await client.get('/harness/llm/pricing/decision',params={'requestId':'immutable','route_id':'private-api','model':'api-model'})
    assert read.status_code==200 and read.json()['providerOutcome']=='not_recorded' and len(calls)==1
    other=await enroll(client)
    foreign=await client.get('/harness/llm/pricing/decision',params={'requestId':'immutable','route_id':'private-api','model':'api-model'},
        headers={'Authorization':'Bearer '+other['device_token']})
    assert foreign.status_code==404
