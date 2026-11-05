# Проверки host_audit.py: что значит каждая

Запуск на каждом Linux-сервере контура: серверы RX, база и реплики, брокер, поиск, хранилище, прокси.
Скрипт только читает: ничего не перезапускает, не меняет и не пишет на диск. Под root видно всё,
без root часть проверок честно помечена ERROR с причиной.

```bash
sudo python3 ${CLAUDE_SKILL_DIR}/scripts/host_audit.py \
  --config-dir /srv/DirectumLauncher/etc --config-dir /etc/rabbitmq \
  --tls rx.example.com:443 --tls pg://db.example.com:5432 --tls mq.example.com:5671
python3 ${CLAUDE_SKILL_DIR}/scripts/host_audit.py --list            # имена проверок
python3 ${CLAUDE_SKILL_DIR}/scripts/host_audit.py --only sshd,ports --json
```

Путь `/srv/DirectumLauncher` взят из примеров справки RX, у вас может быть другой. Если скрипта на сервере
нет, его можно скопировать вместе с `mask_secrets.py`: оба файла рядом, нужен только python3 3.6+.

Статусы: OK, WARN, CRIT, INFO (сведения), SKIP (не применимо или не задано), ERROR (не удалось проверить).
Код возврата: 1 есть WARN или CRIT; иначе 2 есть ERROR; иначе 0. В JSON у каждой проверки поле
`questions`: к каким вопросам опросника она относится.

| Проверка | Что смотрит | WARN или CRIT | Без root | Вопросы |
|---|---|---|---|---|
| `system` | ОС, ядро, Astra Linux, root, контейнер | только INFO | работает | q13 |
| `sshd.root` | `PermitRootLogin` | CRIT: `yes` при входе по паролю; WARN: `yes` по ключу, `prohibit-password` | разбор файлов | q7, q8 |
| `sshd.password` | `PasswordAuthentication`, `KbdInteractiveAuthentication` при `UsePAM`, `AuthenticationMethods`, `PermitEmptyPasswords` | WARN: вход по паролю; CRIT: ещё и пустые пароли | разбор файлов | q8 |
| `accounts.uid0` | UID 0 у кого-то кроме root | CRIT | работает | q7 |
| `accounts.login` | учётки с оболочкой входа, число ключей SSH | только INFO | ключи части учёток не видны | q7 |
| `accounts.emptypw` | пустое поле пароля в `/etc/shadow` | CRIT | ERROR | q7 |
| `sudo.nopasswd` | `NOPASSWD` на все команды, `Defaults !authenticate` | WARN; на перечисленные команды INFO | ERROR | q7 |
| `sudo.groups` | участники sudo, wheel, admin, docker, lxd | только INFO | работает | q7 |
| `auditd.service` | служба auditd | WARN: не установлена или не работает | работает | q11 |
| `auditd.rules` | `auditctl -s`, `auditctl -l` | WARN: правил нет, `lost` больше 0, `enabled 0` | ERROR | q11 |
| `logship` | audisp-remote, пересылка rsyslog и syslog-ng, агенты (vector, fluent-bit, filebeat и др.) | WARN: ничего не найдено | частично | q11, q18 |
| `firewall` | firewalld, ufw, nftables (цепочка `hook input` с `policy drop` или drop/reject), iptables INPUT | WARN: фильтрации входящих нет | ERROR, если не видно firewalld или ufw | q19, q4 |
| `ports.exposed` | служебные порты на всех адресах: 5432, 6432, 1433, 5672, 5671, 15672, 15692, 25672, 4369, 9200, 9300, 5601, 9000, 9001, 2375, 2376, 2379, 10250, 5000, 6379, 27017 | WARN, если экран не найден; CRIT: Docker API 2375 в сети | процессы чужих сокетов не видны | q4, q21 |
| `ports.list` | все слушающие сокеты (`ss -tulpn` или `/proc/net`) | только INFO | работает | q21 |
| `secrets:<каталог>` | файлы с паролями и ключами, доступные всем на чтение, в `--config-dir` | WARN | работает полностью: файл, доступный всем, читается и без root | q9 |
| `history` | пароли в истории команд, права `.pgpass` | WARN | только свои файлы | q9 |
| `tls:<хост>:<порт>` | срок, SAN, цепочка, приём TLS 1.0 и 1.1 | CRIT: истёк или меньше `--tls-crit-days`; WARN: меньше `--tls-warn-days`, нет имени в SAN, цепочка не проверяется, принимает TLS 1.0 или 1.1 | работает | q6 |
| `time` | `timedatectl`, `chronyc tracking`, `ntpstat` | WARN: не синхронизировано или смещение больше 60 с | работает | q22 |
| `mac` | SELinux, AppArmor, список LSM | WARN: SELinux Permissive или нет ни SELinux, ни AppArmor | профили AppArmor не видны | доп. |
| `mac.astra` | `/etc/astra_version`, `astra-modeswitch get`, `astra-mic-control status`, `astra-mac-control status`, `parsec.max_ilev` | WARN: МКЦ неактивен | частично | доп. |
| `containers:docker`, `containers:podman` | `--privileged`, сокет Docker внутри, pid host, `SYS_ADMIN` и др., unconfined, образы | WARN | ERROR без группы docker | q16, доп. |
| `repos` | `trusted=yes`, `Trusted: yes`, `gpgcheck=0`, `AllowUnauthenticated`, хосты источников | WARN | работает | q16 |
| `updates` | давность последней установки пакета (dpkg или rpm), нужна ли перезагрузка | WARN: дольше `--updates-days` (90) | работает | q5 |

