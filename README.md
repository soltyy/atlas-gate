# Atlas Gate

Самостоятельный централизованный Gate для Atlas Harness/EDT и нескольких Router.
Claude/Codex SDK, подписочные входы и SDK история находятся на Router. На одной
машине Gate и Router работают на разных портах; удалённые Router доступны через
HTTPS или исходящий NAT connector. Продолжение беседы сохраняет своего владельца.

Python >=3.12, uv: `uv sync --locked`, подготовить защищённый `.env` по
`.env.example`, затем `uv run atlas-gate`. Админка `/harness/admin/` требует
ATLAS_TOKEN, включая loopback. Runtime данные Gate по умолчанию в `gate/`,
в production использовать абсолютные пути и ограниченные ACL.

Router требует NODE_ID, NODE_DB, непустой токен, TRUST_LOCAL=false и выключенный
встроенный Gate. Одобренные узлы в nodes.json определяют org/route/account права.
Account groups разделяют подписочные аккаунты; общий аккаунт нескольких Router
получает одну группу. Новая сессия выбирает готовый узел с доступной ёмкостью;
resume/tools/interrupt/compact/events идут на сохранённый node.

- [Реализация и задачи](docs/IMPLEMENTATION.md)
- [Открытый каталог и настройки харнеса](docs/HARNESS-DISCOVERY.md)
- [Рекон и первичные источники](docs/RECON-STAGES.md)
- [Установка, cohost, mTLS и NAT](docs/DEPLOYMENT.md)
- [Оба продукта на новом сервере](docs/PARALLEL-DEPLOYMENT.md)
- [Миграция и rollback](docs/MIGRATION.md)
- [Один активный Gate / восстановление](docs/HA-ADR.md)
- [Приёмка и ограничения выпуска](docs/ACCEPTANCE.md)
- [Концепция](docs/GATE-ROUTER-CONCEPT.md)
- [Вложения: возможности маршрутов, закрепление и инструменты страниц](docs/ATTACHMENTS.md)

Разработка: `uv run pytest`, `uv build`; в ui — `npm ci`, `npm test`,
`npm run build` (включает typecheck). Для HTTP интеграции задать ATLAS_TEST_ROUTER_ROOT
на отдельный Router checkout и запустить suite его тестовым Python venv.
Без переменной сетевые тесты пропускаются. Production Gate не зависит от Router
Python-пакета или SDK. Реальный Codex тест opt-in ATLAS_LIVE_CODEX=1 использует
существующий ChatGPT вход и подписку; обычные тесты работают с временными БД/stub.

Исправление восстановления квоты: [выпуск 0.2.1](docs/RELEASE-0.2.1.md).

Поиск SDK и оригиналы файлов по умолчанию: [контракт](docs/SDK-WEB-FILES.md), [выпуск 0.2.0](docs/RELEASE-0.2.0.md).
