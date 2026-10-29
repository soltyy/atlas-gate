# Pricing v1: Gate #76 / Harness #404

Серверный контракт не доказывает поставку клиентского импортера или восстановление маршрута.
Router/SDK не участвуют в прямом DeepSeek API. Клиент владеет budget/reserve/settle ledger.

## Подписанный профиль

Обычный `pricing` остаётся прежним. Для динамических exact route/model он **не содержит строки**:
legacy клиент сохраняет отказ admission, а не принимает ложную постоянную/нулевую цену.
Для opt-in моделей профиль дополнительно содержит `pricing_contract` и `pricing_v1`:

```json
{"pricing_contract":{"version":1,"legacy_dynamic_prices":"omitted","selection_required":true,"cost_kind":"estimate"},
 "pricing_v1":[{"version":1,"route_id":"deepseek-api","model":"deepseek-flash",
 "pricing_version":"deepseek-reviewed-20261029","revision":"<sha256 of complete schedule>","currency":"USD","unit":"per_million_tokens",
 "cost_kind":"estimate","time_basis":"gate_admission_utc","provider_confirmed":false,
 "source_url":"https://api-docs.deepseek.com/quick_start/pricing/",
 "calendar_source_url":"https://www.gov.cn/zhengce/content/202511/content_7047090.htm",
 "checked_at":"2026-10-29T00:00:00Z","valid_from":"2026-10-29T00:00:00Z","valid_until":"2026-10-30T00:00:00Z",
 "periods":[{"starts_at":"2026-10-29T00:00:00Z","ends_at":"2026-10-30T00:00:00Z","tariff_id":"illustration-only",
 "rates":{"input_uncached":0.15,"input_cached":0.003,"output":0.6}}],
 "reserve_rates":{"input_uncached":0.15,"input_cached":0.003,"output":0.6},"excluded_charges":["search","fetch","other"]}]}
```

Это пример формы, **не актуальный календарь**: реальный четверг имеет два peak окна.
`periods` — отсортированное полное покрытие `[valid_from,valid_until)`, интервалы полуоткрытые,
явный UTC. Нулевые/отрицательные интервалы, gaps/overlaps, NaN/Infinity/отрицательные ставки,
неизвестная валюта, дубли и смешение static/dynamic exact model отвергаются конфигурацией.
v1 поддерживает USD и OpenAI-compatible token usage. Модель должна быть явно настроена
на gateway route protocol=openai. Anthropic cache-write billing требует отдельного расширения,
не сводится к тарифу cache-miss. Отсутствующий/неполный/невалидный input/output/cache usage
не превращается в нулевой расход.

Клиент проверяет JWS, импортирует **все интервалы** с route/model/version/границами/источником,
не сводит их к одной строке. Никаких локальных догадок о китайских праздниках.
`reserve_rates` вычислены как максимум каждой ставки во всём горизонте. Это консервативная
основа резерва при известной оценке количества токенов, а не цена settlement или гарантия
максимального расхода произвольного tool loop. Инструменты/новые inference требуют своих резервов.

## Выбор и вызов

До создания резерва клиент выбирает действующий интервал. На **каждый** API model call:

```text
Idempotency-Key: <immutable model-call ID>
X-Atlas-Pricing-Contract: 1
X-Atlas-Pricing-Selection: {"pricing_version":"...","revision":"...","tariff_id":"...","starts_at":"...+00:00","ends_at":"...+00:00"}
```

