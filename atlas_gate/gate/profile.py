"""Управляемый профиль организации: сборка из конфигурации, сверка моделей, подпись JWS, версия.

`profile_version` монотонный: растёт, только когда меняется СОДЕРЖИМОЕ профиля или конфигурации
организации, из которой он собран (канонический sha256 тела + sha256 файла в канонической записи);
перезапуск службы с той же конфигурацией отдаёт тот же JWS и тот же ETag. Конфигурация входит в хеш,
чтобы каждая версия однозначно называла её снимок в истории (редактор админки, #6): правка адреса
апстрима или таймаута клиенту не видна, но версией и историей учитывается.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .crypto import SigningKey, canonical_json, sha256_hex
from .orgs import OrgConfig
from .store import Store
from .model_contracts import project_model

log = logging.getLogger("atlas_gate")

TTL_SECONDS = 604800


def build_body(org: OrgConfig, public_url: str, down_routes: set[str],
               agent_ids: set[str] | None, attachment_caps=None) -> tuple[dict[str, Any], list[str]]:
    """Тело профиля без метаданных версии.

    Маршруты клиенту — только id, kind, protocol, base_url (гейт) и region; адрес апстрима, имя переменной
    ключа и таймаут остаются на гейте. Модели gateway-маршрутов без ключа (`down_routes`) не отдаются;
    модели router-agent сверяются со списком подписки роутера (`agent_ids`; None — источник недоступен,
    модели отдаются как настроены, с предупреждением).
    """
    warnings: list[str] = []
    base = public_url.rstrip("/")
    routes = []
    for r in org.routes:
        if not r.enabled:
            continue
        routes.append({"id": r.id, "kind": r.kind, "protocol": r.protocol,
                       "base_url": base + "/harness/llm" if r.kind == "gateway" else base, "region": r.region})
    models = []
    for m in org.models:
        route = org.route(m.route_id)
        if route is not None and not route.enabled:
            continue
        if route is not None and route.kind == "gateway":
            if route.id in down_routes:
                warnings.append(f"модель {m.model} ({m.route_id}) не отдана: нет ключа {route.key_env}")
                continue
        elif agent_ids is None:
            warnings.append(f"модель {m.model} ({m.route_id}) не сверена: список подписки недоступен")
        elif m.model not in agent_ids:
            warnings.append(f"модель {m.model} ({m.route_id}) не отдана: её нет у роутера")
            continue
        models.append(dict(route_id=m.route_id, **project_model(m, (attachment_caps or {}).get((m.route_id, m.model)))))
    kept = {(m["route_id"], m["model"]) for m in models}
    pricing = [p.model_dump() for p in org.pricing if (p.route_id, p.model) in kept]
    body = {
        "org": {"id": org.id, "name": org.name},
        "ttl_seconds": TTL_SECONDS,
        "min_app_version": org.min_app_version,
        "recommended_app_version": org.recommended_app_version,
        "update_channel_url": org.update_channel_url,
        "routes": routes,
        "models": models,
        "pricing": pricing,
        "policy": org.policy.model_dump(),
        "agents": [a.model_dump() for a in org.agents],
        "mcp": [m.model_dump() for m in org.mcp],
        "builtin_tools": org.builtin_tools.model_dump(),
        "overrides": org.overrides.model_dump(),
        "feature_flags": org.feature_flags,
        "quota": org.quota.model_dump(),
        "audit": org.audit.model_dump(),
    }
    return body, warnings


def publish(store: Store, key: SigningKey, org_id: str, body: dict[str, Any], source: str = "") -> dict[str, Any]:
    """Подписать и сохранить, если содержимое изменилось; вернуть строку `profiles` организации.

    `source` — каноническая запись конфигурации организации (`orgs.dump`); пусто — только тело.
    """
    content_hash = sha256_hex(canonical_json(body))
    if source:
        content_hash = sha256_hex(content_hash + ":" + sha256_hex(source))
    current = store.profile(org_id)
    if current is not None and current["content_hash"] == content_hash:
        return current
    version = (current["profile_version"] + 1) if current is not None else 1
    issued_at = time.time()
    payload = {"profile_version": version, "issued_at": int(issued_at), **body}
    jws = key.sign(payload)
    etag = '"' + sha256_hex(jws) + '"'
    store.profile_put(org_id, version, content_hash, jws, etag, issued_at)
    log.info("профиль %s: версия %d подписана (kid %s…)", org_id, version, key.kid[:12])
    return store.profile(org_id) or {}
