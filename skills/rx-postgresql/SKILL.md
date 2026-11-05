---
name: rx-postgresql
description: >-
  Диагностика PostgreSQL под Directum RX: соединения и пулы, долгие транзакции и idle in transaction, цепочки блокировок, дедлоки 40P01, autovacuum и мёртвые строки, wraparound, рост pg_wal и архивация WAL, реплики и слоты, место на диске, тяжёлые запросы из pg_stat_statements, TLS от сервисов RX (Npgsql), здравость настроек. Использовать, когда «база тормозит», «RX виснет при сохранении», «кончились соединения», «pg_wal растёт», «диск под базой кончается», «реплика отстала», а в логах too many clients already, deadlock detected, could not execute query, canceling statement due to statement timeout, 57P01, No space left on device, archive command failed, requested WAL segment has already been removed, to avoid wraparound data loss. Только PostgreSQL и сборки на нём: RX на MS SQL Server этот скил не разбирает. Бэкап и пробное восстановление проверяет rx-backup-check, общий разбор сбоя начинает rx-incident.
license: Apache-2.0
compatibility: >-
  Серверы Directum RX с PostgreSQL или Postgres Pro 13-17 на Linux, в том числе в контейнере. Нужны psql (на сервере базы, в контейнере через --psql-cmd или на рабочем месте администратора) и python3 3.6+; для полной картины роль с pg_monitor или суперпользователь.
metadata:
  author: drxinfra
  version: "0.1.0"
---

# PostgreSQL под Directum RX

Скил проверяет базу RX на PostgreSQL и разбирает инциденты: кончились соединения, всё висит на блокировке, диск заполнен журналом, реплика отстала, запросы стали медленными. Сначала один прогон `pg_check.py` только на чтение, дальше ветка по тому, что он нашёл. Directum RX работает и на MS SQL Server, этот скил только про PostgreSQL и сборки на его основе: справка RX называет PostgreSQL, Postgres Pro, Jatoba и Tantor (inst_podgotovka_k_ustanovke_linux.htm), поддерживаемые версии перечислены в документе вендора «Типовые требования к аппаратному и программному обеспечению». Резервные копии разбирает `rx-backup-check`.

## Правила

- Сначала только смотрим. Команду, которая что-то меняет (перезапуск, удаление, изменение настроек, запись в базу, очистку очереди), сначала покажите пользователю, объясните, что она сделает и как это откатить, и выполняйте только после явного согласия.
- Пароли, токены и ключи не выводите. Конфиги и вывод, где они могут быть, читайте через маскировщик: `python3 ${CLAUDE_SKILL_DIR}/scripts/mask_secrets.py ФАЙЛ` или `КОМАНДА 2>&1 | python3 ${CLAUDE_SKILL_DIR}/scripts/mask_secrets.py`.
- Если подключён rxmcp (есть инструменты `rx_help_search` и `rx_help_topic`), поведение Directum RX сверяйте со справкой той версии, что стоит у пользователя, и называйте статью.
- Чего не знаете точно, так и говорите и предлагайте проверить на тестовом стенде. Не придумывайте параметры, пути и имена служб.
- `${CLAUDE_SKILL_DIR}` — папка этого скила. Если агент не подставляет переменную, скрипты лежат в `scripts/` рядом с этим файлом.

Отдельно для этого скила:

- Подключайтесь к самому PostgreSQL, а не через PgBouncer: пул в режиме transaction перемешивает сессии.
- Таблицы RX (`sungero_*`) не меняйте никогда, даже одну строку: данные правят через RX или разработчики.
- Завершение сессий, удаление слотов, правка `postgresql.conf`, `VACUUM FULL`, `REINDEX` идут по первому правилу. Готовые команды с последствиями и откатом: `references/actions.md`.

## Порядок работы

