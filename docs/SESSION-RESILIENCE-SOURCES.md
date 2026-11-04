# Источники, основания решений и пределы проверки

Приложение к [RFC](SESSION-RESILIENCE-CONTRACT.md), 02.11.2026.
Статусы исходного аудита ниже относятся к этой дате; поставки после него
сверяются по [актуальной матрице](SESSION-RESILIENCE-STATUS.md). Эта правка не
объявляет повторной проверкой внешних страниц обновление локального статуса Atlas.
Внешние страницы открыты при подготовке документа. Они описывают актуальные
продукты поставщиков, **не гарантируют наличие всех возможностей в установленных
SDK** и могут измениться. Численные provider limits здесь не используются для
выдуманного общего ограничения времени работы Atlas.

Нормативный протокол координатора и матрица E/A — проект Atlas, а не цитирование
или обещание поставщиков. Ссылки ниже обосновывают отдельные технические решения.

## 1. OpenAI и Codex

| ID | Первичный источник | Что использовано / граница |
|---|---|---|
| S01 | [Codex App Server](https://learn.chatgpt.com/docs/app-server) | Thread/turn/control и notifications различаются; interrupt и terminal — не один факт. Документация новее зафиксированного SDK0.160.0; дополнительные RPC требуют qualification |
| S02 | [Responses WebSocket mode](https://developers.openai.com/api/docs/guides/websocket-mode) | Срок соединения60мин, потеря connection-local state и способы нового соединения. Это не квота токенов и не рецепт внешнего повторения Codex prompt |
| S03 | [Conversation state](https://developers.openai.com/api/docs/guides/conversation-state) | Состояние response/conversation и клиентского transcript различается; хранение и доступность надо проверять, не угадывать |
| S04 | [Background mode](https://developers.openai.com/api/docs/guides/background) | Для поддержанного API можно наблюдать/получать прежнюю генерацию, а не делать новый POST ради хвоста. Background не предполагается для всех совместимых API |
| S05 | [API error codes](https://developers.openai.com/api/docs/guides/error-codes) | Одинаковый HTTP status имеет разные provider codes; временный rate limit не равен spend/balance restriction. Применяется к OpenAI API, не к любому подписочному SDK автоматически |
| S25 | [Chat Completions reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) | choices/index, finish_reason, tool calls и usage chunks имеют отдельные границы; потеря usage не отменяет полный ответ |
| S26 | [Structured outputs/refusals](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses), [Reasoning/incomplete response](https://developers.openai.com/api/docs/guides/reasoning) | Refusal content и incomplete_details.reason не являются отдельными raw response statuses |

Решение Atlas: native SDK retry остаётся у SDK; наружный слой сохраняет IDs,
обработку результата и effects. History/transport recovery отделены от terminal
continuation, которая требует своего доказанного контракта.

## 2. Claude Agent SDK и прямой Claude API — разные режимы

| ID | Первичный источник | Что использовано / граница |
|---|---|---|
| S06 | [Agent SDK: Work with sessions](https://code.claude.com/docs/en/agent-sdk/sessions) | Exact resume ID, continue/resume/fork, storage/cwd. Наличие SessionStore в SDK0.2.159 не является proof restore конкретной конфигурации; квалификация Atlas описана в CLAUDE-COLD-RESTORE. MirrorErrorMessage не позволяет считать любой mirror полным backup |
| S07 | [Agent SDK: Stream responses](https://code.claude.com/docs/en/agent-sdk/streaming-output) | Partial stream, complete content blocks и финальный ResultMessage различаются; текстовый блок не завершает весь SDK запрос |
| S08 | [Claude API errors](https://platform.claude.com/docs/en/api/errors) | Typed errors, request ID, отдельные rate/billing/auth/overload причины. API errors не заменяют состояние входа Claude Code subscription |
| S09 | [Claude API streaming](https://platform.claude.com/docs/en/build-with-claude/streaming) | Поток может оборваться после начала; способы текстовой continuation зависят от модели. Tool/thinking partial blocks не восстанавливаются как готовый tool call |
| S10 | [Claude API prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) | Раздельные cache creation/read usage и зависимость caching от input prefix/configuration; cache не заменяет durable ledger |
| S27 | [Claude environment variables](https://code.claude.com/docs/en/env-vars) | RESUME_INTERRUPTED_TURN допускает продолжение без host prompt; target history-only restore должен его отключать и квалифицировать отсутствие действий |
| S28 | [Agent SDK Python reference](https://code.claude.com/docs/en/agent-sdk/python) | Interrupt оставляет buffered сообщения прежнего query; перед новым вводом их надо дочитать. Pinned SDK source дополнительно проверен на ResultMessage.origin/terminal_reason и ConversationResetMessage |
| S29 | [Claude server tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/server-tools) | Paused server loop отличается от client tool_result и требует сохранения native content/server state |
| S30 | [Claude stop reasons](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons) | end_turn/tool_use/pause_turn/max_tokens/refusal/context limit определяют разные следующие шаги; message_stop не равен завершению всего Run |

Решение Atlas: две отдельные capability/adapter матрицы. Подписочный login лечится
штатным login+fresh readiness+exact session resume; прямой Messages API работает
с API credentials и сохранённой последовательностью сообщений/tool results.

## 3. DeepSeek и другие обычные API

| ID | Первичный источник | Что использовано / граница |
|---|---|---|
| S11 | [DeepSeek error codes](https://api-docs.deepseek.com/quick_start/error_codes/) | Формат/параметры, auth, balance, rate/concurrency и overload имеют разные исходы |
| S12 | [DeepSeek Responses compatibility](https://api-docs.deepseek.com/guides/responses_api/) | Stateless semantics: previous_response_id/conversation/store/background не поддерживаются; часть параметров игнорируется. Формат Responses не даёт stateful recovery OpenAI |
| S13 | [DeepSeek Rate Limit & Isolation](https://api-docs.deepseek.com/quick_start/rate_limit/) | Keep-alive comments/empty lines во время ожидания не равны output/inference. Ограничения до начала inference — provider facts; scope user/account зависит от реальной конфигурации |
| S14 | [DeepSeek Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/) | Нужные native reasoning/tool message поля следует сохранять и возвращать по правилам выбранного режима, не обрезать универсальным OpenAI serializer |
| S15 | [DeepSeek Context Caching](https://api-docs.deepseek.com/guides/kv_cache/) | Prefix matching и cache usage не являются долговременным сохранением conversation или effect ledger |
| S16 | [Gemini function calling](https://aistudio.google.com/docs/function-calling) | Tool calls/results и agent loop требуют отдельного adapter; разные Gemini endpoints не приравниваются к OpenAI API |
| S17 | [Gemini troubleshooting](https://aistudio.google.com/docs/troubleshooting) | Transient backoff и неретраебельные параметры различаются; вложенные SDK retries нужно учитывать |
| S18 | [Amazon Bedrock Converse](https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html) | Отдельная cloud/model/API identity и собственные request/message/tool структуры; единый Atlas protocol не означает универсальный upstream wire |

Это не проверка новых аккаунтов/моделей платным запросом и не установка Gemini,
Bedrock или нового DeepSeek SDK. Для каждого нового adapter нужна отдельная
conformance запись; неизвестная capability остаётся unsupported.

## 4. Распределённая надёжность и транспорт

| ID | Первичный источник | Применение в Atlas |
|---|---|---|
| S19 | [AWS: Timeouts, retries and backoff with jitter](https://builder.aws.com/content/3EumjoZascWd1oZiEgL8ORlv3qE/timeouts-retries-and-backoff-with-jitter) | Таймауты не означают отсутствие effects; повторы могут усиливать перегрузку. Один retry owner, scoped budget, backoff/jitter |
| S20 | [AWS: Making retries safe with idempotent APIs](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/) | Caller intent IDs, неизменные параметры и атомарная фиксация token+изменения; поздние запросы требуют дедупликации |
| S21 | [RFC9110 §9.2.2](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2) | Non-idempotent retry требует знания semantics/доказанного неприменения. Произвольный proxy не должен повторять business POST |
| S22 | [RFC9110 §10.2.3 Retry-After](https://www.rfc-editor.org/rfc/rfc9110.html#section-10.2.3) | HTTP-date или delay; источник времени сохраняется, повторное чтение не продлевает паузу |
| S23 | [SQLite WAL](https://www.sqlite.org/wal.html) | Один writer, WAL не исключает SQLITE_BUSY. Нужны короткие транзакции, сохранность durable facts и корректное восстановление после commit uncertainty |
| S24 | [MCP transports, version2025-06-18](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports) | Session reinitialize после expiry и optional SSE redelivery — транспорт. MCP request/session IDs не гарантируют exactly-once tool effect. Использовать фактически согласованную protocol version |

Из этих источников не следует, что для Atlas обязательно внедрять конкретный
workflow engine или распределённую СУБД. Outbox/inbox, immutable ledger и CAS можно
ввести в существующей архитектуре; HA требует отдельного fencing/storage решения.

## 5. Собственные первичные источники

Фактическая база Gate:
[`015b26f799025e583f4efda5ab47b0de3ddd8630`](https://github.com/soltyy/atlas-gate/tree/015b26f799025e583f4efda5ab47b0de3ddd8630).
База Router:
[`b214db302fe7a4104301919d80652c35ee79ed6e`](https://github.com/soltyy/atlas-router/tree/b214db302fe7a4104301919d80652c35ee79ed6e).

| Область | Реализация/существующий контракт | Что это доказывает |
|---|---|---|
| Create/alias/affinity | [Gate routing.py](https://github.com/soltyy/atlas-gate/blob/015b26f799025e583f4efda5ab47b0de3ddd8630/atlas_gate/gate/routing.py), [SESSION-CONTRACT](SESSION-CONTRACT.md) | Immutable create/rejected ACK и owner-bound alias; create не является prompt |
| Delivery/admission/receipt | [Gate operations.py](https://github.com/soltyy/atlas-gate/blob/015b26f799025e583f4efda5ab47b0de3ddd8630/atlas_gate/gate/operations.py), [RETRY-RECEIPTS](RETRY-RECEIPTS.md), [ADMISSION-FENCE](ADMISSION-FENCE-V1.md) | Ключи/квитанции и atomic not-sent proof для prompt; не exactly-once внешнего инструмента |
| Router durable events | [journal.py](https://github.com/soltyy/atlas-router/blob/b214db302fe7a4104301919d80652c35ee79ed6e/atlas_router/journal.py), [main.py](https://github.com/soltyy/atlas-router/blob/b214db302fe7a4104301919d80652c35ee79ed6e/atlas_router/main.py) | Dispatch/deferred/turn/summary receipts; чтение journal не тождественно SDK restore |
| Native owner | [Codex backend](https://github.com/soltyy/atlas-router/blob/b214db302fe7a4104301919d80652c35ee79ed6e/atlas_router/backends/codex.py), [WRITER-RECOVERY](WRITER-RECOVERY-V1.md) | Собственный доказанно idle writer передаётся, внешний/живой не уничтожается |
| Auth/no-rollout | [SESSION-RECOVERY](SESSION-RECOVERY.md), [INPUT-RECOVERY](INPUT-RECOVERY-V1.md) | Вход/transport/history раздельны; virgin proof узкий и не разрешает business replay |
| API proxy/биллинг | [upstream.py](https://github.com/soltyy/atlas-gate/blob/015b26f799025e583f4efda5ab47b0de3ddd8630/atlas_gate/gate/upstream.py), [PRICING](PRICING-V1.md) | Обычный API stream proxy и pricing не означают автоматически durable provider result retrieval |
| Connector wire gaps | [Router connector.py](https://github.com/soltyy/atlas-router/blob/b214db302fe7a4104301919d80652c35ee79ed6e/atlas_router/connector.py), [Gate node_channel.py](https://github.com/soltyy/atlas-gate/blob/015b26f799025e583f4efda5ab47b0de3ddd8630/atlas_gate/gate/node_channel.py), [input_recovery.py](https://github.com/soltyy/atlas-gate/blob/015b26f799025e583f4efda5ab47b0de3ddd8630/atlas_gate/gate/input_recovery.py) | Request header/path allowlists и потеря response headers ограничивают capability; legacy missing proof не доказывает восстановление |
| Capacity/контекст/reasoning | [ROUTER-LIMITS](ROUTER-LIMITS.md), [CONTEXT-WINDOWS](CONTEXT-WINDOWS.md), [REASONING](REASONING-V1.md) | Local resources, SDK budget, policy и provider quota имеют разные источники |
| HA и эксплуатация | [HA-ADR](HA-ADR.md), [DEPLOYMENT](DEPLOYMENT.md) | Один активный Gate/SQLite, pinned history и ограниченные гарантии failover |

SDK package versions перечитаны из установленного окружения без генерации запросов.
Read-only просмотр pinned Claude0.2.159 types/client/subprocess transport подтвердил
origin, terminal_reason, ConversationResetMessage, SessionStore/MirrorErrorMessage
и наследование env; Router backend проверен на отсутствие обработки этих recovery
границ. Это code evidence пробелов, не проверка включённого flag на production account.
Новые live/model/tool тесты при подготовке RFC не запускались. Ранее выпущенные
квитанции квалификации не выдаются за проверку предложенного coordinator API.

## 6. Открытые реализации и инциденты

- [Gate88](https://github.com/soltyy/atlas-gate/issues/88): поэтапная реализация этого
  RFC, общий controller/inventory/ACK и API-adapters. Merge документации не закрывает
  реализацию. [Router104](https://github.com/soltyy/atlas-router/issues/104): Claude
  no-action restore/origin/reset и connector conformance, найденные независимым аудитом.
- [Gate87](https://github.com/soltyy/atlas-gate/issues/87),
  [Harness502](https://github.com/soltyy/atlas_harness/issues/502): серверная передача
  writer поставлена; фактический клиентский stale-checkpoint recovery ещё требует
  реализации/приёмки. Последняя read-only сверка обнаружила завершённый terminal и
  12accepted ACK, но новые Run останавливались local binding guard до вызова Gate.
- [Gate77](https://github.com/soltyy/atlas-gate/issues/77),
  [Router96](https://github.com/soltyy/atlas-router/issues/96),
  [EDT220](https://github.com/soltyy/edt-llm-agent-extension/issues/220): отдельный
  будущий terminal continuation; нельзя назвать его уже существующим из-за resume=true.
- [Gate85](https://github.com/soltyy/atlas-gate/issues/85),
  [Router101](https://github.com/soltyy/atlas-router/issues/101),
  [Router103](https://github.com/soltyy/atlas-router/issues/103): ограниченные исправления
  input/writer и соответствующие установленные proofs, не вся матрица этого RFC.

Тикеты и SDK docs — изменяемые источники. Перед реализацией этапа перечитываются
их актуальные состояния; конфликт между proposal и текущим wire явно разрешается
version negotiation, а не молчаливым изменением поведения старого клиента.

## 7. Независимая проверка редакции RFC1

Три агента отдельно проверили подписочные SDK, обычные API и распределённый recovery
protocol. Первичные замечания устранены в документе и перечитаны соответствующим
проверяющим; замечаний, блокирующих эту редакцию документа, не осталось. Обнаруженные
пробелы **реализации** не считаются исправленными публикацией RFC.

| Направление | Что уточнено после проверки |
|---|---|
| Codex/Claude SDK | No-action restore с effective env/settings, origin/query correlation, interrupt buffer, reset lineage/usage и фактическое наличие SessionStore |
| Прямые API | Messages stop/pause_turn, Chat choice/finish/usage-tail, Responses raw status и Run, типизированный provider admission rejection после HTTP dispatch |
| Recovery protocol | Общий branch controller и unique successor, полный inventory seal, первый поздний client ACK после Stop/revision, несколько source Runs и connector wire gaps |

Статически проверены локальные links/anchors, таблицы, все три JSON-примера и их
source manifest SHA-256/plan/action consistency, уникальные E01–E92/A01–A56.
Runtime suites, сборки, SDK/model/API генерации и production restarts для этой
документационной правки не требуются и не выполнялись.
