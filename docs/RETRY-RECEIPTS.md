# Повторы и квитанции Gate/Router

Gate #60; клиентская реализация — Harness #328/#329, UI #305.
Контракт ниже относится к Gate 0.4.7 и Router с durable_commands/durable_events.
Наличие обновлённого сервера не включает повторы в ещё не обновлённом клиенте.

## Идентичность и результат

Изменяющий запрос получает стабильный клиентский `Idempotency-Key` ДО отправки.
Для сессионной команды Gate вычисляет commandId = SHA256(device_id + ':' + key).
Fingerprint включает HTTP method, локальный путь закреплённой сессии и JSON body.
Тот же ключ/параметры воспроизводят исход; другой payload/session — 409
idempotency_conflict. Время повторов не меняет key, turnId, callId, владельца,
узел или модель. Новый логический запрос получает новый key.

Полученный ACK команды и завершение SDK-хода — разные факты. Например, 202
prompt подтверждает принятие хода, а конечный результат читается в events/receipt
хода. 200 tool_result подтверждает доставку результата, а не успех всего задания.

## Матрица

Все сессионные пути ниже под `/harness/agent/sessions/{publicGateSessionId}`.
SDK resume alias не является HTTP ID. Полномочия device/org, node/route и
data-class проверяются и при повторе, и при чтении квитанции. Чтение/отмена своей
операции не требует повторного разрешения отозванной SDK-функции/модели, но не
обходит отзыв устройства, узла, организации или запрет класса данных.

| Операция | Идентичность | Подтверждение / повтор |
|---|---|---|
| GET sdk-tools, files, documents/page, context, turns/events | Session + параметры GET | Повторяемое чтение, новый receipt не создаётся; context — актуальное состояние, events дедуплицируются по seq |
| POST /harness/agent/sessions | Явный Idempotency-Key; отдельный namespace create:hash | Повтор того же POST даёт cached create/rejected либо сверяет Router journal. Отказ с X-Atlas-Create-Outcome:rejected и X-Atlas-Create-Key подтверждён. Unknown не разрешает создать другую сессию |
| POST /prompt?mode=async | Явный key; legacy default prompt:GateSid:turnId | 202 ACK + events результата; receipt или повтор исходного POST |
| POST /files, /documents | Явный key + оригинальный body | Каталог/ACK и receipt; повтор не заменяет проверку hash/размера/владения файла |
| POST /tool_result | Явный key; legacy default tool-result:GateSid:callId | ACK доставки + receipt; инструмент в клиенте второй раз не исполняется |
| POST /compact | Явный key; Prefer:respond-async рекомендуется | 202 commandId → GET receipt. Legacy POST ждёт итог. Обрыв HTTP/connector не отменяет inference |
| POST /tools, /steer, /interrupt, /tasks/{id}/stop; DELETE files/documents/session | Явный key | ACK/terminal response + receipt. На другой логический Stop нужен новый key; старый ACK не отменяет новый ход |

Без явного ключа прочие legacy изменения получают случайный ID Gate: совместимость
одного вызова сохранена, но потерянный ACK не позволяет клиенту восстановить key
догадкой. Для безопасных повторов новый клиент обязан назначать ключ сам.

## Чтение квитанции без повторной отправки

```
GET .../{sid}/commands/{commandId}
GET .../{sid}/commands?requestId=<исходный Idempotency-Key>
```

Эти endpoints читают сессионные операции, включая prompt, files, tool_result,
control и compact. Session-create использует отдельный механизм повторного POST
с тем же ключом; эти сессионные endpoints его не заменяют.

Подтверждённый результат возвращает исходный HTTP status/body (как compact в
прежнем протоколе) и заголовки:

```
X-Atlas-Command-State: complete
X-Atlas-Command-Id: <opaque commandId>
```

Значение status может быть и 4xx/5xx: подтверждённый отказ воспроизводится,
не превращается в новый запрос. Ответ Gate нормализует Router errors существующим
образом. Для pending:

```json
{"commandId":"...","sessionId":"...","state":"pending","receiptState":"started"}
```

