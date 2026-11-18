# Gate 0.4.42 — доставка ответа на вопрос

Добавлены pinned question-answer endpoint, durable outbox исходных байтов и
отдельная квитанция admission/application Router. Владелец устройства, организация,
session/turn/call/clientMessageId, SHA и исходные node/boot проверяются перед
приёмом квитанции. Повтор исходного запроса не создаёт новый prompt и не запускает
инструмент заново. `unknown` не превращается в отрицательную квитанцию по таймеру.

Capability проведена через подписанный профиль, ProfileModel, SessionCreated и
sdk-tools с проверкой header round-trip для direct и connector. Старый connector
без поддержки не включает функцию. Клиент обязан требовать Gate-specific
submitPath/statusPath/requiredHeaders, а не только supported=true от Router.
Прежние `/tool_result`, `/steer`, API-маршруты и старые клиенты сохраняют свои пути.

Настоящие Codex и Claude прошли Gate direct с потерей ACK и connector с потерей POST:
effect=1, question=1, user=1; native callback предшествует user. Целевые проверки:
58 passed. Полный Linux-прогон: 524 passed / 7 skipped / 5 failed; три отказа
воспроизведены на исходной базе, два относятся к metadata общего test-venv.
Для установленного wheel 0.4.42 оба version/health теста прошли отдельно. Полная
регрессия не объявляется зелёной; подробности в [реконе](RECON-QUESTION-ANSWER135.md).

Wheel SHA-256: `583d07c795abf6a211b26d6b07af319443f352bed1707c018d1ef28689c266fa`.
Контракт: [QUESTION-ANSWERS-V1.md](QUESTION-ANSWERS-V1.md).
Требуется Router 0.8.30; клиентский потребитель — Harness #545. Версия v1 root_turn:
вопросы субагентов клиент не должен рекламировать как поддержанные.

Публикация не означает установку. Переключение выполняется штатным Gate deploy
после освобождения Router-ходов и HTTP/API попыток, с резервной копией БД и rollback.
Рабочие SDK-истории, вложения, квитанции и client bindings не очищаются.
