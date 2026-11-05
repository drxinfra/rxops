# Ручные проверки: что host_audit.py не видит

Команды только читают. Вывод, где могут быть пароли, пропускайте через маскировщик:
`КОМАНДА 2>&1 | python3 ${CLAUDE_SKILL_DIR}/scripts/mask_secrets.py`. Адреса в примерах условные.
В скобках номер вопроса опросника, к которому относится проверка.

## PostgreSQL (q3, q4, q7, q10)

Подробная проверка базы в скиле `rx-postgresql`. Здесь минимум для самооценки.

```sql
-- Кто входит и с какими правами
SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolcanlogin, rolvaliduntil
FROM pg_roles WHERE rolcanlogin ORDER BY rolsuper DESC, rolname;

-- Правила доступа: trust для сетевых адресов и md5 вместо scram-sha-256
SELECT line_number, type, database, user_name, address, auth_method
FROM pg_hba_file_rules ORDER BY line_number;

-- Шифруются ли соединения от сервисов RX (q6 для внутреннего трафика)
SELECT a.usename, a.client_addr, a.application_name, s.ssl, s.version
FROM pg_stat_activity a JOIN pg_stat_ssl s USING (pid)
WHERE a.backend_type = 'client backend' ORDER BY s.ssl, a.client_addr;
```

Норма: суперпользователи только у администраторов СУБД, `trust` только для локальных подключений,
`scram-sha-256` вместо `md5`, учётка RX без `rolsuper`, у мониторинга встроенная роль `pg_monitor`.
Если требуется шифрование до базы: соединения с `ssl = f` разбираются по адресу и имени приложения.
Сервисы RX подключаются через Npgsql, режим задаётся параметром `SSL Mode` в строке подключения:
`Require` шифрует, но не проверяет сертификат, проверку дают `VerifyCA` и `VerifyFull`. Поведение режимов
менялось между версиями Npgsql, проверяйте на тестовом стенде. Разбор:
https://drxinfra.ru/blog/rx-postgresql-tls-npgsql.html

Сертификат базы снаружи: `host_audit.py --only tls --tls pg://db.example.com:5432`.
Цепочка в файле целиком: `grep -c 'BEGIN CERTIFICATE' server.crt; grep -c 'END CERTIFICATE' server.crt`
(числа должны совпасть).

## RabbitMQ (q3, q4, q21)

Подробно в скиле `rx-rabbitmq`.

```bash
rabbitmqctl list_users                        # guest, учётки с тегом administrator поимённо
rabbitmqctl list_permissions -p /             # права по виртуальным хостам
rabbitmq-diagnostics listeners                # какие порты слушает: 5672, 5671, 15672
rabbitmqctl list_connections user peer_host ssl | sort | uniq -c   # кто подключён и с TLS ли
```

Норма: guest удалён или пускается только с самого сервера, у RX своя учётка с правами на свой виртуальный
хост, веб-интерфейс 15672 закрыт от сети пользователей. Если нужен TLS до брокера: брокер на время
перехода слушает 5672 и 5671, клиенты переводятся по одному, обычный порт закрывают, когда в списке
соединений сутки нет `ssl = false`. Сертификат: `host_audit.py --only tls --tls mq.example.com:5671`.
После продления сертификата брокер держит старый в кэше. Разбор:
https://drxinfra.ru/blog/rx-rabbitmq-tls-no-downtime.html

## Elasticsearch (q3, q4)

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://es.example.com:9200/   # без учётных данных
```

200 без учётных данных значит, что безопасность выключена: любой, кто дошёл до порта, читает и удаляет
индексы. Норма: 401, доступ по учётке (в RX это `ELASTICSEARCH_LOGIN` и `ELASTICSEARCH_PASSWORD`),
порт 9200 открыт только серверам RX. Подробно в скиле `rx-elasticsearch`.

## Прокси и веб-клиент (q6, q17, q21)

```bash
curl -sI http://rx.example.com/ | head -3                                  # переадресация на https
curl -sI https://rx.example.com/ | grep -iE 'strict-transport|x-frame-options|content-security|x-content-type|^server:'
openssl s_client -connect rx.example.com:443 -servername rx.example.com -showcerts </dev/null 2>/dev/null | grep -c 'BEGIN CERTIFICATE'
```

Норма: HTTP переадресует на HTTPS, есть HSTS, запрет встраивания и nosniff, заголовок `Server` без версии,
сервер отдаёт больше одного сертификата (сертификат сайта и промежуточные). Проверку сертификата целиком
делает `host_audit.py --tls rx.example.com:443`. Прокси в ДМЗ видит изнутри только веб-серверы RX,
не в домене, журналы уходят наружу. Разбор:
https://drxinfra.ru/blog/rx-reverse-proxy-nginx-haproxy-envoy.html

## Keycloak и вход (q7, q21)

- Консоль администрирования из внешней сети закрыта: с внешнего адреса
  `curl -s -o /dev/null -w '%{http_code}\n' https://sso.example.com/admin/` не отдаёт 200.
