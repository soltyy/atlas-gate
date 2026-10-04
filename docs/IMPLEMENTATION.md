# Реализация разделения, 05.10.2026

Gate — самостоятельный SDK-free пакет; Router хранит вход аккаунта и SDK историю.
Реализованы публичные SID/SDK aliases с device/org/node/boot affinity, выбор готового
свободного узла, атомарные резервы ходов/compact/субагентов и общая account_group.
Discovery не выполняет prompt; подтверждённая подписочная квота участвует в выборе.

Router имеет постоянный журнал команд/событий и usage receipts. Gate сверяет
неизвестные ответы, восстанавливает watchers и учёт после restart. Повторное чтение
не дублирует расход. SSE клиента собирается из async polling Router. Инструменты,
interrupt, steer, task stop, native compact/context/resume проходят HTTP зеркало.
История не заменяется пустой сессией при неподтверждённом SDK resume.

Добавлены CSR approval, локальные Ed25519 ключи, одноразовые signed proofs, отдельный
mTLS ingress, продление/revoke сертификатов, исходящий NAT connector с дисковой
очередью и безопасным повтором ACK. Узел не назначает себе org/route права.
Отзыв устройства блокирует доступ немедленно; закрытие недоступной SDK сессии
повторяется после восстановления связи. Сверка освобождает места idle sessions.
Админка показывает узлы/нагрузку/cooldown/drain/регистрацию. Есть миграция и
подготовщик Windows-службы с проверкой WinSW hash и без секретов в XML.

Тикеты: Gate [#1](https://github.com/soltyy/atlas-gate/issues/1),
[#2](https://github.com/soltyy/atlas-gate/issues/2),
[#3](https://github.com/soltyy/atlas-gate/issues/3),
[#5](https://github.com/soltyy/atlas-gate/issues/5),
[#6](https://github.com/soltyy/atlas-gate/issues/6),
[#7](https://github.com/soltyy/atlas-gate/issues/7),
[#8](https://github.com/soltyy/atlas-gate/issues/8);
Router [#22](https://github.com/soltyy/atlas-router/issues/22),
[#23](https://github.com/soltyy/atlas-router/issues/23),
[#25](https://github.com/soltyy/atlas-router/issues/25).

Кандидат Router 0.7.0 / Gate 0.1.0. Production остаётся Router 0.6.0 со встроенным
Gate до приёмки миграции. Один активный Gate на одну БД; Router может быть много,
включая cohost. Автоматического переноса SDK истории между аккаунтами нет.
Неизвестный исход инструмента после crash сообщается явно.

Следующие документы содержат конкретные процедуры и пределы проверки:
[рекон](RECON-STAGES.md), [установка](DEPLOYMENT.md), [миграция](MIGRATION.md),
[HA ADR](HA-ADR.md), [приёмка](ACCEPTANCE.md).
