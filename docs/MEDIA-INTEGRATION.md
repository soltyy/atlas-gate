# Интеграция результатов инструментов с изображениями

Gate #30 / Router #53 и #55, 08.10.2026.

PR Gate #29/#31 и Router #52/#54 добавляют два независимых opt-in поля:
`maxOutputTokens` (Claude provider response budget) и `toolResultImages`
(согласование native image tool results). Старые текстовые запросы сохраняются.
ACK бюджета не ограничивает суммарный SDK turn: автоматическое продолжение
может вызвать следующий ответ провайдера.

Gate проверяет возможность по закреплённой сессии, а не только по каталогу
Router. Старый промежуточный узел, отбросивший opt-in, не даёт разрешения
отправлять изображения. Отсутствующая capability не считается true.

При интеграции выявлены два дополнительных разрыва Router:
- встроенный Gate отбрасывал negotiated flag и объявлял глобальную capability;
- NAT connector не разрешал `/v1/sessions/{id}/attachments`, поэтому запрос
  bound capabilities зависал до таймаута.

Оба исправления ведутся в Router #55 / PR #56. При поставке нужны обновлённые
Router и connector. Проверка `tests/test_tool_result_images.py` проходит по
реальному HTTP для direct и connector; она проверяет сохранение image envelope,
отказы до forwarding и прежний text-only путь. SDK inference в ней сценарный.

Полный контракт и порядок совместимой поставки:
[Router TOOL-RESULT-IMAGES](https://github.com/soltyy/atlas-router/blob/codex/media-integration/docs/TOOL-RESULT-IMAGES.md).
До переключения Router подтвердить клиентскую поддержку `maxTextChars:null`.
Для EDT она выпущена в 1.0.0.202610071825; ЧатИИ требует отдельного подтверждения.
Создание SDK-сессии и ACK не доказывают чтение raster моделью или восстановление
пользовательского UI. Эти результаты нельзя обозначать PASS без отдельной пробы.
