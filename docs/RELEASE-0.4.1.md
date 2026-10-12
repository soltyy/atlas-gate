# Gate 0.4.1 — долгие команды сжатия

Gate #46: общий read timeout70s больше не обрывает здоровый compact. Команда
отслеживается отдельно от HTTP-запроса; async ACK202 и владельческий receipt,
постоянный requestID, прежний узел, восстановление после Gate restart. Сетевой
отказ connector сверяется с собственным журналом Router и не становится ложным
терминальным SDK-отказом. DELETE pending-сессии запрещён; explicit interrupt
отделён от очистки после потери связи. Контракт: COMPACT-COMMANDS.md.

Проверки исходников: 113 passed, 6 opt-in skipped; четыре новых real-HTTP теста:
async/ownership/duplicate, legacy beyond shared read deadline, Gate restart,
потерянный connector reply. Wheel/sdist и locked зависимости. Пользовательскую
HR MAN беседу не сжимали повторно; никакие рабочие истории не переписывались.

Harness source companion — atlas_harness #265. Старый Harness использует
синхронную совместимость Gate и больше не получает прежний70s отказ на здоровом
Router; новый клиент получает ACK202, сохраняет pending ID/payload, проверяет
receipt и пишет результат в каноническую историю до удаления checkpoint.

Поставка Gate-only: отдельный каталог, SOURCE_REVISION, backup конфигурации/SQLite,
atomic symlink и health rollback. Router/Claude службы не рестартовать. Живую
приёмку выполнять на собственной QA сессии, а длительность>70s — отдельным
HTTP-сценарным контуром без пользовательских данных/подписки. По факту приёмки
добавить deployment/acceptance receipt и закрыть Gate #46.
