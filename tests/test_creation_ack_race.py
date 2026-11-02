import concurrent.futures
import json
from types import SimpleNamespace
from atlas_gate.gate.routing import Routing
from atlas_gate.gate.store import Store


def test_creation_and_reconcile_publish_one_immutable_ack(tmp_path):
    store = Store(str(tmp_path/'gate.db'));routing = Routing(store, SimpleNamespace())
    try:
        store._exec("INSERT INTO router_bindings(id,device_id,org,route,node_id,boot_id,model,roles,status,created,sdk_alias) VALUES ('s','d','o','r','n','b','m','[]','creating',0,'alias')")
        first = {'id': 's', 'future': {'z': 1, 'a': 2}, 'resumed': True}
        reordered = dict(reversed(list(first.items())))
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            values = list(pool.map(lambda doc: routing.accept_creation('s','local',doc), [first,reordered]))
        assert values[0] == values[1]
        assert json.loads(values[0][1]) == first
        saved = store._one("SELECT * FROM router_bindings WHERE id='s'")
        assert saved['status'] == 'ready' and saved['local_id'] == 'local'
        store._exec("UPDATE router_bindings SET status='closed' WHERE id='s'")
        assert routing.accept_creation('s','local',reordered) == values[0]
        assert store._one("SELECT status FROM router_bindings WHERE id='s'")['status'] == 'closed'
    finally:
        store.close()
