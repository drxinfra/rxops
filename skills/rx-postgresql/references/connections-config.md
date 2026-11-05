# Соединения, память, таймауты, журнал, TLS

Отправная точка для значений: калькулятор https://drxinfra.ru/postgresql-config.html (формулы открыты в его описании). Он не видит вашу нагрузку: после применения смотрят на работу базы повторным прогоном pg_check.

## Как RX подключается к базе

- Строка подключения `CONNECTION_STRING` в секции `common_config` файла `DirectumLauncher/etc/config.yml`, формат ADO.NET: `Server`, `Port`, `Database`, `User ID`, `Password`, дополнительно `Timeout` и `Encoding` (справка RX: admin_minimalnye_nastroiki_linux.htm). Значения могут быть зашифрованы (admin_shifrovanie_parametrov.htm).
- Сервисы RX работают через драйвер Npgsql, у каждого сервиса свой пул соединений.
- Пул PgBouncer в RX настраивается секцией `SungeroPgBouncer` (справка admin_pgbouncer.htm): рекомендуемый режим `transaction`, по справке поддерживается только аутентификация MD5, и на сервере базы для этих пользователей тоже должен быть MD5. Публикация прикладной разработки идёт мимо пула по `DEPLOY_CONNECTION_STRING`. Если на сервере `password_encryption = scram-sha-256` (значение по умолчанию в новых версиях PostgreSQL), сверьте требования со справкой своей версии RX до включения пула.

## Соединения

- `too many clients already` (53300): занят `max_connections`. `remaining connection slots are reserved ...`: обычным ролям мест уже нет, остались резервные суперпользователя.
- Кто занял: pg_check `connections` или `SELECT application_name, state, count(*) FROM pg_stat_activity WHERE backend_type = 'client backend' GROUP BY 1, 2 ORDER BY 3 DESC;`.
- Оценка калькулятора: полтора соединения на одновременно работающего пользователя плюс сто на сервисы и обслуживание, от 100 до 1000. Значение по умолчанию 100 для RX мало уже на сотне пользователей. Справка RX велит увеличить `max_connections` на число пользователей и задать `max_prepared_transactions` равным ему (inst_podgotovka_k_ustanovke_linux.htm). Оба изменения требуют перезапуска.
- Каждое соединение это процесс и около 10 МБ памяти плюс `work_mem` на операцию. От 600 соединений дешевле поставить пул, чем растить лимит.
- Соединения рвутся после простоя (`connection to client lost`, `could not receive data from client`): межсетевой экран или балансировщик закрывает простаивающие. Помогает `tcp_keepalives_idle` на сервере или проверка соединений в пуле.

## Память

| Параметр | Отправная точка калькулятора |
|---|---|
| `shared_buffers` | четверть памяти, отведённой базе (на общем сервере с RX базе отводят около трети памяти) |
| `effective_cache_size` | 70 % памяти базы |
| `work_mem` | половина памяти за вычетом `shared_buffers`, делённая на `max_connections`, от 4 до 64 МБ |
| `maintenance_work_mem` | 1/16 памяти базы, от 64 МБ до 2 ГБ |
| `huge_pages` | `try` при `shared_buffers` от 8 ГБ; проверка `HugePages_Rsvd` в `/proc/meminfo` |

Грубая проверка запаса: `shared_buffers` плюс на каждое соединение 10 МБ и четверть `work_mem` не больше памяти сервера (pg_check `disk` считает это на сервере базы). Если ядро убило процесс базы (`terminated by signal 9` в журнале PostgreSQL, `Out of memory: Killed process` в `dmesg -T`), база обрывает все сессии и восстанавливается (`57P03 the database system is in recovery mode`). Для сервера только под базу ставят `vm.overcommit_memory = 2`: тогда запрос получит отказ в памяти, а ядро не будет убивать процесс базы.

В контейнере: `/dev/shm` по умолчанию 64 МБ, параллельные запросы падают с `could not resize shared memory segment` (нужен `--shm-size`); лимит памяти контейнера должен покрывать `shared_buffers`, соединения и `/dev/shm` вместе.

## Журнал предзаписи и чекпоинты

При `max_wal_size` по умолчанию (1 ГБ) массовые операции RX вызывают чекпоинты каждые несколько минут, отклик проседает волнами (pg_check `checkpoints` показывает долю вынужденных). Калькулятор даёт 4-16 ГБ по размеру базы, `checkpoint_timeout` 10-15 минут, `checkpoint_completion_target = 0.9`, `wal_compression = on`. Плата: восстановление после сбоя дольше, `pg_wal` больше.

## Таймауты

