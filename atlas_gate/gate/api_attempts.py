"""Opt-in durable API attempts. Provider calls are never retried by this ledger.

Only hashes of requests are stored; provider output is private, owner-bound data.
The Store's SQLite transaction covers terminal receipt and the single usage row.
This module intentionally has no SDK dependency and no provider retrieve/cancel API.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import time
import uuid
from typing import Any

from fastapi.responses import JSONResponse, Response

from .errors import GateError
from .operations import body_sha256

CONTRACT_HEADER = 'x-atlas-api-attempt-contract'
TERMINAL_STATES = ('complete', 'outcome_unknown', 'rejected_not_sent')
ENDPOINTS = ('/chat/completions', '/responses', '/messages', '/images/generations')


def api_attempt_contract() -> dict[str, Any]:
    return {'version': 1, 'header': 'X-Atlas-API-Attempt-Contract',
            'keyHeader': 'Idempotency-Key', 'savedResult': True, 'detach': True,
            'receiptEndpoint': '/harness/llm/attempts/{attemptId}',
            'receiptByKeyEndpoint': '/harness/llm/attempts/lookup',
            'receiptByKeyQuery': ['requestId', 'route_id', 'model', 'bodySha256'],
            'reconcileEndpoint': '/harness/llm/attempts/reconcile',
            'admissionFence': {'version': 1, 'scope': 'managed_api_contract_1',
                               'negativeProof': True, 'protectsLegacy': False},
            'resultEndpoint': '/harness/llm/attempts/{attemptId}/result',
            'upstreamRetrieve': False, 'upstreamCancel': False,
            'providerKeyScope': 'gate_attempt',
            'providerRetry': False, 'retention': 'until_explicit_retirement',
            'storage': 'private_gate_database', 'selectedChoiceHeader': 'X-Atlas-Choice-Index'}


def opted_in(headers) -> bool:
    version = headers.get(CONTRACT_HEADER)
    if version is not None and version != '1':
        raise GateError(422, 'api_attempt_version_unsupported', 'неподдержанная версия API attempt contract',
                        deliveryState='not_sent')
    return version == '1'


def request_key(headers) -> str:
    key = headers.get('idempotency-key') or headers.get('x-harness-model-call')
    if not key or len(key) > 200:
        raise GateError(422, 'api_attempt_key_required', 'API attempt требует неизменный ключ длиной 1..200',
                        deliveryState='not_sent')
    return key


def selected_choice(headers) -> int:
    try:
        value = int(headers.get('x-atlas-choice-index', '0'))
    except (TypeError, ValueError) as failed:
        raise GateError(422, 'api_attempt_choice_invalid', 'choice index должен быть неотрицательным целым',
                        deliveryState='not_sent') from failed
    if value < 0:
        raise GateError(422, 'api_attempt_choice_invalid', 'choice index должен быть неотрицательным целым',
                        deliveryState='not_sent')
    return value


def fingerprint(route_id, model, suffix, body, headers) -> str:
    native_headers = {name: headers.get(name) for name in
                      ('anthropic-version', 'anthropic-beta', 'x-atlas-choice-index',
                       'x-atlas-conversation-id', 'x-atlas-branch-id',
                       'x-atlas-pricing-selection') if headers.get(name) is not None}
    # Arrays and opaque provider content remain in their original order.
    try:
        value = [route_id, model, suffix, json.loads(body), native_headers]
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                        ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    except (ValueError, TypeError) as failed:
        raise GateError(422, 'api_attempt_body_invalid', 'API attempt требует канонический JSON без NaN/Infinity',
                        deliveryState='not_sent') from failed


class ApiAttempts:
    def __init__(self, store):
        self.store = store
        self.tasks: set[asyncio.Task] = set()
        self.changed: dict[str, asyncio.Event] = {}
        self.runtime_failures: dict[str, str] = {}
        self.admission = None  # Root installs the branch admission barrier.
        with store._lock:
            store._db.executescript('''
                CREATE TABLE IF NOT EXISTS api_attempts (
                    id TEXT PRIMARY KEY, org TEXT NOT NULL, device TEXT NOT NULL,
                    user_id TEXT NOT NULL, key_hash TEXT NOT NULL, fingerprint TEXT NOT NULL, body_sha256 TEXT,
                    route TEXT NOT NULL, model TEXT NOT NULL, suffix TEXT NOT NULL,
                    conversation TEXT, branch TEXT, state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                    status INTEGER, headers TEXT NOT NULL DEFAULT '{}', summary TEXT NOT NULL DEFAULT '{}',
                    usage_row INTEGER, admission_fence INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(org,device,key_hash));
                CREATE TABLE IF NOT EXISTS api_attempt_chunks (
                    attempt TEXT NOT NULL, seq INTEGER NOT NULL, payload BLOB NOT NULL,
                    PRIMARY KEY(attempt,seq));
            ''')
            columns = {r[1] for r in store._db.execute('PRAGMA table_info(api_attempts)')}
            for column in ('conversation', 'body_sha256'):
                if column not in columns:
                    store._db.execute(f'ALTER TABLE api_attempts ADD COLUMN {column} TEXT')
            if 'admission_fence' not in columns:
                store._db.execute('ALTER TABLE api_attempts ADD COLUMN admission_fence INTEGER NOT NULL DEFAULT 0')
            # One active Gate owns Store. Its previous producers cannot still write
            # after restart. A missing terminal is uncertainty, never a retry permit.
            now = time.time()
            store._db.execute("UPDATE api_attempts SET state='rejected_not_sent', updated=?, "
                              "summary=? WHERE state='prepared'",
                              (now, json.dumps({'outcome': 'sealed-not-sent', 'terminalKnown': True,
                                               'deliveryState': 'not_sent', 'usageStatus': 'unknown',
                                               'retry': {'allowed': False, 'scope': 'new_intent_after_condition'}})))
            store._db.execute("UPDATE api_attempts SET state='outcome_unknown', updated=?, "
                              "summary=? WHERE state='running'",
                              (now, json.dumps({'outcome': 'unknown', 'terminalKnown': False,
                                               'usageStatus': 'unknown', 'error': {'category': 'producer_lost'},
                                               'retry': {'allowed': False, 'scope': 'reconcile'}})))

    def claim(self, device, route, model, suffix, body, headers, branch=None):
        key_hash = hashlib.sha256(request_key(headers).encode()).hexdigest()
        with self.store._lock:
            db = self.store._db
            db.execute('BEGIN IMMEDIATE')
            try:
                row = db.execute('SELECT * FROM api_attempts WHERE org=? AND device=? AND key_hash=?',
                                 (device.org, device.device_id, key_hash)).fetchone()
                if row and row['admission_fence']:
                    self._fence_matches(row, route.id, model, suffix, self._body_hash(body))
                    db.execute('COMMIT')
                    return dict(row), False
                wanted = fingerprint(route.id, model, suffix, body, headers)
                selected_choice(headers)
                if row:
                    if row['fingerprint'] != wanted or row['branch'] != branch:
                        raise GateError(409, 'idempotency_conflict', 'API attempt изменил параметры или ветвь',
                                        attemptId=row['id'], deliveryState='not_sent')
                    result, fresh = dict(row), False
                else:
                    now, aid = time.time(), uuid.uuid4().hex
                    db.execute('INSERT INTO api_attempts '
                               '(id,org,device,user_id,key_hash,fingerprint,body_sha256,route,model,suffix,conversation,branch,state,created,updated) '
                               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'prepared',?,?)",
                               (aid, device.org, device.device_id, device.user_id, key_hash, wanted,
                                body_sha256(json.loads(body)), route.id, model, suffix,
                                headers.get('x-atlas-conversation-id'), branch, now, now))
                    result = dict(db.execute('SELECT * FROM api_attempts WHERE id=?', (aid,)).fetchone())
                    fresh = True
                db.execute('COMMIT')
                return result, fresh
            except BaseException:
                db.execute('ROLLBACK')
                raise

    def owned(self, attempt_id, device):
        row = self.store._one('SELECT * FROM api_attempts WHERE id=? AND org=? AND device=?',
                              (attempt_id, device.org, device.device_id))
        if row is None:
            raise GateError(404, 'api_attempt_not_found', 'API attempt не найден')
        return row

    def lookup_request(self, device, model, suffix, body, headers):
        key_hash = hashlib.sha256(request_key(headers).encode()).hexdigest()
        row = self.store._one('SELECT * FROM api_attempts WHERE org=? AND device=? AND key_hash=?',
                              (device.org, device.device_id, key_hash))
        if row is not None:
            if row['admission_fence']:
                self._fence_matches(row, row['route'], model, suffix, self._body_hash(body))
                return row
            if (row['fingerprint'] != fingerprint(row['route'], model, suffix, body, headers)
                    or row['branch'] != headers.get('x-atlas-branch-id')):
                raise GateError(409, 'idempotency_conflict', 'API attempt изменил параметры или ветвь',
                                attemptId=row['id'], deliveryState='not_sent')
        return row

    @staticmethod
    def _body_hash(raw):
        try:
            return body_sha256(json.loads(raw))
        except (ValueError, TypeError) as failed:
            raise GateError(422, 'api_attempt_body_invalid', 'API attempt требует канонический JSON без NaN/Infinity',
                            deliveryState='not_sent') from failed

    @staticmethod
    def _fence_matches(row, route_id, model, suffix, wanted_hash):
        if (row['route'], row['model'], row['suffix'], row['body_sha256']) != (route_id, model, suffix, wanted_hash):
            raise GateError(409, 'idempotency_conflict', 'API admission fence относится к другому исходному запросу',
                            attemptId=row['id'], deliveryState='not_sent')

    def reconcile(self, device, route, model: str, suffix: str, key: str, body_sha256: str,
                  *, conversation_id=None, branch_id=None):
        """Atomically observe admission or fence the original managed API key.

        A GET 404 is not this proof. A prepared row can be sealed because every
        network write follows dispatched()'s durable running marker. Existing
        running/unknown/terminal attempts retain their exact original outcome.
        This protects only the explicitly negotiated managed API contract.
        """
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            raise GateError(422, 'api_attempt_key_required', 'API attempt требует неизменный ключ длиной 1..200')
        if suffix not in ENDPOINTS or not isinstance(body_sha256, str) or not re.fullmatch('[a-f0-9]{64}', body_sha256):
            raise GateError(422, 'api_attempt_identity_invalid', 'нужны точный endpoint и SHA-256 исходного JSON')
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        with self.store._lock:
            db = self.store._db
            db.execute('BEGIN IMMEDIATE')
            try:
                previous = db.execute('SELECT * FROM api_attempts WHERE org=? AND device=? AND key_hash=?',
                                      (device.org, device.device_id, key_hash)).fetchone()
                if previous is not None:
                    row = dict(previous)
                    if (row['route'], row['model'], row['suffix']) != (route.id, model, suffix) or (
                            row['body_sha256'] is not None and row['body_sha256'] != body_sha256):
                        raise GateError(409, 'idempotency_conflict', 'API reconcile изменил исходный target или body',
                                        attemptId=row['id'])
                    seal = row['state'] == 'prepared' and row['body_sha256'] is not None
                else:
                    now, aid = time.time(), uuid.uuid4().hex
                    db.execute('INSERT INTO api_attempts '
                               '(id,org,device,user_id,key_hash,fingerprint,body_sha256,route,model,suffix,conversation,branch,state,created,updated) '
                               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'prepared',?,?)",
                               (aid, device.org, device.device_id, device.user_id, key_hash, 'admission-fence:' + aid,
                                body_sha256, route.id, model, suffix, conversation_id, branch_id, now, now))
                    row = dict(db.execute('SELECT * FROM api_attempts WHERE id=?', (aid,)).fetchone())
                    seal = True
                if seal:
                    proof = {'version': 1, 'proofId': 'api-admission:' + row['id'],
                             'scope': 'managed_api_admission_fence', 'contractVersion': 1,
                             'legacyProtected': False, 'attemptId': row['id'],
                             'routeId': row['route'], 'model': row['model'], 'endpoint': row['suffix'],
                             'bodySha256': row['body_sha256'], 'keySha256': row['key_hash'],
                             'notSentProven': True, 'providerDispatch': False}
                    raw = json.dumps({'error': {'code': 'api_attempt_not_sent',
                         'message': 'исходный managed API intent закрыт до отправки провайдеру',
                         'deliveryState': 'not_sent'}, 'admissionFence': proof},
                         sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
                    summary = {**proof, 'outcome': 'sealed-not-sent', 'terminalKnown': True,
                               'deliveryState': 'not_sent', 'runState': 'response-ready', 'usageStatus': 'unknown',
                               'hostExecutionAllowed': False, 'resultSha256': hashlib.sha256(raw).hexdigest(),
                               'retry': {'allowed': False, 'scope': 'new_intent_after_condition'}}
                    # Prepared has no provider output by invariant. Any local
                    # unissued admission-error staging bytes are replaced atomically.
                    db.execute('DELETE FROM api_attempt_chunks WHERE attempt=?', (row['id'],))
                    db.execute('INSERT INTO api_attempt_chunks(attempt,seq,payload) VALUES (?,1,?)', (row['id'], raw))
                    db.execute("UPDATE api_attempts SET admission_fence=1,state='rejected_not_sent',status=409,headers=?,summary=?,updated=? WHERE id=?",
                               (json.dumps({'content-type': 'application/json'}), json.dumps(summary, ensure_ascii=False),
                                time.time(), row['id']))
                    row = dict(db.execute('SELECT * FROM api_attempts WHERE id=?', (row['id'],)).fetchone())
                sizes = dict(db.execute('SELECT COUNT(*) AS chunks,COALESCE(SUM(length(payload)),0) AS bytes '
                                        'FROM api_attempt_chunks WHERE attempt=?', (row['id'],)).fetchone())
                result = self._snapshot(row, sizes)
                if row['body_sha256'] is None:
                    result['identityConfirmed'] = False  # Never backfill a legacy claim from caller input.
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
        self._notify(row['id'])
        return result

    def snapshot(self, attempt_id, device):
        row = self.owned(attempt_id, device)
        sizes = self.store._one('SELECT COUNT(*) AS chunks, COALESCE(SUM(length(payload)),0) AS bytes '
                                'FROM api_attempt_chunks WHERE attempt=?', (attempt_id,))
        return self._snapshot(row, sizes)

    def _snapshot(self, row, sizes):
        attempt_id = row['id']
        result = {'version': 1, 'attemptId': row['id'], 'routeId': row['route'], 'model': row['model'],
                'endpoint': row['suffix'], 'conversationId': row['conversation'],
                'branchId': row['branch'], 'state': row['state'],
                'bodySha256': row['body_sha256'],
                'createdAt': row['created'], 'updatedAt': row['updated'], 'httpStatus': row['status'],
                'resultAvailable': bool(sizes['chunks']), 'resultBytes': sizes['bytes'],
                'receiptPath': f'/harness/llm/attempts/{row["id"]}',
                'resultPath': f'/harness/llm/attempts/{row["id"]}/result',
                **json.loads(row['summary'])}
        if attempt_id in self.runtime_failures and row['state'] not in TERMINAL_STATES:
            result.update(state='outcome_unknown', durableState=row['state'], terminalKnown=False,
                          outcome='unknown', usageStatus='unknown', producerAlive=False,
                          error={'category': 'local_persistence', 'code': self.runtime_failures[attempt_id]},
                          retry={'allowed': False, 'scope': 'reconcile'})
        return result

    def headers(self, row):
        return {**json.loads(row['headers']), 'X-Atlas-API-Attempt-Id': row['id'],
                'X-Atlas-API-Attempt-State': row['state'],
                'X-Atlas-API-Attempt-Receipt': f'/harness/llm/attempts/{row["id"]}',
                'Cache-Control': 'private, no-store'}

    def replay(self, row, device, *, result=False):
        if row['id'] in self.runtime_failures and row['state'] not in TERMINAL_STATES:
            row = {**row, 'state': 'outcome_unknown'}
        if row['state'] not in TERMINAL_STATES:
            return JSONResponse(self.snapshot(row['id'], device), status_code=202,
                                headers={**self.headers(row), 'Retry-After': '1'})
        if row['state'] == 'outcome_unknown' and not result:
            return JSONResponse({'error': {'code': 'api_attempt_outcome_unknown',
                                'message': 'исход прежнего API attempt неизвестен; новый dispatch запрещён',
                                'attemptId': row['id'], 'deliveryState': 'unknown'},
                                 'attempt': self.snapshot(row['id'], device)},
                                status_code=409, headers=self.headers(row))
        chunks = self.store._all('SELECT payload FROM api_attempt_chunks WHERE attempt=? ORDER BY seq', (row['id'],))
        if row['status'] is None or not chunks:
            return JSONResponse(self.snapshot(row['id'], device), status_code=409,
                                headers=self.headers(row))
        return Response(b''.join(bytes(chunk['payload']) for chunk in chunks), status_code=row['status'],
                        headers=self.headers(row))

    def dispatched(self, aid, *, request=None, device=None, route=None):
        with self.store._lock:
            db = self.store._db
            db.execute('BEGIN IMMEDIATE')
            try:
                row = db.execute('SELECT state FROM api_attempts WHERE id=?', (aid,)).fetchone()
                if row is not None and row['state'] == 'rejected_not_sent':
                    db.execute('COMMIT')
                    return False
                if row is None or row['state'] != 'prepared':
                    raise GateError(409, 'api_attempt_dispatch_conflict', 'API attempt уже отправлен или закрыт')
                if self.admission is not None:
                    result = self.admission(request, device, route, aid, db=db)
                    if inspect.isawaitable(result):
                        if inspect.iscoroutine(result):
                            result.close()
                        raise GateError(503, 'api_attempt_admission_invalid', 'API admission должен быть синхронным')
                db.execute("UPDATE api_attempts SET state='running',updated=? WHERE id=?", (time.time(), aid))
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
        self.changed.setdefault(aid, asyncio.Event())
        return True

    def response_headers(self, aid, status, headers):
        self.store._exec('UPDATE api_attempts SET status=?,headers=?,updated=? WHERE id=?',
                         (status, json.dumps(headers), time.time(), aid))
        self._notify(aid)

    def add_response_headers(self, aid, headers):
        """Save server-selected metadata added by the API layer after proxy()."""
        with self.store._lock:
            db = self.store._db
            row = db.execute('SELECT headers,admission_fence FROM api_attempts WHERE id=?', (aid,)).fetchone()
            if row is None or row['admission_fence']:
                return
            merged = json.loads(row['headers'])
            merged.update({k: v for k, v in headers.items() if k.lower() in
                           ('x-atlas-reasoning-decision', 'x-atlas-configured-reasoning')})
            db.execute('UPDATE api_attempts SET headers=? WHERE id=?', (json.dumps(merged), aid))

    def append(self, aid, chunk):
        with self.store._lock:
            db = self.store._db
            db.execute('INSERT INTO api_attempt_chunks(attempt,seq,payload) '
                       'SELECT ?,COALESCE(MAX(seq),0)+1,? FROM api_attempt_chunks WHERE attempt=?',
                       (aid, chunk, aid))
        self._notify(aid)

    def finish(self, aid, summary, *, state='complete', usage_row=None, pricing_decision=None):
        with self.store._lock:
            db = self.store._db
            db.execute('BEGIN IMMEDIATE')
            try:
                row = db.execute('SELECT state FROM api_attempts WHERE id=?', (aid,)).fetchone()
                if row is None or row['state'] in TERMINAL_STATES:
                    db.execute('COMMIT')
                    return
                result_hash, chunks = hashlib.sha256(), 0
                for chunk in db.execute('SELECT payload FROM api_attempt_chunks WHERE attempt=? ORDER BY seq', (aid,)):
                    result_hash.update(bytes(chunk['payload']))
                    chunks += 1
                summary = {**summary, 'resultSha256': result_hash.hexdigest() if chunks else None}
                usage_id = None
                if usage_row is not None:
                    values = {**usage_row, 'ts': time.time()}
                    cols = ','.join(values)
                    usage_id = db.execute(f'INSERT INTO usage({cols}) VALUES ({",".join("?" * len(values))})',
                                          tuple(values.values())).lastrowid
                    if pricing_decision is not None:
                        db.execute('INSERT INTO gateway_usage_pricing VALUES (?,?)',
                                   (usage_id, json.dumps(pricing_decision)))
                db.execute('UPDATE api_attempts SET state=?,summary=?,usage_row=?,updated=? WHERE id=?',
                           (state, json.dumps(summary, ensure_ascii=False), usage_id, time.time(), aid))
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
        self._notify(aid)

    def _notify(self, aid):
        event = self.changed.get(aid)
        if event:
            event.set()

    def producer_failed(self, aid, code):
        # This is explicitly runtime evidence, not a forged durable terminal.
        # It prevents a dead producer or failed final commit from leaving a
        # consumer waiting forever while persistence is being repaired.
        self.runtime_failures[aid] = code
        self._notify(aid)

    def start(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def stream(self, aid):
        cursor = 0
        event = self.changed.setdefault(aid, asyncio.Event())
        while True:
            event.clear()
            chunks = self.store._all('SELECT seq,payload FROM api_attempt_chunks WHERE attempt=? AND seq>? ORDER BY seq',
                                      (aid, cursor))
            for chunk in chunks:
                cursor = chunk['seq']
                yield bytes(chunk['payload'])
            row = self.store._one('SELECT state FROM api_attempts WHERE id=?', (aid,))
            if row is None or row['state'] in TERMINAL_STATES or aid in self.runtime_failures:
                # finish and append are synchronous and serialized; the next read
                # covers a final append made between our first read and state read.
                tail = self.store._all('SELECT payload FROM api_attempt_chunks WHERE attempt=? AND seq>? ORDER BY seq',
                                       (aid, cursor))
                for chunk in tail:
                    yield bytes(chunk['payload'])
                return
            await event.wait()

    async def aclose(self):
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def replay_request(request, device, org, model, protocol, suffix, raw):
    """Early replay before mutable price/quota/key/default admission. ACL still applies."""
    if not opted_in(request.headers):
        return None
    manager = request.app.state.gate.api_attempts
    row = manager.lookup_request(device, model, suffix, raw, request.headers)
    if row is None:
        return None
    route = org.route(row['route'])
    allowed = any(m.route_id == row['route'] and m.model == row['model'] and m.enabled for m in org.models)
    if not allowed or route is None or not route.enabled or route.kind != 'gateway' or route.protocol != protocol:
        raise GateError(403, 'policy_denied', 'доступ к сохранённому API attempt запрещён текущей политикой')
    from .api import check_data_class
    check_data_class(request.app.state.gate, device, request, row['route'])
    return manager.replay(row, device)


def _valid_call(call, legacy=False):
    if not isinstance(call, dict):
        return False
    function = call.get('function') or call
    if not function.get('name') or not legacy and not (call.get('id') or call.get('call_id')):
        return False
    try:
        return isinstance(json.loads(function.get('arguments', '')), dict)
    except (TypeError, ValueError):
        return False


class ResponseFacts:
    """Endpoint-native observations, never execution permission or a retry loop."""
    def __init__(self, suffix, dialect, choice=0):
        self.suffix, self.dialect, self.choice = suffix, dialect, choice
        self.buffer = b''
        self.data: list[bytes] = []
        self.usage = None
        self.start_usage = None
        self.usage_complete = False
        self.terminal = False
        self.outcome = 'unknown'
        self.run_state = 'waiting-recovery'
        self.finish_reason = None
        self.provider_status = None
        self.response_id = None
        self.error = None
        self.calls: dict[Any, dict[str, Any]] = {}
        self.blocks: dict[int, dict[str, Any]] = {}
        self.calls_complete = False
        self.refusal = False
        self.protocol_error = False

    def feed(self, chunk):
        self.buffer += chunk
        while b'\n' in self.buffer:
            line, self.buffer = self.buffer.split(b'\n', 1)
            line = line.rstrip(b'\r')
            if not line:
                self._event()
            elif line.startswith(b'data:'):
                self.data.append(line[5:].lstrip(b' '))

    def _event(self):
        if not self.data:
            return
        payload = b'\n'.join(self.data)
        self.data.clear()
        if payload == b'[DONE]':
            return  # Never a replacement for a finish/stop reason.
        try:
            value = json.loads(payload)
            if not isinstance(value, dict):
                self.protocol_error = True
                return
            self.observe(value, streamed=True)
        except (ValueError, TypeError, AttributeError):
            self.protocol_error = True

    def nonstream(self, raw):
        try:
            doc = json.loads(raw)
            if not isinstance(doc, dict):
                raise ValueError('not an object')
            self.observe(doc, streamed=False)
        except (ValueError, TypeError, AttributeError):
            self.protocol_error = True

    def observe(self, doc, streamed):
        if doc.get('type') == 'error' or doc.get('error') and not (self.suffix == '/responses' and doc.get('status')):
            error = doc.get('error') or {}
            self.error = {'type': error.get('type'), 'code': error.get('code')}
            return
        self.response_id = doc.get('id') or self.response_id
        if self.suffix == '/chat/completions':
            if isinstance(doc.get('usage'), dict):
                self.usage, self.usage_complete = doc['usage'], True
            for choice in doc.get('choices') or []:
                if choice.get('index', 0) != self.choice:
                    continue
                message = choice.get('delta' if streamed else 'message') or {}
                self.refusal |= bool(message.get('refusal'))
                for index, call in enumerate(message.get('tool_calls') or []):
                    if streamed:
                        current = self.calls.setdefault(call.get('index', index), {'id': '', 'name': '', 'arguments': ''})
                        current['id'] += call.get('id') or ''
                        function = call.get('function') or {}
                        current['name'] += function.get('name') or ''
                        current['arguments'] += function.get('arguments') or ''
                    else:
                        self.calls[index] = {**(call.get('function') or {}), 'id': call.get('id')}
                if message.get('function_call'):
                    call = message['function_call']
                    current = self.calls.setdefault('legacy', {'id': 'legacy', 'name': '', 'arguments': ''})
                    for key in ('name', 'arguments'):
                        current[key] += call.get(key) or ''
                reason = choice.get('finish_reason')
                if reason is not None:
                    self.finish_reason, self.terminal = reason, True
                    self.calls_complete = bool(self.calls) and all(_valid_call(c) for c in self.calls.values())
                    self.outcome = ('refused' if self.refusal else 'tool_calls' if reason in ('tool_calls', 'function_call')
                                    else 'incomplete' if reason == 'length' else 'filtered' if reason == 'content_filter'
                                    else 'completed' if reason == 'stop' else 'provider_stop')
                    self.run_state = ('waiting-tools' if self.calls_complete else 'waiting-recovery') if self.outcome == 'tool_calls' else 'response-ready'
        elif self.suffix == '/responses':
            kind = doc.get('type', '')
            response = doc.get('response') if streamed else doc
            if not isinstance(response, dict):
                return
            self.response_id = response.get('id') or self.response_id
            self.provider_status = response.get('status') or self.provider_status
            if isinstance(response.get('usage'), dict):
                self.usage, self.usage_complete = response['usage'], True
            if kind in ('response.completed', 'response.incomplete', 'response.failed') or not streamed:
                status = response.get('status')
                self.terminal = status in ('completed', 'incomplete', 'failed', 'cancelled')
                self.finish_reason = (response.get('incomplete_details') or {}).get('reason')
                output = response.get('output') or []
                self.refusal = any(part.get('type') == 'refusal' for item in output
                                   for part in item.get('content') or [] if isinstance(part, dict))
                calls = [item for item in output if item.get('type') in ('function_call', 'custom_tool_call')]
                self.calls_complete = bool(calls) and all(
                    bool(c.get('call_id')) and isinstance(c.get('input'), str) if c.get('type') == 'custom_tool_call'
                    else _valid_call(c) for c in calls)
                self.outcome = ('refused' if self.refusal else 'tool_calls' if status == 'completed' and calls
                                else status if self.terminal else 'accepted-running')
                self.run_state = (('waiting-tools' if self.calls_complete else 'waiting-recovery') if self.outcome == 'tool_calls'
                                  else 'response-ready' if self.terminal else 'waiting-provider')
                if response.get('error'):
                    error = response['error']
                    self.error = {'type': error.get('type'), 'code': error.get('code')}
        elif self.suffix == '/messages':
            kind = doc.get('type')
            if not streamed:
                self.start_usage = doc.get('usage')
                self.usage, self.usage_complete = self.start_usage, True
                self.finish_reason = doc.get('stop_reason')
                self.blocks = {i: {'block': b, 'complete': True, 'args': None}
                               for i, b in enumerate(doc.get('content') or [])}
                self._message_stop()
            elif kind == 'message_start':
                message = doc.get('message') or {}
                self.response_id = message.get('id') or self.response_id
                self.start_usage = message.get('usage')
                self.usage = self.start_usage
            elif kind == 'content_block_start':
                self.blocks[doc['index']] = {'block': doc.get('content_block') or {}, 'complete': False, 'args': None}
            elif kind == 'content_block_delta':
                delta = doc.get('delta') or {}
                if delta.get('type') == 'input_json_delta':
                    block = self.blocks.get(doc['index'])
                    if block:
                        block['args'] = (block['args'] or '') + (delta.get('partial_json') or '')
            elif kind == 'content_block_stop' and doc.get('index') in self.blocks:
                self.blocks[doc['index']]['complete'] = True
            elif kind == 'message_delta':
                self.finish_reason = (doc.get('delta') or {}).get('stop_reason') or self.finish_reason
                self.usage = {**(self.start_usage or {}), **(doc.get('usage') or {})}
                self.usage_complete = isinstance((doc.get('usage') or {}).get('output_tokens'), int)
            elif kind == 'message_stop':
                self._message_stop()
        elif self.suffix.startswith('/images/') and not streamed:
            self.terminal = isinstance(doc.get('data'), list)
            self.usage = doc.get('usage')
            self.outcome = 'completed' if self.terminal else 'unknown'
            self.run_state = 'response-ready' if self.terminal else 'waiting-recovery'

    def _message_stop(self):
        if self.finish_reason is None:
            return
        self.terminal = True
        self.provider_status = 'message_stop'
        calls = [b for b in self.blocks.values() if b['block'].get('type') == 'tool_use']
        self.calls_complete = bool(calls)
        for block in calls:
            try:
                valid_input = (isinstance(json.loads(block['args']), dict) if block['args'] is not None
                               else isinstance(block['block'].get('input'), dict))
            except (TypeError, ValueError):
                valid_input = False
            self.calls_complete &= bool(block['complete'] and block['block'].get('id')
                                        and block['block'].get('name') and valid_input)
        if self.finish_reason == 'max_tokens':
            self.calls_complete = False
        self.outcome = {'end_turn': 'completed', 'tool_use': 'tool_calls', 'pause_turn': 'paused',
                        'max_tokens': 'incomplete', 'refusal': 'refused',
                        'model_context_window_exceeded': 'incomplete', 'stop_sequence': 'completed'}.get(
                            self.finish_reason, 'provider_stop')
        self.run_state = (('waiting-tools' if self.calls_complete else 'waiting-recovery') if self.outcome == 'tool_calls'
                          else 'awaiting-continuation' if self.outcome == 'paused' else 'response-ready')

    def summary(self, status, headers, normalized_usage=None):
        error = self.error
        category = None
        retry_allowed = False
        if status >= 400:
            category = {401: 'provider_auth', 402: 'provider_billing', 403: 'provider_permission', 408: 'provider_timeout',
                        429: 'provider_rate_or_quota', 529: 'provider_overload', 503: 'provider_overload'}.get(status,
                            'provider_invalid_request' if status in (400, 404, 413, 422) else 'provider_error_unknown')
            # Exact typed failures only. No status-only permission and no retry IO.
            retry_allowed = bool(error and (
                self.dialect == 'anthropic' and status == 529 and error.get('type') == 'overloaded_error'
                or self.dialect == 'deepseek' and status == 503 and
                (error.get('type') in ('server_overloaded', 'overloaded_error') or
                 error.get('code') in ('server_overloaded', 'server_is_overloaded', 'overloaded_error'))))
            self.terminal = status in (400, 401, 402, 403, 404, 409, 413, 422, 429) or retry_allowed
            self.outcome = 'provider_rejected' if self.terminal else 'unknown'
            self.run_state = 'waiting-provider' if retry_allowed else 'response-ready' if self.terminal else 'waiting-recovery'
        elif error:
            category = 'provider_failure' if self.terminal else 'provider_stream_error'
            # A midstream error before native terminal can follow prior effects.
            if not self.terminal:
                self.outcome, self.run_state = 'partial', 'waiting-recovery'
        if self.protocol_error:
            category = category or 'provider_protocol_error'
            self.terminal, self.outcome, self.run_state = False, 'partial', 'waiting-recovery'
        elif self.outcome == 'tool_calls' and not self.calls_complete:
            category = category or 'incomplete_tool_call'
        return {'outcome': self.outcome, 'terminalKnown': self.terminal, 'runState': self.run_state,
                'providerStatus': self.provider_status, 'finishReason': self.finish_reason,
                'providerResponseId': self.response_id,
                'providerRequestId': headers.get('request-id') or headers.get('x-request-id'),
                'selectedChoice': self.choice if self.suffix == '/chat/completions' else None,
                'toolCallsComplete': self.calls_complete, 'hostExecutionAllowed': False,
                'usageStatus': 'known' if self.usage_complete and normalized_usage is not None else
                               'partial' if self.usage is not None else 'unknown',
                'usage': normalized_usage, 'rawUsage': self.usage,
                'error': {**(error or {}), 'category': category} if category or error else None,
                'retry': {'allowed': retry_allowed, 'owner': 'agent_loop',
                          'scope': 'new_attempt_with_policy' if retry_allowed else 'reconcile',
                          'retryAfter': headers.get('retry-after')}}
