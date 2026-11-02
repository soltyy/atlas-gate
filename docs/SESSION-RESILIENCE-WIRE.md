# HTTP-карта существующей системы и целевого recovery

Приложение к [RFC](SESSION-RESILIENCE-CONTRACT.md). Исходная база: Gate0.4.19,
Router0.8.18; это индекс проверенного кода, не новый OpenAPI runtime.
Полные wire schemas остаются в source/OpenAPI и профильных документах.

## 1. Вход, каталог и наблюдение

| Направление | Существующий путь | Существенное правило восстановления |
|---|---|---|
| H→G | `POST /harness/enroll/start`, `/harness/enroll/poll` | Новая регистрация требует штатного admin approval; recovery не регистрирует тихо другое устройство |
| H→G | `POST /harness/token/refresh`, `/harness/token/revoke` | Refresh сохраняет разрешённую identity; revoked device не восстанавливается новым токеном в обход политики |
| H→G | `GET /harness/discovery` | Публичный общий каталог не доказывает разрешения конкретного owner/аккаунта |
| H→G | `GET /harness/settings`, `GET /harness/profile` | Подписанный профиль/JWS, ETag/version; cached304 не разрешает выдумать новую capability |
| H→G | `GET /harness/quota`, `GET /harness/llm/v1/models` | Наблюдение разрешённого каталога/квот; не бизнес-генерация и не доказательство admission у поставщика |
| H→G | `POST /harness/audit` | Audit — диагностическое свидетельство клиента, не durable ACK исполнения внешнего эффекта |
| H→G | `GET /harness/health` | Живой Gate не означает healthy SDK/login/history/модель |
| G→R | `GET /v1/node`, `GET /v1/sessions` | Node/boot/backend/capacity и runtime inventory; activeTurns0 не означает отсутствие idle writer/input I/O |
| Admin/R | `GET /v1/auth/status`, `POST /v1/auth/login`, `/v1/auth/login/code`, `/v1/auth/logout` | SDK login выполняет Router; Gate/H используют одобренный административный путь, не извлекают OAuth секреты |

Device calls Gate используют Bearer device token. Совместимый Anthropic SDK может
передавать этот **device token** в `x-api-key`; это не upstream Anthropic API key.
Gate подставляет ключ provider route самостоятельно и не пересылает device secret.
Прямой Router использует собственный доверенный token; node/admin identity не
является универсальным клиентским разрешением на чужие Gate aliases.

## 2. Агентные сессии

Здесь G-root = `/harness/agent/sessions/{publicGateSid}`,
R-root = `/v1/sessions/{localRouterSid}`. Префиксы похожи, IDs **не взаимозаменяемы**.
Create/list используют пути без `/{sid}`. SDK alias — третья отдельная сущность.

| Метод/суффикс | Существующая семантика | Outcome/recovery |
|---|---|---|
| POST sessions | Create/resume, явный client key | Сохранённый success/rejected ACK; unknown сверяется. Create replay — тот же POST/key/body, не GET сессионного receipt |
| GET sessions | Runtime/сохранённые public сведения | Может показать suspended; alias берётся из договорного поля, не из догадки |
| POST `/prompt?mode=async` | Принятие нового turn | 202/turnId — ACK, результат в exact events/receipt |
| POST `/prompt` с SSE | G наблюдает принятый async R producer; direct R имеет legacy cancel-on-disconnect поведение | Не считать consumer reconnect новым prompt. Политика disconnect согласуется явно |
| GET `/turns/{turnId}` | Состояние exact turn | Не создаёт новый logical turn |
| GET `/turns/{turnId}/events?after=N&wait=M` | Seq/cursor страницы, state/done/events | Full text/media могут быть здесь, summary receipt их не заменяет; expiration410 отдельно |
| GET `/turns/{turnId}/receipt` | Сохранённый terminal summary | Не SDK generation. Чтение own closed turn сохраняет ownership checks/usage dedup |
| POST `/tool_result` | Доставка сохранённого результата exact callId | Accepted ACK не означает success задания; инструмент не исполняется этим endpoint заново |
| POST `/interrupt` | Прервать текущий сессионный ход | Legacy target scope ограничен; RFC требует exact turn/generation fencing для позднего Stop |
| POST `/steer` | Новое сообщение в активный turn | accepted/absorbed различаются; неизменный steer ID |
| POST `/tasks/{taskId}/stop` | Stop конкретного subagent/task | Не отменяет уже совершённый эффект; нужны child receipts |
| POST `/tools` | Изменение согласованного каталога | State-changing, key/config snapshot; не простое обновление UI |
| POST `/compact` | Native compaction | Собственный command outcome. Prefer respond-async/receipt; не повторять из-за потерянного HTTP ответа |
| GET `/context` | Контекст текущей SDK сессии | Может требовать runtime/cold restore. Не равен чистому journal read, не разрешение выставить окно вручную |
| DELETE root | Закрыть HTTP/SDK transport | История отдельно. Идемпотентная cleanup semantics не разрешает воскресить старый intent |

R-only `GET /v1/sessions/{sid}/reconcile` читает persisted binding metadata, SDK ID,
busy/replacement evidence. Клиент Gate не получает доступ к нему напрямую.

Сессионный command receipt на Gate:

```text
GET {G-root}/commands/{commandId}
GET {G-root}/commands?requestId=<original-client-key>
POST {G-root}/commands/reconcile
```

Последний POST — существующий `commandRecovery` admission fence **только prompt**,
не универсальный вызов coordinator. Его body/version/hash и исходы описаны в
[ADMISSION-FENCE-V1](ADMISSION-FENCE-V1.md). Request key от create не искать в этих
сессионных endpoints. Router хранит внутренние команды в `GET /v1/commands/{id}`;
Gate вычисляет/связывает собственные ID, клиент не должен угадывать их как native IDs.

