"""Gate owns organisation policy; SDK facts and application belong to the pinned Router."""
from __future__ import annotations
import json
from ..reasoning import ReasoningError, ReasoningPolicy, policy_request, resolve, concrete
from .errors import GateError


def model_policy(org, route, model):
    entry = next((m for m in org.models if m.route_id == route and m.model == model), None)
    return entry.reasoningPolicy if entry else None


def select_request(request, policy):
    try:
        selected, source = policy_request(request, policy)
        return {'requested': request.model_dump() if hasattr(request, 'model_dump') else request,
                'selected': selected.model_dump() if selected else None, 'source': source,
                'policy': ReasoningPolicy.model_validate(policy or {}).model_dump()}
    except ValueError as failed:
        raise GateError(403, getattr(failed, 'code', 'invalid_reasoning_policy'), str(failed)) from failed


def validate_node(info, model, snapshot):
    selected = snapshot['selected']
    if selected is None:
        return
    if (info.get('reasoningSettings') or {}).get('version') != 1:
        raise ReasoningError('reasoning_protocol_unsupported', 'Router did not advertise reasoning v1')
    metadata = next((m for m in info.get('models', []) if m.get('value') == model), {})
    resolve(selected, metadata.get('reasoning'))


def confirm(snapshot, response):
    if not snapshot or snapshot['selected'] is None:
        return
    decision = response.get('configuredReasoning')
    if not isinstance(decision, dict) or decision.get('version') != 1 or decision.get('configured') != snapshot['selected']:
        raise ValueError('Router did not acknowledge the exact reasoning configuration')
    response['configuredReasoning'] = dict(decision, requested=snapshot['requested'], source=snapshot['source'])


def validate_new_prompt(state, binding, device):
    """Called only for a NEW operation; existing receipts retain their original policy/body/key."""
    from .api import org_of
    policy = model_policy(org_of(state, device), binding['route'], binding['model'])
    saved = json.loads(binding.get('reasoning_snapshot') or '{}')
    if not saved and policy is None:
        return
    current = select_request(saved.get('requested'), policy)
    if current['selected'] != saved.get('selected'):
        raise GateError(409, 'reasoning_rebind_required',
                        'Reasoning policy changed; reopen the transport at a safe boundary with the same SDK history',
                        action='rebind_after_terminal')


def project_capabilities(state, org, route, model):
    """Union for NEW-session selection; the bound endpoint is authoritative for resume."""
    entries, total = [], 0
    for node_id, info in state.nodes.snapshots.items():
        node = state.nodes.config.get(node_id)
        if not node or not node.enabled or org not in node.orgs or route not in node.routes or state.nodes.revoked(node_id):
            continue
        metadata = next((m for m in info.get('models', []) if m.get('value') == model), None)
        if metadata is None:
            continue
        total += 1
        if (info.get('reasoningSettings') or {}).get('version') == 1 and (metadata.get('reasoning') or {}).get('status') == 'supported':
            entries.append(metadata['reasoning'])
    from ..reasoning import Effort
    defaults = {e.get('defaultEffort') for e in entries}
    return {'version':1, 'status':'supported' if entries else 'unknown',
            'efforts':[v for v in Effort.__args__ if any(v in e.get('efforts',[]) for e in entries)],
            'defaultEffort':next(iter(defaults)) if len(defaults)==1 else None,
            'disabled':any(e.get('disabled') for e in entries), 'budgetTokens':any(e.get('budgetTokens') for e in entries),
            'source':'router_sdk', 'scope':'session', 'eligibleNodes':len(entries), 'knownNodes':total,
            'coverage':'all' if total and len(entries)==total else 'partial' if entries else 'unknown',
            'defaultsVaryByNode':len(defaults)>1, 'providerConfirmed':False}
