"""Состояние гейта одного приложения: БД, ключи, конфигурации организаций, собранные профили, апстримы."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI

from ..settings import Settings
from .crypto import Keyring, SigningKey
from .orgs import OrgConfig, Route, dump, load_orgs
from .profile import build_body, publish
from .store import Store
from .nodes import Nodes
from .routing import Routing
from .operations import Operations
from .node_channel import Channel
from .node_identity import NodeIdentity

log = logging.getLogger("atlas_gate")

TEST_KEYRING = "atlas-gate-test-keyring"


@dataclass
class Device:
    device_id: str
    org: str
    user_id: str
    user_email: str
    user_name: str
    row: dict[str, Any]


def read_keys_file(path: str) -> dict[str, str]:
    """Ключи провайдеров из файла `ИМЯ=значение`; пустые значения пропускаются. Значения не логируются."""
    p = Path(path)
    if not p.is_file():
        return {}
    out: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if v.strip():
                out[k.strip()] = v.strip()
    return out


class GateState:
    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.settings: Settings = app.state.settings
        s = self.settings
        self.store = Store(s.ATLAS_GATE_DB)
        self.channel = Channel(self.store)
        self.nodes = Nodes(s.ATLAS_GATE_NODES_FILE, self.store, self.channel)
        self.routing = Routing(self.store, self.nodes)
        self.operations = Operations(self.store)
        from .api_reasoning import ApiReasoning
        self.api_reasoning = ApiReasoning(self.store)
        from .recovery import Recovery
        from .api_attempts import ApiAttempts
        self.recovery = Recovery(self)
        self.api_attempts = ApiAttempts(self.store)
        def api_admission(request, device, route, attempt_id, *, db=None):
            from .api import org_of, check_data_class
            from .errors import GateError
            current_org = org_of(self, device)
            current_route = current_org.route(route.id)
            current_model = next((m for m in current_org.models if m.route_id == route.id
                and m.model == getattr(request.state, 'api_model_name', None)), None)
            if (current_route is None or not current_route.enabled or current_route.model_dump() != route.model_dump()
                    or current_model is None or not current_model.enabled
                    or current_model.model_dump() != getattr(request.state, 'api_model_snapshot', None)):
                raise GateError(409, 'api_policy_changed_before_dispatch', 'профиль изменился до API dispatch; запрос не отправлен',
                                deliveryState='not_sent')
            check_data_class(self, device, request, route.id)
            def integer(name):
                value = request.headers.get(name)
                if value is None:
                    return None
                try:
                    return int(value)
                except ValueError as error:
                    raise GateError(422, 'recovery_fence_invalid', 'неверный recovery fence') from error
            self.recovery.check_admission(device,
                conversation_id=request.headers.get('x-atlas-conversation-id'),
                branch_id=request.headers.get('x-atlas-branch-id'),
                inventory_id=request.headers.get('x-atlas-recovery-inventory'),
                generation=integer('x-atlas-recovery-generation'),
                control_epoch=integer('x-atlas-control-epoch'),
                admitting_api_attempt_id=attempt_id, db=db)
        self.api_attempts.admission = api_admission
        from .context_windows import ContextWindows
        self.context_windows = ContextWindows(self)
        self.watchers: set[asyncio.Task] = set()
        self._closed = False
        self.key = SigningKey.load_or_create(s.ATLAS_GATE_KEY_PATH)
        self.identity = NodeIdentity(self.store, self.nodes, s.ATLAS_GATE_NODE_PKI_DIR, "atlas-gate:" + self.key.kid)
        secret = s.ATLAS_GATE_KEYRING_SECRET
        if not secret and s.ATLAS_GATE_TEST:
            log.warning("ATLAS_GATE_KEYRING_SECRET пуст — тестовый контур шифрует постоянным тестовым секретом")
            secret = TEST_KEYRING
        self.keyring = Keyring(secret)
        # Один клиент на все апстримы: соединения переиспользуются; таймаут — по маршруту на запрос.
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
        self.orgs: dict[str, OrgConfig] = {}
        self.org_errors: dict[str, str] = {}
        self.warnings: dict[str, list[str]] = {}
        # Собранное тело профиля по организации — отсюда passthrough берёт разрешённые модели.
        self.bodies: dict[str, dict[str, Any]] = {}
        # Ключи провайдеров по имени (key_env) — перечитываются при старте и reload; в логи не попадают.
        self._keys: dict[str, str] = {}
        # (org, route_id) → "ok" | "down": есть ли ключ и отвечает ли апстрим (проверка при старте/reload).
        self.route_keyed: dict[tuple[str, str], bool] = {}
        self.upstreams: dict[str, str] = {}
        self.ready = asyncio.Event()
        self._reload_lock = asyncio.Lock()
        # Запись файла организации из админки: сверка ETag, запись и reload — одним шагом (#6).
        self.write_lock = asyncio.Lock()
        self._unpriced_warned: set[tuple[str, str]] = set()

    @property
    def public_url(self) -> str:
        s = self.settings
        if s.ATLAS_GATE_PUBLIC_URL:
            return s.ATLAS_GATE_PUBLIC_URL.rstrip("/")
        host = "127.0.0.1" if s.ATLAS_HOST in ("0.0.0.0", "::", "") else s.ATLAS_HOST
        return f"http://{host}:{s.ATLAS_PORT}"

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        context_task = getattr(self, "context_task", None)
        if context_task is not None:
            context_task.cancel()
            await asyncio.gather(context_task, return_exceptions=True)
        recovery_task = getattr(self, "recovery_task", None)
        if recovery_task is not None and recovery_task is not asyncio.current_task():
            recovery_task.cancel()
            await asyncio.gather(recovery_task, return_exceptions=True)
        for task in self.watchers:
            task.cancel()
        await asyncio.gather(*self.watchers, return_exceptions=True)
        await self.api_attempts.aclose()
        await self.nodes.close()
        await self.http.aclose()
        self.store.close()

    # --- ключи и апстримы ---------------------------------------------------------------------

    def key_for(self, route: Route) -> str | None:
        """Ключ маршрута: файл ключей гейта, затем окружение процесса."""
        return self._keys.get(route.key_env) or os.environ.get(route.key_env) or None

    def warn_unpriced_once(self, route_id: str, model: str) -> None:
        if (route_id, model) not in self._unpriced_warned:
            self._unpriced_warned.add((route_id, model))
            log.warning("нет цены для %s/%s в pricing[] — стоимость вызовов пишется null", route_id, model)

    async def _probe(self, route: Route, key: str) -> str:
        """Лёгкая проверка апстрима: GET {upstream}/models с ключом; 2xx/4xx от провайдера — жив."""
        headers = ({"x-api-key": key, "anthropic-version": self.settings.ATLAS_GATE_ANTHROPIC_VERSION}
                   if route.protocol == "anthropic" else {"Authorization": f"Bearer {key}"})
        try:
            r = await self.http.get(route.upstream() + "/models", headers=headers, timeout=10.0)
        except httpx.HTTPError as failed:
            log.warning("апстрим %s недоступен: %s", route.id, type(failed).__name__)
            return "down"
        if r.status_code in (401, 403):
            log.warning("апстрим %s отверг ключ %s (HTTP %d)", route.id, route.key_env, r.status_code)
            return "down"
        return "ok" if r.status_code < 500 else "down"

    async def _refresh_routes(self, orgs: dict[str, OrgConfig]) -> None:
        self._keys = read_keys_file(self.settings.ATLAS_GATE_KEYS_FILE)
        keyed: dict[tuple[str, str], bool] = {}
        probes: dict[str, Any] = {}
        for org in orgs.values():
            for r in org.routes:
                if r.kind != "gateway":
                    continue
                key = self.key_for(r)
                keyed[(org.id, r.id)] = key is not None
                if key is None:
                    log.warning("маршрут %s/%s: переменной %s нет в файле ключей и окружении — маршрут down",
                                org.id, r.id, r.key_env)
                elif r.id not in probes:
                    probes[r.id] = self._probe(r, key)
        results = dict(zip(probes, await asyncio.gather(*probes.values()))) if probes else {}
        upstreams = {rid: "down" for (_, rid), ok in keyed.items() if not ok}
        upstreams.update(results)
        self.route_keyed, self.upstreams = keyed, upstreams

    # --- конфигурации и профили ---------------------------------------------------------------

    async def _agent_model_ids(self) -> set[str] | None:
        try:
            ids = await asyncio.wait_for(self.nodes.models(), 70)
        except Exception as failed:  # noqa: BLE001
            log.warning("модели подписки для профиля не получены: %s", failed)
            return None
        return ids

    async def reload(self) -> dict[str, Any]:
        """Перечитать конфигурации и ключи, проверить апстримы, переподписать изменённые профили."""
        async with self._reload_lock:
            orgs, errors = load_orgs(self.settings.ATLAS_GATE_ORGS_DIR, self.settings.ATLAS_GATE_TEST)
            await self._refresh_routes(orgs)
            agent_ids = await self._agent_model_ids() if any(o.models_of("router-agent") for o in orgs.values()) else set()
            self.context_windows.catalog()
            versions: dict[str, int] = {}
            warnings: dict[str, list[str]] = {}
            bodies: dict[str, dict[str, Any]] = {}
            for org in orgs.values():
                down = {r.id for r in org.routes
                        if r.kind == "gateway" and (not r.enabled or not self.route_keyed.get((org.id, r.id)))}
                org_agent_ids = self.nodes.model_ids(org.id, [r.id for r in org.routes if r.kind == "router-agent"])
                from .attachments import route_attachments
                caps = {(m.route_id, m.model): route_attachments(self, org.id, m.route_id, m.model)
                        for m in org.models if (r := org.route(m.route_id)) and r.kind == 'router-agent'}
                body, warn = build_body(org, self.public_url, down, org_agent_ids if agent_ids is not None else None, caps)
                from .transport_recovery import CAPABILITY
                body['transportRecovery'] = dict(CAPABILITY)
                body['commandRecovery'] = {'version': 1, 'protocol': 'admission-fence-v1', 'scope': 'prompt',
                    'resolvePath': '/harness/agent/sessions/{sessionId}/commands/reconcile',
                    'canonicalBody': 'json-sort-keys-compact-utf8-v1', 'lateAdmissionBlocked': True,
                    'requiresNewIntent': True}
                body['toolResultRecovery'] = {
                    'version': 1,
                    'statusPath': '/harness/agent/sessions/{sessionId}/turns/{turnId}/tool-result-recovery',
                    'turnStatusPath': '/harness/agent/sessions/{sessionId}/turns/{turnId}',
                    'retryTurnHeader': 'X-Atlas-Tool-Result-Turn',
                    'canonicalBody': 'json-sort-keys-compact-utf8-v1',
                }
                from .recovery import CAPABILITY as RECOVERY_CAPABILITY
                from .api_attempts import api_attempt_contract
                body['sessionRecoveryCoordinator'] = dict(RECOVERY_CAPABILITY)
                body['apiAttemptRecovery'] = api_attempt_contract()
                for model in body['models']:
                    model.setdefault('sessionRecovery', None)
                    model.setdefault('steering', None)
                    route = org.route(model['route_id'])
                    if route and route.kind == 'router-agent':
                        model.update(self.context_windows.project(org.id,route.id,model['model']))
                        from .reasoning import project_capabilities
                        model['reasoningCapabilities'] = project_capabilities(self, org.id, route.id, model['model'])
                        model['sessionRecovery'] = self.project_recovery(org.id, route.id, model['model'])
                        model['steering'] = self.project_steering(org.id, route.id, model['model'])
                    options = [info.get('sdkTools') or {} for node_id, info in self.nodes.snapshots.items()
                        if self.nodes.config[node_id].enabled and org.id in self.nodes.config[node_id].orgs
                        and model['route_id'] in self.nodes.config[node_id].routes
                        and model['model'] in {m.get('value') for m in info.get('models', [])}]
                    modes = sorted({mode for c in options for mode in c.get('webSearch', {}).get('modes', [])
                        if org.sdk_tools.webSearch == 'live' or org.sdk_tools.webSearch == mode})
                    model['sdkTools'] = {'version':1, 'webSearch':{'modes':modes, 'supported':bool(modes),
                        'executor':'sdk', 'accountAccess':'unknown'}, 'files':{'supported':org.sdk_tools.files and
                        any(c.get('files', {}).get('supported') is True for c in options),
                        'executor':'router', 'invocation':'sdk', 'processing':'isolated-python-v1'}}
                    model['sdkTools']['imageGeneration'] = {
                        'supported': org.sdk_tools.imageGeneration and any(
                            c.get('imageGeneration', {}).get('supported') is True for c in options),
                        'artifacts': 'session-media-v1', 'accountAccess': 'unknown'}
                    if route and route.kind == 'gateway':
                        configured = next((m for m in org.models if m.route_id == route.id and m.model == model['model']), None)
                        model['sdkTools']['imageGeneration'] = {
                            'supported': bool(org.sdk_tools.imageGeneration and configured and configured.image_generation and route.protocol == 'openai'),
                            'executor': 'api', 'billing': 'api', 'accountAccess': 'unknown'}
                for w in warn:
                    log.warning("профиль %s: %s", org.id, w)
                row = publish(self.store, self.key, org.id, body, dump(org))
                versions[org.id] = int(row["profile_version"])
                self.snapshot(org, versions[org.id])
                warnings[org.id] = warn
                bodies[org.id] = body
            self.orgs, self.org_errors, self.warnings, self.bodies = orgs, errors, warnings, bodies
            self.ready.set()
            return {"orgs": versions, "errors": errors, "warnings": warnings, "upstreams": dict(self.upstreams)}

    def project_recovery(self, org_id, route_id, model):
        # Exact node and boot, never a union of capabilities across providers.
        return {'version': 1, 'scope': 'pinned_node', 'nodes': {
            nid: {**(info.get('sessionRecoveryProofs') or {'supported': False}),
                  'nodeId': nid, 'bootId': info.get('bootId'),
                  'transportSupported': node.transport == 'direct' or info.get('connectorProtocol') == 2}
            for nid, info in self.nodes.snapshots.items()
            if (node := self.nodes.config[nid]).enabled and org_id in node.orgs
            and route_id in node.routes and model in {m.get('value') for m in info.get('models', [])}}}

    def project_steering(self, org_id, route_id, model):
        from .steering import bound_capability
        return {'version': 1, 'scope': 'pinned_node', 'nodes': {
            nid: {**bound_capability(info.get('steering'), {'node_id': nid, 'boot_id': info.get('bootId')}),
                  'nodeId': nid, 'bootId': info.get('bootId')}
            for nid, info in self.nodes.snapshots.items()
            if (node := self.nodes.config[nid]).enabled and org_id in node.orgs
            and route_id in node.routes and model in {m.get('value') for m in info.get('models', [])}}}

    async def publish_contexts(self):
        """Метаданные меняются без пересоздания агентов, сессий и org config."""
        import copy
        async with self._reload_lock:
            bodies = copy.deepcopy(self.bodies)
            for org in self.orgs.values():
                body = bodies.get(org.id)
                if not body:
                    continue
                for model in body['models']:
                    route = org.route(model['route_id'])
                    if route and route.kind == 'router-agent':
                        model.update(self.context_windows.project(org.id,route.id,model['model']))
                        from .reasoning import project_capabilities
                        model['reasoningCapabilities'] = project_capabilities(self, org.id, route.id, model['model'])
                        model['sessionRecovery'] = self.project_recovery(org.id, route.id, model['model'])
                        model['steering'] = self.project_steering(org.id, route.id, model['model'])
                row = publish(self.store,self.key,org.id,body,dump(org))
                self.snapshot(org,int(row['profile_version']))
            self.bodies = bodies

    # --- история конфигураций организаций (редактор админки, #6) ---------------------------------

    @property
    def history_dir(self) -> Path:
        return Path(self.settings.ATLAS_GATE_ORGS_DIR) / "history"

    def history_path(self, org_id: str, version: int) -> Path:
        return self.history_dir / f"{org_id}-v{version}.json"

    def snapshot(self, org: OrgConfig, version: int) -> None:
        """Конфигурация, из которой собрана версия профиля, — в history/<org>-v<N>.json (один раз на версию).

        Так в историю попадает и сохранение из редактора, и ручная правка файла, подхваченная reload-ом.
        Секретов в конфигурации нет по устройству: только имена переменных (key_env, headers_ref).
        """
        p = self.history_path(org.id, version)
        if p.exists():
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(dump(org), encoding="utf-8", newline="\n")
        os.replace(tmp, p)

    def history(self, org_id: str) -> list[dict[str, Any]]:
        out = []
        for p in self.history_dir.glob(f"{org_id}-v*.json"):
            try:
                n = int(p.stem.rsplit("-v", 1)[1])
            except ValueError:
                continue
            out.append({"version": n, "saved_at": p.stat().st_mtime, "size": p.stat().st_size})
        return sorted(out, key=lambda h: h["version"], reverse=True)

    def secret_present(self, name: str) -> bool:
        return bool(name) and (name in self._keys or bool(os.environ.get(name)))

    def profile_version(self) -> int:
        rows = [self.store.profile(o) for o in self.orgs]
        return max((int(r["profile_version"]) for r in rows if r), default=0)

    # --- здоровье агентного бэкенда -------------------------------------------------------------

    async def agent_backend_ok(self) -> bool:
        await self.nodes.refresh()
        return any(info.get("ready") for info in self.nodes.snapshots.values())
