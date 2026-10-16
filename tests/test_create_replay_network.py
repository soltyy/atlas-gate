"""Настоящий HTTP Gate → Router/journal: отказ создания и прежняя история, без prompt."""
import httpx
import pytest
from .test_network_gate import network, serve
from atlas_gate.main import create_app
from atlas_gate.gate.errors import GateError


@pytest.mark.parametrize('lost_ack', [False, True])
async def test_rejected_create_replays_after_gate_restart_and_resumes_same_history(network, monkeypatch, lost_ack):
    client, app, routers, settings, creds, other = network
    state = app.state.gate
    native = 'hh502-saved-history'
    from atlas_router.backends.stub import StubBackend
    StubBackend.memories[native] = ['marker-42']
    state.store._exec('INSERT INTO legacy_router_history VALUES (?,?,?,?)',
        (native, 'a', creds['device_id'], 'test-org'))
    backend = routers[0].state.backend
    original = backend.open_session
    calls = []
    async def fail_once(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1:
            raise RuntimeError('Codex не ответил на thread/resume за 30 с; процесс закрыт')
        return await original(*args, **kwargs)
    monkeypatch.setattr(backend, 'open_session', fail_once)
    if lost_ack:
        request = state.nodes.request
        dropped = False
        async def lose_response(node_id, method, path, **kwargs):
            nonlocal dropped
            response = await request(node_id, method, path, **kwargs)
            if method == 'POST' and path == '/v1/sessions' and not dropped:
                dropped = True
                raise GateError(503, 'command_outcome_unknown', 'lost negative ACK')
            return response
        monkeypatch.setattr(state.nodes, 'request', lose_response)
    payload = {'system':'test', 'tools':[], 'model':'stub-fast', 'resumeSessionId':native}
    first = await client.post('/harness/agent/sessions',json=payload,headers={'Idempotency-Key':'failed-create'})
    assert first.status_code == (503 if lost_ack else 502), first.text
    retry = await client.post('/harness/agent/sessions',json=payload,headers={'Idempotency-Key':'failed-create'})
    assert retry.status_code == 502 and retry.json()['error']['code'] == 'agent_backend_error'
    assert 'thread/resume' in retry.json()['error']['message']
    assert len(calls) == 1 and len(routers[0].state.registry) == 0
    assert state.store._one("SELECT COUNT(*) n FROM router_operations")["n"] == 0
    await state.close()
    reopened = create_app(settings)
    async with serve(reopened) as url, httpx.AsyncClient(base_url=url,headers={'Authorization':'Bearer '+creds['device_token']}) as restored:
        await reopened.state.gate.ready.wait()
        replay = await restored.post('/harness/agent/sessions',json=payload,headers={'Idempotency-Key':'failed-create'})
        assert replay.status_code == retry.status_code and replay.content == retry.content
        assert len(calls) == 1
        conflict = await restored.post('/harness/agent/sessions',json={**payload,'system':'changed'},headers={'Idempotency-Key':'failed-create'})
        assert conflict.status_code == 409 and conflict.json()['error']['code']=='idempotency_conflict'
        # Отдельная попытка создания, без повторного задания/инструментов.
        resumed = await restored.post('/harness/agent/sessions',json=payload,headers={'Idempotency-Key':'separate-attempt'})
        assert resumed.status_code == 200 and resumed.json()['resumed'], resumed.text
        binding = reopened.state.gate.store._one('SELECT * FROM router_bindings WHERE id=?',(resumed.json()['id'],))
        assert binding['node_id'] == 'a' and len(calls) == 2
        session = routers[0].state.registry.get(binding['local_id']).session
        assert session.session_id == native and StubBackend.memories[native] == ['marker-42']
        assert reopened.state.gate.store._one('SELECT COUNT(*) n FROM router_operations')['n'] == 0
        assert all(not e.buffers for router in routers for e in router.state.registry._entries.values())
