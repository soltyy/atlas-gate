# Авторизация v1: ротация и прежнее устройство (#97 / EDT #224)

Кандидат Gate 0.4.24. Поставку подтверждает release report/issue, не этот документ.

## Wire и rollback

Публичный discovery содержит `auth_contract` и отдельный `auth_contract_jws` EdDSA,
проверяемый клиентом закреплённым при регистрации ключом. Контракт version=1,
purpose=`atlas-gate-auth-recovery`. Его адреса фиксированы:

- POST `/harness/auth/v1/rotate`: `refresh_token`, обязательный `rotation_id` (16–100 знаков).
- POST `/harness/auth/v1/challenge`: `device_id`, `request_id` (16–100 знаков).
- POST `/harness/auth/v1/recover`: те же device/request, `challenge`, `proof` (HMAC-SHA256 hex).
- POST `/harness/auth/v1/enroll/start`: обычные device_name/platform/app_version,
  optional device_id прежнего устройства, если клиент его знает.
- POST `/harness/auth/v1/enroll/approve`: административный X-Atlas-Token,
  user_code/org/user и обязательный recover_device_id.
- POST `/harness/auth/v1/enroll/poll`: device_code исходной recovery заявки.

Legacy enrollment/refresh сохранены. Strong клиент никогда не заменяет эти операции
legacy запросами после 404/потери capability. При rollback старый runtime возвращает
404 до mutation. Recovery enrollment получает `recovery_pending`,
`recovery_approved`, `recovery_consumed`: старые обработчики не принимают эти
состояния за обычную регистрацию. Создание заявки и intent — одна SQLite транзакция.

## Квитанции и proof

SQLite/FULL transaction сохраняет новые token hashes, зашифрованный текущий результат
и квитанцию ротации вместе. Одинаковые predecessor+rotation_id возвращают прежнюю
пару. Конкурирующий другой rotation_id может сойтись к той же паре, только пока она
проверенно текущая. Прежний access действует до своего исходного expiry; device
revocation проверяется при любом доступе, включая чтение квитанций.

Recover читает текущую пару по доказательству владения прежним device_secret.
Этот секрет выдаётся ТОЛЬКО enrollment poll после одобрения администратора.
Refresh/recover и обычный bearer не выдают device_secret; identity endpoint нет.
Legacy клиент без сохранённого секрета должен подтвердить прежнее устройство,
а не получить recovery authority в обмен на короткоживущий bearer.

Challenge — подписанный Gate JWS с device_id/request_id, nonce, issued_at и
expires_at=issued_at+300. HMAC подписывает canonical UTF-8 JSON с ключами по алфавиту:
`challenge, device_id, purpose, request_id, version`;
purpose=`atlas-gate-token-recovery`, version=1. Сервер проверяет подпись, scope и
срок challenge ДО использования квитанции. Клиентское время не задаёт срок.

Повтор recover возвращает неизменную пару этой операции. Истёкшая операция остаётся
terminal: GC очищает encrypted payload, сохраняя постоянный компактный tombstone.
Ни удаление payload, ни новый challenge с прежним request_id не открывают её заново.
Новый read current получает новый request_id; это не новое business действие.

До current recovery/convergence проверяются оба расшифрованных token hashes и оба
expiry против фактической tokens в той же транзакции. Несогласованность/повреждение
→ 409 `device_confirmation_required`, не успешная выдача stale pair. Expired refresh
→ 401 `token_expired`; revoked → 401 `device_revoked`; неизвестные реквизиты → 401
`unauthorized`; неверный/истёкший challenge → 401 `recovery_proof_expired`.

## Уже потерянные реквизиты

В EDT выбрать «Восстановить вход». Администратор в recovery заявке выбирает прежнее
неотозванное устройство и видит точные user/org/device ID перед подтверждением.
Org/user и optional expected device ID проверяет сервер. Восстановление не создаёт
новый device и не меняет router_bindings/SDK aliases/history/media ownership.
Отозванное устройство этим путём не оживляется. Poll/ротация не исполняют prompt/tools.

Перед поставкой сохранить DB/keyring/signing key штатной процедурой. Миграция
аддитивна: auth_current/auth_receipts/auth_access/auth_enrollment/auth_enrollment_intent.
Старые таблицы/владение не переписываются. После legacy hash-only записи/rollback
сверка current остаётся обязательной. Шифротексты не логировать/не помещать в Git.

## Приёмка

`tests/test_auth_rotation97.py`, discovery/version: 18 HTTP tests passed на Windows.
UI: 51 tests passed, production build passed. `tests/auth97_fixture.py` — временный
Gate и два stub Router для Java acceptance; только loopback, synthetic credentials,
один seed prompt. Клиент проверяет legacy confirmation required, затем same-device
approval и автоматическое восстановление следующего mismatch, неизменные binding/
operation counters и байты conversation файлов. SDK/provider inference не используется.

Первый широкий Linux прогон до security/source freeze: 435 passed/7 skipped/2 failed
против установленного Router 0.8.23. Отказы — cold files recovery и сохранённый Router
OpenAPI snapshot (новое steering поле); это не положительная приёмка auth-кандидата.
Повторные серверные проверки ограничить изменённой auth/ownership областью; Router
и уже выполненные SDK приёмки не повторять. Финальный installed/source/wheel proof
публикуется ведущим серверной поставки в #97. Закрытие только после общей приёмки.
