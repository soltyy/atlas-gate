# Создание и продолжение агентной сессии

Gate #65; выпуск 0.4.9. Причина: EDT #209 — клиент требовал поле, отсутствующее
в фактическом ответе создания, и блокировал подтверждённое продолжение истории.

## HTTP и идентификаторы

| Ответ | Схема OpenAPI Gate | Значение |
|---|---|---|
| POST `/harness/agent/sessions`, 200 | `SessionCreated` | `id` — публичная HTTP-сессия Gate; `resumed` и `resumeError` — результат открытия истории |
| GET `/harness/agent/sessions`, 200 | `SessionList` | `sessions[]`: `SessionInfo` либо `SuspendedSessionInfo` |
| Активная запись | `SessionInfo` | `sdkSessionId` — непрозрачный Gate alias для последующего `resumeSessionId`, nullable |
| Недоступный Router | `SuspendedSessionInfo` | `id`, `model`, `state=session_suspended`, известный alias либо null; живые счётчики и флаги не выдумываются |
| Событие `session` | Существующий поток событий | `sessionId` — Gate alias; поле события отличается от `sdkSessionId` в списке |

Create ACK **не содержит договорного поля `sdkSessionId`**. Требовать его для
подтверждения resume или сохранять произвольное добавочное поле из ACK как alias
нельзя. Внутренние Router HTTP IDs и vendor SDK IDs клиенту Gate не передаются
в этих полях. Alias берётся из списка либо события текущей сессии, с учётом владельца.

Только явное булево `resumed: true` подтверждает восстановление SDK-истории.
Отсутствие флага, `false`, строка `"true"`, HTTP 200 сами по себе её не подтверждают.
Дополнительно проверяется точное согласование `sdkTools`. Флаг resume относится
к истории SDK и не доказывает доставку изображения или выполнение задания.

## Совместимость и повторы

Gate возвращает сохранённые байты create ACK, включая известный отказ, через
`Response`; `response_model` описывает успешный контракт в OpenAPI и не подставляет
значения по умолчанию в сохранённый ответ. Операционный исход и заголовки создания
сохраняются. Пропущенный флаг не превращается в подтверждение на повторной доставке.

Модели успешных ответов разрешают дополнительные поля. Список сериализуется с
`exclude_unset`, поэтому новое описание API не теряет добавочные данные и не
превращает отсутствующие поля в новые null/false/пустые массивы. Известные поля
`sdkTools`, nullable `configuredMaxOutputTokens`, субагенты и media capability
остаются частью ответа.

Неизменные тело и `Idempotency-Key` дают прежний исход. Изменённое тело с прежним
ключом — конфликт. При неизвестном исходе проверяется receipt; повтор создания
или prompt без доказательства не допускается. Смена `sdkTools` старой беседы
использует отдельный ключ/тело и прежний alias со строгим resume, после безопасного
закрытия старой idle HTTP-сессии. Этот выпуск не меняет правила исполнения команд,
receipts, compact, хранения истории и media и не вводит временных пределов.

## Проверка контракта

`tests/fixtures/contracts/session-wire-v085.json` содержит фактические схемы
Router 0.8.5 и очищенные production ACK/SessionInfo инцидента 20.10.2026.
Идентификаторы и время заменены, пользовательских сообщений и секретов нет.
Одинаковые live OpenAPI схемы Codex/Claude проверены read-only перед реализацией.

`tests/test_session_contract.py` проверяет опубликованный OpenAPI Gate, замороженный
контракт, текущие схемы отдельного Router checkout и реальные HTTP ответы. Варианты:
direct и connector channel/executor; сценарные Codex/Claude capabilities;
fresh/create/list, история и смена imageGeneration, точный replay, конфликт,
потерянный ACK с receipt recovery, пропущенный resumed, suspended и alias isolation.
Проверяется отсутствие повторных SDK open и отсутствие inference/turn buffers.
Известные rejected исходы дополнительно покрыты `test_create_replay*.py`.

Пример запуска из checkout Gate:

```powershell
$env:ATLAS_TEST_ROUTER_ROOT = 'C:/path/to/atlas-router'
uv run pytest tests/test_session_contract.py tests/test_create_replay.py tests/test_create_replay_network.py
uv run pytest
uv build
```

Router является **тестовой** зависимостью по HTTP; Gate не импортирует Router/SDK
в runtime и не включает их в wheel. Сценарные проверки connector не заменяют
внешнюю mTLS-квалификацию, реальные SDK inference или приёмку установленной EDT.
Состояние поставки и фактические результаты проверок публикуются отдельно в #65.
