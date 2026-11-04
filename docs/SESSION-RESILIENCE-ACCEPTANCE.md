# Приёмка контракта восстановления

Приложение к [RFC](SESSION-RESILIENCE-CONTRACT.md), редакция 1; актуализация 04.11.2026.
**План и критерии проверок. Таблицы не означают, что все сценарии уже проверены.**
Поставленная часть и квалификация отмечены в [матрице статуса](SESSION-RESILIENCE-STATUS.md).

## 1. Как выбирать проверки без лишних прогонов

| Изменение | Необходимые проверки | Что не запускать автоматически |
|---|---|---|
| Только этот документ | Markdown links/anchors, JSON примеры, непротиворечивость current/target и источников | Runtime suites, сборки SDK/EDT/UI, платные inference, перезапуск служб |
| Gate recovery ledger/CAS/API | A01–A05, A10–A18, A32, A40–A45, A47–A48; real HTTP Gate→Router с controlled adapter | Неизменённые UI/model-catalog/sandbox suites |
| Router writer/history/Stop | A03, A06–A09, A19–A24, A30; Claude A49–A51 при его изменении; targeted native inert qualification | Массовые реальные рабочие поручения/все модели всех аккаунтов |
| Harness stale checkpoint/apply ACK | A10–A18, A25–A29, A34, A48; установленный клиент со старой беседой | Общую серверную регрессию, если server code не менялся |
| API-adapter | A07, A31–A38, A52–A55 **только для изменённого endpoint/provider** | Предположение, что OpenAI mock проверил Anthropic/DeepSeek; SDK-only compact/auth тесты неизменённого API adapter |
| Connector/protocol forwarding | A06–A08, A45, A56; direct/connector сравнение exact path/status/body/headers | SDK/model генерацию, когда меняется только header relay |
| Startup/обновление клиента и восстановление открытых чатов | A57: реальные старый/новый JAR, разные JVM и один workspace, несколько damaged и healthy sources | Production prompt/tools, изменение серверного runtime ради клиентского upgrade-теста |
| Executor effect ledger | A10–A14, A28–A30, A43; внешний контролируемый counter/read-back | Повтор настоящего письма/платежа/удаления ради smoke test |
| Хранение/restore/deploy | A21–A24, A40–A46; guard-required qualification | Нагрузочную «пробу» в пользовательской истории |

Набор расширяется только при новом изменении, сбое или незакрытом существенном риске.
Обязательный установленный release guard не обходится. Долгий прогон сам по себе
не сильнее нескольких точных failure-injection сценариев с доказанными outcomes.

## 2. Стенд и оракулы

Использовать временные Gate/Router/клиентские БД, отдельные устройства/организации,
свободные порты, синтетические SDK histories и оригиналы. Бизнес-эффект имитирует
внешний сервис: увеличивает durable counter и выдаёт lookup receipt по effectId.
Исполнитель сохраняет invocation/output отдельно от SDK и HTTP. Сеть управляемо
рвётся до/после каждой указанной границы, а не случайным sleep по времени.

Оракулы обязательны: counter; записанные key/body hashes; native thread identity;
lease/boot/recovery revisions; source/target Run; call IDs; receipts; cursor; history
boundary; policy decision; usage attempts и client-applied state. Успех проверяется
по состояниям и счётчикам, а не по фразе модели «готово».

Fake SDK используется для воспроизводимых гонок и всех cut points. Native SDK
проверяет ровно нужную семантику: собственный inert thread, resume/close/compact
только если это изменённый путь, без пользовательских side effects. На настоящем
аккаунте не тестировать «exactly-once отправку письма» генерацией реального письма.

## 3. Атомарность, идентичность и доставка

