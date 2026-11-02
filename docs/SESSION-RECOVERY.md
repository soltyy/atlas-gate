# Восстановление после повторного входа SDK (Gate #79)

Используется существующий `POST /harness/agent/sessions`. Нового endpoint или
автоматического повторителя prompt/tool_result нет.

1. Gate заново проверяет Router. Additive `authentication.version=1` различает
   `authenticated`, `required`, `unknown`, `not_required`; старый `ready` сохранён.
   `sdk_auth_required` / `sdk_auth_unknown` с `outcome=not_sent` относятся только
   к данной попытке до создания, не опровергают старый uncertain ACK.
2. После входа тот же ещё не принятый create/key снова проходит свежую проверку.
   Если имеется старая завершённая квитанция, возвращаются её исходные байты,
   даже после закрытия транспорта. Для нового транспорта нужен новый create key.
3. `resumeSessionId` — непрозрачный Gate alias законного device/org. Gate сохраняет
   прежние route/model/node, проверяет journal identity и незавершённый turn.
   `session_recovery_pending` требует сверки прежнего хода; вход не разрешает replay.
4. Новый транспорт сначала возобновляет прежний SDK ID. `resumed=true` означает
   подтверждённую историю. Ответ дополняется `sessionRecovery.version=1`,
   `mode=resumed|history_fallback`, `previousSessionId`, `actionsReplayed=false`.
5. Только явный `allowHistoryFallback=true` разрешает пустую SDK при подтверждённом
   отсутствии истории. Router Claude допускает fallback только для точного SDK
   отказа `No conversation found with session ID`, а не для auth/transport ошибок.
   `resumed=false/resumeError` сохраняют существующий путь восстановления Harness
   из его канонической истории. Gate не составляет этот replay и не выполняет его.

Новый транспорт не продолжает автоматически прерванную бизнес-операцию. Полнота
истории и безопасная судьба pending prompt/tools/ACK проверяются существующим
клиентским recovery. Старые journal/events/usage остаются на прежнем binding,
их чтение не требует SDK login. При 404 metadata история не объявляется потерянной:
на закреплённый Router передаётся прежний SDK ID, который подтверждает её судьбу.

Отсутствующая модель даёт `model_unavailable`, а не capacity. Router сохраняет
документированный selector `opus` при SDK picker, перечисляющем только `opus[1m]`
для native-1M Opus >=4.7. Запрошенное значение `model=opus` передаётся SDK без
подмены. Ограничение на старшие версии сохраняет разные окна старых Opus.
Источник: https://code.claude.com/docs/en/model-config#model-aliases.

Покрытие: HTTP Gate→Router с двумя узлами; closed transport→login wait→новый
transport, история есть/нет; неизменные ACK, pinned node, запрет при pending turn;
Router auth false→unknown→true без рестарта; auth/transport resume не открывает
пустую SDK. Пользовательские действия в приёмке не повторяются.
