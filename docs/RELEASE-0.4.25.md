# Gate 0.4.25 — подтверждение identity после resume

Gate #103 / EDT #228: новый read-only `history-identity` endpoint и additive
подписанная capability позволяют клиенту доказательно сопоставить старый opaque
alias и новую Gate binding, если прежняя native SDK история сохранена. Без
сохранённой авторизованной resume-цепочки и известных равных native identities
подмена alias запрещена. Клиентские ограничения checkpoint и аудит обязательны.

Runtime Router и native SDK не меняются, Router 0.8.23 достаточен. Старые клиенты
продолжают работать по прежнему контракту. Никакие prompt/tools не повторяются,
история и usage не очищаются. Выпуск не исправляет иной transport отказ и не
утверждает, что пользователь уже прочёл сохранённый итог.

Проверяются только identity/policy/signed capability/version и целевой путь
Gate→2 deterministic Router: resume, старый/новый alias, доказательство, отказ
старому source manifest, свежая inventory и чтение исходного terminal без новой
генерации. Отдельный loopback fixture обеспечивает Java→Gate→Router приёмку EDT.

Контракт: [SOURCE-HISTORY-IDENTITY103.md](SOURCE-HISTORY-IDENTITY103.md).
