# Единый контракт жизненного цикла и восстановления Harness — Gate — Router

Редакция RFC 1, нормативная база 02.11.2026; статус актуализирован 11.11.2026,
Europe/Moscow. Владелец общего контракта — Gate.

**Статус: нормативный контракт и целевые требования; реализация поэтапная.**
Поставленные Gate **0.4.31** / Router **0.8.24**, клиентские квитанции и точные
границы перечислены в [актуальной матрице](SESSION-RESILIENCE-STATUS.md).
Историческая база исходного рекона: Gate **0.4.19** (`015b26f`), Router **0.8.18**
(`b214db3`); установленный Codex SDK **0.160.0**, Claude Agent SDK **0.2.159**, MCP
**2.2.0**. Клиентский код Harness прочитан, но его локальная БД и установленный
клиент этим документом не меняются. Серверная поставка сама по себе не доказывает
применения результата к локальному checkpoint пользователя.

Нормативные слова **ДОЛЖЕН**, **НЕЛЬЗЯ**, **МОЖЕТ** относятся к целевой системе.
Существующий wire v1 сохраняется; основа coordinator API из раздела 8 уже поставлена.
Фактические paths/capabilities задаёт [WIRE](SESSION-RESILIENCE-WIRE.md), а
неподдержанные части остаются целевыми требованиями.
Примеры идентификаторов синтетические. Секретов, переписки и SDK history в документе нет.

