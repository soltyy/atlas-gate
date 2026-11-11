# Действующий контракт Harness/EDT — Gate — Router

Сверка 11.11.2026, Europe/Moscow. Этот статус отделяет поставленную реализацию от
целевых требований [RFC](SESSION-RESILIENCE-CONTRACT.md). Он не повышает версии wire.

## Проверенная база

- Gate **0.4.31** на vdska-de, merge `46f80f272beeed33793b3d3012613c3410b52fef`.
  Public health возвращает 200. [Выпуск и SHA-256](https://github.com/soltyy/atlas-gate/releases/tag/v0.4.31)
- Оба Router **0.8.24**, merge `4b6567ddea172811e07d3ab02e914323539919cc`.
  `vdska-codex` и `vdska-claude` при проверке ready/undrained, transport direct,
  capability `tool_result_recovery_v1`. Занятость проверяется заново перед
  lifecycle действиями. [Выпуск и SHA-256](https://github.com/soltyy/atlas-router/releases/tag/v0.8.24)
- Квалифицированная SDK-база поставки: Codex SDK0.160.0, Claude Agent SDK0.2.159.
  Возможности другого SDK/CLI или аккаунта нельзя переносить из этой строки.
- EDT **1.0.0.202611111927** опубликован в `program/s1-edt-api`, merge
  `2354b3849a40348462a6fa8cf68356535b23ed98`. Проверены изолированное обновление
  с 202611111729, четыре старые вкладки и повторный запуск. 202611111729 уже
  умеет обновлять токен в активном ходе, а подхват после перезапуска и защищённая
  доставка v1 квалифицированы с 202611111927. Установка на каждом рабочем месте
  проверяется отдельно. [EDT #234](https://github.com/soltyy/edt-llm-agent-extension/issues/234) · [EDT #236](https://github.com/soltyy/edt-llm-agent-extension/issues/236) · [ZIP](https://github.com/soltyy/edt-llm-agent-extension/raw/2354b3849a40348462a6fa8cf68356535b23ed98/release/ay-agents-latest.zip)
- Harness сообщает поставку negotiated controller в **0.1.151**:
  [Harness504](https://github.com/soltyy/atlas_harness/issues/504). Это клиентская
  квитанция, а не проверка текущей установки каждого пользователя и не подтверждение
  поддержки `sourceHistoryIdentity` или `toolResultRecovery` в Harness.

## Матрица возможностей

| Контракт | Поставлено на сервере | Условия и граница клиента |
|---|---|---|
| Create/resume/list, durable command receipts, exact turn journal/media | Действующие v1 endpoints сохраняются | Create ACK не равен terminal; публичный SDK alias не равен Gate SID или native ID. [Session](SESSION-CONTRACT.md), [receipts](RETRY-RECEIPTS.md) |
| Recovery inventory/cases/plan/apply/ACK/cancel | С Gate 0.4.20 / Router 0.8.19 | Подписанный `sessionRecoveryCoordinator.version=1`, локальный inventory, commit и ACK. [WIRE](SESSION-RESILIENCE-WIRE.md) |
| Чтение событий после обрыва | С Gate 0.4.29 | Временный обрыв Gate→Router повторяет чтение того же хода и курсора. После обрыва клиентского SSE — exact GET events. [Gate #115](https://github.com/soltyy/atlas-gate/issues/115) |
| Чтение завершённого результата после Stop/закрытия транспорта | Exact events/terminal receipt и сохранённый command ACK читаются в исходном owner scope | Статус живых callbacks требует ready binding. Если полные события истекли, summary подтверждает доступный итог в пределах retention |
| Сохранённый результат после подтверждённого Stop | С Gate 0.4.30, coordinator proof для `output_durable` | Требуются точный terminal и journal. При возможном допуске нужна исходная квитанция. Клиент применяет proof локально. [Gate #117](https://github.com/soltyy/atlas-gate/issues/117) |
| Активные callbacks и защищённая доставка | Gate 0.4.31 / Router 0.8.24, `toolResultRecovery.version=1` | Exact node/boot/turn/call, исходный digest/key и проверка `retryAllowed`. У старого Router остаются события и обычные квитанции. [Gate #119](https://github.com/soltyy/atlas-gate/issues/119) · [Router #115](https://github.com/soltyy/atlas-router/issues/115) |
| Resume owner/writer, подготовка файлов до первого prompt | Writer/input recovery v1 | Только доказанная передача idle writer; `empty_confirmed` не выдумывается по404. [Writer](WRITER-RECOVERY-V1.md), [input](INPUT-RECOVERY-V1.md) |
| Claude cold history-only restore | Поставлено и квалифицировано в своей области, сохранено в 0.8.24 | Требуется свежая capability exact pinned node/boot/case и допустимая конфигурация. [Claude](CLAUDE-COLD-RESTORE.md) |
| Native досыл в активный ход | Gate23/Router23, steering v1 | Точный target turn/node/boot, durable accepted/absorbed; terminal не переназначает досыл новому ходу. Общая клиентская приёмка [Gate90](https://github.com/soltyy/atlas-gate/issues/90) остаётся отдельной. [Steering](STEERING-V1.md) |
| Подтверждённый отказ prompt до dispatch | Gate24: provenance и admission fence | Только доказанный внутренний отказ или проверенное отдельное свидетельство. Произвольный503/потерянныйACK не становятся not_sent. [Gate98](https://github.com/soltyy/atlas-gate/issues/98) |
| Same-device восстановление входа | Gate24: auth v1 с отдельными versioned endpoints | Старый flow сохраняется; новый repair требует клиентской поддержки и проверки владельца. [Auth97](AUTH-RECOVERY97.md) |
| Текущий history alias и доказанная continuity после resume | Gate25: `sessionRecoveryCoordinator.sourceHistoryIdentity.version=1` | Owner-authorized read-only GET; `same_native_history` допускает узкий клиентский CAS с аудитом до events/tools/case. Не доказывает outcome. В EDT2236 проверено, для всех Harness не заявлено. [Identity103](SOURCE-HISTORY-IDENTITY103.md) |
| Обычные OpenAI Responses/Chat, Claude Messages, DeepSeek/другие API | Для поддержанных managed endpoints применяется opt-in API-attempt ledger/receipts | Обычный API и подписочный Claude Agent SDK используют разные контракты. Legacy API proxy допускает повторный inference при повторе POST. Upstream retrieve/cancel требуют отдельной возможности. [WIRE](SESSION-RESILIENCE-WIRE.md#4-просто-api-через-gate--отдельный-транспорт-и-ledger) |
| Reasoning/model policy | Reasoning v1 и capabilities точной route/model | Клиент сверяет supported/requested/configured; агрегация каталога не гарантирует уровень на закреплённом узле. [Reasoning](REASONING-V1.md) |

Новые поля additive; старые клиенты продолжают прежние поддержанные операции.
Новая функция используется только после её объявления в подписанном профиле и
проверки соответствующего pinned пути. Версия Gate сама по себе не разрешает
resume, смену модели, repair checkpoint или повтор команды. Direct Router и
direct API обходят Gate и сохраняют собственные клиентские adapters/ledgers.

## Квалифицированные границы и отдельные задачи

- Универсальный terminal continuation permit остаётся unsupported
  ([Gate77](https://github.com/soltyy/atlas-gate/issues/77)); transport restore,
  accepted tool ACK или identity proof его не заменяют.
- Подтверждённый server-side v1: живой HTTPS postflight на обоих действующих
  подписочных аккаунтах. Потеря ответа, повтор и гонка со Stop проверены на
  установленных wheel с тестовым SDK. Проверены прежний HTTP/JSON формат
  и смешанная серверная пара с Router 0.8.23. Проверка connector ограничена
  пересылкой пути и параметров на стенде. [Квитанция](SESSION-RESILIENCE-ACCEPTANCE.md#квитанция-gate-0431--router-0824-11112026)
- Автоматическое восстановление **всех открытых чатов после обновления клиента**
  прошло A57 на названном стенде: опубликованные1609→2236; затем независимый
  workspaceB, опубликованный2236→candidate0906→повтор candidate в третьей JVM.
  Реальные product JAR/ChatTabs.restore/WebChatSurface восстановили четыре вкладки:
  две исправлены по identity proof, здоровая сохранена, unprovable source остался
  защищённым. Startup/repeat не увеличили бизнес-счётчики; один новый явный prompt
  детерминированного backend проверил доступность прежней истории. Это Windows
  OSGi/loopback Gate25/Router23-stub приёмка, не универсальная квалификация SDK/OS
  и не пользовательская p2 установка. Точные hashes/счётчики — в
  [приёмке](SESSION-RESILIENCE-ACCEPTANCE.md) и [PR106](https://github.com/soltyy/atlas-gate/pull/106).
- Gate не меняет БД Harness/EDT. Клиент сохраняет исходные root/org/route/model,
  SID/alias/Run/request/body hash и аудит repair; затем создаёт свежую inventory
  с новым ключом и более новой version, сохраняя clientStoreEpoch и unresolved
  sources. При startup repair не выполняются новые prompt/tools.
- Ни один общий контракт не обещает exactly-once произвольных внешних effects,
  восстановления утраченной локальной БД/ключей, автоматической cross-account
  миграции или любых native SDK конфигураций.

Общий tracking [Gate88](https://github.com/soltyy/atlas-gate/issues/88) остаётся
открытым по этим границам. Конкретная серверная поставка, loopback квалификация,
публикация клиента и применение к настоящей пользовательской беседе — разные
факты и не подменяют друг друга.

Доказательства исторической поставки identity:
[установленный Gate25](https://github.com/soltyy/atlas-gate/issues/103#issuecomment-6613729546),
[Gate103 закрыт после EDT2236](https://github.com/soltyy/atlas-gate/issues/103#issuecomment-6613880212).
