"""Публичный каталог и машинный контракт настроек из того же подписанного профиля."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .. import __version__
from .crypto import canonical_json, sha256_hex, verify_profile
from .orgs import Agent, Audit, BuiltinTools, Mcp, ModelEntry, Overrides, Policy, Pricing, Quota
from .attachments import route_attachments


class PublicModel(BaseModel):
    catalog_id: str
    model: str
    display_name: str
    kind: Literal['gateway', 'router-agent', 'local']
    protocol: Literal['openai', 'anthropic', 'atlas-agent']
    context_window: int | None
    max_output: int | None
    enabled: bool
    limits_source: Literal['configured'] = 'configured'
    attachments: dict[str, Any] | None = None


class Connection(BaseModel):
    device_auth: Literal['bearer'] = 'bearer'
    settings_scope: Literal['organization'] = 'organization'
    profile_signature: Literal['EdDSA'] = 'EdDSA'


class HarnessLimits(BaseModel):
    agent_sessions_per_device: int
    anthropic_version: str
    agent_context_source: Literal['session-sdk'] = 'session-sdk'


class Discovery(BaseModel):
    schema_version: Literal[1] = 1
    service: Literal['atlas-gate'] = 'atlas-gate'
    service_version: str = __version__
    endpoints: dict[str, str]
    authentication: Connection = Field(default_factory=Connection)
    limits: HarnessLimits
    models: list[PublicModel]


class ProfileRoute(BaseModel):
    model_config = ConfigDict(extra='allow')
    id: str
    kind: Literal['gateway', 'router-agent', 'local']
    protocol: Literal['openai', 'anthropic', 'atlas-agent']
    base_url: str
    region: str


class ProfileOrg(BaseModel):
    model_config = ConfigDict(extra='allow')
    id: str
    name: str


class ClientProfile(BaseModel):
    model_config = ConfigDict(extra='allow')
    profile_version: int
    issued_at: int
    org: ProfileOrg
    ttl_seconds: int
    min_app_version: str
    recommended_app_version: str
    update_channel_url: str
    routes: list[ProfileRoute]
    models: list[ModelEntry]
    pricing: list[Pricing]
    policy: Policy
    agents: list[Agent]
    mcp: list[Mcp]
    builtin_tools: BuiltinTools
    overrides: Overrides
    feature_flags: dict[str, Any]
    quota: Quota
    audit: Audit


class HarnessSettings(BaseModel):
    schema_version: Literal[1] = 1
    service_version: str = __version__
    endpoints: dict[str, str]
    limits: HarnessLimits
    profile: ClientProfile
    profile_jws: str
    profile_signing_key: dict[str, str]
    attachment_routes: dict[str, dict[str, Any]] = Field(default_factory=dict)


def endpoints():
    # Относительно Gate base URL: reverse proxy/loopback не меняют контракт.
    return {'discovery': '/harness/discovery', 'settings': '/harness/settings',
            'health': '/harness/health',
            'profile': '/harness/profile', 'enroll_start': '/harness/enroll/start',
            'enroll_poll': '/harness/enroll/poll', 'token_refresh': '/harness/token/refresh',
            'quota': '/harness/quota', 'llm_models': '/harness/llm/v1/models',
            'chat_completions': '/harness/llm/v1/chat/completions',
            'messages': '/harness/llm/v1/messages', 'agent_sessions': '/harness/agent/sessions',
            'agent_context': '/harness/agent/sessions/{session_id}/context',
            'agent_attachments': '/harness/agent/sessions/{session_id}/attachments',
            'agent_documents': '/harness/agent/sessions/{session_id}/documents',
            'agent_document_page': '/harness/agent/sessions/{session_id}/documents/{document_id}'}


def limits(state):
    return HarnessLimits(agent_sessions_per_device=state.settings.ATLAS_GATE_AGENT_SESSIONS_PER_DEVICE,
                         anthropic_version=state.settings.ATLAS_GATE_ANTHROPIC_VERSION)


def public_catalog(state):
    variants = {}
    for org in state.orgs.values():
        if org.archived:
            continue
        for model in org.models:
            route = org.route(model.route_id)
            if route is None:
                continue
            # Явная проекция: нет org/node/route IDs, адресов апстримов и key_env.
            fields = dict(model=model.model, display_name=model.display_name or model.model,
                          kind=route.kind, protocol=route.protocol,
                          context_window=model.context_window if model.context_window > 0 else None,
                          max_output=model.max_output if model.max_output > 0 else None)
            fields['attachments'] = route_attachments(state, org.id, route.id, model.model) if route.kind=='router-agent' else None
            identity = sha256_hex(canonical_json(fields))
            enabled = model.enabled and route.enabled
            if identity in variants:
                variants[identity].enabled |= enabled
            else:
                variants[identity] = PublicModel(catalog_id=identity, enabled=enabled, **fields)
    return Discovery(endpoints=endpoints(), limits=limits(state),
                     models=sorted(variants.values(), key=lambda m: (m.model, m.catalog_id)))


def client_settings(state, row):
    # Читаем ровно persisted JWS: reload не может разорвать пару JSON/подпись.
    key = state.key.jwk()
    profile = verify_profile(row['jws'], key)
    attachment_routes = {}
    for route in profile['routes']:
        if route['kind']=='router-agent':
            attachment_routes[route['id']] = {m['model']: route_attachments(state, profile['org']['id'], route['id'], m['model'])
                for m in profile['models'] if m['route_id']==route['id']}
    return HarnessSettings(endpoints=endpoints(), limits=limits(state), profile=profile,
                           profile_jws=row['jws'], profile_signing_key=key, attachment_routes=attachment_routes)


def json_snapshot(request, doc, private=False):
    from fastapi.responses import Response
    data = canonical_json(doc.model_dump(mode='json'))
    etag = '"' + sha256_hex(data) + '"'
    headers = {'ETag': etag, 'Cache-Control': 'private, no-cache' if private else 'public, no-cache'}
    if private:
        headers['Vary'] = 'Authorization, X-API-Key'
        headers['X-Harness-Profile-Version'] = str(doc.profile.profile_version)
    wanted = [tag.strip().removeprefix('W/') for tag in request.headers.get('if-none-match', '').split(',')]
    if etag in wanted or '*' in wanted:
        return Response(status_code=304, headers=headers)
    return Response(content=data, media_type='application/json', headers=headers)
