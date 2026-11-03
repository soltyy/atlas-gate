# Точное направление реплики в текущий ход

Gate #90 расширяет существующий `POST /harness/agent/sessions/{sessionId}/steer`.
Выпуск и приёмка Router #106 и клиента требуются отдельно. Legacy `{text}` сохраняет
прежнее поведение; эта форма не обещает durable absorption.

Новая форма требует исходный стабильный `Idempotency-Key` и обе идентичности:

```json
{"expectedTurnId":"original-turn","clientMessageId":"b62a0539-a97c-4219-983a-c3db7ee41f22","text":"уточнение","attachments":[]}
```

`clientMessageId` — canonical UUID Inbox-карточки. `expectedTurnId` — исходный
логический ход Router, не новый номер хода. Вложения используют существующий
`PromptRequest.attachments`. Gate проверяет весь пакет до отправки: неподдержанное
вложение не позволяет отправить отдельно caption. Флаги `attachments.images` и
`attachments.documents` берутся из фактической квалификации backend/runtime;
Gate дополнительно применяет negotiated attachment limits привязанного узла.

`bodySha256` — SHA256 **точных исходных UTF-8 байтов HTTP body**, сохранённых outbox.
Gate не добавляет defaults и передаёт эти байты Router без пересериализации.
Изменение пробелов, Unicode escaping или добавление пропущенного `attachments:[]`
под прежним ключом даёт `409 idempotency_conflict`. Это правило steer отличается
от существующего canonical JSON digest восстановления prompt; тот контракт не меняется.

Для новой команды Gate проверяет владельца и организацию, текущие права исходного
маршрута/модели/узла и data class. Узел остаётся привязанным в SQLite. Возможность
должна быть объявлена свежим `/v1/node` и подтверждена точной сессией через
`GET /v1/sessions/{id}/sdk-tools?steering=1`. Этот metadata-read не открывает SDK.
После допуска Router атомарно проверяет ещё активность **того же** хода; Stop и
завершение хода не переназначают реплику следующему prompt.

Приватный подписанный профиль содержит `model.steering` с `scope:pinned_node` и
отдельными записями узлов/boot. Это подсказка для выбора, не union возможностей
моделей. Публичный каталог не публикует этот map и идентичности узлов. Ответы
создания и bound sdk-tools сохраняют точное объявление Router:

```json
{"version":1,"expectedTurnId":true,"clientMessageId":true,"durableAbsorption":true,"delivery":"next_boundary","attachments":{"images":false,"documents":false}}
```

Существующий журнал `router_operations` хранит расширение steer и исходный digest;
отдельная очередь/журнал не создаются, дневная квота новых ходов не увеличивается.
Доказанные отказы preflight тоже сохраняются под исходным ключом: смена capability
не разрешает исполнить поздний повтор уже отклонённого intent.
POST и существующие `GET .../commands/{commandId}` / `GET .../commands?requestId=...`
дополняются проекцией:

```json
{"steer":{"version":1,"sessionId":"public-gate-session","turnId":"original-turn","clientMessageId":"b62a0539-a97c-4219-983a-c3db7ee41f22","uuid":"stable-router-input-uuid","state":"accepted","bodySha256":"sha256-of-original-http-body"}}
```

`accepted` подтверждает приём, но не поглощение моделью. `absorbed` требует
доказательства Router из native context и события `steer_absorbed` исходного хода.
Gate сохраняет его до выдачи события/курсора. `not_applied` требует положительного
отказа до dispatch. `unknown` сохраняет неопределённость; 404 и отсутствие памяти
не превращаются в отрицательное доказательство и **никогда не разрешают повторный
POST steer**. Клиент сверяет исходный ключ, не создаёт prompt fallback автоматически.

При restart/lost ACK Gate читает журнал прежнего Router. Текущая lookup-проекция
важнее исходного сохранённого HTTP ACK: `unknown` после reboot не отменяется
запоздалым `accepted`, а доказанное `absorbed` не понижается последующим ACK.
Чтение квитанций не создаёт SDK-транспорт и не вызывает модель.
