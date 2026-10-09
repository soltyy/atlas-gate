"""Opt-in subscription qualification through an isolated, real HTTP Gate and Router."""
import asyncio
import base64
import io
import json
import os
from pathlib import Path
import secrets
import sys

import httpx
import pytest
from .test_network_gate import serve

pytestmark = pytest.mark.skipif(os.environ.get('ATLAS_LIVE_SDK_TOOLS') != '1', reason='paid SDK qualification opt-in')


async def test_live_gate_sdk_search_and_file(tmp_path, monkeypatch):
    sys.path.insert(0, os.environ['ATLAS_TEST_ROUTER_ROOT'])
    from atlas_router.main import create_app as router_app
    from atlas_router.settings import Settings as RouterSettings
    from atlas_gate.main import create_app
    from atlas_gate.settings import Settings
    from atlas_gate.gate.crypto import verify_profile
    from openpyxl import Workbook
    backend = os.environ['ATLAS_QUALIFY_BACKEND']
    model = 'gpt-6.1-sol' if backend == 'codex' else 'sonnet'
    options = {'version': 1, 'webSearch': 'live', 'files': True}
    router = router_app(RouterSettings(_env_file=None, ATLAS_BACKEND=backend,
        ATLAS_GATE_ENABLED=False, ATLAS_TRUST_LOCAL=False, ATLAS_TOKEN='qualification-router',
        ATLAS_DOCUMENTS_DIR=str(tmp_path/'docs'), ATLAS_NODE_ID='qualification',
        ATLAS_NODE_DB=str(tmp_path/'node.db')))
    async with serve(router) as router_url:
        monkeypatch.setenv('QUALIFICATION_TOKEN', 'qualification-router')
        (tmp_path/'nodes.json').write_text(json.dumps([{'node_id':'qualification','url':router_url,
            'token_env':'QUALIFICATION_TOKEN','orgs':['qualification'],'routes':['sub'],'account_group':'qualification'}]))
        orgdir=tmp_path/'orgs'; orgdir.mkdir()
        (orgdir/'qualification.json').write_text(json.dumps({'id':'qualification','name':'Qualification',
            'routes':[{'id':'sub','kind':'router-agent','protocol':'atlas-agent','max_data_class':'internal'}],
            'models':[{'route_id':'sub','model':model,'display_name':model}],
            'policy':{'data_classes':['internal'],'default_data_class':'internal','routes_by_class':{'internal':['sub']}}}))
        gate = create_app(Settings(_env_file=None, ATLAS_TOKEN='qualification-admin',
            ATLAS_GATE_DB=str(tmp_path/'gate.db'), ATLAS_GATE_KEY_PATH=str(tmp_path/'key.pem'),
            ATLAS_GATE_KEYRING_SECRET='qualification-secret', ATLAS_GATE_ORGS_DIR=str(orgdir),
            ATLAS_GATE_KEYS_FILE=str(tmp_path/'no-keys'), ATLAS_GATE_NODES_FILE=str(tmp_path/'nodes.json'),
            ATLAS_GATE_NODE_PKI_DIR=str(tmp_path/'pki')))
        async with serve(gate) as url, httpx.AsyncClient(base_url=url, timeout=240) as http:
            await gate.state.gate.ready.wait()
            start=(await http.post('/harness/enroll/start',json={'device_name':'sdk-qualification','platform':'test','app_version':'1.0'})).json()
            approved=await http.post('/harness/enroll/approve',headers={'X-Atlas-Token':'qualification-admin'},
                json={'user_code':start['user_code'],'org':'qualification','user':{'id':'qualification'}})
            assert approved.status_code==200, approved.text
            creds=(await http.post('/harness/enroll/poll',json={'device_code':start['device_code']})).json()
            http.headers['Authorization']='Bearer '+creds['device_token']
            settings=(await http.get('/harness/settings')).json()
            profile=verify_profile(settings['profile_jws'],creds['profile_signing_key'])
            assert profile['models'][0]['sdkTools']['files']['supported'], profile['models']
            response=await http.post('/harness/agent/sessions',json={'model':model,'tools':[]})
            assert response.status_code==200, response.text
            assert response.json()['sdkTools']==options
            path='/harness/agent/sessions/'+response.json()['id']
            try:
                expected=str(secrets.randbelow(90000000)+10000000)
                wb=Workbook(); wb.active['B3']=expected; raw=io.BytesIO(); wb.save(raw)
                uploaded=await http.post(path+'/files',headers={'Idempotency-Key':'qualification-upload'},
                    json={'id':'qualification-upload','attachments':[{'kind':'file','filename':'qualification.xlsx',
                    'data':base64.b64encode(raw.getvalue()).decode()}]})
                assert uploaded.status_code==200, uploaded.text
                response=await http.post(path+'/prompt?mode=async',json={'id':'qualification-turn', 'text':
                    'Прочитай B3 оригинала qualification.xlsx через atlas_file. Затем сделай нативный интернет-поиск официального сайта Python. Ответь значением B3 и найденной ссылкой. Не угадывай значение.'})
                assert response.status_code==202, response.text
                events=[]; after=0
                for _ in range(120):
                    response=await http.get(path+'/turns/qualification-turn/events',params={'after':after,'wait':2000})
                    assert response.status_code==200, response.text
                    doc=response.json(); events.extend(doc['events']); after=doc['nextAfter']
                    if doc['done']: break
                final=next((e for e in reversed(events) if e['type']=='result'),{})
                assert final.get('ok') and expected in final.get('text',''), final
                completed=[e for e in events if e['type']=='sdk_tool_completed']
                assert any(e['name']=='atlas_file' and not e.get('isError') for e in completed), completed
                assert any(e['name'] in ('WebSearch','web_search') for e in completed), completed
                assert not any(e['type']=='tool_call' for e in events)
                print('GATE QUALIFIED',backend,'signed-profile/ACK/original-Excel/native-search/polling')
            finally:
                await http.delete(path,headers={'Idempotency-Key':'qualification-cleanup'})
