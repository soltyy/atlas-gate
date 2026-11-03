# Gate 0.4.24 — восстановление авторизации и admission prompt

Кандидат #97 / EDT #224 и Gate #98. [Auth wire и приёмка](AUTH-RECOVERY97.md),
[admission fence и operator witness](RETRY-RECEIPTS.md).

Добавлены versioned safe rotation, короткоживущий challenge/device proof recovery,
неизменные encrypted receipts с terminal tombstones и административное подтверждение
прежнего устройства. Bearer paths не выдают device_secret. Legacy endpoints сохранены.
Восстановление не меняет SDK историю, binding, пользователя/организацию и не исполняет
prompt/tools. Router в этой поставке не меняется.

Подтверждённый собственный pre-dispatch503 prompt сохраняет внутреннее свидетельство
в одной транзакции с HTTP-квитанцией. Version1 reconcile выдаёт стабильную
admission fence с привязкой к владельцу, телу и исходному адресату;
исходные status/body/state сохраняются,
поздний исходный POST не отправляется повторно. Продолжение требует нового intent.
Unknown, потерянный ACK, arbitrary5xx и deliveryState:not_sent без внутреннего
свидетельства остаются observation-only.

Исторические записи автоматически не переосмысливаются. Внутренний offline helper
может добавить provenance отдельной операции только по независимо проверенному
operator witness с root:0600 audit и hash, точным scope/response hash и положительным
интервалом остановки процесса, покрывающим admission и завершение HTTP-отказа.
Публичного repair API нет; helper не меняет HTTP-квитанцию, квоту, binding или историю
и не обращается к Router/SDK. Импорт конкретного witness требует отдельного разрешения.

Source tests: 18 auth/discovery/version HTTP, 51 UI и UI build passed на Windows.
Совместная Java→Gate→2 stub Router проверка прошла без новых business operations.
Gate #98: 21 целевая HTTP/SQLite проверка с собственным loopback fake прошла до
объединения веток; SDK/model calls: 0. Проверены restart, replay/races, неизменный
исходный HTTP503, отрицательные guards и successor с effect counter=1.
Финальная SDK-free Linux/installed/wheel qualification и deploy guard — у ведущего
серверного чата. Пока их proof не опубликован, эта страница не означает поставку.
