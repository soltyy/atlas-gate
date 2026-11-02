"""Gate read-only recovery policy; the upstream connection belongs to Router/SDK."""

CAPABILITY = {'version': 1, 'protocol': 'sdk-managed-v1',
              'replayAllowed': False, 'stopRespected': True,
              'continuation': 'explicit_after_reconcile'}


def decorate(event):
    """Do not alter the saved journal, terminal state or original provider error.

    Also handles old Router results where an earlier diagnostic socket error was
    accidentally attached to a subsequent interrupted result.
    """
    kind = event.get('type')
    if kind not in ('provider_retry', 'error', 'result'):
        return event
    if kind == 'result' and event.get('ok'):
        return event
    interrupted = kind == 'result' and 'interrupt' in str(event.get('subtype') or '')
    provider = event.get('providerError')
    expired = (isinstance(provider, dict)
               and provider.get('code') == 'websocket_connection_limit_reached')
    if not interrupted and not expired:
        return event
    retry = kind == 'provider_retry' and event.get('willRetry') is True
    recovery = {'version': 1, 'state': 'stopped' if interrupted else
                ('reconnecting' if retry else 'reconcile_required'),
                'action': 'none' if interrupted else
                ('read_same_turn' if retry else 'explicit_continuation_after_reconcile'),
                'replayAllowed': False}
    if expired and not interrupted:
        recovery['reason'] = 'websocket_connection_limit_reached'
    return dict(event, transportRecovery=recovery)
