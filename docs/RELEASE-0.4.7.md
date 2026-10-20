# Gate 0.4.7 — повторы и квитанции

Gate #60: подтверждение mutation 5xx через Router journal, общий безопасный replay
сессионных операций с прежним boot/durable dedup, квитанции по commandId или
исходному Idempotency-Key. Compact wire совместим; чтение результата проверяет
owner/org/node/data-class. Невалидные receipts не завершают операцию и не роняют
фоновую сверку. Контракт: [RETRY-RECEIPTS.md](RETRY-RECEIPTS.md).

Клиентские повторы/ожидающие задания Harness остаются #328/#329. Обновление Gate
само по себе их не реализует. Поставка и результаты приёмки фиксируются отдельно.