HTTP 202, Retry-After:1. `pending` означает наблюдение операции Gate, не доказательство
того, что модель уже работает. receiptState может быть unconfirmed/unavailable/missing.
При unknown/expired/malformed Router receipt: 503 command_outcome_unknown с command_id
и receiptState. Ни этот статус, ни 404 отсутствующей операции/сессии не доказывают
«действия не выполнялись». Доступ только по commandId/hash без owner session невозможен.
Чтение квитанции не восстанавливает SDK и не запускает inference.

### Подтверждённый отказ prompt до отправки (#98)

Изменение требует установки соответствующего выпуска Gate; локальные HTTP-тесты
не заменяют установленную сквозную приёмку Harness → Gate → Router.

`POST .../{sid}/commands/reconcile` с `version:1`, исходными `requestId`,
`turnId` и `bodySha256` может закрыть уже сохранённый complete503 только при
наличии внутреннего Gate-свидетельства preflight отказа. Оно записывается в
`router_operations` одной транзакцией с исходным HTTP-ответом. Свидетельство
привязано к device/org, session/turn, исходному body hash и node/boot/route/model.
Поле ошибки `deliveryState:not_sent`, её текст или статус 503 сами по себе
доказательством не являются.

Сверка создаёт стабильную `proofType:admission_fence`, `outcome:not_sent`,
`sealed:true`, `requiresNewIntent:true` и возвращает прежний version1 ответ
409 `command_not_sent` с `X-Atlas-Command-Outcome:not_sent` и
`X-Atlas-Recovery-Version:1`. Исходная complete503-квитанция и её GET остаются
неизменными. Поздний исходный POST получает ту же 503-квитанцию без новой отправки.
Продолжение возможно отдельным intent с новым key и turn после сохранения proof;
identity, доверие узла, boot, drain и текущая политика проверяются обычным путём.

Старые complete503-записи без внутреннего provenance, включая исходный инцидент
`b5aa5b68fafe8247f27458dd9a39a0c134eeb499381850271c55def8fd008044`,
автоматически не повышаются до negative proof. Имеющихся HTTP-полей недостаточно:
похожий ответ мог быть получен от Router после отправки. Такие записи,
unknown/потерянный ACK, arbitrary5xx и turn под другим ключом остаются
observation-only (202). Нужна отдельная проверенная процедура восстановления;
удалять операцию, сбрасывать её в prepared или угадывать not_sent нельзя.

### Импорт отдельного проверенного operator witness

Для исторической операции доступен только внутренний метод
`Operations.import_prompt_rejection(command_id, witness)`; HTTP repair API нет.
Это привилегированное действие оператора после независимой проверки журналов.
Отсутствие команды в Router DB или похожий текст ошибки не заменяют positive
evidence остановленного процесса на исходном endpoint.

Оператор сохраняет неизменный audit JSON в своём root-каталоге, root:0600;
проверяет владельца/режим, читает точные bytes и добавляет к `witness` поля
`auditRef` (абсолютный путь) и `auditSha256` (SHA256 этих bytes). Сам audit JSON
не должен содержать свой будущий hash. Метод доверяет выполненной оператором
проверке файла, не открывает его и не обращается в сеть.

Witness имеет `version:1`, `source:operator_process_down`,
`dispatchStarted:false`, `reason:identity_unconfirmed` либо `node_unavailable`;
содержит точные `commandId/deviceId/org/sessionId/turnId/bodySha256/nodeId/bootId/
method/path/route/model`, исходные `created`, `httpStatus:503`, `responseSha256`
(SHA256 исходного сохранённого UTF-8 response), а также `stoppedAt`,
`rejectedAt`, `startedAt` как UTC Unix seconds. Полный положительный интервал
обязателен: `stoppedAt <= created <= rejectedAt < startedAt`. `rejectedAt`
подтверждается исходным Gate HTTP log, не подставляется равным `created` из-за
отсутствия другого времени. В audit остаются источники этих фактов.

