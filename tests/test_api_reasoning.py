import json
from types import SimpleNamespace
import pytest
from atlas_gate.gate.api_reasoning import ApiReasoning
from atlas_gate.gate.orgs import ModelEntry
from atlas_gate.gate.store import Store
from atlas_gate.gate.errors import GateError


@pytest.fixture
def api(tmp_path):
    store=Store(str(tmp_path/'gate.db'))
    yield ApiReasoning(store)
    store.close()


def entry(dialect='openai', policy=None, **caps):
    return ModelEntry(route_id='route',model='m',reasoningDialect=dialect,
        reasoningPolicy=policy,reasoningCapabilities=dict(status='supported',efforts=['low','medium','high'],
        defaultEffort='medium',adaptive=True,disabled=True,budgetTokens=True,source='configured',**caps))


def apply(api,doc,model=None,suffix='/chat/completions',headers=None,device=None):
    doc={'model':'m',**doc};raw=json.dumps(doc,indent=2).encode()
    result,decision=api.apply(raw,doc,model,suffix,device or SimpleNamespace(org='o',device_id='d'),headers or {})
    return json.loads(result),decision


def test_legacy_native_bytes_are_unchanged_without_opt_in(api):
    raw=b'{ "model":"custom", "reasoning_effort":"vendor-special" }'
    result,decision=api.apply(raw,json.loads(raw),None,'/chat/completions',SimpleNamespace(org='o',device_id='d'),{})
    assert result==raw and decision is None


@pytest.mark.parametrize('dialect,suffix,doc,field,value',[
    ('openai','/chat/completions',{'atlas_reasoning':{'effort':'high'}},'reasoning_effort','high'),
    ('openai','/responses',{'atlas_reasoning':{'mode':'disabled'}},'reasoning',{'effort':'none'}),
    ('anthropic','/messages',{'atlas_reasoning':{'mode':'enabled','effort':'high'}},'thinking',{'type':'adaptive'}),
    ('anthropic','/messages',{'max_tokens':8192,'atlas_reasoning':{'budgetTokens':2048}},'thinking',{'type':'enabled','budget_tokens':2048}),
    ('deepseek','/chat/completions',{'atlas_reasoning':{'mode':'enabled','effort':'medium'}},'reasoning_effort','high'),
    ('deepseek','/responses',{'atlas_reasoning':{'effort':'xhigh'}},'reasoning',{'effort':'high'}),
    ('deepseek','/messages',{'atlas_reasoning':{'mode':'enabled','effort':'high'}},'thinking',{'type':'enabled'}),
])
def test_native_channel_mapping(api,dialect,suffix,doc,field,value):
    out,decision=apply(api,doc,entry(dialect),suffix)
    assert out[field]==value and 'atlas_reasoning' not in out
    assert decision['providerConfirmed'] is False
    if dialect=='deepseek' and doc['atlas_reasoning']['effort'] in ('medium','xhigh'):
        assert decision['normalization']=='deepseek_vendor_mapping'
        assert decision['configured']['effort']=='high'


def test_anthropic_effort_does_not_enable_thinking_or_rewrite_history(api):
    history=[{'role':'assistant','content':[{'type':'thinking','thinking':'opaque','signature':'unchanged'}]},
             {'role':'user','content':[{'type':'tool_result','tool_use_id':'call','content':'original'}]}]
    doc={'messages':history,'output_config':{'effort':'high','format':{'type':'json_schema'}}}
    out,decision=apply(api,doc,entry('anthropic'),'/messages')
    assert 'thinking' not in out and out['messages']==history
    assert out['output_config']==doc['output_config']
    assert decision['configured']['mode']=='inherit'
    doc['thinking']={'type':'adaptive','display':'omitted'}
    out,_=apply(api,doc,entry('anthropic'),'/messages')
    assert out['thinking']==doc['thinking']


def test_native_and_canonical_conflicts_are_not_silently_resolved(api):
    with pytest.raises(GateError) as e:
        apply(api,{'reasoning_effort':'low','atlas_reasoning':{'effort':'high'}},entry())
    assert e.value.code=='reasoning_conflict'
    out,_=apply(api,{'reasoning_effort':'high','atlas_reasoning':{'mode':'enabled','effort':'high'}},entry())
    assert out['reasoning_effort']=='high'


def test_policy_force_allowlist_and_unknown_or_unsupported(api):
    with pytest.raises(GateError) as e:
        apply(api,{'reasoning_effort':'high'},entry(policy={'default':{'effort':'low'},'allowOverride':False}))
    assert e.value.status==403
    with pytest.raises(GateError):apply(api,{'atlas_reasoning':{'effort':'high'}},None)
    model=entry();model.reasoningCapabilities=model.reasoningCapabilities.model_copy(update={'status':'unknown'})
    with pytest.raises(GateError):apply(api,{'atlas_reasoning':{'effort':'high'}},model)


def test_defaults_reload_request_replay_and_api_scope_pin(api):
    model=entry(policy={'default':{'effort':'low'}})
    headers={'idempotency-key':'first','x-atlas-reasoning-scope':'run'}
    first,decision=apply(api,{'messages':[]},model,headers=headers)
    assert first['reasoning_effort']=='low'
    model.reasoningPolicy=model.reasoningPolicy.model_copy(update={'default':None})
    replay,again=apply(api,{'messages':[]},model,headers=headers)
    assert replay==first and again==decision
    next_step,_=apply(api,{'messages':[{'role':'user','content':'tool output'}]},model,
        headers={'idempotency-key':'second','x-atlas-reasoning-scope':'run'})
    assert next_step['reasoning_effort']=='low'
    fresh,_=apply(api,{'messages':[]},model,headers={'idempotency-key':'third','x-atlas-reasoning-scope':'new-run'})
    assert 'reasoning_effort' not in fresh
    with pytest.raises(GateError) as e:
        apply(api,{'messages':['different']},model,headers=headers)
    assert e.value.code=='idempotency_conflict'
    with pytest.raises(GateError) as e:
        apply(api,{'reasoning_effort':'high'},model,headers={'idempotency-key':'fourth','x-atlas-reasoning-scope':'run'})
    assert e.value.code=='reasoning_scope_conflict'
    # The same opaque IDs belong to a different principal, not to the original scope.
    foreign,_=apply(api,{},model,headers=headers,device=SimpleNamespace(org='other',device_id='other'))
    assert 'reasoning_effort' not in foreign
    assert 'tool output' not in json.dumps(api.store._all('SELECT * FROM api_reasoning_snapshots'))


def test_disabled_effort_combination_requires_explicit_capability(api):
    doc={'thinking':{'type':'disabled'},'output_config':{'effort':'high'}}
    model=entry('anthropic')
    with pytest.raises(GateError):apply(api,doc,model,'/messages')
    model.reasoningCapabilities=model.reasoningCapabilities.model_copy(update={'disabledEfforts':['low','medium','high']})
    out,_=apply(api,doc,model,'/messages')
    assert out['thinking']=={'type':'disabled'} and out['output_config']['effort']=='high'
