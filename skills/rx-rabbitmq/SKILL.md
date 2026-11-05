---
name: rx-rabbitmq
description: >-
  Диагностика RabbitMQ под Directum RX: очереди и потребители, неподтверждённые сообщения, тревоги по
  памяти и диску, лимиты, утечка временных очередей, кластер и кворумные очереди, TLS, ошибки клиента
  RabbitMQ в логах RX. Использовать, когда «задания не приходят», «уведомления опаздывают», «новые
  документы не ищутся», растёт очередь, consumers 0, копятся unacked, тысячи очередей с GUID в имени,
  горит alarm (memory resource limit alarm, disk resource limit alarm), в логах BrokerUnreachableException,
  «RabbitMQ connection ... recovery failed», «Error occurred while publishing», «RabbitMQ channel is failed
  to close due to timeout», «Already closed: The AMQP operation was interrupted», «RabbitMQ is not
  available. Possible problems: TLS configuration», ACCESS_REFUSED, PRECONDITION_FAILED, delivery
  acknowledgement timed out, noproc у quorum queue. Elasticsearch и базу не разбирает: для поиска скил
  rx-elasticsearch, для медленного обработчика из-за СУБД скил rx-postgresql.
license: Apache-2.0
compatibility: >-
  Directum RX 4.x–26.x на Linux, Windows и в Kubernetes; RabbitMQ 3.8–4.x (скрипт проверен на 3.13.7 и
  4.4.0). Нужен python3 3.6+ и API управления (порт 15672, учётка с тегом monitoring) или rabbitmqctl на
  сервере брокера.
metadata:
  author: drxinfra
  version: "0.1.0"
---

# RabbitMQ под Directum RX

Скил разбирает брокер, через который общаются сервисы RX: очереди и их потребителей, тревоги и лимиты,
кластер, TLS и ошибки клиента RabbitMQ в логах RX. Причину медленного обработчика в базе ищет скил
`rx-postgresql`, поиск и индексы Elasticsearch разбирает скил `rx-elasticsearch`.

## Правила

- Сначала только смотрим. Команду, которая что-то меняет (перезапуск, удаление, изменение настроек, запись в базу, очистку очереди), сначала покажите пользователю, объясните, что она сделает и как это откатить, и выполняйте только после явного согласия.
- Пароли, токены и ключи не выводите. Конфиги и вывод, где они могут быть, читайте через маскировщик: `python3 ${CLAUDE_SKILL_DIR}/scripts/mask_secrets.py ФАЙЛ` или `КОМАНДА 2>&1 | python3 ${CLAUDE_SKILL_DIR}/scripts/mask_secrets.py`.
- Если подключён rxmcp (есть инструменты `rx_help_search` и `rx_help_topic`), поведение Directum RX сверяйте со справкой той версии, что стоит у пользователя, и называйте статью.
- Чего не знаете точно, так и говорите и предлагайте проверить на тестовом стенде. Не придумывайте параметры, пути и имена служб.
- `${CLAUDE_SKILL_DIR}` — папка этого скила. Если агент не подставляет переменную, скрипты лежат в `scripts/` рядом с этим файлом.

Для этого скила дополнительно:

- Очередь не очищать и не удалять, брокер не перезапускать первым действием. В очереди лежат задания
  пользователей и результаты интеграций, а сервис, который не подключился из-за прав или сертификата,
  не подключится и после перезапуска брокера.
- Пороги памяти и диска не поднимать, чтобы погасить тревогу. Тревога значит, что ресурса правда мало.
  Порог меняют, только когда он заведомо неверный (пример в `references/alarms-limits.md`).

## Что известно об RX и брокере

По справке Directum RX 25.2 (статьи в скобках):

- Сервисы RX обмениваются сообщениями через RabbitMQ: веб-сервер публикует, Workflow, WorkflowBlockService,
  Worker, PreviewService, IndexingService, JobScheduler, DelayedOperationsService и другие забирают
  (`ar_server_components.htm`, `ar_servis_otlozhennykh_operatcii.htm`).
