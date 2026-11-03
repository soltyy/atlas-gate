# Claude cold restore, Gate #93 / Router #110

Gate 0.4.21 обновляет `models[].sessionRecovery.nodes[nodeId]` при фоновой
публикации подписанного профиля, включая точные nodeId/bootId. Эти сведения
не объединяются между узлами. Текущее решение coordinator по-прежнему сверяет
закреплённый узел и его свежий native proof: старый профиль не является разрешением
переехать на другой Router или повторить prompt/tool_result.

Router 0.8.20 квалифицирует history-only restore для закреплённого SDK/CLI,
персонального Claude.ai Pro/Max без managed policy и сторонних MCP. Вне этой
области capability или конкретная попытка возвращает явный отказ. Gate не
загружает SDK, не подменяет историю и не объявляет ACK успехом всего хода.

Контракт: [Router CLAUDE-COLD-RESTORE](https://github.com/soltyy/atlas-router/blob/main/docs/CLAUDE-COLD-RESTORE.md).
Существующие v1 endpoints и старые клиенты сохраняются. Поддержка локального
checkpoint остаётся в Harness #504 / EDT #222.

Приёмка: подписанный профиль до/после изменения capabilities одного Router,
сохранение ограничений другого Router, неизменность агентов; HTTP coordinator,
direct/connector receipt, ownership/boot/source/Stop fences; инертный postflight
на действующем Claude без повторения пользовательских действий.
