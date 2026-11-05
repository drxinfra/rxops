# Autovacuum, мёртвые строки, wraparound

## Почему autovacuum не успевает на RX

По умолчанию autovacuum приходит к таблице, когда в ней изменилось 20 % строк (`autovacuum_vacuum_scale_factor = 0.2`). У больших таблиц RX (задания, история, права доступа) это миллионы мёртвых строк до первой очистки: таблицы раздуваются, планировщик работает со старой статистикой. Калькулятор drxinfra (https://drxinfra.ru/postgresql-config.html) предлагает отправную точку:

```
autovacuum_vacuum_scale_factor = 0.01
autovacuum_analyze_scale_factor = 0.005
autovacuum_vacuum_insert_scale_factor = 0.01
autovacuum_naptime = 10s
autovacuum_max_workers = 3        # 4 при 8 ядрах и больше, нужен перезапуск
log_autovacuum_min_duration = 2s
```

Глобальные настройки предпочтительнее настроек отдельных таблиц: таблицы RX принадлежат прикладной разработке, а параметры хранения на них (`ALTER TABLE ... SET (...)`) это изменение её объектов. Если без этого никак, согласуйте и запишите в журнал изменений.

## Что мешает очистке

Vacuum не может убрать строки, которые ещё видны самой старой транзакции. Горизонт держат:

- долгие транзакции и `idle in transaction` (pg_check `long_xact`, `idle_in_xact`);
- слоты репликации с `xmin` или `catalog_xmin`, логические слоты;
- подготовленные транзакции (`pg_prepared_xacts`);
- реплики с `hot_standby_feedback = on` и долгими запросами на них.

pg_check в проверке `wraparound` называет держателя горизонта. Пока он есть, ручной `VACUUM` тоже ничего не очистит.

## Мёртвые строки и раздувание

```sql
SELECT schemaname || '.' || relname AS rel, n_live_tup, n_dead_tup,
       round(100.0 * n_dead_tup / greatest(n_live_tup + n_dead_tup, 1), 1) AS dead_pct,
       last_autovacuum, last_autoanalyze, pg_size_pretty(pg_relation_size(relid)) AS size
FROM pg_stat_user_tables ORDER BY n_dead_tup DESC LIMIT 20;
```

- Давно нет `last_autovacuum` при большом `n_dead_tup`: очистка не доходит до таблицы или её постоянно прерывают. В журнале это `canceling autovacuum task`: autovacuum уступает тому, кто попросил конфликтующую блокировку (`ALTER TABLE`, `TRUNCATE`, `LOCK TABLE`, `REINDEX`, `CREATE INDEX` без `CONCURRENTLY`). Разовая отмена безвредна, регулярная на одной таблице значит, что её надо развести по времени с этой операцией и почистить вручную в окно.
- `autovacuum_enabled = false` на таблице (pg_check показывает такие) показывают пользователю: очистку на ней кто-то выключил, часто «на время», и про это забыли.
- Обычный `VACUUM` место операционной системе не возвращает, он делает его доступным для новых строк. Вернуть место может `VACUUM FULL` или перестроение с `AccessExclusiveLock`: только в окно, с местом под копию таблицы и под WAL (`references/actions.md`).

## Wraparound

Номера транзакций идут по кругу, старые строки «замораживает» vacuum. Если заморозка не успевает, база сначала пишет `must be vacuumed within N transactions`, а у предела перестаёт принимать запись: `database is not accepting commands to avoid wraparound data loss`. Для RX это полная остановка.

```sql
SELECT datname, age(datfrozenxid) AS xid_age, mxid_age(datminmxid) AS mxid_age
FROM pg_database ORDER BY 2 DESC;
```

Тревожно от 1 млрд, предел около 2,1 млрд. Что делать:

1. Убрать то, что держит горизонт (список выше): завершить зависшие транзакции, удалить брошенные слоты, разобраться с подготовленными транзакциями. Всё по `references/actions.md`.
2. Заморозить старейшие таблицы: `VACUUM (FREEZE, VERBOSE) <таблица>;`. Список таблиц по возрасту:

   ```sql
   SELECT c.oid::regclass, age(c.relfrozenxid), pg_size_pretty(pg_total_relation_size(c.oid))
   FROM pg_class c WHERE c.relkind IN ('r', 'm', 't') ORDER BY 2 DESC LIMIT 20;
   ```

3. Если база уже не принимает запись, следуйте подсказке в тексте ошибки. В современных версиях достаточно `VACUUM` под суперпользователем в обычной сессии, однопользовательский режим не нужен. Операция долгая: на неё нужно окно.
4. На будущее: алерт на `age(datfrozenxid)` задолго до предела и настройки autovacuum выше.

Подробно: https://drxinfra.ru/errors/postgresql-wraparound.html, https://drxinfra.ru/errors/postgresql-canceling-autovacuum-task.html.
