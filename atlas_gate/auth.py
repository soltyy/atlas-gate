"""Гейт доступа: локальному клиенту доверяем, снаружи обязателен заголовок `X-Atlas-Token`.

Решение заказчика 15.09.2026: админка открывается только локально и без токена; токен — для харнеса
и доступа снаружи. Поэтому запрос с loopback-адреса (127.0.0.1/::1) проходит без токена, а любой
другой — только с верным токеном. Флаг `ATLAS_TRUST_LOCAL` выключает доверие (в тестах), тогда даже
loopback ведёт себя как «снаружи».
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Request

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def is_local_request(request: Request) -> bool:
    """Пришёл ли запрос с этой же машины (loopback). Учитывает доверие из настроек.

    Порт-форвардинг снаружи (DNAT) сохраняет исходный IP клиента, а перед uvicorn нет обратного
    прокси — значит `request.client.host` для внешнего обращения НЕ loopback, и токен обязателен.
    """
    settings = request.app.state.settings
    if not getattr(settings, "ATLAS_TRUST_LOCAL", True):
        return False
    client = request.client
    if client is None:
        return False
    host = client.host or ""
    if host.startswith("::ffff:"):  # IPv4, отображённый в IPv6
        host = host[len("::ffff:"):]
    return host in _LOOPBACK


async def require_token(
    request: Request,
    x_atlas_token: str | None = Header(default=None, alias="X-Atlas-Token"),
) -> None:
    if is_local_request(request):
        return  # локальная машина — без токена (админка и локальный харнес)
    expected: str = request.app.state.settings.ATLAS_TOKEN
    if not expected:
        return  # ATLAS_TOKEN пуст — проверки нет
    if x_atlas_token is None or not hmac.compare_digest(x_atlas_token.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="нет или неверен заголовок X-Atlas-Token")


async def local_only(request: Request) -> None:
    """Страница админки и её ассеты доступны только локально; снаружи — 403."""
    if not is_local_request(request):
        raise HTTPException(status_code=403, detail="админка доступна только локально")
