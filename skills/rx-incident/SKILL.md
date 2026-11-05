---
name: rx-incident
description: >-
  Первый разбор аварии Directum RX за 10–15 минут: спокойный порядок от масштаба и последних изменений
  через ресурсы сервера, контейнеры, зависимости и проверки работоспособности к гипотезе с фактами
  и передаче в скил нужного компонента. Снимок сервера только на чтение делает collect.sh. Использовать,
  когда «RX не открывается», «всё тормозит», «у всех ошибка», «сервисы падают», «после обновления
  не работает», сработал алерт, на входе 502 Bad Gateway или 504, в журнале No space left on device,
  Too many open files, upstream timed out, OOMKilled, exit code 137, CrashLoopBackOff, статус unhealthy
  или degraded; когда нужно вести заметки по инциденту и собрать материалы для поддержки Directum.
  Компоненты скил не чинит: база в rx-postgresql, брокер в rx-rabbitmq, поиск в rx-elasticsearch,
  разбор записей журналов и справочник ошибок в rx-logs.
license: Apache-2.0
compatibility: >-
  Linux-серверы Directum RX 4.x–26.x: Docker или Podman, Kubernetes, установка на хост. Нужны bash 4
  и coreutils, python3 3.6+ для маскировки вывода; journalctl, docker, kubectl, ss используются,
  если есть. Полный снимок требует sudo.
metadata:
  author: drxinfra
  version: "0.1.0"
---

# Первый разбор инцидента Directum RX

Скил ведёт первые 10–15 минут аварии: собрать факты, не потеряв улик, сузить круг до компонента
и передать работу дальше с гипотезой. Он ничего не чинит сам. Починка в скилах компонентов, разбор
записей журналов в `rx-logs`.

## Правила

- Сначала только смотрим. Команду, которая что-то меняет (перезапуск, удаление, изменение настроек, запись в базу, очистку очереди), сначала покажите пользователю, объясните, что она сделает и как это откатить, и выполняйте только после явного согласия.
- Пароли, токены и ключи не выводите. Конфиги и вывод, где они могут быть, читайте через маскировщик: `python3 ${CLAUDE_SKILL_DIR}/scripts/mask_secrets.py ФАЙЛ` или `КОМАНДА 2>&1 | python3 ${CLAUDE_SKILL_DIR}/scripts/mask_secrets.py`.
- Если подключён rxmcp (есть инструменты `rx_help_search` и `rx_help_topic`), поведение Directum RX сверяйте со справкой той версии, что стоит у пользователя, и называйте статью.
- Чего не знаете точно, так и говорите и предлагайте проверить на тестовом стенде. Не придумывайте параметры, пути и имена служб.
- `${CLAUDE_SKILL_DIR}` — папка этого скила. Если агент не подставляет переменную, скрипты лежат в `scripts/` рядом с этим файлом.

Ещё два правила этого скила:

- До сбора фактов не перезагружайте сервер и не перезапускайте всё подряд: пропадут журнал ядра
  в памяти, зависшие процессы и журнал прежнего экземпляра контейнера. Если людей надо срочно вернуть
  в работу, сначала снимите `collect.sh` и журналы, потом предлагайте перезапуск.
- Ведите заметки с первой минуты: время каждого факта и действия. Шаблон в `references/incident-notes.md`.

## Порядок работы

### 1. Масштаб и что менялось (2–3 минуты)

Спросите и запишите:

- кто затронут: все, часть (какие подразделения, сеть, VPN, терминальный сервер) или один человек;
- что не работает: вход, открытие документов, предпросмотр, поиск, задания и уведомления, интеграции;
- с какого времени, по первой жалобе или алерту;
- что менялось за сутки: обновление RX или ОС, перезапуск, сертификат, пароль служебной учётки,
  правила сети или WAF, перенос диска, массовая загрузка или рассылка.

| Симптом | Где искать первым |
|---|---|
| не открывается у всех, 502 или 504 | прокси, веб-сервер RX, контейнеры, место на диске |
| не входит, ошибка входа или переадресации | Keycloak или AD, время на серверах, сертификаты |
| поиск не находит новое | Elasticsearch, сервис индексирования, очередь индексации |
| задания и уведомления опаздывают | RabbitMQ, сервисы Worker и Workflow, блокировки в базе |
| не открывается предпросмотр | PreviewService, PreviewStorage, хранилище |
| тормозит у всех | ресурсы, блокировки и долгие транзакции в базе, очереди |
| тормозит у части людей | рабочие места, тонкий клиент, сеть, размер объекта |

Одна ошибка у одного человека это не авария: сразу скил `rx-logs`.

### 2. Снимок сервера

