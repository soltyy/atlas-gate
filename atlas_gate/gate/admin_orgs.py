"""Редактор профиля организации в админке гейта (GW-ADMIN-01, тикет #6).

Источник истины — по-прежнему файл `gate/orgs/<org>.json`; API читает и пишет его:
- `GET  /harness/admin/orgs` — сводка по организациям;
- `GET  /harness/admin/orgs/{org}` — конфигурация без секретов (у key_env/headers_ref — имя и `present`),
  плюс модели подписки роутера и состояние апстримов; ETag — sha256 файла;
- `PUT  /harness/admin/orgs/{org}` (If-Match: ETag) — та же pydantic-модель, что при старте; 422 с ошибками
  по полям (файл не тронут), 409 — файл изменился после GET; атомарная запись (tmp + os.replace), reload;
- `GET  …/history`, `GET …/history/{N}`, `POST …/history/{N}/restore` — версии и откат (как PUT).
- `POST /harness/admin/orgs` — новая организация из шаблона или копией (#7); `PATCH …/{org}` — название;
  `POST …/{org}/archive|unarchive` — архив. Удаления нет: файл и история остаются всегда.

Секретов в файле нет по устройству (только имена переменных), поэтому их нет ни в ответах, ни в истории.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from ..auth import require_token
from .api import gate, ready_gate, router
from .errors import GateError
from .orgs import ORG_ID, OrgConfig, dump, from_template, org_file

log = logging.getLogger("atlas_gate")
admin = [Depends(require_token)]
AGENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def file_etag(path: Path) -> str:
    return '"' + hashlib.sha256(path.read_bytes()).hexdigest() + '"'


def _secrets(state: Any, org: OrgConfig) -> dict[str, bool]:
    names = {r.key_env for r in org.routes if r.key_env} | {m.headers_ref for m in org.mcp if m.headers_ref}
    return {n: state.secret_present(n) for n in sorted(names)}


def _fields(bad: ValidationError | ValueError) -> list[dict[str, str]]:
    if isinstance(bad, ValidationError):
        return [{"loc": ".".join(str(x) for x in e["loc"]), "msg": e["msg"].removeprefix("Value error, ")}
                for e in bad.errors()]
    return [{"loc": "", "msg": str(bad)}]


def _validate(state: Any, org_id: str, doc: Any) -> OrgConfig:
    try:
        if not isinstance(doc, dict):
            raise ValueError("тело — JSON-объект конфигурации организации")
        org = OrgConfig.model_validate(doc)
        if org.id != org_id:
            raise ValueError(f"id {org.id!r} не совпадает с организацией {org_id!r} в адресе")
        # Редактор: у организации с агентами — ровно один агент по умолчанию.
        if org.agents and sum(1 for a in org.agents if a.default) != 1:
            raise ValueError("нужен ровно один агент с default=true")
        # Редактор (#12): у агента осмысленный id — пустой агент из «Добавить агента» уходил в профиль.
        bad_ids = [{"loc": f"agents.{i}.id", "msg": "id агента: латиница, цифры, «-» и «_» (1–64 знака)"}
                   for i, a in enumerate(org.agents) if not AGENT_ID.fullmatch(a.id)]
        if bad_ids:
            raise GateError(422, "profile_invalid", bad_ids[0]["msg"], fields=bad_ids)
    except (ValidationError, ValueError) as bad:
        fields = _fields(bad)
        raise GateError(422, "profile_invalid", "; ".join(f"{f['loc'] or 'профиль'}: {f['msg']}" for f in fields[:5]),
                        fields=fields) from bad
    return org


def _path(state: Any, org_id: str) -> Path:
    p = org_file(state.settings.ATLAS_GATE_ORGS_DIR, org_id, state.settings.ATLAS_GATE_TEST)
    if p is None:
        raise GateError(404, "not_found", f"организации {org_id!r} нет")
    return p


def route_usage(state: Any, org: OrgConfig) -> dict[str, dict[str, Any]]:
    """Можно ли удалить маршрут: у него нет ни одного вызова в usage и ни одного устройства с его
    моделями (#8). Иначе — только выключить: вызовы в учёте ссылаются на маршрут, а устройства
    держат его модели в полученном профиле.

    Вызовы: gateway — по полю route; записи без маршрута (импорт LiteLLM) — по модели маршрута;
    router-agent — ходы на подписке (kind=agent, маршрут в учёт не пишется). Устройства: активные
    устройства организации, если модели маршрута есть в подписанном профиле, то есть были им отданы.
    """
    groups = state.store.usage_groups(org.id)
    delivered = {m.get("route_id") for m in (state.bodies.get(org.id) or {}).get("models", [])}
    active = sum(1 for d in state.store.devices() if d["org"] == org.id and d["revoked_at"] is None)
    out: dict[str, dict[str, Any]] = {}
    for r in org.routes:
        models = {m.model for m in org.models if m.route_id == r.id}
        if r.kind == "router-agent":
            calls = sum(g["n"] for g in groups if g["kind"] == "agent")
        else:
            calls = sum(g["n"] for g in groups
                        if g["route"] == r.id or (g["route"] is None and g["kind"] == "gateway" and g["model"] in models))
        devices = active if r.id in delivered else 0
        reasons = []
        if calls:
            reasons.append(f"вызовов в учёте: {calls}")
        if devices:
            reasons.append(f"устройств с его моделями: {devices}")
        out[r.id] = {"calls": calls, "devices": devices, "deletable": not reasons, "reason": ", ".join(reasons)}
    return out


def _atomic_write(target: Path, text: str) -> None:
    """tmp в том же каталоге + os.replace: читатель видит старый файл или новый, но не половину."""
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)


async def _write(state: Any, org_id: str, org: OrgConfig, if_match: str | None, require_match: bool) -> dict[str, Any]:
    async with state.write_lock:
        return await _write_locked(state, org_id, org, if_match, require_match)


async def _write_locked(state: Any, org_id: str, org: OrgConfig, if_match: str | None,
                        require_match: bool) -> dict[str, Any]:
    path = _path(state, org_id)
    current = file_etag(path)
    if (require_match or if_match) and if_match != current:
        raise GateError(409, "conflict", "файл организации изменился после чтения — перечитайте и повторите",
                        etag=current)
    old = state.orgs.get(org_id)
    if old is not None:
        usage = route_usage(state, old)
        kept = {r.id for r in org.routes}
        refused = [(rid, u) for rid, u in usage.items() if rid not in kept and not u["deletable"]]
        if refused:
            fields = [{"loc": "routes", "msg": f"маршрут {rid} нельзя удалить: {u['reason']} — его можно только выключить"}
                      for rid, u in refused]
            raise GateError(422, "profile_invalid", "; ".join(f["msg"] for f in fields), fields=fields)
    # Поставочная test-org правится копией в каталоге организаций (он в приоритете при загрузке).
    target = Path(state.settings.ATLAS_GATE_ORGS_DIR) / f"{org_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    row = state.store.profile(org_id)
    if row is not None and org_id in state.orgs:
        state.snapshot(state.orgs[org_id], int(row["profile_version"]))  # текущая версия — в истории до записи
    _atomic_write(target, dump(org))
    result = await state.reload()
    if org_id in result["errors"]:
        raise GateError(500, "profile_invalid", f"после записи организация не загрузилась: {result['errors'][org_id]}")
    log.info("организация %s сохранена из админки: профиль версии %s", org_id, result["orgs"].get(org_id))
    return {"profile_version": result["orgs"].get(org_id), "warnings": result["warnings"].get(org_id, []),
            "etag": file_etag(target)}


@router.get("/harness/admin/orgs", dependencies=admin)
async def orgs_list(request: Request) -> Any:
    state = await ready_gate(request)
    devices: dict[str, int] = {}
    for d in state.store.devices():
        if d["revoked_at"] is None:
            devices[d["org"]] = devices.get(d["org"], 0) + 1
    out = []
    for org_id, org in sorted(state.orgs.items()):
        row = state.store.profile(org_id) or {}
        out.append({"org": org_id, "name": org.name, "archived": org.archived,
                    "profile_version": row.get("profile_version"),
                    "signed_at": row.get("issued_at"),
                    "counts": {"routes": len(org.routes), "models": len(org.models), "agents": len(org.agents),
                               "devices": devices.get(org_id, 0)},
                    "warnings": state.warnings.get(org_id, [])})
    return {"orgs": out, "errors": state.org_errors}


@router.get("/harness/admin/orgs/{org_id}", dependencies=admin)
async def org_get(org_id: str, request: Request) -> Any:
    state = await ready_gate(request)
    org = state.orgs.get(org_id)
    if org is None:
        raise GateError(404, "not_found", f"организация {org_id!r} не загружена: " + state.org_errors.get(org_id, "нет файла"))
    path = _path(state, org_id)
    etag = file_etag(path)
    listed = await state._agent_model_ids()
    row = state.store.profile(org_id) or {}
    body = {"org": json.loads(dump(org)), "etag": etag, "archived": org.archived,
            "profile_version": row.get("profile_version"),
            "signed_at": row.get("issued_at"), "secrets": _secrets(state, org),
            "subscription_models": sorted(listed or []), "upstreams": dict(state.upstreams),
            "subscription_models_by_route": {r.id: state.nodes.route_catalog(org.id, r.id)
                                             for r in org.routes if r.kind == "router-agent"},
            "route_usage": route_usage(state, org),
            "warnings": state.warnings.get(org_id, [])}
    return JSONResponse(body, headers={"ETag": etag})


@router.put("/harness/admin/orgs/{org_id}", dependencies=admin,
            openapi_extra={"requestBody": {"content": {"application/json": {"schema": OrgConfig.model_json_schema()}}}})
async def org_put(org_id: str, request: Request) -> Any:
    state = await ready_gate(request)
    try:
        doc = json.loads(await request.body())
    except ValueError as bad:
        raise GateError(422, "profile_invalid", "тело не JSON", fields=[{"loc": "", "msg": "тело не JSON"}]) from bad
    org = _validate(state, org_id, doc)
    return await _write(state, org_id, org, request.headers.get("if-match"), require_match=True)


@router.get("/harness/admin/orgs/{org_id}/history", dependencies=admin)
async def org_history(org_id: str, request: Request) -> Any:
    state = await ready_gate(request)
    row = state.store.profile(org_id) or {}
    return {"org": org_id, "current": row.get("profile_version"), "versions": state.history(org_id)}


@router.get("/harness/admin/orgs/{org_id}/history/{version}", dependencies=admin)
async def org_history_version(org_id: str, version: int, request: Request) -> Any:
    state = gate(request)
    p = state.history_path(org_id, version)
    if not p.is_file():
        raise GateError(404, "not_found", f"версии {version} организации {org_id!r} нет в истории")
    return json.loads(p.read_text(encoding="utf-8"))


@router.post("/harness/admin/orgs/{org_id}/history/{version}/restore", dependencies=admin)
async def org_restore(org_id: str, version: int, request: Request) -> Any:
    state = await ready_gate(request)
    p = state.history_path(org_id, version)
    if not p.is_file():
        raise GateError(404, "not_found", f"версии {version} организации {org_id!r} нет в истории")
    doc = json.loads(p.read_text(encoding="utf-8"))
    note = _mark_default(state, org_id, doc)
    org = _validate(state, org_id, doc)
    result = await _write(state, org_id, org, request.headers.get("if-match"), require_match=False)
    if note:
        result["warnings"] = [note, *result["warnings"]]
    return result


def _mark_default(state: Any, org_id: str, doc: Any) -> str:
    """Версия из истории до #6 не знает флага default: отметить агента, как в текущей конфигурации
    (если он есть в восстанавливаемой), иначе последнюю версию первого агента. Возвращает пояснение."""
    agents = doc.get("agents") if isinstance(doc, dict) else None
    if not isinstance(agents, list) or not agents or any(isinstance(a, dict) and a.get("default") for a in agents):
        return ""
    current = state.orgs.get(org_id)
    cur = next(((a.id, a.version) for a in (current.agents if current else []) if a.default), None)
    pick = next((a for a in agents if isinstance(a, dict) and (a.get("id"), a.get("version")) == cur), None)
    if pick is None:
        first = [a for a in agents if isinstance(a, dict) and a.get("id") == agents[0].get("id")]
        pick = first[-1] if first else None
    if pick is None:
        return ""
    pick["default"] = True
    return f"в восстановленной версии не был отмечен агент по умолчанию — отмечен {pick.get('id')} v{pick.get('version')}"


# --- создание, переименование, архив (GW-ADMIN-02, #7) ------------------------------------------


class OrgCreate(BaseModel):
    id: str
    name: str
    # template | copy_of:<org>
    source: str = "template"


class OrgRename(BaseModel):
    name: str


def _invalid(loc: str, msg: str) -> GateError:
    return GateError(422, "profile_invalid", f"{loc}: {msg}", fields=[{"loc": loc, "msg": msg}])


@router.post("/harness/admin/orgs", dependencies=admin, status_code=201)
async def org_create(body: OrgCreate, request: Request) -> Any:
    """Новая организация: файл <id>.json, профиль версии 1, история с v1. 409 — id уже занят."""
    state = await ready_gate(request)
    org_id, name = body.id.strip(), body.name.strip()
    if not ORG_ID.fullmatch(org_id):
        raise _invalid("id", "только латиница, цифры и дефис (1–64 знака) — это же имя файла")
    if not name:
        raise _invalid("name", "название не может быть пустым")
    src: OrgConfig | None = None
    if body.source.startswith("copy_of:"):
        src = state.orgs.get(body.source.removeprefix("copy_of:"))
        if src is None:
            raise _invalid("source", f"организации-источника {body.source.removeprefix('copy_of:')!r} нет")
    elif body.source != "template":
        raise _invalid("source", "источник — template или copy_of:<организация>")
    async with state.write_lock:
        target = Path(state.settings.ATLAS_GATE_ORGS_DIR) / f"{org_id}.json"
        # Занятый id: файл, загруженная организация, подписанный профиль или устройства с этим id.
        # Повтор id удалённой вручную организации отдал бы ей чужие устройства — поэтому тоже 409.
        taken = (target.exists() or org_id in state.orgs or org_id in state.org_errors
                 or state.store.profile(org_id) is not None
                 or any(d["org"] == org_id for d in state.store.devices()))
        if taken:
            raise GateError(409, "conflict", f"организация с id {org_id!r} уже есть (или была) — выберите другой id")
        try:
            if src is None:
                org = from_template(org_id, name)
            else:
                # Копируется конфигурация целиком; устройства и история живут вне файла — у копии их нет.
                doc = src.model_dump(mode="json")
                doc.update(id=org_id, name=name, archived=False)
                org = OrgConfig.model_validate(doc)
        except (ValidationError, ValueError) as bad:
            raise GateError(422, "profile_invalid", "конфигурация не прошла проверку", fields=_fields(bad)) from bad
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, dump(org))
        result = await state.reload()
    if org_id in result["errors"]:
        raise GateError(500, "profile_invalid", f"после записи организация не загрузилась: {result['errors'][org_id]}")
    log.info("организация %s создана из админки (%s): профиль версии %s", org_id, body.source,
             result["orgs"].get(org_id))
    return {"org": org_id, "profile_version": result["orgs"].get(org_id),
            "warnings": result["warnings"].get(org_id, []), "etag": file_etag(target)}


def _loaded(state: Any, org_id: str) -> OrgConfig:
    org = state.orgs.get(org_id)
    if org is None:
        raise GateError(404, "not_found",
                        f"организация {org_id!r} не загружена: " + state.org_errors.get(org_id, "нет файла"))
    return org


@router.patch("/harness/admin/orgs/{org_id}", dependencies=admin)
async def org_rename(org_id: str, body: OrgRename, request: Request) -> Any:
    """Новое название; id (имя файла, привязка устройств) неизменен."""
    state = await ready_gate(request)
    name = body.name.strip()
    if not name:
        raise _invalid("name", "название не может быть пустым")
    org = _loaded(state, org_id).model_copy(update={"name": name}, deep=True)
    return await _write(state, org_id, org, request.headers.get("if-match"), require_match=False)


async def _set_archived(request: Request, org_id: str, archived: bool) -> Any:
    state = await ready_gate(request)
    org = _loaded(state, org_id).model_copy(update={"archived": archived}, deep=True)
    result = await _write(state, org_id, org, request.headers.get("if-match"), require_match=False)
    log.info("организация %s %s", org_id, "в архиве" if archived else "возвращена из архива")
    return {"archived": archived, **result}


@router.post("/harness/admin/orgs/{org_id}/archive", dependencies=admin)
async def org_archive(org_id: str, request: Request) -> Any:
    return await _set_archived(request, org_id, True)


@router.post("/harness/admin/orgs/{org_id}/unarchive", dependencies=admin)
async def org_unarchive(org_id: str, request: Request) -> Any:
    return await _set_archived(request, org_id, False)
