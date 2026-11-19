# Аудит действующих контрактов, 19.11.2026

Область: клиент → Gate → Router → durable journal и обратная доставка.
Проверка чтения исходников и изолированных HTTP-сценариев отделена от живого
inference и установки клиентского приложения. Рабочие ходы этим аудитом не
перезапускаются. Ни один произвольный внешний effect не получает обещания exactly-once.

## Срез и критерии

| Компонент | Проверяемый опубликованный срез |
|---|---|
| Gate | 0.4.43, release `bfda35146c39ebc608138b6639dac92b5f3b8bd5`; исходники main `4ea84530e0729f0f3cf2e5927e16a6ee39d126a5` |
| Router Codex и Claude | 0.8.31, `b9c1f8979da6a9214acb330e5a8a879f5b3be11d` |
| Atlas Harness | 0.1.183, main `ad06d4daad61ee6e692424b64da6d3d6e347ff96` |
| EDT | 1.0.0.202611190128, `2500376abefd72b52b13a7cd0ef20850db439d51`, канал `program/s1-edt-api` |
| Чат 1С | По явному уточнению пользователя аудит продолжается без его исходников; серверный `/harness/*` проверяется, клиентская приёмка 1С исключена из этого прохода |

На живых узлах read-only подтверждены Gate 0.4.43, оба Router 0.8.31,
protocolVersion=1, прежние boot IDs, ready=true, draining=false. Gate и Router
работают на разных машинах через защищённый частный канал. GET `/v1/node`
проверяет SDK/auth/model readiness: длительность probe не равна сетевой задержке.
Клиентские версии в таблице — опубликованные, не утверждение об установке на
каждом рабочем месте. Исходники старых локальных веток не использованы как baseline.

Конечные критерии: реальные пути и DTO совпадают; capabilities не объявляют
непроведённый путь; потеря запроса/ACK не повторяет инструменты; узел/boot/owner/
turn/call/key/body сохраняются; старый клиент сохраняет прежние операции;
найденные расхождения имеют воспроизведение, исправление и отрицательные проверки;
после основного прохода выполняется независимый агентский аудит.

## Карта фактической проводки