1. **Где база и как к ней подключиться.** Имя базы и сервер записаны в `CONNECTION_STRING` (секция `common_config` файла `DirectumLauncher/etc/config.yml`, справка RX: admin_minimalnye_nastroiki_linux.htm). Читайте только через маскировщик:

   ```bash
   grep -nE "DATABASE_ENGINE|CONNECTION_STRING|SungeroPgBouncer" <папка Directum Launcher>/etc/config.yml \
     | python3 ${CLAUDE_SKILL_DIR}/scripts/mask_secrets.py
   ```

   `DATABASE_ENGINE: mssql` означает MS SQL Server: скил не подходит, скажите об этом. Если значения зашифрованы (`config_password`, справка admin_shifrovanie_parametrov.htm), ничего не расшифровывайте: спросите имя базы у администратора или посмотрите `\l` на сервере базы. Где работает PostgreSQL: `systemctl list-units 'postgresql*'`, `docker ps --format '{{.Names}} {{.Image}}' | grep -i postgres` или под в Kubernetes.

2. **Прогон pg_check.** Одна из команд, по тому, откуда есть доступ:

   ```bash
   sudo -u postgres python3 ${CLAUDE_SKILL_DIR}/scripts/pg_check.py -d <база RX>
   python3 ${CLAUDE_SKILL_DIR}/scripts/pg_check.py -d "host=<сервер базы> dbname=<база RX> user=<роль>"
   python3 ${CLAUDE_SKILL_DIR}/scripts/pg_check.py --psql-cmd "docker exec -i <контейнер> psql -U postgres" -d <база RX>
   ```

   Пароль берётся из `~/.pgpass` (права 600), который пользователь заполнил сам, или из `PGPASSWORD`, заданной до запуска агента; в чате пароль не спрашивайте. Прогон занимает около 15 секунд: кэш, дедлоки и запись WAL меряются за окно 10 с. Код 0 норма, 1 есть WARN или CRIT, 2 не удалось подключиться. На сервере базы скрипт смотрит ещё место на разделах и память. Без python тот же набор запросов: `psql -X -d <база RX> -f ${CLAUDE_SKILL_DIR}/scripts/pg_check.sql`. Пороги, права, формат JSON: `references/pg-check.md`.

   Норма: строки OK и INFO. Разбирайте CRIT, потом WARN, по веткам ниже. Повтор одной проверки: `--only locks`.

3. **Соединения** (`connections`, в логах `too many clients already`, `remaining connection slots are reserved`). В деталях видно, какое приложение держит соединения и в каком состоянии. Много `idle in transaction` ведёт в шаг 4. Много `idle` от одного сервиса значит большой пул или утечку. Сейчас: завершить только зависшие сессии из шага 4. Потом: `max_connections` с запасом (оценка калькулятора drxinfra: полтора соединения на одновременно работающего пользователя плюс сто на сервисы, нужен перезапуск); от 600 соединений ставят пул. В RX пул описан в справке admin_pgbouncer.htm (секция `SungeroPgBouncer`, режим `transaction`, по справке поддерживается только аутентификация MD5). Подробно: `references/connections-config.md`.

4. **Долгие транзакции и idle in transaction** (`long_xact`, `idle_in_xact`). В деталях pid, приложение и последний запрос: по нему видно сервис и место в коде. `idle in transaction` значит, что приложение открыло транзакцию и не закрыло её: это дефект кода, его передают разработчикам с запросом и временем. Такая сессия держит блокировки и не даёт autovacuum чистить строки. Зависшую сессию завершают по `references/actions.md`, чтобы не ждать таймаута; на будущее ставят `idle_in_transaction_session_timeout` 20-30 минут. Справка RX требует `max_prepared_transactions` равным `max_connections` (inst_podgotovka_k_ustanovke_linux.htm), значит подготовленные транзакции (2PC) в RX возможны. Обычно они завершаются сразу; висящая минутами держит блокировки и горизонт очистки. Её показывают пользователю: `COMMIT PREPARED` или `ROLLBACK PREPARED` это решение о данных, его принимают с разработчиками или вендором.

5. **Блокировки** (`locks`, в журнале `still waiting for ShareLock`, `canceling statement due to lock timeout`). Скрипт строит цепочки и называет корень: сессию, которая держит блокировку и сама ничего не ждёт. Корень в `idle in transaction` ведёт в шаг 4. Корень с `AccessExclusiveLock` это обслуживание или выкладка (`ALTER TABLE`, `REINDEX`, `VACUUM FULL`, `CREATE INDEX` без `CONCURRENTLY`): выясните, кто его запустил. Сообщения RX «Не удалось установить блокировку» и «Запись была изменена в другой транзакции» относятся к блокировкам самого RX, а не PostgreSQL: в `pg_locks` их не видно. Их снимает администратор RX штатно (справка admin_snyatie_blokirovki_s_kartochki_objekta.htm), журнал блокировок описан в admin_log_locks_linux.htm. Разбор: `references/locks-transactions.md`.

