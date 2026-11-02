# Gate 0.4.20 — coordinator и managed API attempts

**Кандидат, не поставлен.** Состояние документа на 03.11.2026. Gate #88 связан
с Router #104 / кандидатом Router 0.8.19. Версия пакета и опубликованные wire
fixtures не являются доказательством установки или installed SDK qualification.

## Серверный контракт

Подписанный профиль объявляет `sessionRecoveryCoordinator.version=1`. Inventory
фиксирует полный список pending sources ветви до создания отдельных cases; seal
закрывает барьер регистрации. Cases принадлежат одному старому Run и одной
managed session/API attempt. Общие branch generation/control epoch не позволяют
разным окнам получить независимые права на продолжение.

Coordinator читает реальные command/turn receipts и доступные events, выдаёт
план локального применения ACK/terminal. Client-applied ACK хранится как
исторический факт и принимается после revision advance/Stop без повторения
локального commit. Partial ACK не закрывает остальные action kinds. Pending apply
повторяется с прежним key и **неизменным body**; unknown не обходится новым key.

SDK-free Gate делегирует только конкретные разрешённые native transport actions
на прежний node/boot. Возобновление транспорта не отправляет prompt/tool_result.
Для Router 0.8.19 cold resume ограничен Codex/stub. У Claude
`historyOnlyRestore=false` / `noActionRestoreQualified=false`; сервер не обещает
безопасный холодный resume по одному сохранённому session ID.

## API и connector

Отдельная capability `apiAttemptRecovery.version=1` требует исходного opt-in
`X-Atlas-API-Attempt-Contract: 1` и стабильного request key. Gate сохраняет producer,
dispatch marker, raw response chunks, result hash, summary и usage. Disconnect
consumer не создаёт второй inference. Lookup/result читают прежний attempt;
API reconcile атомарно запечатывает только отсутствующий/доказанно prepared
managed запрос. Lookup 404 не является negative proof.

Legacy API proxy без opt-in сохраняет прежнее поведение: одинаковый key и pricing
snapshot не гарантируют inference dedup. Provider retrieval/cancel и автоматический
paid retry не объявлены. SDK/API raw frames, `terminalKnown` и provider `completed`
не являются разрешением исполнять host tools или доказательством бизнес-успеха.

Connector protocol 2 передаёт recovery paths, input proof и разрешённые response
headers, включая исходный Retry-After. Новые recovery work не выдаются старому
worker. `model.sessionRecovery.nodes` отражает пересечение Gate–connector–Router;
`exactPinnedPath` не означает resume на любом узле discovery.

## Границы и поставка

Harness #504 / EDT #222 остаются отдельной поставкой: Gate не меняет клиентскую
SQLite и не исправляет local binding guard без manifest/локального commit/ACK.
После Stop/cancel разрешены чтение, reconciliation и исторический ACK, без
возобновления отменённого intent. Legacy interrupt остаётся session-scoped.
Terminal continuation Gate #77 / Router #96 — **unsupported**. Не реализованы
автоматическая cross-account/node/device migration и произвольный effect recovery.

Согласованная резервная копия сохраняет новые ledgers/fences и приватные API
results. Rollback на код без managed API contract не должен открывать opt-in POST
как legacy inference; deploy/maintenance guard сохраняет блокировку до совместимого
runtime. Активные producer/input I/O/native close учитываются при drain; один
`activeTurns=0` недостаточен для остановки процесса.

Wire и точные схемы: [HTTP-карта](SESSION-RESILIENCE-WIRE.md),
[примеры и canonical vectors](fixtures/session-recovery-v1.json),
[OpenAPI кандидата](fixtures/session-recovery-openapi.json). Проверки кандидата,
required release guard и фактическая поставка фиксируются отдельно; эта заметка
не объявляет всю матрицу RFC или старую клиентскую беседу восстановленными.
