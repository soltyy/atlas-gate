# Reasoning v1: контракт Gate / Router / клиентов

Рекон 28.10.2026; Gate #73, Router #93, Harness #382, EDT #217.
Серверная реализация не означает готовность клиентского селектора или поставку.

## Что установлено до изменения

На VDSKA Gate 0.4.12, оба Router 0.8.14. Проверка конфигурации не обнаружила
явного `model_reasoning_effort` или override effort в окружении служб.
Codex model/list: gpt-6.1-sol default low; gpt-6-astra и gpt-6-sol medium;
gpt-6-luna medium. Наборы уровней зависят от exact model/account.
Последний просмотренный turn_context effort=null означает наследование,
а не доказанный medium или отсутствие рассуждений.

Claude Agent SDK 0.2.159: публичные ClaudeAgentOptions.effort/thinking есть,
публичного runtime set_effort нет. Собственный initialize без query подтвердил
low/medium/high/xhigh/max и adaptive для opus, sonnet и fable этого аккаунта.
У haiku метаданных не было: unknown. Возможность disabled/manual budget SDK
не подтвердил, поэтому сервер не объявляет их доступными для подписки.
Router до изменения выбрасывал эти SDK metadata при проекции моделей.

Harness хранит model_settings.thinking, но managed agent_model не передавал
его в создание SDK transport. Прямой API использует PydanticAI 2.46.0;
у него есть собственные provider mappings и конфликтующие native overrides.
EDT имеет отдельную OpenAI-compatible reasoning_effort/off логику.
Эти клиенты данным серверным изменением не переписаны.

## Ответственность

| Уровень | Владеет | Не делает |
|---|---|---|
| Gate | exact route/model policy, signed profile, выбор совместимого узла, проверка ACK; API mapping | Не импортирует SDK и не задаёт SDK context window |
| Router | SDK model capabilities, локальные defaults/allowlist, native параметры и durable session snapshot | Не редактирует организационный профиль |
| Клиент | версия агента, выбор модели/настройки, неизменяемый outbox, безопасное переоткрытие transport | Не переписывает принятый body/key/fingerprint после reload |
| Провайдер | фактическое исполнение, native defaults, тариф/ограничения | Отправленный параметр не доказывает внутреннюю вычислительную затрату |

Defaults: explicit agent/request > Gate default > Router model/global default > provider.
Hard allowlists/locked Gate policy и Router allowlist не ослабляются этим порядком.
В v1 это политика основного SDK transport / API request. Отдельные native
SDK subagent overrides не поддержаны и явно отклоняются. Их внутреннюю effort
настройку и наследование другой моделью сервер не подтверждает. Клиентские
дочерние агенты с самостоятельным transport используют обычный контракт.

## Настройка SDK

POST /v1/sessions или /harness/agent/sessions:

```json
{"model":"gpt-6.1-sol","reasoning":{"version":1,"mode":"inherit","effort":"high"}}
```

- Поле отсутствует / пустой inherit: наследовать defaults; при отсутствии
  defaults оставить старое поведение SDK, без нового параметра.
- effort без mode: изменить effort, не включать thinking принудительно.
- provider_default: обойти серверные defaults, но не hard policy; это не disabled.
- enabled: включить reasoning только подтверждённым способом точного канала.
- disabled: требуется отдельная capability; low не равно выключено.
- budgetTokens: положительное целое, переводит inherit в enabled;
  поддерживается только при подтверждённом manual budget. Это не max output.
- Efforts — minimal/low/medium/high/xhigh/max/ultra, но каждый exact model/channel
  принимает только собственный объявленный набор. auto/off — не общие уровни.

GET /v1/node содержит reasoningSettings.version=1, scope=session,
configuredEcho=true, nativeSubagentOverrides=false. GET /v1/models сохраняет
model.reasoning с source/resolvedModel/efforts/defaultEffort/adaptive/disabled/budgetTokens.
Unknown не превращается в supported; отсутствие metadata не заполняется по названию модели.

Ответ создания и session/result/error events содержат configuredReasoning:
requested, configured, source, capabilityRevision, resolvedModel,
providerConfirmed=false; Router также сохраняет capabilities snapshot.
Codex nativeEffort фиксирует разрешённый model default и передаётся в thread
config и каждый native turn/start, включая steer continuation и summary.
Claude использует ClaudeAgentOptions и явное process effort окружение только
для настроенной сессии. Отсутствующая настройка не меняет legacy options.

