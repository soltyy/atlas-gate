# Текущее окно по route/model

Gate#44, Router#61. context_window для router-agent означает **эффективную**
доступную ёмкость SDK. Полное окно отдельно context_full_window; неизвестное null.
У gateway API семантика model-full, источник конфигурация API-маршрута.
Экспериментальное максимальное окно не равно текущему окну.

## Источник

Gate читает только аутентифицированный серверный контракт Router:
1. /v1/node.models[].contextWindow {source:sdk,revision:integer,effectiveTokens,fullTokens}.
2. Для старого Router — GET /v1/sessions/{local_id}/context, maxTokens, либо result.context.
Проверяется node/boot, модель или её resolved alias. Наблюдения маршрутизируются
по org/route/model и eligible node, никогда не по одному глобальному имени.

Router Codex вычисляетdefault из собственного SDK models_cache.json после model/list:
context_window500000 × effective_context_window_percent95% =475000.
max_context_window872000 экспериментальное и не подставляется. client_version
должен совпадать с initialize.serverInfo.version работающего SDK, fetched_at≤300s;
operatoroverride model_context_window требует runtimeSDK, не угадывается.
Gate не читает SDKфайлы клиентов или сервера. Для Claude без полного каталога
окна используются подтверждённые sessionSDK observations, не APIконстанты.

## Обновление и порядок

Фоновый refresh каждые30s, metadata probe и context sampling по20s deadline;
при отвечающем источнике цикл обновления≤70s. Terminal result или явныйcontext
обновляет окно в том же обработчике. Синхронизация не создаёт SDKсессий/ходов,
не возобновляет историю и не тратит подписку на проверочные prompts.

revision упорядочивает catalog наблюдения внутри node/boot. SDKcatalog имеет
приоритет над session observations. Для runtime fallback newest binding/turn
имеет приоритет; request start order защищает от позднего ответа того же источника.
При нескольких Router каталог публикует минимум подтверждённых окон кандидатов;
неизвестный кандидат не позволяет обещать всем клиентам большое окно.

router_context_windows хранит измерения в SQLite. При restartGate сведения
восстанавливаются, но проверяются boot/свежесть источника. Информация старше300s,
другойboot, недоступныйRouter или невалидное окно не публикуются как актуальные:
context_window=null, context_status=stale/unavailable/unknown. Верхняя граница
публикации устаревания при отказавшем источнике —300s+одинцикл≤370s.
Старое статическоеorg.context_window не подменяет runtime. Полныйfallbacknull
не является запретом использовать модель или уменьшением фактического SDKокна.

## Доставка

Профиль переподписывается только при изменении значения/статуса/источника;
служебный timestamp каждогоpoll не входит в hash. Меняются profile_version и ETag.
/harness/settings, legacy/harness/profile, discovery и admin.model_contracts
читают одну согласованную проекцию. Агентные определения, orgconfig и bindings
не меняются. Существующий клиент получаетновуюподписаннуюверсию обычнымrefresh;
Gate не может заставить клиента, который не читаетsettings, обновить локальныйUI.

/harness/agent/sessions/{id}/context остаётся настоящим контекстом **этой** сессии:
старый SDKthread может иметь другое окно, чем новый defaultmodel. Gate не рисует
475000 поверх подтверждённого старогоthread258400. Семантики разделены контрактом.
context_source=router-catalog/session-sdk/unknown, limits_source=router-sdk/unknown;
для API limits_source=configured, context_source=configured.

Админка показывает SDKокно read-only из опубликованного контракта, включая
карточки существующих агентов. Изменение черновика не перекрывает SDKчисло.
