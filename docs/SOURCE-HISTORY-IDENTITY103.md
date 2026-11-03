# Source history identity v1

Gate #103 / EDT #228. Подписанный профиль публикует
`sessionRecoveryCoordinator.sourceHistoryIdentity`. Старые контракты не меняются.

```json
{"version":1,"pathTemplate":"/harness/agent/sessions/{sid}/history-identity","previousAliasQuery":"previousAlias","readOnly":true,"businessReplayAllowed":false,"provesOutcome":false}
```

GET по точному Gate SID выполняется с токеном прежнего устройства. Без
`previousAlias` возвращает назначенную текущей привязке opaque identity, включая
случай до первого native SDK event. `continuity.kind=current_binding` не доказывает
правомерность замены старого alias. С `previousAlias`, отличным от текущего,
Gate проверяет существующую durable resume-цепочку до законно принадлежащего
этому устройству и организации predecessor. Каждое звено требует сохранённого
ACK `resumed:true`, отсутствия `resumeError`, `allowHistoryFallback:false`, равных
известных native identities и прежних owner/org/route/model/node. Простого
совпадения native ID недостаточно. Closed predecessors разрешены к чтению;
авторизация, data policy, архивирование организации и отзыв узла сохраняются.

Ответ содержит `version:1`, `proofType:source_history_identity`, `gateRoot`,
`sessionId`, `orgId`, `routeId`, `model`, `historyAlias`, `proofId`,
`businessReplayAllowed:false`, `outcomeKnown:false`. Доказанная непрерывность:
`continuity:{kind:same_native_history,previousAlias,previousSessionId}`.
`proofId` — SHA256 канонического JSON всего ответа до добавления proofId
(`json-sort-keys-compact-utf8-v1`). Raw native IDs и credentials не публикуются.
Это согласованный снимок локальных данных, без SQL writes, Router I/O, SDK,
resume, prompt или tool_result. Он не является квитанцией исполнения или
доказательством бизнес-успеха. Повторное чтение неизменённых фактов стабильно.

Неизвестная/чужая привязка или claimed alias →404; отсутствие подтверждённой
цепочки/identity, fallback, ошибка resume или цикл →409
`history_identity_unavailable`; отказ политики →403, неверный вход →401.
Пустой/слишком длинный previousAlias →422. Отказы не разрешают повтор prompt.

Клиент сравнивает root/org/route/model/точные SID/aliases со своим неизменяемым
source ledger. Ремонт alias допустим только по явной доказанной continuity и
клиентскому CAS до любых events/tools/terminal/recovery case. Исходная binding и
полный proof сохраняются аудитом; requestId/body SHA/Run/outputs не меняются.
Затем клиент инвалидирует cached inventory, повышает checkpointInventoryVersion,
сохраняет clientStoreEpoch и unresolved sources, создаёт новую inventory новым
ключом. Gate создаёт новый inventoryId/generation; старый unsealed snapshot не
переигрывается. Старые case/ACK/команды не удаляются. Нужный terminal читается
через существующий coordinator, а не получается повторной генерацией.

API attempts не используют SDK history aliases: их отдельный attempt-контракт
не меняется. Native SDK особенности принадлежат Router; этот протокол не
создаёт новых гарантий native resume и не требует новой версии Router.
