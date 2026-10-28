"""API reasoning policy and immutable request/task settings. Never retries provider IO.

Stores only hashes/settings, never full API prompts. Provider-native histories remain opaque.
"""
from __future__ import annotations
import copy
import hashlib
import json
import time
import threading
from ..reasoning import ReasoningSettings, ReasoningPolicy, ReasoningCapabilities, ReasoningError, concrete, policy_request, resolve
from .errors import GateError


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def normalize(value, dialect):
    value = concrete(value)
    if value is not None and dialect == 'deepseek' and value.effort is not None:
        mapped = {'minimal':'low', 'low':'low', 'medium':'high', 'high':'high', 'xhigh':'high', 'max':'max', 'ultra':'max'}
        return value.model_copy(update={'effort':mapped[value.effort]})
    return value


def agrees(requested, configured):
    if requested is None:
        return True
    return all(requested.model_dump()[k] == configured.get(k)
               for k in ('mode','effort','budgetTokens')
               if (k != 'mode' or requested.mode != 'inherit') and requested.model_dump()[k] is not None)


def combine(canonical, native):
    if canonical is None:return native
    if native is None:return canonical
    if canonical.mode == 'provider_default' or native.mode == 'provider_default':
        if canonical != native:raise ReasoningError('reasoning_conflict', 'provider default conflicts with explicit native settings')
        return canonical
    for key in ('effort','budgetTokens'):
        a,b=getattr(canonical,key),getattr(native,key)
        if a is not None and b is not None and a != b:
            raise ReasoningError('reasoning_conflict', 'canonical and native '+key+' disagree')
    if canonical.mode != 'inherit' and native.mode != 'inherit' and canonical.mode != native.mode:
        raise ReasoningError('reasoning_conflict', 'canonical and native thinking modes disagree')
    return ReasoningSettings(mode=canonical.mode if canonical.mode!='inherit' else native.mode,
                             effort=canonical.effort or native.effort,
                             budgetTokens=canonical.budgetTokens or native.budgetTokens)


def native_request(doc, dialect, suffix):
    """Read semantics without modifying history, tools, budgets or opaque native keys."""
    effort, thinking, budget = None, None, None
    if suffix == '/responses':
        effort = (doc.get('reasoning') or {}).get('effort')
    elif suffix == '/chat/completions':
        effort = doc.get('reasoning_effort')
    elif suffix == '/messages':
        effort = (doc.get('output_config') or {}).get('effort')
    if suffix == '/messages' or dialect == 'deepseek' and suffix == '/chat/completions':
        spec = doc.get('thinking') or {}
        thinking = spec.get('type')
        budget = spec.get('budget_tokens')
    if effort == 'none' or thinking == 'disabled':
        if budget is not None or effort == 'none' and thinking in ('enabled','adaptive'):
            raise ReasoningError('reasoning_conflict', 'disabled thinking conflicts with effort/budget')
        return ReasoningSettings(mode='disabled', effort=None if effort=='none' else effort)
    if effort is None and thinking is None and budget is None:
        return None
    if thinking not in (None, 'enabled', 'adaptive'):
        raise ReasoningError('reasoning_unsupported', 'thinking mode has no supported mapping in reasoning v1')
    if dialect == 'deepseek' and thinking == 'adaptive':
        raise ReasoningError('reasoning_unsupported', 'DeepSeek API does not declare adaptive as an accepted thinking type')
    return ReasoningSettings(mode='enabled' if thinking is not None or budget is not None else 'inherit', effort=effort, budgetTokens=budget)


