"""Steering v1: only Router's exact runtime may promise absorption evidence."""
from .errors import GateError

CONTRACT_HEADER = 'X-Atlas-Steer-Contract'
TARGET_HEADER = 'X-Atlas-Steer-Target-Boot'


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
