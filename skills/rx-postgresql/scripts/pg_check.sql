-- pg_check.sql: проверка PostgreSQL под Directum RX, только чтение.
-- Сгенерирован командой: python3 pg_check.py --print-sql > pg_check.sql (руками не править).
-- Запуск: psql -X -d <база RX> -f pg_check.sql
-- Скрипт для psql: строки с обратной косой чертой это команды psql. В DBeaver или pgAdmin
-- выполняйте запросы по одному и пропускайте такие строки.
\set ON_ERROR_STOP off
\pset pager off
\x auto
SET default_transaction_read_only = on;
SET statement_timeout = '10s';
SET lock_timeout = '1s';
SELECT current_setting('server_version_num')::int >= 170000 AS pg17,
       pg_is_in_recovery() AS replica,
       EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements') AS has_pgss \gset

\echo '== Сервер: версия, роль, время работы, похожа ли база на базу RX (rx_tables > 0)'
SELECT current_setting('server_version_num')::int AS version_num,
       version() AS version,
       current_database() AS db,
       current_user AS usr,
       pg_is_in_recovery() AS replica,
       extract(epoch FROM now() - pg_postmaster_start_time())::bigint AS uptime_s,
       extract(epoch FROM pg_postmaster_start_time())::bigint AS started_epoch,
       to_char(now(), 'YYYY-MM-DD HH24:MI:SS TZ') AS now,
       (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS superuser,
       pg_has_role(current_user, 'pg_monitor', 'USAGE') AS pg_monitor,
       (SELECT count(*) FROM pg_class WHERE relkind = 'r' AND relname LIKE 'sungero\_%') AS rx_tables;

\echo '== Настройки, которые важны для RX'
SELECT name, reset_val AS setting, coalesce(unit, '') AS unit, source, boot_val, pending_restart
FROM pg_settings
WHERE name IN ('max_connections', 'superuser_reserved_connections', 'reserved_connections', 'shared_buffers', 'effective_cache_size',
               'work_mem', 'maintenance_work_mem', 'huge_pages', 'max_wal_size', 'min_wal_size',
               'wal_keep_size', 'max_slot_wal_keep_size', 'checkpoint_timeout', 'wal_level', 'wal_compression',
               'archive_mode', 'archive_command', 'archive_library', 'archive_timeout', 'synchronous_commit',
               'synchronous_standby_names', 'hot_standby_feedback', 'max_standby_streaming_delay', 'wal_log_hints', 'data_checksums',
               'fsync', 'full_page_writes', 'autovacuum', 'autovacuum_max_workers', 'autovacuum_naptime',
               'autovacuum_vacuum_scale_factor', 'autovacuum_analyze_scale_factor', 'autovacuum_vacuum_cost_limit', 'autovacuum_freeze_max_age', 'autovacuum_multixact_freeze_max_age',
               'idle_in_transaction_session_timeout', 'statement_timeout', 'lock_timeout', 'deadlock_timeout', 'log_lock_waits',
               'log_min_duration_statement', 'log_autovacuum_min_duration', 'log_checkpoints', 'log_temp_files', 'log_line_prefix',
               'shared_preload_libraries', 'track_io_timing', 'random_page_cost', 'effective_io_concurrency', 'max_parallel_workers_per_gather',
               'max_locks_per_transaction', 'max_prepared_transactions', 'ssl', 'ssl_min_protocol_version', 'password_encryption',
               'listen_addresses', 'port', 'data_directory', 'tcp_keepalives_idle')
ORDER BY name;

\echo '== Соединения по приложениям и состояниям; норма: всего меньше 80 % от max_connections'
SELECT coalesce(application_name, '') AS app, coalesce(usename, '') AS usr,
       coalesce(state, '') AS state, coalesce(host(client_addr), 'local') AS addr, count(*) AS n
FROM pg_stat_activity
WHERE (backend_type = 'client backend' OR (backend_type IS NULL AND usesysid IS NOT NULL))
  AND pid <> pg_backend_pid()
GROUP BY 1, 2, 3, 4
ORDER BY 5 DESC;

\echo '== Транзакции дольше минуты; тревожно от 5 минут, idle in transaction смотреть по state_s'
SELECT pid, coalesce(usename, '') AS usr, coalesce(application_name, '') AS app,
       coalesce(host(client_addr), 'local') AS addr, coalesce(state, '') AS state,
       extract(epoch FROM now() - xact_start)::bigint AS xact_s,
       extract(epoch FROM now() - state_change)::bigint AS state_s,
       coalesce(wait_event_type, '') AS wait_type, coalesce(wait_event, '') AS wait_event,
       age(backend_xmin) AS xmin_age,
       left(regexp_replace(coalesce(query, ''), '\s+', ' ', 'g'), 300) AS query
FROM pg_stat_activity
WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()
  AND xact_start < now() - interval '60 seconds'
ORDER BY xact_start
LIMIT 50;

\echo '== Подготовленные транзакции (2PC); норма: пусто'
SELECT gid, owner::text AS owner, database::text AS database,
       extract(epoch FROM now() - prepared)::bigint AS age_s, age(transaction) AS xid_age
FROM pg_prepared_xacts
ORDER BY prepared;

\echo '== Кто кого ждёт: waiting ждут, blocker держат; корень цепочки тот, у кого blocked_by пуст'
WITH l AS (
  SELECT pl.pid, min((to_jsonb(pl) ->> 'waitstart')::timestamptz) AS waitstart
  FROM pg_locks pl
  WHERE NOT pl.granted AND pl.pid IS NOT NULL AND pl.pid <> pg_backend_pid()
  GROUP BY pl.pid
), w AS (
  SELECT l.pid, pg_blocking_pids(l.pid) AS blocked_by,
         extract(epoch FROM now() - coalesce(l.waitstart, a.query_start))::bigint AS wait_s
  FROM l LEFT JOIN pg_stat_activity a ON a.pid = l.pid
), b AS (
  SELECT DISTINCT unnest(blocked_by) AS pid FROM w
), s AS (
  SELECT 'waiting' AS role, w.pid, w.wait_s, w.blocked_by FROM w
  UNION ALL
  SELECT 'blocker', b.pid, NULL::bigint, pg_blocking_pids(b.pid) FROM b
  WHERE b.pid NOT IN (SELECT pid FROM w)
)
SELECT s.role, s.pid, s.wait_s, s.blocked_by,
       coalesce(a.usename, '') AS usr, coalesce(a.application_name, '') AS app,
       coalesce(host(a.client_addr), 'local') AS addr, coalesce(a.state, '') AS state,
       coalesce(a.backend_type, '') AS backend_type,
       extract(epoch FROM now() - a.xact_start)::bigint AS xact_s,
       extract(epoch FROM now() - a.state_change)::bigint AS state_s,
       (SELECT string_agg(DISTINCT pl.relation::regclass::text, ', ')
          FROM pg_locks pl
         WHERE pl.pid = s.pid AND pl.granted AND pl.locktype = 'relation'
           AND pl.mode = 'AccessExclusiveLock') AS exclusive_on,
       left(regexp_replace(coalesce(a.query, ''), '\s+', ' ', 'g'), 300) AS query
FROM s LEFT JOIN pg_stat_activity a ON a.pid = s.pid
ORDER BY s.role DESC, s.wait_s DESC NULLS LAST;

\echo '== Статистика баз: deadlocks, попадание в кэш (blks_hit, blks_read), временные файлы'
SELECT datname, datname = current_database() AS current, numbackends,
       xact_commit, xact_rollback, blks_read, blks_hit, temp_files, temp_bytes, deadlocks, conflicts,
       extract(epoch FROM now() - coalesce(stats_reset, pg_postmaster_start_time()))::bigint AS stats_age_s,
       stats_reset IS NOT NULL AS was_reset,
       CASE WHEN pg_is_in_recovery() THEN NULL ELSE pg_current_wal_lsn()::text END AS wal_lsn
FROM pg_stat_database
WHERE datname IS NOT NULL AND datname NOT IN ('template0', 'template1')
ORDER BY datname;

\echo '== Размер pg_wal; норма: около max_wal_size + wal_keep_size'
SELECT count(*) AS files, coalesce(sum(size), 0)::bigint AS bytes FROM pg_ls_waldir();

\echo '== Сегменты в очереди на архивацию (.ready); норма: единицы и не растёт'
SELECT count(*) FILTER (WHERE name LIKE '%.ready') AS ready,
       count(*) FILTER (WHERE name LIKE '%.done') AS done
FROM pg_ls_archive_statusdir();

\echo '== Архиватор: failing_now = t значит архивация падает сейчас'
SELECT archived_count, failed_count, coalesce(last_archived_wal, '') AS last_archived_wal,
       extract(epoch FROM now() - last_archived_time)::bigint AS archived_age_s,
       coalesce(last_failed_wal, '') AS last_failed_wal,
       extract(epoch FROM now() - last_failed_time)::bigint AS failed_age_s,
       (last_failed_time IS NOT NULL
        AND (last_archived_time IS NULL OR last_failed_time > last_archived_time)) AS failing_now,
       CASE WHEN pg_is_in_recovery() THEN NULL ELSE pg_walfile_name(pg_current_wal_lsn()) END AS current_wal
FROM pg_stat_archiver;

\echo '== Слоты репликации: неактивный слот с большим retained заполняет диск'
WITH cur AS (
  SELECT CASE WHEN pg_is_in_recovery() THEN pg_last_wal_receive_lsn() ELSE pg_current_wal_lsn() END AS lsn
)
SELECT s.slot_name::text AS slot_name, s.slot_type, coalesce(s.database::text, '') AS database,
       s.active, s.active_pid,
       to_jsonb(s) ->> 'wal_status' AS wal_status,
       (to_jsonb(s) ->> 'safe_wal_size')::bigint AS safe_wal_size,
       to_jsonb(s) ->> 'invalidation_reason' AS invalidation_reason,
       pg_wal_lsn_diff(cur.lsn, s.restart_lsn)::bigint AS retained_bytes,
       pg_size_pretty(pg_wal_lsn_diff(cur.lsn, s.restart_lsn)) AS retained,
       age(s.xmin) AS xmin_age, age(s.catalog_xmin) AS catalog_xmin_age
FROM pg_replication_slots s, cur
ORDER BY retained_bytes DESC NULLS LAST;

\echo '== Реплики на ведущем: отставание в байтах'
WITH cur AS (
  SELECT CASE WHEN pg_is_in_recovery() THEN pg_last_wal_receive_lsn() ELSE pg_current_wal_lsn() END AS lsn
)
SELECT r.pid, coalesce(r.application_name, '') AS app, coalesce(host(r.client_addr), 'local') AS addr,
       coalesce(r.state, '') AS state, coalesce(r.sync_state, '') AS sync_state,
       pg_wal_lsn_diff(cur.lsn, r.replay_lsn)::bigint AS replay_lag_bytes,
       pg_wal_lsn_diff(cur.lsn, r.flush_lsn)::bigint AS flush_lag_bytes,
       round(extract(epoch FROM r.replay_lag)::numeric, 1) AS replay_lag_s,
       r.backend_xmin IS NOT NULL AS feedback
FROM pg_stat_replication r, cur
ORDER BY replay_lag_bytes DESC NULLS LAST;

\echo '== Только на реплике: приём и применение WAL'
\if :replica
SELECT coalesce(r.status, '') AS status, coalesce(r.sender_host, '') AS sender_host, r.sender_port,
       coalesce(r.slot_name, '') AS slot_name,
       extract(epoch FROM now() - r.last_msg_receipt_time)::bigint AS last_msg_age_s,
       pg_wal_lsn_diff(pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn())::bigint AS replay_backlog_bytes,
       extract(epoch FROM now() - pg_last_xact_replay_timestamp())::bigint AS last_replay_age_s,
       pg_is_wal_replay_paused() AS paused
FROM (SELECT 1) x LEFT JOIN pg_stat_wal_receiver r ON true;
\endif

\echo '== Мёртвые строки и давность vacuum и analyze'
SELECT schemaname || '.' || relname AS rel, n_live_tup AS live, n_dead_tup AS dead,
       round(100.0 * n_dead_tup / greatest(n_live_tup + n_dead_tup, 1), 1) AS dead_pct,
       pg_relation_size(relid) AS heap_bytes,
       extract(epoch FROM now() - greatest(last_autovacuum, last_vacuum))::bigint AS vacuum_age_s,
       extract(epoch FROM now() - greatest(last_autoanalyze, last_analyze))::bigint AS analyze_age_s,
       autovacuum_count + vacuum_count AS vacuums
FROM pg_stat_user_tables
ORDER BY n_dead_tup DESC
LIMIT 15;

\echo '== Vacuum, который идёт сейчас'
SELECT p.pid, p.datname::text AS datname, p.relid::regclass::text AS rel, p.phase,
       coalesce(a.query, '') LIKE 'autovacuum:%' AS auto,
       coalesce(a.query, '') LIKE '%to prevent wraparound%' AS wraparound,
       extract(epoch FROM now() - a.xact_start)::bigint AS running_s
FROM pg_stat_progress_vacuum p LEFT JOIN pg_stat_activity a ON a.pid = p.pid
ORDER BY running_s DESC NULLS LAST;

\echo '== Таблицы с выключенным autovacuum; норма: пусто'
SELECT c.oid::regclass::text AS rel
FROM pg_class c
WHERE c.relkind IN ('r', 'm') AND c.reloptions::text ~ 'autovacuum_enabled=(false|off|0)'
ORDER BY 1
LIMIT 20;

\echo '== Возраст транзакций баз; тревожно от 1 млрд, предел около 2,1 млрд'
SELECT datname, age(datfrozenxid) AS xid_age, mxid_age(datminmxid) AS mxid_age
FROM pg_database
ORDER BY 2 DESC;

\echo '== Самые старые таблицы текущей базы'
SELECT c.oid::regclass::text AS rel, age(c.relfrozenxid) AS xid_age, pg_total_relation_size(c.oid) AS bytes
FROM pg_class c
WHERE c.relkind IN ('r', 'm', 't')
ORDER BY age(c.relfrozenxid) DESC
LIMIT 5;

\echo '== Кто держит горизонт очистки'
SELECT * FROM (
  SELECT 'backend' AS kind, pid::text AS name, coalesce(application_name, '') AS app,
         coalesce(backend_type, '') AS backend_type, age(backend_xmin) AS xid_age
  FROM pg_stat_activity WHERE backend_xmin IS NOT NULL AND pid <> pg_backend_pid()
  UNION ALL
  SELECT 'slot', slot_name::text, '', slot_type, greatest(age(xmin), age(catalog_xmin))
  FROM pg_replication_slots WHERE xmin IS NOT NULL OR catalog_xmin IS NOT NULL
  UNION ALL
  SELECT 'prepared', gid, '', '', age(transaction) FROM pg_prepared_xacts
) h
ORDER BY xid_age DESC NULLS LAST
LIMIT 5;

\echo '== Чекпоинты: много req значит мал max_wal_size'
\if :pg17
SELECT num_timed AS timed, num_requested AS req,
       extract(epoch FROM now() - stats_reset)::bigint AS stats_age_s
FROM pg_stat_checkpointer;
\else
SELECT checkpoints_timed AS timed, checkpoints_req AS req,
       extract(epoch FROM now() - stats_reset)::bigint AS stats_age_s
FROM pg_stat_bgwriter;
\endif

\echo '== Тяжёлые запросы из pg_stat_statements: по суммарному времени'
\if :has_pgss
-- для расширения 1.8 и новее (PostgreSQL 13+); в старых версиях столбцы total_time, mean_time
WITH s AS (
  SELECT queryid, calls, total_exec_time AS total_ms, mean_exec_time AS mean_ms, max_exec_time AS max_ms, rows,
         shared_blks_read, shared_blks_hit, temp_blks_written,
         left(regexp_replace(query, '\s+', ' ', 'g'), 300) AS query
  FROM pg_stat_statements
  WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
    AND query !~* '^\s*(set|reset|begin|commit|rollback|start transaction|discard|deallocate)\M'
    AND query NOT LIKE 'SELECT coalesce(json_agg(q), %'
), r AS (
  SELECT s.*,
         row_number() OVER (ORDER BY total_ms DESC) AS rn_total,
         row_number() OVER (ORDER BY calls DESC) AS rn_calls,
         row_number() OVER (ORDER BY shared_blks_read DESC) AS rn_read,
         round((100 * total_ms / nullif(sum(total_ms) OVER (), 0))::numeric, 1) AS pct
  FROM s
)
SELECT queryid::text AS queryid, calls, round(total_ms::numeric, 1) AS total_ms,
       round(mean_ms::numeric, 2) AS mean_ms, round(max_ms::numeric, 1) AS max_ms, rows,
       shared_blks_read, shared_blks_hit, temp_blks_written, pct, rn_total, rn_calls, rn_read, query
FROM r
WHERE rn_total <= 10 OR rn_calls <= 10 OR rn_read <= 10
ORDER BY rn_total;
\else
\echo '   pg_stat_statements в этой базе не установлен'
\endif

\echo '== Шифрование соединений: ssl = f у соединений по сети'
SELECT coalesce(a.application_name, '') AS app, coalesce(host(a.client_addr), 'local') AS addr,
       coalesce(a.usename, '') AS usr, s.ssl, coalesce(s.version, '') AS tls_version, count(*) AS n
FROM pg_stat_activity a JOIN pg_stat_ssl s ON s.pid = a.pid
WHERE a.backend_type = 'client backend' AND a.pid <> pg_backend_pid()
GROUP BY 1, 2, 3, 4, 5
ORDER BY 6 DESC;

\echo '== Параметры, которые ждут перезапуска'
SELECT name, setting FROM pg_settings WHERE pending_restart ORDER BY name;

\echo '== Ошибки в файлах конфигурации (нужен суперпользователь)'
SELECT sourcefile, sourceline, name, error FROM pg_file_settings WHERE error IS NOT NULL;

\echo '== Ошибки в pg_hba.conf (нужен суперпользователь)'
SELECT line_number, error FROM pg_hba_file_rules WHERE error IS NOT NULL;

\echo '== Число таблиц: сравните с max_locks_per_transaction x (max_connections + max_prepared_transactions)'
SELECT count(*) AS tables
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'm', 'f')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname !~ '^pg_toast';

\echo '== Размеры баз'
SELECT datname::text AS datname,
       CASE WHEN has_database_privilege(oid, 'CONNECT') THEN pg_database_size(oid) END AS bytes
FROM pg_database
WHERE datallowconn AND NOT datistemplate
ORDER BY 2 DESC NULLS LAST;

\echo '== Самые большие таблицы текущей базы'
SELECT c.oid::regclass::text AS rel, pg_total_relation_size(c.oid) AS total_bytes,
       pg_relation_size(c.oid) AS heap_bytes, pg_indexes_size(c.oid) AS index_bytes,
       c.reltuples::bigint AS est_rows
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'm', 'p')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname !~ '^pg_toast'
ORDER BY pg_total_relation_size(c.oid) DESC
LIMIT 10;
