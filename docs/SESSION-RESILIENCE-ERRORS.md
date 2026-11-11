# Матрица сбоев и восстановления

Приложение к [единому контракту](SESSION-RESILIENCE-CONTRACT.md), RFC 1 от 02.11.2026.
**Это целевое поведение; не все перечисленные обработчики реализованы.** E-ID —
классификационная метка, не обещание существующего HTTP error code. Исходные native
code/status/request ID сохраняются. Новый вариант ошибки попадает в ближайший класс;
нераспознанный — E55, без автоматического повторения side effect.

Сокращения: **H** — Harness/EDT/agent loop; **G** — Gate; **R** — Router/native adapter;
**X** — исполнитель/внешняя система. «Тот же key» означает также тот же owner, target,
body hash и policy scope. «Новый intent» допустим только после доказанного исхода
прежнего, с причинной ссылкой на него; не является способом обойти unknown.

## 1. Связь и наблюдение

| ID | Сбой/симптом | Что выяснить | Порядок восстановления и сторона | Чего нельзя делать |
|---|---|---|---|---|
| E01 | DNS/refused/connect/pool timeout до первого соединения | Был ли вообще dispatch в предыдущей попытке этого intent | H/G сохраняют outbox; читают receipt; повтор соединения с прежним key. Только локальное доказательство отсутствия отправки относится к этой попытке | Считать все прежние попытки not_sent |
| E02 | TLS/certificate/hostname/mTLS failure | Auth peer, срок сертификата, отзыв, node identity | G/R проверяют trust/renewal; сохраняют binding; наблюдение возобновляется после исправления | Выключать TLS verification, доверять другому node по адресу |
| E03 | Разрыв при записи тела HTTP/upload | Мог ли peer принять весь запрос до разрыва | G/R сверяют command receipt/hash/оригинал; H сохраняет payload и key; допустим exact redelivery при dedup | Новый key, новый файл с иным hash, новый prompt |
| E04 | Read timeout после POST, proxy502/504 | Приём/dispatch/terminal неизвестны | G получает exact receipt/turn; H переходит в reconciliation, не отменяет producer неявно | Приравнивать timeout к отказу до исполнения |
| E05 | SSE/WS/HTTP EOF посреди ответа | Есть ли terminal, последний применённый cursor, какой producer жив | G при временном обрыве чтения Router повторяет exact GET с тем же `after`. H после обрыва своего SSE дочитывает тот же turn через events. У прямых Router/API действуют их правила detach/retrieval | Считать EOF success, повторять весь prompt для получения хвоста |
| E06 | HTTP200 с HTML, malformed JSON, пустым телом | Это proxy/login страница или protocol error, а не receipt | Adapter сохраняет redacted transport evidence, читает исходный ledger; UI показывает protocol/recovery failure | Превращать пустое тело в пустой успешный ответ |
| E07 | gzip/double-decode/неподдержанный encoding | Где нарушено framed body; успел ли upstream выполнить | G/connector исправляет транспорт; исходный command/result читается из ledger, если сохранён | Лечить декодирование новым оплачиваемым model call |
| E08 | Клиент уснул/потерял сеть/закрыл окно | Различать detach consumer, Stop intent и смерть executor | H после запуска загружает outbox/cursors, вызывает recovery; G продолжает допустимый producer | Считать закрытие окна Stop либо освобождением writer |
| E09 | Late callback, reorder, duplicate event | Exact turn/generation/seq; применён ли уже факт | H/R дедуплицируют, дочитывают gap; late факт относится к старому ledger | Менять состояние/ошибку/usage нового Run |

## 2. Create, history и writer

