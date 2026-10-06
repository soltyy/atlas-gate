# Развёртывание vdska — 06.10.2026

Пользователь назначил SSH alias `vdska`, HTTPS `gate.atlcon.ru`, текущую подписку
ChatGPT и существующий Claude вход сервера. Ubuntu 24.04, Python 3.12.3.
Новый независимый Gate не является активной копией Windows production.

| Служба | Bind | Пользователь | Данные |
|---|---|---|---|
| atlas-gate | 127.0.0.1:8766 | atlas-gate | /var/lib/atlas-gate |
| atlas-router (Codex) | 127.0.0.1:8765 | atlas-router | /var/lib/atlas-router |
| atlas-router-claude | 127.0.0.1:8768 | akbot | /var/lib/atlas-router-claude |

Установленные commits: Router `1b2ed58b02e475a0d06a179a578f79aa77c6a5cb`,
Gate первоначально `c360caf20f8454f4f4efe577b22c1421ac4058a5`, затем обновлён до
`7be197721188807c72c007ebea19e263ce79fd27` (редактор моделей, #11).
Git archive SHA-256 сверены,
зависимости установлены `uv sync --locked --no-dev --python /usr/bin/python3`.
Релизы `/opt/atlas/releases/{router-1b2ed58,gate-7be19772}`, ссылки
`/opt/atlas/{router,gate}`. Установлен код кандидатов, PR ещё не слиты в main.

Конфигурации `/etc/atlas/{router,router-claude,gate}.env`, root 0600;
секреты не записаны в репозиторий или unit files. У Gate свои SQLite/signing
key/keyring secret/admin token и организация `vdska`. nodes.json и
node-secrets.env в его каталоге данных. NODE_ID `vdska-codex`/`vdska-claude`,
разные account_group и routes codex-sub/claude-sub. Capacity/active units 4,
до 3 subagents на Router; профиль ассистента по умолчанию sonnet.

Claude service работает от akbot для использования существующего server
profile `/home/akbot/.claude`; этот профиль не копировался. Codex использует
отдельный `/var/lib/atlas-router/.codex`, без переноса credentials с Windows.
По указанию пользователя device-auth отменён без изменения security settings.
Пользователь завершил обычный browser OAuth в своём текущем ChatGPT аккаунте;
callback 127.0.0.1:1455 прошёл через SSH-туннель на server app-server.
Router подтвердил loggedIn=true, method=chatgpt; auth.json сохранён в отдельном
CODEX_HOME. После приёмки временный callback-туннель закрыт.

## HTTPS и запуск

DNS gate.atlcon.ru указывает на сервер. Новый nginx virtual host `atlas-gate`
проксирует Gate, ограничивает body 32m, передаёт исходный IP через собственный
X-Forwarded-For, отключает buffering SSE; admin paths снаружи 403.
Let's Encrypt certificate, ACME webroot и renewal deploy hook nginx reload
настроены. Имеющийся corestack/aplatform virtual host сохранён и отвечает 200.
Router порты не опубликованы наружу. Корень HTTPS перенаправляет на
`/harness/discovery`: проверка из Windows вернула 200. После сверки каталога
и удаления устаревшей gpt-5.5 профиль версии 10 содержит 12 моделей.
Единичный 502 во время перезапуска Gate устранён; это не ошибка OAuth.

Все три systemd units enabled/active, без автоматических restart на момент
проверки. Gate начинает после обеих Router служб; bounded pre-start warmup
ждёт их model catalogs, чтобы холодный SDK старт не удалял модели из нового
профиля. По timeout Gate запускается и использует сохранённые каталоги.
Reboot общего сервера не выполнялся; факт enabled не называем reboot test.

## Подтверждённая приёмка

- На установленных systemd службах stub: enrollment → JWS settings →
  session/tool call/result → terminal events, закрытие сессии.
- После переключения на SDK: оба Router подключены; discovery/settings
  содержат модели обоих Router и разные PDF capabilities. После #11 сверка
  каталога даёт 5 моделей Claude и 7 Codex. Редактор получает отдельные каталоги
  org+route с именами SDK; неизвестные лимиты показаны как неизвестные.
- Настоящий Claude sonnet через `https://gate.atlcon.ru`: mixed PDF 925144
  bytes, TEXT 42017 и raster SCAN 7391 прочитаны; каталог содержит 2 страницы.
  Цифры отсутствуют в prompt. SDK 0.2.159, существующий вход loggedIn=true.
- Настоящий Codex gpt-6.1-sol через тот же публичный HTTPS Gate: mixed PDF
  925144 bytes, TEXT 42017 и raster SCAN 7391 прочитаны правильно; один документ,
  ошибок нет. SDK 0.160.0, method=chatgpt. Проверка прошла на установленных
  systemd службах сервера, тестовая сессия удалена.

SQLite Online Backup/PRAGMA integrity_check прошли для нового Gate и обоих
Router journals. После входа выполнен свежий root-only backup
`/var/backups/atlas/20261006T092310Z`, затем перед обновлением #11
`/var/backups/atlas/20261006T094241Z`, содержащий SQLite/config/orgs/signing key,
units и nginx virtual host. Credentials/histories
существующего Claude профиля в backup не копировались. Каталог содержит
секреты: не публиковать и не передавать неавторизованным пользователям.
Manifest `/var/lib/atlas-gate/deployment-manifest.json` и отдельные acceptance
JSON фиксируют фактический серверный результат. Restore drill ещё не выполнен.

Администрирование JSON API: SSH и защищённый admin token из серверной
конфигурации. `ATLAS_TRUST_LOCAL=false`; отключать auth ради открытия UI не надо.
Harness использует URL `https://gate.atlcon.ru` и новый enrollment; старые
device tokens/signing trust Windows Gate здесь не действуют.

Откат нового контура: остановить только три atlas units, убрать его proxy из
доступа тестовых клиентов. Старые Windows production Router/EDT и их Store
не менялись; массовый перевод устройств/DNS старого Gate не выполнялся.
