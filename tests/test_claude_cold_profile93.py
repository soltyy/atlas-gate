from atlas_gate.gate.crypto import verify_profile
from .test_network_gate import network


async def test_signed_profile_refreshes_exact_node_recovery_without_reload(network):
    c, app, *_ = network
    state = app.state.gate
    names = list(state.nodes.snapshots)
    assert len(names) == 2
    first, second = names
    before = (await c.get('/harness/settings')).json()['profile']
    state.nodes.snapshots[first]['bootId'] = 'claude-new-boot'
    state.nodes.snapshots[first]['sessionRecoveryProofs'] = {
        'version': 1, 'supported': True, 'resumeTransport': True,
        'native': {'protocol': 'claude-history-only-v1', 'noActionRestoreQualified': True}}
    state.nodes.snapshots[second]['sessionRecoveryProofs'] = {
        'version': 1, 'supported': True, 'resumeTransport': False,
        'native': {'reason': 'sdk_restore_account_policy_unqualified'}}
    await state.publish_contexts()
    doc = (await c.get('/harness/settings')).json()
    profile = verify_profile(doc['profile_jws'], doc['profile_signing_key'])
    assert profile == doc['profile'] and profile['profile_version'] > before['profile_version']
    model = next(m for m in profile['models'] if m['model'] == 'stub-fast' and m['route_id'] == 'claude-sub')
    nodes = model['sessionRecovery']['nodes']
    assert nodes[first]['resumeTransport'] is True and nodes[first]['bootId'] == 'claude-new-boot'
    assert nodes[second]['resumeTransport'] is False
    assert profile['agents'] == before['agents']