Под `BEGIN IMMEDIATE` метод сверяет все исходные поля, текущие owner/org и
local/node/route/model binding. Закрытая binding и последующий boot допустимы:
proof сохраняет boot исходной операции. Меняется только nullable
`admission_rejection`; исходные state/status/response, квота, история и binding
сохраняются. Повтор того же witness идемпотентен; замена или несовпадение — 409.
После импорта клиент владельца использует обычный version1 reconcile; сам
импорт не создаёт fence, сессию или SDK-ход.

## Доставка и потеря ответа

1. После локального client→Gate ConnectTimeout клиент может повторить безопасный
   GET или тот же запрос с прежним ключом. Одного server error code gateway_down
   недостаточно, чтобы назвать запрос неотправленным. Клиентская классификация
   и backoff/pending — Harness #328/#329.
2. Gate сохраняет prepared до отправки. При неоднозначной доставке хранит unknown.
   Повтор сначала читает Router `/v1/commands/{id}` на том же узле. Валидный complete
   (state/status/response и requestId, если поле присутствует) фиксируется в Gate; started/unknown/expired,
   чужой requestId и повреждённые ответы никогда не разрешают исполнение.
3. Для сессионных изменений повтор отправки допустим только после Router 404 И
   нового probe, подтвердившего исходный bootId, прежнюю binding и durable_commands.
   Router сохраняет tombstone expired, поэтому истечение receipt не становится 404.
   Один Gate/SQLite и per-operation lock сериализуют доставку. Смена boot или legacy
   операция без boot запрещают повтор даже при 404. Это условный безопасный replay,
   а не вывод «любое 404 означает не принято».
4. HTTP 5xx на канале mutation может принадлежать relay/connector после успешного
   исполнения. Gate сверяет журнал Router до terminal фиксации. Истинный terminal
   5xx воспроизводится; неподтверждённый 5xx остаётся unknown, не отнимает квоту как
   доказанный отказ и не запускает failover.
5. Отдельный случай: Nodes отказал ДО отправки свежей команды, не подтвердив
   доступность/identity узла (`node_unavailable`). Это сохранённый terminal отказ
   доставки с `deliveryState:not_sent`, резерв квоты освобождается. Повтор прежнего
   ключа воспроизводит этот отказ. Новая попытка доставки допустима с новым ключом
   только при этом явном доказательстве, сохраняя логический run/turn; это не
   разрешение менять ID при unknown. Не каждый HTTP 503 является not_sent.

## Хранение и restart

- Gate router_operations и create bindings не имеют автоматического TTL в этой
  реализации: сохраняют ключ/fingerprint/status/body в Gate SQLite. Backup/restore
  этой БД — часть сохранения гарантии; новая/потерянная БД её не восстанавливает.
- Router по ATLAS_NODE_JOURNAL_RETENTION_SEC (по умолчанию 7 дней) очищает response
  завершённых команд, оставляя expired tombstone. GET commands возвращает state:expired;
  повтор POST — 410. Если Gate уже сохранил complete, он воспроизводит свой ответ.
- После Gate restart pending сверяется, SDK не запускается повторно. После Router
  restart журнал complete читается без SDK; started становится unknown. Повторное
  исполнение unknown не разрешается. После client restart сохраняются исходные key
  и pending-состояние; новый ID запрещён как замена утраченного результата.
- Connector node_work: lease 40 с, HTTP delivery deadline 70 с, сборка complete/expired
  записей через сутки. Это параметры доставки, не срока inference. Просроченный
  relay-ответ требует Router receipt; lease retry использует прежний Idempotency-Key.

## Квалификация и границы

`ATLAS_TEST_ROUTER_ROOT=<checkout>` и тесты test_retry_contract,
test_tool_result_recovery, test_create_replay, test_compact_pending,
test_network_gate: настоящие HTTP Gate/Router, временные SQLite и ключи, stub SDK.
Проверяются до/после приёма, relay 502, конфликты, отрицательные/невалидные receipts,
перезапуски приложений с той же БД, owner/data-class/node и реальный Connector.
Это не proof новой доставки установленным UI Harness и не выяснение первопричины
сетевого инцидента. Платные запросы провайдеров для квалификации дедупликации не нужны.