6. **Дедлоки 40P01** (`deadlocks`, `deadlock detected`). Дедлок означает, что два места прикладного кода берут одни строки в разном порядке; база откатывает одну транзакцию, повтор обычно проходит. Включите `log_lock_waits = on` и `log_line_prefix` с `%a` (перечитывание конфигурации, без перезапуска). Соберите с одного случая `DETAIL` и `CONTEXT`, запросы обеих сторон, сервисы, частоту в сутки и версии RX и PostgreSQL, и отдайте разработчикам. `deadlock_timeout` не поднимайте: дедлоков станет столько же, ждать будут дольше.

7. **pg_wal и диск** (`wal`, `disk`, `PANIC: could not write to file "pg_wal/..."`, `No space left on device`). Файлы из `pg_wal` руками не удалять никогда: по ним база поднимается после сбоя. Если база уже встала, освободите место в другом месте или расширьте том, запустите базу и уберите причину. Причин три: архивация (шаг 8), слот (шаг 9), реплика не успевает применять журнал. На Astra Linux проверьте опцию `secdel` на разделе с данными: `findmnt -no TARGET,OPTIONS <раздел>`; скрипт смотрит её сам, если запущен на сервере базы. Разбор: `references/wal-replication.md`.

8. **Архивация WAL** (`archiver`, `archive command failed with exit code`). Строка «падает прямо сейчас» значит, что последняя ошибка свежее последнего успеха. Текст ошибки ищите в журнале PostgreSQL рядом с `archive command failed`. Частые причины: место, права, отвалившийся сетевой ресурс, сегмент уже лежит в архиве. Команду руками в настоящий архив не повторяйте, проверяйте по частям (`references/actions.md`, раздел «Проверка команды архивации»). После исправления очередь `.ready` тает сама. `archive_mode` не выключайте и `archive_command` не очищайте: точка восстановления уедет на последнюю полную копию, а при пустой команде сегменты продолжат копиться. Проверку самих копий ведёт `rx-backup-check`.

9. **Слоты и реплики** (`slots`, `replication`). Неактивный слот держит WAL, пока не кончится диск ведущего. Сначала выясните, чья это реплика и нужна ли она; удаляют слот только после согласия (`references/actions.md`). На будущее `max_slot_wal_keep_size`. Отставание реплики меряйте в байтах: время последней транзакции ночью растёт и у исправной реплики. `requested WAL segment has already been removed` значит, что реплику придётся пересоздавать. `recovery is in progress` в журнале реплики обычно даёт мониторинг, который вызывает функции ведущего сервера без проверки `pg_is_in_recovery()`.

10. **Autovacuum и wraparound** (`autovacuum`, `wraparound`). Доля мёртвых строк от 20 % у большой таблицы значит, что очистка не успевает или ей мешает долгая транзакция (шаг 4). `autovacuum_enabled = false` на таблице показывают пользователю. Возраст `age(datfrozenxid)` от 1 млрд тревожен, у предела около 2,1 млрд база перестаёт принимать запись. Сначала убирают то, что держит горизонт (скрипт его называет: сессия, слот, подготовленная транзакция), потом `VACUUM (FREEZE, VERBOSE)` старейших таблиц в окно. Разбор: `references/vacuum-wraparound.md`.

11. **«Тормозит»** (`statements`, `cache`, `checkpoints`, `canceling statement due to statement timeout`, `could not execute query`). Сначала убедитесь, что время уходит в базу: разбор от браузера до SQL описан в статье https://drxinfra.ru/blog/rx-performance-triage.html. В базе сортируйте `pg_stat_statements` по суммарному времени, а не по среднему: дешёвый запрос, вызванный тысячи раз, обычно и есть причина. План снимайте `EXPLAIN (ANALYZE, BUFFERS)` на копии базы, не на рабочей. Индекс создают `CREATE INDEX CONCURRENTLY` и вносят в прикладную разработку, иначе он живёт до первого обновления. Для SQL-запросов веб-сервера справка 25.2 описывает тайм-аут `SQL_TIMEOUT`, по умолчанию 120 с (admin_web_server_linux.htm, раздел «Разработка SQL-запросов»); к каким запросам он относится в вашей версии, сверяйте со справкой. Разбор: `references/slow-queries.md`.

