# Atlas Gate

Самостоятельный центральный Gate для Atlas Harness/EDT и нескольких Atlas Router.
Исходные Gate-модули перенесены из собственного проекта `soltyy/atlas-router` v0.6.0;
Claude/Codex SDK, входы и история бесед остаются на Router.

Начальная реализация для проверки разделения; ограничения и задачи:
[IMPLEMENTATION.md](docs/IMPLEMENTATION.md). Эксплуатационную миграцию выполнять
отдельно после приёмки. [Концепция](docs/GATE-ROUTER-CONCEPT.md).

## Установка и локальный запуск

Python >=3.12, uv. `uv sync --locked`, затем `uv run atlas-gate`.
Настройки читаются из `.env`; задайте административный `ATLAS_TOKEN`,
`ATLAS_GATE_KEYRING_SECRET`, пути DB/ключа/организаций и `ATLAS_GATE_NODES_FILE`.
HTTP-админка: `/harness/admin/`; токен нужен и на loopback.
Для реального Router требуется версия с `/v1/node`, `ATLAS_NODE_ID`, собственным
`ATLAS_TOKEN` и `ATLAS_TRUST_LOCAL=false`.

Пример файла одобренных узлов (его не добавлять в Git):

```json
[
  {
    "node_id": "router-a",
    "url": "http://127.0.0.1:8765",
    "token_env": "ROUTER_A_TOKEN",
    "orgs": ["my-org"],
    "routes": ["claude-sub"],
    "account_group": "claude-account-a"
  }
]
```

Секрет endpoint берётся из окружения **процесса** по `token_env`; файл `.env`
настроек автоматически не экспортирует произвольные переменные в окружение.
Допустим отдельный защищённый launcher/env-file, читаемый менеджером службы.
Для HTTPS с частным CA можно указать `ca_file`; проверка сертификата не отключается.
HTTP разрешён только через loopback. Адреса утверждает администратор.

Gate по умолчанию порт 8766, Router — 8765. На одной машине работают две службы,
с независимыми конфигурацией/портами/данными. Можно добавить другие Router на
той же машине или удалённые HTTPS endpoint; клиенты используют прежний URL Gate.
Несколько аккаунтов указываются разными `account_group`; один общий аккаунт — одной группой.

## Разработка

`uv run pytest`; `uv build`. Для сетевых интеграционных тестов задайте
`ATLAS_TEST_ROUTER_ROOT` на checkout Router с `/v1/node` и запустите pytest
из корня Gate интерпретатором тестового venv Router. Его SDK зависимости
используются только тестами, не пакетом Gate. Без переменной сетевые тесты пропускаются.
В `ui`: `npm ci`, `npm test`, `npm run build`.
Тесты и сборки не используют рабочую службу и её SDK credentials.
