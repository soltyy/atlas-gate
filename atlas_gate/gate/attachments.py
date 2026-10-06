"""Проекция возможностей узлов и preflight без PDF/SDK зависимостей."""
import base64
import binascii
from .errors import GateError

FLAGS = ('text', 'image', 'pdfNative', 'documentPages', 'agentDocumentTools')
LIMITS = ('maxFiles', 'maxRawBytes', 'maxTotalBytes', 'maxTextChars', 'maxPdfPages', 'maxPagePixels')


def node_attachments(info):
    value = (info or {}).get('attachments')
    if not isinstance(value, dict) or value.get('schemaVersion') != 1:
        return None
    if any(not isinstance(value.get(k), bool) for k in FLAGS):
        return None
    if any(type(value.get(k)) is not int or value[k] <= 0 for k in LIMITS):
        return None
    return dict(schemaVersion=1, source='router-reported',
                documentAuthorization=value.get('documentAuthorization') if isinstance(value.get('documentAuthorization'), bool) else None,
                **{k: value[k] for k in FLAGS+LIMITS})


def route_attachments(state, org, route, model):
    candidates = [n for n in state.nodes.config.values() if n.enabled and org in n.orgs and route in n.routes
                  and not state.nodes.revoked(n.node_id)
                  and any(m.get('value') == model for m in state.nodes.catalog.get(n.node_id, []))]
    values = [node_attachments(state.nodes.snapshots.get(n.node_id)) for n in candidates]
    if not values or any(v is None for v in values):
        return None  # неизвестный/недоступный узел не становится обещанием поддержки PDF
    authorization = [v.get('documentAuthorization') for v in values]
    known = False if any(v is False for v in authorization) else True if all(v is True for v in authorization) else None
    return dict(schemaVersion=1, source='router-reported', documentAuthorization=known,
                **{k: all(v[k] for v in values) for k in FLAGS},
                **{k: min(v[k] for v in values) for k in LIMITS})


def validate_prompt(body, caps):
    # Общий transport ceiling есть и для старых Router. Размеры считаются, sizeBytes не доверяем.
    limits = {'maxFiles':8, 'maxRawBytes':15*1024*1024, 'maxTotalBytes':20*1024*1024, 'maxTextChars':1_000_000}
    if caps:
        limits.update({k:min(limits[k], caps[k]) for k in limits})
    if len(body.attachments) > limits['maxFiles']:
        raise GateError(422, 'attachment_limit', 'Превышено число вложений', requestId=body.id)
    total, text_size = 0, len(body.text)
    for a in body.attachments:
        if len(a.filename) > 512:
            raise GateError(422, 'attachment_limit', 'Имя вложения превышает 512 символов', requestId=body.id)
        if a.kind not in ('text', 'image', 'pdf'):
            raise GateError(422, 'attachment_invalid', 'Неизвестный тип вложения', requestId=body.id)
        if caps and a.kind=='image' and not caps['image']:
            raise GateError(422, 'unsupported_media', 'Этот Router не принимает изображения', requestId=body.id)
        if caps and a.kind=='pdf' and not (caps['pdfNative'] or caps['documentPages']):
            raise GateError(422, 'unsupported_media', 'Этот Router не принимает PDF', requestId=body.id)
        if a.kind=='text':
            size = len(a.data.encode())
            text_size += len(a.data)+len(a.filename)+100
        else:
            if len(a.data) > (limits['maxRawBytes']+2)//3*4:
                raise GateError(422, 'attachment_limit', 'Вложение слишком велико', requestId=body.id)
            try:
                raw = base64.b64decode(a.data, validate=True)
            except (binascii.Error, ValueError):
                raise GateError(422, 'attachment_invalid', 'Некорректный base64 вложения', requestId=body.id) from None
            if not raw or (a.kind=='pdf' and not raw.startswith(b'%PDF-')):
                raise GateError(422, 'attachment_invalid', 'Вложение пусто или не является PDF', requestId=body.id)
            size = len(raw)
        if size > limits['maxRawBytes']:
            raise GateError(422, 'attachment_limit', 'Вложение слишком велико', requestId=body.id)
        total += size
    if total > limits['maxTotalBytes'] or text_size > limits['maxTextChars']:
        raise GateError(422, 'attachment_limit', 'Превышен суммарный предел вложений или текста', requestId=body.id)
