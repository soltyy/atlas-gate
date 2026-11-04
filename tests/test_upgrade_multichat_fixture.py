from .test_network_gate import network
from .history103_fixture import seed


async def test_three_distinct_upgrade_sources_preserve_effect_counts(network):
    client,app,routers,*_=network
    state=app.state.gate
    chats=[await seed(client,state,'chat-'+str(i+1)) for i in range(3)]
    for field in ('currentSID','previousSID','previousAlias','historyAlias','sourceRunId','requestId'):
        assert len({c[field] for c in chats})==3,field
    before=state.store._one('SELECT COUNT(*) n FROM router_operations')['n']
    assert state.store._one("SELECT COUNT(*) n FROM router_operations WHERE path LIKE '%/prompt'")['n']==3
    for chat in chats:
        response=await client.get('/harness/agent/sessions/'+chat['currentSID']+'/history-identity',
                                  params={'previousAlias':chat['previousAlias']})
        assert response.status_code==200,response.text
        assert response.json()['historyAlias']==chat['historyAlias']
        assert response.json()['continuity']['kind']=='same_native_history'
    assert state.store._one('SELECT COUNT(*) n FROM router_operations')['n']==before
    assert sum(r.state.journal.db.execute('SELECT COUNT(*) FROM turns').fetchone()[0] for r in routers)==3