GET /v1/sessions/{sid}/reasoning и зеркало Gate читают сохранённое решение,
включая закрытую сессию, без SDK connect/query/resume. Для legacy оно null;
это не подтверждение native default. capabilities в этом ответе — снимок
принятия, свежие факты находятся в каталоге узла.

Настройка SDK session-scoped. Поле reasoning в prompt отклоняется до запуска.
Для изменения после terminal клиент сверяет исход старого хода, создаёт новый
transport с resumeSessionId той же истории на том же node/account, проверяет
resumed и configuredReasoning. Новый request ID относится только к новой
операции; старые инструменты и prompts не переигрываются. После Stop сначала
нужна сверка terminal/ACK, затем rebind. При изменении Gate default следующий
новый prompt старого transport получает reasoning_rebind_required; replay
старой принятой операции возвращает прежнюю квитанцию до новой policy.
Router restore использует сохранённую настройку, а не сегодняшние defaults.
Изменение model alias resolution требует явного решения, не тихой подмены модели.

## Серверная конфигурация

Router env (JSON), опционально, без изменения defaults действующих служб:

```text
ATLAS_REASONING_DEFAULT={"effort":"medium"}
ATLAS_REASONING_MODEL_DEFAULTS={"gpt-6.1-sol":{"effort":"low"}}
ATLAS_REASONING_ALLOWED_EFFORTS=["low","medium","high"]
```

Gate exact ModelEntry в конфигурации организации:

```json
{"route_id":"codex-sub","model":"gpt-6.1-sol",
 "reasoningPolicy":{"default":{"effort":"medium"},"allowOverride":true,
                    "allowedEfforts":["low","medium","high"]}}
```

allowOverride=false требует совпадения explicit request с default; provider_default
не обходит запрет. Allowlist требует явного проверяемого effort; неизвестный
inherited/provider default не считается доказанно разрешённым. Сужение policy
не переписывает активный ход и не препятствует чтению/доставке уже выполненного
tool result; оно проверяется на следующем новом prompt. Резкое административное
прекращение работы — отдельный interrupt/revoke, не скрытая мутация настройки.

Gate публикует policy и aggregate SDK capabilities в подписанном профиле.
Это union совместимых известных узлов для нового выбора; он не гарантирует
свободную мощность и не разрешает миграцию существующей беседы. Для закреплённой
сессии действуют её node и snapshot. Разные defaults узлов отмечаются
defaultsVaryByNode; единый fleet default задаётся явно на Gate.

## Стандартные API и DeepSeek

Существующий Gate API proxy поддерживает Chat Completions, Responses и Messages.
Reasoning v1 включается у точного ModelEntry через reasoningDialect
(openai/anthropic/deepseek), reasoningCapabilities и/или reasoningPolicy.
Для API capabilities — **декларация оператора**, а не выдуманное обнаружение
по имени. Сверять с exact provider/model/API version; DeepSeek /models может
служить источником поддержанных levels/default. Автоматического обновления
этой декларации в v1 нет. Неизвестный endpoint без opt-in сохраняет старое
provider-native тело; это passthrough без подтверждённой reasoning поддержки.
Явный atlas_reasoning без подтверждения отклоняется.

Пример API ModelEntry (уровни указаны как пример и требуют сверки модели):

```json
{"route_id":"api","model":"exact-model","reasoningDialect":"openai",
 "reasoningCapabilities":{"status":"supported","source":"operator-verified",
   "efforts":["low","medium","high"],"defaultEffort":"medium","disabled":true},
 "reasoningPolicy":{"default":{"effort":"medium"}}}
```

Клиент добавляет atlas_reasoning с той же схемой; Gate удаляет расширение перед
upstream. Native settings также проверяются при opt-in; противоречащие canonical
и native параметры отклоняются. Messages/thinking signatures/tool results,
reasoning summary и output_config.format остаются непрозрачными и сохраняются.

| Канал | effort | enabled / disabled |
|---|---|---|
| OpenAI/Azure-compatible Chat | reasoning_effort | explicit effort / none, только подтверждённые моделью |
| OpenAI/Azure Responses | reasoning.effort | explicit effort / none |
| Anthropic Messages | output_config.effort | adaptive / disabled; manual enabled+budget_tokens отдельно |
| DeepSeek Chat | reasoning_effort | thinking.type enabled / disabled |
| DeepSeek Responses | reasoning.effort | effort / none |
| DeepSeek Messages | output_config.effort | thinking.type enabled / disabled |
| Bedrock Converse, direct API клиента | provider adapter клиента | Gate не имеет такого transport; unsupported в этом server scope |