```bash
sudo bash ${CLAUDE_SKILL_DIR}/scripts/collect.sh --since "2 hours ago" --logs <LOGS_PATH>
```

Скрипт только читает и через минуту печатает: нагрузку и перезагрузки, синхронизацию времени, место
и inode, память, крупные процессы и процессы в состоянии D, убийства по памяти в журнале ядра, упавшие
службы, ошибки journald, контейнеры (перезапуски, OOMKilled, код выхода, память к лимиту), поды
и события Kubernetes, порты, статус-файлы проверок RX. В конце раздел «Итог» со списком отклонений.
Код 0: отклонений нет, 1: есть, 2: ошибка в ключах. Вывод проходит через маскировщик. Запустите его
на каждом сервере контура: приложений, базы, брокера, поиска. `LOGS_PATH` из `config.yml` Directum
Launcher, без `--logs` статус-файлы не читаются. Без sudo не видно журнала ядра, части процессов
и контейнеров.

### 3. Ресурсы сервера

| Что видно в снимке | Куда копать |
|---|---|
| раздел или inode заняты на 100 % | место: [No space left on device](https://drxinfra.ru/errors/linux-no-space-left-on-device.html), у базы [pg_wal](https://drxinfra.ru/errors/postgresql-no-space-left-pg-wal.html) |
| мало `available`, swap растёт, OOM в журнале ядра | память: [Out of memory](https://drxinfra.ru/errors/linux-out-of-memory-killed-process.html), [OOMKilled, 137](https://drxinfra.ru/errors/container-oomkilled-exit-code-137.html) |
| нагрузка выше числа ядер, процессор простаивает, процессы в D | диск или сетевой диск, `nfs` в `wchan`: [nfs](https://drxinfra.ru/errors/nfs-server-not-responding.html) |
| высокий `st` в vmstat | процессор забирает гипервизор, вопрос к виртуализации |
| часы не синхронизированы | вход Kerberos и токены: [Clock skew](https://drxinfra.ru/errors/kerberos-clock-skew-too-great.html) |
| сервер недавно перезагружался | `last -x`, `journalctl -b -1 -p err` про прошлую загрузку |
| Too many open files в журналах | [лимит файлов](https://drxinfra.ru/errors/linux-too-many-open-files.html) |

Место, которое `df` видит занятым, а `du` не находит, держат удалённые, но открытые файлы:
`sudo lsof -nP +L1 | head`. Не запускайте `du -sh /*` и `find /` на перегруженном или сетевом диске.

Память сервисов .NET: процессы `dotnet` держат гигабайты по устройству сборщика мусора. Без лимита
контейнера сервис считает своей всю память сервера. Утечку отличают двумя замерами с разницей в сутки
(статья [про память .NET](https://drxinfra.ru/blog/rx-dotnet-memory-gc.html)). По справке лимит памяти
контейнера сервиса RX равен порогу `value` проверки `PROCESS_MEMORY`, умноженному на 1,5, swap в контейнере
выключен, а при превышении лимита контейнер перезапускается (`references/health-checks.md`).

### 4. Контейнеры и поды

```bash
docker ps -a --format 'table {{.Names}}\t{{.Status}}'      # Restarting, Exited, Up 2 minutes
docker logs --tail 200 --timestamps <контейнер> 2>&1 | python3 ${CLAUDE_SKILL_DIR}/scripts/mask_secrets.py
kubectl -n <пространство> get pods -o wide
kubectl -n <пространство> describe pod <под>                # Last State, Reason, Exit Code, Events
kubectl -n <пространство> logs <под> --previous --tail 200  # журнал до последнего падения
```

Контейнер «Up 2 minutes» на сервере с недельным аптаймом перезапускается. Код 137 значит, что его убили,
чаще всего по памяти. Разборы: [CrashLoopBackOff](https://drxinfra.ru/errors/kubernetes-crashloopbackoff.html),
[ImagePullBackOff](https://drxinfra.ru/errors/kubernetes-imagepullbackoff.html),
[узел NotReady](https://drxinfra.ru/errors/kubernetes-node-not-ready.html),
[проба не подключается](https://drxinfra.ru/errors/kubernetes-probe-failed-connection-refused.html),
[etcd превысил квоту](https://drxinfra.ru/errors/etcd-mvcc-database-space-exceeded.html).
Поды RX в Kubernetes помечены меткой `app=rx` (справка, сбор журналов Vector в Kubernetes).

### 5. Зависимости

Быстрые проверки только на чтение. При отклонении передавайте в скил компонента.

| Зависимость | Проверка | Дальше |
|---|---|---|
| PostgreSQL | `pg_isready -h <хост> -p 5432`; из папки Directum Launcher `./do.sh db check` (справка: проверка подключения по `CONNECTION_STRING`) | `rx-postgresql` |
| RabbitMQ | `rabbitmq-diagnostics -q check_running`, `rabbitmq-diagnostics -q alarms`, `rabbitmqctl list_queues name messages consumers` (в контейнере через `docker exec`) | `rx-rabbitmq` |
| Elasticsearch | `curl -s -u <пользователь> --cacert <ca.crt> 'https://<хост>:9200/_cluster/health?pretty'` (пароль curl спросит сам) | `rx-elasticsearch` |
| Хранилище S3 | проверка `S3_SERVICE_ACCESS` в статус-файле сервиса хранилищ; у MinIO `curl -s -o /dev/null -w '%{http_code}\n' http://<хост>:9000/minio/health/live`, у кластера `/minio/health/cluster` | [MinIO заполнен](https://drxinfra.ru/errors/minio-storage-full.html), [нет кворума](https://drxinfra.ru/errors/minio-insufficient-drives-online.html) |
| Файловое хранилище, NFS | `df -hT`, в журнале ядра `nfs: server not responding` | [nfs](https://drxinfra.ru/errors/nfs-server-not-responding.html) |
| Вход: Keycloak | `curl -s https://<keycloak>/realms/<realm>/.well-known/openid-configuration \| head -c 300` (в старых версиях путь с `/auth`) | [RX не получает настройки OIDC](https://drxinfra.ru/errors/rx-oidc-unable-to-obtain-configuration.html) |
| Обратный прокси | запрос через прокси и в обход него, журнал ошибок прокси | ниже |
| Сертификаты | `echo \| openssl s_client -connect <хост>:443 -servername <хост> 2>/dev/null \| openssl x509 -noout -subject -enddate` | [x509](https://drxinfra.ru/errors/x509-certificate-signed-by-unknown-authority.html) |

Прокси или система: повторите запрос с машины прокси прямо на сервер RX.

```bash
curl -sk -o /dev/null -w '%{http_code} %{time_total}s\n' https://<адрес RX>/<путь>
curl -s -o /dev/null -w '%{http_code} %{time_total}s\n' -H 'Host: <адрес RX>' http://<сервер RX>:<порт>/<путь>
```

Напрямую работает, через прокси нет: проблема в прокси. В журнале nginx ищите
[upstream timed out](https://drxinfra.ru/errors/nginx-upstream-timed-out.html) (504),
[connect() failed](https://drxinfra.ru/errors/nginx-connect-failed-connection-refused.html) (502),
[413](https://drxinfra.ru/errors/nginx-413-request-entity-too-large.html). У HAProxy код завершения
`sH` значит, что сервер не прислал заголовки вовремя, `SC` отказ в соединении, `cD` таймаут клиента
(часто WebSocket без `timeout tunnel`). Подробно в статье
[RX за обратным прокси](https://drxinfra.ru/blog/rx-reverse-proxy-nginx-haproxy-envoy.html).

### 6. Сервисы RX и проверки работоспособности

- Из папки Directum Launcher `./do.sh all check` проверяет работоспособность всех сервисов,
  `./do.sh <сервис> check` одного (справка, «Команды скриптов развертывания»). Статусы проверок
  видны и в Directum Launcher в режиме «Состояние».
- Статус-файлы `<LOGS_PATH>/status/<компьютер>.<сервис>.status` обновляются каждые 10 секунд: общий
  статус `healthy`, `degraded` или `unhealthy` и статус каждой проверки. Файл, который не обновляется,
  значит, что сервис не работает. `collect.sh --logs` читает их сам.
- Адреса проверок из примера настройки HAProxy в справке: `/client/api/health`, `/client/health`,
  `/integration/health`, `/storage/health`, `/preview/health`, `/log/health`, `/keyderivation/health`.
  Вместо `Client` бывает `drxweb`, проверьте по своей публикации.

```bash
curl -sk -o /dev/null -w '%{http_code} %{time_total}s\n' https://<адрес RX>/client/api/health
```

Имя проверки, которая не в норме, указывает на зависимость: `DATABASE` база, `QUEUE` брокер,
`ELASTIC_SEARCH` и `INDEXING_SERVICE` поиск, `S3_SERVICE_ACCESS` хранилище, `CENTRIFUGO` уведомления
веб-клиента, `QUEUE_MESSAGE_PROCESSING` зависшие фоновые процессы и обработчики, `PROCESS_MEMORY`
и `THREAD_POOL` сам сервис. Пороги по умолчанию и остальные проверки в `references/health-checks.md`.

### 7. Журналы

Окно возьмите от начала инцидента. Дальше скил `rx-logs`: его `match_errors.py` с ключом `--sort time`
покажет, какая известная ошибка появилась первой, это часто причина, остальное следствия. Без него
хотя бы журнал ядра, `journalctl -p err` и журналы упавших контейнеров за то же время.

### 8. Отчёт rxdoctor, если он есть

Бесплатная утилита drxinfra [rxdoctor](https://drxinfra.ru/rxdoctor.html) за полминуты проверяет
PostgreSQL, RabbitMQ, Elasticsearch, сервер и контейнеры и ничего не меняет. Если у администратора
есть отчёт или он готов запустить утилиту, начните с раздела «Сначала главное»: красные пункты
проблемы, жёлтые внимание, компонент без ответа стоит красной строкой первым. Коды и что делать
с каждым в `references/rxdoctor.md`. Скачивать и запускать программу на сервере решает администратор.

### 9. Гипотеза и передача

Сформулируйте одной фразой: «вероятная причина X, потому что факты A и B; не проверено C». Дальше:

| Факты указывают на | Скил |
|---|---|
| базу: соединения, блокировки, долгие транзакции, место под WAL | `rx-postgresql` |
| брокер: тревоги памяти или диска, очереди без потребителей, рост очередей | `rx-rabbitmq` |
| поиск: red или yellow, read-only индексы, куча, индексирование стоит | `rx-elasticsearch` |
| конкретную ошибку в журналах сервисов, трассу действия | `rx-logs` |
| сервер или контейнеры: место, память, процессор, перезапуски | шаги 3 и 4 этого скила, исправление с согласия пользователя |

### 10. Если сломалось после изменения

Обновление RX меняет структуру базы, поэтому старые сервисы на преобразованной базе не запустить:
откат идёт только через восстановление базы из копии, сделанной до обновления, и теряет всё,
что пользователи успели внести после (статья [обновление с откатом](https://drxinfra.ru/blog/rx-update-with-rollback.html)).
Решение об откате принимает владелец системы по заранее записанным условиям. Сертификат, пароль
или настройку возвращают на прежнее значение, если оно сохранено; каждое такое действие с согласия
и записью в заметках.

### 11. Материалы для поддержки Directum

Справка просит время ошибки и журналы за весь день. Приложите: время начала и симптомы, что менялось,
журналы сервисов за день со всех серверов и папку `remote`, вывод `collect.sh`, статус-файлы,
версии RX и прикладной разработки, что уже проверено и исключено. Как отобрать и проверить журналы
перед отправкой, в скиле `rx-logs`.

## Что сказать пользователю

Коротко, в таком порядке:

1. Статус: авария идёт или снята, с какого времени, кого затрагивает.
2. Гипотеза: вероятная причина одной фразой и факты под ней (команда, файл, строка, время).
3. Что сделано и что не трогали.
4. Следующий шаг: какое действие нужно, что оно изменит, как откатить, кто решает.
5. Что проверить после: какой признак покажет, что стало лучше.

После инцидента запишите, какое правило мониторинга сработало бы раньше пользователей
(статья [о мониторинге RX](https://drxinfra.ru/blog/rx-monitoring-alerts.html)).

## Справочник

- `references/incident-notes.md`: шаблон заметок: хронология, факты, гипотезы, действия, откат.
- `references/health-checks.md`: проверки работоспособности RX, статусы, пороги по умолчанию, адреса.
- `references/rxdoctor.md`: как читать отчёт rxdoctor и коды пунктов.
- Статьи: [разбор инцидента на Linux за десять команд](https://drxinfra.ru/blog/linux-incident-ten-commands.html),
  [RX тормозит](https://drxinfra.ru/blog/rx-performance-triage.html),
  [перцентили p90, p95, p99](https://drxinfra.ru/blog/rx-percentiles-p90-p95-p99.html),
  [базовый стек Kubernetes под RX](https://drxinfra.ru/blog/rx-kubernetes-base-stack.html),
  [справочник ошибок](https://drxinfra.ru/errors/).
- Справка Directum RX 25.2: «Проверка работоспособности сервисов» (`admin_service_health_check_linux.htm`),
  «Общие проверки работоспособности» (`admin_general_health_checks_linux.htm`), «Статус-файлы и лог-файлы»
  (`admin_status_files_and_log_files_linux.htm`), «Настройка HAProxy» (`admin_nastroikahaproxy.htm`),
  «Команды скриптов развертывания» (`admin_komandy_skriptov_razvertyvaniia_linux.htm`),
  «Устранение ошибок с помощью лог-файлов» (`admin_ustranenije_oshibok_v_log_failah_linux.htm`).