`X-Atlas-Command-State: complete` означает durable response, включая отказ;
`X-Atlas-Create-Outcome: rejected` и matching create key относятся к отказу создания.
Для каждого scope проверяются source IDs/body/owner и immutable receipt, не один header.

## 3. Вложения и media

| Суффикс G-root/R-root | Контракт |
|---|---|
| GET `/sdk-tools` | Exact bound capabilities/catalog, не union discovery. Current R может cold-restore SDK |
| GET `/attachments` | Возможности форматов/доставки и ограничения |
| POST/GET `/files`, DELETE `/files/{id}` | Upload original bytes/каталог/отзыв, request key/hash и owner scope |
| POST/GET `/documents`, GET/DELETE `/documents/{id}` | Производные страницы/поиск/текст с авторизацией document IDs |
| GET `/media`, GET `/media/{id}` | Созданные artifacts; durable payload reading отдельно от новой image generation |

Current files/sdk-tools могут требовать transport recovery; это важно отличать от
pure journal receipts. При **direct** доставке Gate запрашивает
`X-Atlas-Input-Recovery: 1` у Router и заменяет internal proof IDs публичными aliases.
В текущем исходящем connector этот request header отфильтровывается; Router тогда
не выдаёт соответствующий proof, а Gate допускает отсутствие proof как legacy.
Нельзя объявлять input-recovery доказанным для connector по одному direct тесту.
Старый Gate получает legacy ответ.
Ни empty-confirmed proof, ни наличие original file не доказывают запуск business prompt.

## 4. Просто API через Gate — отдельный транспорт и ledger

| Существующий путь | Режим |
|---|---|
| `POST /harness/llm/v1/chat/completions` | OpenAI-compatible chat, включая разрешённый DeepSeek route |
| `POST /harness/llm/v1/responses` | Responses-compatible payload; реальные возможности зависят от upstream |
| `POST /harness/llm/v1/messages` | Прямой Anthropic-compatible API, **не** Claude Agent SDK |
| `POST /harness/llm/v1/images/generations` | Отдельная разрешённая API image generation; token pricing v1 не универсальный image tariff |
| `GET /harness/llm/pricing/decision?requestId=...&route_id=...&model=...` | Прочитать свой сохранённый price snapshot; providerOutcome=not_recorded |

Клиент выбирает разрешённую route/model из профиля, сохраняет canonical conversation
и инструментальный ledger. Gate применяет data-class/quota/reasoning/pricing policy
и проксирует upstream. Router в этой цепочке может отсутствовать совсем.

Pricing v1 для соответствующих маршрутов требует `X-Atlas-Pricing-Contract: 1`,
точную `X-Atlas-Pricing-Selection` и устойчивый `Idempotency-Key` либо
`X-Harness-Model-Call`. Configured reasoning/price decisions могут возвращаться
в договорных headers. Эти snapshots фиксируют **настройки**, не provider result.

**В текущем API proxy повтор того же POST даже с тем же key может повторно вызвать
провайдера.** Gateway pricing fingerprint не является inference dedup/result cache.
Нельзя переносить сюда replay гарантии `/harness/agent/sessions/*`.

Provider retrieve/cancel/background/WS пути не возникают автоматически из наличия
POST Responses. Для их объявления нужны adapter-specific endpoints/capability,
owner-bound result ledger, data retention policy и conformance. До этого клиент
сохраняет output сам и рассматривает неизвестный model attempt честно как unknown.
Смена device/root/API credentials также не разрешает использовать чужой response ID.

## 5. Gate↔Router direct/connector и эксплуатация

Direct transport и исходящий connector предназначены для доставки logical команд
на закреплённый node, но **полной wire parity в этой базе нет**. Сертификат/nodeId/bootId и org/route registration
проверяются Gate. Relay lease, HTTP timeout и receipt retention не становятся
ограничением общего времени SDK задачи. Outgoing ACK replay не выполняет SDK вновь.

Проверенные ограничения: request allowlist в Router connector пропускает только
ограниченный набор headers и не включает `X-Atlas-Input-Recovery`; path allowlist
не допускает будущие `/v1/recovery/cases*`. Gate node_channel реконструирует response
без исходных protocol headers, включая `Retry-After`. Сохранность body/ACK не
доказывает сохранность всех рекомендаций retry/capability negotiation.

Целевой transport conformance проверяет разрешённые paths/methods, request/response
headers (proof versions, command/create state, retry timing, configured settings),
status/body/stream framing и ошибки в обоих направлениях. Allowlist расширяется
явно, без произвольного tunnel к admin/чужому URL. Capability вычисляется по
пересечению **Gate–connector–Router**, закрепляется на exact node/boot/transport
version; legacy fallback допустим, но не выдаётся за полное self-healing. При потере
Retry-After из наблюдения нельзя выдумать нулевую паузу или переписать исходный срок.

Документированные процедуры: [Router onboarding](ROUTER-ONBOARDING.md),
[deployment](DEPLOYMENT.md), [HA](HA-ADR.md). Admin drain/reload/enrollment не являются
обычными пользовательскими recovery actions и требуют соответствующих прав.

## 6. Где начинается новый контракт

Все `/harness/recovery/inventories*`, `/harness/recovery/cases*` и `/v1/recovery/cases*` из RFC — **предложение**, их нет
в указанной базе. Это общий orchestration слой над существующими receipts и adapters,
а не переименование старого `/commands/reconcile`. Договорные v1 paths сохраняются.
При реализации OpenAPI, capability, клиент, Router и end-to-end proofs меняются вместе.
