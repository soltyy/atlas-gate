"""Reasoning wire v1. No provider IO; configured is never provider-confirmed.

Kept byte-identical in standalone Gate: neither service imports the other.
"""
from __future__ import annotations

import hashlib
import json
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

Effort = Literal['minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra']


class ReasoningSettings(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    version: Literal[1] = 1
    mode: Literal['inherit', 'provider_default', 'enabled', 'disabled'] = 'inherit'
    effort: Effort | None = None
    budgetTokens: StrictInt | None = Field(default=None, gt=0)

    @model_validator(mode='after')
    def coherent(self):
        if self.mode == 'inherit' and self.budgetTokens is not None:
            object.__setattr__(self, 'mode', 'enabled')
        if self.mode == 'provider_default' and (self.effort is not None or self.budgetTokens is not None):
            raise ValueError('reasoning_conflict: mode cannot carry effort/budget')
        if self.mode == 'disabled' and self.budgetTokens is not None:
            raise ValueError('reasoning_conflict: disabled thinking cannot carry a token budget')
        return self


class ReasoningCapabilities(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    version: Literal[1] = 1
    status: Literal['supported', 'unsupported', 'unknown'] = 'unknown'
    efforts: list[Effort] = Field(default_factory=list)
    defaultEffort: str | None = None
    disabled: StrictBool = False
    disabledEfforts: list[Effort] = Field(default_factory=list)
    budgetTokens: StrictBool = False
    adaptive: StrictBool = False
    source: str = 'unknown'
    resolvedModel: str | None = None

    @property
    def revision(self):
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()


class ReasoningPolicy(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    default: ReasoningSettings | None = None
    allowOverride: StrictBool = True
    allowedEfforts: list[Effort] | None = None


class ReasoningError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def concrete(value):
    if value is None:
        return None
    value = ReasoningSettings.model_validate(value)
    return None if value.mode == 'inherit' and value.effort is None and value.budgetTokens is None else value


def policy_request(request, policy):
    """Hard policy is not a default and cannot be bypassed with provider_default."""
    requested = concrete(request)
    policy = ReasoningPolicy.model_validate(policy or {})
    default = concrete(policy.default)
    if not policy.allowOverride and requested is not None and requested != default:
        raise ReasoningError('reasoning_policy_denied', 'explicit reasoning conflicts with locked policy')
    selected = requested or default
    if policy.allowedEfforts is not None:
        # An unknown inherited/provider default cannot prove compliance with a hard allowlist.
        if selected is None or selected.effort not in policy.allowedEfforts:
            raise ReasoningError('reasoning_policy_denied', 'reasoning effort is outside policy allowlist')
    return selected, 'request' if requested is not None else 'gate_default' if default is not None else 'inherit'


def resolve(request, capabilities, *, default=None, source='request'):
    """Resolve once, before opening SDK. Persist this decision, never re-resolve on replay."""
    requested = concrete(request)
    chosen = requested or concrete(default)
    if chosen is None:
        return None  # legacy omission: preserve native configuration and old wire bytes
    caps = ReasoningCapabilities.model_validate(capabilities or {})
    if caps.status != 'supported':
        raise ReasoningError('reasoning_unsupported', 'model/channel reasoning capabilities are not confirmed')
    if chosen.effort is not None and chosen.effort not in caps.efforts:
        raise ReasoningError('reasoning_unsupported', 'effort is not supported by this model/channel')
    if chosen.mode == 'disabled' and not caps.disabled:
        raise ReasoningError('reasoning_unsupported', 'thinking cannot be disabled on this model/channel')
    if chosen.mode == 'disabled' and chosen.effort is not None and chosen.effort not in caps.disabledEfforts:
        raise ReasoningError('reasoning_unsupported', 'this effort/disabled combination is not confirmed')
    if chosen.budgetTokens is not None and not caps.budgetTokens:
        raise ReasoningError('reasoning_unsupported', 'manual thinking budget is not supported')
    if chosen.effort is None and caps.defaultEffort is not None and caps.defaultEffort not in caps.efforts and not (caps.defaultEffort == 'none' and caps.disabled):
        raise ReasoningError('reasoning_unsupported', 'declared model default does not match capabilities')
    if chosen.mode == 'enabled' and chosen.effort is None and chosen.budgetTokens is None and caps.defaultEffort == 'none':
        raise ReasoningError('reasoning_effort_required', 'choose an enabled effort; the native default disables reasoning')
    return {'version': 1, 'requested': requested.model_dump() if requested else None,
            'configured': chosen.model_dump(), 'source': source if requested else 'router_default',
            'capabilityRevision': caps.revision, 'resolvedModel': caps.resolvedModel,
            'providerConfirmed': False}


def sdk_capabilities(raw, backend):
    """Only SDK-declared facts; missing metadata is unknown, not positive support."""
    raw = raw or {}
    resolved = raw.get('resolvedModel') or raw.get('resolved') or raw.get('model') or raw.get('value')
    resolved = resolved if isinstance(resolved, str) else None
    if backend == 'codex':
        values = raw.get('supportedReasoningEfforts')
        if not isinstance(values, list):
            values = []
        levels = [x.get('reasoningEffort') if isinstance(x, dict) else x for x in values or []]
        return ReasoningCapabilities(status='supported' if any(x in Effort.__args__ or x == 'none' for x in levels) else 'unknown',
            efforts=[x for x in levels if x in Effort.__args__], defaultEffort=raw.get('defaultReasoningEffort') if isinstance(raw.get('defaultReasoningEffort'), str) else None,
            disabled='none' in levels, source='codex_model_list', resolvedModel=resolved).model_dump()
    if backend == 'claude':
        levels = raw.get('supportedEffortLevels') or []
        if not isinstance(levels, list):
            levels = []
        status = 'supported' if raw.get('supportsEffort') is True and any(x in Effort.__args__ for x in levels) else 'unsupported' if raw.get('supportsEffort') is False else 'unknown'
        return ReasoningCapabilities(status=status, efforts=[x for x in levels if x in Effort.__args__],
            adaptive=raw.get('supportsAdaptiveThinking') is True,
            disabled=raw.get('supportsThinkingDisabled') is True,
            budgetTokens=raw.get('supportsThinkingBudget') is True,
            defaultEffort=raw.get('defaultEffort') if isinstance(raw.get('defaultEffort'), str) else None,
            source='claude_initialize', resolvedModel=resolved).model_dump()
    return ReasoningCapabilities().model_dump()


def codex_effort(decision, capabilities):
    if decision is None:
        return None
    config = decision['configured']
    if config['mode'] == 'disabled':
        return 'none'
    effort = config.get('effort') or (capabilities or {}).get('defaultEffort')
    if effort is None or (config['mode'] == 'enabled' and effort == 'none') or (effort not in (capabilities or {}).get('efforts', []) and not (effort == 'none' and (capabilities or {}).get('disabled'))):
        raise ReasoningError('reasoning_unsupported', 'SDK did not declare a model default to pin')
    return effort
