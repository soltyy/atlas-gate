import httpx

from atlas_gate.gate.operations import body_sha256
from .test_api_attempts import managed, provider_available, configured, provider, completion, BODY, HEADERS, CHAT
from .test_discovery import enroll


async def test_lost_attempt_id_recovers_by_original_key_without_post(managed):
    client, state, _ = managed
    calls = []
    def reply(request):
        calls.append(request)
        return httpx.Response(200, json=completion())
    await provider(state, reply)
    assert (await client.post(CHAT, json=BODY, headers=HEADERS)).status_code == 200
    query = {'requestId': 'attempt-one', 'route_id': 'private-api', 'model': BODY['model'],
             'bodySha256': body_sha256(BODY)}
    # These are read-only lookups even if cancellation occurred after dispatch.
    for _ in range(2):
        r = await client.get('/harness/llm/attempts/lookup', params=query)
        assert r.status_code == 200 and r.json()['state'] == 'complete'
        assert r.headers['Cache-Control'] == 'private, no-store'
    assert (await client.get('/harness/llm/attempts/lookup', params={**query, 'bodySha256': 'a' * 64})).status_code == 409
    assert len(calls) == 1
    other = await enroll(client)
    client.headers['Authorization'] = 'Bearer ' + other['device_token']
    assert (await client.get('/harness/llm/attempts/lookup', params=query)).status_code == 404
    assert len(calls) == 1


async def test_missing_key_is_unknown_and_does_not_create_inference(managed):
    client, state, _ = managed
    def forbidden(request):
        raise AssertionError('lookup must never invoke provider')
    await provider(state, forbidden)
    r = await client.get('/harness/llm/attempts/lookup', params={'requestId': 'never-seen',
        'route_id': 'private-api', 'model': BODY['model'], 'bodySha256': body_sha256(BODY)})
    assert r.status_code == 404
    assert r.json()['error']['deliveryState'] == 'unknown' and r.json()['error']['notSentProven'] is False
    assert state.store._one('SELECT COUNT(*) n FROM api_attempts')['n'] == 0
