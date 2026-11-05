# Сверка работающей установки с расчётом

Команды только на чтение. Их цель: узнать, сколько ресурсов у каждой роли на самом деле и сколько из них занято, и сравнить с расчётом `sizing.py` для тех же входов. Вывод, где могут оказаться пароли (конфиги, переменные окружения контейнеров), пропускайте через маскировщик: `... 2>&1 | python3 ${CLAUDE_SKILL_DIR}/scripts/mask_secrets.py`.

## Пороги из справки RX

Справка Directum RX 25.2, чек-лист проверки состояния системы (статья `monitoring_analiz_proizvoditelnosti.htm`):

- пик считается продолжительным, если в течение часа держится загрузка процессора от 80%, общее потребление памяти от 90% или один сервис занимает 4 ГБ и больше; условие проверяют на каждом сервере с компонентами RX;
- на дисках виртуальных машин с сервисами свободно не меньше 30%; плановое удаление устаревших данных работает; журналы и временные файлы на отдельном разделе;
- массовые операции в среднем не дольше 3 секунд, в расчёт берут операции, выполненные больше 100 раз (дашборд Web Client. Performance решения «Мониторинг Directum RX»).

Для сервера MS SQL справка даёт ещё ориентиры (статья `admin_algoritm_analiza_shag1.htm`): пиковая загрузка процессора не больше 70%, очередь диска не больше 7 запросов для RAID 10 из четырёх дисков (меньше двух на диск), операционной системе остаётся не меньше 4 ГБ памяти. Эти же ориентиры разумны для сервера PostgreSQL.

## Сервер: процессор, память, диски

```bash
nproc; lscpu | grep -E '^(Model name|Socket|Core|Thread|CPU\(s\))'
free -g
df -hT -x tmpfs -x devtmpfs -x overlay
lsblk -d -o NAME,ROTA,SIZE,TYPE,MODEL      # ROTA=1: вращающийся диск
vmstat 5 3                                  # st: украденное хостом время, wa: ожидание диска
uptime
```

- `st` в `vmstat` стабильно выше 5–10%: хост виртуализации перегружен, виртуальных процессоров выдано больше, чем у него потоков. Добавлять vCPU в такой ВМ бесполезно, вопрос к владельцу хоста.
- `wa` высокий при свободном процессоре: упор в диск, объём тут ни при чём.
- История загрузки, если стоит sysstat: `sar -u`, `sar -r`, `sar -d` за нужный день (`sar -f /var/log/sa/saДД` или `/var/log/sysstat/saДД`, путь зависит от дистрибутива).