| ID | Сценарий | Обязательный наблюдаемый результат |
|---|---|---|
| A01 | Два одинаковых create/apply одновременно | Один logical action/native open; одинаковые immutable receipts |
| A02 | Тот же key, изменённый body/target/owner | Conflict до эффекта; старый результат не перезаписан |
| A03 | Resume из двух bindings одной history | Не более одного writer; active owner не прерван; после release новый transport сходится к ready |
| A04 | Два клиента применяют разные recovery revisions, в том числе разных cases одной branch | Branch controller допускает один совместимый action; второй читает snapshot; per-case CAS недостаточен |
| A05 | Crash на каждом шаге prepare→admission→dispatch→receipt→client apply | Исход после рестарта получен из ledger либо честный unknown; отсутствие ACK не вызывает второй effect |
| A06 | Boot поменялся, node/account/history прежние | Identity сверена; старый journal читается без нового SDK prompt |
| A07 | SSE/WS рвётся до данных, в тексте, в tool args, после terminal | Дочитывание прежнего результата либо typed unknown; partial tool не исполняется |
| A08 | Replay/gap/reorder cursor, несколько observers | Применено по одному разу, gap дочитан; нет второго producer/tool execution |
| A09 | Истёк payload, сохранён summary receipt | Available summary/usage и unavailable text/media раздельно; success не выдуман |

## 4. Tool effects, stale state и Stop

| ID | Сценарий | Обязательный наблюдаемый результат |
|---|---|---|
| A10 | Side effect→output durable→tool_result accepted→SQLite busy при local ACK | Counter1; повторяется только фиксация/receipt/output delivery; ACK применяется один раз |
| A11 | Side effect→crash до output, external lookup есть | Counter1; исход восстановлен read-back, не новым tool execution |
| A12 | То же, lookup/idempotency нет | Effect-unknown и конкретное решение; зависимые действия заблокированы, независимая работа доступна |
| A13 | Новый callId просит ту же domain operation | Domain effectId предотвращает дубликат; легитимные разные операции не склеены |
| A14 | Pending output предъявлен в чужой Run/root/session | Никакой silent rebind. Recovery старой операции под её identity, затем новый Run |
| A15 | Завершённый old Run, новая модель/агент, старый checkpoint | On-open/send вызывает coordinator до fatal guard; terminal/ACK освобождают old checkpoint, история сохранена |
| A16 | В одном manifest ACK accepted, terminal failed, другой effect unknown | Каждый outcome различён; готовое применяется, unknown изолирован; общий blanket success не выдаётся |
| A17 | Старый prompt не найден в Gate, возможна гонка admission | Atomic fence или доказанное admission, никогда оба; поздний старый POST не проходит после fence |
| A18 | Неполный legacy checkpoint: нет исходного body hash/key | Нельзя выдать отрицательный proof. Сохранено доступное evidence, есть repair/export/решение |
| A19 | Tool side effect1 и принятый ACK; **SDK turn ещё active**, затем Stop→resume | Stop приводит к terminal; cancelled intent не воскресает; history/ACK сохранены; tool counter1 |
| A20 | Native SDK willRetry=true, затем terminal или Stop | Один producer; после terminal прежний willRetry не даёт права на новый prompt; первичная причина сохранена |
| A21 | Stop до dispatch, offline Stop, Stop во время deploy | Непосланный intent не уходит после восстановления; поздний control не отменяет новый turn |
| A22 | Прежний idle writer; активный compact; неизвестный внешний writer | Только доказанно idle собственный transport закрывается; compact/foreign owner не прерываются |
| A23 | Lease истёк, PID reused или registry исчез, native owner жив | Никакого kill/unlink lock или второго writer по возрасту/heartbeat |
| A24 | ACK compact потерян, затем новый prompt/model switch | Сверка старого compact; нет второго compact и недоказанной подмены history boundary |
| A25 | Закрыть окно/перезапустить клиент сразу после result | Result/output применяется из сохранённого receipt; model/tool не повторяются |
| A26 | Agent change пришёл после Stop, но до применения ACK | Старые proofs применяются под старой binding; новое намерение не перезаписывает old operation |
| A27 | pending_rebind Astra→Sol при заполненном окне | Compact использует прежнюю модель/history, затем rebind; старый alias не выдаётся новой модели |
| A28 | Parent Stop, child/foreign effect завершился поздно | Child receipt остаётся связан с parent; нет ложного rollback/воскрешения parent |
| A29 | Два окна/устройства и одновременно cron occurrence | Один claim исполняющей ветви, stable occurrence ID; политика overlap/catch-up соблюдена |
| A30 | Fresh4uploads→restart до первого prompt и другой case с unknown dispatch | Только virgin case может получить пустую замену; bytes/hash/scope/ACK сохранены; unknown case защищён |