| ID | Сбой/симптом | Что выяснить | Порядок восстановления и сторона | Чего нельзя делать |
|---|---|---|---|---|
| E10 | Create ACK потерян | Immutable create key/body, сохранённый response | G возвращает прежний create результат; R сверяет собственную команду; H сохраняет public alias из договорного источника | Требовать недоговорное sdkSessionId в ACK, создавать вторую историю |
| E11 | Create outcome unknown | Нет достоверного rejected или success receipt | G/R сверяют тот же intent; H не переключает key/root/model; case остаётся наблюдаемым | Считать любой5xx доказанным not_sent |
| E12 | Повтор create возвращает прежний отказ | Отказ сохранён? Он до admission или уже complete rejected? | Complete rejected неизменен. После устранения причины новый допустимый create intent с ссылкой на старый; pre-admission может заново проверить прежний key | Бесконечно ждать изменения cached rejected; менять payload прежнего key |
| E13 | Resume не соответствует route/model/device/org | Immutable binding и фактические права | G восстанавливает старую операцию под старой binding; смену модели выполняет отдельно. Для чужого owner отказ доступа | Снимать model/owner guard ради продолжения |
| E14 | Fresh SDK no-rollout до первого prompt | Durable virgin descriptor, dispatch/compact/effect отсутствуют, точный native rejection | R может заменить пустой transport со стабильной logical identity/scopes; G проверяет proof; H не отправляет старые инструменты | Объявлять неизвестную/legacy history пустой или ставить resumed=true |
| E15 | History/session действительно отсутствует | Exact SDK missing-history, retention/deletion, доступность канонического transcript | R/G подтверждают unavailable; H восстанавливает разрешённый context в новой lineage либо выбирает restore/export | Считать auth/timeout/404 runtime доказательством потери истории |
| E16 | Прежний собственный idle writer мешает resume | Registry+journal/task/tool/compact/child согласованно idle | R сериализует handoff, retiring до await, штатно закрывает transport, сохраняет lifecycle proof и открывает ту же историю | Kill/удаление lock, сброс transcript, второй writer |
| E17 | Живой законный writer | Exact owner/turn/queued/compact/pending effects | G/R возвращают typed waiting; H хранит новый input и наблюдает; после безопасной границы продолжает transport | Останавливать чужую работу или исчерпывать фиксированные3попытки busy как окончательный failure |
| E18 | Внешний/неучтённый writer, якобы orphan | PID+start time+boot, fd/native lock, сервис и фактический lifecycle | R/G собирают evidence; доказанно завершённый собственный процесс закрывается штатно; неизвестный требует оператора | Считать lease expiry/возраст/отсутствие Run доказательством смерти |
| E19 | Router restart/boot mismatch | Та же node/account/history? Durable journal и SDK ID доступны? | G сверяет node/boot; читает journal без SDK; R восстанавливает только разрешённый transport. Deferred недоставленный prompt имеет отдельный proof | Переназначать старую беседу случайному свободному node |
| E20 | Несколько aliases/bindings или конкурентные resumes | Canonical logical/native history identity, кто уже opening/retiring | R сериализует по истории; G сохраняет отдельные receipts; busy/unknown owner не обходится | Дедуплицировать только по HTTP SID, игнорируя общий native thread |

## 3. Ход, результат и локальное сохранение

