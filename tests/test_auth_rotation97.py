"""Real HTTP, encrypted SQLite receipts and same-device recovery; no inference."""
import asyncio
import time

from atlas_gate.gate.crypto import hmac_hex, sha256_hex, verify_profile
from .test_discovery import configured, enroll


def proof(creds, state, request_id):
    from atlas_gate.gate.auth_recovery import challenge
    signed = challenge(state,creds["device_id"],request_id)["challenge"]
    return dict(device_id=creds['device_id'], request_id=request_id, challenge=signed,
        proof=hmac_hex(creds['device_secret'], dict(version=1, purpose='atlas-gate-token-recovery',
            device_id=creds['device_id'], request_id=request_id, challenge=signed)))


async def test_lost_ack_restart_and_competing_rotations(configured):
    http, state, _ = configured
    first = await enroll(http)
    body = dict(refresh_token=first['refresh_token'], rotation_id='durable-operation-1')
    response = await http.post('/harness/auth/v1/rotate', json=body)
    assert response.status_code == 200, response.text
    second = response.json()
    # Dropping response and replaying the saved request retrieves the same credentials.
    replay = (await http.post('/harness/auth/v1/rotate', json=body)).json()
    assert replay['device_token'] == second['device_token']
    assert replay['refresh_token'] == second['refresh_token']
    concurrent = (await http.post('/harness/auth/v1/rotate', json=dict(body, rotation_id='competing-operation-2'))).json()
    assert concurrent['refresh_token'] == second['refresh_token']
    # Durable rows survive reopening the real SQLite DB, not just a Python cache.
    from atlas_gate.gate.store import Store
    path = state.settings.ATLAS_GATE_DB
    state.store.close()
    state.store = Store(path)
    replay = (await http.post('/harness/auth/v1/rotate', json=body)).json()
    assert replay['device_token'] == second['device_token']
    responses = await asyncio.gather(*[http.post('/harness/auth/v1/rotate', json=dict(
        refresh_token=second['refresh_token'], rotation_id=f'concurrent-operation-{i}')) for i in range(3)])
    assert all(r.status_code == 200 for r in responses)
    assert len({r.json()['refresh_token'] for r in responses}) == 1
    for creds in (first, second, responses[0].json()):
        assert (await http.get('/harness/settings', headers={'Authorization':'Bearer '+creds['device_token']})).status_code == 200
    # Receipts are ciphertext and token lookup tables contain only hashes.
    dump = '\n'.join(state.store._db.iterdump())
    assert first['device_secret'] not in dump and second['device_token'] not in dump


async def test_old_generation_recovery_expiration_and_revocation(configured):
    http, state, _ = configured
    first = await enroll(http)
    newer = (await http.post('/harness/auth/v1/rotate', json=dict(refresh_token=first['refresh_token'], rotation_id='rotation-operation-1'))).json()
    newest = (await http.post('/harness/auth/v1/rotate', json=dict(refresh_token=newer['refresh_token'], rotation_id='rotation-operation-2'))).json()
    old = await http.post('/harness/auth/v1/rotate', json=dict(refresh_token=first['refresh_token'], rotation_id='different-operation-3'))
    assert old.status_code == 401
    recovered = await http.post('/harness/auth/v1/recover', json=proof(first, state, 'recovery-operation-1'))
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()['device_token'] == newest['device_token']
    bad = dict(proof(first, state, 'recovery-operation-2'), proof='0'*64)
    assert (await http.post('/harness/auth/v1/recover', json=bad)).status_code == 401
    # An expired refresh cannot be made live by a device proof.
    state.store._exec('UPDATE tokens SET refresh_expires_at=? WHERE device_id=?', (time.time()-1,first['device_id']))
    expired = await http.post('/harness/auth/v1/recover', json=proof(first, state, 'recovery-operation-1'))
    assert expired.json()['error']['code'] == 'token_expired'
    state.store.device_revoke(first['device_id'])
    assert (await http.post('/harness/auth/v1/recover', json=proof(first, state, 'recovery-operation-1'))).json()['error']['code'] == 'device_revoked'
    assert (await http.post('/harness/auth/v1/rotate', json=dict(refresh_token=first['refresh_token'], rotation_id='rotation-operation-1'))).json()['error']['code'] == 'device_revoked'
    assert (await http.get('/harness/settings', headers={'Authorization':'Bearer '+first['device_token']})).json()['error']['code'] == 'device_revoked'


async def test_admin_confirmation_keeps_device_identity_and_ownership(configured):
    http, state, _ = configured
    first = await enroll(http)
    state.store._exec('INSERT INTO agent_sessions VALUES (?,?,?)', ('old-sdk-history',first['device_id'],time.time()))
    before = state.store._all('SELECT * FROM agent_sessions')
    start = (await http.post('/harness/auth/v1/enroll/start', json=dict(device_name='restore',platform='edt',app_version='test'))).json()
    approve = dict(user_code=start['user_code'],org='second',user={'id':'other'},recover_device_id=first['device_id'])
    assert (await http.post('/harness/auth/v1/enroll/approve', json=approve)).status_code == 401
    headers = {'X-Atlas-Token':'test-admin-secret'}
    assert (await http.post('/harness/auth/v1/enroll/approve', json=approve,headers=headers)).status_code == 409
    approve.update(org='first',user={'id':'test-user'})
    # Legacy approval / poll must not reinterpret a recovery intent as a new device.
    assert (await http.post('/harness/enroll/approve', json=approve,headers=headers)).status_code == 410
    assert (await http.post('/harness/enroll/poll',json={'device_code':start['device_code']})).status_code == 409
    assert (await http.post('/harness/auth/v1/enroll/approve', json=approve,headers=headers)).status_code == 200
    restored = (await http.post('/harness/auth/v1/enroll/poll',json={'device_code':start['device_code']})).json()
    assert restored['device_id'] == first['device_id']
    assert state.store._all('SELECT * FROM agent_sessions') == before
    assert len(state.store.devices()) == 1
    repeated = await http.post('/harness/auth/v1/enroll/poll',json={'device_code':start['device_code']})
    assert repeated.status_code == 200
    assert repeated.json()['device_token'] == restored['device_token']
    auth = {'Authorization':'Bearer '+restored['device_token']}
    assert (await http.get('/harness/settings',headers=auth)).status_code == 200
    assert (await http.get('/harness/token/identity',headers=auth)).status_code == 404
    discovery = (await http.get('/harness/discovery')).json()
    assert verify_profile(discovery['auth_contract_jws'],first['profile_signing_key']) == discovery['auth_contract']