## Как читать находки

- **sshd.** Под root берётся `sshd -T` (действующие значения). Без root разбираются `sshd_config` и файлы
  из `Include` по правилу sshd «первое значение побеждает», блоки `Match` пропускаются и упоминаются
  в выводе; для незаданных ключей подставлены умолчания OpenSSH. Если часть файлов не прочитана, итог
  помечен ERROR: непрочитанный файл мог задать значение раньше.
- **ports и firewall.** Скрипт видит только сам сервер. Порт на всех адресах при включённом экране
  выводится как INFO: правило экрана нужно посмотреть глазами. Порты, опубликованные Docker, обходят
  правила ufw и firewalld, ограничивать их нужно адресом публикации или цепочкой `DOCKER-USER`.
  Порт 5000 и 9000 бывают не только у Directum Launcher и MinIO: смотрите на процесс.
- **secrets.** Значения не печатаются никогда, только путь, права, владелец и число найденных значений.
  Файлы больше 2 МБ и двоичные пропускаются, кроме контейнеров ключей (`.pfx`, `.p12`, `.jks`, `.key`).
  Для `config.yml` RX без `config_password` выводится заметка: шифрование параметров не настроено.
- **history.** Пароль, найденный в истории, считается скомпрометированным: его меняют, а не только
  стирают историю.
- **tls.** Цепочка проверяется по доверенным корням той машины, где запущен скрипт. Для внутреннего центра
  передайте его корень: `--tls-ca /path/ca.crt`. Старые протоколы проверяются отдельными подключениями
  с `-tls1_1` и `-tls1`; если местный openssl их не умеет, это написано в выводе. Схема `pg://` включает
  STARTTLS PostgreSQL, `ldap://` и `smtp://` тоже STARTTLS.
- **time.** В контейнере время берётся с хоста: проверяйте хост.
- **mac.astra.** Вывод команд Astra печатается как есть; значения уровней сверяйте с документацией Astra
  Linux своей версии. Отключать МКЦ ради контейнеров нельзя.
- **containers.** Каждый контейнер с `--privileged` или сокетом Docker в оценке рисков равен root на хосте.
  Без root podman показывает только контейнеры текущего пользователя.

## Чего скрипт не делает

Не подключается к PostgreSQL, RabbitMQ и Elasticsearch, не читает настройки RX в базе, не сканирует сеть
и не проверяет Windows-серверы. Это ручные проверки из `manual-checks.md` и `rx-settings.md`.