Azure-compatible означает существующий совместимый proxy route, не новый
Azure auth/API-version transport. Для Bedrock требуются PydanticAI/SDK клиента,
SigV4 и model-specific additional fields; это Harness #382 / EDT #217.
Не отправлять Anthropic thinking JSON в произвольный OpenAI endpoint.
Anthropic disabled+effort допускается лишь при explicit disabledEfforts;
manual budget >=1024 и <max_tokens, если max_tokens задан. Подписочный Claude
не получает эти возможности автоматически из API спецификации.
DeepSeek документирует minimal→low, medium/xhigh→high, ultra→max;
requested сохраняется, configured содержит нормализованный уровень и пометку.
Это специфическое преобразование DeepSeek, не универсальная шкала качества.

Idempotency-Key или X-Harness-Model-Call фиксирует API body hash + решение
в SQLite до отправки. Gate не повторяет provider IO и не обещает API result
deduplication: назначение записи — не изменить параметры повторной доставки.
X-Atlas-Reasoning-Scope дополнительно фиксирует настройки для одного API
tool loop. Область ключа: org/device/session/agent/route/model/scope;
каждый шаг имеет отдельный model-call ID. Без scope это отдельные запросы;
нельзя обещать сохранение cache prefix между шагами старого клиента.
После reload старый request/scope использует прежний snapshot, новый scope —
новую policy. Explicit изменение внутри scope даёт 409, изменённый body с
прежним request key — idempotency_conflict. Сохраняются только hashes/settings,
не prompts; пока записи имеют срок жизни БД, их нельзя удалять как временные
поставочные каталоги. Контрольные заголовки не уходят провайдеру.
Ответ содержит X-Atlas-Reasoning-Decision и X-Atlas-Configured-Reasoning;
providerConfirmed всегда false. Профиль и HTTP echo не доказывают эффективность кэша.

## Кэш, стоимость и изменения версий

Claude top-level effort/thinking влияет на cache prefix. Поэтому v1 не
переключает уровни на каждом tool step и не использует per-message beta без
отдельной negotiation. Не переписывать opaque thinking blocks и историю ради
экономии. Сравнивать cache_read/cache_creation/input/output usage на одинаковом
моделе и префиксе; низкий effort не гарантирует фиксированную экономию подписки.
Effort не является context window, output budget или service tier.
Provider-default Claude без объявленного default остаётся provider-controlled:
его внутреннее значение нельзя зафиксировать или подтвердить сервером.

| Обновления | Поведение |
|---|---|
| Router новый, Gate старый | Без opt-in defaults старые запросы прежние; клиент не отправляет v1 через старый Gate, который может отбросить неизвестное поле |
| Gate новый, Router старый | Legacy разрешён, explicit/default v1 отсекается до SDK; отсутствие capability не считается успехом |
| Оба новые, часть fleet старая | Новое создание выбирает подтверждённый узел; существующая привязка сохраняется |
| Сначала Router default, затем Gate default | Новые сессии используют Gate; принятые сессии/операции не меняются, новый prompt может потребовать safe rebind |
| Новая policy во время инструмента/ACK/retry | Прежние payload/key/fingerprint и результат сохраняются; никаких повторов бизнес-действий |
| Rollback | Старый код может игнорировать новую настройку; остановить новые v1 admissions и выполнить safe rebind, не объявлять его прозрачным |

Рекомендуемый порядок: Router capability/apply/echo → Gate policy/profile/validation
→ клиенты → opt-in defaults. Перед включением проверить inert SDK turn в каждом
реальном канале, durable restart/replay, tool-result/Stop, историю и isolation.
Поставка Router только через установленный deploy guard, при свободных узлах,
с transport/lifecycle и live native SDK web qualification. Этот документ не квитанция поставки.
Legacy embedded Gate внутри Router не публикует v1 и отклоняет explicit settings;
управляемый серверный маршрут v1 использует самостоятельный Atlas Gate.

## Первичные источники

- [Codex app-server model/list и turn/start](https://learn.chatgpt.com/docs/app-server)
- [Codex configuration](https://learn.chatgpt.com/docs/config-file/config-reference)
- [OpenAI API reasoning](https://developers.openai.com/api/docs/guides/reasoning)
- [Claude API effort](https://platform.claude.com/docs/en/build-with-claude/effort)
- [Claude adaptive thinking](https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking)
- [Claude Code model configuration](https://code.claude.com/docs/en/model-config)
- [DeepSeek thinking/protocol mappings](https://api-docs.deepseek.com/guides/thinking_mode/)
- [DeepSeek model capabilities](https://api-docs.deepseek.com/api/list-models/)
- [Bedrock Converse](https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html)