- `statement_timeout` на весь сервер не ставят: таймаут дают роли или сессии. Ошибка `canceling statement due to statement timeout` (57014) в логах RX приходит внутри `could not execute query`.
- Для SQL-запросов веб-сервера справка 25.2 описывает тайм-аут `SQL_TIMEOUT`: по умолчанию 120 с, допустимо от 0 до 255 с (admin_web_server_linux.htm, раздел «Разработка SQL-запросов»). К каким запросам он относится в вашей версии, сверяйте со справкой. Запрос, прерванный клиентом по его таймауту, PostgreSQL записывает как `canceling statement due to user request`.
- `idle_in_transaction_session_timeout` 20-30 минут закрывает зависшие транзакции.
- `lock_timeout` ограничивает ожидание блокировки (`55P03 canceling statement due to lock timeout`).

## Что писать в журнал

```
log_min_duration_statement = 2s
log_lock_waits = on
log_temp_files = 10MB
log_autovacuum_min_duration = 2s
log_checkpoints = on
log_line_prefix = '%m [%p] %u@%d %a '
shared_preload_libraries = 'pg_stat_statements'   # перезапуск
track_io_timing = on
compute_query_id = on                             # PostgreSQL 14 и новее
```

Без этого разбор «вчера тормозило» превращается в гадание. Расширение в базе RX создаётся один раз: `CREATE EXTENSION IF NOT EXISTS pg_stat_statements;` (изменение, с согласия).

## Доступ и пароли

- `no pg_hba.conf entry for host ...`: для адреса, пользователя, базы и типа шифрования нет правила. `pg_hba.conf rejects connection`: первым подошло правило `reject`. Правила читаются сверху вниз, применяется первое подходящее; проверка `SELECT line_number, type, database, user_name, address, auth_method, error FROM pg_hba_file_rules;` (нужен суперпользователь), применение `SELECT pg_reload_conf();`.
- `password authentication failed for user`: неверный пароль, пароль не задан, истёк `VALID UNTIL` или способ хеширования не совпадает с правилом (пароль сохранён как `md5`, а правило требует `scram-sha-256`). Точная причина в строке `DETAIL` журнала сервера. Много таких ошибок с чужих адресов значит перебор паролей.

## TLS от сервисов RX

Три отдельных вопроса:

1. Умеет ли сервер шифровать: `ssl = on`, сертификат с полной цепочкой и ключ (права `0600` у `postgres` или `0640` у `root` с группой `postgres`), `ssl_min_protocol_version = 'TLSv1.2'`. В SAN сертификата имя или адрес, по которому подключается RX. Перечитывается без перезапуска.
2. Обязан ли клиент шифровать: правила `hostssl` в `pg_hba.conf` и ниже `hostnossl ... reject` для сети приложений.
3. Проверяет ли клиент сервер: режим `SSL Mode` в строке подключения Npgsql.

| SSL Mode | Шифрует | Проверяет сертификат | Проверяет имя |
|---|---|---|---|
| `Disable` | нет | нет | нет |
| `Prefer` | если сервер умеет | нет | нет |
| `Require` | да | нет | нет |
| `VerifyCA` | да | да | нет |
| `VerifyFull` | да | да | да |

Поведение `Require` и параметр `Trust Server Certificate` менялись между версиями Npgsql, поэтому режим задают явно, а результат проверяют на сервере. В справке RX `SSL Mode` не описан: работу своей версии RX с ним проверяйте на тестовом стенде.

Сервисы RX работают в контейнерах со своим хранилищем доверенных сертификатов: корень, добавленный на хост, контейнер не видит. Типовая картина: `psql` с хоста подключается, а сервис RX пишет `The remote certificate is invalid according to the validation procedure` (`UntrustedRoot`). Если между RX и базой стоит пул, TLS настраивают и проверяют на двух участках.

Проверка на сервере базы:

```sql
SELECT a.usename, a.client_addr, a.application_name, s.ssl, s.version
FROM pg_stat_activity a JOIN pg_stat_ssl s USING (pid)
WHERE a.backend_type = 'client backend' ORDER BY s.ssl, a.client_addr;
```

С машины клиента: `openssl s_client -starttls postgres -connect <сервер>:5432 -showcerts </dev/null` (смотрите `Verify return code` и сколько сертификатов отдал сервер). Обрезанную цепочку ловит счёт строк: `grep -c 'BEGIN CERTIFICATE' server.crt; grep -c 'END CERTIFICATE' server.crt`.

Порядок перевода: включить `ssl`, проверить клиентов с `VerifyFull`, перевести строки подключения RX и остальных клиентов, по `pg_stat_ssl` убедиться, что открытых соединений не осталось, и только потом `hostssl` и запрет `hostnossl`. Иначе отвалится то, о чём забыли: резервное копирование, мониторинг, отчётные системы. Срок сертификата держат под наблюдением.

Подробно: https://drxinfra.ru/blog/rx-postgresql-tls-npgsql.html, https://drxinfra.ru/errors/postgresql-too-many-clients.html, https://drxinfra.ru/errors/tls-remote-certificate-is-invalid.html.
