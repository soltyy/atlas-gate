# Точное направление реплики в текущий ход

Gate #90 расширяет существующий `POST /harness/agent/sessions/{sessionId}/steer`.
Выпуск и приёмка Router #106 и клиента требуются отдельно. Legacy `{text}` сохраняет
прежнее поведение; эта форма не обещает durable absorption.

Новая форма требует исходный стабильный `Idempotency-Key`, обе идентичности и
**обязательные** заголовки `X-Atlas-Steer-Contract: 1` и
`X-Atlas-Steer-Target-Boot: <captured original targetBootId>`:

```json
{"expectedTurnId":"original-turn","clientMessageId":"b62a0539-a97c-4219-983a-c3db7ee41f22","text":"уточнение","attachments":[]}
```

`clientMessageId` — canonical UUID Inbox-карточки. `expectedTurnId` — исходный
логический ход Router, не новый номер хода. Вложения используют существующий
`PromptRequest.attachments`. Gate проверяет весь пакет до отправки: неподдержанное
вложение не позволяет отправить отдельно caption. Флаги `attachments.images` и
`attachments.documents` берутся из фактической квалификации backend/runtime;
Gate проверяет DTO, формат/base64/PDF и exact steering support flags. Общие числовые
ceilings старого prompt/upload не выдаются за ограничения strong SDK input; фактические
provider/runtime bounds остаются у выбранного adapter.

Клиент захватывает `steering.requiredHeaders` из authenticated create/bound
sdk-tools вместе с intent, сохраняет их в outbox и использует при POST **и**
GET commands/by-key. Текущий boot нельзя подставлять вместо сохранённого после
перезапуска или обновления профиля. Заголовок contract без paired IDs и paired IDs
без contract/target boot отклоняются, не становятся legacy text-only.

`bodySha256` — SHA256 **точных исходных UTF-8 байтов HTTP body**, сохранённых outbox.
Gate не добавляет defaults и передаёт эти байты Router без пересериализации.
Изменение пробелов, Unicode escaping или добавление пропущенного `attachments:[]`
под прежним ключом даёт `409 idempotency_conflict`. Это правило steer отличается
от существующего canonical JSON digest восстановления prompt; тот контракт не меняется.
Contract/target boot дополнительно входят в source fingerprint; замена boot под
прежним ключом конфликтует даже при байтово идентичном body. Повтор `clientMessageId`
с новым ключом проверяется по device+org+UUID, включая другие сессии/модели, и
возвращает исходные public session/turn/state: новый preflight отказ не доказывает
неисполнение уже неизвестной оригинальной команды. Обычный явный follow-up prompt
остаётся отдельным intent; новые самостоятельные сообщения получают новые UUID.

Для новой команды Gate проверяет владельца и организацию, текущие права исходного
маршрута/модели/узла и data class. Узел остаётся привязанным в SQLite. Возможность
должна быть объявлена свежим `/v1/node` и подтверждена точной сессией через
`GET /v1/sessions/{id}/sdk-tools?steering=1`. Этот metadata-read не открывает SDK.
Сам read также требует contract1 и исходные Node/Boot; так обнаруживается старый
connector, который теряет новый header, до POST. Gate проверяет roundtrip до
публикации usable bound capability в create ACK и sdk-tools, не выдаёт backend hint
за доказанную работоспособность транспорта.
После допуска Router атомарно проверяет ещё активность **того же** хода; Stop и
завершение хода не переназначают реплику следующему prompt.
Gate не делает `recover_boot` при новом strong admission: captured boot должен
совпасть с постоянной binding, свежим node и bound capability. Gate передаёт Router
`X-Atlas-Steer-Contract:1`, исходные `X-Atlas-Node`/`X-Atlas-Boot`; прямой strong
Router клиент обязан передать все три. Отсутствие/mismatch не открывает SDK.

