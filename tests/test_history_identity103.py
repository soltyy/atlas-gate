import json

import pytest

from .test_recovery_coordinator import recovery_http, source


def bindings(state, nodes):
    source(state, nodes, 'old', tool=False, terminal=False)
    source(state, nodes, 'new', tool=False, terminal=False)
    state.store._exec("UPDATE router_bindings SET sdk_id='same-private-native-id'")
    state.store._exec("UPDATE router_bindings SET recovery_source=?,response=? WHERE id='new'",
        (json.dumps({'previousSessionId':'old','allowHistoryFallback':False}),
         json.dumps({'resumed':True,'resumeError':None})))


async def test_exact_closed_lineage_stable_owner_read_without_writes_or_router(recovery_http):
    client, state, nodes = recovery_http
    bindings(state,nodes)
    before, calls = state.store._db.total_changes, list(nodes.calls)
    path='/harness/agent/sessions/new/history-identity?previousAlias=alias-old'
    first=await client.get(path)
    assert first.status_code==200,first.text
    assert first.headers['cache-control']=='private, no-store' and first.headers['vary']=='Authorization'
    proof=first.json()
    assert proof['sessionId']=='new' and proof['historyAlias']=='alias-new'
    assert proof['orgId']=='org' and proof['routeId']=='sub' and proof['model']=='model'
    assert proof['continuity']=={'kind':'same_native_history','previousAlias':'alias-old','previousSessionId':'old'}
    assert len(proof['proofId'])==64 and proof['outcomeKnown'] is False and proof['businessReplayAllowed'] is False
    assert 'private-native' not in first.text
    assert (await client.get(path)).content==first.content
    assert state.store._db.total_changes==before and nodes.calls==calls
    assert (await client.get(path,headers={'Authorization':'Bearer other'})).status_code==404


async def test_current_binding_identity_before_native_event_is_not_continuity(recovery_http):
    client,state,nodes=recovery_http
    bindings(state,nodes)
    state.store._exec("UPDATE router_bindings SET sdk_id=NULL WHERE id='new'")
    current=await client.get('/harness/agent/sessions/new/history-identity')
    assert current.status_code==200 and current.json()['continuity']=={'kind':'current_binding'}
    assert current.json()['historyAlias']=='alias-new'
    old=await client.get('/harness/agent/sessions/new/history-identity?previousAlias=alias-old')
    assert old.status_code==409 and old.json()['error']['code']=='history_identity_unavailable'


@pytest.mark.parametrize('change',[
    "UPDATE router_bindings SET sdk_id='different' WHERE id='old'",
    "UPDATE router_bindings SET sdk_id=NULL WHERE id='old'",
    "UPDATE router_bindings SET recovery_source=NULL WHERE id='new'",
    "UPDATE router_bindings SET recovery_source='{\"previousSessionId\":\"old\",\"allowHistoryFallback\":true}' WHERE id='new'",
    "UPDATE router_bindings SET response='{\"resumed\":false}' WHERE id='new'",
    "UPDATE router_bindings SET response='{\"resumed\":true,\"resumeError\":\"fallback\"}' WHERE id='new'",
    "UPDATE router_bindings SET route='other' WHERE id='old'",
    "UPDATE router_bindings SET model='other' WHERE id='old'",
    "UPDATE router_bindings SET device_id='other' WHERE id='old'",
    "UPDATE router_bindings SET org='other' WHERE id='old'",
    "UPDATE router_bindings SET recovery_source='[]' WHERE id='new'",
])
async def test_incomplete_or_unrelated_history_never_gets_continuity(recovery_http,change):
    client,state,nodes=recovery_http
    bindings(state,nodes);state.store._exec(change)
    response=await client.get('/harness/agent/sessions/new/history-identity?previousAlias=alias-old')
    assert response.status_code in (403,404,409),response.text
    assert 'same_native_history' not in response.text and 'private-native' not in response.text


async def test_owned_multihop_chain_and_cycle(recovery_http):
    client,state,nodes=recovery_http
    bindings(state,nodes)
    source(state,nodes,'latest',tool=False,terminal=False)
    state.store._exec("UPDATE router_bindings SET sdk_id='same-private-native-id',recovery_source=?,response=? WHERE id='latest'",
        (json.dumps({'previousSessionId':'new','allowHistoryFallback':False}),json.dumps({'resumed':True})))
    path='/harness/agent/sessions/latest/history-identity?previousAlias=alias-old'
    good=await client.get(path)
    assert good.status_code==200 and good.json()['continuity']['previousSessionId']=='old'
    state.store._exec("UPDATE router_bindings SET recovery_source=? WHERE id='new'",
        (json.dumps({'previousSessionId':'latest','allowHistoryFallback':False}),))
    assert (await client.get(path)).status_code==409


async def test_policy_and_revocation_remain_enforced(recovery_http):
    client,state,nodes=recovery_http
    bindings(state,nodes)
    path='/harness/agent/sessions/new/history-identity?previousAlias=alias-old'
    state.orgs['org'].archived=True
    assert (await client.get(path)).status_code==403
    state.orgs['org'].archived=False
    nodes.config['node'].enabled=False
    assert (await client.get(path)).status_code==403
    nodes.config['node'].enabled=True
    state.store.device_revoke('owner')
    assert (await client.get(path)).status_code==401