## 5. Подписки, Claude и обычные API

| ID | Сценарий | Обязательный наблюдаемый результат |
|---|---|---|
| A31 | Claude Agent SDK login lost→browser login→новый auth epoch | Fresh readiness; exact session ID resume при доступной history; прежние tools не повторены; отменённый Run не воскрес |
| A32 | OpenAI/Codex transient429 против subscription quota и model capacity | Разные причины/scopes; Retry-After не продлевается GET; нет вложенных retry loops |
| A33 | Прямой Claude Messages HTTP200, затем SSE error; partial tool block | Не success; partial call не исполняется; канонические block/tool pairs сохранены |
| A34 | Прямой API output получен, client ACK/save упал | Восстанавливается сохранённый output/receipt, а не весь model+tools loop |
| A35 | OpenAI Responses stored/retrievable vs store=false, WS reconnect | Читается прежний response либо честный unavailable/context recovery; storage policy не изменена |
| A36 | DeepSeek Responses-compatible endpoint без previous_response_id/storage | Не выдумана server session/retrieval; контекст/tool outputs сохраняются client loop |
| A37 | DeepSeek empty lines/keep-alive и thinking+tools | Нет пустого success/false timeout по отсутствию текста; native необходимые поля не отброшены |
| A38 | API402/price missing/quote changed и paid retry | Нет inference при недопуске; settlement/unknown reserve честны; реальные attempts не скрыты |
| A39 | MCP session expired, требуется initialize; старый tools/call outcome unknown | Transport восстановлен отдельно; business call не повторён, native required server не отключён молча |

Конкретный adapter получает также один capability conformance сценарий для
заявленного retrieve/cancel/idempotency/continuation. Если функция unsupported,
проверяется корректный отказ и выход, а не симуляция успешной поддержки.

## 6. Хранение, доступ, эксплуатация

| ID | Сценарий | Обязательный наблюдаемый результат |
|---|---|---|
| A40 | Чужие device/org/branch/document, revoked node/token, policy changed | Deny на чтение и apply; recovery не расширяет права; разрешённый own result читается по политике |
| A41 | Disk full/SQLITE_BUSY до и после effect/ACK | До эффекта нет dispatch; после нет повторения; сохранён безопасный аварийный read/recovery путь |
| A42 | Restore старого backup без новых receipts/fences | Новый store epoch/quarantine; отсутствие записи не даёт not_sent proof; rollback не оживляет поздний запрос |
| A43 | Ошибка внешнего эффекта и последующая компенсация | Не называется автоматическим rollback; отдельный разрешённый intent/effectId, audit сохранён |
| A44 | Повтор terminal/usage receipt/case apply | Usage settlement один на попытку, client-applied один; новое платное inference учитывается отдельно |
| A45 | Gate/Router/client mixed versions и один node без capability | Advertised subset честен, pinned resume не использует union; legacy контракты не ломаются |
| A46 | Deploy с active input body/SDK close/child/compact | Нет stop до безопасной границы; guard/maintenance/postflight проходят; production history не используется как тестовый счётчик |

## 7. Дополнительные границы независимого аудита