12. **TLS от RX** (`tls`). `ssl = on` только разрешает шифрование; обязательным его делают правила `hostssl` в `pg_hba.conf`, а защиту от подмены сервера режим `SSL Mode=VerifyFull` в строке подключения Npgsql и доверенный корень внутри контейнеров RX. В справке RX параметр `SSL Mode` не описан: это параметр драйвера Npgsql, поведение своей версии проверяйте на тестовом стенде. Что проверить и в каком порядке переводить: `references/connections-config.md`.

13. **Настройки** (`settings`). Значения по умолчанию (`shared_buffers` 128 МБ, `max_wal_size` 1 ГБ, `random_page_cost` 4) для RX малы. По справке RX `max_connections` увеличивают на число пользователей, а `max_prepared_transactions` задают равным ему (inst_podgotovka_k_ustanovke_linux.htm). Отправная точка: калькулятор https://drxinfra.ru/postgresql-config.html. Перед применением `SELECT sourcefile, sourceline, name, error FROM pg_file_settings WHERE error IS NOT NULL;`, после него `SELECT pg_reload_conf();` и `SELECT name FROM pg_settings WHERE pending_restart;`. Перезапуск обрывает все сессии RX (в логах 57P01), его делают в окно.

14. **Журнал PostgreSQL за время инцидента.** Обычно в Debian и Astra `/var/log/postgresql/`, в RHEL и РЕД ОС `$PGDATA/log/`, в контейнере `docker logs <контейнер>`; точное место показывает `SHOW log_directory;`. Что искать:

    ```bash
    grep -E "PANIC|FATAL|deadlock detected|still waiting for|archive command failed|terminated by signal|No space left" <файл журнала> | tail -50
    ```

    `terminated by signal 9` вместе с `Out of memory` в `dmesg -T` значит, что ядро убило процесс базы: пересчитайте память (`references/connections-config.md`).

15. **После правок** запустите pg_check ещё раз той же командой и сравните с первым прогоном (`--json` в файл).

## Что сказать пользователю

Коротко, по пунктам:

1. Что нашли: строки CRIT и WARN своими словами, с цифрами (сколько сессий ждут, сколько занято места, какой слот).
2. Насколько срочно: CRIT остановит работу RX сейчас или скоро (диск, архив, wraparound, лимит соединений), WARN ухудшает работу, INFO на плановую работу.
3. Что сделать: команда из `references/actions.md` с последствиями и откатом, или кому передать (разработчикам: запросы, pid, время; вендору: пакет фактов по дедлоку).
4. Чем подтверждено: строка pg_check, запрос, файл журнала. Чего скрипт не видел (нет прав, не на сервере базы), так и скажите.

## Справочник

- `references/pg-check.md`: что смотрит каждая проверка, пороги и как их менять, права (роль с pg_monitor), ручной SQL.
- `references/actions.md`: команды, которые меняют систему, с последствиями и откатом.
- `references/locks-transactions.md`: долгие транзакции, цепочки блокировок, дедлоки, что отдавать разработчикам.
- `references/wal-replication.md`: рост pg_wal, переполнение диска, архивация, слоты, реплики и переключение.
- `references/vacuum-wraparound.md`: autovacuum под нагрузку RX, мёртвые строки, раздувание, wraparound.
- `references/connections-config.md`: соединения и пул, память, таймауты, журнал, TLS и Npgsql.
- `references/slow-queries.md`: pg_stat_statements, планы, индексы, кэш.
- `references/errors.md`: текст ошибки, что значит, какая ветка.
- Статьи drxinfra.ru: https://drxinfra.ru/blog/postgresql-deadlock-40p01.html, https://drxinfra.ru/blog/postgresql-wal-archive-full.html, https://drxinfra.ru/blog/rx-postgresql-replica.html, https://drxinfra.ru/blog/rx-postgresql-tls-npgsql.html, https://drxinfra.ru/blog/rx-performance-triage.html, https://drxinfra.ru/blog/rx-monitoring-alerts.html, справочник ошибок https://drxinfra.ru/errors/.