def wire(doc, configured, dialect, suffix, caps):
    out = copy.deepcopy(doc)
    out.pop('atlas_reasoning', None)
    mode, effort = configured['mode'], configured.get('effort')
    budget = configured.get('budgetTokens')
    if mode == 'disabled' and effort is not None and dialect != 'anthropic':
        raise ReasoningError('reasoning_unsupported', 'this API cannot combine disabled thinking with a separate effort')
    if budget is not None and (dialect != 'anthropic' or suffix != '/messages'):
        raise ReasoningError('reasoning_unsupported', 'manual thinking budget has no mapping for this API channel')
    if suffix not in ('/chat/completions', '/responses', '/messages'):
        raise ReasoningError('reasoning_unsupported', 'endpoint does not support reasoning settings')
    if mode == 'provider_default':
        effort = caps.get('defaultEffort')
    elif mode == 'enabled' and effort is None and budget is None:
        effort = caps.get('defaultEffort')
        if effort is None and (dialect == 'openai' or suffix == '/responses'):
            raise ReasoningError('reasoning_effort_required', 'model default is unknown; choose an explicit effort')
    if suffix == '/responses':
        native = dict(out.get('reasoning') or {})
        if mode == 'disabled':native['effort'] = 'none'
        elif effort is not None:native['effort'] = effort
        else:native.pop('effort', None)
        if native:out['reasoning'] = native
        else:out.pop('reasoning', None)
    elif suffix == '/chat/completions':
        if mode == 'disabled' and dialect != 'deepseek':out['reasoning_effort'] = 'none'
        elif effort is not None:out['reasoning_effort'] = effort
        else:out.pop('reasoning_effort', None)
        if dialect == 'deepseek':
            if mode in ('enabled','disabled'):out['thinking'] = {**(out.get('thinking') or {}), 'type':mode}
            else:out.pop('thinking', None)
    else:
        output = dict(out.get('output_config') or {})
        if effort is not None:output['effort'] = effort
        else:output.pop('effort', None)
        if output:out['output_config'] = output
        else:out.pop('output_config', None)
        thinking = dict(out.get('thinking') or {})
        thinking.pop('type', None)
        thinking.pop('budget_tokens', None)
        if mode == 'disabled':out['thinking'] = {**thinking,'type':'disabled'}
        elif budget is not None:
            if budget < 1024 or isinstance(out.get('max_tokens'), int) and budget >= out['max_tokens']:
                raise ReasoningError('invalid_reasoning_budget', 'manual budget must be >=1024 and below max_tokens')
            out['thinking'] = {**thinking,'type':'enabled', 'budget_tokens':budget}
        elif mode == 'enabled':
            if dialect != 'deepseek' and not caps.get('adaptive'):
                raise ReasoningError('reasoning_unsupported', 'adaptive thinking is not confirmed for this model')
            out['thinking'] = {**thinking,'type':'enabled' if dialect == 'deepseek' else 'adaptive'}
        elif mode == 'provider_default':out.pop('thinking', None)
    return out