| ID | Сценарий | Обязательный наблюдаемый результат |
|---|---|---|
| A47 | Два окна, разные manifests/keys/recoveryIds одной source branch/turn; sourceA завершён до регистрации sourceB; crash до seal; последовательное освобождение writer | До полного inventory seal ready/admission нет; новый source повышает generation; общий branch fence/unique source successor дают один continuation; несколько Runs представлены без подмены binding |
| A48 | Local apply commit→новое evidence/revision или Stop→первый ACK→crash/retry | Принят historical-applied immutable факт; local apply не повторён; cancelled Run/новый transport не ожили; changed ACK hash конфликтует; deny revoked owner |
| A49 | Claude interrupted history+унаследованный RESUME_INTERRUPTED_TURN=1 и разрешённый settings env=1+persisted Stop; отдельно pending deferred tool | Effective startup/runtime settings не включают автоисполнение; history-only restore не вызывает inference/native tool/Run resurrection; если нельзя доказать, typed unsupported |
| A50 | Injected result с текстом/success между own prompt и result; buffered old result после Stop/new input | Origin/query/generation отделены; own Run не завершён чужим result, writer/permit не освобождены; старый terminal применён только прежнему query |
| A51 | Claude reset между queries и при pending ACK/late events; SDK usage totals reset | Новая lineage/session ID доказана; old receipts/effects/usage сохранены; ни false resumed history, ни old permit в новой истории |
| A52 | Claude API pause_turn несколько раз, mixed server/client tools, Stop между attempts, пустой end_turn | Paused content/server state сохранены без изменения; новые attempts учтены; Stop запрещает continuation; host calls counter1; пустой завершённый ответ не unknown |
| A53 | Chat HTTP/SSE обрыв до/после finish_reason и до usage tail; multiple choices/parallel calls | Только выбранная полная choice; incomplete call не исполняется; completed text/calls не повторены из-за lost usage; length/filter отдельны |
| A54 | Responses completed с function_call; incomplete max_output_tokens; refusal content; store=false background | API status/reason/Run не смешаны; waiting-tools; data policy учитывает временное background storage, retrieve только при реальной поддержке |
| A55 | Документированный nonstream Claude529/DeepSeek503 admission rejection после HTTP dispatch против generic5xx/midstream200 error | Только доказанный отказ до inference даёт новый attempt по policy/backoff; unknown/partial не повторены; причина/расход сохранены |
| A56 | Direct и connector: input proof header, Retry-After, command/create headers; новый cases path; legacy node | Negotiation не теряется; current connector gaps явно unsupported; capability равна всему пути; нет произвольного admin tunnel |
| A57 | Реальные старый и новый JAR в разных JVM на одном сохранённом workspace; минимум два damaged и один healthy открытый чат; вкладки/selected tab, SDK history, compact/media, checkpoints/cursors/outputs; автоматический startup без клика/нового prompt | Все открытые чаты восстановлены; законные identity proofs и saved terminal применены один раз под source CAS с аудитом, keys/hashes/история сохранены; бизнес-счётчики не растут. Healthy чат не закрывается и не блокируется спорным source. Недоступный proof даёт точный статус конкретного чата; unsupported не подменяется успехом. Приёмка относится к точным версиям JAR, не к новому пустому workspace |

## 8. Выход из инцидента Зинаиды

1. В точной старой беседе сохранён old Run со stale checkpoint и отменённый следующий intent.
2. Gate получает manifest, находит успешный terminal и12accepted ACK; не угадывает
   вид pending по одному коду agent_delivery_binding_changed.
3. Если локальный output доступен, hashes совпадают; missing output восстанавливается
   только из реального источника. Непредъявленный prompt получает receipt/fence.
4. Harness применяет proofs одной транзакцией, сохраняет old cancellation и историю,
   освобождает новый Run; Gate получает client-applied ACK.
5. Один **новый явный** ввод в этой же беседе проходит обычный admission/resume;
   cancellation старого не отменяет новый. Native history подтверждена либо честно
   согласован отдельный context recovery.
6. Счётчики прежних12эффектов не увеличились, usage/ACK не удвоены, история доступна.

Ни открытие нового пустого тестового чата, ни thread/read без успешного client apply
не закрывают эту приёмку. Read-only проверка истории полезна, но её предел надо назвать.

## 9. Форма отчёта поставки

Отчёт содержит commit/version всех изменённых компонентов, exact SDK/API версии,
advertised и реально квалифицированные возможности, выбранные A-ID, результаты,
границы mock/native/installed-client проверки, side-effect counters, proof paths
и факт отзыва тестовых устройств/закрытия своих транспортов.

Отдельно: что не проверяли и почему; остался ли client guard/неопределённый effect;
доступен ли **exact** старый turn, а не похожая другая беседа; можно ли открыть следующий
ввод. Нельзя объявить общий контракт реализованным по одной компонентной поставке.
