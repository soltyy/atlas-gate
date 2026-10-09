import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from atlas_gate.gate.orgs import OrgConfig, Quota
from atlas_gate.gate.upstream import check_quota
from atlas_gate.gate.store import Store
from .test_discovery import configured, enroll
from .test_network_gate import serve


def test_defaults_and_template_do_not_impose_money_or_turn_quotas(tmp_path):
    template = Path(__file__).parents[1] / 'atlas_gate/gate/templates/org.json'
    org = OrgConfig.model_validate_json(template.read_text(encoding='utf-8'))
    assert org.quota.limit is None and org.quota.agent_turns_per_day is None
    store = Store(str(tmp_path/'test.db'))
    store.usage_add(org=org.id, user_id='user', device_id='device', kind='gateway', cost=1000)
    check_quota(SimpleNamespace(store=store), SimpleNamespace(user_id='user'), org)
    for _ in range(105):
        assert store.agent_turns_reserve('user', '2026-10-09', org.quota.agent_turns_per_day)
    assert store.agent_turns('user', '2026-10-09') == 105
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('chunked', [False, True])
async def test_large_llm_body_reaches_upstream_unchanged(configured, monkeypatch, chunked):
    http, state, orgs = configured
    content = 'x' * (11 * 1024 * 1024)
    expected = hashlib.sha256(content.encode()).hexdigest()
    seen = []
    upstream = FastAPI()
    @upstream.get('/v1/models')
    async def models():
        return {'data': [{'id': 'api-model'}]}
    @upstream.post('/v1/chat/completions')
    async def completion(request: Request):
        doc = await request.json()
        seen.append((len(doc['messages'][0]['content']), hashlib.sha256(doc['messages'][0]['content'].encode()).hexdigest()))
        assert request.headers['authorization'] == 'Bearer test-upstream-key'
        if doc.get('reject'):
            return JSONResponse({'error': {'message': 'provider actual limit'}}, status_code=413)
        return {'choices': [{'message': {'content': 'OK'}}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}
    async with serve(upstream) as url:
        monkeypatch.setenv('PRIVATE_KEY_NAME','test-upstream-key')
        path = orgs/'first.json'
        org = json.loads(path.read_text(encoding='utf-8'))
        org['routes'][1]['upstream_url'] = url+'/v1'
        path.write_text(json.dumps(org),encoding='utf-8')
        assert not (await state.reload())['errors']
        creds = await enroll(http)
        headers = {'Authorization': 'Bearer '+creds['device_token'], 'Content-Type': 'application/json'}
        body = json.dumps({'model':'api-model', 'messages':[{'role':'user','content':content}]}).encode()
        async def chunks():
            for offset in range(0,len(body),65536):
                yield body[offset:offset+65536]
        result = await http.post('/harness/llm/v1/chat/completions', content=chunks() if chunked else body, headers=headers)
        assert result.status_code == 200, result.text
        assert seen == [(len(content),expected)]
        # Provider refusal remains visible; absence of a Gate ceiling does not bypass it.
        denied = await http.post('/harness/llm/v1/chat/completions', json={'model':'api-model','reject':True,'messages':[{'content':'x'}]}, headers=headers)
        assert denied.status_code == 413 and 'provider actual limit' in denied.text
        assert state.store._one("SELECT COUNT(*) n FROM usage WHERE kind='gateway'")['n'] == 1
