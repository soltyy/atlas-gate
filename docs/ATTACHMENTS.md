# Вложения: контракт Gate #10

Gate не извлекает PDF и не содержит Codex/Claude SDK. Обработка принадлежит
Router. Контракт и доказательства ограничения RPC описаны в
[Router ATTACHMENTS](https://github.com/soltyy/atlas-router/blob/codex/router-node-contract/docs/ATTACHMENTS.md).
Хранилище оригиналов Harness уже существует и не дублируется в Gate.

## Возможности для клиентов

Публичный `/harness/discovery.models[].attachments` — nullable безопасная
проекция возможностей: schemaVersion=1, text/image/pdfNative/documentPages/
agentDocumentTools и maxFiles/maxRawBytes/maxTotalBytes/maxTextChars/
maxPdfPages/maxPagePixels. Нет адресов узлов, nodeId, токенов и env references.
Объединение маршрута консервативно: пересечение флагов и минимумы пределов
по его узлам, поддерживающим model (`/v1/models[].value`). Для неизвестного
или недоступного Router значение null, а не предположение о поддержке PDF.

Авторизованный `/harness/settings.attachment_routes[route][model]` даёт эти
настройки для организации устройства. Подписанный профиль остаётся прежним;
добавка не выдаётся за содержимое JWS. После создания сессии клиент получает
возможности именно закреплённого Router через
`GET /harness/agent/sessions/{sid}/attachments` → `{attachments:...}`.
Это описание реализации backend, не подтверждение права доступа подписки
к любой модели и не способ вычислять модельное окно контекста.

## Транспорт и владение

Preflight на Gate до reservation/operation проверяет strict base64, реальный
размер bytes, kind и поддержку bound Router. Пределы: 8 файлов, каждый 15 МиБ,
суммарно 20 МиБ, текст 1000000 символов, имя 512 символов; меньшее ограничение
Router имеет приоритет. sizeBytes не влияет на разрешение. Ошибка 422 имеет
code/message/requestId. Reverse proxy/body timeout нужно настроить отдельно:
эти пределы не заменяют лимиты внешнего HTTP сервера.

`GET /harness/agent/sessions/{sid}/documents` → каталог.
`GET .../documents/{documentId}?operation=text|image|search&page=1&offset=0&query=...`
→ текст/изображение/поиск на Router. `DELETE .../documents/{documentId}` →
удаление его транспортного кэша. Ответы Router сохраняются, параметры query
передаются. Все ручки проходят device auth, ownership и policy.

Документный запрос следует постоянному binding sid → node/local session.
Gate не выбирает свободный Router заново при чтении страниц. После отказа
узла нет автоматической передачи PDF другому подписочному аккаунту.
Connector allowlist содержит только scoped document endpoints; произвольный
URL, путь файловой системы и чужую сессию передать нельзя.

Поддержаны Router напрямую, встроенный Gate, отдельный Gate на той же машине
и Gate с удалённым Router: direct HTTP/mTLS или обратный connector.
SSE и async polling используют одну документную проводку; connector
транспортирует поток через существующий async/event protocol.

## Проверка и владельцы

Gate: projection/preflight, binding и проксирование (#10).
Router: SDK input, worker, каталог, agent tool, resume и retention (#28).
Harness (#129): отправка mixed PDF даже при непустом extracted text,
has_text/UI, выдача ошибок, восстановление вложений и вызов удаления кэша.

`tests/test_document_transport.py`: реальные HTTP Gate/Router и PDF worker,
сценарный SDK, матрица direct/connector × SSE/async.
`tests/test_live_documents.py`: opt-in ATLAS_LIVE_CODEX=1, та же матрица и
настоящий gpt-6.1-sol; модель должна назвать TEXT 42017 и raster SCAN 7391,
которых нет в запросе. Connector в этих тестах использует настоящий Channel/
execute/poll, без отдельного TLS сетевого канала. TLS и enrollment проверяются
отдельным набором безопасности; PDF inference не объявляется проверкой TLS.
SDK-free Gate suite запускается отдельным venv без Router и SDK зависимостей.

Candidate не развёрнут в production. Установленный Harness не получает этот
фикс автоматически: нужны Router/Gate candidate и атомарные клиентские задачи.
