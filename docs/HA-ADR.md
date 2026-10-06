# ADR: один активный Gate, много Router

Принято для первого отдельного выпуска. Масштабируется слой Router, включая
несколько установок на одной машине. Gate — один активный владелец SQLite;
file lock/транзакции защищают резервы. Два listener используют один Store.

Беседа закреплена за node/account/SDK history. Failover допустим для новой беседы.
При потере ACK команда сверяется по ID; неизвестный исход не равен неисполнению.
Restart того же Router допускает lazy SDK resume после проверки SDK ID;
отсутствие истории даёт ошибку.

Passive Gate требует fencing старого владельца и согласованных SQLite/signing
key/keyring/orgs/PKI backups с прежними NODE_ID и Router journals. Shared WAL,
multi-active Gate и leader failover без fencing не поддерживаются. Postgres/consensus
ради гипотетических нескольких Router не вводятся.

Retention больших событий — 7 дней по умолчанию, затем 410. Usage receipts остаются
без текста модели. Command IDs/tombstones сохраняют защиту от повторного выполнения.
Нужны контроль размера БД, backup и restoration drills; archive/rotation БД
только с остановленным владельцем и сохранением dedup history.
Проверены process restart и настоящий Codex. Multi-host Gate failover и перенос
SDK аккаунта не заявляются.