class ApiReasoning:
    def __init__(self, store):
        self.store = store
        # Store methods acquire their own non-reentrant lock. Serialize the
        # decision here without holding that lock across nested Store calls.
        self._lock = threading.Lock()
        store._exec('''CREATE TABLE IF NOT EXISTS api_reasoning_snapshots (
            key TEXT PRIMARY KEY, kind TEXT NOT NULL, fingerprint TEXT NOT NULL,
            snapshot TEXT NOT NULL, created REAL NOT NULL)''')

    def apply(self, raw, doc, entry, suffix, device, headers):
        """A supplied request key pins one body. An optional scope pins one API tool loop.

        Missing identity means an independent API request, not a claim of retry deduplication.
        Ordinary legacy/native requests with no opted-in policy/capabilities stay byte-exact.
        """
        try:
            with self._lock:
                return self._apply(raw, doc, entry, suffix, device, headers)
        except (ValueError, TypeError, AttributeError) as failed:
            raise GateError(403 if getattr(failed,'code','') == 'reasoning_policy_denied' else 422,
                            getattr(failed,'code','invalid_reasoning'), str(failed)) from failed

    def _apply(self, raw, doc, entry, suffix, device, headers):
        identity = headers.get('idempotency-key') or headers.get('x-harness-model-call')
        scope = headers.get('x-atlas-reasoning-scope')
        if any(v is not None and (not v or len(v)>200) for v in (identity,scope)):
            raise ReasoningError('invalid_reasoning_identity', 'reasoning identity must contain 1..200 characters')
        model = entry.model if entry else str(doc.get('model'))
        route = entry.route_id if entry else ''
        namespace = [device.org, device.device_id]
        rid = digest(namespace + ['request',identity]) if identity else None
        sid = digest(namespace + ['scope',headers.get('x-harness-session'),headers.get('x-harness-agent'),route,model,scope]) if scope else None
        fingerprint = digest([route,model,suffix,doc])
        stored = self.store._one('SELECT * FROM api_reasoning_snapshots WHERE key=?',(rid,)) if rid else None
        if stored and stored['fingerprint'] != fingerprint:
            raise GateError(409, 'idempotency_conflict', 'API request key was reused with different parameters')
        pinned = self.store._one('SELECT * FROM api_reasoning_snapshots WHERE key=?',(sid,)) if sid else None
        saved = json.loads((stored or pinned)['snapshot']) if stored or pinned else None
        if saved:
            dialect, caps, policy = saved['dialect'], saved['caps'], saved['policy']
        else:
            dialect = getattr(entry,'reasoningDialect',None)
            caps_value = getattr(entry,'reasoningCapabilities',None)
            caps = caps_value.model_dump() if caps_value is not None else None
            policy = getattr(entry,'reasoningPolicy',None)
            policy = policy.model_dump() if policy is not None else None
        opted = saved is not None or 'atlas_reasoning' in doc or policy is not None or caps is not None
        if not opted:
            return raw, None
        if not saved and policy is None and caps is None and concrete(doc.get('atlas_reasoning')) is None:
            output = dict(doc); output.pop('atlas_reasoning', None)
            return raw if output == doc else json.dumps(output,ensure_ascii=False).encode(), None
        if dialect is None:
            raise ReasoningError('reasoning_unsupported', 'exact API route/model dialect is not configured')
        if (dialect=='openai' and suffix=='/messages') or (dialect=='anthropic' and suffix!='/messages'):
            raise ReasoningError('reasoning_unsupported', 'reasoning dialect does not match endpoint')
        native = native_request(doc,dialect,suffix)
        canonical = concrete(doc.get('atlas_reasoning')) if 'atlas_reasoning' in doc else None
        requested = canonical or native
        normalized = combine(normalize(canonical,dialect), normalize(native,dialect))
        normalized_policy = ReasoningPolicy.model_validate(policy or {})
        if normalized_policy.default is not None:
            normalized_policy = normalized_policy.model_copy(update={'default':normalize(normalized_policy.default,dialect)})
        if saved:
            decision = saved['decision']
            if normalized is not None and not agrees(normalized,(decision or {}).get('configured') or {}):
                raise GateError(409, 'reasoning_scope_conflict', 'reasoning cannot change inside an accepted API scope')
        else:
            selected, source = policy_request(normalized,normalized_policy)
            decision = resolve(selected,caps,source=source)
            if decision is not None:
                decision['requested'] = requested.model_dump() if requested else None
                decision['nativeRequested'] = native.model_dump() if native else None
                decision['normalization'] = 'deepseek_vendor_mapping' if requested != normalized else None
        if decision is None:
            # A known scope with provider inheritance is still pinned against later policy reload.
            output = dict(doc); output.pop('atlas_reasoning',None)
        else:
            output = wire(doc,decision['configured'],dialect,suffix,caps or {})
        snapshot = {'version':1,'dialect':dialect,'caps':caps,'policy':policy,'decision':decision}
        encoded = json.dumps(snapshot,sort_keys=True)
        if sid and pinned is None:
            self.store._exec('INSERT INTO api_reasoning_snapshots VALUES (?,?,?,?,?)',(sid,'scope','',encoded,time.time()))
        if rid and stored is None:
            self.store._exec('INSERT INTO api_reasoning_snapshots VALUES (?,?,?,?,?)',(rid,'request',fingerprint,encoded,time.time()))
        unchanged = output == doc
        return raw if unchanged else json.dumps(output,ensure_ascii=False).encode(), decision
