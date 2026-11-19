# Ответ на вопрос: questionAnswers v1

Контракт Router #127 / Gate #135 для Harness #545. Серверная часть опубликована
в Router 0.8.30 / Gate 0.4.42 и сохранена в Router 0.8.31 / Gate 0.4.43.
Живая серверная приёмка Codex и Claude выполнена; она не означает подключения
карточки вопросов в Harness. На срезе Harness 0.1.183 задача #545 открыта,
клиентский обработчик этого контракта отсутствует. Текущий статус сверять
по [аудиту контрактов](CONTRACT-AUDIT-20261119.md), а не по номеру версии клиента.

## Назначение и границы

Одна операция связывает ожидающий клиентский `ask_user` с одной обычной репликой
человека в исходной SDK-истории. Технический callback не содержит текст ответа.
Admission в журнале не равен применению текста моделью. Вторая стадия подтверждается
нативным consumed UUID / context item, а не HTTP 200 или завершением callback.

v1 поддерживает **корневой ход** (`scope=root_turn`). Для вопроса субагента
возвращается `question_answer_subagent_unsupported`: ответ нельзя перенаправлять
в корневую историю. Это не общий механизм одобрений и не ответ на произвольный
tool_call. Обычные инструменты, их результаты и старые клиенты работают по прежним
маршрутам. Автоматические задания Harness не должны зависать на вопросе: клиент
подключает инструмент только для интерактивного `source=ui`.

## Согласование

Проверять `questionAnswers.supported == true` именно у **закреплённой сессии**:
ответ создания или `GET .../sdk-tools`. Профиль Gate и `/v1/node` — discovery,
они не заменяют эту проверку. Router требует квалифицированные версии SDK,
durable SQLite и node/boot; Gate проверяет проход обязательного заголовка через
конкретный HTTP/connector. Старый connector получает `supported=false`.

Capability включает `version=1`, `scope=root_turn`, `tool=ask_user`,
`linkedCallback`, `userMessage`, `durableAdmission`, `separateApplicationReceipt`,
`immutableRetry` (все true), исходные `targetNodeId`, `targetBootId`.
Gate дополнительно возвращает `submitPath`, `statusPath`, `requiredHeaders`.
Для маршрута Gate эти поля обязательны: проверять ожидаемые пути `/harness/agent/…`
и полный набор заголовков, включая исходный target boot. Один `supported=true`
недостаточен: старая Gate может пропустить незнакомую Router capability как
непрозрачное расширение ответа, но собственного обработчика ещё не иметь.
Raw Router capability без Gate URL/requiredHeaders не включает клиентскую функцию.

## Запрос

Router: `POST /v1/sessions/{sessionId}/question-answer`.
Gate: `POST /harness/agent/sessions/{sessionId}/question-answer`.
Обычная авторизация поверхности обязательна (Router token / Gate device bearer).

Общие заголовки: `Content-Type: application/json`, `Idempotency-Key: <saved key>`,
`X-Atlas-Question-Answer-Contract: 1`.
Router также требует `X-Atlas-Node` и `X-Atlas-Boot` из capability.
Gate требует `X-Atlas-Question-Answer-Target-Boot` из `requiredHeaders`; внутренний
Router key Gate вычисляет из владельца и исходного клиентского ключа.

```json
{
  "version": 1,
  "turnId": "original-logical-turn",
  "callId": "original-ask_user-call",
  "clientMessageId": "8cecb54f-7a44-4c98-9cc1-09216bdf4900",
  "text": "Точный ответ человека"
}
```

`clientMessageId` — один сохранённый canonical UUID, `version` — integer 1.
Поля строго проверяются, лишние поля запрещены. Пробелы и переводы строк текста
не нормализуются. До HTTP клиент сохраняет исходные UTF-8 **байты всего JSON**,
их SHA-256, session/turn/call, clientMessageId, key и target node/boot. Повторная
сериализация даже с другим форматированием — другой hash и конфликт 409.

Сначала Gate сохраняет outbox, Router — durable admission. Только Router
производит единственную native-запись с собственным сохранённым UUID. Пока
callback удерживает границу шага, ставится обычный user-ввод; затем callback
освобождается техническим результатом `status=answered, clientMessageId`.
Порядок в нативной истории проверяется отдельно от порядка HTTP-запросов.