- Подключение задаёт `QUEUE_CONNECTION_STRING` в секции `common_config` файла config.yml: hostName, port
  (5672), virtualhost (по умолчанию `/`), userName, password, exchange. Имя exchange входит в имена
  очередей, у разных систем на одном брокере оно должно отличаться. Узлы кластера перечисляют в
  `QUEUE_ADDITIONAL_HOSTS` (`admin_minimalnye_nastroiki_linux.htm`, `admin_obshchie_nastroiki_linux.htm`).
- Подтверждённые имена очередей: `directumrx_<exchange>_indexing_service` и `..._initial_bulk` для
  первоначального индексирования (`admin_indeksirovanie_dokumentov_linux.htm`). Других имён справка не
  перечисляет. Очередь с GUID в имени может принадлежать фоновому процессу (GUID совпадает с JobId в
  таблице Sungero_Core_Job) или асинхронному обработчику (`monitoring_sostoyanie_rabbitmq.htm`).
- Чек-лист вендора: в каждой очереди меньше 50 тысяч сообщений, в таблице отложенных операций
  sungero_system_delayedoperation тоже (`monitoring_otsutstviye_peregruzhennykh_ocheredey_rabbitmq.htm`).

## Порядок работы

1. **Симптом.** Уточните, что стоит: задачи и задания, уведомления, предпросмотр, поиск новых документов,
   фоновые процессы; с какого времени; один тип операций или всё сразу. Обычно страдает один тип
   операций, и это указывает на одну очередь. Встало всё сразу: начинайте с шага 4 (тревоги брокера).
   Не ищутся только новые документы: шаги 3 и 5 для очереди индексирования, затем скил `rx-elasticsearch`.

2. **Куда подключается RX.** Найдите строку подключения, пароль спрячет маскировщик:
   ```bash
   grep -n -E 'QUEUE_CONNECTION_STRING|QUEUE_ADDITIONAL_HOSTS' <папка Directum Launcher>/etc/config.yml \
     | python3 ${CLAUDE_SKILL_DIR}/scripts/mask_secrets.py
   ```
   Запишите адрес, порт, vhost и exchange. В Kubernetes те же параметры лежат в настройках чарта
   (где именно, смотрите у себя). Брокер в контейнере: `docker ps --format '{{.Names}} {{.Image}}' | grep -i rabbit`.

3. **Снимок брокера.** Пароль агенту не нужен. Попросите пользователя один раз записать учётку только
   на чтение в файл netrc (`machine <брокер> login <учётка> password <пароль>`, права 600), например
   `~/.config/rxops/rabbitmq.netrc`, и запускайте:
   ```bash
   RABBITMQ_URL=http://<брокер>:15672 python3 ${CLAUDE_SKILL_DIR}/scripts/mq_check.py --netrc ~/.config/rxops/rabbitmq.netrc
   ```
   Без API управления: на сервере брокера `python3 ${CLAUDE_SKILL_DIR}/scripts/mq_check.py --ctl`, для
   контейнера `--ctl --ctl-prefix "docker exec <контейнер>"`. Если есть только вывод
   `rabbitmqctl list_queues -p <vhost> name messages messages_ready messages_unacknowledged consumers`,
   передайте его в `mq_check.py --stdin --vhost <vhost>`. Норма: «Итог: OK», код 0. Строки CRIT и WARN
   ведут в шаги ниже по имени проверки. Если что-то копится, сделайте второй замер:
   `python3 ${CLAUDE_SKILL_DIR}/scripts/mq_check.py --interval 60`. Учётку только на чтение и все ключи описывает `references/mq-check.md`.

