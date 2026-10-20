# Генерация изображений: Gate и handoff клиенту

Gate #58, Router soltyy/atlas-router#68. Рекон инцидента: #57.
Серверный контракт: `atlas-router/docs/IMAGE-GENERATION.md`.

## Подписочные агенты

sdkTools.imageGeneration согласуется по политике организации и capability
закреплённого узла. При отсутствии sdk_tools стандартная политика разрешает
генерацию; старые явно записанные политики без imageGeneration её не разрешают:

```json
{"sdk_tools":{"version":1,"webSearch":"live","files":true,"imageGeneration":true}}
```

Codex использует лимиты подписки. Claude получает atlas_image только на Router
с явно настроенным Images API, с отдельной API-оплатой. Платного fallback нет.

`GET /harness/agent/sessions/{id}/media` возвращает каталог, а
`GET /harness/agent/sessions/{id}/media/{artifactId}` возвращает
`{artifact:{id,sha256,mediaType,sizeBytes,width,height,callId,turnId,source},data:<base64>}`.
Обязательно владение сессией; ID/hash сам по себе не даёт доступ. Direct и
connector поддержаны. GET можно повторить после reconnect/restart узла.

## Прямой API

OpenAI gateway route должна содержать image-модель либо Responses-модель с
подтверждённым оператором `image_generation:true`. Политика
sdk_tools.imageGeneration тоже должна разрешать генерацию. Ключ — обычный
key_env маршрута. Поддержаны:

- `POST /harness/llm/v1/images/generations` — JSON Images API.
- `POST /harness/llm/v1/responses` — в том числе image_generation, input_image
  и последовательное редактирование, если это поддержано upstream.

Модель/маршрут, класс данных, архив организации и квота проверяются штатно.
Тело идёт без chat-specific stream_options. Поддержка произвольного
OpenAI-compatible upstream не угадывается. API-ошибки передаются клиенту.
Usage читается из input_tokens/output_tokens, включая response.completed SSE.
Стоимость image-запроса — `null`: цены Gate описывают текстовые токены.
Это не нулевая цена и не жёсткая денежная квота на изображения.

## Handoff Harness

1. Сохранить `media_artifact` до продвижения cursor, дедуплицировать по artifact.id.
   sdk_tool_completed не считать запросом tool_result.
2. Получить байты через авторизованный GET, проверить MIME/размер/SHA256,
   сохранить вложение и показать в ленте. Повторный GET безопасен.
3. Для API-маршрута включить image_generation в Responses либо объявить инструмент
   с исполнением через разрешённый Gate API. HTTP endpoint сам не добавляет
   функцию в каталог API-агента.
4. Отказы показывать как отказы; «картинка создана» — только после сохранения
   подтверждённого результата. Отправка в другой чат — отдельное действие.

#57 не закрывается по серверному smoke. Нужна приёмка Harness: новый и
восстановленный чат, генерация, PNG-вложение, повторная загрузка и показ.
Клиентская задача: https://github.com/soltyy/atlas_harness/issues/324.
Клиентские исходники в этих серверных PR не меняются; рабочие службы не обновлены.
