"""Owner-bound local identity evidence, never an outcome or execution receipt."""
import json

from .errors import GateError
from .operations import body_sha256

CAPABILITY = {
    'version': 1,
    'pathTemplate': '/harness/agent/sessions/{sid}/history-identity',
    'previousAliasQuery': 'previousAlias',
    'readOnly': True,
    'businessReplayAllowed': False,
    'provesOutcome': False,
}


def identity(recovery, device, request, sid, previous_alias=None):
    def unavailable():
        raise GateError(409, 'history_identity_unavailable',
                        'Непрерывность исходной истории не подтверждена', retryable=False)

    # Read one consistent snapshot under Store's existing non-reentrant lock.
    # Authorization uses Store helpers and therefore runs outside this lock.
    recovery.authorize(device, request, sid)
    with recovery.store._lock:
        rows = recovery.store._db.execute(
            'SELECT id,device_id,org,route,node_id,model,status,sdk_alias,sdk_id,recovery_source,response '
            'FROM router_bindings WHERE device_id=? AND org=?',
            (device.device_id, device.org)).fetchall()
        owned = {row['id']: dict(row) for row in rows}
    target = owned.get(sid)
    if target is None:
        raise GateError(404, 'not_found', 'Исходная привязка не найдена')
    if target.get('api_attempt') or target['status'] not in ('ready', 'closing', 'closed'):
        unavailable()
    if not target.get('sdk_alias'):
        unavailable()
    result = {'version': 1, 'proofType': 'source_history_identity',
              'gateRoot': recovery.state.public_url.rstrip('/'),
              'sessionId': target['id'], 'orgId': target['org'],
              'routeId': target['route'], 'model': target['model'],
              'historyAlias': target['sdk_alias'],
              'businessReplayAllowed': False, 'outcomeKnown': False,
              'continuity': {'kind': 'current_binding'}}
    if previous_alias and previous_alias != target['sdk_alias']:
        # Ownership is checked before following an alias or disclosing a SID.
        source = next((row for row in owned.values() if row['sdk_alias'] == previous_alias), None)
        if not source:
            raise GateError(404, 'not_found', 'Исходная привязка не найдена')
        current, visited = target, {target['id']}
        while True:
            try:
                edge = json.loads(current.get('recovery_source') or '{}')
                ack = json.loads(current.get('response') or '{}')
            except (ValueError, TypeError):
                unavailable()
            if (not isinstance(edge, dict) or not isinstance(ack, dict)
                    or edge.get('allowHistoryFallback') is not False
                    or ack.get('resumed') is not True or ack.get('resumeError') is not None):
                unavailable()
            predecessor = edge.get('previousSessionId')
            if not isinstance(predecessor, str) or predecessor in visited:
                unavailable()
            prior = owned.get(predecessor)
            if prior is None:
                raise GateError(404, 'not_found', 'Исходная привязка не найдена')
            recovery.authorize(device, request, predecessor)
            if (prior.get('api_attempt') or prior['status'] not in ('ready', 'closing', 'closed')
                    or not target.get('sdk_id') or not prior.get('sdk_id')
                    or current.get('sdk_id') != target['sdk_id'] or prior['sdk_id'] != target['sdk_id']
                    or any(prior.get(k) != target.get(k) for k in
                           ('device_id', 'org', 'route', 'model', 'node_id'))):
                unavailable()
            visited.add(predecessor)
            if predecessor == source['id']:
                result['continuity'] = {'kind': 'same_native_history',
                                        'previousAlias': previous_alias,
                                        'previousSessionId': prior['id']}
                break
            current = prior
    elif previous_alias:
        result['continuity'].update(previousAlias=previous_alias, previousSessionId=target['id'])
    result['proofId'] = body_sha256(result)
    return result