| ID | Сбой/симптом | Что выяснить | Порядок восстановления и сторона | Чего нельзя делать |
|---|---|---|---|---|
| E21 | Prompt ACK потерян | Admission/dispatch/exact turn receipt | G/R возвращают прежний ACK/terminal; H наблюдает тот же turn | Генерировать новый turnId для повторения неизвестного поручения |
| E22 | GET command возвращает 404 | Нет записи, недоступна привязка, другой owner или ещё гонка допуска | Для prompt предусмотрен admission fence. Для `tool_result` на v1 нужен exact proof `retryAllowed=true` для прежнего node/boot/turn/call и body hash. При недоступном proof клиент сверяет дальше | Принимать 404 за отрицательную квитанцию |
| E23 | Crash после SDK dispatch, до его receipt | Native provider/turn evidence; могли ли возникнуть side effects | R помечает unknown; G/H сверяют native/executor ledger. Unsupported continuation остаётся unsupported | Восстанавливать удалённый deferred marker и посылать prompt вновь |
| E24 | Crash до dispatch queued prompt | Сохранилась ли durable deferred запись | R восстанавливает именно доказанно неотправленную запись с теми же turn/cursor; Stop и expiry проверяются до dispatch | Считать отсутствие dispatch receipt достаточным deferred proof |
| E25 | SDK сообщает willRetry=true | Жив ли прежний producer, не был ли уже terminal/Stop | R/G передают событие и первичную причину; H наблюдает тот же turn | Параллельный retry-loop клиента/Gate/Router |
| E26 | SDK terminal overload после tool effects | Terminal, все tool outputs/ACK, history, наличие continuation permit | G #77/R #96 target contract либо новый явный запрос после сверки; H применяет сохранённые outputs | «Продолжить» путём отправки исходного prompt как нового |
| E27 | WebSocket lifetime/connection/stream limit | Provider code и adapter; что поддерживает native SDK | R/adapter переподключает transport и сверяет прежний результат; история/input восстанавливаются по возможностям | Считать ошибку общей квотой или автоматически повторять инструменты |
| E28 | Stop отправлен, ACK потерян | Exact control key/target generation; terminal/settled | H/G сверяют Stop receipt и turn; сохраняют cancellation epoch | Новый Stop старого target без проверки отменяет другой turn |
| E29 | Stop пришёл после completion/нового хода | Causal order, old target и новый generation | Клиент сверяет исходный ход и квитанцию. Публичный `/interrupt` действует в scope сессии, поэтому нельзя посылать поздний Stop по уже сменившемуся ходу. Exact-turn Stop остаётся целевым требованием | Переписывать успешный эффект в «не выполнялся» |
| E30 | Steer принят, absorbed не подтверждён | ID/producer, SDK echo, terminal boundary | R/G/H сохраняют accepted/unabsorbed; возвращают пользователю точный исход | Добавлять steer задним числом в завершённую модельную историю |
| E31 | Prompt/compact/tools update одновременно | Session/writer operation lock, expected configuration revision | R/G сериализуют совместимые операции; остальные ждут/получают typed conflict с рабочим выходом | Потерять сохранённый intent либо выполнить обе операции на одной истории параллельно |
| E32 | События дублируются/имеют gap | Applied cursor, durable seq и payload hash | H запрашивает недостающую страницу, применяет один раз; G/R не создают новый producer | Повторять инструменты из replay старого stream |
| E33 | Полный event journal истёк410, summary receipt есть | Что сохранилось: terminal/usage/text/artifacts | G возвращает доступные части и unavailable для остальных; H не выдаёт summary за полный ответ | Успех с пустым текстом/media без предупреждения об отсутствующем результате |
| E34 | SQLite busy при сохранении tool ACK/result, обрыв при доставке | Принят ли исходный результат, сохранён ли output, ждёт ли exact callback | H сверяет command ACK и повторяет локальную фиксацию. Для неопределённой доставки применяет только разрешённый v1 POST сохранённого тела. После подтверждённого Stop отдельный proof закрывает допустимый checkpoint | Повтор исполнения инструмента, очистка outbox или подмена terminal одним ACK |
| E35 | Disk full/DB read-only/corruption | До или после effect/admission; доступен ли ledger | До эффекта — backpressure; после — unknown с внешней сверкой. Ops очищает безопасный staging/cache, восстанавливает согласованный backup | Удалять history/fences/неразобранные outputs для освобождения места |
| E36 | Local agent_delivery_binding_changed / stale Run checkpoint | Source run/root/route/model/turn и вид pending; клиентский state нужен Gate в manifest | H вызывает G recovery до нового guard; старый checkpoint разрешается доказательством под старой identity, затем новый Run | Blind reset state, перенос pending на новый Run или вечный guard-loop |
| E37 | Инструмент выполнен, локальный output утрачен | External receipt/read-back, DB/WAL/backup; полный payload доступен? | X/H восстанавливают исходный результат по доказательству; G связывает refs; иначе effect-unknown | Выдумывать output по тексту модели либо выполнять действие заново |
| E38 | Terminal получен, клиент упал до его применения | Client-applied ACK отсутствует; server receipt доступен | G/H повторяют доставку данных и локальную транзакцию, не model call. Cursor/usage dedup | Считать ответ утраченным окончательно или списать usage второй раз |

## 4. Исполнители, MCP и встроенные инструменты

