# Медленные запросы

## Сначала убедиться, что дело в базе

Время одного действия в RX складывается из браузера, обратного прокси, сервисов RX, PostgreSQL и диска. Нередко база отвечает за десятки миллисекунд из нескольких секунд. Разбор сверху вниз описан в статье https://drxinfra.ru/blog/rx-performance-triage.html: клиент против сервера по вкладке Network, затем метод в журналах сервисов RX и доля времени в SQL. В базу спускаются, когда эта доля заметна. Крутить `shared_buffers` и `work_mem` до того, как найден запрос, бесполезно.

## pg_stat_statements

Нужен `shared_preload_libraries = 'pg_stat_statements'` (перезапуск) и `CREATE EXTENSION pg_stat_statements;` в базе RX. Решение «Мониторинг Directum RX» требует того же (справка RX: monitoring_nastroyka_postgresql.htm). Если расширения нет, это изменение: только с согласия и в окно.

```sql
SELECT calls,
       round(total_exec_time::numeric) AS total_ms,
       round(mean_exec_time::numeric, 2) AS mean_ms,
       round((100 * total_exec_time / sum(total_exec_time) OVER ())::numeric, 1) AS pct,
       queryid,
       left(regexp_replace(query, '\s+', ' ', 'g'), 120) AS query
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 20;
```

- Сортировать по суммарному времени. Запрос на 8 мс, вызванный двести тысяч раз, дороже трёхсекундного, вызванного дважды; обычно это поштучная обработка в коде, где на каждый элемент коллекции идёт отдельный запрос.
- Ещё два среза: по `calls` и по чтению с диска (`shared_blks_read`, при `track_io_timing = on` время чтения: в расширении для PostgreSQL 17 столбец `shared_blk_read_time`, раньше `blk_read_time`).
- Столбцы `total_exec_time` и `mean_exec_time` есть в версии расширения 1.8 и новее (PostgreSQL 13+). После обновления PostgreSQL версию расширения обновляют отдельно (`ALTER EXTENSION pg_stat_statements UPDATE;`, изменение).
- Статистика копится с последнего сброса. Чтобы увидеть картину за час после релиза, сохраните выборку, подождите и вычтите. Сброс `SELECT pg_stat_statements_reset();` стирает историю для всех, это изменение.

## Что происходит сейчас

```sql
SELECT pid, state, wait_event_type, wait_event,
       now() - xact_start AS xact_age, now() - query_start AS query_age,
       application_name, left(regexp_replace(query, '\s+', ' ', 'g'), 120) AS query
FROM pg_stat_activity
WHERE backend_type = 'client backend' AND state <> 'idle'
ORDER BY xact_age DESC NULLS LAST LIMIT 20;
```

Транзакция, большую часть времени `idle in transaction`, выглядит снаружи как «медленная база», а на деле приложение в цикле обрабатывает элементы и держит транзакцию. Лечится только в коде.

## Таблицы и индексы

```sql
-- большие таблицы, которые читаются полностью
SELECT relname, seq_scan, seq_tup_read, idx_scan, pg_size_pretty(pg_relation_size(relid)) AS size
FROM pg_stat_user_tables WHERE pg_relation_size(relid) > 100 * 1024 * 1024
ORDER BY seq_tup_read DESC LIMIT 20;
```

- План найденного запроса снимают на копии базы: `EXPLAIN (ANALYZE, BUFFERS) <запрос>;`. `ANALYZE` выполняет запрос по-настоящему: для `UPDATE`, `DELETE` и `INSERT` на рабочей базе он изменит данные, поэтому только на копии.
- Индекс ставится под конкретный план, а не пачкой: каждый замедляет запись и растит WAL. Создают `CREATE INDEX CONCURRENTLY` и вносят в прикладную разработку, иначе он живёт до первого обновления платформы.
- Индекс на колонке внешнего ключа нужен, если родительские строки меняют или удаляют: без него каждое такое изменение проверяет всю дочернюю таблицу.
- Неиспользуемые индексы видны в `pg_stat_user_indexes` (`idx_scan = 0`), но индексы таблиц RX не удаляют: они часть прикладной разработки.

## Кэш, временные файлы, диск

- Попадание в кэш меряйте за окно, а не накопительно с запуска: накопительное значение прячет свежий всплеск. pg_check делает два замера с разницей в 10 с. Чтение с диска выводит на запросы с большим `shared_blks_read`.
- Временные файлы (`temp_bytes` в `pg_stat_database`, `log_temp_files`) значат, что сортировкам и хешам не хватает `work_mem`. Поднимают его для роли или сессии тяжёлого отчёта, а не глобально.
- Если диск под PostgreSQL отвечает за десятки миллисекунд вместо единиц, медленно будет всё сразу: `iostat -x 5`, колонка `await`.

## Пакет фактов

Разбор заканчивается пакетом, который понятен и своим разработчикам, и вендору:

1. Сценарий: роль, действие, объект и его размер.
2. Замер: всего, на сервере, в SQL; холодный и тёплый прогон отдельно, хотя бы три раза.
3. Запрос, `queryid`, число вызовов на операцию, план `EXPLAIN (ANALYZE, BUFFERS)` с копии.
4. Версии RX и PostgreSQL, с какой версии стало хуже.
5. Что проверено и исключено: блокировки, ресурсы, состояние базы (вывод pg_check).

Подробно: https://drxinfra.ru/blog/rx-performance-triage.html, https://drxinfra.ru/errors/postgresql-canceling-statement-due-to-statement-timeout.html, https://drxinfra.ru/errors/rx-could-not-execute-query.html.
