# Передача знаний Atlas Gate

Проект выделен 04.10.2026 из `soltyy/atlas-router`, исходная база v0.6.0
`e9613374caa640738f4b197ea83d8d3e50f0e943`. Перенесены собственные Gate-модули,
схемы HTTP-протокола, auth и админка; референсный AGPL-код не использовался.
История источника: https://github.com/soltyy/atlas-router/tree/e9613374caa640738f4b197ea83d8d3e50f0e943

Gate хранит корпоративные данные, подписывает профиль и вызывает Router по HTTP.
SDK вход/история/compact/resume принадлежат Router. Нет доступа к его Python-реестру.
Текущая основная служба Router остаётся на прежнем выпуске до отдельной миграции.

Разработка: Python 3.12, uv, FastAPI, SQLite/WAL, Vue. Проверки: `uv run pytest`,
`npm ci`, `npm test`, `npm run build` в ui; Python wheel: `uv build`.
Рекон/концепция: `GATE-ROUTER-CONCEPT.md`; текущие границы: `IMPLEMENTATION.md`.
Тикеты проектов имеют независимые номера. Сведения прежних чатов не являются
новыми поручениями или разрешением на поставку.