| ID | Сбой/симптом | Что выяснить | Порядок восстановления и сторона | Чего нельзя делать |
|---|---|---|---|---|
| E39 | Tool timeout/потеря устройства во время выполнения | Side-effect class, external operation ID и worker ownership | X наблюдает job/read-back; H/G изолируют зависимую работу и сохраняют вопрос | Объявлять timeout неисполнением |
| E40 | Side effect совершён, процесс умер до записи | Есть ли external idempotency key, receipt, revision/content proof | X/H восстанавливают по внешнему факту; если нельзя — needs_decision с конкретной проверкой | Автоматический повтор неидемпотентного действия |
| E41 | Tool result отправлен, ACK потерян | Точный callId, output hash и receiving receipt | H/G/R читают квитанцию; при дедупликации доставляют **сохранённый** output прежним key | Повторять tool function для получения того же output |
| E42 | Тот же key/callId с другим body | Перепутан Run, payload изменён, поздний callback? | G/R/H отвергают конфликт; сохраняют оба evidence refs; исправляется correlation | Молча принять новое содержимое по старому key |
| E43 | Другой callId повторяет ту же бизнес-операцию | Domain effectId и внешний объект, а не текстовое сходство | X не исполняет второй раз; возвращает прежний business receipt при совпадении exact semantics | Уповать на уникальность нового callId либо дедуплицировать разные операции по имени |
| E44 | Неполный/невалидный tool JSON/schema | Завершён ли native argument block, согласован ли tool catalog | R/H валидируют до исполнения; возвращают protocol/tool error по тому же callId | Исполнять частично разобранные аргументы |
| E45 | MCP session404/обрыв SSE | Negotiated MCP version, old request outcome, серверная resumability | X/R/H заново initialize transport; старый результат сверяют отдельно. Last-Event-ID только при реальной поддержке | Повторять tools/call лишь потому, что MCP session пересоздана |
| E46 | Required MCP/worker не стартовал | Какой server/tool обязателен, есть ли pending side effects | R/H возвращают dependency-required; восстанавливают конфигурацию/доступ. Каталог ACK должен быть точным | Тихо продолжать с другим каталогом и утверждать совместимость |
| E47 | Native shell/computer/MCP выполняет SDK | Имеются ли native action evidence/permit; host tools могут быть пусты | R/G включают эти эффекты в reconciliation и ограничивают continuation | Считать tools=[] доказательством отсутствия любых действий |
| E48 | Provider stream оборван посреди tool/thinking block | Блок complete? Native call signature и matching result есть? | Adapter сохраняет raw fragments; только completed валидный call выдаётся X; далее result recovery | Склеивать fragment в новый выполненный вызов без доказательства |
| E49 | Родитель остановлен, child/foreign tool ещё работает | Parent-child ledger, running claims и внешний результат | R/H/X распространяют target Stop по политике, ждут/изолируют результаты; поздние ACK относятся к исходным children | Освобождать все резервы как будто children уже завершены |

## 5. Вход, квоты, цена, модель

