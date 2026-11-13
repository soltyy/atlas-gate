"""Admission/drain observation includes detached API producers and incoming SDK I/O."""
class GateActivity:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        method = scope.get('method')
        tracked = (scope['type'] == 'http' and (
            path.startswith('/harness/agent/') and (method != 'GET' or path.endswith(('/context', '/files', '/sdk-tools', '/files/adaptive'))) or
            path.startswith('/harness/llm/v1/') and method == 'POST' or
            path.startswith('/harness/recovery/') and method == 'POST'))
        if not tracked:
            return await self.app(scope, receive, send)
        owner = scope['app']
        owner.state.active_gate_requests = getattr(owner.state, 'active_gate_requests', 0) + 1
        try:
            await self.app(scope, receive, send)
        finally:
            owner.state.active_gate_requests -= 1
