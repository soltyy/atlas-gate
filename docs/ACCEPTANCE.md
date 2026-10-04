# Проверка кандидата, 05.10.2026

Все проверки выполнены в отдельных dev checkout/временных runtime каталогах.
Рабочая AtlasRouter 0.6.0 и профили устройств не менялись.

| Проверка | Свежий результат |
|---|---|
| Router полная pytest suite | 231 passed, 28 opt-in skipped, 183.44 s |
| Gate suite, два настоящих HTTP Router на stub | 21 passed, 1 live opt-in skipped, 18.82 s |
| Gate отдельный venv без SDK | 13 passed, 9 integration/live skipped, 4.26 s |
| Настоящий Codex/ChatGPT, независимые Gate и Router | 1 passed, 62.34 s |
| Gate Vue UI | 42 passed, typecheck и build прошли |
| Пакеты | uv build Router 0.7.0 и Gate 0.1.0; wheel metadata/static/entry points и syntax проверены, Gate SDK-free |
| Windows service prepare | WinSW SHA-256/XML проверены во временном каталоге; install/start не выполнялись |

Сетевой путь: enrollment/profile/models → session selection → tools/results →
usage, owner isolation, shared account limits, drain, compact/resume, Gate restart,
SSE/control, потеря create/prompt/connector ACK, node identity/revoke, настоящий
TLS handshake без/с client certificate, очередь NAT, idle close/deferred revoke,
истёкшие события и receipt dedup. Unit tests покрывают journal crash/retention/pages,
strict resume, capacity/subagent enforcement и immediate interrupt.

Настоящий Codex: ChatGPT loggedIn=true, клиентский инструмент, повтор prompt без
повторного расхода, native compact с сохранением КЕДР-729, restart обеих служб с
тем же public SID/native SDK ID и новым boot, продолжение/usage с cost=null.
Это не проверка оплачиваемого API key.

## Оставшаяся приёмка перед production

- Claude SDK сейчас loggedIn=false. Stub/hooks тесты прошли, реальный Claude
  compact/resume/subagents/outage через новый Gate не подтверждён. Router #17.
- Конкретный EDT клиент не проходил свежую OSGi/workbench end-to-end приёмку этого
  разделения. HTTP совместимость и tools проверены сетевым harness. Gate #8.
- Реальные Windows service account ACL, внешний DNS/TLS/proxy, snapshot/restore
  текущих данных и cutover/rollback production ещё не выполнялись. Gate #7.
- UI toolchain замечание Router #19 и внешняя TLS инфраструктура #4 остаются
  отдельными открытыми задачами; данное разделение не является их закрытием.

Тикеты остаются открыты до выпуска/эксплуатационной приёмки. Подготовленные
source/wheel артефакты — кандидаты; публикация production релиза не заявляется.
