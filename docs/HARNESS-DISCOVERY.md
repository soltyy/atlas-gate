# Каталог и настройки харнеса

Кандидат Gate 0.1.0, [#9](https://github.com/soltyy/atlas-gate/issues/9).
Рабочая служба этим изменением не обновляется. Контракт описан в `/openapi.json`:
схемы `Discovery`, `HarnessSettings`, `ClientProfile`.

## Открытая ручка

`GET /harness/discovery` — JSON, без токена. Возвращает `schema_version=1`,
service/version, относительные пути enrollment/settings/profile/LLM/agent/context,
способ авторизации устройства, алгоритм подписи профиля и общие ограничения:
agent_sessions_per_device, anthropic_version, agent_context_source=session-sdk.

`models[]` — объединение всех настроенных моделей неархивных организаций:
model, display_name, kind, protocol, context_window, max_output, enabled,
limits_source=configured и стабильный catalog_id. Включены gateway и подписные
router-agent модели, в том числе настроенные, но выключенные. Одинаковые
варианты объединяются; разные окна, протоколы или виды маршрутов сохраняются
отдельно. `enabled=true` означает включение хотя бы в одной организации,
а не право конкретного устройства или готовность аккаунта. Нет org/route/node
IDs, upstream адресов, ключей, промптов, MCP и политик организации.

Пустое/неизвестное настроенное окно либо max_output отдаётся как null.
Каталог не делает prompt и не расходует подписку. Он не включает произвольные
неодобренные модели из SDK-провайдера вне конфигурации Gate. Ошибки загрузки
конфигурации администратор видит через существующий reload/admin API.

## Полные применимые настройки

`GET /harness/settings` с `Authorization: Bearer <device_token>` возвращает:

- schema_version, service_version, endpoints и общие limits;
- profile — полный JSON подписанного профиля своей организации;
- profile_jws — тот же JWS, что отдаёт прежний `/harness/profile`;
- profile_signing_key — публичный JWK для проверки, без приватного ключа.

В profile находятся profile_version, issued_at, ttl_seconds, версии приложения,
канал обновлений, org, routes, models, pricing, agents с prompts/settings,
mcp, policy, builtin_tools, overrides, feature_flags, quota и audit.
Устройство получает свою организацию независимо от query-параметров.
Неверный/истёкший/отозванный токен — 401, архивная организация — 403.
Неподписанный профиль — 503. Отсутствующие gateway ключи и каталог Router
обрабатываются тем же источником, что в прежнем профиле; discovery шире,
чем доступный устройству набор моделей.

JSON profile читается из persisted JWS, а не параллельного mutable словаря:
reload не разрывает JSON, подпись и версию. Старый JWS endpoint совместим.
Параметры транспорта endpoints/limits вне profile защищаются HTTPS; JWS
подписывает именно profile. Текущий расход квоты получается отдельным
GET quota, текущий размер подписного контекста — GET agent_context с session_id.

## Как харнес применяет данные

1. По настроенному адресу Gate прочитать discovery и поддерживаемую schema_version.
   Все endpoints разрешать относительно этого адреса, сохраняя тот же origin.
2. Пройти enrollment, закрепить полученный profile_signing_key и device_token.
3. Прочитать settings. Проверить JWS закреплённым enrollment ключом, сравнить
   декодированный payload с profile. Ключ из settings не заменяет закреплённый
   ключ автоматически. Проверить ttl/issued_at и минимальную версию приложения.
4. Атомарно применить profile своей организации: модели/маршруты, выбранного
   агента, MCP и инструментальные политики, квоты и ограничения overrides.
   Для выбора использовать profile.models и profile.routes, а не публичный union.
5. При старте и обновлении перечитывать settings с If-None-Match. 304 сохраняет
   прежний снимок. 401 требует refresh/enrollment, 403 прекращает использование
   корпоративного профиля. Неизвестная schema_version не применяется молча.
6. Создать agent session с выбранной моделью и агентным system_prompt; все
   продолжения идут через выданный публичный SID. Индикатор контекста подписки
   читать из session-sdk: configured context_window не заменяет runtime лимит.

## Версия, кэш и источники

Обе ручки возвращают ETag по каноническому JSON. Списки If-None-Match, слабое
W/ сравнение и * поддерживаются; ответ 304 без тела. Discovery: public, no-cache;
settings: private, no-cache и Vary: Authorization, X-API-Key. JSON модель/профиль
обновляется после штатного admin reload; неизменная конфигурация сохраняет
ETag после reload и restart. ETag settings отличается от ETag legacy JWS,
поскольку различаются представления. Изменения контракта требуют schema_version;
profile_version описывает содержимое организации.

HTTP семантика основана на [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html#section-13.1.2),
разделение общего и private кэша — на
[RFC 9111](https://www.rfc-editor.org/rfc/rfc9111.html#section-3.5).
Путь выбран `/harness/discovery`: собственная спецификация не выдаётся за
зарегистрированный стандарт `/.well-known/`; его регистрация описана в
[RFC 8615](https://www.rfc-editor.org/rfc/rfc8615.html#section-3.1).

HTTP проверки проходят публичный discovery → enrollment → settings/JWS →
reload/cache, отказы доступа, разные организации и Gate restart. Сетевой
тест харнеса читает settings, выбирает разрешённую модель и default агент,
создаёт session через настоящий Gate/Router HTTP, выполняет prompt и читает
context. Это проводка тестового харнеса; автоматическое подключение нового
endpoint в установленной EDT этим Gate PR не заявляется.
