# Подготовка файлов до первого SDK prompt: Router101 / Gate85

Codex thread/start до первого turn может не создать rollout. При restart native
thread/resume возвращает точный `no rollout found`. Это не несовместимость files.

Таблица virgin_sessions создаётся только для новых Codex-сессий без resume.
Legacy/неизвестная история не считается пустой. Descriptor связан с локальной
сессией и её logical SDK ID. Положительное empty_confirmed допустимо только при
eligible=1 и отсутствии ходов, кроме durable deferred, ещё не отправленных SDK.
Перед dispatch SDK eligible необратимо сбрасывается; отсутствие deferred после
crash также запрещает пустую замену. Compact защищает историю до вызова SDK.

Только точный native no-rollout, положительный durable descriptor и отсутствие
действий разрешают принять созданный пустой native transport. Нового prompt нет.
Logical SDK ID сохраняется атомарной штатной картой Codex public→native; file,
document и media scopes, documentIds и параметры согласованного SDK сохраняются.
resumed=false не подменяется true: пустая сессия не объявляется прежней историей.
После первого явного prompt обычная история возобновляется по той же карте.

`inputRecovery` v1 в reconcile/files/sdk-tools: sessionId, logicalSdkSessionId,
state=empty_confirmed|protected, generation, actionsReplayed=false. Gate проверяет
эти поля вместе с device/org/node/boot/policy до принятия нового boot. GET файлов
не выдаёт proof отсутствовавшего или завершённого business turn.
В публичном ответе Gate sessionId и logicalSdkSessionId заменены его непрозрачными
публичными IDs; внутренние Router/SDK IDs клиенту не раскрываются.

InputMiddleware учитывает аутентифицированные create/files/documents/sdk-tools
запросы до чтения тела и до завершения ответа. preparingInputs включён в
reservedUnits: существующие guards не останавливают активный input I/O. Drain
закрывает новый admission, но не отменяет уже принятый. 503 input_preparation_paused
с scope=attempt сообщает только отказ текущей попытки, не опровергает ранее
неизвестную доставку. ACK загрузки читается по исходному idempotency key.
Новый временной предел ходов/загрузок не вводится.

Старые API сохранены, новые поля additive. Ошибки восстановления SDK отделены от
файлового protocol mismatch. Восстановление закрытой пользователем сессии, её
запроса или бизнес-инструментов автоматически не выполняется. История и оригиналы
не очищаются. Клиентская классификация ошибок — отдельный handoff Harness495.

Обязательная установленная проверка input_check использует собственные temp DB,
оригиналы и новые native SDK threads:4uploads →cold restore до prompt →один
инертный prompt →ещё cold restore; никаких пользовательских действий/истории.

GET files/sdk-tools includes inputRecovery only with X-Atlas-Input-Recovery: 1 from the trusted Gate. Older Gate receives the legacy response without internal identities. New Gate replaces identifiers with public aliases. Reconcile is internal and always includes available proof.
