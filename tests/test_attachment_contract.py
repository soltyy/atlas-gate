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
