"""Формы запросов и ответов `/harness/*` (имена с приставкой Gate — схемы роутера не затеняются)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ..schemas import AgentSpec, ToolDefinition


class GateErrorBody(BaseModel):
    code: str
    message: str


class GateErrorResponse(BaseModel):
    error: GateErrorBody


class GateEnrollStart(BaseModel):
    device_name: str = Field(min_length=1, max_length=200)
    app_version: str = Field(min_length=1, max_length=50)
    platform: str = Field(min_length=1, max_length=50)


class GateEnrollStarted(BaseModel):
    device_code: str
    user_code: str
    verification_url: str
    interval: int
    expires_in: int


class GateEnrollPoll(BaseModel):
    device_code: str


class GateUser(BaseModel):
    id: str = Field(min_length=1)
    email: str = ""
    name: str = ""


class GateOrg(BaseModel):
    id: str
    name: str


class GateEnrollResult(BaseModel):
    """Ответ poll (errata B.8): без gateway_key/gateway_url — ключи провайдеров на устройство не уходят."""

    device_id: str
    device_token: str
    expires_in: int
    refresh_token: str
    device_secret: str
    profile_signing_key: dict[str, str]
    user: GateUser
    org: GateOrg


class GateEnrollApprove(BaseModel):
    user_code: str
    user: GateUser
    org: str


class GateEnrollDeny(BaseModel):
    user_code: str


class GateRefresh(BaseModel):
    refresh_token: str


class GateTokens(BaseModel):
    device_token: str
    expires_in: int
    refresh_token: str


class GateOk(BaseModel):
    ok: bool = True


class GateRevoked(BaseModel):
    ok: bool = True
    device_id: str
    agent_sessions_closed: int


class GateQuota(BaseModel):
    period: Literal["month"] = "month"
    limit: float | None
    used: float
    remaining: float | None
    currency: str
    reset_at: str | None
    agent_turns: dict[str, Any] = Field(default_factory=dict)


class GateAuditEvent(BaseModel):
    seq: int = Field(ge=0)
    type: str
    ts: float | int | str
    payload: dict[str, Any] = Field(default_factory=dict)


class GateAuditBatch(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    events: list[GateAuditEvent]
    hmac: str


class GateAuditAccepted(BaseModel):
    accepted_up_to: int


class GateManifest(BaseModel):
    version: str
    sha256: str
    url: str
    notes_url: str = ""


class GateHealth(BaseModel):
    ok: bool
    version: str
    profile_version: int
    upstreams: dict[str, Literal["ok", "down"]] = Field(default_factory=dict)
    agent_backend: Literal["ok", "down"]


class GateReloaded(BaseModel):
    orgs: dict[str, int]
    errors: dict[str, str]
    warnings: dict[str, list[str]]
    upstreams: dict[str, str] = Field(default_factory=dict)


class GateAgentSessionCreate(BaseModel):
    """Рукопожатие агентной сессии с устройства: только эти поля уходят в `/v1/sessions` роутера.

    `foreignTools`/`foreignServers` не принимаются: серверы MCP человека на машине роутера
    устройству недоступны.
    """

    system: str | None = None
    tools: list[ToolDefinition] = Field(default_factory=list)
    model: str | None = None
    resumeSessionId: str | None = None
    agents: list[AgentSpec] = Field(default_factory=list)
