# Рекон и решения, 05.10.2026

Проверены собственные исходники обоих проектов, HTTP проводка, SDK callbacks,
SQLite и Vue. Изменения сделаны в отдельных ветках. Production служба и клиентские
профили не изменены; исторические материалы использованы как свидетельства.

| Найденная проблема | Реализация | Тикет |
|---|---|---|
| Gate импортировал SDK/реестр Router | SDK-free пакет, независимый venv, HTTP контракт | Gate #1 |
| Общий аккаунт мог обходить лимит на другом узле | Атомарные резервы, hard limits Router, account_group, subagent budget | Gate #2, Router #25 |
| Потеря ACK могла повторить SDK ход | Command ID/fingerprint, journal reconciliation без failover беседы | Router #23 |
| Restart терял events/usage/SDK binding | WAL journal, SDK ID/boot проверка, watchers, terminal receipts | Router #23, Gate #8 |
| Endpoint не удостоверяет установку | CSR fingerprint approval, Ed25519 proof, mTLS, revoke | Gate #5 |
| NAT требовал входящего порта Router | Исходящий polling, durable bounded queue/lease и ACK replay | Gate #6 |
| Idle close на Router не освобождал Gate capacity | Сверка journal metadata | Gate #2, #8 |
| Offline revoke оставлял SDK сессию | Closing state и повторное DELETE | Gate #5, #8 |
| Не описаны backup/cutover | SQLite backup, сохранение signing key/keyring, migration manifest | Gate #7 |
| Interrupt до старта coroutine оставлял busy turn | Отмена до SDK вызова, terminal event, освобождение резервов | Router #26; связано с #23, #25 |

## Первичные источники

[AWS idempotent APIs](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/):
используем caller intent ID и fingerprint; после потери ACK читаем journal,
не повторяем непроверенный ход на другом узле.

[SQLite WAL](https://www.sqlite.org/wal.html), [PRAGMA](https://sqlite.org/pragma.html):
WAL/FULL/busy timeout, транзакционные резервы, backup API вместо копии одного db
при живом WAL. File lock запрещает два владельца одного Store.

[Envoy circuit breaking](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/circuit_breaking):
ограничиваем работу узла/аккаунта, очередь и payload; readiness/drain/cooldown
учитываются до новых запросов.

[SPIFFE concepts](https://spiffe.io/docs/latest/spiffe/concepts/): идентичность
установки отделена от IP/boot. URI spiffe://atlas.local/router/ID применяется
в собственной PKI с ручным CSR approval; SPIRE attestation не реализован.

[Uvicorn](https://www.uvicorn.org/settings/),
[NGINX SSL](https://nginx.org/en/docs/http/ngx_http_ssl_module.html): выделенный
TLS listener с CERT_REQUIRED. Proxy header не удостоверяет клиентский сертификат.
[PyJWT](https://pyjwt.readthedocs.io/en/latest/api.html): фиксированный EdDSA,
aud/iss/sub/exp/iat, одноразовый jti и hash запроса.
[Cryptography X.509](https://cryptography.io/en/latest/x509/reference/): проверка
CSR signature и принадлежности выданного сертификата локальному ключу.

[Codex app-server](https://learn.chatgpt.com/docs/app-server): account/rateLimits/read
и updated используются как телеметрия подписки. Каталог не подтверждает остаток
квоты; неизвестная телеметрия остаётся неизвестной. Для ChatGPT входа API key не нужен.

Принятые эксплуатационные ограничения: [HA-ADR.md](HA-ADR.md).