- Каталог подключён по LDAPS (порт 636), сертификат контроллера проверяется:
  `host_audit.py --only tls --tls dc.example.com:636`.
- Администраторы Keycloak перечислены поимённо, у ушедших доступ закрыт.

Схема входа: https://drxinfra.ru/blog/rx-sso-kerberos-ldaps-2fa.html

## Контейнеры Docker (сверх вопросов, требование изоляции)

`host_audit.py --only containers` показывает привилегированные контейнеры, сокет Docker внутри, лишние
привилегии и образы. Дополнительно:

```bash
cat /sys/kernel/security/lsm                       # активные модули безопасности ядра
docker info --format '{{json .SecurityOptions}}'   # seccomp, apparmor или selinux, userns, rootless
```

Норма: в прикладных контейнерах нет `--privileged`, сокета Docker и `SYS_ADMIN`; профиль seccomp
по умолчанию не выключен. Без поломок обычно включается `no-new-privileges` для демона, но это правка
`/etc/docker/daemon.json` и перезапуск Docker: только с согласия и сначала на тестовом стенде.
На РЕД ОС SELinux ограничивает контейнеры только с `selinux-enabled` и метками томов, на Astra Linux
главный механизм МКЦ, отключать его ради контейнеров нельзя. Разбор:
https://drxinfra.ru/blog/rx-container-isolation-apparmor-selinux.html

## Kubernetes (если RX в кластере)

```bash
kubectl get ns rx --show-labels                     # уровень Pod Security на пространстве имён
kubectl label --dry-run=server --overwrite ns rx pod-security.kubernetes.io/enforce=restricted   # что нарушит restricted, ничего не меняет
kubectl get networkpolicy -n rx                     # есть ли запрет по умолчанию и правило для DNS
kubectl auth can-i --list --as=system:serviceaccount:rx:default -n rx
kubectl get pods -A -o jsonpath='{range .items[*]}{range .spec.containers[*]}{.image}{"\n"}{end}{end}' | sort -u
```

Подставьте своё пространство имён вместо `rx`. Норма: политики сначала в `warn` и `audit`, сетевые
политики поддерживает сетевой плагин (с flannel без дополнений они ничего не фильтруют), сервисным
учёткам RX токен не монтируется, `pods/exec` и чтение секретов у короткого списка людей, образы из своего
реестра. Разбор: https://drxinfra.ru/blog/rx-kubernetes-security-policies.html

## Цепочка поставок (q16)

```bash
grep -rn "trusted=yes" /etc/apt/sources.list /etc/apt/sources.list.d/ 2>/dev/null
grep -rn "gpgcheck=0" /etc/yum.repos.d/ 2>/dev/null
docker ps --format '{{.Image}}' | sort -u | xargs -n1 docker inspect --format '{{index .RepoDigests 0}}'
```

Если установлен сканер (`trivy image --severity HIGH,CRITICAL <образ>`), отчёт разбирают по слоям:
базовая ОС образа, среда выполнения, библиотеки приложения, компоненты рядом с RX. Находки в продукте
вендора передают вендору, а не публикуют. Разбор: https://drxinfra.ru/blog/rx-supply-chain-images-packages.html
и https://drxinfra.ru/blog/container-image-digests.html

## ИИ-помощник к RX (если подключают)

- Сервис интеграции доступен только из корпоративной сети или через VPN; если он опубликован наружу
  и включён вход по паролю, его можно перебирать.
- Способ входа MCP-сервера: лучше OIDC с отдельным клиентом, хуже пароль в файле на компьютере.
- Запись в RX выключена, пока не нужна; подтверждение каждого действия в клиенте включено.
- Решено, какие документы нельзя отправлять внешнему провайдеру модели.

Разбор: https://drxinfra.ru/blog/rx-ai-assistant-security.html. Подключение помощника: скил `rx-connect`.

## Подрядчик с доступом к контуру

Если серверы сопровождает подрядчик, те же вопросы закрываются договором: именной доступ, одна точка входа,
изменения через репозиторий, секреты в хранилище, происхождение образов, бэкап с протоколом восстановления,
ежемесячный отчёт. Методика ФСТЭК об оценке уровня зрелости позволяет заказчику записать требования
к зрелости подрядчика в договор. Разбор: https://drxinfra.ru/blog/fstek-maturity-contractor.html
