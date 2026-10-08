# Изображения в результатах инструментов — Gate #30

Зависимости: Router #53, Harness #192. Реализация протокола в PR не означает выпуск или live SDK приёмку.

Клиент запрашивает `SessionCreate.toolResultImages: true` (strict bool, default false). Gate передаёт флаг Router без изменения. Router подтверждает его в `SessionCreated.toolResultImages`; старый промежуточный Gate, который отбросил поле, получает false. Глобальная `attachments.toolResultImages` в подписанном профиле — возможность узла, а не подтверждение конкретной сессии.

`GET /harness/agent/sessions/{sid}/attachments` сохраняет прежнюю оболочку `{"attachments": ...}`, но получает negotiated capabilities из Router `GET /v1/sessions/{id}/attachments` (`{"capabilities": ...}`). Старый Router без такого endpoint сохраняет остальные известные возможности, а `toolResultImages` возвращается false.

`POST .../tool_result` допускает необязательный `attachments` с существующими полями `kind`, `filename`, `mediaType`, `data`, `sizeBytes`; только image, MIME png/jpeg/gif/webp, base64 bytes. Перед передачей Gate проверяет владельца/организацию/маршрут/закреплённый узел, bound flag и прежние объявленные Router лимиты. Отсутствующий или false flag возвращает `unsupported_media`; некорректные изображения — `attachment_invalid`; превышение лимитов — `attachment_limit`. Gate не доверяет `sizeBytes` вместо длины декодированных байтов.

Для запроса без изображений negotiation не требуется: прежние текст, isError, idempotency и raw forwarding сохранены. Policy/permission режимы не расширяются.

Проверка: реальные временные Gate и Router HTTP servers, stub сценарий backend receiver; no provider inference. Signed model discovery, bound acknowledgement, exact typed image values, image-only/base64/MIME/count refusal before forwarding, старый middle schema с отброшенным create flag, unchanged text path. Это transport доказательство, не проверка Claude SDK.

```sh
ATLAS_TEST_ROUTER_ROOT=/path/to/router uv run --with /path/to/router pytest tests/test_tool_result_images.py tests/test_attachment_contract.py -q --tb=short
```