План реализации: [Gate #88](https://github.com/soltyy/atlas-gate/issues/88).
SDK/connector требования: [Router #104](https://github.com/soltyy/atlas-router/issues/104).

## Навигация

- [1. Цель и пределы гарантий](#1-цель-и-пределы-гарантий)
- [2. Топологии и границы сторон](#2-топологии-и-границы-сторон)
- [3. Идентичность и источники истины](#3-идентичность-и-источники-истины)
- [4. Инварианты безопасности](#4-инварианты-безопасности)
- [5. Независимые автоматы состояний](#5-независимые-автоматы-состояний)
- [6. Штатный порядок операций](#6-штатный-порядок-операций)
- [7. Общий алгоритм восстановления](#7-общий-алгоритм-восстановления)
- [8. Целевой API координатора Gate](#8-целевой-api-координатора-gate)
- [9. Владение SDK и передача транспорта](#9-владение-sdk-и-передача-транспорта)
- [10. Особенности SDK и обычных API](#10-особенности-sdk-и-обычных-api)
- [11. Ошибки, повторы и сроки](#11-ошибки-повторы-и-сроки)
- [12. Пользовательские действия и фоновые задания](#12-пользовательские-действия-и-фоновые-задания)
- [13. Хранение, учёт, доступ и эксплуатация](#13-хранение-учёт-доступ-и-эксплуатация)
- [14. Совместимость и план внедрения](#14-совместимость-и-план-внедрения)
- [15. Приёмка и работа дежурного](#15-приёмка-и-работа-дежурного)

Неотъемлемые приложения:

1. [Матрица сбоев, решений и владельцев](SESSION-RESILIENCE-ERRORS.md).
2. [Сценарии приёмки и выбор необходимых проверок](SESSION-RESILIENCE-ACCEPTANCE.md).
3. [Первичные источники и проверенная реализация](SESSION-RESILIENCE-SOURCES.md).
4. [Действующие HTTP-пути и отличия от целевого API](SESSION-RESILIENCE-WIRE.md).
5. [Поставленные возможности и клиентские границы](SESSION-RESILIENCE-STATUS.md).

## 1. Цель и пределы гарантий

Пользователь открывает старую беседу, нажимает «Продолжить», меняет агента,
останавливает выполнение или возвращается после потери связи. Система сама
сверяет прежнюю работу, восстанавливает доступный результат и подготавливает
следующее безопасное действие. Обычная временная неисправность не должна
превращать беседу в постоянную ошибку, требующую очистки истории руками.

Приоритеты: **сохранность результатов → отсутствие повторных побочных эффектов →
восстановление хода → удобство и скорость**. Самовосстановление означает выполнение
всех доказанно безопасных шагов, а не безусловный повтор исходного поручения.

Абсолютное exactly-once исполнение произвольной команды во внешней системе
невозможно обещать: процесс может завершиться после перевода денег/отправки письма,
но до фиксации результата. Требуются ключ операции внешнего сервиса, его журнал
или проверка фактического состояния. Если их нет, система сохраняет неопределённость,
изолирует спорную операцию и предоставляет работающий путь сверки/решения.
Ни отсутствие ACK, ни HTTP 404, ни долгий простой не доказывают неисполнение.

Гарантии действуют при сохранности журналов, ключей доступа и истории в пределах
объявленного хранения. Потеря всех реплик, отзыв доступа, прекращение работы
поставщика или недоступность внешнего исполнителя не устраняются сменой SDK-сессии.
Даже в этих случаях остаются экспорт доказательств, доступ к разрешённой истории,
работа с независимыми задачами и явная процедура решения, а не вечный spinner.

## 2. Топологии и границы сторон

| Топология | Владелец генерации и истории модели | Что восстанавливает Gate |
|---|---|---|
| Harness/EDT → Gate → Router → Codex SDK, подписка | Router, закреплённый аккаунт и SDK home | Ledger/owner/доступ; поручает Router восстановить транспорт и прочитать историю |
| Harness/EDT → Gate → Router → Claude Agent SDK, подписка | Router, профиль Claude, точный session ID и окружение | То же; отдельно наблюдает восстановление входа |
| Harness → Gate → API OpenAI, Anthropic, DeepSeek и др. | Provider response/conversation либо канонический transcript клиента — по возможностям API | Собственный admission/receipt/billing; retrieval/cancel только при поддержке upstream |
| Harness → Router напрямую | Router и клиент; Gate отсутствует | Не участвует. Router реализует соответствующие гарантии самостоятельно |
| Harness → API напрямую | Клиент и provider | Не участвует. Клиент содержит тот же outbox, tool ledger и recovery adapter |
| Gate → исходящий connector → Router | Router; connector хранит доставку, не смысл задачи | Сверяет доставку по прежнему key, node identity и Router receipt |
| SDK/клиент → MCP, внешний инструмент, локальный worker | Конкретный исполнитель и его effect ledger | Связывает доказательства, но не выдумывает outcome за исполнителя |

**Gate остаётся SDK-free.** Он не импортирует Claude/Codex, не правит их transcripts,
не входит в аккаунт от имени пользователя и не обращается к памяти Python Router.
Он — координатор доказательств и переходов. Native операции выполняет Router/адаптер.

**Harness/EDT** хранит пользовательские сообщения, очередь ввода, Run, outbox,
точные результаты исполненных инструментов и состояние применения ACK в своей БД.
Gate не может восстановить несохранённый локальный результат по одному callId.
Клиент сообщает ему manifest старых операций даже при несовпадении нового агента.

**Router** хранит связь logical history ↔ native history, журнал SDK-команд,
события/результаты, writer ownership, возможности и вход аккаунта. Он не выбирает
сам новую организацию/учётную запись и не переисполняет пользовательские host tools.

**API-adapter** может находиться на Gate либо в клиенте. Его stateful/stateless,
retrieval, cancel, retry и streaming свойства задаются отдельно для endpoint/model.
Наличие OpenAI-совместимого JSON не является доказательством семантической совместимости.

## 3. Идентичность и источники истины

### 3.1. Не смешивать идентификаторы

| Сущность | Стабильная идентичность | Жизненный цикл |
|---|---|---|
| Беседа/ветвь | `conversationId`, `branchId` клиента | Переживает смену модели, транспорта и Run |
| Run и пользовательский intent | `runId`, `intentId`, источник и номер очереди | Новый intent — осознанная новая работа, не маскировка повтора |
| HTTP-команда | `requestId`/Idempotency-Key + канонический body hash | Неизменны до получения окончательной квитанции |
| Gate binding | Публичный `sessionId` | Владелец device/org, route/model, node/account affinity |
| SDK history | Непрозрачный Gate alias; native ID известен Router | Отличается от HTTP SID; закрытие транспорта не удаляет историю |
| Ход | `(binding, turnId)` | От admission до terminal/unknown; не равен одному HTTP-запросу |
| Инструмент/эффект | `(history, turnId, callId)`, дополнительно domain `effectId` | От решения выполнить до доказательства внешнего результата |
| Наблюдение | `(binding, turnId, seq)` + consumer cursor | Повторная доставка допустима, повторное исполнение — нет |
| Попытка upstream | `attemptId`, provider request/response ID | Отличать несколько оплаченных попыток от одного logical intent |
| Владение | `nodeId`, `bootId`, account/history lineage, lease generation | Boot и heartbeat не дают права обойти native writer lock |
| Recovery case | `recoveryId`, revision, immutable source manifest | Не является новым business intent |

У SDK provider ID сохраняются **original** и **resolved** значения. При compact,
fork или переносе может измениться native ID; lineage и старые квитанции не исчезают.
Клиент через Gate не должен требовать native ID либо выдавать alias за Router SID.
Canonical history identity включает provider account, adapter, storage identity и
branch lineage, не один UUID. Одинаковые ID из разных SDK homes/аккаунтов не дают
права объединить histories. Физический путь SDK home и credentials остаются приватными
Router; Gate получает проверяемую storage/account identity, клиент — opaque alias.

### 3.2. Чей факт является окончательным

| Факт | Источник доказательства |
|---|---|
| Инструмент запущен/выполнен, внешний объект создан | Durable ledger исполнителя + внешняя квитанция/read-back |
| Результат инструмента принят | Сохранённый ACK принимающего слоя, точный callId/body hash |
| Prompt принят Gate/Router | Admission ledger + immutable command receipt |
| SDK получил запрос | Dispatch/native receipt; HTTP 202 Gate само по себе недостаточно |
| Ход закончился | Терминальный журнал SDK/Router, привязанный к exact turn |
| Пользовательское задание выполнено | Проверка результата по условиям задачи; не просто текст модели |
| Пользователь нажал Stop | Durable control intent с target turn и cancellation epoch |
| Вход/квота восстановлены | Свежая telemetry правильного аккаунта; прежний error banner не источник истины |
| Восстановление применено клиентом | Локальная транзакция + client-applied ACK координатору |

`acknowledged`, `terminal`, `successful`, `verified` — четыре разных признака.
Например, accepted tool_result может сопровождаться failed SDK turn; success SDK
не доказывает доставку письма адресату или достижение бизнес-цели.

## 4. Инварианты безопасности

**INV-01.** Не менять исходный key, body, owner или target при unknown outcome.
Одинаковый key и другие параметры — конфликт, не новый запрос.

**INV-02.** ACK/события/результаты пишутся durable до уведомления следующей стороны.
Инструмент имеет write-ahead запись ДО side effect и сохранённый output ДО отправки ACK.

**INV-03.** Проверка полномочий обязательна и для recovery. Ошибка сессии не отменяет
device/org/data-class/route/branch/document policy, отзыв узла или устройства.

**INV-04.** Один native writer на историю. Истечение lease, отсутствие Run в UI или
возраст процесса не доказывают, что его можно завершить или удалить lock-файл.

**INV-05.** Наблюдение terminal результата закрытого binding не требует открытия SDK,
нового login, генерации, оплаты новой попытки или восстановления runtime-сессии.

**INV-06.** Transport restore не отправляет prompt и не допускает скрытого native
auto-continuation/inference/tools. Одного отсутствия host query недостаточно: adapter
доказывает отсутствие исполнения либо объявляет такой restore unsupported. History
resume не равен продолжению терминально упавшего задания. Context replay не означает
повтор tool calls.

**INV-07.** Все повторы ограничены конкретным scope: чтение, transport, точная доставка
saved output, SDK internal retry или явная continuation. Одного `retryable:true` мало.

**INV-08.** Stop/закрытие/переподключение/удаление истории — разные команды. Stop имеет
приоритет над автоматическим продолжением своего intent и не откатывает side effects.

**INV-09.** Поздний callback старого turn/generation не изменяет новый Run, cursor,
writer, окончательную причину завершения или учёт расхода.

**INV-10.** Не очищать неопределённый checkpoint ради снятия блокировки. Его можно
закрыть доказательством или изолировать с явным unresolved-effect record.

**INV-11.** Новый transport на другой node/account не восстанавливает автоматически
старую SDK history. Failover новой работы и миграция существующей истории — разные процедуры.

**INV-12.** Никакое краткое summary не заменяет call/result ledger, control intents,
разрешения и ссылки на оригиналы. Именно они защищают от повторения действий.

**INV-13.** Сохранять первое первичное исключение и цепочку причин. Временный 429,
истёкший WebSocket и quota exhausted не объединять в один лимит подписки.

**INV-14.** Нулевой/пустой ответ, EOF или HTTP 200 не являются успешным terminal.
Нужен определённый adapter-specific завершитель, включая outcome/usage availability.

**INV-15.** Восстановление завершается видимым исходом. Допустимы ожидание внешнего
условия, отделение независимой работы и запрос конкретного решения, но не вечный
spinner и не повтор одного и того же guard без действий по сверке.

## 5. Независимые автоматы состояний

### 5.1. Состояния

| Автомат | Состояния и важные различия |
|---|---|
| Transport | absent → opening → ready → draining/closing → closed; error/unknown отдельно |
| History | confirmed / virgin-confirmed / unconfirmed / unavailable / explicitly-deleted |
| Command | prepared → admitted → dispatching → accepted → terminal; rejected-not-sent либо outcome-unknown |
| Tool effect | planned → claimed → executing → output-durable → result-delivering → accepted; effect-unknown отдельно |
| Run | queued → active → waiting-tools/provider/auth/recovery → terminal-success/error/cancelled |
| Result delivery | recorded → client-pending → client-applied; server terminal не означает client-applied |
| Recovery case | collecting → reconciling → waiting → plan_ready → applying → completed; blocked/needs_input отдельно |
| Scheduled task | enabled/paused/deleted; occurrences имеют отдельные intent IDs и исходы |

Занятая SDK history может иметь `activeTurns=0`: idle transport всё ещё держит
writer. Открытая сессия может не иметь persisted rollout. Закрытая HTTP-сессия
может иметь доступный полный terminal журнал. Эти случаи не противоречат друг другу.

### 5.2. Приоритет фактов

Терминальное состояние конкретного поколения неизменно. Позднее чтение уточняет
доказательства, но не воскрешает отменённый intent. При гонке Stop/completion
сохраняются **оба** факта: действие могло завершиться до применения Stop.
Нельзя заменить успешный terminal техническим EOF и нельзя скрыть реальное SDK
исключение под сообщением «отменено», если causal order этого не подтверждает.

При отсутствии надёжного порядка ставится `completionOrder=unknown`; это не повод
переисполнить инструмент. Новая попытка continuation имеет собственный attempt ID
и ссылку на исходный terminal, а не переписывает его.

## 6. Штатный порядок операций

### 6.1. Открытие беседы и подготовка следующего ввода

1. Клиент загружает локальные messages/inbox/outbox/Run и все pending checkpoints.
2. Проверяет доступ и профиль. Политика нового агента не подменяет identity старых операций.
3. **До fatal guard несовпадения binding** вызывает координатор со старым manifest.
4. Gate читает прежние receipts/events, сверяет Router и формирует безопасный план.
5. Клиент транзакционно применяет восстановленные результаты, закрывает доказанно
   завершённые ожидания. Неразрешённые эффекты остаются явно изолированы.
6. Только затем согласуются новые route/model/reasoning/tools и новый Run.
7. Resume требует подтверждения истории и exact node возможностей. Недоступность
   coordinator у legacy стороны не разрешает очистку checkpoints.

Это обязательный handshake на startup, открытии старой беседы, отправке после
ошибки, входе SDK, восстановлении сети, Stop и смене агента. Если Harness вообще
не делает сетевого запроса, Gate физически не может исправить его SQLite-состояние.
Полная автоматизация требует небольшой, но обязательной клиентской части.

### 6.2. Create/resume

Клиент сохраняет неизменный create body/key, затем отправляет существующий
`POST /harness/agent/sessions`. Alias принадлежит прежним device/org/route/model/node.
ACK берётся из сохранённого исхода; `resumed:true` подтверждает именно history.
Create ACK v1 не обязан содержать sdkSessionId: alias читается из session list/event.

При complete rejected create новая допустимая попытка получает новый create key
со ссылкой на отказ. Старый key всегда возвращает старый исход. При unknown сначала
сверяется именно старый key; новый key не используется для обхода неизвестности.
Наличие auth/capacity до admission позволяет ждать и снова проверить прежний ещё
не принятый intent. Срок ожидания не меняет неизменяемую уже выданную квитанцию.

### 6.3. Prompt и поток

Сохранить prompt intent/body hash; admission в Gate; durable forwarding/dispatch
в Router/adapter; сохранить ACK; читать exact turn. Для managed recovery использовать
async producer и отдельного consumer. Disconnect consumer не должен отменять producer
в целевом режиме detach; legacy direct Router SSE сейчас может отменять ход.
Поведение `disconnectPolicy` должно быть согласовано, а не угадано клиентом.

Сначала journal append, затем SSE/poll/WS delivery. Клиент дедуплицирует по tuple
session/turn/seq, сохраняет cursor вместе с применёнными событиями. Несколько
наблюдателей не создают несколько producers. Пропуск seq требует дочитывания;
partial page без terminal не завершает Run.

### 6.4. Инструмент и ACK

Исполнитель сохраняет callId, schema/arguments hash, effectId, класс повторяемости
и claim. После выполнения — точный output, artifact hashes и внешний receipt.
Только затем отправляет tool_result с устойчивым key. При потере ACK клиент
читает исходную квитанцию. Повтор доставки того же сохранённого вывода
разрешён по доказательству для конкретного callback. Входной отказ истёкшего
токена требует обновления доступа прежнего устройства и сверки исходной
привязки. Принятый ACK, закрытие callback и terminal всего хода хранятся как
отдельные факты. [Действующий wire](SESSION-RESILIENCE-WIRE.md#результаты-инструментов-в-активном-ходе)

Классы инструмента объявляет доверенный исполнитель, а не модель:

| Класс | Что допустимо после сбоя |
|---|---|
| Чистое чтение | Повтор чтения по политике freshness; старый результат не подменять новым незаметно |
| Идемпотентная запись | Повтор только с тем же external effect key и теми же параметрами |
| Неидемпотентная, есть lookup | Read-back по внешнему ID; восстановить существующий результат |
| Неидемпотентная, lookup нет | Effect-unknown; сверка/решение, запрет автоматического повторения |
| Локальная транзакция/файл | Проверить transaction ID, revision/content hash и результат штатного сохранения |

Тот же callId с другими arguments — конфликт. Другое имя вызова/новый callId не
доказывает новую бизнес-операцию: для отправки/оплаты/публикации нужен domain effectId.
Дедупликация по одному тексту или имени инструмента запрещена.

### 6.5. Terminal и закрытие

Router сохраняет terminal и summary receipt; полный text/media/context может
находиться в paginated events/artifact store. Клиент дочитывает требуемые данные,
применяет их один раз и подтверждает client-applied. Usage reconcile независим
от отображения. Старый transport можно штатно закрыть, сохранив ledger/history.
Истёкший полный журнал и доступный только terminal summary показываются раздельно.

### 6.6. Stop, steer и очередь

Stop сначала сохраняется с exact target turn/generation, затем доставляется.
ACK interrupt означает приём команды; окончание подтверждает terminal/settled state.
Повтор доставки Stop использует старый key; новый Stop нового turn — новый key.
Целевой v2 control **не должен** отменять текущий новый turn из-за запоздавшего
старого session-scoped interrupt. Для legacy контракта эту гонку нельзя скрывать.

Stop до dispatch закрывает допуск queued prompt. Stop после side effect не делает
его неисполненным. Отменённый intent автоматически не продолжается после login,
перезапуска клиента или смены модели. Поздний tool_result дочитывается в старый ledger.
Steer имеет отдельный ID и признаки accepted/absorbed; неприменённый steer не считается
частью результата. Новый queued input имеет собственный intent, а не затирает старый.

## 7. Общий алгоритм восстановления

### 7.1. Порядок, обязательный для Gate

**R1 — заморозить новый dispatch этой ветви, а не весь клиент.** Разрешены чтение,
сверка ACK и Stop. Независимые ветви могут продолжать работу по политике доступа.

**R2 — собрать immutable source manifest.** Старые run/key/body hash/session/turn,
cursor, alias, root, owner, model и outbox остаются прежними, даже если UI уже выбрал
другого агента. Отсутствующее поле — неизвестность, не разрешение заполнить его новым.

**R3 — прочитать доказательства.** Gate ledger → exact Router/adapter receipt →
exact turn events/terminal → client executor/external effect evidence. Чтение
не создаёт SDK transport. Разрешены только данные законного владельца.

**R4 — классифицировать каждую операцию независимо.** `complete`, `accepted-running`,
`rejected-not-sent`, `sealed-not-sent`, `effect-unknown`, `history-unavailable` и
`forbidden` не сливаются в общий «запрос не выполнен».

**R5 — выполнить все безопасные восстановления.** Получить готовый output, повторить
фиксацию ACK, дочитать события, завершить прежний checkpoint, дождаться auth/provider,
передать idle writer или восстановить transport на прежней истории. Не запрашивать
повторно одобрение уже разрешённой операции чтения/сверки.

**R6 — закрыть отрицательную неопределённость только доказательством.** Для prompt
без admission использовать существующий atomic admission fence по исходным
key/turn/body hash. Обычный 404 не является fence. Для tool effect отсутствие
записи Gate не доказывает отсутствие внешнего действия.

**R7 — зафиксировать план и fencing.** Revision/CAS, source manifest hash, policy
revision и общий branch recovery generation/control epoch сериализуют все cases
исполняющей ветви, включая разные manifests из разных окон. Одного CAS внутри case
недостаточно для исключения двух последовательных продолжений одного source turn.
Сетевые и SDK-вызовы выполняются вне длинной SQLite-транзакции; их шаги имеют
собственные immutable IDs и сверку после неизвестного ответа.

**R8 — применить клиентскую часть.** Gate присылает конкретные records/receipts и
порядок применения. Harness транзакционно помечает старый checkpoint resolved,
добавляет недостающий результат и освобождает новый Run. ACK этого шага сохраняется.
Gate не сообщает `completed`, пока обязательная клиентская часть не подтверждена.

**R9 — выдать доступный следующий шаг.** `ready`, `waiting(condition)`, `needs_input`
с точным действием или `blocked` с доказательствами. На отсутствующие history/output
есть export/restore/import/изолированная новая ветвь; на auth — вход; на revoked —
администратор. Нельзя бесконечно повторять тот же local binding guard.

### 7.2. Случай Зинаиды как обязательный общий сценарий

У старого Run сохранены terminal success и 12 принятых tool ACK. Следующий transport
не отправил prompt. Новый клиентский Run упирается в checkpoint прежней binding/Run
после Stop/смены агента. Смена модели обратно не устраняет сам checkpoint.

Gate должен связать старые receipts с manifest, вернуть output/terminal evidence и
план завершения прежнего ожидания. Harness применяет его под **старой** identity,
не записывает эти 12 инструментов в очередь исполнения нового Run. Затем создаётся
новый transport с прежней историей и разрешёнными текущими настройками. Если в
manifest остаётся непредъявленный prompt, сначала его receipt/fence, а не очистка.
Такой алгоритм нужен и для Claude SDK, и для прямого API; их proof различается.

### 7.3. Неопределённый внешний эффект

Неопределённость конкретного письма/платежа не должна блокировать всю программу.
Система сохраняет unresolved effect, прекращает dispatch зависимых действий,
предлагает read-back у внешнего сервиса, подтверждение существующего результата
или явное решение оператора. Независимая новая ветвь получает reference на этот
эффект и запрет дублирования. Компенсация — отдельная разрешённая операция, а не
автоматический «откат» SDK history.

## 8. Целевой API координатора Gate

### 8.1. Объявление и совместимость

Подписанный `sessionRecoveryCoordinator.version=1` поставлен начиная с Gate0.4.20;
в исторической базе0.4.19 его не было. Это отдельная capability, не повышение
версии существующих commandRecovery/transportRecovery/inputRecovery.

Capability объявляет supported operations, topology, replay policy, retention,
paths и минимум версии клиента. Router публикует отдельные реальные возможности
proof/owner/history/continuation. Gate проектирует их на exact pinned node; объединение
в discovery не гарантирует возможность resume на любом узле. При отсутствии поддержки
поле supported=false/отсутствует, никакого синтетического разрешения.

### 8.2. Предлагаемые endpoints

Таблица описывает целевую форму. Поставленные методы, request schemas и результаты
сверяются по [WIRE](SESSION-RESILIENCE-WIRE.md) и подписанному профилю; не все
целевые методы/адаптеры ниже объявлены реализованными.

| Сторона | Метод/путь | Семантика |
|---|---|---|
| Harness → Gate | `POST /harness/recovery/inventories` | Заморозить branch admission и объявить полный snapshot source references всех pending checkpoints данного client store |
| Harness → Gate | `POST /harness/recovery/inventories/{id}/seal` | Подтвердить регистрацию всех объявленных sources и зафиксировать inventory version; не дать разрешение на prompt |
| Harness → Gate | `POST /harness/recovery/cases` | Создать/получить case по immutable manifest и Idempotency-Key; не выполнить бизнес-поручение |
| Harness → Gate | `GET /harness/recovery/cases/{recoveryId}` | Durable snapshot/plan/evidence; безопасное повторное чтение |
| Harness → Gate | `POST .../{recoveryId}/evidence` | Append-only доказательства/claims по прежним IDs, без изменения source manifest |
| Harness → Gate | `POST .../{recoveryId}/apply` | CAS-подтверждение конкретного плана; transport/read/delivery шаги, не исходный prompt |
| Harness → Gate | `POST .../{recoveryId}/ack` | Добавить историческое evidence локального применения immutable plan/actions; не разрешить новое исполнение |
| Harness → Gate | `POST .../{recoveryId}/cancel` | Прекратить auto recovery этого intent; сохранить ledger и выполненные эффекты |
| Gate → Router | `POST /v1/recovery/cases` | Привязать case к собственным exact session/turn/history; вернуть proof/план без generation |
| Gate → Router | `GET /v1/recovery/cases/{id}` | Наблюдать owner/auth/history/receipts/terminal |
| Gate → Router | `POST .../{id}/apply` | Идемпотентный native transport/owner шаг с expected boot/generation |

Ни один GET не создаёт SDK, не посылает prompt, не продлевает cooldown и не списывает
usage. Для прямого API Gate вызывает собственный adapter вместо выдуманного Router.
Для прямого Router клиента тот же owner/ledger алгоритм доступен без Gate namespace.

### 8.3. Manifest — обязательные группы данных

Один case относится строго к **одному source Run и одной source binding**. Все
операции manifest принадлежат этой паре; иной source отклоняется, не приписывается
первому. При нескольких pending Runs/bindings клиент предъявляет отдельные manifests,
а Gate регистрирует их у общего durable branch controller. Его identity определяется
owner/org и canonical conversation/branch lineage, а не выбранной новой моделью,
новым Run или случайным recovery key. Новый device требует разрешённого ACL/handoff.

Controller хранит `recoveryGeneration`, `controlEpoch`, claim и список unresolved
source cases. Новый зависимый dispatch запрещён, пока эти cases не reconciled либо
явно изолированы по правилам §7.3. Разные immutable manifests/key одного source могут
дать разные cases, но **не независимые права на продолжение**: apply, transport open
и successor dispatch проверяют общие generation/epoch/policy. Транзакционная выдача
claim и запись шага исключают конкурирующее применение; lease сам не отменяет native
owner. Дополнительное evidence не переписывает manifest уже существующего case.

Перечисление sources имеет **барьер полноты**. До регистрации первого case клиент
одной локальной snapshot-транзакцией фиксирует `clientStoreEpoch`, checkpoint inventory
version и полный список source references. `POST inventories` атомарно закрывает
зависимый admission ветви и сохраняет этот объявленный список под стабильным key.
Каждый case связывается с inventory/sourceRef; `seal` допустим лишь когда все refs
зарегистрированы и сервер добавил известные ему незакрытые операции ветви. Ни завершение
первого case, ни unresolved=0 до seal не дают ready. Immutable sealed inventory hash
и generation входят в plan/admission fence; после сверки всех источников либо их
явной изоляции controller может разрешить следующий шаг.

Новый pending source или изменение checkpoint snapshot во время recovery открывает
новую inventory version, увеличивает generation и закрывает старый допуск до нового
seal. Stop также инвалидирует допуск через control epoch. Crash при регистрации
восстанавливает тот же inventory/key, не выдаёт пустой список. Сервер не может
технически доказать полноту скрытой/утраченной клиентской SQLite: клиент обязан
предъявлять все свои checkpoints, handoff другого device требует его inventory
либо явной quarantine. Протокол не выдаёт отсутствие этих данных за not_sent.

| Группа | Поля и проверка |
|---|---|
| Версия/источник | version, inventoryId/sourceRef, conversationId, branchId, sourceRunId, trigger; schema version проверяется до действий |
| Прежняя binding | Gate root, public session ID, history alias, route/model; node/boot только известные клиенту |
| Операции | kind, original requestId, turnId/callId, body hash, local state, saved-output reference |
| Наблюдение | lastAppliedSeq, terminalKnown, client-applied markers и hashes |
| Управление | Stop/cancel intent и epoch, pending agent/model/reasoning change, next intent reference |
| Исполнитель | Наличие durable output/effect evidence и доступность owner device |
| Условия | expected recovery revision, policy/profile version, разрешённые transport-only действия |

Owner/org определяется аутентификацией и ledger, а не доверенным полем клиента.
Gate не принимает произвольный URL как инструкцию обращаться к прежнему root:
только собственная/явно зарегистрированная federation identity. Иной Gate требует
отдельной авторизации и передачи доказательств; это не SSRF и не обход изоляции.

Manifest содержит **ссылки и хеши**. Большие/чувствительные outputs передаются
отдельно в защищённое content-addressed хранилище по разрешению, а не в error message.
Хеш без доступного payload позволяет сравнение, но не восстановление содержимого.

Хеш manifest использует явно названный алгоритм `json-sort-keys-compact-utf8-v1`
из [admission fence v1](ADMISSION-FENCE-V1.md): UTF-8, сортировка ключей, неизменный
порядок arrays, без NaN/Infinity, без добавления defaults и без заявления RFC8785.
Очередной факт добавляется через evidence с собственным key, а не подменой manifest.
Сообщение клиента «выполнено» — claim; доверие к executor, его durable receipt и
external read-back проверяются отдельно. Attestation оператора сохраняется как
явное решение с авторством, а не выдаётся за техническое доказательство.

Пример manifest для `POST /harness/recovery/cases` (новый recovery key, не key
старого prompt/tool_result; `outputRef` — opaque локальная ссылка, не URL для SSRF):

```json
{
  "version": 1,
  "inventoryId": "inventory-example",
  "sourceRef": "source-example",
  "conversationId": "conversation-example",
  "branchId": "branch-example",
  "sourceRunId": "run-old",
  "trigger": "open_conversation",
  "source": {
    "gateRoot": "https://gate.example",
    "sessionId": "session-old",
    "historyAlias": "history-alias",
    "routeId": "route-example",
    "model": "model-example"
  },
  "operations": [
    {"kind": "tool_result", "requestId": "old-tool-result", "turnId": "turn-old", "callId": "call-old", "bodySha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "localState": "output_durable", "outputRef": "local-output-01"},
    {"kind": "prompt", "requestId": "old-prompt", "turnId": "turn-old", "bodySha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc", "localState": "outcome_unknown"}
  ],
  "observation": {"turnId": "turn-old", "lastAppliedSeq": 71},
  "control": {"cancellationEpoch": 4, "cancelledIntentIds": ["intent-old"]},
  "desired": {"routeId": "route-example", "model": "model-example"},
  "permissions": {"allowedRecovery": ["read_receipt", "apply_saved_ack", "resolve_source_checkpoint", "resume_transport"], "businessReplayAllowed": false}
}
```

Client permissions в manifest только **сужают** серверную policy, не расширяют её.
Local output/body hashes примера синтетические; в реальной операции проверяются
по исходным сохранённым payload, а не генерируются из восстановленного текста.

### 8.4. Пример ответа: клиенту осталось применить готовые доказательства

Это **целевой пример**, не ответ текущего endpoint:

```json
{
  "version": 1,
  "recoveryId": "recovery-example",
  "revision": 7,
  "planId": "plan-example-7",
  "branchFence": {"recoveryGeneration": 12, "controlEpoch": 4},
  "inventory": {"id": "inventory-example", "version": 2, "sealed": true, "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
  "sourceManifestSha256": "07140ff7c16770b196bdca8c53f11efc4895e3444509486944df3590475e85c6",
  "state": "plan_ready",
  "operations": [
    {"requestId": "old-tool-result", "kind": "tool_result", "outcome": "accepted", "evidenceId": "ack-example"},
    {"requestId": "old-prompt", "kind": "prompt", "outcome": "terminal_success", "evidenceId": "terminal-example"}
  ],
  "actions": [
    {"id": "apply-ack", "executor": "harness", "kind": "apply_saved_ack", "evidenceId": "ack-example", "evidenceSha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd", "sourceRunId": "run-old", "sourceSessionId": "session-old", "sourceCheckpointVersion": 3},
    {"id": "resolve-old", "executor": "harness", "kind": "resolve_source_checkpoint", "evidenceId": "terminal-example", "evidenceSha256": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee", "sourceRunId": "run-old", "sourceSessionId": "session-old", "sourceCheckpointVersion": 3},
    {"id": "open-transport", "executor": "gate", "kind": "resume_transport", "dependsOn": ["resolve-old"]}
  ],
  "resume": {"history": "confirmed", "businessReplayAllowed": false},
  "next": {"state": "awaiting_client_apply", "businessDispatchAllowed": false}
}
```

`evidenceId` — locator внутри авторизованного case, не bearer permission. Evidence
содержит issuer/source, operation tuple, output/body hashes, terminal/ACK scope,
store epoch и время наблюдения. Snapshot — не отдельно переносимый JWS-токен.
Cross-Gate federation потребует отдельной подписи, audience и проверки revocation.

### 8.5. Ответы, CAS и выполнение плана

| HTTP | Тело/значение | Действие клиента |
|---|---|---|
| 200 | Case найден; state может быть waiting/plan_ready/completed/needs_input | Читать семантическое state, не считать recovery успешным по HTTP |
| 202 | Асинхронная сверка, recoveryId и snapshot path | Опрос/события того же case; не новый prompt |
| 409 | `recovery_revision_conflict`, manifest/key conflict | Получить прежний case, сверить изменения; не заменить immutable body |
| 401/403/404 | Доступ/скрытие чужого объекта | Штатный auth/admin путь, не другая учётная запись |
| 410 | Удалённое/истёкшее доказательство | `evidence_unavailable`, восстановить backup/исполнителя либо решение |
| 422 | Неполный/неподдержанный manifest | Сохранить состояние, запросить нужные поля/обновление клиента |
| 503 | Координатор/зависимость недоступны | Сохранить case, backoff/Retry-After; unknown не превращать в not_sent |

Каждый изменяющий серверное/native состояние apply содержит immutable plan ID/revision,
source hash, стабильный action key и ожидаемые owner, общие branch generation/control
epoch, policy revision. Повтор идентичного apply возвращает сохранённый step receipt.
Изменившийся plan требует новой revision и пересчёта, а не выдачи старого permit.
Сначала сохраняется step intent; потом native действие; потом его наблюдаемый исход.
Crash между ними вызывает reconciliation, не повтор SDK-команды вслепую.
После проверки доступа поиск неизменяемой уже выданной квитанции выполняется **до**
проверки новой текущей revision: повтор ранее принятого apply/ack должен получить
свой исход и после продвижения case. Для нового state-changing шага CAS обязателен;
исторический client-applied ACK имеет отдельные правила ниже. GET snapshot
отдаёт private/no-store; ETag/revision может применяться для условного наблюдения.
Stop/cancel epoch входит в fencing всех ещё не начатых шагов плана.

В apply передаются только IDs действий **сохранённого** плана; клиент не может
подставить новый kind/URL/SDK payload. Plan/action descriptors неизменяемы: action ID,
source tuple/checkpoint version и evidence hash не получают другого смысла в новой
revision. Harness одной локальной транзакцией применяет доказательства под исходной
identity и пишет applied receipt вместе с outbox ACK. Stable `ackKey`, `clientStoreEpoch`,
`localCommitRef` и commit hash переживают crash. Пример ACK:

```json
{
  "version": 1,
  "ackKey": "client-apply-example",
  "planId": "plan-example-7",
  "planRevision": 7,
  "sourceManifestSha256": "07140ff7c16770b196bdca8c53f11efc4895e3444509486944df3590475e85c6",
  "clientStoreEpoch": "client-store-example",
  "sourceRunId": "run-old",
  "sourceSessionId": "session-old",
  "sourceCheckpointVersion": 3,
  "appliedActions": [
    {"actionId": "apply-ack", "evidenceSha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"},
    {"actionId": "resolve-old", "evidenceSha256": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"}
  ],
  "localCommitRef": "client-commit-example",
  "localCommitSha256": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
}
```

Первый ACK и его повторы добавляют **монотонный исторический факт** о применении
указанного immutable плана. После проверки текущего доступа, source/action/evidence
descriptors и dedup ACK key/commit hash первый ACK принимается даже при продвижении
case revision: local commit уже произошёл и не может быть повторён ради нового CAS.
Тот же key с изменённым фактом — conflict; идентичный повтор возвращает прежнюю
квитанцию. Gate атомарно добавляет факт и пересчитывает **текущее** состояние case.

Если между local commit и первым ACK был Stop, cancel или новый recovery generation,
ACK фиксируется как `historical-applied`. Он не воскресит Run, не потребит новый
permit и сам не разрешит transport/business action. Для последующих изменений нужны
CURRENT branch fence/policy/CAS и действующие dependencies. Клиент не может поздним
ACK перезаписать новый checkpoint: source version проверена локальной транзакцией.

Если transport action получил complete rejected/busy, его receipt неизменен. После
свежего наблюдения условия coordinator выпускает новую plan revision/action ID со
ссылкой на прежний доказанный отказ. Unknown step сначала сверяется под старым key.
`businessDispatchAllowed` разрешает только следующий согласованный intent в своей
ветви; это не разрешение повторить любой старый prompt/effect или обойти обычный admission.

Gate действует автоматически в рамках уже разрешённых transport/read/receipt шагов.
Он не просит пользователя подтверждать каждую сверку. Но не выдаёт себе право на
новую платную генерацию, новый аккаунт, инструмент, внешнюю компенсацию или
ослабление политики. Если нужно новое разрешение, UI показывает конкретный выбор.
Действующая заранее согласованная retry/budget policy может разрешать новый model
attempt без нового вопроса пользователю; coordinator обязан проверить этот допуск,
а не вводить дополнительное подтверждение каждой штатной сетевой попытки.

### 8.6. Отдельный контракт terminal continuation

Gate #77 / Router #96 описывают **ещё не поставленное** безопасное продолжение
терминально упавшего SDK хода. Coordinator не подменяет его словом resume.
`willRetry=true` — текущий SDK producer; после terminal он закончился.

Continuation permit требует exact source turn, reconciled tool/effect ledger,
подтверждённой истории, source terminal revision и отменяемого одноразового action
ID. Consume permit и запись successor intent атомарны с общим branch controller;
уникальность successor задаётся canonical source history/branch/turn/terminal revision,
**независимо от case/permit ID**. Два окна с разными cases не создают два продолжения
даже последовательно после освобождения writer. Consume проверяет текущие generation,
control epoch и policy; повтор consume возвращает тот же successor. Старый prompt
не посылается заново. Если native adapter не умеет
доказанно продолжать, supported=false. Восстановление истории может быть доступно
при недоступной автоматической continuation. Модель в новом разрешённом ходе
получает сохранённые outputs и запрет повторения domain effects; это не абсолютная
гарантия против нового семантически эквивалентного tool call без effect ledger.

## 9. Владение SDK и передача транспорта

Router сопоставляет native thread, logical ID, aliases, registry entries, процессы,
pending journal, tools, compact и subagents. Open/resume/compact/handoff сериализуются
по canonical history identity. Перед await старый entry помечается retiring;
новый prompt этого entry не допускается. Переход не должен терять ожидающую очередь.

Idle handoff разрешён, когда registry и journal согласованно не имеют активной/
неопределённой работы, producer закончен, нет pending results/compact/children.
Закрывается **собственный** SDK transport штатным способом; после подтверждения
закрытия сохраняется lifecycle proof. История, файлы и old turn receipts остаются.

Живой другой owner → типизированное ожидание. Внешний или неизвестный owner →
сверка/оператор, никакого kill по возрасту и удаления lock. PID проверяется вместе
со start time/boot, чтобы не принять переиспользованный PID за прежнего владельца.
Lease expiry ограничивает право новых команд, но не освобождает native writer сам.

Fresh no-rollout: пустой native transport можно заменить лишь при durable virgin
proof, отсутствии dispatch/compact/effects и точном SDK missing-history rejection.
Сохраняются logical IDs, scopes, оригиналы и capabilities. `resumed:false` не
подменяется true. Legacy/no descriptor или unknown dispatch запрещают такую замену.

Обычное отсутствие runtime в памяти не является отсутствием SDK history. Router
восстанавливает транспорт только после сверки exact journal и разрешений. Новый
boot требует identity reconciliation; неизменный nodeId не делает старый boot живым.

## 10. Особенности SDK и обычных API

Ниже — ограничения adapters, а не единый универсальный retry loop.
Подробные первичные ссылки и границы версий — [источники](SESSION-RESILIENCE-SOURCES.md).

**Критическое ограничение действующего API gateway:** сохранённый pricing snapshot
и одинаковый Idempotency-Key сейчас не являются result cache и не гарантируют
дедупликацию upstream inference. Повтор POST к `/harness/llm/v1/*` может снова вызвать
провайдера и повторно расходовать деньги. Нельзя переносить гарантии managed session
commands на эти API-пути. P3 требует отдельного model-attempt/result ledger;
до него unknown API outcome нельзя «лечить» слепым повтором всего agent loop.

### 10.1. Codex SDK по подписке

В установленном контуре история native thread находится у Router. Его startup,
resume, tool results, interrupt и compaction — различные RPC/события. Принятый
interrupt не заменяет terminal уведомление. Актуальная документация описывает
дополнительные app-server возможности, которые нельзя автоматически приписать
зафиксированному SDK 0.160.0.

Router сопоставляет original RPC/native error и turn IDs, ждёт terminal, не исполняет
host tool повторно по notification replay. Native WebSocket retry принадлежит SDK;
внешний Gate не отправляет второй prompt из-за старого transport snapshot.
При writer conflict действует раздел 9. При auth повторный вход не снимает fence,
не восстанавливает отменённый Run и не делает unconfirmed history подтверждённой.

### 10.2. Claude Agent SDK по подписке

Использовать **точный session ID**, а не «последнюю сессию каталога» многопользовательского
Router. CLI version, cwd, профиль, разрешённые tools/MCP и SDK options входят в
resume identity. В SDK0.2.159 уже есть SessionStore и MirrorErrorMessage, но текущий
Router не интегрировал и не квалифицировал этот recovery путь. Наличие store не
доказывает полноту mirror/backup: потерянная партия требует отдельной сверки.

Session transcript — история разговора, не транзакционный снимок внешнего мира.
Partial StreamEvent/AssistantMessage не равны окончательному ResultMessage.
Ошибка после текста/инструмента может требовать сверки, а не выдачи текста за success.
Параллельные tool calls коррелируются по native toolUseId, не очереди имён.

ResultMessage завершает конкретный native query, в том числе injected notification,
scheduled/channel/peer input или локальную команду. Terminal Harness публикуется
только после доказанной корреляции с собственным input: `origin`, session/query и
generation, terminal reason (в том числе `aborted_streaming`/`aborted_tools`). Один
текст/`is_error=false` не является корреляцией. Чужой/неатрибутированный result не
освобождает writer/checkpoint и не потребляет continuation permit. После interrupt
старые сообщения остаются в буфере: adapter дочитывает результат прежнего query
перед новым dispatch, поздний result не завершает следующий Run.

**History-only restore не должен сам продолжить прерванный ход.** Переменная
`CLAUDE_CODE_RESUME_INTERRUPTED_TURN=1` разрешает native auto-continuation без нового
host prompt. Это свойство native SDK, а не утверждение о flag действующего аккаунта.
Квалифицированный adapter Router23 отключает auto-continuation в своей
[поддержанной области](CLAUDE-COLD-RESTORE.md) и fencing-ом запрещает
любые native inference/tools до действующего admitted intent/permit. Отдельно
проверяет effective env с разрешёнными user/managed settings при старте и изменении:
одного `options.env` override недостаточно считать защиту доказанной. Квалификация
включает унаследованный flag=1 и settings env=1, а также pending deferred tools.
Если отсутствие автоматического
исполнения не доказано для точной CLI/конфигурации, `resume_transport.supported=false`.

ConversationResetMessage (`/clear`) — state-changing смена history lineage внутри
живого connection. `new_conversation_id` не подставляется как новый session ID:
его берут из последующих native сообщений. Прежние receipts/pending effects/usage
baseline сохраняются; старые Runs/permits не наследуются новой историей. Running
SDK totals могут сброситься, но Atlas settlement не обнуляется и не удваивается.
Неожиданный reset требует reconciliation; живой transport не доказывает сохранность
прежнего контекста. Origin/reset обработка поставлена в adapter; восстановление
сверяет exact case и не переносит эту квалификацию на произвольную CLI/конфигурацию.

После потери login: required/unknown/authenticated различаются; состояние проверяется
заново после входа. Если прежний transport умер, coordinator создаёт новый через
существующий create/resume путь и прежний session ID. Pending ACK/Run сначала сверяются.
Только точное missing-session доказательство и разрешённый context recovery допускают
новую историю. Auth/timeout/writer error не являются доказательством missing session.

### 10.3. OpenAI API: Responses, HTTP/streaming/WebSocket/background

Provider response ID, conversation ID, transport и client Run — разные сущности.
При наличии stored/retrievable response сначала читать его состояние; поддержка
retrieval/cancel/background определяется endpoint, retention и data policy.
`store=false`/ZDR нельзя незаметно менять на хранение ради удобства recovery.
Background имеет отдельную временную запись response примерно на10мин даже при
store=false; до включения проверяются data policy/retention, а не только этот флаг.

Документированный предел Responses WebSocket — 60 минут на соединение. При expiry
восстанавливается transport, не вся задача. Возможность цепочки зависит от доступности
previous response; при отсутствии состояния нужен полный разрешённый контекст.
Восстановление input history не является разрешением повторить выполненные функции.
Нельзя переносить семантику WebSocket напрямую на Codex SDK, который сам управляет им.

Сломанный stream после tool arguments/deltas не означает valid tool call. Исполнитель
получает только завершённый и проверенный вызов. Background/stream retrieval, если
поддержаны, читают прежний response; повтор POST не является способом «дочитать хвост».
Raw response status (`completed`, `failed`, `incomplete`, `cancelled` и активные
состояния), `incomplete_details.reason` (например, `max_output_tokens`), refusal
content и нормализованный Atlas outcome сохраняются раздельно. Refusal — не raw
response status. `response.completed` с `function_call` завершает API attempt,
но agent Run переходит в `waiting-tools`; completed не доказывает бизнес-успех.

### 10.3.1. Chat Completions — отдельный HTTP/SSE wire

В nonstream/SSE собираются `choices[index]`, text/tool deltas и `finish_reason`.
Выбор одного из нескольких choices задаётся до dispatch; инструменты альтернативных
choices нельзя выполнить все как одно поручение. Вызов допустим после полного
tool-call ID/name/arguments, schema validation и подтверждённого завершения выбранной
choice, не по первому fragment. Параллельные calls имеют собственные IDs/ledger.

| finish_reason | API attempt / Run | Разрешённый следующий шаг |
|---|---|---|
| stop | Известный завершённый ответ выбранной choice | Применить результат; проверка бизнес-цели отдельно |
| tool_calls / legacy function_call | Attempt завершён, Run waiting-tools | Исполнить только полные допустимые calls по effect ledger; результат следующему attempt |
| length | Частичный ответ | Сохранить partial; новая генерация только по continuation/budget policy |
| content_filter | Известное policy termination | Показать исход; не обходить фильтр автоповтором |
| null/нет finish_reason при обрыве | Частичный/unknown attempt | Сверка доступного результата, никакого blind POST |

Если полный выбранный ответ/tool_calls и finish_reason уже durable, обрыв до usage
tail/`[DONE]` не превращает их в unknown и не разрешает повтор модели. Usage может
остаться unknown и сверяется отдельно. Наличие `[DONE]` без полного нужного ответа
не компенсирует потерянные fragments. Другие совместимые providers квалифицируют
эту семантику отдельно, а не наследуют её по имени endpoint.

### 10.4. Прямой Claude Messages API

Это не Claude Agent SDK и не его login/session resume. Контекст и tool-use/result
пары сохраняет клиентский agent loop; API key/account/billing принадлежат API route.
HTTP 200 может сопровождаться последующей SSE error; adapter обязан дочитать событие.
429, 529 overload, API auth и spend/billing классифицируются по provider body и request ID.

Разорванный текст иногда можно продолжить новой генерацией по модельной инструкции,
но это новый API attempt с учётом расхода, не восстановление прежнего stream.
Неполные tool/thinking blocks не превращаются в принятые функции. Подробности
continuation зависят от модели/API-версии; сохраняются native block IDs/подписи и
точные tool_result пары. Политика на retry — раздел 11, не слепой повтор SDK helper.

`message_stop` завершает API request, но дальнейшее состояние Run задаёт stop_reason:

| stop_reason | Request outcome / Run | Следующее допустимое действие |
|---|---|---|
| end_turn | Известный завершённый ответ, в том числе пустой | Применить; если нужна новая генерация, отдельный attempt по политике |
| tool_use | Request завершён; Run waiting-tools | Client tool исполняется по ledger; server tool без result требует штатного продолжения, не host tool_result |
| pause_turn | Server-tool loop приостановлен; Run awaiting-continuation | Новый оплачиваемый Messages attempt с response.content **без изменений**, прежними server tools и применимым container state |
| max_tokens | Известный частичный ответ | Сохранить блоки, согласовать новый budget/continuation; incomplete call не исполнять |
| stop_sequence | Завершение по настроенной последовательности | Сохранить reason/sequence; оценка задачи отдельно |
| refusal | Известный отказ | Показать отказ; не обходить policy |
| model_context_window_exceeded | Частичное завершение из-за контекста | Сохранить исход; управляемое context recovery, не replay effects |

Повторный pause_turn, смешанные server/client tools и Stop между attempts сохраняют
всю последовательность paused response/content и доступное server state. Новый
attempt имеет свой ID/расход и проверяет cancellation epoch. Host tool ledger не
заменяет server-tool/container состояние и не даёт права запустить server tool заново.

### 10.5. DeepSeek: Chat Completions и совместимые форматы

Текущая официальная Responses-совместимость DeepSeek **stateless**: previous_response_id,
conversation, store и background не поддерживаются; ряд иных параметров игнорируется.
Adapter не выдаёт фиктивный stored response/retrieval/cancel. Канонический input и
tool outputs нужны на стороне владельца loop, включая provider-native поля reasoning,
если они требуются выбранным thinking/tool режимом.

Сервер может посылать пустые строки и SSE keep-alive во время ожидания. Это не
пустой успешный ответ, не доказательство начала inference и не tool result.
Ограничение ожидания до inference и квоты поставщика фиксируются как provider facts,
не как произвольный общий timeout задачи Atlas. 402 balance, 429 concurrency/rate и
503 overload — разные причины. Смена Flash/Pro — явная смена model/binding с проверкой
истории, возможностей, reasoning и подписанного тарифа.

### 10.6. Другие API: Gemini, Bedrock, Azure, Mistral, локальные и совместимые

Новый adapter проходит тот же capability checklist: stateful history, retrieval,
cancel, idempotency, streaming terminal, tool IDs, opaque state, usage, context,
auth, quota и retention. Gemini generateContent и stateful interactions, Bedrock
Converse и provider-specific APIs нельзя свести к одному API по названию модели.
Native tool/result/opaque state сохраняются без потерь при сериализации.

Azure deployment/region/API version, Bedrock region/IAM/model/endpoint и локальный
server/model digest являются частью route identity. Unknown/непроверенная функция
явно unsupported. Gate не обещает hot migration существующего SDK thread между ними.

### 10.7. MCP, SDK web/files/images и встроенные исполнители

MCP request ID/session ID/SSE event ID не заменяют domain effect ID. После истечения
MCP transport session проводится новый initialize по согласованному протоколу;
результат старого tool call сначала сверяется. Возможность SSE redelivery зависит
от MCP server, она не универсальна. Незавершённый JSON/новый connection не разрешают
повторить внешнее действие. Required MCP failure не должен тихо менять каталог tools.

Native SDK web/image/file tools — отдельный контур исполнителя. Если модель исполняет
shell/computer/MCP сама, её side effects также требуют ledger/политики. Atlas обязан
запретить недедуплицируемое автоматическое continuation при отсутствии доказательств.
Нельзя заявить отсутствие side effects только потому, что список host tools пуст.

Поиск: запрошен/начат/завершён/ошибка различаются; null аргументы на раннем событии
не являются доказательством отказа. После terminal не остаётся «выполняется» без
основания. Изображение: generation receipt, доступный файл и его доставка — три
шага; expired URL обновляется/читается по artifact ID, а не новой генерацией.

## 11. Ошибки, повторы и сроки

Полный порядок по классам — [матрица](SESSION-RESILIENCE-ERRORS.md). Набор строк ошибок
не бывает окончательным: unknown provider/SDK code тоже имеет безопасную ветвь.

### 11.1. Минимальный нормализованный error envelope

Целевые поля, дополнительные к сохранённому v1 error:

| Поле | Значение |
|---|---|
| code / category / origin / phase | Машинный код, класс, Gate/Router/SDK/provider/executor, стадия |
| operation | Immutable request/turn/call/attempt reference, доступная владельцу |
| delivery / effect / history | Каждая certainty отдельно: not_sent/accepted/unknown и available/unavailable |
| terminal / willRetry / retryOwner | Завершён ли producer; кто реально владеет повтором |
| retryScope / retryAt / retryCondition | Чтение/transport/exact-output/native retry; абсолютный срок или условие |
| evidence / recovery | Proof references, case ID, next action, поддерживаемость |
| provider | Raw type/code/status/request ID и redacted details, не секреты/полный prompt |

HTTP status не определяет outcome. В частности, 400 бывает expiry/контекст/формат,
409 — занятость/identity conflict, 429 — transient либо квота, 503 — maintenance,
transport unknown либо overload. Primary cause хранится даже при нормализации UI.

### 11.2. Кто повторяет

| Повтор | Владелец | Условие |
|---|---|---|
| GET receipt/events/result | Consumer клиента/Gate | Тот же объект, cursor и доступ |
| Доставка уже сохранённой команды | Один delivery controller | Тот же key/body; принимающая сторона гарантирует dedup |
| SDK internal retry | SDK producer | Наблюдать тот же turn; внешние loop не дублируют его |
| Restore transport/history | Router либо stateless adapter | Точная история/owner proof; без business prompt |
| Новый model attempt | Владелец agent loop по Gate policy | Подтверждённый отказ до inference либо explicit safe continuation |
| Внешнее действие инструмента | Его executor | Exact idempotency/read-back proof, не совет LLM |

В одной операции нет умножения SDK × Router × Gate × Harness retries.
Отказ до inference подтверждается документированной семантикой adapter/provider,
а не самим HTTP status; состоявшийся HTTP dispatch этого доказательства не исключает.
Каждый новый model attempt имеет собственный ID и заново проверяет Stop и бюджет.
Adapter объявляет встроенные retries и их область; внешние уровни наблюдают.
Retry-After сохраняется как время первоначального отказа, GET не продлевает паузу.
При отсутствии срока — backoff с jitter и условием повторной проверки, без
выдуманного времени восстановления подписки. Account-wide breaker применяется
только при доказанной account scope; неизвестная модельная пауза не блокирует всех.

Исчерпание короткого автоматического burst бюджета переводит case в durable waiting
с планом следующей проверки; не стирает поручение и не означает исчерпанную подписку.
Неретраебельные ошибки и persistent unknown дают другой явный выход. Stop отменяет
ожидание. Unattended ожидание ограничивается сроком актуальности задачи/политикой,
объявленной пользователю, а не тайным общим wall-clock cutoff.

### 11.3. Какие таймеры допустимы

Connect, TLS, pool acquisition, idle channel, lease observation и UI poll могут
иметь таймеры. Они обнаруживают неисправность транспорта/владения, а не доказывают
неисполнение. Истечение сетевого read timeout после отправки даёт outcome unknown.
Timeout не должен посылать implicit Stop активному producer без согласованной политики.

Долгий healthy inference/tool/ожидание auth не обрывается только из-за прошедших
180/360 секунд. Реальные provider limits, бюджеты/дедлайны пользователя, рабочие
окна задания и resource policy объявляются явно. Heartbeat означает жизнь канала,
но не прогресс модели; отсутствие текста во время thinking само по себе не deadlock.

## 12. Пользовательские действия и фоновые задания

| Действие | Обязательное поведение |
|---|---|
| Открыть старый чат | Recovery handshake до fatal stale binding guard |
| Отправить новое сообщение при pending | Сохранить новый inbox item; сверить старый, не заменить его body |
| Нажать «Повторить» | Сначала receipt/recovery; UI различает redelivery и новое поручение |
| Stop offline/при закрытом окне | Durable target cancellation, доставка при связи; поздний Stop не отменяет новый turn |
| Сменить модель/агента/reasoning | Сначала завершить прежний recovery; pending rebind не меняет параметры старого compact |
| Уменьшить context window | Проверить фактический budget, compact на прежней модели/истории, потом rebind |
| Сменить tools/MCP/политику | Existing result разрешён к сверке по access policy; новое исполнение — по новым правам |
| Войти заново/увеличить подписку | Свежий auth/quota probe; восстановить transport, не воскресить отменённый Run |
| Перезапустить приложение/ОС/сменить сеть | Outbox/cursors/Run читаются с диска; один recovery owner; HTTP response не считается записанным ACK |
| Два окна/устройства | Один writer/consumer claim на исполняющую ветвь; чтение может быть параллельным |
| Передать работу другому устройству | Явный ACL/lease handoff с доступными outputs; device-bound aliases не переносимы сами |
| Переименовать/архивировать чат | Не терять идентичность, receipts и расписание; архив UI не удаляет SDK history |
| Удалить историю/вложения | Отдельное подтверждённое действие, fence поздних команд и согласованная retention policy |
| Cron/периодический запуск | Stable occurrence ID, нет overlap одной исполняющей ветви, catch-up политика и expiry |
| Изменить задание во время выполнения | Новая версия влияет на будущие occurrences; старый intent остаётся неизменным |
| Субагент/делегирование | Parent/child IDs, отдельные ledgers; Stop дерева учитывает дочерние эффекты и ACK |

Compact — state-changing команда, а не безопасный GET. Потерянный compact ACK
сверяется по прежнему key. Пока его исход неизвестен, нельзя начать второй compact,
перенести старую историю на новую модель или выпустить зависимый prompt. Summary
и cursor связываются с подтверждённой границей; отменённый compact не считается успешным.

Смена поставщика создаёт новую lineage с разрешённым portable context. Не переносить
opaque reasoning/signatures как понятные другой модели инструкции. Отдельно сохраняются
системные правила, tool effects, unresolved operations, permissions и artifact references.

Изменение конфигурации тоже имеет версию. Доступные значения — пересечение provider
capabilities, ограничений Router и политики Gate; администратор нижнего уровня не
расширяет полномочия верхнего. Явное разрешённое пользовательское значение имеет
приоритет над defaults. Изменившийся Router default не должен молча поменять уже
закреплённый configuredReasoning при resume; Gate default применяется к новым
согласованиям, не переписывает receipt старого запроса. Новое запрещающее правило
проверяется до следующего dispatch; чтение разрешённых старых результатов отдельно.
Requested/configured values, resolved model и policy revision сохраняются вместе.
Silent ignore параметра upstream не выдаётся за подтверждённую настройку.

## 13. Хранение, учёт, доступ и эксплуатация

### 13.1. Durable ledgers и SQLite

Gate/Router/client не разделяют одну транзакцию. Между ними — outbox/inbox,
неизменяемые receipts и reconciliation. SQLite WAL имеет одного writer и допускает
SQLITE_BUSY; socket/SDK нельзя ждать внутри долгой write transaction.
ACK-save busy после выполненного инструмента лечится повтором **фиксации/чтения ACK**,
а не повтором инструмента. Нужны очереди записи, корректный commit/rollback и
наблюдение lock wait. Встроенный busy_timeout не заменяет recovery.

Disk-full/corruption/read-only FS до эффекта запрещают dispatch; после эффекта
оставляют unknown до внешней сверки. При отсутствии места нельзя вытеснять
dedup/fences ради продолжения записи. Должен оставаться аварийный путь чтения,
освобождения безопасного cache/staging и восстановления из согласованного backup.

Backup включает Gate ledger/fences/keys, Router journal/identity/history mapping
и исходы клиентского executor. Старый backup не доказывает, что более новых действий
не было. После rollback, потерявшего ledger, нужна новая store epoch и quarantine
неизвестных операций; нельзя сбрасывать ключи и повторять их как новые.

### 13.2. Retention и результаты

Полный payload, компактная terminal receipt, dedup tombstone и usage receipt имеют
разные сроки хранения. Клиент узнаёт их до отправки. Потеря полного payload возвращает
typed unavailable/expired, а не пустой success. Tombstones/fences нельзя удалять
раньше replay horizon без отдельного retirement протокола.

Оригиналы/изображения адресуются hash/ID, ACL и lineage; base64 не копируется в каждый
prompt. Потерянный upload ACK сверяется по key и checksum. Ограничение размера
сообщается до загрузки, с источником и способом продолжения: resumable/chunked upload
только при реальной поддержке, смена файла либо отправка текста без него. Ошибка
одного исторического файла не делает все последующие сообщения неотправляемыми.

### 13.3. Usage, прайс, cache и ресурсы

Logical intent, SDK turn, provider attempt и billable model call учитываются отдельно.
Повторное чтение receipts и повтор ACK не списывают второй раз. Реально выполненные
дополнительные provider attempts могут стоить денег; скрывать их под dedup нельзя.
Unknown usage не равен нулю; reserve/settle/release имеют собственные idempotency keys.
Подписочная telemetry не превращается в выдуманную API-цену.

Для API сохранить подписанный pricing snapshot/effective interval, reserve и факт
upstream dispatch. Missing/expired price исправляется прайсом, не retry модели.
После неизвестного dispatch резерв не освобождается по одному network error.

Контекст окна — фактический budget точного SDK/route/model, а не сумма входных tokens
всех шагов. Cached input/read/write, uncached input, output и reasoning нормализуются
adapter-ом с raw provenance. Cache hit не означает маленький context и не гарантирует
бесплатного повторения. Стабильные system/tool prefixes и история помогают cache;
обновления профиля/инструментов/compact могут его менять. Для Claude API cache-write
не объединяется произвольно с cache-miss. Для DeepSeek cache не служит session history.

Resource telemetry разделяет active turns, queued work, SDK sessions, writer owners,
input I/O, compact и subagents. Слоты, provider rate, подписочная quota и цена —
разные ограничения. Recovery имеет fair admission, не starvation существующих ходов.

### 13.4. Безопасность и обновления

Recovery проверяет device/org/route/data class/node/boot/history/branch и права на
каждый artifact. Повторный login не снимает revocation. Чужой ресурс маскируется
согласно политике, но отсутствие доступа не объявляется отсутствием истории.
Токены/refresh/session secrets, raw provider bodies и приватные outputs не попадают
в общедоступные логи. Diagnostic evidence redacted, хранится с ограниченными ACL.

В текущей архитектуре один активный Gate на SQLite; multi-active без fencing
не поддерживается. Целевой HA требует согласованного leader/store epoch, shared
durable ledgers и fencing старого владельца. Просто запустить второй Gate нельзя.

Deploy: прекратить новый admission, дождаться включая input bodies/SDK close;
сверить producer/child/compact/unknown операции, выполнить guard qualification,
backup, switch, readiness и собственную инертную приёмку. Browser disconnect не
считается свободой узла. Rollback не удаляет новые receipts/fences и не откатывает
эпоху истории так, чтобы старый запрос мог исполниться вновь.

## 14. Совместимость и план внедрения

### 14.1. Поставленная реализация и оставшиеся границы

| Возможность | Статус Gate 0.4.31 / Router 0.8.24 по [матрице](SESSION-RESILIENCE-STATUS.md) |
|---|---|
| Durable command ACK, exact turn events, summary receipt, owner affinity | Реализовано; смотреть конкретные v1 документы и retention |
| Чтение после временного разрыва и восстановление сохранённого tool_result | Gate 0.4.29 возобновляет чтение с прежнего курсора. Gate 0.4.30 добавляет доказательство для закрытого callback после Stop. Gate 0.4.31 / Router 0.8.24 подтверждают активный callback и защищённую доставку |
| Prompt admission fence | Gate commandRecovery v1 реализован; не универсальный fence внешних инструментов |
| Auth refresh/create-resume, input no-rollout, idle writer handoff | Реализовано частями Gate79/85/87, Router101/103; клиентский recovery не следует из серверной поставки |
| Recovery через исходящий connector | Transport/lifecycle conformance поставлен; capabilities всё равно проверяются по точному пути/узлу. Direct qualification не переносится на произвольный connector |
| Claude native auto-continuation guard, origin/drain/reset, history-only restore | Поставлено и квалифицировано в заявленной области, см. [Claude](CLAUDE-COLD-RESTORE.md); не универсальная гарантия любой конфигурации |
| Provider retry до dispatch и SDK internal retry observation | Реализовано в соответствующих путях; общая account-group координация не гарантируется |
| Coordinator case API, manifest, CAS и client-applied ACK | Серверный v1 поставлен; клиентский commit/ACK и охват старых checkpoints требуют собственной приёмки |
| Cross-Run stale checkpoint и обновление всех открытых чатов | Есть клиентские поставки отдельных repair путей; общий startup/upgrade сценарий нескольких старых чатов проверяется отдельно |
| Same-device auth и opaque history identity repair | Gate24 auth v1 / Gate25 sourceHistoryIdentity v1; нужен понимающий capability клиент. SDK history и инструмент не переисполняются |
| Terminal safe continuation permit | **Требуется**, Gate77 / Router96 / EDT220 |
| Stateful durable recovery прямых API requests | API-attempt ledger/coordinator поставлен; retrieval/cancel и неизвестные upstream outcomes ограничены конкретным протоколом/провайдером |
| Target-aware late Stop fencing, единый executor effect ledger | Целевой контракт; нельзя объявлять реализованными в legacy endpoints |
| Автоматическая cross-node/account SDK migration, multi-active Gate HA | Не реализовано; отдельная процедура/проект |

Исторические handoff/README могут описывать старые версии. Runtime/source и
версионированные wire contracts имеют приоритет над рекламным списком возможностей.

### 14.2. Разбиение на поставки

| Этап | Владелец | Результат и критерий завершения |
|---|---|---|
| P0: source recovery | Harness/EDT + Gate | Вход в старый чат вызывает сверку; готовый ACK/terminal освобождает старый checkpoint без повторов |
| P1: coordinator core | Gate + connector | Inventory/seal/cases/manifest/branch controller/CAS/исторический client ACK; read/receipt/transport планы и полный negotiated wire transport |
| P2: SDK proof adapters | Router | Codex/Claude auth/history/writer/compact/Stop proofs; Claude no-auto-continuation/origin/drain/reset; exact capabilities |
| P3: direct API recovery | Gate API adapters + client loops | OpenAI, Claude Messages, DeepSeek; retrieval либо stateless context recovery, честные unknown |
| P4: effects and continuation | Executors + Router + Gate | External idempotency/read-back, permit consumption; terminal continuation только где доказана |
| P5: operation resilience | Gate/Router/clients/ops | Restore drills, target Stop, storage pressure, telemetry, controlled upgrade/rollback |

Нельзя объявить P0 завершённым по успешной новой инертной беседе. Нужна именно старая
беседа, stale checkpoint, прежние эффекты и новый явный запрос с сохранённой историей.

### 14.3. Поэтапные версии

Новый Gate со старым Router использует только его проверенные v1 proofs и явно
возвращает unsupported для остальных шагов. Новый Router со старым Gate не раскрывает
внутренние поля без negotiation и не включает silent replay. Старый Harness продолжает
работать по прежнему контракту, но не приобретает автоматическое лечение local guard.

Mixed nodes: session capabilities закрепляются при create/resume и проверяются на
том же node после boot. Нельзя публиковать union как гарантию continuation на каждом
узле. Min-client версии применяются к новой функции явно, не ломая произвольные старые
чаты скрытым изменением формата. В target errors сохраняются исходные codes/proof scope.

## 15. Приёмка и работа дежурного

Изменение документа требует проверки ссылок, примеров и непротиворечивости версий,
**не** полного runtime test suite и не вызова платной модели. При реализации выбираются
только затронутые сценарии приложения. Guard-required qualification не обходится.

Минимальный журнал инцидента: exact identities, версии/SDK/account group, стадия,
последний durable cursor/ACK, primary cause, source Run/control epoch, side-effect
evidence, что было прочитано/отправлено и кто владеет повтором. Доступные доказательства
и их отсутствие перечисляются отдельно. Не начинать расследование с Repeat/Reset.

Порядок дежурного: получить snapshot → подтвердить владельца/политику → найти exact
operation/terminal → сравнить executor outputs/ACK → проверить writer и source Run →
применить безопасный план → убедиться в client-applied → проверить один новый
разрешённый ввод. Пользовательские side effects для диагностики не повторяются.

Успех recovery: прежний результат доступен, старые эффекты не повторились, Stop
сохранён, следующий допустимый запрос проходит, usage не удвоен, старые receipts
по-прежнему читаются. Если этого ещё нет, отчёт честно говорит waiting/needs_input/
unsupported, указывает работающий выход и владельца оставшейся реализации.
