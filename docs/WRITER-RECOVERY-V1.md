# Writer recovery v1: Gate87 / Router103

Stop завершает ход, но idle SDK app-server может продолжать держать writer thread.
Повторное создание транспорта той же истории не должно открывать второй writer.
Router сериализует open/resume и сопоставляет logical/native ID с собственными
backend sessions. Старый transport освобождается только при согласованном idle
registry + journal + task + pending tools + compact + children. Entry помечается
retiring до await; begin_turn такого entry запрещён. Штатный RPC close сохраняет
историю и original ACK; после успеха journal metadata writer-release:{localId}
фиксирует transport_closed/actionsReplayed=false. Срок простоя не доказывает смерть.

Для живого или неизвестного владельца:
HTTP409 {"error":{"code":"sdk_writer_busy","phase":"session_create",
"outcome":"not_sent","retryable":true,"recovery":{"version":1,
"scope":"transport_only","ownerState":"busy|unconfirmed","actionsReplayed":false}}}
Текст локализован, без внутренних SDK IDs. Router оборачивает в detail; Gate
выдаёт error. Retry-After отсутствует, если SDK не сообщил срок освобождения.
Gate сохраняет ответ create и X-Atlas-Create-Outcome: rejected с исходным key.
Неизвестный HTTP outcome сверяется по тому же key; он не становится not_sent.
После подтверждённого rejected create повторяется только создание транспорта с
НОВЫМ intent/key. Старый key навсегда сохраняет прежний отказ. Ожидание с backoff
относится к транспорту, не к prompt/tool_result. Stop пользователя прекращает
ожидание и не воскрешает отменённый запрос. После SDK resume required resumed=true.

Клиентский handoff Harness: сохранить ожидающий Run, распознать sdk_writer_busy,
дождаться освобождения и продолжить create/resume, используя новый ключ только
после durable rejected receipt. Не исчерпывать фиксированное число попыток busy.
При неизвестном outcome сперва receipt/reconcile старого intent. Подтверждение
пустого input transport и writer wait не разрешают повторять бизнес-действия.
Нового endpoint/протокола нет: additive typed error в существующем create contract.
Внутренний node.writerRecovery v1.idleHandoff позволяет Gate передавать busy create
новому Router для durable отказа; старые Router сохраняют прежнюю защиту preflight.

Смена модели Astra→Sol отдельно: сохраняется проверка model/route binding. Пока
pending_rebind, сжатие прежнего alias выполняется на прежней модели/маршруте;
новый alias другой модели выдаётся только штатным rebind после сохранения контекста.
Нельзя исправлять client compaction выбором новой модели для старого alias.

Квалификация: свой native thread (без бизнес-инструментов) после cold resume
передаётся новому transport без нового prompt; старый writer закрыт, история и
4 original файла доступны. Рабочий rollout Зинаиды не меняется, его проверка
выполняется thread/read без thread/resume, prompt, compact или tool_result.

Native lock не обходится и не удаляется. Механизм exclusive writer описан в
[OpenAI source](https://github.com/openai/codex/blob/main/codex-rs/rollout/src/writer_lock.rs).