## Контейнеры и Kubernetes

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}'
docker inspect --format '{{.Name}} mem={{.HostConfig.Memory}} cpus={{.HostConfig.NanoCpus}}' $(docker ps -q)
```

`mem=0` и `cpus=0` значат, что лимита нет: контейнер может занять всю память сервера. Для Podman те же команды через `podman`.

```bash
kubectl top nodes
kubectl -n NAMESPACE top pods --sort-by=memory
kubectl -n NAMESPACE get resourcequota
kubectl describe nodes | grep -A8 'Allocated resources'
```

Сравнивайте с квотой из `sizing.py servers --place k8s`: запросы (requests) и лимиты суммарно по подам RX и брокера.

## PostgreSQL

```bash
psql -XAt -d ИМЯ_БАЗЫ -c "select name, setting, unit from pg_settings where name in ('shared_buffers','effective_cache_size','work_mem','maintenance_work_mem','max_connections','max_prepared_transactions','huge_pages','max_wal_size')"
psql -XAt -d ИМЯ_БАЗЫ -c "select pg_size_pretty(pg_database_size(current_database()))"
psql -XAt -d ИМЯ_БАЗЫ -c "select count(*), count(*) filter (where state = 'active') from pg_stat_activity"
psql -XAt -d ИМЯ_БАЗЫ -c "select round(100.0 * blks_hit / nullif(blks_hit + blks_read, 0), 2) as cache_hit, temp_files, pg_size_pretty(temp_bytes) from pg_stat_database where datname = current_database()"
```

- Подставьте фактические память, ядра, диск, число активных и размер базы в `sizing.py postgres` и сравните строки.
- Число соединений близко к `max_connections`: поднимать лимит или ставить PgBouncer.
- Доля попаданий в кэш считается с момента сброса статистики; ниже 99% на базе RX при заметном чтении с диска говорит о нехватке памяти. Растущие `temp_files`: мал `work_mem` для части запросов.
- `max_prepared_transactions` меньше `max_connections`: по справке RX они должны совпадать.
- Пароль для `psql` берите из `~/.pgpass` или переменной окружения, которую задал пользователь, не в командной строке.

## Хранилище тел документов

Объём и прирост за месяц: команды в `storage-growth.md`. Подставьте их в `sizing.py storage --have-gb ФАКТ` с текущим потоком и посмотрите, в каком году закончится том. Для срочности: свободное место / суточный прирост = сколько дней осталось.

## Elasticsearch

```bash
curl -s -u ЛОГИН "https://ES:9200/_cluster/health?filter_path=status,number_of_nodes,active_shards,unassigned_shards"
curl -s -u ЛОГИН "https://ES:9200/_cat/nodes?v&h=name,heap.percent,heap.max,ram.max,disk.used_percent"
curl -s -u ЛОГИН "https://ES:9200/_cat/allocation?v"
curl -s -u ЛОГИН "https://ES:9200/_cat/indices?v&s=store.size:desc&h=index,pri,rep,docs.count,store.size" | head -20
```

`curl -u ЛОГИН` без пароля спросит его сам, пароль не попадёт в историю команд. Адрес берите из `ELASTICSEARCH_URL` в настройках RX (часто через прокси).

- `heap.max` больше 31 ГБ или меньше половины `ram.max` при свободной памяти: куча настроена не по правилу.
- `heap.percent` стабильно выше 80%: по справке RX памяти нужно больше.
- `disk.used_percent` от 85%: новые шарды не размещаются, от 95% индексы закрыты на запись.
- Размер индексов поиска RX против объёма тел документов: так видно, подходит ли допущение 10% из модели к вашим документам.

## RabbitMQ

```bash
rabbitmq-diagnostics status | grep -i -E -A2 'memory|disk'
rabbitmq-diagnostics check_local_alarms
rabbitmqctl list_queues -p VHOST name messages consumers | sort -k2 -n | tail
rabbitmqctl list_queues -p VHOST name | wc -l
```

Если брокер в контейнере, те же команды через `docker exec ИМЯ_КОНТЕЙНЕРА`, имя смотрите в `docker ps`. Порог памяти должен быть 60% от отведённой брокеру памяти (в контейнере от лимита, а не от памяти сервера), порог диска не меньше 2 ГБ, том втрое больше порога. Тысячи очередей с UUID в имени: политика срока жизни (скил `rx-rabbitmq`).

## Журналы RX в день

Каталог журналов задан параметром `LOGS_PATH` в `config.yml` Directum Launcher, по умолчанию `<папка с данными>/logs` (справка RX, `admin_logs_linux.htm`).

```bash
du -sh "$LOGS_PATH"
find "$LOGS_PATH" -type f -newermt "$(date -d '-1 day' +%F)" -printf '%s\n' | awk '{s+=$1} END {printf "%.2f ГБ за сутки\n", s/1024/1024/1024}'
```

Модель закладывает 0,4 ГБ в день на 100 активных при обычном уровне логирования. Поток в разы больше: проверьте, не включён ли Debug или Trace (скил `rx-logs`).

## Число одновременно работающих

Это главный вход расчёта. Лучший источник: пиковое число сессий за обычную рабочую неделю из мониторинга (решение «Мониторинг Directum RX» или ваш Prometheus и Grafana). Если данных нет, 30% от учётных записей, и это записывается как допущение.

## Как читать сравнение

| Факт против расчёта | Что сказать |
|---|---|
| Ресурсов не меньше расчёта, продолжительных пиков нет | хватает, запас есть |
| Ресурсов меньше расчёта, но пиков нет | пока хватает, но запаса на рост нет; пересчитать с прогнозом на 1–3 года |
| Есть продолжительные пики (процессор от 80%, память от 90% в течение часа) | не хватает; сначала исключить одну тяжёлую причину (запрос, интеграция, зависший обработчик: скилы `rx-incident`, `rx-logs`), потом добавлять ресурсы |
| Ресурсов вдвое больше расчёта, метрики спокойные | можно отдать часть, если ресурсы нужны другим |
| Диск: свободно меньше 30% или прирост съест остаток меньше чем за квартал | срочно расширять или переносить данные |
