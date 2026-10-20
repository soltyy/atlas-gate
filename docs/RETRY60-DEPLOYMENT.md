# Gate #60 — выпуск и установленная приёмка

20 октября 2026, проверка завершена в 12:21 МСК (09:21 UTC).

## Состав поставки

- Gate **0.4.7**, код `29cf3cbefd36de60645a7727406cc2fffef40a62`, merge
  `f38b65f07080e449c21e75c63264024d4bbcb0bc`, [PR #61](https://github.com/soltyy/atlas-gate/pull/61).
- Оба Router (Codex/Claude) **0.8.4**, код `ed0751981b79e5ff4fed28a90c8825559ee9ac5c`,
  merge `334b7e909967f94b4524ef0e8b428078d1501597`,
  [PR #71](https://github.com/soltyy/atlas-router/pull/71), Router #70.
- Wheel SHA256: Gate `ac1a6abaa737b26277396f304b5ddb2c026a2134545eb50551ade907d9ad87fb`;
  Router `45844fff797727c09662e7b9631f0417dd69e81216ef064a7ae1987b600e7785`.
- Runtime: `/opt/atlas/releases/gate-0.4.7-retry60` и
  `/opt/atlas/releases/router-0.8.4-compact70`, прежние service ExecStart сохранены
  через `/opt/atlas/gate` и `/opt/atlas/router`.

## Проверки

| Проверка | Результат / граница |
|---|---|
| Новые regressions до исправления | 5 failed: prompt/control до приёма и relay 502 после приёма, policy чтения receipt |
| Полная Gate suite, Windows | 164 passed, 6 skipped |
| Полная Router suite, Windows | 344 passed, 36 skipped |
| Собранные Linux wheel, изолированный HTTP Gate/Router на VDS | 48 passed; direct + настоящий connector, временные DB/keys, stub SDK |
| Установленный Router deployment_check | 8 параллельных synthetic children и положительный предел — PASS; отдельный от live SDK результат |
| Публичный HTTPS → Gate → Codex gpt-6.1-sol и Claude claude-fable-5-1[1m] | SDK session-create/replay, SDK-tools ACK, загрузка оригинала, GET receipt по ID/исходному ключу, duplicate upload, fingerprint conflict, запрет data-class, close и чтение сохранённого receipt — PASS |

Во время HTTPS-приёмки inference не вызывалась. Байты загружались в настоящие
сессионные хранилища. Создано отдельное QA-устройство, диагностические сессии
закрыты, QA-устройство отозвано. Пользовательские задания повторно не запускались.

Первый общий прогон выявил регрессию квоты для доказанного pre-send node_unavailable:
после исправления not_sent резерв освобождается. Остальные первичные отказы были
ошибками стенда: restart без освобождения journal lock, отсутствие Gate metadata
в общем тестовом venv; в Linux-копию тестов сначала не перенесли asyncio_mode=auto.
Указанные выше зелёные прогоны выполнены после исправления соответствующего стенда.

## Переключение и откат

Перед переключением оба Router переведены в drain; activeTurns=0 и
activeSubagents=0. Выполнены SQLite backup API (с учётом WAL) для Gate/обоих Router
и SQLite SDK; конфигурации и SDK-профили сохранены в приватной резервной копии
`/var/backups/atlas/retry60-20261020T091649Z`. Данные не переносились в Git.
Заранее подготовлены новые venv/wheel, затем переключены ссылки и перезапущены
atlas-gate, atlas-router, atlas-router-claude. Drain снят после проверки готовности.

После поставки обе SDK готовы, версия Router=0.8.4, Gate discovery=0.4.7.
Конфигурации `/etc/atlas/*.env` и signing key побайтово совпали с резервной копией;
SDK homes, документы и постоянные БД остались на прежних путях. Откат кода —
обратное переключение ссылок на gate-0.4.6-9e70be3 и router-0.8.3-b1ad3d70870b
при drained/idle узлах с последующим запуском служб и снятием drain.
Автоматический откат БД поверх новых пользовательских событий не выполняется.

Приватные server receipts: `/var/lib/atlas-gate/retry60-acceptance.json`,
`retry60-deployment.json`; сводная версия внесена в deployment-manifest.json.

## Что эта поставка не доказывает

Новый UI/transport retry/pending jobs установленного Harness не поставлялись:
[Harness #328](https://github.com/soltyy/atlas_harness/issues/328),
[#329](https://github.com/soltyy/atlas_harness/issues/329) и UI #305 отдельные.
Изолированный протокольный HTTP-клиент не выдаётся за проверку GUI Harness.
Происхождение сетевого ConnectTimeout исходного инцидента не установлено.
Реальное сжатие пользовательской истории 405k в этой приёмке не повторялось;
удаление wall-clock пределов и обработка ошибок квалифицированы сценарно.
