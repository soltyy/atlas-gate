import json
import pytest
from atlas_gate.gate.attachments import node_attachments, route_attachments, validate_prompt
from atlas_gate.gate.nodes import Nodes
from atlas_gate.schemas import PromptRequest
from atlas_gate.gate.errors import GateError


async def test_connector_constructor_has_no_incoming_token_requirement(tmp_path):
    path=tmp_path/'nodes.json'
    path.write_text(json.dumps([{'node_id':'nat','transport':'connector','orgs':['org'],'routes':['route'],
        'account_group':'subscription'}]))
    nodes=Nodes(str(path),channel=object())
    assert type(nodes.clients['nat']).__name__=='ChannelClient'
    await nodes.close()


def test_unknown_capabilities_and_size_bytes_not_trusted():
    assert node_attachments({}) is None
    assert node_attachments({'attachments':{'schemaVersion':1}}) is None
    with pytest.raises(GateError) as e:
        validate_prompt(PromptRequest(id='bad',text='',attachments=[{'kind':'pdf','data':'oops','sizeBytes':1}]),None)
    assert e.value.code=='attachment_invalid'
    with pytest.raises(GateError) as e:
        validate_prompt(PromptRequest(id='too-long',text='x'*1_000_001),None)
    assert e.value.code=='attachment_limit'


def test_document_authorization_nullable_and_intersection():
    from types import SimpleNamespace
    flags = {'text':True, 'image':False, 'pdfNative':False, 'documentPages':True, 'agentDocumentTools':True}
    limits = {'maxFiles':8, 'maxRawBytes':15*1024*1024, 'maxTotalBytes':20*1024*1024,
              'maxTextChars':1_000_000, 'maxPdfPages':500, 'maxPagePixels':4_000_000}
    caps = dict(schemaVersion=1, **flags, **limits)
    assert node_attachments({'attachments':caps})['documentAuthorization'] is None
    state=SimpleNamespace(nodes=SimpleNamespace(
        config={n:SimpleNamespace(enabled=True,orgs=['org'],routes=['route'],node_id=n) for n in ('a','b')},
        revoked=lambda _:False, catalog={n:[{'value':'model'}] for n in ('a','b')},
        snapshots={n:{'attachments':dict(caps,documentAuthorization=True)} for n in ('a','b')}))
    assert route_attachments(state,'org','route','model')['documentAuthorization'] is True
    state.nodes.snapshots['b']['attachments'].pop('documentAuthorization')
    assert route_attachments(state,'org','route','model')['documentAuthorization'] is None
    state.nodes.snapshots['b']['attachments']['documentAuthorization']=False
    assert route_attachments(state,'org','route','model')['documentAuthorization'] is False


def test_document_scope_survives_public_to_router_schema():
    from atlas_gate.schemas import SessionCreate
    from atlas_gate.gate.schemas import GateAgentSessionCreate
    source=SessionCreate(system='test',model='model',documentAccess='client-authorized-v1',documentIds=['a'*64])
    target=GateAgentSessionCreate.model_validate(source.model_dump(exclude_none=True))
    assert target.documentAccess=='client-authorized-v1' and target.documentIds==['a'*64]
    for invalid in (['not-sha'], ['a'*64]*501):
        with pytest.raises(ValueError):
            SessionCreate(system='test',model='model',documentAccess='client-authorized-v1',documentIds=invalid)