`revision` — вычисленный сервером SHA256 всего schedule, включая ставки/источники/горизонт;
клиент берёт его из подписанного профиля, не вычисляет самостоятельно. Повторное использование
человеческого pricing_version не позволит принять старый резерв после изменения ставок.
Границы selection — нормализованные ISO UTC с `+00:00`; профиль может сериализовать UTC как `Z`.
При изменении окна/версии до отправки Gate возвращает `409 pricing_requote_required`,
при истечении покрытия — `409 pricing_expired`, без negotiation — `422 pricing_contract_required`.
Отсутствие стабильного ID — `422 pricing_request_key_required`. Все эти отказы имеют
`deliveryState:not_sent` **для этой попытки**, это не доказательство исхода предыдущего запроса.
Клиент освобождает резерв/пересчитывает его только после сверки собственного состояния.
Ценовые отказы не требуют сжатия/сброса SDK или повторения инструментов.

Gate сохраняет hash body+selection и снимок цены до provider IO, namespaced по org/device/route/model/key.
Повтор с изменённым телом/снимком даёт `idempotency_conflict`. Это **не API result cache**,
не защита от повторной оплаты inference: одинаковая повторная доставка может снова вызвать
провайдера. Gate не делает автоматических provider retries. Клиент не должен повторять
неизвестный outcome как новый запрос. Старые price-request snapshots не удалять как deploy tmp.

Снимок фиксируется на Gate admission; изменение времени/конфига во время потока не переоценивает
вызов. Ответ, включая отказ upstream, содержит `X-Atlas-Pricing-Decision` — выбранный snapshot.
Прокси удаляет все `X-Atlas-Pricing-*` до upstream; payload/opaque reasoning не переписываются.
Если транспорт не дал ответа, клиент не придумывает usage/стоимость. Сохранённые settings не равны
подтверждённой генерации. `GET /harness/llm/pricing/decision?requestId=...&route_id=...&model=...`
читает свой снимок без inference: `{decision,providerOutcome:"not_recorded"}`. Чужой device/org
или отсутствующий ключ получают404. Это не квитанция результата/usage; после потери HTTP
ответа нельзя считать один snapshot основанием повторного inference. Endpoint объявлен
в `pricing_contract.decision_endpoint`.

## Учёт и точность

Стоимость по cache miss/hit/output — **расчётная оценка** в USD по времени admission Gate,
а не подтверждённый баланс/инвойс DeepSeek. Момент vendor-тарификации длинного запроса,
пересекающего окно, публичным прайсом однозначно не установлен. Нельзя обещать точное совпадение.
Gate атомарно сохраняет usage и связанный snapshot в `gateway_usage_pricing`; этот snapshot
не хранит prompts/секреты. При неизвестном usage записи/списания не выдумываются.
Клиент фиксирует в reservation/model-call ту же revision и в settlement использует snapshot,
не текущую версию профиля; повтор проекции receipt не создаёт второй ledger settlement.

Images API не покрыт token-only тарифом. Для Responses image generation cost остаётся unknown.
Search/Fetch и `other` в excluded_charges не считаются бесплатно: клиент должен запретить
непокрытые платные возможности либо вести отдельный подтверждённый учёт. Gate v1 не имеет
universal adapter для per-tool billing; такую цепочку нельзя объявлять полностью учтённой.

## Календарь и поставка

`tools/deepseek_pricing.py` генерирует публичный конфиг по reviewed UTC-дням, пяти интервалам
в день (peak01–04,06–10 по будням) и явным offpeak датам. Оператор проверяет календарный горизонт,
передаёт `--calendar-reviewed`, источник, checked-at и revision. Генератор не скачивает прайс
и не создаёт расписание обновления. За пределами горизонта запрос fail-closed; не продлевать
календарь молча и не подменять expiry TTL подписи. Клиент показывает expiry и действие refresh.

Источники, проверенные29.10.2026:
- https://api-docs.deepseek.com/quick_start/pricing/
- https://www.beijing.gov.cn/cs/gncs/zcwj/202603/t20260327_4568275.html

Поставка: сначала server release+profile, затем согласованный Harness import/selector/headers,
приёмка отдельными инертными вызовами через настоящий Harness reserve→call→settle для обеих моделей.
До этой приёмки Gate #76 остаётся открытым. Не повторять пользовательские задачи #404.
