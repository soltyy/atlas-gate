"""Question-answer v1 proxy: byte-exact outbox and pinned Router receipt.

Gate has no SDK. Replays use the original Router operation key, never a fresh
prompt/tool_result/steer pair and never another node or boot.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from urllib.parse import quote
from typing import Literal
import uuid

from fastapi import APIRouter, Depends, Request, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.responses import JSONResponse

from .api import gate, require_device
from .errors import GateError, GateRoute
from .state import Device

router = APIRouter(route_class=GateRoute)
HEADER = 'X-Atlas-Question-Answer-Contract'
TARGET_HEADER = 'X-Atlas-Question-Answer-Target-Boot'
SUBMIT_PATH = '/harness/agent/sessions/{sessionId}/question-answer'
STATUS_PATH = '/harness/agent/sessions/{sessionId}/question-answers/{clientMessageId}'


class AnswerRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    version: Literal[1]
    turnId: str = Field(min_length=1, max_length=200)
    callId: str = Field(min_length=1, max_length=200)
    clientMessageId: str
    text: str = Field(min_length=1)

    @field_validator('version', mode='before')
    @classmethod
    def version_one(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError('version must be integer 1')
        return value

    @field_validator('clientMessageId')
    @classmethod
    def canonical_uuid(cls, value):
        if str(uuid.UUID(value)) != value:
            raise ValueError('clientMessageId must be a canonical UUID')
        return value

    @field_validator('text')
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError('answer must not be blank')
        return value


def capability(value, binding):
    required = ('linkedCallback', 'userMessage', 'durableAdmission', 'separateApplicationReceipt', 'immutableRetry')
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1
            or value.get('supported') is not True or value.get('scope') != 'root_turn'
            or value.get('tool') != 'ask_user' or value.get('requiredHeader') != HEADER
            or any(value.get(k) is not True for k in required)
            or value.get('targetNodeId') != binding['node_id'] or value.get('targetBootId') != binding['boot_id']):
        return {'version': 1, 'supported': False}
    return {**{k: value[k] for k in ('version', 'supported', 'scope', 'tool', *required)},
        'targetNodeId': binding['node_id'], 'targetBootId': binding['boot_id'],
        'requiredHeader': HEADER,
        'requiredHeaders': {HEADER: '1', TARGET_HEADER: binding['boot_id']},
        'submitPath': SUBMIT_PATH, 'statusPath': STATUS_PATH,
        'bodyHash': 'sha256-original-http-body-utf8', 'replay': 'same-key-same-bytes'}


async def negotiate(nodes, binding):
    try:
        upstream = await nodes.request(binding['node_id'], 'GET',
            '/v1/sessions/' + quote(binding['local_id'], safe='') + '/sdk-tools',
            params={'questionAnswers': '1'}, expected_boot=binding['boot_id'])
        if upstream.status_code == 200:
            return capability(upstream.json().get('questionAnswers'), binding)
    except (GateError, ValueError, TypeError, AttributeError):
        pass
    return {'version': 1, 'supported': False}


class Answers:
    def __init__(self, store):
        self.store = store
        store._exec('''CREATE TABLE IF NOT EXISTS gate_question_answers(
            id TEXT PRIMARY KEY, device_id TEXT NOT NULL, org TEXT NOT NULL,
            session_id TEXT NOT NULL, client_message_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL, key TEXT NOT NULL, target_boot TEXT NOT NULL,
            raw BLOB NOT NULL, receipt TEXT NOT NULL,
            UNIQUE(device_id,org,client_message_id))''')
        # Keep the legacy answer table shape: older releases INSERT ten values.
        # A missing marker denotes an old possibly dispatched row, never proof
        # that no request reached Router. Rollback keeps its original schema.
        store._exec('''CREATE TABLE IF NOT EXISTS gate_question_answer_dispatches(
            id TEXT PRIMARY KEY, dispatched INTEGER NOT NULL)''')

    def get(self, key=None, *, device=None, sid=None, message=None):
        if key is not None:
            return self.store._one('SELECT * FROM gate_question_answers WHERE id=?', (key,))
        return self.store._one('SELECT * FROM gate_question_answers WHERE device_id=? AND org=? AND session_id=? AND client_message_id=?',
            (device.device_id, device.org, sid, message))

    def admit(self, rid, device, binding, key, boot, raw, body):
        digest = hashlib.sha256(raw).hexdigest()
        fingerprint = hashlib.sha256(json.dumps([device.device_id, device.org, binding['id'],
            binding['node_id'], boot, digest]).encode()).hexdigest()
        with self.store._lock:
            db = self.store._db
            found = db.execute('SELECT * FROM gate_question_answers WHERE id=?', (rid,)).fetchone()
            old = dict(found) if found else None
            if old:
                if old['fingerprint'] != fingerprint:
                    raise GateError(409, 'question_answer_idempotency_conflict', 'Исходный ключ относится к другим байтам или привязке')
                return old, False
            receipt = {'version': 1, 'requestId': key, 'sessionId': binding['id'],
                'turnId': body.turnId, 'callId': body.callId, 'clientMessageId': body.clientMessageId,
                'bodySha256': digest, 'targetNodeId': binding['node_id'], 'targetBootId': boot,
                'uuid': None, 'state': 'unknown', 'gateAdmission': 'accepted',
                'admission': 'unknown', 'callback': 'pending', 'application': 'unknown',
                'replaySafe': True, 'retryAllowed': False}
            try:
                db.execute('INSERT INTO gate_question_answers VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (rid, device.device_id, device.org, binding['id'], body.clientMessageId,
                     fingerprint, key, boot, raw, json.dumps(receipt)))
                db.execute('INSERT INTO gate_question_answer_dispatches VALUES (?,0)', (rid,))
            except sqlite3.IntegrityError:
                raise GateError(409, 'question_answer_identity_conflict', 'Сообщение уже принадлежит исходному ключу') from None
            return dict(db.execute('SELECT * FROM gate_question_answers WHERE id=?', (rid,)).fetchone()), True

    def dispatch(self, row):
        """Linearize possible delivery against a concurrent preflight rejection."""
        with self.store._lock:
            db = self.store._db
            current = json.loads(db.execute('SELECT receipt FROM gate_question_answers WHERE id=?', (row['id'],)).fetchone()['receipt'])
            if current['state'] in ('applied', 'not_applied'):
                return current, False
            db.execute('INSERT INTO gate_question_answer_dispatches VALUES (?,1) '
                       'ON CONFLICT(id) DO UPDATE SET dispatched=1', (row['id'],))
            return current, True

    def save(self, row, receipt, *, pre_dispatch_rejection=False):
        with self.store._lock:
            db = self.store._db
            stored = db.execute('SELECT a.receipt,d.dispatched FROM gate_question_answers a '
                                'LEFT JOIN gate_question_answer_dispatches d ON d.id=a.id WHERE a.id=?', (row['id'],)).fetchone()
            current = json.loads(stored['receipt'])
            if current['state'] in ('applied', 'not_applied'):
                return current
            if pre_dispatch_rejection and stored['dispatched'] != 0:
                return current  # An older probe cannot negate a possible native admission.
            if current.get('uuid') and receipt.get('uuid') and current['uuid'] != receipt['uuid']:
                raise GateError(502, 'question_answer_receipt_identity_mismatch', 'UUID исходного SDK-ввода изменился')
            if current['application'] == 'applied':
                receipt = {**receipt, 'application': 'applied'}
            if current['callback'] == 'released':
                receipt = {**receipt, 'callback': 'released'}
            if receipt['application'] == 'applied' and receipt['callback'] == 'released':
                receipt = {**receipt, 'state': 'applied'}
            db.execute('UPDATE gate_question_answers SET receipt=? WHERE id=?',
                (json.dumps(receipt), row['id']))
            return receipt


def response(receipt, *, status=None):
    return JSONResponse({'questionAnswer': receipt}, status_code=status or (
        200 if receipt['state'] in ('applied', 'not_applied') else 202),
        headers={'Cache-Control': 'private, no-store'})


def merge(row, binding, value):
    old = json.loads(row['receipt'])
    try:
        valid_uuid = isinstance(value, dict) and str(uuid.UUID(value.get('uuid'))) == value.get('uuid')
    except (ValueError, TypeError, AttributeError):
        valid_uuid = False
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1
            or value.get('requestId') != row['id'] or value.get('sessionId') != binding['local_id']
            or any(value.get(k) != old.get(k) for k in
                ('turnId', 'callId', 'clientMessageId', 'bodySha256', 'targetNodeId', 'targetBootId'))
            or value.get('state') not in ('admitted', 'pending', 'applied', 'not_applied', 'unknown')
            or value.get('admission') != 'accepted'
            or value.get('callback') not in ('pending', 'released', 'not_released')
            or value.get('application') not in ('pending', 'applied', 'not_applied', 'unknown')
            or not valid_uuid
            or old.get('uuid') and old['uuid'] != value['uuid']
            or value.get('state') == 'applied' and (value.get('callback') != 'released' or value.get('application') != 'applied')
            or value.get('state') == 'not_applied' and (value.get('callback') != 'not_released' or value.get('application') != 'not_applied')):
        raise GateError(502, 'question_answer_receipt_identity_mismatch', 'Router не подтвердил исходную операцию')
    return {**{k: v for k, v in value.items() if not k.startswith('_')},
        'requestId': row['key'], 'sessionId': binding['id'], 'gateAdmission': 'accepted'}


@router.post('/harness/agent/sessions/{sid}/question-answer')
async def submit(sid: str, body: AnswerRequest, request: Request, device: Device = Depends(require_device)):
    from .agent import _policy, _path
    state = gate(request)
    key = request.headers.get('idempotency-key')
    rid = state.operations.key(device, key)
    target = request.headers.get(TARGET_HEADER)
    if request.headers.get(HEADER) != '1' or not target or len(target) > 200:
        raise GateError(422, 'question_answer_contract_required', 'Нужны contract 1 и исходный target boot')
    binding = state.routing.get(sid, device, include_closed=True)
    _policy(state, device, binding, request)
    raw = await request.body()
    row, fresh = state.question_answers.admit(rid, device, binding, key, target, raw, body)
    cached = json.loads(row['receipt'])
    if cached['state'] in ('applied', 'not_applied'):
        return response(cached)
    if target != binding['boot_id'] or binding['status'] != 'ready':
        if fresh:
            cached.update(state='not_applied', admission='rejected', callback='not_released',
                application='not_applied', reason='question_answer_target_not_active')
            cached = state.question_answers.save(row, cached, pre_dispatch_rejection=True)
        return response(cached, status=409 if cached['admission'] == 'rejected' else None)
    try:
        negotiated = await state.nodes.request(binding['node_id'], 'GET', _path(binding, '/sdk-tools'),
            params={'questionAnswers': '1'}, expected_boot=target)
        if negotiated.status_code != 200:
            raise GateError(503, 'question_answer_negotiation_unavailable', 'Router не подтвердил закреплённый SDK')
        caps = capability(negotiated.json().get('questionAnswers'), binding)
        if caps.get('supported') is not True:
            if fresh:
                cached.update(state='not_applied', admission='rejected', callback='not_released',
                    application='not_applied', reason='question_answer_unsupported')
                cached = state.question_answers.save(row, cached, pre_dispatch_rejection=True)
            return response(cached, status=422 if cached['admission'] == 'rejected' else None)
        # Router owns the atomic native admission. A retry of the identical key
        # is safe even if the first HTTP request is still in flight at that node.
        cached, dispatch = state.question_answers.dispatch(row)
        if not dispatch:
            return response(cached)
        upstream = await state.nodes.request(binding['node_id'], 'POST', _path(binding, '/question-answer'),
            content=bytes(row['raw']), expected_boot=row['target_boot'], request_id=row['id'])
        doc = upstream.json()
        if isinstance(doc, dict) and 'questionAnswer' in doc:
            saved = state.question_answers.save(row, merge(row, binding, doc['questionAnswer']))
            return response(saved, status=upstream.status_code)
        raise GateError(upstream.status_code if upstream.status_code >= 400 else 502,
            'question_answer_unconfirmed', 'Router не вернул квитанцию исходного ответа')
    except GateError as failed:
        if failed.status < 500:
            raise
        return JSONResponse({'error': {'code': failed.code}, 'questionAnswer': cached},
            status_code=503, headers={'Cache-Control': 'private, no-store'})


@router.get('/harness/agent/sessions/{sid}/question-answers/{client_message_id}')
async def observe(sid: str, client_message_id: str, request: Request,
                  turnId: str, callId: str, bodySha256: str = Query(pattern='^[a-f0-9]{64}$'),
                  device: Device = Depends(require_device)):
    from .agent import _policy, _path
    state = gate(request)
    binding = state.routing.get(sid, device, include_closed=True)
    _policy(state, device, binding, request, require_model=False, require_sdk=False)
    row = state.question_answers.get(device=device, sid=sid, message=client_message_id)
    if row is None:
        raise GateError(404, 'question_answer_not_found', 'Исходный ответ не зарегистрирован')
    cached = json.loads(row['receipt'])
    if any(cached[k] != v for k, v in [('turnId', turnId), ('callId', callId), ('bodySha256', bodySha256)]):
        raise GateError(409, 'question_answer_receipt_identity_mismatch', 'Не совпадает идентичность исходного ответа')
    if cached['state'] in ('applied', 'not_applied'):
        return response(cached)
    try:
        upstream = await state.nodes.request(binding['node_id'], 'GET',
            _path(binding, '/question-answers/' + quote(client_message_id, safe='')),
            params={'turnId': turnId, 'callId': callId, 'bodySha256': bodySha256})
        if upstream.status_code in (200, 202):
            saved = state.question_answers.save(row, merge(row, binding, upstream.json()['questionAnswer']))
            return response(saved)
        return response(cached)
    except (GateError, ValueError, KeyError, TypeError):
        return response(cached)