У Codex user-ввод применяется в текущем native turn. У Claude используется
`priority=later`: обычная реплика после текущего SDK query в той же session/history,
а существующий ledger удерживает исходный **Router logical turn** до её потребления.
Параллельные SDK tools не прерываются. Это не отдельный клиентский prompt и не
повтор инструмента. `priority=next` для вопросов не годится: CLI превращает его
в attachment/system-reminder; `now` прерывает tools. Число native queries у SDK
может различаться, admission остаётся pending до фактического применения.

## Квитанция и восстановление

Ответ содержит `questionAnswer`. Поля идентичности:
`version`, `requestId`, `sessionId`, `turnId`, `callId`, `clientMessageId`,
`bodySha256`, `targetNodeId`, `targetBootId`, `uuid` (native message UUID).
Gate возвращает внешний SID/key и добавляет `gateAdmission=accepted`.
До получения Router-квитанции `uuid=null`, `admission=unknown`.

| Поле | Значения / смысл |
|---|---|
| state | admitted, pending, applied, not_applied, unknown |
| admission | accepted подтверждает Router admission; Gate outbox сам по себе этого не доказывает |
| callback | pending, released, not_released |
| application | pending, applied, not_applied, unknown |
| replaySafe | true: разрешён транспортный повтор исходных байтов и ключа |
| retryAllowed | false: запрещён новый логический ввод или повтор инструмента |
| reason | Машинная причина; отсутствие evidence никогда не означает not_applied |

`state=applied` требует одновременно `callback=released` и `application=applied`.
200 означает терминальную квитанцию (включая not_applied); 202 — продолжающуюся
сверку. Клиент обязан смотреть поля, а не только HTTP-код.

GET Router: `/v1/sessions/{sid}/question-answers/{clientMessageId}`.
GET Gate: `/harness/agent/sessions/{sid}/question-answers/{clientMessageId}`.
Обязательные query: `turnId`, `callId`, `bodySha256` (64 lowercase hex).
GET не создаёт SDK-транспорт, не возобновляет сессию, не отправляет prompt,
не исполняет инструмент и не является heartbeat сессии.

Потерян POST до admission: GET может дать 404; повторять только сохранённый
POST с прежним ключом и байтами. Потерян ACK: тот же POST и GET возвращают
исходную операцию; SDK-ввод не повторяется. После смены boot старую квитанцию
можно прочесть, но доставлять на новый boot / другой узел нельзя.
После restart до начала SDK dispatch Router фиксирует not_applied; после
начала без полного доказательства — unknown. Положительное нативное evidence
может подтвердить application; недоступный журнал сохраняет unknown.
Stop использует существующее закрытие native producer до отказа callbacks;
pending/unknown не разрешают новый prompt или compact. Без доказательства
клиент сохраняет историю и показывает причину, а не делает слепой replay.

## Ошибки

| HTTP | Причины / действие клиента |
|---|---|
| 401/403 | Проверить авторизацию/владельца; не менять SID ради обхода |
| 404 | question_answer_not_found: нет локальной квитанции; это не доказательство отсутствия записи у upstream |
| 409 | idempotency_conflict, identity_conflict, receipt_identity_mismatch: запрет нового key/body |
| 409 | target_boot_mismatch, target_not_active, call_not_waiting, call_mismatch, subagent_unsupported, stopped: прочесть сохранённый исход |
| 409 | question_answer_owns_callback: не отправлять обычный tool_result для уже связанного вопроса |
| 409 | question_answer_reconciliation_required: сначала разрешить прежнюю операцию, не запускать новый ход/compact |
| 422 | Неверная схема/обязательные заголовки; в Gate также unsupported capability |
| 501 | Router question_answer_unsupported: SDK/журнал не квалифицированы |
| 502/503 | Нет достоверной квитанции/связи: сверять и повторять только исходный транспортный POST |

Точные коды в JSON имеют префикс `question_answer_`; ошибки Gate могут также
нести исходный код node/connector. Ни timeout, ни 404, ни HTTP 200 callback
не разрешают повторный запуск инструмента. Изменённые hash/turn/call/владелец,
другой UUID или node/boot отвергаются до принятия квитанции клиентом.

## Подключение Harness

В `user_questions` включить `ask_user` для интерактивного router-agent только
после bound capability. Карточку и обычную реплику в клиентской истории связать
одним clientMessageId транзакционно. Ответ отправлять новой операцией, не парой
`tool_result` + новый `prompt`. Сохранять outbox до applied/not_applied; при
unknown показывать причину и повторять сверку. При restart, активации вкладки,
Stop и у границы compact сохранять исходные идентификаторы и байты. Вопросы
субагентов до отдельной версии контракта не рекламировать.
