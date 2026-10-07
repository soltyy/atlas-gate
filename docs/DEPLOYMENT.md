# Размещение

Добавление из админки и объяснение ресурсов: [ROUTER-ONBOARDING.md](ROUTER-ONBOARDING.md).

Текущий согласованный вариант — оба продукта на новом сервере без изменений
действующего production: [PARALLEL-DEPLOYMENT.md](PARALLEL-DEPLOYMENT.md).

Gate 8766 и Router 8765 могут работать на одной машине: разные службы, конфигурации
и runtime каталоги. Дополнительные Router имеют разные порты, NODE_ID, NODE_DB
и SDK home/profile. Внешние установки используют HTTPS или исходящий connector.
Для cohost Router bind loopback. Административный токен обязателен и на loopback.

Ссылка подтверждения устройства должна открываться извне: настройка HTTPS ingress
и разграничение HTML/API описаны в [ENROLLMENT-ENTRY.md](ENROLLMENT-ENTRY.md).

Из `.env.example` создать защищённый `.env`, заменить REPLACE, задать абсолютные
runtime пути. `ATLAS_GATE_NODE_ENV_FILE` читает только token_env из одобренных
узлов; SDK credentials на Gate не нужны. Секреты/БД/ключи не добавлять в Git.

Router: `ATLAS_NODE_ID=router-a`, `ATLAS_NODE_DB=C:/ProgramData/Atlas/router-a/node.node.db`,
`ATLAS_GATE_ENABLED=false`, `ATLAS_TRUST_LOCAL=false`, непустой ATLAS_TOKEN.
NODE_CAPACITY — открытые SDK sessions, NODE_TURN_CAPACITY — активные units,
NODE_SUBAGENTS — резерв дочерних агентов; префикс всех настроек `ATLAS_`.
Каждый подписочный аккаунт имеет отдельный Router SDK профиль. Узлы одного аккаунта
получают одинаковую account_group/account_turn_capacity; общая сеть не делает
разные аккаунты одной группой.

nodes.json — массив объектов:

```json
[{"node_id":"router-a","transport":"direct","url":"http://127.0.0.1:8765",
"token_env":"ROUTER_A_TOKEN","orgs":["my-org"],"routes":["claude-sub"],
"account_group":"claude-a","account_turn_capacity":0}]
```

Удалённый direct: HTTPS обязательно; ca_file/cert_file/key_file позволяют Gate
предъявить сертификат операторскому TLS proxy Router. CA verification не отключается.
URL и org/route права назначает администратор, а не регистрирующийся узел.

## Исходящий connector и mTLS

В nodes.json transport=connector, без url/token_env; node_id/orgs/routes/group
сохраняются. NODE_DB обязателен для обоих транспортов.

1. `uv run atlas-gate-pki --ca-directory C:/ProgramData/Atlas/gate/pki --action init`.
   Передать CA fingerprint доверенным каналом.
2. `uv run atlas-gate-pki --ca-directory C:/ProgramData/Atlas/gate/pki --action server --directory C:/ProgramData/Atlas/gate/tls --hosts gate.example.org`.
3. Задать ATLAS_GATE_NODE_TLS_PORT=8767, NODE_TLS_CERT/KEY/CA с тем же
   ATLAS_GATE_ префиксом. CLI запускает два listener с одним Store.
   Node listener требует client certificate. Начальный enroll доступен через
   отдельный public HTTPS ingress; HTTP только loopback.
4. Создать invitation в admin Nodes, передать через ATLAS_NODE_INVITATION:
   `uv run atlas-node enroll --directory C:/ProgramData/Atlas/router-a/identity --gate https://gate.example.org --node-id router-a --ca C:/ProgramData/Atlas/router-a/gate-ca.pem`.
5. Сверить CSR fingerprint из локального вывода с admin и одобрить. Ключ остаётся
   на Router; invitation действует 15 минут.
6. `uv run atlas-node run --directory C:/ProgramData/Atlas/router-a/identity --router http://127.0.0.1:8765 --gate https://gate.example.org:8767 --ca C:/ProgramData/Atlas/router-a/gate-ca.pem --mtls`.
   Локальный ATLAS_TOKEN берётся из окружения или Router `.env`.

Router certificate живёт 30 дней, автоматически продлевается при остатке <7 дней
на том же ключе; предыдущий serial допускается час. Смена ключа — новый admin
enrollment. CA живёт 10 лет. CLI server certificate живёт 30 дней: продление
выполняет оператор/внешняя PKI; контролировать сроки и синхронизацию часов.
Revoke узла блокирует proof/назначения. Device revoke блокирует клиента сразу,
а закрытие offline SDK сессии повторяется после восстановления связи.

## Windows

`service/Install-Gate.ps1`: ProjectRoot/PythonPath/WinSWPath/WinSWSha256/ServiceDirectory,
Mode=Prepare по умолчанию; проверяет hash/существующую службу, готовит wrapper/XML.
Mode=Install регистрирует без запуска. Секреты в XML не записываются.
`service/Protect-Data.ps1` ограничивает ACL только явно указанного каталога данных.
LocalService — default. Python, исходники, `.env` и data должны быть доступны этому
пользователю. Нужен машинный Python/runtime или выбранный service account;
Python в личном AppData не считается автоматически доступным LocalService.
Router SDK запускается от пользователя с подтверждённым подписочным входом.

Prepare → ACL/порты/TLS → тестовый запуск → приёмка → [миграция](MIGRATION.md).
Один Store — один владелец; несколько workers/активных Gate на одну БД запрещены.
