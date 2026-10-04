# Миграция встроенного Gate

Сохранить SQLite через backup API, signing key, org JSON/history, защищённый
keyring secret и provider env. Последние два переносит оператор отдельно без
печати в manifest. Сохранить Router journal и SDK home/history под тем же
пользователем/аккаунтом. Проверить restoration отдельно: копия одного db при
живом WAL недостаточна.

Новый Gate подготовить на отдельном порту. Перед финальным снимком drain новых
бесед, завершение ходов, остановка старого control plane по процедуре обслуживания.
Не запускать старый и новый Gate на расходящихся активных копиях данных.

```text
uv run atlas-gate-migrate --source-db C:/old/gate.db --source-orgs C:/old/gate/orgs --source-signing-key C:/old/gate/signing-key.pem --destination C:/ProgramData/Atlas/gate --legacy-node router-a
```

Destination новый; существующий не перезаписывается. Source read-only, SQLite
backup/quick_check; перенос signing key и JSON orgs, device/token/profile сохраняются.
SDK credentials не копируются; внешние ссылки в orgs отклоняются. Настроить новые
пути DB/key/orgs и **тот же keyring secret**. Тот же signing key сохраняет kid/JWS
доверие. Provider keys — из защищённого источника.

legacy-node допускается только для Router с прежним SDK профилем/history. Таблица
legacy history связывает native SDK ID с device/org/node. Чужая история недоступна.
Старые активные HTTP SID встроенного Gate автоматически не переносятся: дать
завершиться, клиент открывает новую сессию с прежним SDK resume ID. Router должен
подтвердить resumed=true; пустая история вместо resume запрещена.

После приёмки сохранить прежний внешний HTTPS origin через proxy/DNS. Для cohost
Router 8765/Gate 8766 `/harness` направляется в Gate, прямой `/v1` — Router там,
где нужен клиентам. Реальную proxy конфигурацию проверить до cutover.
Проверить прежнюю регистрацию/profile, модели, свободный узел, tools, stop/tasks,
compact/resume, restart, quota/usage, revoke, node outage и ACK retry. Реальный
Claude и EDT — оставшаяся обязательная приёмка.

Rollback: остановить новый Gate, сохранить его snapshot, вернуть старый ingress
и согласованный старый Store. Не перезаписывать живую БД и не объединять записи
вручную. После появления новых сессий rollback требует сверки SID/usage:
слепой старый backup потеряет новые записи. SDK history остаётся на Router.