| ID | Сбой/симптом | Что выяснить | Порядок восстановления и сторона | Чего нельзя делать |
|---|---|---|---|---|
| E50 | SDK login expired/401/not logged in | SDK account required/unknown/authenticated; точная history identity | R обслуживает штатный login; G после нового auth epoch переоценивает readiness, восстанавливает transport после сверки | Кэшировать старый auth error навечно, повторять отменённую работу после входа |
| E51 | Gate access token истёк, реквизиты неизвестны или устройство отозвано | Входной `token_expired`, `unauthorized`, `device_revoked`, запрет политики или upstream auth | H обновляет реквизиты того же устройства, читает исходный ход и сверяет ранее начатую доставку. Ротация использует исходный rotation ID. Отзыв и запрет политики сохраняют силу | Подставить другой device token для чтения чужого alias или переотправить prompt из-за 401 |
| E52 | Подтверждённая subscription/spend/balance quota | Provider typed code/scope/reset, API price отдельно | G/R ставят scoped waiting/needs_input; свежая telemetry/изменение оплаты переоценивает запрет | Называть любой429 «подписка исчерпана» |
| E53 | Временный429/slow_down/concurrency | Retry-After, quota vs transient, scope certainty | Один retry owner; wait до исходного срока, jitter/fair queue, G/R не повторяют принятый prompt | Глобальная пауза всех моделей при неизвестной области либо retry storm |
| E54 | Selected model at capacity / 503/529 overload | Model/account/region; upstream_dispatched, typed provider_admission_rejected, inference_started/unknown или partial_output | Документированный отказ до inference допускает новый attempt ID по прежней retry/budget policy и backoff даже после HTTP dispatch. Generic5xx/потеря ответа/midstream200 требуют сверки/partial handling | Считать любой5xx безопасным not_sent либо молча сменить модель/аккаунт |
| E55 | Неизвестная SDK/provider/internal ошибка | Raw typed code/status/request ID/phase и effect certainty | G/R нормализуют unknown без потери причины; H получает case/экспорт/решение. Есть рабочий выход из UI | Success по отсутствию известного кода; бесконечный повтор либо generic502 без evidence |
| E56 | Нет/истёк прайс | Profile/JWS/effective interval, был ли upstream dispatch | G не начинает новый billable attempt; обновляет тариф/резерв. H сохраняет запрос | Лечить отсутствующую цену повторным запросом к модели |
| E57 | Цена изменилась/резерв конфликтует | Exact quote/attempt/body version, not_sent proof | G пересчитывает до отправки; старый unknown reserve не освобождает без сверки | Списать дважды либо принять изменённый body прежним key |
| E58 | 400/422 invalid parameter/model/tool unsupported | Adapter capability exact node/model/API-version; не expiry ли это | Исправить конфигурацию для нового допустимого intent после подтверждённого отказа; сохранить исход | Retry неизменного заведомо невалидного запроса |
| E59 | Refusal/content filter/max-output/empty result | Native finish reason, complete text/tool blocks, primary code | H показывает частичный/отклонённый результат; новый разрешённый ввод отдельно. Policy refusal не обходится | Считать любой пустой result success или автоматически обходить фильтр |
| E60 | Context limit, слишком много reasoning/output | Фактическое SDK window, input vs суммарный расход, сохранённые эффекты | H/R согласуют compact или перенос context на безопасной границе, затем новый admissible input | Поднять цифру окна в UI без SDK proof, обрезать tool ledger |
| E61 | Compact ACK потерян/долгая compaction | Exact compact command receipt, SDK boundary/summary | G/R наблюдают ту же state-changing операцию; H не начинает второй compact/rebind | Считать сетевой timeout остановкой compact |
| E62 | Compact частично выполнен/summary недоступно | Native history boundary, mapping и отдельно payload summary | R восстанавливает evidence; H использует только подтверждённый context или asks restore | Выдать красивое summary за доказанную сохранность истории |
| E63 | pending_rebind: Astra→Sol/Claude→API | Старые binding/model/window и состояние compact/ACK | H/G сначала сверяют/сжимают старую историю на её модели, потом штатный rebind и новая lineage | Использовать старый Astra alias с выбранной Sol ради обхода guard |
| E64 | Reasoning/tools option не поддержан после смены | Requested/configured echo, policy revision и точный pinned node | G/R согласуют либо явно отвергают; H не переносит ожидающий body под старым key | Union каталога считать capability каждого узла |
| E65 | SDK обновился/поменялась схема native events | Installed versions, negotiated adapter support и raw event | R сохраняет unknown event, проверяет terminal/arguments, capability становится ограниченной | Угадывать meaning по красивому display name либо выполнять partial tool |

## 6. Оригиналы, media, API state и учёт

