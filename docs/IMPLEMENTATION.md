# Начальный этап разделения, 04.10.2026

Связанные задачи: [Gate #1](https://github.com/soltyy/atlas-gate/issues/1),
[Gate #2](https://github.com/soltyy/atlas-gate/issues/2),
[Router #22](https://github.com/soltyy/atlas-router/issues/22).

Реализованы самостоятельный Python-пакет и CLI Gate без SDK зависимостей;
перенесённые control plane/provider proxy/подпись/админка; HTTP RouterClient;
администраторский файл endpoint с ограничениями org/route/account;
дисковые bindings и Gate SDK resume aliases; выбор готового совместимого узла
по active/capacity, затем занятости SDK sessions; резерв concurrent create;
SSE клиента через async producer/polling; атомарная дедупликация usage результата.
Нагрузка SDK сессий ограничивается capacity, даже когда ход не выполняется.

Это первый проверяемый этап, а не завершённая production миграция. Остаются:

- [Router #23](https://github.com/soltyy/atlas-router/issues/23): durable
  create/prompt/control IDs, журнал событий и восстановление SDK после crash;
  без него сетевой запрос с неизвестным исходом не повторяется на другом узле.
  Запись unknown блокирует новые create устройства до сверки. Нет автоматического
  reconcile и дедупликации повторных клиентских POST с новым request ID.
- [Gate #2](https://github.com/soltyy/atlas-gate/issues/2): распределённое
  резервирование ходов/субагентов и общих лимитов аккаунта, полноценная готовность
  пулов/health в админке, account quota telemetry. Первичный cooldown HTTP 429
  общий для account_group; ограничения SDK из событий ещё требуют интеграции.
  Подписочная квота с неизвестным остатком не оценивается как свободная.
- [Gate #3](https://github.com/soltyy/atlas-gate/issues/3): mTLS/регистрация,
  отзыв/ротация сертификатов, NAT connector, Windows installer, миграция и rollback.
  Сейчас предварительно утверждённые endpoint с индивидуальным секретом и
  проверяемым TLS либо loopback; за пределами изолированного контура вводить
  только после реализации удостоверений и эксплуатационной приёмки.
- Вход/revoke устройства останавливает доступ, но отключённый Router требует
  последующей сверки закрытия его SDK-сессий. Gate watchers не восстанавливаются
  автоматически после рестарта; usage можно дочитать через client events.
- Полная real Claude/Codex и EDT-приёмка, события heartbeat, история retention,
  durable quotas/pending turn reconciliation и поколение владения — впереди.
  Несколько активных Gate и перенос бесед между SDK узлами не реализованы.

Исходный встроенный Gate в Router временно остаётся совместимым до миграции.
Работающий AtlasRouter и профиль организации не переключены этим этапом.

Проверка первого этапа: Gate — 12 passed в контуре с двумя HTTP Router/stub;
отдельно 9 passed без SDK и 3 сетевых skip. UI — 41 passed, typecheck/build;
wheel собран и проверен на отсутствие SDK/Router зависимостей. Router —
221 passed, 28 opt-in skip. Покрыты выбор менее занятого узла, конкурентные
create, одинаковые local IDs, владение, Gate restart, compact/resume, SSE,
возврат квоты при отказе до POST и interrupt после отключения маршрута.
Реальные подписки и EDT в этой приёмке не запускались.
