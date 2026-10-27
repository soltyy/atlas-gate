# Gate 0.4.12 — восстановление результата закрытого хода

Gate #71, Harness #372. Чтение `/harness/agent/sessions/{sid}/turns/{turn}/events`
теперь допускает owned `closed` binding наряду с `ready`. Существующий контракт
передаётся целиком: turnId, seq, nextAfter, events, state, done, terminal result,
ответ, usage и media descriptors. Отсутствующий журнал остаётся 404, истёкший —
410; ACK инструмента не превращается в успех хода.

Сохраняются device/org, data-class/node policy и привязка node/boot/SDK history.
При смене boot только GET reconcile подтверждает постоянную сессию; SDK не
создаётся и не возобновляется. Привязка остаётся closed, новые команды запрещены.
Повторное чтение использует существующую транзакционную дедупликацию usage.

GET media и media/{artifact} также допускают closed binding. Для байтов закрытых
сессий требуется Router 0.8.14 (Router #91); это только чтение ранее сохранённых
артефактов, без генерации и импорта истории SDK. Политики доступа не обходятся.

Клиентское восстановление локальной записи ACK/ответа и SQLite contention остаётся
в Harness #372. Серверное исправление не запускает инструмент, prompt или
tool_result и не удаляет историю.
