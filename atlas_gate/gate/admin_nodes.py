"""Добавление одобренного узла без рестарта и без изменения существующих привязок."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Literal

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from .api import admin, gate, router
from .errors import GateError
from .nodes import Node, Nodes
from .state import read_keys_file

IDENTIFIER = r"^[A-Za-z0-9_-]{1,64}$"


def revision(path):
    p = Path(path)
    return hashlib.sha256(p.read_bytes() if p.exists() else b"[]").hexdigest()


def atomic_private(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class NewNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str
    node_id: str = Field(pattern=IDENTIFIER)
    backend: Literal["claude", "codex"]
    transport: Literal["direct", "connector"]
    org: str
    routes: list[str] = Field(min_length=1, max_length=100)
    account_group: str = Field(pattern=IDENTIFIER)
    share_account: bool = False
    account_turn_capacity: int = Field(default=0, ge=0, le=256)
    url: str = Field(default="", max_length=2048)
    token: SecretStr = Field(default=SecretStr(""))


def backend_for(state, node):
    return node.backend or state.nodes.snapshots.get(node.node_id, {}).get("backend", "")


@router.get("/harness/admin/node-options", dependencies=admin)
async def options(request: Request):
    state = gate(request)
    if revision(state.settings.ATLAS_GATE_NODES_FILE) != state.nodes.config_revision:
        raise GateError(409, "conflict", "nodes.json изменён вне Gate. Перезапустите Gate для чтения конфигурации.")
    return {"revision": state.nodes.config_revision,
            "gate_url": state.public_url,
            "mtls_port": state.settings.ATLAS_GATE_NODE_TLS_PORT,
            "orgs": [{"id": org.id, "name": org.name, "routes": [
                {"id": r.id, "backends": sorted({backend_for(state, n) for n in state.nodes.config.values()
                    if org.id in n.orgs and r.id in n.routes and backend_for(state, n)})}
                for r in org.routes if r.kind == "router-agent" and r.enabled]}
                for org in state.orgs.values() if not org.archived],
            "account_groups": [{"id": group, "capacity": nodes[0].account_turn_capacity,
                "backends": sorted({backend_for(state, n) for n in nodes if backend_for(state, n)})}
                for group in sorted({n.account_group for n in state.nodes.config.values()})
                for nodes in [[n for n in state.nodes.config.values() if n.account_group == group]]]}


@router.post("/harness/admin/nodes", dependencies=admin, status_code=201)
async def add(body: NewNode, request: Request):
    state = gate(request)
    async with state.write_lock:
        path = state.settings.ATLAS_GATE_NODES_FILE
        if revision(path) != body.revision or body.revision != state.nodes.config_revision:
            raise GateError(409, "conflict", "Список Router изменился. Обновите его и повторите добавление.")
        if body.node_id in state.nodes.config:
            raise GateError(409, "conflict", "Такой идентификатор Router уже занят. Существующий узел не изменён.")
        org = state.orgs.get(body.org)
        allowed = {r.id for r in org.routes if r.enabled and r.kind == "router-agent"} if org and not org.archived else set()
        if not set(body.routes).issubset(allowed):
            raise GateError(422, "invalid_request", "Выберите действующую организацию и её маршруты подписки.")
        for n in state.nodes.config.values():
            backend = backend_for(state, n)
            if backend and backend != body.backend and body.org in n.orgs and set(n.routes).intersection(body.routes):
                raise GateError(422, "invalid_request", "Этот маршрут используется другим поставщиком. Создайте отдельный маршрут в организации.")
            if n.account_group == body.account_group:
                if not body.share_account:
                    raise GateError(409, "conflict", "Это имя группы уже используется. Для другого аккаунта задайте новое имя; для того же выберите общую группу.")
                if n.account_turn_capacity != body.account_turn_capacity or (backend and backend != body.backend):
                    raise GateError(422, "invalid_request", "У общего аккаунта должны совпадать поставщик и лимит параллельной работы.")
        if body.share_account and not any(n.account_group == body.account_group for n in state.nodes.config.values()):
            raise GateError(422, "invalid_request", "Общая группа аккаунта больше не существует. Обновите список.")
        token = body.token.get_secret_value()
        if len(token) > 4096 or any(c in token for c in "\r\n\x00"):
            raise GateError(422, "invalid_request", "Ключ Router должен быть одной строкой до 4096 знаков.")
        if body.transport == "direct" and not token:
            raise GateError(422, "invalid_request", "Для прямого подключения нужен ATLAS_TOKEN Router.")
        if body.transport == "connector" and (token or body.url):
            raise GateError(422, "invalid_request", "Исходящий Router не передаёт Gate свой локальный токен или адрес.")
        token_env = "ATLAS_NODE_" + uuid.uuid4().hex.upper() if token else ""
        try:
            node = Node(node_id=body.node_id, backend=body.backend, transport=body.transport,
                url=body.url, token_env=token_env, orgs=[body.org], routes=sorted(set(body.routes)),
                account_group=body.account_group, account_turn_capacity=body.account_turn_capacity)
            # Дополнительно запрещаем shell/control символы в origin, который показывается оператору.
            if node.url and not re.fullmatch(r"https?://[A-Za-z0-9.\-:\[\]]+/?", node.url):
                raise ValueError("нужен HTTP(S) origin без пути и специальных символов")
        except (ValueError, ValidationError):
            raise GateError(422, "invalid_request", "Нужен HTTPS-адрес Router без пути. HTTP допускается только на loopback машины Gate.")
        # Один новый клиент; существующие соединения, snapshots и bindings сохраняются.
        with tempfile.TemporaryDirectory() as root:
            candidate_path = Path(root) / "node.json"
            candidate_path.write_text(json.dumps([node.model_dump()]), encoding="utf-8")
            candidate = Nodes(str(candidate_path), channel=state.channel, tokens={token_env: token})
        adopted = False
        try:
            if node.transport == "direct" and not await candidate.probe(node.node_id):
                raise GateError(422, "node_check_failed", candidate.probe_errors[node.node_id])
            if revision(path) != body.revision:
                raise GateError(409, "conflict", "Конфигурация изменилась во время проверки. Повторите добавление.")
            if token:
                secret_path = state.settings.ATLAS_GATE_NODE_ENV_FILE
                secrets = read_keys_file(secret_path)
                secrets[token_env] = token
                # Сначала секрет, затем ссылка на него. Сбой оставляет только неиспользуемый секрет.
                atomic_private(secret_path, "".join(f"{key}={value}\n" for key, value in secrets.items()))
            configs = [n.model_dump() for n in state.nodes.config.values()] + [node.model_dump()]
            atomic_private(path, json.dumps(configs, ensure_ascii=False, indent=2) + "\n")
            state.nodes.config_revision = revision(path)
            state.nodes.config[node.node_id] = node
            state.nodes.clients[node.node_id] = candidate.clients[node.node_id]
            state.nodes.snapshots.update(candidate.snapshots)
            state.nodes.catalog.update(candidate.catalog)
            adopted = True
            logging.getLogger("atlas_gate").info("добавлен Router %s (%s)", node.node_id, node.transport)
            return {"node_id": node.node_id, "revision": revision(path),
                    "status": state.nodes.snapshots.get(node.node_id)}
        finally:
            if not adopted:
                await candidate.close()