4. **Тревоги, память, диск, дескрипторы** (`alarms`, `memory`, `disk`, `fd`). При тревоге брокер
   блокирует всех, кто публикует: RX не может отправить ни одного сообщения.
   ```bash
   rabbitmq-diagnostics check_local_alarms
   rabbitmq-diagnostics status | grep -i -A3 -E 'alarm|memory|disk'
   rabbitmq-diagnostics memory_breakdown
   df -h /var/lib/rabbitmq
   ```
   Норма: тревог нет, память ниже 80 % порога, свободного места больше двух порогов `disk_free_limit`,
   сам порог не меньше 2 ГБ. Отклонение: найти, что съело ресурс (очередь без обработчика, тысячи
   очередей, журналы брокера), освободить место. Порог в контейнере и том меньше порога разобраны в
   `references/alarms-limits.md`.

5. **Очередь с сообщениями без потребителей** (`no_consumers`, CRIT). Сообщения никто не забирает.
   - Чья очередь: `mq_check.py` пишет подсказку. `indexing_service` читает IndexingService. Для остальных
     смотрите, какой сервис читает однотипные очереди (имя соединения клиента вида
     `HostName.PreviewService.Host.PublishersConnection`, как в примере журнала из справки
     `admin_ustranenije_oshibok_v_log_failah_linux.htm`). Не уверены: так и скажите.
   - Жив ли сервис: `docker ps -a | grep -i <сервис>`, в Kubernetes `kubectl get pods -A | grep -i <сервис>`;
     в его логе ищите `recovery failed`, `BrokerUnreachableException`, `ACCESS_REFUSED`.
   - IndexingService запущен, а очередь стоит: после 25 ошибок подряд он перестаёт забирать сообщения,
     проверка `INDEXING_SERVICE` становится degraded (`admin_unikalnyye_proverki_dlya_servisov_linux.htm`).
     Причина почти всегда в Elasticsearch: скил `rx-elasticsearch`.
   - Брокер долго был недоступен: по справке сервисы могли отключиться совсем, и их перезапускают
     (`admin_ustranenije_oshibok_v_log_failah_linux.htm`).
   - Перезапуск сервиса это изменение: покажите команду, дождитесь согласия, после него `--interval 60`
     должен показать потребителей больше нуля и убывание очереди.

6. **Потребители есть, очередь растёт или стоят неподтверждённые** (`backlog`, `growth`, `unacked`).
   - Обработчик не успевает: каждое сообщение упирается в базу (долгий запрос, блокировка) или во
     внешнюю систему. Активные запросы и блокировки смотрит скил `rx-postgresql`.
   - Одна и та же ошибка в логе сервиса на каждое сообщение: «ядовитое» сообщение возвращается и
     держит очередь. Посмотреть его можно в панели управления: очередь, Get messages, режим
     `Ack Mode: Nack message requeue true`. Это уже не чистое чтение (сообщение вернётся в очередь с
     признаком redelivered), делайте с согласия и передайте текст ошибки разработчикам или вендору.
   - В логе брокера `delivery acknowledgement on channel N timed out`: обработчик держит сообщение
     дольше `consumer_timeout` (по умолчанию 30 минут), брокер закрывает канал, и всё повторяется.
     При двух замерах `mq_check.py` показывает таймаут у потребителей. Разбор в `references/alarms-limits.md`.
   - Отложенные операции: чек-лист вендора требует меньше 50 тысяч строк в sungero_system_delayedoperation,
     посчитать их поможет скил `rx-postgresql` (только SELECT).

7. **Тысячи очередей с GUID или хешем** (`temp_queues`). Скрипт группирует очереди по форме имени.
   Серия однотипных очередей, где живая одна, похожа на очереди экземпляров, которые остались после
   перезапусков сервисов (так их описывает калькулятор drxinfra). Одиночная очередь с GUID может быть
   очередью фонового процесса, и если в ней копятся сообщения без потребителя, проверьте Worker, как в
   шаге 5. Прежде чем что-то предлагать, проверьте шаблон на живом списке:
   `mq_check.py --vhost <vhost> --match '<регулярное выражение>'`. Политику с `expires` предлагайте
   только как изменение с согласия, по `references/temp-queues-policy.md`: там же риски и откат.

