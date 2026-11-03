# Gate 0.4.24 — авторизация без смены владельца истории

Кандидат #97 / EDT #224. [Wire и приёмка](AUTH-RECOVERY97.md).

Добавлены versioned safe rotation, короткоживущий challenge/device proof recovery,
неизменные encrypted receipts с terminal tombstones и административное подтверждение
прежнего устройства. Bearer paths не выдают device_secret. Legacy endpoints сохранены.
Восстановление не меняет SDK историю, binding, пользователя/организацию и не исполняет
prompt/tools. Router в этой поставке не меняется.

Source tests: 18 auth/discovery/version HTTP, 51 UI и UI build passed на Windows.
Совместная Java→Gate→2 stub Router проверка прошла без новых business operations.
Финальная SDK-free Linux/installed/wheel qualification и deploy guard — у ведущего
серверного чата. Пока их proof не опубликован, эта страница не означает поставку.
