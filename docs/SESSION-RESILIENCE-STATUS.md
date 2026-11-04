# Действующий контракт Harness/EDT — Gate — Router

Сверка 04.11.2026, Europe/Moscow. Этот статус отделяет поставленную реализацию от
целевых требований [RFC](SESSION-RESILIENCE-CONTRACT.md). Он не повышает версии wire.

## Проверенная база

- Развёрнут Gate **0.4.25**, source `f69e3b4253394444921f6cbb863ff704f53aac98`,
  [выпуск](https://github.com/soltyy/atlas-gate/releases/tag/v0.4.25).
- Оба Router **0.8.23**, source `8ec0b6d8be0a91fc1351d99bff3a125cc3e660c5`,
  ready/undrained при read-only проверке. Состояние занятости не является частью
  постоянного контракта и проверяется заново перед lifecycle действиями.
- Квалифицированная SDK-база поставки: Codex SDK0.160.0, Claude Agent SDK0.2.159.
  Возможности другого SDK/CLI или аккаунта нельзя переносить из этой строки.
- EDT **1.0.0.202611032236** опубликован с приёмкой identity/coordinator:
  [EDT228](https://github.com/soltyy/edt-llm-agent-extension/issues/228#issuecomment-6613861177).
- Кандидат EDT **1.0.0.202611040906**, source
  `3948a77c0d518fcf1ede9376c0e38ef8dd35bdaa`, прошёл отдельную приёмку обновления
  и повторного запуска A57. Его публикация и установка через p2 в рабочую EDT
  этим результатом не подтверждаются. [EDT230](https://github.com/soltyy/edt-llm-agent-extension/issues/230).
- Harness сообщает поставку negotiated controller в **0.1.151**:
  [Harness504](https://github.com/soltyy/atlas_harness/issues/504). Это клиентская
  квитанция, а не проверка текущей установки каждого пользователя и не подтверждение
  поддержки всех последующих additions, включая sourceHistoryIdentity.

## Матрица возможностей

| Контракт | Поставлено на сервере | Условия и граница клиента |
|---|---|---|
| Create/resume/list, durable command receipts, exact turn journal/media | Действующие v1 endpoints сохраняются | Create ACK не равен terminal; публичный SDK alias не равен Gate SID или native ID. [Session](SESSION-CONTRACT.md), [receipts](RETRY-RECEIPTS.md) |
| Recovery inventory/cases/plan/apply/ACK/cancel | Gate20/Router19, сохранено в25/23 | Согласуется по подписанному `sessionRecoveryCoordinator.version=1`; требуется локальный inventory, commit и ACK клиента. [WIRE](SESSION-RESILIENCE-WIRE.md) |
| Чтение завершённого результата после Stop/закрытия транспорта | Сохранённый exact journal/receipt читается по owner/source | Stop не разрешает новый dispatch отменённого intent. Отсутствующий полный журнал не заменяется успехом |
| Resume owner/writer, подготовка файлов до первого prompt | Writer/input recovery v1 | Только доказанная передача idle writer; `empty_confirmed` не выдумывается по404. [Writer](WRITER-RECOVERY-V1.md), [input](INPUT-RECOVERY-V1.md) |
| Claude cold history-only restore | Поставлено и квалифицировано в своей области, сохранено Router23 | Требуется свежая capability exact pinned node/boot/case и допустимая конфигурация. Это не универсальный resume любого аккаунта/политики. [Claude](CLAUDE-COLD-RESTORE.md) |
| Native досыл в активный ход | Gate23/Router23, steering v1 | Точный target turn/node/boot, durable accepted/absorbed; terminal не переназначает досыл новому ходу. Общая клиентская приёмка [Gate90](https://github.com/soltyy/atlas-gate/issues/90) остаётся отдельной. [Steering](STEERING-V1.md) |
| Подтверждённый отказ prompt до dispatch | Gate24: provenance и admission fence | Только доказанный внутренний отказ или проверенное отдельное свидетельство. Произвольный503/потерянныйACK не становятся not_sent. [Gate98](https://github.com/soltyy/atlas-gate/issues/98) |
| Same-device восстановление входа | Gate24: auth v1 с отдельными versioned endpoints | Старый flow сохраняется; новый repair требует клиентской поддержки и проверки владельца. [Auth97](AUTH-RECOVERY97.md) |
| Текущий history alias и доказанная continuity после resume | Gate25: `sessionRecoveryCoordinator.sourceHistoryIdentity.version=1` | Owner-authorized read-only GET; `same_native_history` допускает узкий клиентский CAS с аудитом до events/tools/case. Не доказывает outcome. В EDT2236 проверено, для всех Harness не заявлено. [Identity103](SOURCE-HISTORY-IDENTITY103.md) |
| Обычные OpenAI Responses/Chat, Claude Messages, DeepSeek/другие API | API-attempt ledger/receipts и protocol-specific adapters | Retrieval/cancel/повторы зависят от фактического upstream. OpenAI-совместимый JSON не гарантирует одинаковую семантику или inference dedup |
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

Доказательства текущего identity контракта и совместной поставки:
[установленный Gate25](https://github.com/soltyy/atlas-gate/issues/103#issuecomment-6613729546),
[Gate103 закрыт после EDT2236](https://github.com/soltyy/atlas-gate/issues/103#issuecomment-6613880212).
