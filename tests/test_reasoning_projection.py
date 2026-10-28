from types import SimpleNamespace
import pytest
from atlas_gate.reasoning import ReasoningError
from atlas_gate.gate.reasoning import project_capabilities, validate_node


def info(backend='claude',**caps):
    return {'backend':backend,'reasoningSettings':{'version':1},'models':[{'value':'m',
        'reasoning':{'status':'supported','efforts':['low','high'],'source':'test',**caps}}]}


def test_profile_preserves_mode_capabilities_and_keeps_unknown_nodes_distinct():
    node=SimpleNamespace(enabled=True,orgs=['o'],routes=['r'])
    nodes=SimpleNamespace(config={'a':node,'b':node},snapshots={
        'a':info(adaptive=True,disabled=True,disabledEfforts=['low']), 'b':{'models':[{'value':'m'}]}},revoked=lambda n:False)
    projected=project_capabilities(SimpleNamespace(nodes=nodes),'o','r','m')
    assert projected['adaptive'] is True and projected['disabledEfforts']==['low']
    assert projected['coverage']=='partial' and projected['knownNodes']==2
    assert projected['providerConfirmed'] is False
    assert project_capabilities(SimpleNamespace(nodes=nodes),'foreign','r','m')['status']=='unknown'


def test_selection_validates_native_mode_before_dispatch():
    selected={'selected':{'version':1,'mode':'enabled','effort':'high','budgetTokens':None}}
    with pytest.raises(ReasoningError,match='adaptive'):
        validate_node(info(adaptive=False),'m',selected)
    validate_node(info(adaptive=True),'m',selected)
    with pytest.raises(ReasoningError,match='default'):
        validate_node(info('codex'),'m',{'selected':{'version':1,'mode':'provider_default'}})
