"""Конфигурация организации `<org>.json` — источник управляемого профиля (GW-HARNESS-02 §E).

Валидация pydantic при старте и при `POST /harness/admin/reload`: битая конфигурация не
подписывается и не уходит на устройства — причина словами (`profile_invalid`).
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

log = logging.getLogger("atlas_gate")

PACKAGE_ORGS = Path(__file__).parent / "orgs"
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: str) -> str:
    """${VAR} и ${VAR:-по умолчанию} из окружения процесса (адреса апстримов тестового контура)."""
    return _ENV_REF.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value)
TEST_ORG = "test-org"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Route(_Strict):
    id: str
    kind: Literal["gateway", "router-agent", "local"]
    protocol: Literal["openai", "anthropic", "atlas-agent"]
    # Внутренний адрес апстрима провайдера (GW-DIRECT-01): база, к которой дописывается /chat/completions,
    # /messages или /models, например https://api.deepseek.com/v1. Клиенту в профиль не уходит.
    # Допускается ${VAR} / ${VAR:-по умолчанию} из окружения — для тестового контура.
    upstream_url: str = ""
    # Имя переменной с ключом провайдера в файле ключей гейта (ATLAS_GATE_KEYS_FILE) или окружении.
    key_env: str = ""
    timeout_s: float = 120.0
    # Выключенный маршрут остаётся в файле (редактор админки, #6), но в профиль и прокси не идёт.
    enabled: bool = True
    # Необязательные метаданные: на политику и валидацию не влияют (решение 28.09.2026).
    region: str = ""
    zdr: bool = False
    # Пусто — потолка нет (классы данных — метка сессии, не ограничение маршрута).
    max_data_class: str = ""

    def upstream(self) -> str:
        return expand_env(self.upstream_url).rstrip("/")


class ModelEntry(_Strict):
    route_id: str
    model: str
    display_name: str = ""
    context_window: int = 0
    max_output: int = 0
    enabled: bool = True


class Pricing(_Strict):
    route_id: str
    model: str
    currency: str = "USD"
    input_uncached: float = 0.0
    input_cached: float = 0.0
    output: float = 0.0
    pricing_version: str = "1"
    subscription: bool = False


class ToolPolicy(_Strict):
    tool: str
    mode: Literal["auto", "ask", "deny"]
    max_data_class: str
    escalatable: bool = False


class Policy(_Strict):
    data_classes: list[str] = Field(default_factory=lambda: ["public", "internal", "personal", "client-confidential"])
    routes_by_class: dict[str, list[str]] = Field(default_factory=dict)
    tool_policies: list[ToolPolicy] = Field(default_factory=list)
    default_data_class: str = "internal"


class Agent(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    version: str
    name: str = Field(default="", validate_default=True)
    system_prompt: str = ""
    model: str = ""
    allowed_routes: list[str] = Field(default_factory=list)
    # "*" — все инструменты, включая добавленные позже (#12); рядом редактор пишет и явный список
    # известных на момент сохранения — клиент, ещё не знающий "*", получит их поимённо.
    tools_allowlist: list[str] = Field(default_factory=list)
    mcp_ids: list[str] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=dict)
    # Агент по умолчанию для устройства; в редакторе админки — ровно один (#6).
    default: bool = False

    @field_validator("name")
    @classmethod
    def nonempty_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("имя агента не должно быть пустым")
        return value


class Mcp(_Strict):
    id: str
    name: str = ""
    transport: Literal["http", "stdio"] = "http"
    url: str | None = None
    command: str | None = None
    args: list[str] | None = None
    cwd: str | None = None
    env_ref: list[str] = Field(default_factory=list)
    lifecycle: Literal["run", "session"] = "run"
    headers_ref: str | None = None
    required: bool = False
    tools_allowlist: list[str] = Field(default_factory=list)
    ref_fields: dict[str, list[str]] = Field(default_factory=dict)
    display_fields: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_transport(self) -> Mcp:
        if self.transport == "http":
            if not self.url or not self.url.strip():
                raise ValueError("HTTP MCP требует url")
        else:
            if not self.command or not self.command.strip() or self.args is None:
                raise ValueError("stdio MCP требует command и args (допустим пустой список)")
            if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in self.env_ref):
                raise ValueError("env_ref содержит только имена переменных окружения")
        return self


class BuiltinTools(_Strict):
    read: Literal["allow", "deny"] = "allow"
    write: Literal["allow", "deny"] = "deny"
    edit: Literal["allow", "deny"] = "deny"
    shell: Literal["allow", "deny"] = "deny"


class Overrides(_Strict):
    allow_personal_agents: bool = False
    allow_local_mcp: bool = False
    allow_local_routes: bool = False
    allow_data_class_raise: bool = False
    allow_workspace_root: bool = True


class Quota(_Strict):
    limit: float | None = None
    currency: str = "USD"
    agent_turns_per_day: int | None = None


class Audit(_Strict):
    include_messages: bool = False


class UpdateManifest(_Strict):
    version: str
    sha256: str
    url: str
    notes_url: str = ""


from ..sdk_tools import SdkTools


class OrgConfig(_Strict):
    id: str
    name: str
    routes: list[Route]
    models: list[ModelEntry] = Field(default_factory=list)
    pricing: list[Pricing] = Field(default_factory=list)
    policy: Policy = Field(default_factory=Policy)
    agents: list[Agent] = Field(default_factory=list)
    mcp: list[Mcp] = Field(default_factory=list)
    builtin_tools: BuiltinTools = Field(default_factory=BuiltinTools)
    sdk_tools: SdkTools = Field(default_factory=lambda: SdkTools(webSearch='live', files=True))
    overrides: Overrides = Field(default_factory=Overrides)
    feature_flags: dict[str, Any] = Field(default_factory=dict)
    min_app_version: str = "0.0.0"
    recommended_app_version: str = ""
    update_channel_url: str = ""
    update_manifest: UpdateManifest | None = None
    quota: Quota = Field(default_factory=Quota)
    audit: Audit = Field(default_factory=Audit)
    # Архивная организация (#7): файл и история остаются, устройствам — 403 org_archived на профиль,
    # /harness/llm и /harness/agent; при одобрении регистраций не предлагается. Удаления нет.
    archived: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> "OrgConfig":
        classes = self.policy.data_classes
        ids = [r.id for r in self.routes]
        if len(set(ids)) != len(ids):
            raise ValueError("повтор id маршрута")
        for r in self.routes:
            # ЛОКАЛЬНЫЙ МАРШРУТ — ТОЛЬКО АВТОНОМНЫЙ РЕЖИМ HARNESS (§B.7): корпоративный профиль его
            # не несёт, иначе данные ушли бы мимо гейта, политики и учёта.
            if r.kind == "local":
                raise ValueError(f"маршрут {r.id}: kind=local недопустим в корпоративном профиле")
            if (r.kind == "router-agent") != (r.protocol == "atlas-agent"):
                raise ValueError(f"маршрут {r.id}: router-agent ⇔ protocol atlas-agent")
            if r.max_data_class and r.max_data_class not in classes:
                raise ValueError(f"маршрут {r.id}: класс {r.max_data_class!r} не в policy.data_classes")
            if r.kind == "gateway" and (not r.upstream_url or not r.key_env):
                raise ValueError(f"маршрут {r.id}: у gateway-маршрута обязательны upstream_url и key_env")
        seen_agents: set[tuple[str, str]] = set()
        for a in self.agents:
            if (a.id, a.version) in seen_agents:
                raise ValueError(f"агент {a.id} версии {a.version}: повтор")
            seen_agents.add((a.id, a.version))
        if sum(1 for a in self.agents if a.default) > 1:
            raise ValueError("агентов с default=true больше одного")
        for m in self.models:
            if m.route_id not in ids:
                raise ValueError(f"модель {m.model}: нет маршрута {m.route_id}")
        for p in self.pricing:
            if p.route_id not in ids:
                raise ValueError(f"цена {p.model}: нет маршрута {p.route_id}")
        for cls, routes in self.policy.routes_by_class.items():
            if cls not in classes:
                raise ValueError(f"routes_by_class: класс {cls!r} не в policy.data_classes")
            for rid in routes:
                if rid not in ids:
                    raise ValueError(f"routes_by_class[{cls}]: нет маршрута {rid}")
        if self.policy.default_data_class not in classes:
            raise ValueError("policy.default_data_class не в policy.data_classes")
        for tp in self.policy.tool_policies:
            if tp.max_data_class not in classes:
                raise ValueError(f"tool_policies[{tp.tool}]: класс {tp.max_data_class!r} не в policy.data_classes")
        return self

    def route(self, route_id: str) -> Route | None:
        return next((r for r in self.routes if r.id == route_id), None)

    def models_of(self, kind: str) -> list[ModelEntry]:
        return [m for m in self.models if m.enabled and (r := self.route(m.route_id)) is not None and r.kind == kind]


def describe(bad: ValidationError | ValueError) -> str:
    if isinstance(bad, ValidationError):
        return "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in bad.errors()[:5])
    return str(bad)


def parse(text: str) -> OrgConfig:
    return OrgConfig.model_validate(json.loads(text))


def org_file(orgs_dir: str, org_id: str, include_test: bool) -> Path | None:
    """Файл конфигурации организации: каталог организаций, иначе поставочная test-org в тестовом контуре."""
    p = Path(orgs_dir) / f"{org_id}.json"
    if p.is_file():
        return p
    if include_test and org_id == TEST_ORG:
        return PACKAGE_ORGS / f"{TEST_ORG}.json"
    return None


def dump(org: OrgConfig) -> str:
    """Каноническая запись файла организации: порядок ключей схемы, отступ 2, UTF-8 без экранирования."""
    doc = org.model_dump(mode="json")
    # Неархивная организация пишется без поля archived: файлы до #7 и их хеш (версия профиля) не меняются.
    if not doc.get("archived"):
        doc.pop("archived", None)
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


# id организации = имя файла: латиница, цифры, дефис (#7).
ORG_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
TEMPLATE = Path(__file__).parent / "templates" / "org.json"


def from_template(org_id: str, name: str) -> OrgConfig:
    """Новая организация из шаблона: один маршрут claude-sub (подписка) без моделей — модели подписки
    отмечаются в редакторе, провайдеры добавляются «Добавить маршрут» (#8); каждый класс данных → все
    маршруты; агент assistant по умолчанию с промптом шаблона, квота 50 USD."""
    doc = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    doc["id"], doc["name"] = org_id, name
    for a in doc.get("agents", []):
        a["system_prompt"] = a.get("system_prompt", "").replace("{name}", name)
    route_ids = [r["id"] for r in doc.get("routes", [])]
    doc["policy"]["routes_by_class"] = {cls: list(route_ids) for cls in doc["policy"]["data_classes"]}
    return OrgConfig.model_validate(doc)


def load_orgs(orgs_dir: str, include_test: bool) -> tuple[dict[str, OrgConfig], dict[str, str]]:
    """Все `<org>.json` каталога (+ поставочная `test-org` в тестовом контуре). Возвращает (годные, ошибки)."""
    files: list[Path] = []
    d = Path(orgs_dir)
    if d.is_dir():
        files += sorted(d.glob("*.json"))
    # Поставочная test-org — только если в каталоге нет своей копии (её пишет редактор админки).
    if include_test and not (d / f"{TEST_ORG}.json").is_file():
        files.append(PACKAGE_ORGS / f"{TEST_ORG}.json")
    good: dict[str, OrgConfig] = {}
    errors: dict[str, str] = {}
    for f in files:
        try:
            org = parse(f.read_text(encoding="utf-8"))
        except (ValidationError, ValueError) as bad:
            errors[f.stem] = describe(bad)
            log.error("конфигурация организации %s отвергнута: %s", f, errors[f.stem])
            continue
        if org.id != f.stem:
            errors[f.stem] = f"id {org.id!r} не совпадает с именем файла"
            continue
        good[org.id] = org
    return good, errors
