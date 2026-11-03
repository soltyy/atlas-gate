"""Subprocess fixture: imports pinned historical runtimes, SDK input is scripted."""
import asyncio
import json
import os
from pathlib import Path
import sys
import uuid

import uvicorn
import atlas_gate
import atlas_router
from atlas_gate.main import create_app as gate_app
from atlas_gate.settings import Settings as GateSettings
from atlas_router.main import create_app as router_app
from atlas_router.settings import Settings as RouterSettings
from atlas_router.backends.stub import StubSession

root=Path(sys.argv[1]);gate_port=int(sys.argv[2]);router_port=int(sys.argv[3])
records=[];gate_hits=[]
native=router_app(RouterSettings(_env_file=None,ATLAS_BACKEND='stub',ATLAS_GATE_ENABLED=False,
    ATLAS_TRUST_LOCAL=False,ATLAS_TOKEN='test-router',ATLAS_NODE_ID='legacy',ATLAS_NODE_CAPACITY=4,
    ATLAS_NODE_DB=str(root/'router.db'),ATLAS_DOCUMENTS_DIR=str(root/'documents')))

async def legacy_steer(self,text):
    entry=next(e for e in native.state.registry._entries.values() if e.session is self)
    records.append({'turnId':entry.current_turn,'text':text})
    return str(uuid.uuid4())
StubSession.steer=legacy_steer

@native.get('/qa/legacy-steer')
async def evidence():
    return {'records':records,'gateHits':gate_hits,'gateVersion':atlas_gate.__version__,
            'routerVersion':atlas_router.__version__,'gateSource':atlas_gate.__file__,
            'routerSource':atlas_router.__file__}

os.environ['NODE_TOKEN']='test-router'
(root/'nodes.json').write_text(json.dumps([{'node_id':'legacy','url':f'http://127.0.0.1:{router_port}',
    'token_env':'NODE_TOKEN','orgs':['test-org'],'routes':['claude-sub'],'account_group':'qa'}]))
gate=gate_app(GateSettings(_env_file=None,ATLAS_TOKEN='admin',ATLAS_GATE_TEST=True,
    ATLAS_GATE_DB=str(root/'gate.db'),ATLAS_GATE_KEY_PATH=str(root/'key.pem'),
    ATLAS_GATE_KEYRING_SECRET='inert-qa-secret',ATLAS_GATE_ORGS_DIR=str(root/'orgs'),
    ATLAS_GATE_KEYS_FILE=str(root/'no-keys'),ATLAS_GATE_NODES_FILE=str(root/'nodes.json'),
    ATLAS_GATE_NODE_PKI_DIR=str(root/'pki')))

@gate.middleware('http')
async def count_ingress(request,call_next):
    gate_hits.append(request.url.path)
    return await call_next(request)

async def main():
    servers=[uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='warning',
              timeout_graceful_shutdown=3)) for app,port in ((native,router_port),(gate,gate_port))]
    task=asyncio.create_task(servers[0].serve())
    while not servers[0].started:
        if task.done():task.result();raise RuntimeError('legacy Router exited')
        await asyncio.sleep(.01)
    try:
        await servers[1].serve()
    finally:
        servers[0].should_exit=True
        await task

asyncio.run(main())