async def test_legacy_refresh_and_unknown_token_stay_explicit(configured):
    http, state, _ = configured
    first = await enroll(http)
    response = await http.post('/harness/token/refresh',json={'refresh_token':first['refresh_token']})
    assert response.status_code == 200
    assert response.json()['device_id'] == first['device_id']
    assert 'device_secret' not in response.json()
    assert (await http.post('/harness/token/refresh',json={'refresh_token':'unknown'})).json()['error']['code'] == 'unauthorized'


async def test_bearer_never_elevates_to_device_proof_and_stale_current_is_rejected(configured):
    http, state, _ = configured
    first = await enroll(http)
    second = (await http.post('/harness/auth/v1/rotate',json=dict(refresh_token=first['refresh_token'],rotation_id='durable-rotation-1'))).json()
    assert 'device_secret' not in second
    recovered = await http.post('/harness/auth/v1/recover',json=proof(first, state,'device-recovery-1'))
    assert recovered.status_code == 200
    assert 'device_secret' not in recovered.json()
    # Simulate a legacy hash-only replacement / partial rollback. Encrypted current B must
    # not be returned as successful current credentials when actual tokens contain C.
    row = state.store.token_by_refresh_hash(sha256_hex(second['refresh_token']))
    state.store.tokens_set(first['device_id'],sha256_hex('different-access'),row['expires_at'],
        sha256_hex('different-refresh'),row['refresh_expires_at'])
    result = await http.post('/harness/auth/v1/recover',json=proof(first, state,'device-recovery-2'))
    assert result.status_code == 409
    assert result.json()['error']['code'] == 'device_confirmation_required'
    # Even a previously cached recovery receipt cannot conceal this inconsistency.
    assert (await http.post('/harness/auth/v1/recover',json=proof(first, state,'device-recovery-1'))).status_code == 409
    converged = await http.post('/harness/auth/v1/rotate',json=dict(refresh_token=first['refresh_token'],rotation_id='competing-rotation-2'))
    assert converged.status_code == 409


async def test_expired_proof_cannot_revive_after_receipt_cleanup(configured, monkeypatch):
    http, state, _ = configured
    first = await enroll(http)
    old = proof(first,state,'long-lived-operation-1')
    assert (await http.post('/harness/auth/v1/recover',json=old)).status_code == 200
    # This is the cleanup/replay trace: deleting receipts cannot extend a signed proof's validity.
    from atlas_gate.gate import auth_recovery
    original = time.time()
    monkeypatch.setattr(auth_recovery.time,'time',lambda:original+301)
    state.store._exec('DELETE FROM auth_receipts')
    expired = await http.post('/harness/auth/v1/recover',json=old)
    assert expired.status_code == 401
    assert expired.json()['error']['code'] == 'recovery_proof_expired'
    request = {'device_id':first['device_id'],'request_id':'new-recovery-request-2'}
    challenge = await http.post('/harness/auth/v1/challenge',json=request)
    assert challenge.status_code == 200
    signed = challenge.json()['challenge']
    new_proof = dict(request,challenge=signed,proof=hmac_hex(first['device_secret'],dict(
        version=1,purpose='atlas-gate-token-recovery',challenge=signed,**request)))
    assert (await http.post('/harness/auth/v1/recover',json=new_proof)).status_code == 200


async def test_terminal_expired_operation_stays_terminal_after_gc_with_live_challenge(configured, monkeypatch):
    http,state,_ = configured
    from atlas_gate.gate import auth_recovery
    clock = [1000.0]
    monkeypatch.setattr(auth_recovery.time,'time',lambda:clock[0])
    state.settings.ATLAS_GATE_DEVICE_TOKEN_TTL_SEC = 50
    state.settings.ATLAS_GATE_REFRESH_TOKEN_TTL_SEC = 100
    first = await enroll(http)
    clock[0] = 1095
    old = proof(first,state,'terminal-operation-1')
    assert (await http.post('/harness/auth/v1/recover',json=old)).status_code == 200
    clock[0] = 1098
    second = (await http.post('/harness/auth/v1/rotate',json=dict(refresh_token=first['refresh_token'],rotation_id='rotation-operation-1'))).json()
    clock[0] = 1101
    assert (await http.post('/harness/auth/v1/recover',json=old)).json()['error']['code'] == 'token_expired'
    assert (await http.post('/harness/auth/v1/rotate',json=dict(refresh_token=second['refresh_token'],rotation_id='rotation-operation-2'))).status_code == 200
    assert (await http.post('/harness/auth/v1/recover',json=old)).json()['error']['code'] == 'token_expired'
    replacement = proof(first,state,'terminal-operation-1')
    assert (await http.post('/harness/auth/v1/recover',json=replacement)).json()['error']['code'] == 'token_expired'
    assert state.store._one("SELECT payload FROM auth_receipts WHERE predecessor='' LIMIT 1")['payload'] == ''