Приватный подписанный профиль содержит `model.steering` с `scope:pinned_node` и
отдельными записями узлов/boot. Это подсказка для выбора, не union возможностей
моделей. Публичный каталог не публикует этот map и идентичности узлов. Ответы
создания и bound sdk-tools сохраняют точное объявление Router:

```json
{"version":1,"expectedTurnId":true,"clientMessageId":true,"durableAbsorption":true,"delivery":"next_boundary","requiredHeader":"X-Atlas-Steer-Contract","targetNodeId":"bound-node","targetBootId":"original-boot","requiredHeaders":{"X-Atlas-Steer-Contract":"1","X-Atlas-Steer-Target-Boot":"original-boot"},"attachments":{"images":false,"documents":false}}
```

Существующий журнал `router_operations` хранит расширение steer и исходный digest;
отдельная очередь/журнал не создаются, дневная квота новых ходов не увеличивается.
Доказанные отказы preflight тоже сохраняются под исходным ключом: смена capability
не разрешает исполнить поздний повтор уже отклонённого intent.
POST и существующие `GET .../commands/{commandId}` / `GET .../commands?requestId=...`
дополняются проекцией:

```json
{"steer":{"version":1,"sessionId":"public-gate-session","turnId":"original-turn","clientMessageId":"b62a0539-a97c-4219-983a-c3db7ee41f22","uuid":"stable-router-input-uuid","state":"accepted","targetBootId":"original-boot","bodySha256":"sha256-of-original-http-body"}}
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

Gate 0.4.23 сохраняет известные измерения даже при terminal error после completed
SDK results: требуются `usageIncomplete:true`, `usageSource:completed_sdk_results`
и валидные nonnegative integer token counters. Это только известная часть:
неизвестные cache/total/tail значения остаются null, стоимость не оценивается,
событие остаётся error с исходным outcome. Те же markers сохраняются в historical
receipt, usage details и отчёте (`incompleteRecords`); dedup/учёт terminal происходят
один раз. Нет повторной генерации ради получения usage.

## Откат и startup

Постоянный Nginx snippet блокирует managed POST steer и GET commands/by-key, если
отсутствует `/run/atlas-gate-steer-v1`. `open_file_cache off` исключает stale stat;
`.pending` не разрешает запросы. Ответ `503 steer_contract_unavailable` со
`state:unknown` не является отрицательной квитанцией и не очищает старый intent.
Legacy без новых заголовков сохраняет прежнее поведение.

Постоянный systemd drop-in перед **любым** стартом Gate, включая старый wheel,
вызывает `--reset-runtime-contracts`: под отдельным коротким lifecycle flock
он меняет `/run/atlas-gate-contract-generation` и удаляет все positive/pending
markers. Deployment flock в ExecStartPre не используется. Отдельный root oneshot
под общим deploy flock вызывает
`--refresh-api-contract`: он восстанавливает API и steer markers независимо по
свежему authenticated runtime probe и actual Gate version. Старый Gate не содержит
`steerContractVersion:1`, поэтому не сможет оставить положительный steer marker.
Publisher захватывает generation до health/probe и публикует только при том же
generation под коротким lifecycle lock; сетевого I/O под ним нет. Startup clear
между probe и publish делает старого publisher obsolete. Активный oneshot повторяет
health/probe нового generation, поэтому concurrent restart не теряет startup refresh.
Это bookkeeping внешнего барьера, не журнал пользовательских действий/очередь.
Activation guard проверяет наличие startup hook и внешний Nginx fence до cutover.
Инфраструктура предназначена для отдельного steering release; в production этим PR
ничего не устанавливается.

Linux CI использует pinned Gate21 `34e9a7d01f794b70a74bb9ca6884fa1c2b494a7f` и
Router21 `d749bbe863baf16aa6e98835f60b43979e707732`, actual HTTP и собственный
непривилегированный Nginx (PID, high ports, все пять temp paths). SDK-ввод scripted.
Положительный контроль показывает, что старые runtime без opt-in теряют exact IDs;
managed cached intent не достигает их после rollback/startup probe.