| Механизм | Клиент ↔ Gate | Gate ↔ Router и предел гарантии |
|---|---|---|
| Enrollment / токены / профиль | `/harness/enroll/*`, `/token/*`, `/profile`, `/settings`; JWS EdDSA, version/TTL, owner/device | SDK credential остаётся на Router; Gate не импортирует SDK. Самостоятельный direct API к провайдеру обходит этот контракт |
| Same-device auth v1 | Отдельные `/harness/auth/v1/*`, challenge/proof/rotation identity | Старый refresh/enroll не меняет смысл. Новый device не является восстановлением старого |
| Node discovery | Подписанный профиль — каталог, не доказательство конкретного SDK | `/v1/node`, token и node/boot guards; новые сессии выбирают узел, существующие закреплены в SQLite |
| Create / strict resume | POST `/harness/agent/sessions`, прежние key/body, `resumed` строго boolean true | POST `/v1/sessions`, durable command. Gate SID, Router SID, SDK history alias — разные идентичности. В create ACK нет договорного `sdkSessionId` |
| Prompt | Async 202/turnId, stable intent; SSE — представление того же producer | `?mode=async` + journal. ACK приёма не означает завершения; legacy Router SSE имеет отдельную cancel-on-disconnect семантику |
| Events / tail | GET `/turns/{turn}/events?after=N&wait=M`, nextAfter и локальная дедупликация | Только чтение исходного turn/cursor, страницы 512/8 MiB. done относится к последней странице. 410 → summary receipt, 404 не разрешает replay |
| Результат инструмента | `/tool_result`; toolResultRecovery v1, исходный call/key и canonical JSON digest | Exact waiting callback и journal; initialAdmission proof отдельно. Accepted доставка не является новым запуском инструмента |
| HTTP receipt | `/commands/{commandId}` либо `/commands?requestId=<original key>` | GET `/v1/commands/{id}`. 202 pending и HTTP command complete различаются; complete 202 prompt — ACK admission, не terminal SDK |
| Native досыл | `/steer`, expectedTurnId/clientMessageId, обязательные contract/target-boot headers | Native consumed evidence отдельно от accepted. Старый `{text}` не получает durable absorption автоматически; новый turn не наследует старый досыл |
| Ответ на вопрос | questionAnswers v1 только root ask_user; отдельный POST и GET | Серверы опубликованы. Точные исходные UTF-8 bytes/SHA/key/UUID/node/boot. Callback released и user text applied — две разные стадии. Harness 0.1.183 этот обработчик ещё не подключает (#545) |
| Stop | Legacy `/interrupt` session-scoped. EDT0128 сохраняет stop intent и при unknown читает его receipt | Router выбирает текущий ход при входе в handler. Полный settled ACK либо доказанное завершение producer нужны перед новым вводом. Нет turn-targeted capability; фиктивное поле expectedTurnId не защищает legacy Stop |
| Stop агента | `/tasks/{taskId}/stop` | Точный task ID; завершение дочернего callback и выполненные эффекты требуют собственных свидетельств |
| Compact | Отдельный command/outbox, Prefer respond-async, сохранённые SDK history/attachments | Native compact не новый prompt. Нельзя считать выполненным по времени, 404 или pending ACK; unsettled steers/questions блокируют compact |
| Legacy files | POST/GET `/files`, session-scoped manifest и receipts | Прежний API сохраняется; adaptive не включается отсутствующим полем или новой версией сервера |
| Adaptive files | Отдельный `/files/adaptive`, точные branch/boot/request/turn/call и bytes digest | Protocol3 connector, request/receipt. Старая доставка `/files` остаётся отдельной; capabilities bound-сессии обязательны |
| PDF / atlas_document / media | DTO и access policy клиента; `/documents`, `/attachments`, `/media` scoped reads | Набор Router SDK tools зависит от backend. Наличие локального инструмента Harness не доказывает Router capability. Доступ/отзыв и owner isolation проверяются отдельно |
| Recovery coordinator | `/harness/recovery/inventories`, seal/cases/evidence/apply/ack/cancel | `/v1/recovery/cases`; серверный plan не изменяет локальную БД клиента. Client applied ACK обязателен; terminalContinuation=false на живых узлах |
| Input / writer recovery | Профиль discovery + pinned proof; нет нового prompt при startup repair | Virgin/empty только по proof, cold history и idle handoff зависят от SDK/config. Отсутствующая квитанция не подтверждает пустую историю |
| Direct managed API | `/harness/llm/v1/{chat/completions,messages,responses,images/generations}` | Отдельный API attempt ledger и pricing v1, Router не участвует. Legacy API без opt-in attempt не обещает dedup inference при повторном POST |
| Pricing / reasoning / usage | Подписанное расписание и selection; configured ACK точной route/model | Суммарный каталог не разрешает неизвестный режим на закреплённом узле. Usage dedup отдельно от inference; неизвестные цена/usage не превращаются в ноль |
| Connector | allowlist путей, методов, query и заголовков; protocol2 и protocol3 | Header negotiation проверяется на конкретном канале; cookies/auth произвольно не пересылаются. Loopback connector-тест не является новой внешней mTLS-квалификацией |
| Maintenance / update | Admission fence до drain; чтение/ACK ранее принятого сохраняются | Активные native units и callbacks учитываются до остановки. Аудит не меняет работающие службы |

Atlas Harness: фактические потребители находятся в `managed/agent_model.py`,
`recovery_contract.py`, `tool_result_recovery.py`, `adaptive_files.py`,
`steering.py`, `api_attempts.py`, `pricing_transport.py`. Прямой маршрут
`source=local` использует `/v1/sessions` и Router token; managed — Gate/device bearer.
Вопросы direct API в `runs/questions.py` не являются реализацией подписочного
questionAnswers v1. EDT: `RouterSidecar`, `GateRecoveryWire`, `RecoveryStore`,
`RecoveryCoordinator` и `GateProvider` сохраняют собственные checkpoint/receipts.

## Подтверждённые расхождения и корректировка

1. **Закрытый prompt зависает после обновления Router.** Gate сравнивал
   `routerVersion == 0.8.24` в `_closed_prompt_absent`. С Router0.8.31 существующий
   сетевой тест `[missing]` вернул202 вместо bound negative409. Candidate добавляет
   Router capability `closed_prompt_absence_v1` только при durable journal;
   Gate принимает её и сохраняет прежний квалифицированный fallback0.8.24.
   Сам по себе больший номер версии не разрешает отрицательное доказательство.
   Exact owner/node/boot/SDK identity, closed/busy/pending metadata, отсутствие
   command/turn receipt/events и повторный boot probe остаются обязательными.
   Уже принятый, expired, alternate-key turn или недоступный канал не становятся not_sent.

2. **Неизвестный Stop повторно направляется следующему ходу.** На двух настоящих
   HTTP-приложениях Gate/Router потерянный до Router Stop, затем завершение старого
   и запуск следующего хода приводили при повторе старого ключа к interrupt нового.
   Candidate запрещает повторный upstream POST неизвестного session-scoped Stop
   после missing receipt; только чтение исходной квитанции. Если ACK был потерян
   после admission, прежний completed receipt возвращается без нового interrupt.
   Это защита повтора, а не новая exact-turn Stop capability: впервые запоздавший
   HTTP Stop legacy-контракт по-прежнему не умеет атомарно адресовать старому turn.

3. **Статус документов отстал от реализации.** AGENTS ошибочно требовал Gate и
   Router на одной машине; questions doc называл выпущенный серверный контракт
   кандидатом; status/wire называли текущими версии11.11. Исправлены актуальные
   указатели. Исторические свидетельства и их ограничения сохранены.

4. **Python3.14 и воспроизводимость клиентского contract driver.** У Gate
   `return` внутри `finally` в producer API attempt не компилируется при
   warnings-as-errors на Python3.14 Harness. Вместо выхода из finally финализация
   теперь условная: уже записанный admission rejection не перезаписывается,
   `ready` гарантированно выставляется. Gate штатно работает на другом процессе,
   поэтому этот отказ тестового импорта не называется сетевым сбоем production.
   Сам TCP driver Harness объявляет file-checkpoint isolation вместо неполного
   объекта production-модели без route/DB. UTF-8 JSON fixture читается с явной
   кодировкой. Бизнес-поведение клиента этими правками тестов не изменено.

5. **Устаревшие проверки observation/admission.** Cold GET файлов с0.8.31 читает
   manifest/proof без native open; generation увеличивается при следующей явно
   допущенной изменяющей операции. Проверка теперь требует ноль новых SDK instances
   на GET и replay ACK, затем один подтверждённый cold restore на новом upload,
   прежние scopes/alias/manifest и отсутствие prompt. Симуляция старого reasoning
   Router исправлена на границе probe, которую реально вызывает scoped admission;
   прежний hook whole-pool refresh больше не использовался. Критерий отказа старому
   Router до создания SDK сохранён.

6. **Гонка двух повторов question-answer.** Независимый агент воспроизвёл:
   один POST задержан на negotiation, второй с теми же байтами/key уже принят
   Router, затем поздний unsupported первого превращал Gate receipt в terminal
   not_applied. Router фактически применял текст один раз, Gate оставался в ложном
   отказе. Добавлен durable dispatch fence, взаимоисключённый с preflight rejection
   под Store lock. После возможного dispatch отрицательный probe не создаёт
   доказательство неотправки; отрицательный fence, выигравший первым, запрещает POST.
   Маркеры хранятся в отдельной таблице: исходная десятиколоночная таблица и старый
   INSERT Gate0.4.43 работают при откате. Legacy без marker остаётся possibly sent.
   Это не повтор инструмента и не новый user message; исходные bytes/key/UUID сохранены.

7. **Gate терял structured callback refusal.** Router409 содержал status/callId,
   Gate превращал его в общий conflict, включая чтение сохранённой команды.
   EDT legacy читает top-level status, Harness — error.status. Теперь ограниченные
   known callback outcomes сохранены в обеих формах. Незнакомые значения/поля
   не повышаются до доказательства. Protected recovery сохраняет exact-body proof;
   answered само по себе не стало подтверждением SHA результата.

8. **Разные envelopes вложений Gate и direct Router.** Router `/attachments`
   возвращает capabilities, Gate — attachments. Harness читал только второй
   вариант и при bind, и при preflight. Исправлен общий parser с точным выбором
   по сохранённому transport path. Проверена полная цепочка rich result → bind →
   preflight для обоих путей, лимит whole batch и неизменные call/content/bytes.
   Нельзя включить поддержку по чужому envelope, без create ACK или с false flag.
   Реальные Router metadata сняты на изолированном SDK stub; опубликованный
   consumer0.1.183 на тех же данных давал false, candidate — true. Реальный SDK
   image inference этим не проверялся.

## Незавершённые возможности, которые не объявляются готовыми

- Harness #545: подключение подписочного questionAnswers v1 к карточке клиента.
- Gate #147 / Router #130 / Harness #568: per-role native subagent capabilities
  и exact ACK. Общий agents DTO не доказывает отдельную модель/reasoning на роль.
- Gate #77 / Router #96: universal terminal continuation. Живые capabilities
  явно возвращают false; tool-result ACK, resume identity и elapsed time его не заменяют.
- Клиентская поверхность 1С исключена пользователем из текущего прохода;
  серверные тесты не выдаются за её клиентскую приёмку.

## Проверки и поставка

Первичный свежий проход опубликованной серверной пары: **97 passed**, 139.73с:
session schema/create/list/resume/replay, questions, transport recovery,
tool-result recovery, steering и API attempts. Отдельно сохранены исходные FAIL
закрытого prompt (202 вместо409) и позднего Stop (новый ход interrupted).

После исправлений первый адресный проход: **10 passed**, 35.31с: прежние шесть
closed-prompt случаев и четыре Stop случая (direct/connector × потеря запроса/ACK).
Новые проверки future opt-in/no-capability/legacy fallback и расширенный impact
набор прошли в расширенном запуске: **73 passed / 3 failed**, 155.35с. Исходные FAIL
сохранены: два устаревших driver ожидания, описанных выше; POSIX mode600 на Windows.
После корректировки двух driver и API finalization: **68 passed**, 44.11с
(input/reasoning/API attempts/admission fences). Это отдельный проход, числа
разных наборов не суммируются в «полный suite».

Router: **21 passed**, 13.33с (node/connector/journal), затем **34 passed /
1 deselected**, 15.68с (recovery proofs/writer/resume/SDK tools; явно исключён
Linux worker на Windows). Сама capability проверена с NODE_DB и без неё.

Harness: сетевой create-restart/ACK-loss driver с настоящими Gate Nodes/Routing/Store,
pricing transport, steering, admission и HTTP delivery: **130 passed**, exit0.
Это Python3.14 на Windows и изолированные HTTP backend fixtures, не установленный
Electron/WebView и не новая живая приёмка платных API/SDK. Дополнительные клиентские
cases и независимый проход записываются по завершении.

После замечаний независимого агента окончательные questions/race/refusal/tool
recovery: **26 passed**, 49.12с. Включены обе противоположные интерливинговые
последовательности dispatch/rejection, legacy migration и старый ten-value INSERT.
Harness rich images: **22 passed**, включая настоящие bind/preflight внутри
rich-result сборки, обе HTTP-формы, whole-batch limit и авторизацию.

Дополнительный Harness core: **37 passed / 8 skipped**, 324.71с. Это обычные и
scheduled delivery, Stop/restart, compact binding, coordinator и старые SDK flags.
Первоначальные scheduled FAIL вызваны отсутствующей объявленной tzdata в старом
shared venv; tzdata2026.5 установлена только в собственный isolated target,
пользовательское окружение не менялось. Восемь source-stand HTTP fault cases
изначально выключены opt-in флагом; их отдельный запуск указан ниже. Один
приостановленный широкий adaptive прогон не называется PASS. Его адресный PDF
authorization/restore case: **1 passed**, 21.02с, без моков access checks, но с
мокированным documents HTTP.

## Независимый второй проход

Отдельный агент получил те же репозитории и исходную задачу, самостоятельно
прочитал handlers/DTO/headers/journal/клиентские потребители и обнаружил пункты6–7.
После исправления повторил собственные исходные HTTP воспроизведения: **4 passed**,
7.59с. Основные closed-prompt/Stop candidates независимо: **13 passed**, 41.92с.
В дополнительном direct-images изменении агент нашёл пропущенный второй consumer
(preflight); он также исправлен. Последний независимый полный клиентский
rich-result/bind/preflight тест: **4 случая PASS**, 6.86с wall.
После этих корректировок дополнительных подтверждённых блокеров в проверенном
scope агент не обнаружил. Это не универсальная live SDK/UI/внешняя TLS/mTLS приёмка.

POSIX mode600 проверен отдельно на Linux: выполнена исходная async-функция
`test_large_file_queue_metadata_stays_small_and_payload_survives_replay` с её
assertions, `pytest.raises` заменён стандартным `unittest.assertRaises` в driver.
Установленный Gate0.4.43, собственный TemporaryDirectory, 33MiB fixture,
owner isolation и ACK replay; PASS, каталог удалён при выходе. Тестовый файл
SHA256 `00dcb998d2cb9506e1715269c854d26ccf218eff9ea83062676fa5c49f45d9f2`.
На production не установлены pytest/новые зависимости и не менялись службы.
Windows FAIL mode600 сохранён; он не доказывает проверку Windows ACL.

Эти изменения пока **candidate**, не установлены в production. Серверные службы,
бизнес-сессии и пользовательская история не изменены. Нельзя закрывать связанные
тикеты по этому отчёту до поставки и требуемой живой приёмки. Удалению подлежат
только собственные временные QA данные; рабочие профили/БД/журналы сохраняются.
