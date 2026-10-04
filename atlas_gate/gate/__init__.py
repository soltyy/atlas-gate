"""Самостоятельный Gate: control plane, сетевые Router и прямые API-провайдеры."""

from ..settings import Settings
from . import admin_orgs as _admin_orgs  # noqa: F401 — регистрирует /harness/admin/orgs* (редактор, #6)
from . import agent as _agent  # noqa: F401 — регистрирует /harness/agent/* на общем роутере
from .api import router

__all__ = ["GateConfigError", "check_settings", "router"]


class GateConfigError(RuntimeError):
    """Гейт с опасной конфигурацией не стартует — причина словами в журнале службы."""


def check_settings(settings: Settings) -> None:
    """Отказ старта (тикет #2): гейт и доверие к loopback несовместимы вне разработки."""
    if settings.ATLAS_TRUST_LOCAL and not settings.ATLAS_GATE_ALLOW_TRUST_LOCAL:
        raise GateConfigError(
            "ATLAS_GATE_ENABLED=True несовместим с ATLAS_TRUST_LOCAL=True: гейт открывает роутер наружу через"
            " TLS-прокси, и запрос, пришедший через прокси на этой машине, выглядел бы локальным — /v1/*, "
            "/harness/admin и одобрение регистраций открылись бы без X-Atlas-Token. Задайте ATLAS_TRUST_LOCAL=False"
            " (локальные клиенты, включая EDT, тогда шлют X-Atlas-Token). Только для разработки:"
            " ATLAS_GATE_ALLOW_TRUST_LOCAL=True.")
