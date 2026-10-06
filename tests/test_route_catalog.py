from types import SimpleNamespace
from atlas_gate.gate.nodes import Nodes


def test_route_catalog_keeps_cached_models_scoped_to_approved_nodes():
    nodes = object.__new__(Nodes)
    def node(org='org', route='claude', enabled=True):
        return SimpleNamespace(orgs=[org], routes=[route], enabled=enabled)
    nodes.config = {'claude': node(), 'codex': node(route='codex'), 'foreign': node(org='other'),
                    'disabled': node(enabled=False), 'revoked': node()}
    nodes.catalog = {key: [{'value': key, 'name': key.upper()}] for key in nodes.config}
    nodes.snapshots = {}  # No live status: cached catalog survives an outage.
    nodes.revoked = lambda key: key == 'revoked'
    assert nodes.route_catalog('org', 'claude') == [{'value': 'claude', 'name': 'CLAUDE'}]
    assert nodes.route_catalog('org', 'codex') == [{'value': 'codex', 'name': 'CODEX'}]
    assert nodes.route_catalog('org', 'missing') == []