| ID | Сбой/симптом | Что выяснить | Порядок восстановления и сторона | Чего нельзя делать |
|---|---|---|---|---|
| E66 | Вложение больше лимита | Чей лимит: provider/proxy/SDK/storage/config; точные bytes и base64 overhead | G/H проверяют до dispatch; поддержанный resumable upload/другой файл/продолжение без файла; исходный текст доступен | Ввести скрытый15MiB либо бесконечно загружать невалидный старый файл при каждом сообщении |
| E67 | Upload ACK потерян/Router drain во время body | Input lease, request receipt, hash/metadata/byte-original | R завершает принятый I/O; G/H сверяют receipt; новым admission typed wait. Сохраняют4из4успешных originals | Объявить files protocol incompatible из-за503/restart |
| E68 | Catalog/ACK есть, оригинала нет/ACL сменился | Payload existence, hash/retention, authorized client original | G/R явно unavailable/forbidden; H может восстановить **точные bytes** отдельно по проверяемому upload recovery | Притвориться, что модель видит файл, или исполнять прежнюю задачу для его восстановления |
| E69 | Signed media URL истёк | Artifact ID, доступ и сохранённые bytes | G/R перевыпускают разрешённый locator/читают durable artifact, H восстанавливает карточку | Генерировать картинку второй раз вместо доставки первой |
| E70 | Image generation incomplete/получен job ID без файла | Terminal/job receipt, polling/retrieval capability, media scope | Adapter читает существующий job/result; при missing явно сообщает частичный исход и цену | Считать attachment UI подтверждением завершения, повторять оплаченный job |
| E71 | SDK web IN/OUT null, индикатор застрял | Native event phase/type, result/error и eventual terminal | R/G нормализуют lifecycle; H закрывает индикатор по фактам. Документирует неподтверждённые поисковые результаты | Выдавать null за доказанную остановку всей задачи либо несуществующий успешный поиск |
| E72 | Provider вернул incomplete response/прервал200stream | Endpoint finish/error event, response ID, side effects | API-adapter сверяет stored response либо сохраняет stateless partial/unknown; новый attempt только по политике | Retry всего SDK-like поручения на основании HTTP200 |
| E73 | Usage отсутствует/частичен/неразбираем | Provider attempt, cache/read/write/reasoning fields и billing policy | G сохраняет usage_unknown, reserve ждёт settlement/reconcile; H честно показывает неопределённость | Приравнивать отсутствующий usage нулевой цене/квоте |
| E74 | Повторная доставка terminal/двойной watcher | Stable usage receipt/attempt ID, delta vs cumulative | G/R/H дедуплицируют logical settlement; реальные новые оплаченные attempts учитываются отдельно | Удвоить usage при чтении receipt или спрятать реально оплаченный retry |
| E82 | previous_response_not_found/store=false/retention expiry | Endpoint действительно stateful? Есть канонический input и output effects? | Adapter использует разрешённый full context без replay effects; DeepSeek не выдумывает retrieval. Политика ZDR сохранена | Включать store=true без разрешения, считать cache долговременной history |
| E83 | Нужен fallback на другую модель/поставщика | Права/стоимость/data-class, portable context и pending effects | G предлагает только разрешённый явный rebind после сверки; legacy SDK history не мигрирует автоматически | Молчаливый переход subscription→платныйAPI или другой аккаунт |

## 7. Эксплуатация и пользовательские гонки

| ID | Сбой/симптом | Что выяснить | Порядок восстановления и сторона | Чего нельзя делать |
|---|---|---|---|---|
| E75 | Gate crash после forward/до local receipt | Router exact command state и Gate store epoch | G восстанавливает watchers/receipts/reserves по immutable IDs; H ждёт recovery | Ещё раз вызвать SDK на другом Router |
| E76 | Restore старой БД, rollback потерял новые tombstones | Последняя согласованная epoch, внешние effects и surviving ledgers | Ops/G карантинируют недоказанные операции; восстанавливают согласованные proofs; чтение истории по доступу остаётся | Считать отсутствие записи в старом backup not_sent |
| E77 | NAT connector lease/сертификат/канал сменился | Node/boot/mTLS identity и exact Router receipt | Connector обновляет transport/сертификат; G/R сверяют исходный command. Доставка ACK не повторяет SDK | Timeout relay lease выдавать за неисполнение |
| E78 | Deploy/drain во время input/compact/SDK close | Active/queued/subagents/reserved/input I/O и pending ownership | Guard останавливает новый admission и ждёт безопасной границы; backend/user history не меняется | Restart по одному activeTurns0, пока ещё открыт body или child |
| E79 | Дубли cron occurrence/очереди/два окна | Stable occurrence/inbox ID, parent claim и expiry | H/G допускают один producer, coalesce/skip по явной политике; просроченная задача получает честный исход | Переисполнить все накопленные occurrences после login без проверки актуальности |
| E80 | Удаление/архивирование чата во время доставки | UI archive vs history deletion, retention/ACL, late command fences | H/G сохраняют факт удаления, закрывают admission и позднюю доставку по политике; экспорт/учёт остаются где разрешено | Возвращать удалённый чат автоповтором или удалять общий SDK home |
| E81 | Cancel дошёл после внешнего effect | Доказательство effect и принятия cancel | X/H фиксируют эффект выполненным, cancel не откатывает; возможная компенсация — новый разрешённый intent | «Отменено» трактовать как «не было списания/письма/записи» |
| E84 | Профиль/сервер/клиент разных версий | Advertised coordinator и exact pinned-node capabilities | H/G/R используют поддержанный subset, показывают upgrade/recovery path; неизвестные extension fields сохраняют по wire правилам | Выдавать невыпущенный recovery API за существующий либо ломать все legacy чаты |

