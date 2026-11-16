"""Steering v1: only Router's exact runtime may promise absorption evidence."""
import asyncio
import logging
import time
from urllib.parse import quote

import httpx

from .errors import GateError, GateFreshNodeError

CONTRACT_HEADER = 'X-Atlas-Steer-Contract'
TARGET_HEADER = 'X-Atlas-Steer-Target-Boot'
_log = logging.getLogger('atlas_gate')


def _transient_read(failed):
    if isinstance(failed, GateFreshNodeError):
        return failed.retryable
    return failed.code == 'node_unavailable' and isinstance(failed.__cause__, httpx.TransportError)


async def verify_target(nodes, binding, target_boot, body):
    """Retry metadata reads before the first possible dispatch, on the captured boot only."""
    if binding['boot_id'] != target_boot:
        raise GateError(409, 'steer_target_boot_mismatch', 'Исходный runtime сессии изменился')
    node_id = binding['node_id']
    path = '/v1/sessions/' + quote(binding['local_id'], safe='') + '/sdk-tools'
    loop = asyncio.get_running_loop()
    deadline = None
    failure = None
    stage = 'node_identity'
    for delay in (0.15, 0.35, 0.70, 1.2, None):
        try:
            async with asyncio.timeout_at(deadline):
                stage = 'node_identity'
                info = nodes.snapshots.get(node_id)
                if not info or time.monotonic() - info.get('observed', 0) > 5:
                    info = await nodes.probe(node_id)
                if info is None:
                    raise GateFreshNodeError('Идентичность Router не подтверждена',
                        node_id=node_id, method='GET', path='/v1/node', reason='identity_unconfirmed',
                        retryable=nodes.probe_retryable(node_id))
                if info.get('bootId') != target_boot:
                    raise GateError(409, 'steer_target_boot_mismatch', 'Исходный runtime Router изменился')
                declared = capability(info.get('steering'))
                validate(body, declared)
                if declared['targetNodeId'] != node_id or declared['targetBootId'] != target_boot:
                    raise GateError(409, 'steer_target_boot_mismatch', 'Capability относится к другому runtime')
                stage = 'bound_metadata'
                response = await nodes.request(node_id, 'GET', path, params={'steering': '1'},
                                               expected_boot=target_boot)
                if response.status_code not in (502, 503, 504) or 'Retry-After' in response.headers:
                    return response
                failure = response
        except TimeoutError:
            break
        except GateError as failed:
            if not _transient_read(failed):
                raise
            failure = failed
        if deadline is None:
            # The budget bounds recovery reads, not the SDK turn or its tool.
            deadline = loop.time() + 4
            _log.warning('Steer metadata read interrupted: node=%s stage=%s boot=%s',
                         node_id, stage, target_boot)
        if delay is None or loop.time() + delay >= deadline:
            break
        await asyncio.sleep(delay)
    if isinstance(failure, httpx.Response):
        return failure
    raise GateError(503, 'node_unavailable', 'Router не подтвердил назначение досыла',
                    node_id=node_id, preflightStage=stage) from failure


def capability(value):
    if (not isinstance(value, dict) or type(value.get('version')) is not int
            or value.get('version') != 1 or value.get('supported') is False
            or any(value.get(k) is not True for k in
                   ('expectedTurnId', 'clientMessageId', 'durableAbsorption'))
            or value.get('delivery') != 'next_boundary'
            or value.get('requiredHeader') != CONTRACT_HEADER
            or any(not isinstance(value.get(k), str) or not value[k] or len(value[k]) > 200
                   for k in ('targetNodeId', 'targetBootId'))):
        return {'version': 1, 'supported': False}
    attachments = value.get('attachments')
    if not isinstance(attachments, dict) or any(type(attachments.get(k)) is not bool
                                               for k in ('images', 'documents')):
        return {'version': 1, 'supported': False}
    return {'version': 1, 'expectedTurnId': True, 'clientMessageId': True,
            'durableAbsorption': True, 'delivery': 'next_boundary',
            'requiredHeader': CONTRACT_HEADER,
            'requiredHeaders': {CONTRACT_HEADER: '1', TARGET_HEADER: value['targetBootId']},
            'targetNodeId': value['targetNodeId'], 'targetBootId': value['targetBootId'],
            'attachments': {k: attachments[k] for k in ('images', 'documents')}}


def validate(body, caps):
    if caps.get('supported') is False:
        raise GateError(422, 'steering_unsupported', 'Router не подтвердил точный durable steer',
                        state='not_applied', deliveryState='not_sent')
    for item in body.attachments:
        flag = 'images' if item.kind == 'image' else 'documents'
        if not caps['attachments'][flag]:
            raise GateError(422, 'steer_attachments_unsupported',
                            'Router не поддерживает весь смешанный steer; запрос не отправлен',
                            state='not_applied', deliveryState='not_sent')


def bound_capability(value, binding):
    caps = capability(value)
    if caps.get('supported') is not False and (caps['targetNodeId'] != binding['node_id']
                                             or caps['targetBootId'] != binding['boot_id']):
        return {'version': 1, 'supported': False}
    return caps


async def negotiate(nodes, binding):
    """Prove the header roundtrip before publishing a usable bound capability."""
    from urllib.parse import quote
    try:
        response = await nodes.request(binding['node_id'], 'GET',
            '/v1/sessions/' + quote(binding['local_id'], safe='') + '/sdk-tools',
            params={'steering': '1'}, expected_boot=binding['boot_id'])
        if response.status_code == 200:
            return bound_capability(response.json().get('steering'), binding)
    except (GateError, ValueError, AttributeError, TypeError):
        pass
    return {'version': 1, 'supported': False}


def merge(old, new):
    """Validate receipt identity; an ACK must never overwrite prior absorption."""
    if (not isinstance(new, dict) or new.get('version') != 1
            or any(new.get(k) != old.get(k) for k in
                   ('sessionId', 'turnId', 'clientMessageId', 'bodySha256'))
            or new.get('state') not in ('accepted', 'absorbed', 'not_applied', 'unknown')
            or (new.get('uuid') is not None and
                (not isinstance(new['uuid'], str) or not new['uuid']))
            or (old.get('uuid') and new.get('uuid') not in (None, old['uuid']))
            or (new.get('state') in ('accepted', 'absorbed') and not new.get('uuid'))):
        raise ValueError('steer receipt identity mismatch')
    if old['state'] in ('absorbed', 'not_applied'):
        if new['state'] not in (old['state'], 'unknown', 'accepted'):
            raise ValueError('steer terminal outcome conflict')
        return old
    if old.get('_lookupUnknown') and new['state'] == 'accepted':
        return old  # A delayed original HTTP ACK cannot undo current reconciliation.
    return {**old, 'state': new['state'], 'uuid': new.get('uuid') or old.get('uuid')}


def public(value, session_id):
    result = {**{k: v for k, v in value.items() if not k.startswith('_')}, 'sessionId': session_id}
    if value.get('_targetBoot'):
        result['targetBootId'] = value['_targetBoot']
    return result