8. **Соединения** (`connections`, `users`, `tls`). Шторм подключений при двух замерах значит, что
   клиент переподключается по кругу: неверный пароль, сертификат, балансировщик с таймаутом простоя,
   падающий сервис. Кто именно, видно в журнале брокера (команды в `references/client-errors.md`).
   Заблокированные соединения (blocked) означают тревогу из шага 4. Пользователь guest и переход на TLS
   разобраны в `references/tls.md` и `references/alarms-limits.md`.

9. **Кластер и кворумные очереди** (`nodes`, `quorum`, `queue_state`, `policies`).
   ```bash
   rabbitmqctl cluster_status
   rabbitmq-queues quorum_status --vhost <vhost> <очередь>
   rabbitmq-diagnostics check_if_node_is_quorum_critical
   ```
   Норма: все узлы в строю, разделений нет, у кворумных очередей большинство копий и лидер. Справка
   RX 25.2 строит кластер на RabbitMQ 3.13.7 с политикой зеркалирования `ha-all`
   (`admin_cluster_rmq.htm`); RabbitMQ 4.x такую политику не принимает. Перед обновлением брокера и при
   `noproc` или падении WAL читайте `references/cluster-quorum.md`.

10. **Ошибки в логах RX.** Текст ошибки найдите в таблице `references/client-errors.md`: там значение,
    проверка и следующий шаг для каждой ошибки клиента и брокера из описания скила.

11. **После исправления.** Сохраните замер до работ (`mq_check.py --json > before.json`) и сравните после
    (`mq_check.py --compare before.json`). Норма: потребители на месте, очереди убывают, тревог нет. На
    стороне RX: в логах сервисов новые записи о подключении, проверка `QUEUE` в статус-файлах healthy
    (degraded дольше 2000 мс, unhealthy после ошибки или дольше 5000 мс, `admin_general_health_checks_linux.htm`).

## Что сказать пользователю

Коротко, по пунктам:

- **Что нашли.** Каждая проблема одной строкой с цифрами: очередь, сообщений, потребителей, рост в
  минуту, тревога и порог.
- **Насколько срочно.** Авария: тревога брокера, очередь с сообщениями без потребителей, кворумная
  очередь без большинства, узел вне кластера. Скоро: память и диск к порогам, дескрипторы, рост очереди,
  шторм подключений. В план: временные очереди, guest, порог диска по умолчанию, переход на TLS.
- **Что сделать.** Команды по порядку. Каждую, что меняет систему, пометьте, объясните и дайте откат.
- **Чем подтверждено.** Строки `mq_check.py`, строки журналов с временем, статьи справки и drxinfra.ru.
  Догадки, например чей сервис у очереди, отметьте как догадки.

## Справочник

- `references/mq-check.md`: запуск `mq_check.py`, учётка только на чтение, пороги, коды возврата, задержка
  статистики API.
- `references/client-errors.md`: ошибки в логах RX и брокера, что значат и что проверить.
- `references/alarms-limits.md`: пороги памяти и диска, брокер в контейнере, дескрипторы, consumer_timeout,
  guest.
- `references/temp-queues-policy.md`: временные очереди, проверка шаблона, политика expires, риски, откат.
- `references/cluster-quorum.md`: кластер, кворумные очереди, зеркалирование, noproc, WAL, переход на 4.x.
- `references/tls.md`: TLS без простоя, продление сертификата, ошибки подключения по TLS.
- Статьи drxinfra.ru: [очередь растёт, задания не двигаются](https://drxinfra.ru/blog/rabbitmq-queue-growth.html),
  [RabbitMQ для RX по TLS](https://drxinfra.ru/blog/rx-rabbitmq-tls-no-downtime.html),
  [мониторинг RX и алерты](https://drxinfra.ru/blog/rx-monitoring-alerts.html),
  [калькулятор настроек RabbitMQ](https://drxinfra.ru/rabbitmq-config.html),
  [справочник ошибок](https://drxinfra.ru/errors/).
