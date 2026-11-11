# HTTP-карта recovery: Gate 0.4.25 / Router 0.8.23

Приложение к [RFC](SESSION-RESILIENCE-CONTRACT.md), актуализация 04.11.2026.
Текущая поставка Gate **0.4.25** / оба Router **0.8.23** и клиентские границы —
в [матрице статуса](SESSION-RESILIENCE-STATUS.md). Ниже сохраняется описание
базовой реализации coordinator v1, дополненной без изменения прежних wire схем.
Исходная база RFC — Gate **0.4.19** / Router **0.8.18**. Ниже отдельно описан
первоначально поставленный coordinator **Gate 0.4.20 / оба Router 0.8.19**. Guard,
native SDK web и HTTPS postflight завершены; [квитанция поставки](https://github.com/soltyy/atlas-gate/issues/88#issuecomment-6590421898).
Это подмножество RFC: ограничения квалификации Claude cold restore, continuation и клиентской
локальной транзакции перечислены ниже. Версия или fixture сами по себе не доказывают
поддержку другого развёрнутого узла: клиент проверяет подписанный профиль и exact case.

Точные request schemas: [OpenAPI wire v1](fixtures/session-recovery-openapi.json).
Примеры, capability, headers и canonical JSON vectors:
[session-recovery-v1.json](fixtures/session-recovery-v1.json). В опубликованном
снимке `6740fca` зафиксированы 12 базовых recovery/API-attempt путей и 18 схем.
Снимок не является полным OpenAPI последующих additive возможностей Gate25.
Остальные v1 paths сохраняются в source/OpenAPI и профильных документах.

Gate #88 / Router #104 реализуют серверную часть. Harness #504 / EDT #222 —
отдельная клиентская проводка inventory, локального commit и ACK. Сервер не
изменяет SQLite клиента и не снимает его local binding guard без обращения клиента.

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

### Результаты инструментов в активном ходе

Подписанный профиль объявляет `toolResultRecovery.version=1` с путями статуса и
именем заголовка повторной доставки. Для Router требуется `tool_result_recovery_v1`.
Gate возвращает `toolCallbacks` в `GET {G-root}/turns/{turnId}`, когда Router подтвердил
исходный runtime. Поля `sessionId`, `turnId`, `nodeId`, `bootId` и `seq` задают
область снимка. `pendingCallIds` содержит только вызовы, которые ждут ответ и
в журнале активного хода, и в SDK. Пустой список подтверждает отсутствие ожидающих
вызовов в этом снимке. Отсутствие всего объекта означает отсутствие такого доказательства.

Для ещё не заявленного вызова клиент сверяет точное событие `tool_call` с журналом
и указанным снимком, затем сохраняет локальное намерение по `callId` до исполнения.
Если намерение или результат уже сохранены, применяется его прежний исход.
Позиция чтения берётся из последнего применённого события. `seq` снимка статуса
указывает серверную позицию и не продвигает локальный курсор сам по себе.

`GET {G-root}/turns/{turnId}/tool-result-recovery?callId=…&bodySha256=…`
сверяет исходную операцию с каноническим хешем сохранённого JSON. Ответ содержит
`sessionId`, `turnId`, `callId`, `requestId`, `commandId` и `bodySha256`.

- `outcome=accepted` означает сохранённую квитанцию Router для результата.
- `outcome=closed` означает подтверждённый отказ исходной доставки с `turn_not_waiting`.
- `outcome=unknown` означает недостаток доказательств. Повтор допустим только с
  `retryAllowed=true` для подтверждённого ожидающего вызова на прежнем boot.
  Отсутствие квитанции без подтверждения callback оставляет `retryAllowed=false`.

Для повторной доставки клиент посылает прежнее сохранённое тело в `POST {G-root}/tool_result`
с `Idempotency-Key: tool-result:{sessionId}:{callId}` и
`X-Atlas-Tool-Result-Turn: {turnId}`. Gate сверяет владельца, исходный ключ и тело.
Router сверяет тот же callback перед доставкой и использует дисковую дедупликацию.
На внутреннем HTTP/connector-канале Gate запрашивает снимок с `toolResultRecovery=1`
и передаёт исходный ход для защищённой доставки параметром `toolResultTurn`.
Обычный статус Router сохраняет прежнюю схему.
Если Stop или смена хода успели изменить ожидание, Router возвращает
`409 recovery_unconfirmed`. Итог хода и причина отказа провайдера по-прежнему
читаются по exact terminal receipt. Оценка окна SDK и compact имеют свои события
и не определяют исход доставки результата.

## 3. Вложения и media

| Суффикс G-root/R-root | Контракт |
|---|---|
| GET `/sdk-tools` | Exact bound capabilities/catalog, не union discovery. Current R может cold-restore SDK |
| GET `/attachments` | Возможности форматов/доставки и ограничения |
| POST/GET `/files`, DELETE `/files/{id}` | Upload original bytes/каталог/отзыв, request key/hash и owner scope |
| POST/GET `/documents`, GET/DELETE `/documents/{id}` | Производные страницы/поиск/текст с авторизацией document IDs |
| GET `/media`, GET `/media/{id}` | Созданные artifacts; durable payload reading отдельно от новой image generation |

Files/sdk-tools могут требовать transport recovery; это отличается от pure journal
receipts. Gate запрашивает `X-Atlas-Input-Recovery: 1` у Router и заменяет internal
proof IDs публичными aliases. В базе G19/R18 connector отбрасывал этот header.
Кандидат connector protocol 2 передаёт его; старый worker по-прежнему может вернуть
legacy ответ без proof. Отсутствие proof не превращается в empty-confirmed.
Холодное восстановление через эти пути зависит от backend: для Claude безопасный
history-only restore в R19 не квалифицирован, `historyOnlyRestore=false`.
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

**Legacy API proxy без `X-Atlas-API-Attempt-Contract: 1` сохраняет прежнее поведение:**
повтор POST даже с тем же key может повторно вызвать провайдера. Pricing fingerprint
не является inference dedup/result cache; гарантии managed sessions сюда не переносятся.

### Managed API attempts в кандидате G20

Подписанный профиль объявляет отдельный `apiAttemptRecovery.version=1`. Клиент
согласует contract **до исходной отправки**: `X-Atlas-API-Attempt-Contract: 1` и
неизменный `Idempotency-Key` (допускается прежний `X-Harness-Model-Call`). Для
ветви передаются `X-Atlas-Conversation-Id` / `X-Atlas-Branch-Id`; после recovery —
также три admission fence headers из §6. Выбранный choice задаётся
`X-Atlas-Choice-Index`, по умолчанию 0, и входит в identity запроса.

Gate хранит исходный body hash, producer/dispatch state, приватные result chunks,
terminal summary, result hash и usage receipt. Один producer переживает disconnect
consumer. Повтор того же opt-in POST получает сохранённый результат либо состояние
того же attempt; changed body/target/branch — conflict. Unknown не создаёт второй
provider attempt. Это сохранённые байты Gate, а не upstream retrieve/cancel.

| Кандидатный путь | Точная роль |
|---|---|
| `GET /harness/llm/attempts/{attempt_id}` | Owner-bound receipt/state, без inference |
| `GET /harness/llm/attempts/{attempt_id}/result` | Сохранённые raw bytes, в том числе доступный partial при unknown; читать state и summary отдельно |
| `GET /harness/llm/attempts/lookup` | Query: `requestId`, **`route_id`**, `model`, `bodySha256`; поиск своего исходного key, без новой отправки |
| `POST /harness/llm/attempts/reconcile` | Atomic admission fence только contract 1; observe существующего либо seal отсутствующего/доказанно prepared запроса |

Body reconcile: `version:1`, `requestId`, **`routeId`**, `model`, `endpoint`,
`bodySha256`; `conversationId` / `branchId` необязательны. Endpoint — один из
`/chat/completions`, `/responses`, `/messages`, `/images/generations`.
Header `X-Atlas-API-Attempt-Contract: 1` обязателен. Здесь `requestId` — старый
API key, а не новый key recovery case. Lookup 404 возвращает unknown, **не** not-sent.

Negative proof имеет `scope=managed_api_admission_fence`, `notSentProven=true`,
`providerDispatch=false` и exact key/body/route/model/endpoint. Prepared attempt
запечатывается под прежним attempt ID; running/unknown/complete не выдаются за
неотправленные. Поздний исходный managed POST получает сохранённый отказ. Этот
fence **не защищает legacy API POST**, для которого contract 1 не был согласован.

`terminalKnown`, provider outcome, `runState`, `toolCallsComplete` и usage certainty
различаются. HTTP 200, Responses `completed` или сохранённые SDK/API raw frames
не дают host-execution permit. `hostExecutionAllowed=false`: исполнитель клиента
проверяет завершённость/schema/call ID, текущие права и собственный effect ledger.
Failed/incomplete/refused/paused/tool_calls не выдаются за проверенный успех задачи.
В coordinator API source имеет `kind=api_attempt`; ACK этого source использует
`sourceAttemptId`, а не SDK `sourceSessionId`.

Provider retrieve/cancel/background/WS пути не возникают автоматически из наличия
POST Responses. Для их объявления нужны adapter-specific endpoints/capability,
owner-bound result ledger, data retention policy и conformance. До этого клиент
сохраняет output сам и рассматривает неизвестный model attempt честно как unknown.
Смена device/root/API credentials также не разрешает использовать чужой response ID.

## 5. Gate↔Router direct/connector и эксплуатация

Direct transport и исходящий connector доставляют logical команды на закреплённый
node. Сертификат/nodeId/bootId и org/route registration проверяются Gate.
Relay lease, HTTP timeout и receipt retention не становятся
ограничением общего времени SDK задачи. Outgoing ACK replay не выполняет SDK вновь.

В G20/R19 protocol 2 расширяет allowlist для `/v1/recovery/cases*`, передаёт input
proof header и сохраняет разрешённые response headers: `Retry-After`, command/create
state, configured reasoning и transport contract. Auth/cookies и произвольные headers
не туннелируются. Gate сохраняет headers вместе с relay response; повтор не сдвигает
первоначальный Retry-After. Новые recovery work требуют protocol 2 и не выдаются
legacy worker; это не утверждение о полной поддержке новых функций старым connector.

Capability — пересечение **Gate–connector–Router**. В подписанном профиле
`model.sessionRecovery={version:1,scope:pinned_node,nodes:{...}}`; каждый node имеет
свои proof capabilities и `transportSupported`. `exactPinnedPath=true` у coordinator
означает проверку конкретного node/boot/transport на действии, а не resume на любом
узле из общего discovery. При потере рекомендации retry нельзя выдумывать нулевую паузу.

Документированные процедуры: [Router onboarding](ROUTER-ONBOARDING.md),
[deployment](DEPLOYMENT.md), [HA](HA-ADR.md). Admin drain/reload/enrollment не являются
обычными пользовательскими recovery actions и требуют соответствующих прав.

Drain учитывает также `nativeClosingUnits`, `nativePendingUnits`,
`unsettledCloseUnits` и входящий I/O, включая cold GET/context. Lease ставится до
первого await SDK open. Незарегистрированный transport с неопределённым close
удерживает резерв процесса. Эти counters служат наблюдению/guard, не являются
публичной model capability или durable доказательством освобождения SDK. После
crash/restart пустой registry и нулевые counters требуют сверки ownership/history,
а не означают, что прежний writer исчез.

## 6. Coordinator v1 в коде кандидата G20

Подписанная capability — `sessionRecoveryCoordinator.version=1`, protocol
`session-recovery-coordinator-v1`. Это отдельный слой над прежними receipts,
не переименование `commandRecovery` и не автоматическое обновление старого клиента.

| Метод/путь | Результат |
|---|---|
| `POST /harness/recovery/inventories` | Durable snapshot всех pending sources ветви и закрытие нового admission |
| `POST /harness/recovery/inventories/{inventory_id}/seal` | Барьер регистрации всех sources; сам по себе не разрешает prompt |
| `POST /harness/recovery/cases` | Один immutable manifest на один source Run и binding/attempt |
| `GET /harness/recovery/cases/{case_id}` | Текущий snapshot, evidence и сохранённый plan; без SDK/model dispatch |
| `POST .../{case_id}/evidence` | Append-only клиентский claim, не техническое доказательство исполнения |
| `POST .../{case_id}/apply` | Выполнить разрешённые server action IDs сохранённого плана под CAS/fence |
| `POST .../{case_id}/ack` | Зафиксировать локальное применение immutable client actions после commit |
| `POST .../{case_id}/cancel` | Прекратить auto recovery, поднять control epoch и запретить ещё не отправленные шаги |

Inventory body содержит `version`, `conversationId`, `branchId`, `clientStoreEpoch`,
`checkpointInventoryVersion`, `sources`. Source reference: `sourceRef`, `sourceRunId`
и либо `kind=managed_session` + `sessionId`, либо `kind=api_attempt` + `attemptId`.
Cases связываются через `inventoryId/sourceRef`. Несколько sources — несколько cases
под одним branch generation/control epoch. Unresolved source нельзя потерять при
смене snapshot; завершение первого case до seal не открывает ветвь.

Create/seal/evidence/apply/cancel используют устойчивый `Idempotency-Key` и неизменный
body. ACK использует `ackKey` в body. **Pending apply повторяется с тем же key и тем
же body**, включая старые `planId`, `expectedRevision`, source hash, `branchFence`
и `actionIds`. Нельзя заменить pending body данными свежего snapshot. Принятый шаг
сверяется по своему ledger; новый plan/action/key нужен после доказанного отказа,
а не для обхода unknown. `apply` не принимает новый SDK payload, prompt или tool body.

ACK содержит `planId`, **`planRevision`**, `sourceManifestSha256`, `clientStoreEpoch`,
`sourceRunId`, один из `sourceSessionId/sourceAttemptId`, `sourceCheckpointVersion`,
`appliedActions[{actionId,evidenceSha256}]`, `localCommitRef/localCommitSha256`.
Он не требует текущего `branchFence/expectedRevision`: первый поздний ACK принимается
как historical-applied после продвижения revision/Stop. Неизменные descriptors
проверяются; partial ACK не закрывает остальные обязательные действия. Равный hash
у API receipt и checkpoint resolution не делает их одним action kind.

После регистрации recovery ветви новый prompt/managed API dispatch предъявляет
`X-Atlas-Recovery-Inventory`, `X-Atlas-Recovery-Generation`, `X-Atlas-Control-Epoch`.
Для новой binding/API запроса передаются также `X-Atlas-Conversation-Id` и
`X-Atlas-Branch-Id`. Legacy ветви без регистрации продолжают прежний протокол;
старое окно зарегистрированной ветви без актуального fence получает recovery_required.

Stop/cancel не разрешает продолжение отменённого intent. После него остаются
чтение/сверка receipts и ACK уже применённых фактов; historical ACK не открывает
новый transport/model dispatch. Cancel case не подменяет SDK interrupt. Старый
`/interrupt` остаётся session-scoped; target-aware late Stop не объявляется
реализованным этим выпуском.

Canonical body — прежний `json-sort-keys-compact-utf8-v1`, с Python float formatting
и сортировкой Unicode code points, без добавления defaults. Это не RFC8785/JCS.
Evidence предоставляет `canonicalPayloadUTF8`: hash можно проверить по точным UTF-8
bytes этой строки. Hash не заменяет проверку source tuple, action kind и полномочий.

## 7. Router proof adapter и пределы кандидата

При включённом durable journal R19 реализует `POST /v1/recovery/cases`,
`GET /v1/recovery/cases/{case_id}`, `POST .../{case_id}/apply`.
Create body: `version`, стабильный `id`, локальный `sessionId`, optional `turnId`,
`expectedBootId`, `sourceManifestSha256`, `generation`, `controlEpoch`.
Apply body: `version`, `actionId`, `expectedRevision`, `generation`, `controlEpoch`.
SDK IDs остаются Router/Gate-внутренними; публичная SDK alias — другой namespace.

Read/create proof не открывают SDK. History `recorded` означает journal mapping,
а `confirmed` — соответствующий живой runtime; это не универсальная проверка всего
transcript. Lifecycle change делает старый case stale и не разрешает его старый action.
Native шаги ограничены `resume_transport` / `release_idle_transport` для доказанно
собственного idle transport, с проверкой pending turns/tools/children/compact/input I/O.
Cold resume объявляется по квалифицированному pinned adapter. В первоначальном
R19 Claude `historyOnlyRestore=false` / `noActionRestoreQualified=false`; эта
граница обновлена последующей [поставкой Claude cold restore](CLAUDE-COLD-RESTORE.md),
сохранённой в Router23. Конкретный case и текущая конфигурация по-прежнему могут
дать unsupported; наличие origin/drain/reset accounting само по себе не proof.

`sdk_writer_busy` и локальный pre-SDK `capacity_exceeded` могут иметь точный
`rejected_not_sent`, `scope=transport_only`, `retryable=true`. Это отказ transport
шага, не отрицательное доказательство business prompt. Остальные неизвестные native
исходы не очищаются по HTTP status. Complete resume допускает подтверждение точного
postcondition без SDK RPC; отсутствие registry entry не доказывает завершённый close.

Gate #77 / Router #96 terminal continuation остаётся **unsupported**. Ни native
resume, ни SDK/API raw frames, ни accepted tool ACK не дают разрешения повторить
prompt/инструмент или объявить бизнес-задачу выполненной. Прямой Router клиент имеет
node-token контракт, прямой API без Gate — собственный клиентский ledger; coordinator
Gate не появляется в этих топологиях автоматически. Cross-account/node/device
migration, восстановление утраченной SQLite клиента и произвольные внешние effects
этим серверным кандидатом не реализованы.

## 8. Additive возможности Gate24/25

- [Auth recovery v1](AUTH-RECOVERY97.md): отдельные versioned paths для сохранения
  прежнего устройства после потери входа. Legacy refresh/enroll не меняют смысл.
- [Source history identity v1](SOURCE-HISTORY-IDENTITY103.md): подписанный
  `sessionRecoveryCoordinator.sourceHistoryIdentity`, GET
  `/harness/agent/sessions/{sid}/history-identity?previousAlias=<opaque-alias>`.
  Только local read и проверка полномочий; `same_native_history` доказывает
  resume continuity, не outcome и не право повторить prompt. Клиент применяет
  узкий CAS с аудитом и затем новую inventory/version/key.
- [Подтверждённый отказ prompt](RETRY-RECEIPTS.md): полный admission fence после
  доказанного собственного pre-dispatch отказа; arbitrary503 не становится proof.
- [Steering v1](STEERING-V1.md): уже поставленный Gate23/Router23 контракт
  exact-turn accepted/absorbed. Он не является terminal continuation.

Для прямых Router/API клиентов эти Gate additions не появляются автоматически.
Старые clients не обязаны вызывать новые paths; новые используют их только по
подписанным capabilities и сохраняют безопасный fallback/typed unsupported.
