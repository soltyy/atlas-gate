# Media: установленная приёмка Router #72 / EDT #207

Серверная приёмка: **20.10.2026 14:00 МСК**. Gate и оба подключённых Router обновлены.
Выпущен **EDT 1.0.0.202610201358**, [PR #208](https://github.com/soltyy/edt-llm-agent-extension/pull/208),
merge `645b48090cfa47267b6557108fd989ac3f14ada5`. [Проверенный ZIP](https://github.com/soltyy/edt-llm-agent-extension/raw/645b48090cfa47267b6557108fd989ac3f14ada5/release/ay-agents-latest.zip).
EDT на другой машине автоматически не обновлялась.

## Поставка

- Router **0.8.5**, source `d4b5875fd7f16054b8f6acc92ad43becf82a7f11`,
  merge `10994eda1e699c03f088c57d19e5888f6ee41f9f`, [PR #73](https://github.com/soltyy/atlas-router/pull/73).
  Включены media #69, compact #71 и history #67.
- Gate **0.4.8**, source `1449d25b099f1aaca4c351138632df3edd18fd35`,
  merge `b84100f3422b60d1c1b5288249902526a50856a8`, [PR #63](https://github.com/soltyy/atlas-gate/pull/63).
  Включены media #59 и retry/receipts #61/#62.
- Wheel SHA256 Router: `f38b50b02354d06c7b823dac233503082eb31b6bedcaf0b5fdfe8cde53db497b`.
- Wheel SHA256 Gate: `db6540f4e9663cadef92f95fdb46504786eb84f49d966b769977409de45bbf9f`.
- Runtime: `/opt/atlas/releases/router-0.8.5-media72` и `/opt/atlas/releases/gate-0.4.8-media72`.
  Проверены все 46 Router / 37 Gate runtime-файлов установленного wheel.

## Проверки

| Проверка | Результат |
|---|---|
| Router full Linux, временные DB/keys/порты, stub/scenario SDK | **354 passed, 38 skipped** |
| Gate full Linux с настоящим HTTP Router | **169 passed, 6 skipped** |
| Gate full Windows | 169 passed, 6 skipped |
| Router full Windows | 354 passed, 37 skipped, 1 ReadTimeout реального CLI auth probe в /health; Linux HTTP/auth и установленная приёмка проходят |
| Router media/legacy/compact targeted | 15 passed |
| Gate UI | 49 passed; build passed; generated-only файлы восстановлены к committed bytes |
| Router static | Имена и SHA256 совпали под pinned Node 22.23.3 |
| Installed deployment_check | 8 synthetic children / положительный предел — PASS; отдельная проверка от live SDK |
| Публичный HTTPS Gate → Codex | Генерация, media event, GET PNG, SHA256, каталог и replay — PASS |
| Прежняя SDK-история с новым согласованием | Legacy без imageGeneration → текстовый marker → close idle → новый key/body + прежний SDK alias + allowHistoryFallback=false → resumed=true/точный ACK/replay → image; marker сохранён |
| Исходная пользовательская картинка | PNG 1254×1254, 2261206 байт восстановлен из собственной истории; размер/MIME/хеш/повторный каталог проверены, **без inference** |
| QA PNG | 1536×1024, 859604 байт, одна нативная генерация; tool_result клиента не нужен |
| Изоляция | QA-устройство не читает исходную чужую Gate-сессию; неизвестный session/artifact не выдаётся |
| Cold restart всех трёх служб | Прежний и новый PNG доступны с прежними хешами; только GET, без повторной генерации |
| Сохранение retry60 на двух SDK | Create/replay, originals upload/hash, receipts по key/ID, duplicate/conflict, data-class, close и saved receipt — PASS без inference |

Linux-сценарный RPC исправлен для контекста originals: команда определяется по
первой строке, полный вход сохраняется. Lossless transport явно отключает
дополнительные SDK tools. Производственные таймауты не ослаблялись ради тестов.
Первичные ошибки переноса QA fixtures/прав доступа исправлены в отдельном стенде;
таблица содержит окончательные прогоны.

## Состояние и резервная копия

Перед каждым переключением узлы drained/idle: activeTurns=0, activeSubagents=0.
SQLite backup API с учётом WAL, конфигурации, SDK-профили, оригиналы и документы
сохранены в приватной копии `/var/backups/atlas/media72-20261020T105043Z`. Настройки `/etc/atlas/*.env` и signing
key после поставки побайтово прежние. Исходные session/node/SDK-tools поля старой
привязки сохранены. SDK homes, документы и постоянные DB не переносились.

После контрольного перезапуска оба узла ready=true, draining=false; три службы
active. Все QA-сессии закрыты, устройства отозваны, временный device token удалён.
Исходная пользовательская беседа не получала новых prompt/генераций.

Код можно откатить на `router-0.8.4-compact70` и `gate-0.4.7-retry60` после drain
и проверки idle. Новые пользовательские данные/БД поверх новых событий не откатываются.
Приватные отчёты: `/var/lib/atlas-gate/media72-deployment.json`,
`media72-acceptance.json`, `media72-receipts-acceptance.json`. Они не включены в Git.

## Границы

У Codex capability imageGeneration=true, billing=subscription. У Claude
capability=false: отдельный платный Images API не настроен. Реальный платный API
не проверялся; сценарные transport/error/reference проверки не заменяют его.
Harness UI остаётся отдельной задачей #324. Удалённая установленная EDT со
скриншота не наблюдалась; клиентская автоматизированная приёмка и релиз подтверждены
в [EDT #207](https://github.com/soltyy/edt-llm-agent-extension/issues/207#issuecomment-6289620432).

## Клиентская приёмка и опубликованный ZIP

Продуктовый source `ae1ee0901e27f2c127d1a2e26c19117fea618a44`; release commit
`b83af5ae49285858521233fed5f1c4a222b87e10`. Полный `clean verify` в чистом клоне:
**3409 tests, 0 failures/errors, 6 skips**, 9:19. Пять пропусков — opt-in
проверки провайдеров/сети/SDK originals, один — кадр/follow WebView2.
Targeted migration/media/reuse/compact: **49 passed**, без пропусков.
После упаковки: p2 audit **2 passed**, SWT **55 tests, 0 failures/errors,
2 skips** (живая генерация и кадр WebView2). Closure, CRC, восемь бандлов,
десять собственных p2 units/feature и семь закреплённых JAR проверены.

ZIP **30176388 байт**, SHA256
`970b6ca601356f636d722e8a023c70677b743256e1e952813422e44c4549a309`.
Опубликованный Git blob побайтно совпадает с проверенным ZIP на рабочем столе;
дополнительно сверены Git blob SHA1/размер по GitHub API и локальный SHA256.
Канонический ZIP и основная рабочая копия EDT обновлены с сохранением чужого WIP.
Клиент получает старый каталог без прежнего флага генерации; безопасная миграция
сохраняет старый key/body, прежние webSearch/files и SDK thread, не принимает
пустую историю и возобновляется после lost ACK/сбоя записи. Незагруженный PNG
может получить новую подтверждённую HTTP-привязку без смены своего artifact ID.
