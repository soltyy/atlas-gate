"""Настройки службы: переменные окружения `ATLAS_*` и файл `.env` в рабочем каталоге."""

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    """Все настройки читаются один раз при создании приложения (`create_app`).

    Значение попадает в процесс только при старте — сверять «с чем поднято», а не с файлом.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ATLAS_HOST: str = "127.0.0.1"
    ATLAS_PORT: int = 8766
    # Пусто — проверки заголовка X-Atlas-Token нет. Непусто — заголовок обязателен на /health и /v1/*
    # при обращении СНАРУЖИ. Токен — для харнеса и внешнего доступа, не для локальной админки.
    ATLAS_TOKEN: str = ""
    # Локальному клиенту (127.0.0.1/::1) доверяем: админка открывается без токена, а сама админка
    # (страница и её ассеты) отдаётся ТОЛЬКО локально. Решение заказчика 15.09.2026: «админка должна
    # открываться только локально и без токена — токен это для другого». Выключается в тестах, чтобы
    # loopback вёл себя как «снаружи» и проверял и гейт токена, и запрет админки извне.
    ATLAS_TRUST_LOCAL: bool = False
    # --- Гейт для Atlas Harness (GW-HARNESS-02, тикет atlas-router#1) ------------------------------
    # Выключен — `/harness/*` не подключаются, БД не открывается, ключ подписи не создаётся,
    # апстримы не вызываются: роутер для харнеса EDT ровно тот же (снимок OpenAPI в tests/snapshots).
    ATLAS_GATE_ENABLED: bool = True
    # Гейт открывает роутер наружу (через TLS-прокси), а доверие к loopback пускает любой запрос с
    # 127.0.0.1 без токена — прокси на той же машине превратил бы внешний запрос в «локальный»
    # (тикет #2). Поэтому гейт с ATLAS_TRUST_LOCAL=True не стартует; исключение — только разработка.
    ATLAS_GATE_ALLOW_TRUST_LOCAL: bool = False
    # SQLite гейта: регистрации, устройства, токены (хэшами), аудит, учёт, подписанные профили.
    ATLAS_GATE_DB: str = "gate.db"
    # Внешний адрес гейта за TLS-прокси: из него собираются `routes[].base_url` профиля и
    # `verification_url`. Пусто — адрес текущего запроса (годится только для разработки).
    ATLAS_GATE_PUBLIC_URL: str = ""
    # Файл ключей провайдеров `ИМЯ=значение` (имена — routes[].key_env организации). Перечитывается при
    # старте и POST /harness/admin/reload: новый ключ поднимает маршрут без перезапуска. Значения не логируются.
    ATLAS_GATE_KEYS_FILE: str = "gate/.env.gate"
    # Заголовок anthropic-version для маршрутов protocol=anthropic, если клиент его не прислал.
    ATLAS_GATE_ANTHROPIC_VERSION: str = "2023-06-01"
    # Секрет шифрования device_secret в БД (Fernet от sha256 секрета).
    ATLAS_GATE_KEYRING_SECRET: str = ""
    # Приватный ключ Ed25519 подписи профиля; создаётся при первом старте с включённым гейтом.
    ATLAS_GATE_KEY_PATH: str = "gate/signing-key.pem"
    # Каталог конфигураций организаций `<org>.json`.
    ATLAS_GATE_ORGS_DIR: str = "gate/orgs"
    # Тестовый контур: организация `test-org` из поставки (scripted-апстримы из ${ATLAS_TEST_*_UPSTREAM} + stub-агент).
    ATLAS_GATE_TEST: bool = False
    # Одновременных агентных сессий (`/harness/agent/*`) на устройство.
    ATLAS_GATE_AGENT_SESSIONS_PER_DEVICE: int = Field(default=0, ge=0)
    # Срок device_token (не больше суток) и refresh_token.
    ATLAS_GATE_DEVICE_TOKEN_TTL_SEC: int = 86400
    ATLAS_GATE_REFRESH_TOKEN_TTL_SEC: int = 30 * 86400

    # Список одобренных endpoint: ключи берутся из env по имени, не из org/profile.
    ATLAS_GATE_NODES_FILE: str = "gate/nodes.json"
    ATLAS_GATE_NODE_ENV_FILE: str = "gate/node-secrets.env"
    ATLAS_GATE_NODE_PKI_DIR: str = "gate/node-pki"
    ATLAS_GATE_NODE_TLS_PORT: int = 0
    ATLAS_GATE_NODE_TLS_CERT: str = ""
    ATLAS_GATE_NODE_TLS_KEY: str = ""
    ATLAS_GATE_NODE_TLS_CA: str = ""
