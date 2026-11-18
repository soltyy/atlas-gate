# Рекон Gate #135 — ответы на вопросы Harness #545

18.11.2026. База `e9777f1dbe73ea94ed7675229b3c4f2772bfd919` (0.4.41),
Router `58a4129bd85e3611ca3c714b0977ac46afeb9d66` (0.8.29).
На рабочих службах рекон не менял БД, org-профили, SDK-истории или процессы.

В прежнем контракте `/tool_result` и `/steer` не образуют единой операции
«callback вопроса + обычная реплика». Реализован отдельный outbox и endpoint,
описанные в [QUESTION-ANSWERS-V1.md](QUESTION-ANSWERS-V1.md). В Gate нет SDK:
authoritative admission/application принадлежат точному Router node/boot.

Проводка: профиль → ProfileModel → bound SessionCreated / sdk-tools → device/org
policy → сохранённые raw bytes → direct HTTP либо enrolled connector → Router
question-answer → проверенная квитанция → внешний SID/key. В полной регрессии
найдена и исправлена потеря нового поля в ProfileModel: подписанный payload
теперь совпадает с выдаваемым API-профилем. PublicModel capability узлов не раскрывает.

Старый connector не проходит header round-trip, поэтому supported=false.
Поддержка на уровне node/profile не разрешает использовать операцию без проверки
конкретной сессии. Доставка не переключает node/boot, не создаёт prompt и не
повторяет инструмент. Квитанция проверяет владельца, org, SID, turn, call, key,
SHA, UUID и исходный boot. Поздний ACK не понижает applied до pending.

Изолированные настоящие HTTP/connector проверки покрыли потерю POST/ACK,
изменённые байты, чужого владельца, boot, UUID/hash квитанции, reopen store,
legacy tool_result и старый connector. В живом Codex доказаны exact user=1,
callback-before-user, effect=1 и question=1 по обоим transport. Claude SDK/CLI
проверен на локальном провайдере; живая подписка пока вернула временный rate_limit.
Это не положительная живая приёмка Claude и не разрешение включить feature клиенту.

## Регрессия исходной базы, отдельно от #135

На Windows полный прогон: 503 passed / 19 skipped / 14 failed. Новые ошибки
подписанного профиля и additive OpenAPI устранены, затем целевые проверки дали
58 passed. Metadata старого общего test-venv (0.4.39) не совпадала с исходниками
0.4.41: установленные wheel проверены отдельно, 2 passed. POSIX chmod 0600
невозможно подтвердить Windows st_mode, это не проверка Linux production.

Следующие три сбоя воспроизведены **без изменений #135/#127** на чистых базовых
Gate e9777f1 / Router 58a4129 (3 failed), поэтому не выдаются за новые регрессии:

- `test_closed_prompt_admission[missing]`: ожидает 409, получает 202;
- `test_gate_cold_files_restore_keeps_scope_owner_and_upload_ack`: generation 0 вместо 1;
- `test_stop_after_route_disabled_and_quota_before_send`: agent_turns 1 вместо 0
  при отказе discovery до POST.

Ни один из этих результатов не является зелёной приёмкой соответствующей функции.
В #135 они фиксируются как известная граница базы. Полные логи остаются вне Git:
они могут содержать только тестовые enrollment tokens, а не рабочие credentials.

Кандидат 0.4.42 не считать поставленным до живого Claude, финальной проверки
артефактов и штатного переключения после освобождения ходов. Контракт v1 имеет
явную границу root_turn; вопросы субагентов клиент не должен рекламировать.
