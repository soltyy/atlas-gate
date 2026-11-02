# Admission fence v1 (Gate #81 / Harness #495)

Это разрешение **отказаться от не принятого intent**, не continuation SDK.
Чтение старой квитанции по GET не изменилось: обычный404 ничего не доказывает.
Протокол объявлен в подписанном профиле `commandRecovery.version=1`,
`protocol=admission-fence-v1`, `scope=prompt`, `lateAdmissionBlocked=true`.

После unknown отправки сначала читать обычную квитанцию исходного ключа. Если
команды нет, клиент может явно закрыть её допуск:

```http
POST /harness/agent/sessions/{gateSessionId}/commands/reconcile
Authorization: Bearer <device token>
Content-Type: application/json

{"version":1,"requestId":"prompt:<original-turn>","turnId":"<original-turn>","bodySha256":"<64 lowercase hex>"}
```

SHA256 вычисляется от **исходного JSON тела POST**, не system prompt/model input,
не HTTP bytes с произвольными пробелами и не Pydantic body с добавленными defaults.
Алгоритм `json-sort-keys-compact-utf8-v1`: Python
`json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
allow_nan=False).encode('utf-8')`. JSON object keys сортируются, arrays сохраняют
порядок. Это не объявление RFC8785/JCS. Векторы: Unicode строки и отсутствие
непереданных полей существенны. Harness `managed.contract.canonical_json` совпадает.

## Исходы

**409 command_not_sent**, headers `X-Atlas-Command-State: complete`,
`X-Atlas-Command-Outcome: not_sent`, `X-Atlas-Recovery-Version: 1`,
`X-Atlas-Command-Id: <sha256(deviceId+':'+requestId)>`:

```json
{
  "error":{"code":"command_not_sent","message":"Исходный intent закрыт до допуска"},
  "recovery":{
    "version":1,"outcome":"not_sent","proofType":"admission_fence",
    "proofId":"<stable uuid>","commandId":"<stable hash>",
    "requestId":"<original key>","deviceId":"<owner>","org":"<owner org>",
    "sessionId":"<original Gate SID>","turnId":"<original turn>",
    "bodySha256":"<original canonical body SHA256>",
    "nodeId":"<pinned node>","bootId":"<binding boot>",
    "route":"<original route>","model":"<original model>",
    "sealed":true,"requiresNewIntent":true
  }
}
```

Проверять version/type/outcome/sealed, исходные key/SID/turn/SHA, owner device/org,
commandId и headers до обработки общего HTTP409. node/boot/route/model проверяет
Gate по binding; клиент сохраняет их и сверяет известные ему значения, не требует
публичного native SDK ID. Это квитанция аутентифицированного Gate через TLS,
не отдельно подписанный переносимый bearer token. `proofId` не даёт прав доступа.

Ответ сохраняется атомарно с запретом допуска. Оба пути — seal и begin→forward —
проходят одну SQLite BEGIN IMMEDIATE транзакцию. Побеждает либо admission (никакого
negative proof), либо fence (никакого forward). Даже поздний POST и откат на старый
Gate не исполнят запечатанный intent: SQLite trigger блокирует старый INSERT.
Квитанции и fences не имеют TTL, не удаляются обычной уборкой. Их удаление/смена
эпохи хранения требуют отдельного протокола; backup/rollback данных нельзя считать
прозрачным сбросом fences. Для восстановления БД надо сохранять весь ledger/fences.

Повтор этого POST и прежний GET
`/commands?requestId=<original-key>` возвращают тот же409/recovery и complete headers,
включая после закрытия binding и рестарта Gate. Чужой device/org получает404, отказ
политики/отозванного узла не обходится. Другой body/turn/session/key — conflict,
не новое доказательство.

**202**, body `{"recovery":{"version":1,"outcome":"unknown",...}}`, Retry-After1:
команда/тот же turn уже имеет admission или иной исход не доказан. Никакой fence не
создан. Продолжить **обычный GET commands/exact turn events** по старому key. Новый
endpoint не отправляет запрос и не превращает отсутствие ACK в отказ. Уже complete
accepted/rejected receipt сохраняется и читается существующим GET контрактом.
Для legacy operation без сохранённого raw body SHA negative proof не выдаётся.

Согласованные поля сохраняются клиентом с исходным immutable outbox. После
доказанного not_sent старый intent закрывается; новый запрос получает **новые turn
и key**, с ссылкой на прежний proof. Повтор исходного POST получает409 до квоты/SDK.
Старый turn нельзя повторно использовать под другим key. Если транспорт закрыт,
применяется штатный create/resume на прежней истории/узле, а не очистка outbox.
Существующие tool_result, прошлые ответы и usage не повторяются и не меняются.

## Поставка и maintenance

`deploy/nginx-gate-maintenance.conf` включается один раз в HTTPS vhost, проверяется
nginx -t и reload. Root-owned `deploy/gate_activate.py` устанавливается как
`/usr/local/sbin/atlas-gate-deploy`. Он проверяет wheel SHA, установленный пакет,
финальный test proof и installed `python -m atlas_gate.admission_check` до изменений.
Затем только при ready/undrained и activeTurns/subagents/reservedUnits0 создаёт
`/run/atlas-gate-maintenance`, проверяет публичный503JSON/Retry-After, drain,
повторный idle, stop/SQLite backup/symlink/start/readiness, undrain, удаление marker.
Сохраняются SQLite single-writer и SDK history. При ошибке запуска — rollback
исполняемого выпуска с сохранёнными fences и чтением старых квитанций.

Maintenance503 не содержит complete/negative proof и не разрешает повтор unknown
intent. Прямые новые create/prompt на живом Gate тоже отклоняются marker-ом.
Только полученная admission fence после восстановления сервиса закрывает прежнюю
неопределённость. Не нужно SQL-патчить рабочий pending prompt для инцидента #495.

## Совместная приёмка

Сначала GET existing receipt, затем seal отсутствующего admission. Проверяются:
seal/late POST race, restart/legacy INSERT, другой owner/session/key/turn/SHA;
accepted/lost ACK никогда не запечатывается; новый intent исполняет инструмент
один раз, старый не исполняется; journal/usage прежнего остаются неизменны.
Развёрнутый сервер отдельно проверяется собственной диагностической сессией без
бизнес-инструментов. Harness495 остаётся клиентской зависимостью: старые клиенты
не начинают автоматически пользоваться новым POST без его реализации.
