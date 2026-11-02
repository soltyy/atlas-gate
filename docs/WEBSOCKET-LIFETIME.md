# WebSocket lifetime и продолжение (Router #99 / Gate #83)

`websocket_connection_limit_reached` означает срок жизни одного upstream WebSocket,
а не квоту подписки. Официальный Responses API указывает 60 минут:
https://developers.openai.com/api/docs/guides/websocket-mode#reconnect-and-recover .
Новый сокет теряет connection-local cache. Для API требуется stored response ID
либо полный сохранённый/compact контекст. Нельзя включать store=true ради обхода
ограничения или считать SDK threadId значением API previous_response_id.

Codex CLI rust-v0.160.0 уже отображает этот machine code в ApiError::Retryable
(`codex-api/src/endpoint/responses_websocket.rs`). SDK владеет повтором модельного
запроса, контекстом и новым сокетом; Router не создаёт повторный turn/start.
Никакой новый общий часовой таймаут агентного хода не вводится. Профилактическое
закрытие сокета Router не делает: сокет принадлежит SDK, готового публичного RPC
для его безопасной ротации нет.

Router сохраняет `provider_retry` с willRetry=true как нетерминальное событие
того же публичного turn. Gate добавляет `transportRecovery` v1:

| Состояние | action | Значение |
|---|---|---|
| reconnecting | read_same_turn | SDK восстанавливается; читать тот же журнал |
| reconcile_required | explicit_continuation_after_reconcile | terminal, сверить результаты, отдельное явное продолжение через существующий resume |
| stopped | none | Stop имеет приоритет, автоматическое продолжение запрещено |

Всегда replayAllowed=false. Это не разрешение отправлять исходный prompt или
повторно исполнять/публиковать tool_result. При неоднозначном результате инструмента
сверить существующий callId/ACK. При утрате истории сообщить об этом явно.
Gate не открывает upstream WS, не меняет аккаунт и не ставит account cooldown.
Обычные 400/429/503 не классифицируются по свободному тексту как lifetime expiry.

Диагностический snapshot expiry сбрасывается при новом response.created после
native reconnect. Терминальный interrupted не наследует старую ошибку транспорта.
Старые журналы неизменны; Gate накладывает read-only recovery-политику при чтении.
GET /harness/agent/sessions/{sid}/turns/{turn}/receipt работает и для closed binding
с прежними owner/org/node/boot/turn ограничениями и usage once. Подписанный профиль
Gate объявляет transportRecovery protocol=sdk-managed-v1; старый клиент может
игнорировать дополнительное поле, ему не обещается автоматическое восстановление.

Изолированные тесты не доказывают реальную серверную ротацию через60мин.
Нативную совместимость проверять установленным SDK с синтетическим lifetime
отказом на собственном диагностическом upstream; живой SDKweb — отдельная приёмка.