## 8. Дополнительные границы SDK, API и координатора

| ID | Сбой/симптом | Что выяснить | Порядок восстановления и сторона | Чего нельзя делать |
|---|---|---|---|---|
| E85 | Claude resume сам продолжает interrupted turn | Effective env/CLI options, native deferred tools, cancellation epoch | R отключает native auto-continuation для history-only и квалифицирует no-inference/no-tools; иначе supported=false | Считать отсутствие нового host prompt доказательством отсутствия исполнения |
| E86 | ResultMessage от injected turn/старого interrupt buffer | Origin, own query/input, session/generation, terminal_reason | R атрибутирует result прежнему/чужому query, дочитывает Stop до следующего dispatch | Закрыть Harness Run/writer или потребить permit по чужому success/text |
| E87 | Claude ConversationResetMessage внутри живого transport | Новая lineage, фактический следующий session_id, старые effects/ACK/usage baseline | R/G фиксируют reset; неожиданный reset→reconcile; H не наследует old permits новой истории | Выдать new_conversation_id за session_id, обнулить учёт или объявить сохранённую old history |
| E88 | Claude API pause_turn/server-tool result ожидается | Полная последовательность content, server tools/container, Stop | API loop сохраняет paused state; новый attempt по допуску, без переделки content и host replay | Считать message_stop завершением всей задачи или ждать несуществующий host tool_result |
| E89 | Chat finish_reason получен, usage tail потерян | Полнота selected choice/calls, durable finish и отдельный usage | Adapter применяет завершённый ответ/calls один раз; usage_unknown сверяется независимо | Повторить модель из-за отсутствия usage chunk/DONE |
| E90 | Два recovery cases одной source branch | Общие branch generation/control epoch, unique successor source tuple | G сериализует cases общим controller; единый successor даже при разных manifests/permits | Ограничиться CAS одного case или native writer lock |
| E91 | Local commit выполнен, первый ACK пришёл после revision advance/Stop | Immutable plan/action/evidence/source, ACK key/commit hash и current access | G принимает monotonic historical-applied ACK; новые действия только по текущему fence; H не повторяет local apply | Требовать новый local commit для ACK новой revision или воскресить отменённый Run |
| E92 | Connector потерял proof/retry headers или отверг recovery path | Exact path/method/header allowlist и версии всех hops | G/R согласуют транспортную capability; unsupported/legacy обозначены честно; только нужные allowlist расширения | Рекламировать direct capability по всей группе или обойти auth произвольным tunnel |

## 9. Выходы из восстановления

Каждая строка заканчивается одним из следующих исходов, с причиной и свидетельствами:

| Исход | Что увидит пользователь | Можно ли продолжать |
|---|---|---|
| ready | Прежний результат восстановлен; следующая операция подготовлена | Да, по исходной или явно новой согласованной binding |
| waiting | Конкретно: writer/auth/provider/network/client executor; есть следующее условие проверки и Stop | Автоматически после условия; независимая работа остаётся доступна |
| needs_input | Войти, разрешить расход/rebind, предоставить original/внешний receipt, сверить эффект | После конкретного решения, без сброса истории |
| evidence_unavailable | Что утрачено и какие summary/ACK/backup остались | Через разрешённый restore/новую изолированную ветвь; повтор эффектов запрещён |
| forbidden | Отозван доступ/нарушена policy | Только после штатного изменения прав, не через retry |
| unsupported | Adapter не умеет доказанное продолжение | Read/export/новый явный запрос после сверки; не silent replay |
| cancelled | Пользователь прекратил конкретный intent/recovery | Автоматического возобновления нет; новое поручение отдельно |

`waiting` должен иметь observable состояние, последнюю попытку, владельца следующего
шага и условие выхода. Сеть может оставаться недоступной долго, но это не оправдывает
busy loop, бесконечный визуальный spinner без причины или блокировку всех ветвей.
