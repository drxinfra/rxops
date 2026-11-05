#!/usr/bin/env python3
"""Проверка PostgreSQL под Directum RX. Только чтение: запросы к служебным представлениям через psql.

Использование:
  sudo -u postgres python3 pg_check.py -d rx      на сервере базы через unix-сокет
  python3 pg_check.py -d "host=db01 dbname=rx user=rx_monitor"
                                                по сети; пароль из ~/.pgpass или PGPASSWORD
  python3 pg_check.py --psql-cmd "docker exec -i rx-postgres psql -U postgres" -d rx
                                                база в контейнере
  python3 pg_check.py --json                    итог в JSON
  python3 pg_check.py --only locks,long_xact    только выбранные проверки
  python3 pg_check.py --print-sql               те же запросы для ручного запуска (pg_check.sql)

Подключение задаётся как у psql: ключ -d (имя базы или строка подключения) и переменные
PGHOST, PGPORT, PGUSER, PGDATABASE, PGPASSFILE. Пароль скрипт не спрашивает (psql -w).
Каждый запрос идёт отдельной сессией только для чтения со statement_timeout и lock_timeout.
Подключайтесь к самому PostgreSQL, а не через PgBouncer.

Код возврата: 0 норма, 1 найдено отклонение (WARN или CRIT), 2 не удалось проверить.
Только стандартная библиотека Python 3.6+.
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
try:
    import mask_secrets as _ms  # копия shared/mask_secrets.py рядом со скриптом
except ImportError:  # pragma: no cover
    _ms = None

VERSION = "0.1.0"
APP = "rxops-pg_check"
KIB, MIB, GIB = 1024, 1024 ** 2, 1024 ** 3
RANK = {"SKIP": 0, "OK": 1, "INFO": 2, "WARN": 3, "CRIT": 4}

# Пороги: стартовые значения. Подстраиваются ключом --threshold ИМЯ=ЗНАЧЕНИЕ под фон своей системы.
THRESHOLDS = [
    ("conn_warn_pct", 80, "соединений занято от max_connections, %: WARN"),
    ("conn_crit_pct", 95, "соединений занято от max_connections, %: CRIT"),
    ("xact_warn_s", 300, "транзакция открыта дольше, с: WARN"),
    ("xact_crit_s", 1800, "транзакция открыта дольше, с: CRIT"),
    ("idle_warn_s", 300, "idle in transaction дольше, с: WARN"),
    ("idle_crit_s", 1800, "idle in transaction дольше, с: CRIT"),
    ("prepared_warn_s", 300, "подготовленная транзакция (2PC) висит дольше, с: WARN"),
    ("lock_warn_s", 30, "ожидание блокировки дольше, с: WARN"),
    ("lock_crit_s", 300, "ожидание блокировки дольше, с: CRIT"),
    ("deadlock_warn_per_day", 10, "дедлоков в сутки в среднем: WARN"),
    ("deadlock_crit_per_day", 100, "дедлоков в сутки в среднем: CRIT"),
    ("dead_warn_pct", 20, "доля мёртвых строк в таблице, %: WARN"),
    ("dead_crit_pct", 50, "доля мёртвых строк в таблице от 1 ГБ, %: CRIT"),
    ("dead_min_rows", 100000, "мёртвых строк меньше этого числа не считаем"),
    ("xid_warn", 1000000000, "возраст транзакций базы, age(datfrozenxid): WARN"),
    ("xid_crit", 1500000000, "возраст транзакций базы, age(datfrozenxid): CRIT"),
    ("wal_warn_ratio", 2.0, "pg_wal больше (max_wal_size + wal_keep_size) во столько раз: WARN"),
    ("wal_crit_ratio", 5.0, "pg_wal больше (max_wal_size + wal_keep_size) во столько раз: CRIT"),
    ("ready_warn", 10, "сегментов ждут архивации (.ready): WARN"),
    ("ready_crit", 100, "сегментов ждут архивации (.ready): CRIT"),
    ("archive_stale_s", 3600, "есть очередь на архивацию, а последний успех старше, с: WARN"),
    ("slot_warn_bytes", GIB, "слот удерживает WAL, байт: WARN"),
    ("slot_crit_bytes", 10 * GIB, "слот удерживает WAL, байт: CRIT"),
    ("repl_warn_bytes", GIB, "реплика отстаёт по применению, байт: WARN"),
    ("repl_crit_bytes", 10 * GIB, "реплика отстаёт по применению, байт: CRIT"),
    ("disk_warn_free_pct", 20, "свободно на разделе базы меньше, %: WARN"),
    ("disk_crit_free_pct", 10, "свободно на разделе базы меньше, %: CRIT"),
    ("ckpt_req_warn_pct", 50, "доля чекпоинтов по объёму WAL, %: WARN"),
    ("ckpt_min_total", 20, "чекпоинтов меньше этого числа не оцениваем"),
    ("cache_warn_pct", 95, "попадание в кэш за окно ниже, %: WARN"),
    ("cache_min_read_blocks", 10000, "блоков прочитано с диска за окно меньше этого: не оцениваем"),
    ("eol_soon_days", 365, "до конца поддержки мажорной версии меньше, дней: INFO"),
]

# Конец поддержки мажорных версий сообществом PostgreSQL (postgresql.org/support/versioning).
EOL = {12: "2024-11-21", 13: "2025-11-13", 14: "2026-11-12", 15: "2027-11-11",
       16: "2028-11-09", 17: "2029-11-08", 18: "2030-11-14"}

SETTING_NAMES = [
    "max_connections", "superuser_reserved_connections", "reserved_connections", "shared_buffers",
    "effective_cache_size", "work_mem", "maintenance_work_mem", "huge_pages", "max_wal_size", "min_wal_size",
    "wal_keep_size", "max_slot_wal_keep_size", "checkpoint_timeout", "wal_level", "wal_compression",
    "archive_mode", "archive_command", "archive_library", "archive_timeout", "synchronous_commit",
    "synchronous_standby_names", "hot_standby_feedback", "max_standby_streaming_delay", "wal_log_hints",
    "data_checksums", "fsync", "full_page_writes", "autovacuum", "autovacuum_max_workers", "autovacuum_naptime",
    "autovacuum_vacuum_scale_factor", "autovacuum_analyze_scale_factor", "autovacuum_vacuum_cost_limit",
    "autovacuum_freeze_max_age", "autovacuum_multixact_freeze_max_age", "idle_in_transaction_session_timeout",
    "statement_timeout", "lock_timeout", "deadlock_timeout", "log_lock_waits", "log_min_duration_statement",
    "log_autovacuum_min_duration", "log_checkpoints", "log_temp_files", "log_line_prefix",
    "shared_preload_libraries", "track_io_timing", "random_page_cost", "effective_io_concurrency",
    "max_parallel_workers_per_gather", "max_locks_per_transaction", "max_prepared_transactions", "ssl",
    "ssl_min_protocol_version", "password_encryption", "listen_addresses", "port", "data_directory",
    "tcp_keepalives_idle",
]

# --- Запросы. Работают на PostgreSQL 13-17 и на ведущем сервере, и на реплике. ---
# Столбцы, которых нет в старых версиях, берутся через to_jsonb(строка) ->> 'имя': там они просто NULL.

Q = {}

Q["server"] = r"""
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
       (SELECT count(*) FROM pg_class WHERE relkind = 'r' AND relname LIKE 'sungero\_%') AS rx_tables
"""

Q["settings"] = r"""
SELECT name, reset_val AS setting, coalesce(unit, '') AS unit, source, boot_val, pending_restart
FROM pg_settings
WHERE name IN (%s)
ORDER BY name
""" % ",\n               ".join(", ".join("'%s'" % n for n in SETTING_NAMES[i:i + 5])
                              for i in range(0, len(SETTING_NAMES), 5))

Q["pending_restart"] = r"""
SELECT name, setting FROM pg_settings WHERE pending_restart ORDER BY name
"""

Q["file_errors"] = r"""
SELECT sourcefile, sourceline, name, error FROM pg_file_settings WHERE error IS NOT NULL
"""

Q["hba_errors"] = r"""
SELECT line_number, error FROM pg_hba_file_rules WHERE error IS NOT NULL
"""

Q["table_count"] = r"""
SELECT count(*) AS tables
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'm', 'f')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname !~ '^pg_toast'
"""

Q["connections"] = r"""
SELECT coalesce(application_name, '') AS app, coalesce(usename, '') AS usr,
       coalesce(state, '') AS state, coalesce(host(client_addr), 'local') AS addr, count(*) AS n
FROM pg_stat_activity
WHERE (backend_type = 'client backend' OR (backend_type IS NULL AND usesysid IS NOT NULL))
  AND pid <> pg_backend_pid()
GROUP BY 1, 2, 3, 4
ORDER BY 5 DESC
"""

Q["activity"] = r"""
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
LIMIT 50
"""

Q["prepared"] = r"""
SELECT gid, owner::text AS owner, database::text AS database,
       extract(epoch FROM now() - prepared)::bigint AS age_s, age(transaction) AS xid_age
FROM pg_prepared_xacts
ORDER BY prepared
"""

Q["locks"] = r"""
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
ORDER BY s.role DESC, s.wait_s DESC NULLS LAST
"""

Q["dbstats"] = r"""
SELECT datname, datname = current_database() AS current, numbackends,
       xact_commit, xact_rollback, blks_read, blks_hit, temp_files, temp_bytes, deadlocks, conflicts,
       extract(epoch FROM now() - coalesce(stats_reset, pg_postmaster_start_time()))::bigint AS stats_age_s,
       stats_reset IS NOT NULL AS was_reset,
       CASE WHEN pg_is_in_recovery() THEN NULL ELSE pg_current_wal_lsn()::text END AS wal_lsn
FROM pg_stat_database
WHERE datname IS NOT NULL AND datname NOT IN ('template0', 'template1')
ORDER BY datname
"""

Q["vacuum_tables"] = r"""
SELECT schemaname || '.' || relname AS rel, n_live_tup AS live, n_dead_tup AS dead,
       round(100.0 * n_dead_tup / greatest(n_live_tup + n_dead_tup, 1), 1) AS dead_pct,
       pg_relation_size(relid) AS heap_bytes,
       extract(epoch FROM now() - greatest(last_autovacuum, last_vacuum))::bigint AS vacuum_age_s,
       extract(epoch FROM now() - greatest(last_autoanalyze, last_analyze))::bigint AS analyze_age_s,
       autovacuum_count + vacuum_count AS vacuums
FROM pg_stat_user_tables
ORDER BY n_dead_tup DESC
LIMIT 15
"""

Q["vacuum_running"] = r"""
SELECT p.pid, p.datname::text AS datname, p.relid::regclass::text AS rel, p.phase,
       coalesce(a.query, '') LIKE 'autovacuum:%' AS auto,
       coalesce(a.query, '') LIKE '%to prevent wraparound%' AS wraparound,
       extract(epoch FROM now() - a.xact_start)::bigint AS running_s
FROM pg_stat_progress_vacuum p LEFT JOIN pg_stat_activity a ON a.pid = p.pid
ORDER BY running_s DESC NULLS LAST
"""

Q["autovacuum_off"] = r"""
SELECT c.oid::regclass::text AS rel
FROM pg_class c
WHERE c.relkind IN ('r', 'm') AND c.reloptions::text ~ 'autovacuum_enabled=(false|off|0)'
ORDER BY 1
LIMIT 20
"""

Q["wrap_db"] = r"""
SELECT datname, age(datfrozenxid) AS xid_age, mxid_age(datminmxid) AS mxid_age
FROM pg_database
ORDER BY 2 DESC
"""

Q["wrap_tables"] = r"""
SELECT c.oid::regclass::text AS rel, age(c.relfrozenxid) AS xid_age, pg_total_relation_size(c.oid) AS bytes
FROM pg_class c
WHERE c.relkind IN ('r', 'm', 't')
ORDER BY age(c.relfrozenxid) DESC
LIMIT 5
"""

Q["xmin_holders"] = r"""
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
LIMIT 5
"""

Q["wal_dir"] = r"""
SELECT count(*) AS files, coalesce(sum(size), 0)::bigint AS bytes FROM pg_ls_waldir()
"""

Q["archive_status"] = r"""
SELECT count(*) FILTER (WHERE name LIKE '%.ready') AS ready,
       count(*) FILTER (WHERE name LIKE '%.done') AS done
FROM pg_ls_archive_statusdir()
"""

Q["archiver"] = r"""
SELECT archived_count, failed_count, coalesce(last_archived_wal, '') AS last_archived_wal,
       extract(epoch FROM now() - last_archived_time)::bigint AS archived_age_s,
       coalesce(last_failed_wal, '') AS last_failed_wal,
       extract(epoch FROM now() - last_failed_time)::bigint AS failed_age_s,
       (last_failed_time IS NOT NULL
        AND (last_archived_time IS NULL OR last_failed_time > last_archived_time)) AS failing_now,
       CASE WHEN pg_is_in_recovery() THEN NULL ELSE pg_walfile_name(pg_current_wal_lsn()) END AS current_wal
FROM pg_stat_archiver
"""

Q["slots"] = r"""
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
ORDER BY retained_bytes DESC NULLS LAST
"""

Q["replication"] = r"""
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
ORDER BY replay_lag_bytes DESC NULLS LAST
"""

Q["receiver"] = r"""
SELECT coalesce(r.status, '') AS status, coalesce(r.sender_host, '') AS sender_host, r.sender_port,
       coalesce(r.slot_name, '') AS slot_name,
       extract(epoch FROM now() - r.last_msg_receipt_time)::bigint AS last_msg_age_s,
       pg_wal_lsn_diff(pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn())::bigint AS replay_backlog_bytes,
       extract(epoch FROM now() - pg_last_xact_replay_timestamp())::bigint AS last_replay_age_s,
       pg_is_wal_replay_paused() AS paused
FROM (SELECT 1) x LEFT JOIN pg_stat_wal_receiver r ON true
"""

Q["checkpoints"] = r"""
SELECT checkpoints_timed AS timed, checkpoints_req AS req,
       extract(epoch FROM now() - stats_reset)::bigint AS stats_age_s
FROM pg_stat_bgwriter
"""

Q["checkpoints17"] = r"""
SELECT num_timed AS timed, num_requested AS req,
       extract(epoch FROM now() - stats_reset)::bigint AS stats_age_s
FROM pg_stat_checkpointer
"""

Q["pgss_ext"] = r"""
SELECT n.nspname::text AS schema, e.extversion AS version,
       (SELECT setting FROM pg_settings WHERE name = 'shared_preload_libraries') LIKE '%pg_stat_statements%' AS preloaded
FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
WHERE e.extname = 'pg_stat_statements'
"""

PGSS_TEMPLATE = r"""
WITH s AS (
  SELECT queryid, calls, {total} AS total_ms, {mean} AS mean_ms, {max} AS max_ms, rows,
         shared_blks_read, shared_blks_hit, temp_blks_written,
         left(regexp_replace(query, '\s+', ' ', 'g'), 300) AS query
  FROM {schema}pg_stat_statements
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
WHERE rn_total <= {top} OR rn_calls <= {top} OR rn_read <= {top}
ORDER BY rn_total
"""

Q["tls"] = r"""
SELECT coalesce(a.application_name, '') AS app, coalesce(host(a.client_addr), 'local') AS addr,
       coalesce(a.usename, '') AS usr, s.ssl, coalesce(s.version, '') AS tls_version, count(*) AS n
FROM pg_stat_activity a JOIN pg_stat_ssl s ON s.pid = a.pid
WHERE a.backend_type = 'client backend' AND a.pid <> pg_backend_pid()
GROUP BY 1, 2, 3, 4, 5
ORDER BY 6 DESC
"""

Q["tablespaces"] = r"""
SELECT spcname::text AS name, pg_tablespace_location(oid) AS location
FROM pg_tablespace
WHERE pg_tablespace_location(oid) <> ''
"""

Q["db_sizes"] = r"""
SELECT datname::text AS datname,
       CASE WHEN has_database_privilege(oid, 'CONNECT') THEN pg_database_size(oid) END AS bytes
FROM pg_database
WHERE datallowconn AND NOT datistemplate
ORDER BY 2 DESC NULLS LAST
"""

Q["top_tables"] = r"""
SELECT c.oid::regclass::text AS rel, pg_total_relation_size(c.oid) AS total_bytes,
       pg_relation_size(c.oid) AS heap_bytes, pg_indexes_size(c.oid) AS index_bytes,
       c.reltuples::bigint AS est_rows
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'm', 'p')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname !~ '^pg_toast'
ORDER BY pg_total_relation_size(c.oid) DESC
LIMIT 10
"""

PREAMBLE = """SET client_encoding = 'UTF8';
SET application_name = '%s';
SET default_transaction_read_only = on;
SET statement_timeout = '%ds';
SET lock_timeout = '1s';
SET idle_in_transaction_session_timeout = '60s';
"""


# --- Вспомогательное ---

SQL_PASSWORD = re.compile(r"(?i)\b(password|passwd)(\s+)(E?'(?:[^']|'')*'|\"[^\"]*\")")
# Запасной вариант, если копии mask_secrets.py рядом нет: KEY=значение для очевидных имён.
FALLBACK = re.compile(r"(?i)\b([\w.\-]*(?:password|passwd|pwd|secret|token|access_key)[\w.\-]*\s*[=:]\s*)([^\s;'\"]+)")


# Присваивание переменной внутри команды: env AWS_SECRET_ACCESS_KEY=... wal-g wal-push %p
INLINE_ENV = re.compile(r"(?<![\w$])([A-Za-z_][A-Za-z0-9_]*(?:PASSWORD|PASSWD|PASS|PWD|SECRET|TOKEN|ACCESS_KEY|SECRET_KEY|"
                        r"password|secret|token)[A-Za-z0-9_]*=)(\"[^\"]*\"|'[^']*'|[^\s;|&\"']+)")


def mask(text):
    """Прячет пароли в тексте запросов и команд: PASSWORD '...' и всё, что ловит mask_secrets."""
    if not text:
        return text
    text = SQL_PASSWORD.sub(lambda m: m.group(1) + m.group(2) + "'‹скрыто›'", text)
    text = INLINE_ENV.sub(lambda m: m.group(1) + ("‹скрыто›" if not m.group(2).startswith("$") else m.group(2)), text)
    if _ms is not None:
        return _ms.mask(text)[0]
    return FALLBACK.sub(lambda m: m.group(1) + "‹скрыто›", text)


def dur(s):
    if s is None:
        return "нет данных"
    s = int(s)
    if s < 60:
        return "%d с" % max(s, 0)
    m = s // 60
    if m < 60:
        return "%d мин" % m
    h, m = divmod(m, 60)
    if h < 48:
        return "%d ч %d мин" % (h, m) if m else "%d ч" % h
    d, h = divmod(h, 24)
    return "%d дн %d ч" % (d, h) if h else "%d дн" % d


def size(b):
    if b is None:
        return "нет данных"
    b = float(b)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(b) < 1024 or unit == "ТБ":
            if unit == "Б":
                return "%d Б" % b
            return ("%.1f %s" % (b, unit)).replace(".", ",")
        b /= 1024.0


def num(n):
    if n is None:
        return "нет данных"
    if isinstance(n, float) and not n.is_integer():
        return ("%.1f" % n).replace(".", ",")
    return "{:,}".format(int(n)).replace(",", " ")


def pct(x):
    return ("%.1f" % x).replace(".", ",").replace(",0", "") + " %"


def lsn_int(lsn):
    """'16/B374D848' -> число байт."""
    if not lsn or "/" not in lsn:
        return None
    hi, lo = lsn.split("/", 1)
    try:
        return (int(hi, 16) << 32) + int(lo, 16)
    except ValueError:
        return None


def wal_seg(name):
    """Имя сегмента WAL -> (таймлайн, номер сегмента) для сегментов по 16 МБ."""
    if not name or not re.match(r"^[0-9A-F]{24}$", name):
        return None
    tli, log, seg = int(name[:8], 16), int(name[8:16], 16), int(name[16:24], 16)
    return tli, log * 0x100 + seg


UNIT_BYTES = {"B": 1, "kB": KIB, "MB": MIB, "GB": GIB, "TB": 1024 * GIB}
UNIT_MS = {"us": 0.001, "ms": 1, "s": 1000, "min": 60000, "h": 3600000, "d": 86400000}


def setting_bytes(row):
    """Значение параметра памяти в байтах по полю unit из pg_settings (8kB, kB, MB). -1 остаётся -1."""
    if not row:
        return None
    m = re.match(r"^(\d+)?\s*(B|kB|MB|GB|TB)$", row.get("unit") or "")
    try:
        val = int(row.get("setting"))
    except (TypeError, ValueError):
        return None
    if not m:
        return None
    if val < 0:
        return val
    return val * int(m.group(1) or 1) * UNIT_BYTES[m.group(2)]


def setting_ms(row):
    if not row:
        return None
    try:
        val = float(row.get("setting"))
    except (TypeError, ValueError):
        return None
    unit = row.get("unit") or "ms"
    return val * UNIT_MS.get(unit, 1) if val >= 0 else val


class QueryError(Exception):
    pass


def first_error(err):
    """Самая содержательная строка из stderr psql без префикса psql:<stdin>:N:."""
    lines = [l.strip() for l in err.splitlines() if l.strip()]
    for want in ("FATAL:", "ERROR:", "could not", "connection to server", "psql:"):
        for l in lines:
            if want in l:
                return mask(re.sub(r"^psql:(<stdin>|[^:]*):\d+:\s*", "", l))
    return mask(lines[0]) if lines else ""


def parse_json_output(out):
    s = out.strip()
    m = re.search(r"(?m)^\[", s)
    if not m:
        raise QueryError("psql вернул неожиданный вывод: %s" % mask(s[:200]))
    return json.loads(s[m.start():])


class Psql(object):
    """Каждый запрос отдельным вызовом psql: ошибка одного не мешает остальным."""

    def __init__(self, base_cmd, dbname=None, timeout_s=10):
        self.base = list(base_cmd)
        self.dbname = dbname
        self.timeout_s = int(timeout_s)

    def script(self, sql):
        return (PREAMBLE % (APP, self.timeout_s) +
                "SELECT coalesce(json_agg(q), '[]'::json) FROM (\n%s\n) q;\n" % sql.strip().rstrip(";"))

    def run(self, sql):
        cmd = self.base + ["-X", "-q", "-A", "-t", "-w", "-v", "ON_ERROR_STOP=1", "-f", "-"]
        if self.dbname:
            cmd += ["-d", self.dbname]
        env = dict(os.environ)
        env["PGCLIENTENCODING"] = "UTF8"
        env["PGAPPNAME"] = APP
        env.setdefault("PGCONNECT_TIMEOUT", "10")
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        except OSError as e:
            raise QueryError("не удалось запустить %s: %s" % (cmd[0], e))
        try:
            out, err = p.communicate(self.script(sql).encode("utf-8"), timeout=self.timeout_s + 30)
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()
            raise QueryError("psql не ответил за %d с" % (self.timeout_s + 30))
        out, err = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
        if p.returncode != 0:
            raise QueryError(first_error(err) or "psql завершился с кодом %d" % p.returncode)
        return parse_json_output(out)


class Result(object):
    def __init__(self, key, title):
        self.key, self.title = key, title
        self.status, self.summary, self.details, self.data = "OK", "", [], {}

    def bump(self, status):
        if RANK[status] > RANK[self.status]:
            self.status = status

    def as_dict(self):
        return {"key": self.key, "title": self.title, "status": self.status, "summary": self.summary,
                "details": self.details, "data": self.data}


class Ctx(object):
    """Всё, что собрали запросы: результаты по имени, сервер, настройки, два снимка статистики."""

    def __init__(self, th, top=10, window_s=0, local=True):
        self.th, self.top, self.window_s, self.local = th, top, window_s, local
        self.q, self.server, self.settings = {}, {}, {}
        self.snap1 = self.snap2 = None
        self.window_real = 0.0
        self.local_info = None

    def rows(self, name):
        v = self.q.get(name)
        if isinstance(v, QueryError):
            raise v
        if v is None:
            raise QueryError("запрос %s не выполнялся" % name)
        return v

    def err(self, name):
        v = self.q.get(name)
        return str(v) if isinstance(v, QueryError) else None

    def s(self, name):
        return self.settings.get(name) or {}

    def sv(self, name, default=""):
        return (self.settings.get(name) or {}).get("setting", default)

    @property
    def major(self):
        return int(self.server.get("version_num", 0)) // 10000

    @property
    def replica(self):
        return bool(self.server.get("replica"))


def limited(ctx):
    """Без суперпользователя и pg_monitor (pg_read_all_stats) чужие сессии видны без состояния и запроса."""
    return not ctx.server.get("superuser") and not ctx.server.get("pg_monitor")


LIMITED = "чужие сессии не видны без роли pg_monitor: проверка неполная"


def short(text, n=160):
    text = mask(text or "")
    return text if len(text) <= n else text[:n - 1] + "…"


def who(r):
    app = r.get("app") or "-"
    return "pid %s %s %s@%s" % (r.get("pid"), app, r.get("usr") or "?", r.get("addr") or "?")


# --- Проверки ---

def check_server(ctx):
    r = Result("server", "Сервер")
    s = ctx.server
    role = "реплика (режим восстановления)" if ctx.replica else "ведущий сервер"
    r.summary = "%s, база %s, пользователь %s, работает %s" % (role, s.get("db"), s.get("usr"), dur(s.get("uptime_s")))
    r.data = {"replica": ctx.replica, "db": s.get("db"), "uptime_s": s.get("uptime_s"),
              "rx_tables": s.get("rx_tables"), "superuser": s.get("superuser"), "pg_monitor": s.get("pg_monitor")}
    if int(s.get("rx_tables") or 0) == 0:
        r.bump("WARN")
        r.details.append("в базе %s нет таблиц sungero_*: похоже, это не база RX, проверки таблиц к RX не относятся. "
                         "Укажите базу RX ключом -d (имя есть в CONNECTION_STRING, параметр Database)" % s.get("db"))
        try:
            dbs = [d for d in ctx.rows("db_sizes") if d.get("datname") not in ("postgres",)]
            if dbs:
                r.details.append("базы на сервере: " + ", ".join("%s %s" % (d["datname"], size(d.get("bytes"))) for d in dbs[:8]))
        except QueryError:
            pass
    if int(s.get("uptime_s") or 0) < 3600:
        r.bump("INFO")
        r.details.append("сервер перезапускался меньше часа назад: в логах RX в это время ожидаемы 57P01 и 57P03, "
                         "причину перезапуска ищите в журнале PostgreSQL")
    if not s.get("superuser") and not s.get("pg_monitor"):
        r.bump("INFO")
        r.details.append("у пользователя нет роли pg_monitor: тексты чужих запросов, каталог WAL и часть настроек не видны")
    return r


def check_version(ctx):
    r = Result("version", "Версия")
    s = ctx.server
    ver = s.get("version") or ""
    m = re.match(r"^(\S+ \S+)", ver)
    r.summary = m.group(1) if m else ver[:40]
    major = ctx.major
    r.data = {"version_num": s.get("version_num"), "major": major, "version": ver}
    fork = None
    for name in ("Postgres Pro", "Tantor", "Jatoba"):
        if name.lower() in ver.lower():
            fork = name
    if fork:
        r.bump("INFO")
        r.details.append("сборка %s: сроки поддержки смотрите у её поставщика" % fork)
    elif major in EOL:
        eol = datetime.strptime(EOL[major], "%Y-%m-%d").date()
        days = (eol - date.today()).days
        r.data["eol"] = EOL[major]
        if days < 0:
            r.bump("WARN")
            r.details.append("PostgreSQL %d не поддерживается сообществом с %s: исправлений безопасности больше не будет"
                             % (major, EOL[major]))
        elif days < ctx.th["eol_soon_days"]:
            r.bump("INFO")
            r.details.append("поддержка PostgreSQL %d заканчивается %s, через %d дн." % (major, EOL[major], days))
        else:
            r.summary += ", поддержка до %s" % EOL[major]
    elif major and major < min(EOL):
        r.bump("WARN")
        r.details.append("очень старая мажорная версия, давно без поддержки")
    r.details.append("какие версии PostgreSQL поддерживает ваша версия RX, сказано в документе вендора "
                     "«Типовые требования к аппаратному и программному обеспечению»")
    return r


def check_connections(ctx):
    r = Result("connections", "Соединения")
    rows = ctx.rows("connections")
    maxc = int(ctx.sv("max_connections", "0") or 0)
    reserved = int(ctx.sv("superuser_reserved_connections", "0") or 0) + int(ctx.sv("reserved_connections", "0") or 0)
    total = sum(int(x["n"]) for x in rows) + 1
    by_state, by_app = {}, {}
    for x in rows:
        st = x["state"] or "нет прав"
        by_state[st] = by_state.get(st, 0) + int(x["n"])
        a = by_app.setdefault(x["app"] or "(без имени)", {})
        a[st] = a.get(st, 0) + int(x["n"])
    used = 100.0 * total / maxc if maxc else 0
    avail = maxc - reserved
    # своя сессия могла занять резервное место суперпользователя: обычным ролям считаем остальных
    app_pct = 100.0 * (total - 1) / avail if avail > 0 else used
    worst = max(used, app_pct)
    r.data = {"total": total, "max_connections": maxc, "reserved": reserved, "pct": round(used, 1),
              "app_available": avail, "app_pct": round(app_pct, 1), "by_state": by_state}
    states = ", ".join("%s %d" % (k, v) for k, v in sorted(by_state.items(), key=lambda kv: -kv[1]))
    r.summary = "%d из %d (%s); %s" % (total, maxc, pct(used), states or "других сессий нет")
    if reserved and app_pct >= ctx.th["conn_warn_pct"]:
        r.summary += "; обычным ролям доступно %d, занято %d (%s)" % (avail, total - 1, pct(app_pct))
    if worst >= ctx.th["conn_crit_pct"]:
        r.bump("CRIT")
    elif worst >= ctx.th["conn_warn_pct"]:
        r.bump("WARN")
    if app_pct >= 100:
        r.details.append("обычные роли уже получают remaining connection slots are reserved: RX не может открыть "
                         "новые соединения")
    if limited(ctx):
        r.bump("INFO")
        r.details.append(LIMITED + "; состояния чужих сессий записаны как «нет прав»")
    if r.status in ("WARN", "CRIT"):
        r.details.append("при исчерпании лимита новые подключения RX получают too many clients already; "
                         "%d мест оставлено для суперпользователя" % reserved)
    top = sorted(by_app.items(), key=lambda kv: -sum(kv[1].values()))[:5]
    for app, st in top:
        r.details.append("%s: %d (%s)" % (short(app, 60), sum(st.values()),
                                          ", ".join("%s %d" % kv for kv in sorted(st.items(), key=lambda kv: -kv[1]))))
    if maxc == 100:
        r.bump("INFO")
        r.details.append("max_connections = 100 по умолчанию: для RX мало уже на сотне пользователей")
    return r


def _sessions(ctx, idle):
    rows = ctx.rows("activity")
    out = []
    for x in rows:
        is_idle = (x.get("state") or "").startswith("idle in transaction")
        if idle and is_idle:
            out.append((int(x.get("state_s") or 0), x))
        elif not idle:
            out.append((int(x.get("xact_s") or 0), x))
    out.sort(key=lambda t: -t[0])
    return out


def check_long_xact(ctx):
    r = Result("long_xact", "Долгие транзакции")
    th_w, th_c = ctx.th["xact_warn_s"], ctx.th["xact_crit_s"]
    found = [(age, x) for age, x in _sessions(ctx, idle=False) if age >= th_w]
    r.data = {"count": len(found), "max_s": found[0][0] if found else 0}
    if not found:
        r.summary = "дольше %s нет" % dur(th_w)
        if limited(ctx):
            r.bump("INFO")
            r.summary = LIMITED
    if found:
        r.bump("CRIT" if found[0][0] >= th_c else "WARN")
        r.summary = "%d дольше %s, самая старая %s" % (len(found), dur(th_w), dur(found[0][0]))
        for age, x in found[:ctx.top]:
            r.details.append("%s, %s, транзакция %s: %s" % (who(x), x.get("state") or "?", dur(age), short(x.get("query"))))
        r.details.append("долгая транзакция держит блокировки и не даёт autovacuum чистить строки")
    try:
        prep = ctx.rows("prepared")
    except QueryError:
        prep = []
    old = [p for p in prep if int(p.get("age_s") or 0) >= ctx.th["prepared_warn_s"]]
    r.data["prepared"] = len(old)
    if old:
        r.bump("WARN")
        r.summary += "; подготовленных транзакций (2PC) %d" % len(old)
        for p in old[:5]:
            r.details.append("подготовленная транзакция %s (%s, база %s) висит %s: обычно такие завершаются сразу, "
                             "зависшая держит блокировки и горизонт очистки"
                             % (short(p.get("gid"), 60), p.get("owner"), p.get("database"), dur(p.get("age_s"))))
    return r


def check_idle(ctx):
    r = Result("idle_in_xact", "Сессии idle in transaction")
    th_w, th_c = ctx.th["idle_warn_s"], ctx.th["idle_crit_s"]
    found = [(age, x) for age, x in _sessions(ctx, idle=True) if age >= th_w]
    r.data = {"count": len(found), "max_s": found[0][0] if found else 0,
              "timeout": ctx.sv("idle_in_transaction_session_timeout")}
    if not found:
        r.summary = "дольше %s нет" % dur(th_w)
        if limited(ctx):
            r.bump("INFO")
            r.summary = LIMITED
        return r
    r.bump("CRIT" if found[0][0] >= th_c else "WARN")
    r.summary = "%d дольше %s, самая старая %s" % (len(found), dur(th_w), dur(found[0][0]))
    for age, x in found[:ctx.top]:
        r.details.append("%s молчит %s, транзакция %s: %s" % (who(x), dur(age), dur(x.get("xact_s")), short(x.get("query"))))
    r.details.append("приложение открыло транзакцию и не закрывает: последний запрос выше показывает место в коде")
    if ctx.sv("idle_in_transaction_session_timeout") == "0":
        r.details.append("idle_in_transaction_session_timeout не задан")
    return r


def check_locks(ctx):
    r = Result("locks", "Блокировки")
    rows = ctx.rows("locks")
    waiting = [x for x in rows if x.get("role") == "waiting"]
    info = dict((x["pid"], x) for x in rows)
    if not waiting:
        r.summary = "ожидающих блокировку сессий нет"
        return r
    if limited(ctx):
        r.details.append(LIMITED + ": время ожидания и запросы чужих сессий скрыты")
    maxw = max(int(x.get("wait_s") or 0) for x in waiting)
    long_ = [x for x in waiting if int(x.get("wait_s") or 0) >= ctx.th["lock_warn_s"]]
    r.data = {"waiting": len(waiting), "max_wait_s": maxw}
    if maxw >= ctx.th["lock_crit_s"]:
        r.bump("CRIT")
    elif long_:
        r.bump("WARN")
    else:
        r.bump("INFO")
    # корни цепочек: блокирующие, которые сами ничего не ждут
    roots = {}
    for x in waiting:
        for b in x.get("blocked_by") or []:
            seen, cur = set(), b
            while cur in info and info[cur].get("blocked_by") and cur not in seen:
                seen.add(cur)
                cur = info[cur]["blocked_by"][0]
            roots.setdefault(cur, set()).add(x["pid"])
    r.summary = "ждут %d, самое долгое ожидание %s" % (len(waiting), dur(maxw))
    if roots:
        r.summary += ", корней цепочек %d" % len(roots)
    r.data["roots"] = sorted(roots)
    for root, victims in sorted(roots.items(), key=lambda kv: -len(kv[1]))[:ctx.top]:
        x = info.get(root, {"pid": root})
        line = "корень pid %s (%s, %s, транзакция %s) держит %d сесс." % (
            root, x.get("app") or x.get("backend_type") or "?", x.get("state") or "?", dur(x.get("xact_s")), len(victims))
        if x.get("exclusive_on"):
            line += ", AccessExclusiveLock на %s" % short(x["exclusive_on"], 80)
        r.details.append(line + ": " + short(x.get("query")))
    for x in sorted(waiting, key=lambda y: -int(y.get("wait_s") or 0))[:ctx.top]:
        r.details.append("%s ждёт %s, блокируют %s: %s" % (who(x), dur(x.get("wait_s")),
                                                          ", ".join(str(p) for p in x.get("blocked_by") or []) or "?",
                                                          short(x.get("query"))))
    return r


def _db_delta(ctx, field, only_current=False):
    if not ctx.snap1 or not ctx.snap2:
        return None
    a = dict((x["datname"], x) for x in ctx.snap1)
    total = 0
    for x in ctx.snap2:
        if only_current and not x.get("current"):
            continue
        if x["datname"] in a:
            total += int(x.get(field) or 0) - int(a[x["datname"]].get(field) or 0)
    return total


def check_deadlocks(ctx):
    r = Result("deadlocks", "Дедлоки (40P01)")
    rows = ctx.snap2 or ctx.snap1 or ctx.rows("dbstats")
    worst = None
    for x in rows:
        days = max(float(x.get("stats_age_s") or 0) / 86400.0, 1.0)  # меньше суток считаем как сутки
        rate = int(x.get("deadlocks") or 0) / days
        x["_rate"] = rate
        if worst is None or rate > worst["_rate"]:
            worst = x
    total = sum(int(x.get("deadlocks") or 0) for x in rows)
    delta = _db_delta(ctx, "deadlocks")
    r.data = {"total": total, "window": delta,
              "per_db": dict((x["datname"], int(x.get("deadlocks") or 0)) for x in rows)}
    if not worst or total == 0:
        r.summary = "за время статистики дедлоков нет"
    else:
        since = "с последнего сброса статистики" if worst.get("was_reset") else "за время сбора статистики"
        if float(worst.get("stats_age_s") or 0) < 86400:
            r.summary = "%d %s (%s, меньше суток), больше всего в базе %s" % (
                total, since, dur(worst.get("stats_age_s")), worst["datname"])
        else:
            r.summary = "%d %s (%s); в базе %s около %s в сутки" % (
                total, since, dur(worst.get("stats_age_s")), worst["datname"], num(round(worst["_rate"], 1)))
        if worst["_rate"] >= ctx.th["deadlock_crit_per_day"]:
            r.bump("CRIT")
        elif worst["_rate"] >= ctx.th["deadlock_warn_per_day"]:
            r.bump("WARN")
        else:
            r.bump("INFO")
    if delta:
        r.bump("WARN")
        r.details.append("за окно замера %d с новых дедлоков: %d" % (int(ctx.window_real), delta))
    if r.status in ("WARN", "CRIT", "INFO") and total:
        if ctx.sv("log_lock_waits") != "on":
            r.details.append("включите log_lock_waits = on, чтобы в журнале были обе стороны дедлока")
        r.details.append("дедлок чинится порядком обращения к записям в прикладном коде; соберите DETAIL и CONTEXT из журнала")
    return r


def check_wal(ctx):
    r = Result("wal", "Каталог pg_wal")
    rows = ctx.rows("wal_dir")
    w = rows[0] if rows else {"files": 0, "bytes": 0}
    maxw = setting_bytes(ctx.s("max_wal_size")) or GIB
    keep = max(setting_bytes(ctx.s("wal_keep_size")) or 0, 0)
    limit = maxw + keep
    ratio = float(w["bytes"]) / limit if limit else 0
    r.data = {"files": w["files"], "bytes": w["bytes"], "max_wal_size": maxw, "wal_keep_size": keep,
              "ratio": round(ratio, 2)}
    r.summary = "%s в %s файлах; max_wal_size %s" % (size(w["bytes"]), num(w["files"]), size(maxw))
    if keep:
        r.summary += ", wal_keep_size %s" % size(keep)
    if ratio >= ctx.th["wal_crit_ratio"]:
        r.bump("CRIT")
    elif ratio >= ctx.th["wal_warn_ratio"]:
        r.bump("WARN")
    if r.status in ("WARN", "CRIT"):
        r.details.append("журнал не удаляется: причину ищите в проверках archiver (очередь .ready) и slots "
                         "(удержание слотами); файлы из pg_wal руками не удалять")
    if ctx.snap1 and ctx.snap2 and not ctx.replica:
        a = lsn_int(next((x.get("wal_lsn") for x in ctx.snap1 if x.get("wal_lsn")), None))
        b = lsn_int(next((x.get("wal_lsn") for x in ctx.snap2 if x.get("wal_lsn")), None))
        if a is not None and b is not None and ctx.window_real > 0 and b > a:
            per_h = (b - a) / ctx.window_real * 3600
            r.data["wal_bytes_per_hour"] = int(per_h)
            r.details.append("за окно %d с записано %s WAL, грубо %s в час; для расчёта места под архив "
                             "мерьте разницу pg_current_wal_lsn() за час в рабочее время"
                             % (int(ctx.window_real), size(b - a), size(per_h)))
    return r


def check_archiver(ctx):
    r = Result("archiver", "Архивация WAL")
    mode = ctx.sv("archive_mode", "off")
    cmd = (ctx.sv("archive_command") or "").strip()
    lib = (ctx.sv("archive_library") or "").strip()
    r.data = {"archive_mode": mode, "archive_command": mask(cmd), "archive_library": lib}
    if mode == "off":
        r.bump("INFO")
        r.summary = "archive_mode = off: восстановление только на момент последней полной копии"
        return r
    if not cmd and not lib:
        r.bump("CRIT")
        r.summary = "archive_mode = %s, а archive_command пуст: сегменты копятся в pg_wal" % mode
        return r
    a = ctx.rows("archiver")[0]
    r.data.update(a)
    ready = None
    if ctx.err("archive_status") is None and ctx.q.get("archive_status"):
        ready = int(ctx.q["archive_status"][0].get("ready") or 0)
    else:
        cur, last = wal_seg(a.get("current_wal")), wal_seg(a.get("last_archived_wal"))
        if cur and last and cur[0] == last[0]:
            ready = max(cur[1] - last[1] - 1, 0)
            r.data["ready_estimated"] = True
    r.data["ready"] = ready
    if ctx.replica and mode != "always":
        r.bump("INFO")
        r.summary = "реплика с archive_mode = %s: архивирует ведущий сервер" % mode
        return r
    parts = ["последний успех %s назад (%s)" % (dur(a.get("archived_age_s")), a.get("last_archived_wal") or "нет")
             if a.get("archived_age_s") is not None else "успешных архиваций нет"]
    if ready is not None:
        parts.append("ждут архивации %d" % ready)
    if int(a.get("failed_count") or 0):
        parts.append("ошибок %d, последняя %s назад" % (int(a["failed_count"]), dur(a.get("failed_age_s"))))
    r.summary = "; ".join(parts)
    r.details.append("команда: %s" % short(cmd or lib, 160))
    if a.get("failing_now"):
        r.bump("CRIT")
        r.details.append("архивация падает прямо сейчас (последняя ошибка позже последнего успеха), сегмент %s. "
                         "Выполните archive_command руками под postgres и прочитайте ошибку: место, права, сеть"
                         % (a.get("last_failed_wal") or "?"))
    if ready is not None:
        if ready >= ctx.th["ready_crit"]:
            r.bump("CRIT")
        elif ready >= ctx.th["ready_warn"]:
            r.bump("WARN")
        if ready > 0 and a.get("archived_age_s") is None:
            r.bump("WARN")
            r.details.append("есть очередь на архивацию, а успешных архиваций за время статистики нет")
        elif ready > 0 and int(a["archived_age_s"]) >= ctx.th["archive_stale_s"]:
            r.bump("WARN")
            r.details.append("есть очередь на архивацию, а последний успех старше %s" % dur(ctx.th["archive_stale_s"]))
    if r.status in ("WARN", "CRIT"):
        r.details.append("пока архив стоит, pg_wal растёт и точка восстановления не двигается; "
                         "archive_mode не выключать, archive_command не очищать")
    return r


def check_slots(ctx):
    r = Result("slots", "Слоты репликации")
    rows = ctx.rows("slots")
    keep = ctx.sv("max_slot_wal_keep_size", "-1")
    r.data = {"slots": rows, "max_slot_wal_keep_size": keep}
    if not rows:
        r.summary = "слотов нет"
        return r
    inactive = [x for x in rows if not x.get("active")]
    retained = max(int(x.get("retained_bytes") or 0) for x in rows)
    r.summary = "%d, неактивных %d, наибольшее удержание %s" % (len(rows), len(inactive), size(retained))
    for x in rows:
        ret = int(x.get("retained_bytes") or 0)
        st = (x.get("wal_status") or "").lower()
        line = "%s (%s%s): %s, удерживает %s" % (x["slot_name"], x.get("slot_type"),
                                               ", база " + x["database"] if x.get("database") else "",
                                               "активен" if x.get("active") else "неактивен", size(ret))
        if st:
            line += ", wal_status %s" % st
        if st == "lost":
            r.bump("CRIT")
            line += ": слот потерял нужный WAL, потребителя придётся пересоздать"
        elif st == "unreserved":
            r.bump("CRIT")
            line += ": WAL слота вот-вот удалится"
        elif st == "extended":
            r.bump("WARN")
        if ret >= ctx.th["slot_crit_bytes"]:
            r.bump("CRIT")
        elif ret >= ctx.th["slot_warn_bytes"] or not x.get("active"):
            r.bump("WARN")
        r.details.append(line)
    if inactive:
        r.details.append("неактивный слот держит WAL, пока не кончится диск: если потребителя больше нет, слот удаляют "
                         "(pg_drop_replication_slot, только после проверки, что реплика не нужна)")
    if keep in ("-1", ""):
        r.bump("INFO")
        r.details.append("max_slot_wal_keep_size = -1: предела удержания нет; с пределом выключенная реплика "
                         "сломается сама, но не заполнит диск ведущего")
    return r


def check_replication(ctx):
    r = Result("replication", "Реплики")
    if ctx.replica:
        x = ctx.rows("receiver")[0]
        r.data = x
        status = x.get("status") or ""
        backlog = int(x.get("replay_backlog_bytes") or 0)
        r.summary = "приём WAL: %s%s; не применено %s; последняя применённая транзакция %s назад" % (
            status or "нет потока", " от " + x["sender_host"] if x.get("sender_host") else "", size(backlog),
            dur(x.get("last_replay_age_s")))
        if status != "streaming":
            r.bump("WARN")
            r.details.append("реплика не получает WAL потоком: если она не настроена брать WAL из архива "
                             "(restore_command), она отстаёт")
        if x.get("paused"):
            r.bump("WARN")
            r.details.append("применение WAL приостановлено (pg_wal_replay_pause)")
        if backlog >= ctx.th["repl_crit_bytes"]:
            r.bump("CRIT")
        elif backlog >= ctx.th["repl_warn_bytes"]:
            r.bump("WARN")
        r.details.append("время последней транзакции ночью растёт и у исправной реплики; надёжнее мерить отставание в байтах")
        return r
    rows = ctx.rows("replication")
    r.data = {"replicas": rows}
    if not rows:
        r.summary = "реплик не подключено"
        try:
            phys = [s for s in ctx.rows("slots") if s.get("slot_type") == "physical"]
        except QueryError:
            phys = []
        if phys:
            r.bump("WARN")
            r.details.append("есть физические слоты (%s), а реплик в pg_stat_replication нет: реплика выключена или "
                             "не может подключиться" % ", ".join(s["slot_name"] for s in phys[:5]))
        return r
    worst = max(int(x.get("replay_lag_bytes") or 0) for x in rows)
    r.summary = "%d, наибольшее отставание %s" % (len(rows), size(worst))
    for x in rows:
        lag = int(x.get("replay_lag_bytes") or 0)
        r.details.append("%s %s: %s, %s, не применено %s%s" % (
            x.get("app") or "-", x.get("addr"), x.get("state"), x.get("sync_state"), size(lag),
            ", replay_lag %s с" % num(x["replay_lag_s"]) if x.get("replay_lag_s") is not None else ""))
        if x.get("state") != "streaming":
            r.bump("WARN")
        if lag >= ctx.th["repl_crit_bytes"]:
            r.bump("CRIT")
        elif lag >= ctx.th["repl_warn_bytes"]:
            r.bump("WARN")
    sync = (ctx.sv("synchronous_standby_names") or "").strip()
    sync_rows = [x for x in rows if x.get("sync_state") in ("sync", "quorum")]
    if sync and len(rows) == 1 and sync_rows:
        r.bump("WARN")
        r.details.append("единственная реплика синхронная: при её выключении запись в базу встанет; "
                         "синхронную схему делают на двух репликах с ANY 1 (...)")
    if ctx.sv("wal_log_hints") != "on" and ctx.sv("data_checksums") != "on":
        r.bump("INFO")
        r.details.append("wal_log_hints и контрольные суммы выключены: вернуть старый мастер через pg_rewind не получится")
    return r


def _mount_of(path, mounts):
    best = None
    for dev, mp, fstype, opts in mounts:
        if path == mp or path.startswith(mp.rstrip("/") + "/") or mp == "/":
            if best is None or len(mp) > len(best[1]):
                best = (dev, mp, fstype, opts)
    return best


def read_mounts(path="/proc/self/mounts"):
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                p = line.split()
                if len(p) >= 4:
                    out.append((p[0], p[1].replace("\\040", " "), p[2], p[3]))
    except OSError:
        pass
    return out


def local_data_dir(ctx, pgdata):
    """Каталог данных виден с этой машины и принадлежит тому же серверу: сверка postmaster.pid."""
    if not pgdata:
        return None, "нет прав на data_directory (нужна роль pg_monitor или суперпользователь)"
    try:
        with open(os.path.join(pgdata, "postmaster.pid"), encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return None, "каталог данных %s не виден с этой машины или нет прав (запустите под postgres)" % pgdata
    try:
        started = int(lines[2])
    except (IndexError, ValueError):
        return None, "не удалось прочитать postmaster.pid"
    if abs(started - int(ctx.server.get("started_epoch") or 0)) > 2:
        return None, "каталог %s на этой машине принадлежит другому экземпляру PostgreSQL" % pgdata
    return pgdata, None


def check_disk(ctx):
    r = Result("disk", "Диск и память сервера базы")
    if not ctx.local:
        r.status = "SKIP"
        r.summary = "база проверяется не с её сервера (или задан --psql-cmd, --no-disk): место смотрите df -h на сервере"
        return r
    pgdata, why = local_data_dir(ctx, ctx.sv("data_directory"))
    if not pgdata:
        r.status = "SKIP"
        r.summary = why + "; место смотрите df -h на сервере базы"
        return r
    mounts = read_mounts()
    paths = [("данные", pgdata), ("pg_wal", os.path.realpath(os.path.join(pgdata, "pg_wal")))]
    try:
        for t in ctx.rows("tablespaces"):
            paths.append(("табличное пространство " + t["name"], t["location"]))
    except QueryError:
        pass
    seen, parts = {}, []
    for label, p in paths:
        try:
            st, vfs = os.stat(p), os.statvfs(p)
        except OSError as e:
            r.details.append("%s %s: %s" % (label, p, e.strerror))
            continue
        if st.st_dev in seen:
            continue
        seen[st.st_dev] = label
        total = vfs.f_blocks * vfs.f_frsize
        free = vfs.f_bavail * vfs.f_frsize
        free_pct = 100.0 * free / total if total else 0
        inodes_pct = 100.0 * vfs.f_favail / vfs.f_files if vfs.f_files else 100
        m = _mount_of(p, mounts)
        parts.append("%s: свободно %s из %s (%s)" % (label, size(free), size(total), pct(free_pct)))
        r.data[label] = {"path": p, "free_bytes": free, "total_bytes": total, "free_pct": round(free_pct, 1),
                         "mount": m[1] if m else None, "fstype": m[2] if m else None}
        if free_pct < ctx.th["disk_crit_free_pct"]:
            r.bump("CRIT")
        elif free_pct < ctx.th["disk_warn_free_pct"]:
            r.bump("WARN")
        if inodes_pct < 10:
            r.bump("WARN")
            r.details.append("%s: свободных инодов %s" % (label, pct(inodes_pct)))
        if m and "secdel" in m[3].split(","):
            r.bump("WARN")
            r.details.append("%s: раздел %s смонтирован с secdel: удаление и усечение файлов затирает блоки, "
                             "накат WAL на реплике и чистка становятся очень медленными" % (label, m[1]))
    if len(seen) == 1:
        r.details.append("pg_wal на том же разделе, что и данные: переполнение журнала остановит всю базу; "
                         "отдельный раздел под pg_wal даёт запас для манёвра")
    r.summary = "; ".join(parts) or "не удалось посмотреть разделы"
    mem = read_meminfo()
    if mem:
        sb = setting_bytes(ctx.s("shared_buffers")) or 0
        wm = setting_bytes(ctx.s("work_mem")) or 0
        conn = int(ctx.sv("max_connections", "0") or 0)
        peak = sb + conn * 10 * MIB + conn * wm * 0.25
        r.data["mem_total"] = mem
        r.data["mem_peak_estimate"] = int(peak)
        line = "память сервера %s; оценка под нагрузкой %s (shared_buffers + 10 МБ и четверть work_mem на соединение)" % (
            size(mem), size(peak))
        if peak > mem * 0.9:
            r.bump("WARN")
            line += ": запаса нет, при пиках ядро может убить процесс базы (signal 9)"
        r.details.append(line)
    return r


def read_meminfo(path="/proc/meminfo"):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * KIB
    except (OSError, ValueError, IndexError):
        pass
    return None


def check_autovacuum(ctx):
    r = Result("autovacuum", "Autovacuum и мёртвые строки")
    if ctx.replica:
        r.status = "SKIP"
        r.summary = "на реплике autovacuum не работает, смотрите ведущий сервер"
        return r
    rows = ctx.rows("vacuum_tables")
    th = ctx.th
    if ctx.sv("autovacuum") != "on":
        r.bump("CRIT")
        r.details.append("autovacuum выключен: таблицы раздуваются, приближается wraparound")
    bad = []
    for x in rows:
        dead, dpct, heap = int(x.get("dead") or 0), float(x.get("dead_pct") or 0), int(x.get("heap_bytes") or 0)
        if dead < th["dead_min_rows"]:
            continue
        if dpct >= th["dead_crit_pct"] and heap >= GIB:
            r.bump("CRIT")
            bad.append(x)
        elif dpct >= th["dead_warn_pct"]:
            r.bump("WARN")
            bad.append(x)
    worst = rows[0] if rows else None
    r.summary = ("больше всего мёртвых строк: %s %s (%s)" % (worst["rel"], num(worst["dead"]), pct(float(worst["dead_pct"])))
                 if worst and int(worst.get("dead") or 0) else "мёртвых строк почти нет")
    if bad:
        r.summary = "%d таблиц с долей мёртвых строк от %d %%; " % (len(bad), th["dead_warn_pct"]) + r.summary
    for x in (bad or rows[:3])[:ctx.top]:
        if not int(x.get("dead") or 0):
            continue
        r.details.append("%s: мёртвых %s (%s), таблица %s, vacuum %s, analyze %s" % (
            x["rel"], num(x["dead"]), pct(float(x["dead_pct"])), size(x.get("heap_bytes")),
            dur(x["vacuum_age_s"]) + " назад" if x.get("vacuum_age_s") is not None else "не было",
            dur(x["analyze_age_s"]) + " назад" if x.get("analyze_age_s") is not None else "не было"))
    try:
        running = ctx.rows("vacuum_running")
    except QueryError:
        running = []
    workers = int(ctx.sv("autovacuum_max_workers", "3") or 3)
    auto = [x for x in running if x.get("auto")]
    if running:
        r.details.append("сейчас идёт vacuum: " + "; ".join(
            "%s %s %s%s" % (x.get("rel"), x.get("phase"), dur(x.get("running_s")),
                            " (защита от wraparound)" if x.get("wraparound") else "") for x in running[:5]))
    if workers and len(auto) >= workers:
        r.bump("INFO")
        r.details.append("заняты все %d воркера autovacuum: очистка может не успевать" % workers)
    try:
        off = ctx.rows("autovacuum_off")
    except QueryError:
        off = []
    if off:
        r.bump("WARN")
        r.details.append("autovacuum выключен на таблицах: " + ", ".join(x["rel"] for x in off[:10]))
    r.data = {"tables": rows[:ctx.top], "running": running, "autovacuum_off": off}
    return r


def check_wraparound(ctx):
    r = Result("wraparound", "Возраст транзакций (wraparound)")
    rows = ctx.rows("wrap_db")
    th = ctx.th
    top = rows[0] if rows else {"datname": "?", "xid_age": 0, "mxid_age": 0}
    mx = max(rows, key=lambda x: int(x.get("mxid_age") or 0)) if rows else top
    r.summary = "наибольший возраст %s у %s; multixact %s" % (num(top["xid_age"]), top["datname"], num(mx.get("mxid_age")))
    r.data = {"databases": rows}
    for val in (int(top.get("xid_age") or 0), int(mx.get("mxid_age") or 0)):
        if val >= th["xid_crit"]:
            r.bump("CRIT")
        elif val >= th["xid_warn"]:
            r.bump("WARN")
    freeze = int(ctx.sv("autovacuum_freeze_max_age", "200000000") or 200000000)
    if int(top.get("xid_age") or 0) > 2 * freeze and r.status == "OK":
        r.bump("INFO")
        r.details.append("возраст больше 2 × autovacuum_freeze_max_age (%s): заморозка отстаёт" % num(freeze))
    if r.status in ("WARN", "CRIT", "INFO"):
        try:
            for t in ctx.rows("wrap_tables"):
                r.details.append("старейшая таблица: %s, возраст %s, %s" % (t["rel"], num(t["xid_age"]), size(t["bytes"])))
        except QueryError:
            pass
        try:
            for h in ctx.rows("xmin_holders"):
                r.details.append("держит горизонт очистки: %s %s %s, возраст %s" % (
                    h["kind"], h["name"], h.get("app") or h.get("backend_type") or "", num(h.get("xid_age"))))
        except QueryError:
            pass
        if r.status in ("WARN", "CRIT"):
            r.details.append("у предела около 2,1 млрд база перестаёт принимать запись (to avoid wraparound data loss)")
    return r


def check_checkpoints(ctx):
    r = Result("checkpoints", "Чекпоинты")
    if ctx.replica:
        r.status = "SKIP"
        r.summary = "на реплике вместо чекпоинтов точки перезапуска, смотрите ведущий сервер"
        return r
    x = ctx.rows("checkpoints")[0]
    timed, req = int(x.get("timed") or 0), int(x.get("req") or 0)
    total = timed + req
    share = 100.0 * req / total if total else 0
    r.data = {"timed": timed, "requested": req, "requested_pct": round(share, 1)}
    r.summary = "по расписанию %d, по объёму WAL и по запросу %d (%s) за %s" % (timed, req, pct(share), dur(x.get("stats_age_s")))
    if total >= ctx.th["ckpt_min_total"] and share >= ctx.th["ckpt_req_warn_pct"]:
        r.bump("WARN")
        r.details.append("чекпоинты чаще всего вынужденные: max_wal_size (%s) мал для нагрузки, отклик проседает волнами"
                         % size(setting_bytes(ctx.s("max_wal_size"))))
    return r


def check_cache(ctx):
    r = Result("cache", "Попадание в кэш")
    cur = [x for x in (ctx.snap2 or ctx.snap1 or []) if x.get("current")]
    if not cur:
        r.status = "SKIP"
        r.summary = "нет статистики по текущей базе"
        return r
    c = cur[0]
    hit, read = int(c.get("blks_hit") or 0), int(c.get("blks_read") or 0)
    cum = 100.0 * hit / (hit + read) if hit + read else 100.0
    r.data = {"cumulative_pct": round(cum, 2)}
    if ctx.snap1 and ctx.snap2:
        dh, dr = _db_delta(ctx, "blks_hit", True), _db_delta(ctx, "blks_read", True)
        tb = _db_delta(ctx, "temp_bytes", True)
        win = 100.0 * dh / (dh + dr) if dh + dr else 100.0
        r.data.update({"window_s": round(ctx.window_real, 1), "window_hit": dh, "window_read": dr,
                       "window_pct": round(win, 2), "window_temp_bytes": tb})
        r.summary = "за %d с: %s обращений, с диска %s (%s), из кэша %s; накопительно %s" % (
            int(ctx.window_real), num(dh + dr), num(dr), size(dr * 8192), pct(win), pct(cum))
        if dr >= ctx.th["cache_min_read_blocks"] and win < ctx.th["cache_warn_pct"]:
            r.bump("WARN")
            r.details.append("база заметно читает с диска: запросы не помещаются в shared_buffers и кэш ОС, "
                             "ищите их по shared_blks_read в pg_stat_statements")
        if tb:
            r.bump("INFO")
            r.details.append("за окно во временные файлы записано %s: сортировкам не хватает work_mem" % size(tb))
    else:
        r.summary = "накопительно %s (без окна замера; запустите с --window, чтобы увидеть текущую картину)" % pct(cum)
    return r


def prefetch_statements(ctx, psql):
    """pg_stat_statements: схема и колонки зависят от версии расширения, запрос собирается на месте."""
    try:
        ext = psql.run(Q["pgss_ext"])
    except QueryError as e:
        ctx.q["pgss"] = e
        return
    ctx.q["pgss_ext"] = ext
    if not ext:
        return
    schema = ext[0]["schema"]
    schema_sql = schema if re.match(r"^[a-z_][a-z0-9_]*$", schema) else '"%s"' % schema.replace('"', '""')
    try:
        cols = psql.run("SELECT attname::text AS name FROM pg_attribute WHERE attrelid = '%s.pg_stat_statements'::regclass "
                        "AND attnum > 0 AND NOT attisdropped" % schema_sql.replace("'", "''"))
        names = set(c["name"] for c in cols)
        ctx.q["pgss"] = psql.run(pgss_sql(schema_sql, names, ctx.top))
    except QueryError as e:
        ctx.q["pgss"] = e


def pgss_sql(schema_sql, names, top):
    """schema_sql = None: имя без схемы, через search_path (так в pg_check.sql)."""
    new = "total_exec_time" in names
    return PGSS_TEMPLATE.format(schema=schema_sql + "." if schema_sql else "", top=int(top),
                                total="total_exec_time" if new else "total_time",
                                mean="mean_exec_time" if new else "mean_time",
                                max="max_exec_time" if new else "max_time")


def check_statements(ctx):
    r = Result("statements", "Тяжёлые запросы (pg_stat_statements)")
    ext = ctx.q.get("pgss_ext")
    if ext is not None and not ext:
        r.status = "INFO"
        pre = "pg_stat_statements" in (ctx.sv("shared_preload_libraries") or "")
        r.summary = ("библиотека загружена, но расширение в этой базе не создано" if pre else
                     "pg_stat_statements не подключён к этой базе: без него не видно, куда уходит время")
        r.details.append("подключение меняет конфигурацию и требует перезапуска базы: делать в окно и с согласия")
        return r
    rows = ctx.rows("pgss")
    if not rows:
        r.status = "INFO"
        r.summary = "статистика пуста (сброшена или нет прав pg_read_all_stats)"
        return r
    r.status = "INFO"
    total = [x for x in rows if int(x["rn_total"]) <= ctx.top]
    lead = total[0]
    r.summary = "лидер по суммарному времени: %s мс за %s вызовов (%s общего времени базы)" % (
        num(lead["total_ms"]), num(lead["calls"]), pct(float(lead["pct"] or 0)))
    r.data = {"top": rows}
    r.details.append("по суммарному времени:")
    for x in total[:ctx.top]:
        r.details.append("  %s%% %s мс, вызовов %s, среднее %s мс, queryid %s: %s" % (
            num(x["pct"]), num(x["total_ms"]), num(x["calls"]), num(x["mean_ms"]), x["queryid"], short(x["query"], 120)))
    calls = sorted([x for x in rows if int(x["rn_calls"]) <= 5], key=lambda x: int(x["rn_calls"]))
    if calls:
        r.details.append("по числу вызовов:")
        for x in calls:
            r.details.append("  вызовов %s, среднее %s мс, queryid %s: %s" % (num(x["calls"]), num(x["mean_ms"]),
                                                                       x["queryid"], short(x["query"], 120)))
    reads = sorted([x for x in rows if int(x["rn_read"]) <= 5 and int(x["shared_blks_read"] or 0)],
                   key=lambda x: int(x["rn_read"]))
    if reads:
        r.details.append("по чтению с диска:")
        for x in reads:
            r.details.append("  прочитано %s, queryid %s: %s" % (size(int(x["shared_blks_read"]) * 8192), x["queryid"],
                                                                 short(x["query"], 120)))
    r.details.append("сортируйте по суммарному времени, а не по среднему: дешёвый запрос, вызванный тысячи раз, "
                     "обычно и есть причина")
    return r


def check_tls(ctx):
    r = Result("tls", "Шифрование соединений")
    rows = ctx.rows("tls")
    ssl = ctx.sv("ssl", "off")
    tcp = [x for x in rows if x.get("addr") != "local"]
    plain = [x for x in tcp if not x.get("ssl")]
    n_tcp, n_plain = sum(int(x["n"]) for x in tcp), sum(int(x["n"]) for x in plain)
    r.data = {"ssl": ssl, "tcp": n_tcp, "plain": n_plain}
    r.status = "INFO" if ssl != "on" or n_plain else "OK"
    if ssl != "on":
        r.summary = "ssl = off: соединения по сети, в том числе от RX, идут открытым текстом"
    else:
        r.summary = "ssl = on; соединений по сети %d, без шифрования %d" % (n_tcp, n_plain)
        groups = {}
        for x in plain:
            k = "%s (%s, %s)" % (x["addr"], x.get("app") or "-", x.get("usr"))
            groups[k] = groups.get(k, 0) + int(x["n"])
        for k, n in sorted(groups.items(), key=lambda kv: -kv[1])[:ctx.top]:
            r.details.append("без шифрования: %s, %d" % (k, n))
        if n_plain:
            r.details.append("правило host в pg_hba.conf пускает и без TLS; обязательным шифрование делает hostssl")
        minv = ctx.sv("ssl_min_protocol_version")
        if minv and minv < "TLSv1.2":
            r.details.append("ssl_min_protocol_version = %s: разрешены устаревшие версии TLS" % minv)
    return r


def check_settings(ctx):
    r = Result("settings", "Настройки")
    th = ctx.th
    notes = []

    def note(status, text):
        r.bump(status)
        notes.append("[%s] %s" % (status, text))

    if ctx.sv("fsync") == "off":
        note("CRIT", "fsync = off: при сбое питания база может разрушиться")
    if ctx.sv("full_page_writes") == "off":
        note("CRIT", "full_page_writes = off: после сбоя возможны повреждённые страницы")
    if ctx.sv("autovacuum") == "off":
        note("CRIT", "autovacuum = off")
    try:
        mpt, mc = int(ctx.sv("max_prepared_transactions", "0") or 0), int(ctx.sv("max_connections", "0") or 0)
        if ctx.s("max_prepared_transactions") and mpt < mc:
            note("WARN", "max_prepared_transactions = %d меньше max_connections = %d: справка RX велит задавать их "
                 "равными (inst_podgotovka_k_ustanovke_linux.htm), нужен перезапуск" % (mpt, mc))
    except ValueError:
        pass
    sb = setting_bytes(ctx.s("shared_buffers"))
    if sb is not None and sb <= 128 * MIB:
        note("WARN", "shared_buffers = %s, значение по умолчанию: под базу RX обычно четверть памяти, отведённой базе"
             % size(sb))
    mw = setting_bytes(ctx.s("max_wal_size"))
    if mw is not None and mw <= GIB:
        note("INFO", "max_wal_size = %s: массовые операции RX вызывают частые чекпоинты, обычно ставят 4-16 ГБ" % size(mw))
    if ctx.sv("random_page_cost") in ("4", "4.0"):
        note("INFO", "random_page_cost = 4 рассчитан на HDD; для SSD обычно 1.1")
    if ctx.sv("idle_in_transaction_session_timeout") == "0":
        note("INFO", "idle_in_transaction_session_timeout не задан: зависшие транзакции живут часами, для RX разумно 20-30 мин")
    if ctx.sv("log_lock_waits") == "off":
        note("INFO", "log_lock_waits = off: в журнале не будет второй стороны блокировок и дедлоков")
    if ctx.sv("log_min_duration_statement") == "-1":
        note("INFO", "log_min_duration_statement = -1: долгие запросы в журнал не пишутся (обычно 2s)")
    if ctx.s("shared_preload_libraries") and "pg_stat_statements" not in ctx.sv("shared_preload_libraries"):
        note("INFO", "pg_stat_statements нет в shared_preload_libraries")
    if ctx.sv("track_io_timing") == "off":
        note("INFO", "track_io_timing = off: нет времени чтения с диска по запросам")
    if ctx.sv("wal_level") == "minimal":
        note("INFO", "wal_level = minimal: ни реплики, ни архива WAL")
    st = ctx.s("statement_timeout")
    if st and st.get("setting") not in ("0", None) and st.get("setting") != st.get("boot_val"):
        note("INFO", "statement_timeout = %s %s задан для всего сервера: таймаут лучше давать роли или сессии"
             % (st.get("setting"), st.get("unit")))
    try:
        pend = ctx.rows("pending_restart")
        if pend:
            note("INFO", "ждут перезапуска: " + ", ".join(p["name"] for p in pend))
    except QueryError:
        pass
    if ctx.q.get("file_errors") and not isinstance(ctx.q["file_errors"], QueryError):
        for e in ctx.q["file_errors"][:5]:
            note("WARN", "ошибка в %s:%s (%s): %s" % (e.get("sourcefile"), e.get("sourceline"), e.get("name"), e.get("error")))
    if ctx.q.get("hba_errors") and not isinstance(ctx.q["hba_errors"], QueryError):
        for e in ctx.q["hba_errors"][:5]:
            note("WARN", "pg_hba.conf, строка %s игнорируется: %s" % (e.get("line_number"), e.get("error")))
    try:
        tables = int(ctx.rows("table_count")[0]["tables"])
        cap = int(ctx.sv("max_locks_per_transaction", "64") or 64) * (
            int(ctx.sv("max_connections", "100") or 100) + int(ctx.sv("max_prepared_transactions", "0") or 0))
        r.data["tables"], r.data["lock_capacity"] = tables, cap
        if tables > cap * 0.5:
            note("WARN", "таблиц %s, а таблица блокировок вмещает %s: pg_dump может упасть с out of shared memory, "
                 "max_locks_per_transaction обычно поднимают до 256" % (num(tables), num(cap)))
    except (QueryError, IndexError, KeyError, ValueError):
        pass
    keys = ["max_connections", "shared_buffers", "work_mem", "maintenance_work_mem", "effective_cache_size",
            "max_wal_size", "checkpoint_timeout", "autovacuum_vacuum_scale_factor", "idle_in_transaction_session_timeout",
            "statement_timeout", "random_page_cost", "wal_level", "archive_mode"]
    vals = []
    for k in keys:
        row = ctx.s(k)
        if not row:
            continue
        b = setting_bytes(row)
        if b is not None and b >= 0 and (row.get("unit") or "").endswith("B"):
            vals.append("%s %s" % (k, size(b)))
        else:
            vals.append("%s %s%s" % (k, row.get("setting"), row.get("unit") or ""))
    worst = [n for n in notes if n.startswith("[CRIT]")] or [n for n in notes if n.startswith("[WARN]")]
    if worst:
        r.summary = "%d замечаний, главное: %s" % (len(notes), worst[0].split("] ", 1)[1])
    elif notes:
        r.summary = "%d рекомендаций без срочности" % len(notes)
    else:
        r.summary = "замечаний нет"
    r.details = notes + ["значения: " + "; ".join(vals)]
    r.data["settings"] = dict((k, v.get("setting")) for k, v in ctx.settings.items() if k != "archive_command")
    r.data["settings"]["archive_command"] = mask(ctx.sv("archive_command"))
    return r


def check_sizes(ctx):
    r = Result("sizes", "Размеры")
    r.status = "INFO"
    dbs = ctx.rows("db_sizes")
    r.summary = ", ".join("%s %s" % (d["datname"], size(d.get("bytes"))) for d in dbs[:6])
    try:
        for t in ctx.rows("top_tables")[:ctx.top]:
            rows_ = "строк около %s" % num(t["est_rows"]) if int(t.get("est_rows") or -1) >= 0 else "статистики ещё нет"
            r.details.append("%s: всего %s, данные %s, индексы %s, %s" % (
                t["rel"], size(t["total_bytes"]), size(t["heap_bytes"]), size(t["index_bytes"]), rows_))
    except QueryError as e:
        r.details.append("таблицы: %s" % e)
    r.data = {"databases": dbs}
    r.details.append("рост между запусками видно, если сохранять отчёт --json и сравнивать")
    return r


# Порядок вывода. Для каждой проверки: ключ, функция, нужные запросы.
CHECKS = [
    ("server", check_server, ["db_sizes"]),
    ("version", check_version, []),
    ("connections", check_connections, ["connections"]),
    ("long_xact", check_long_xact, ["activity", "prepared"]),
    ("idle_in_xact", check_idle, ["activity"]),
    ("locks", check_locks, ["locks"]),
    ("deadlocks", check_deadlocks, []),
    ("wal", check_wal, ["wal_dir"]),
    ("archiver", check_archiver, ["archiver", "archive_status"]),
    ("slots", check_slots, ["slots"]),
    ("replication", check_replication, ["replication", "receiver", "slots"]),
    ("disk", check_disk, ["tablespaces"]),
    ("autovacuum", check_autovacuum, ["vacuum_tables", "vacuum_running", "autovacuum_off"]),
    ("wraparound", check_wraparound, ["wrap_db", "wrap_tables", "xmin_holders"]),
    ("checkpoints", check_checkpoints, ["checkpoints"]),
    ("cache", check_cache, []),
    ("statements", check_statements, ["@pgss"]),
    ("tls", check_tls, ["tls"]),
    ("settings", check_settings, ["pending_restart", "file_errors", "hba_errors", "table_count"]),
    ("sizes", check_sizes, ["db_sizes", "top_tables"]),
]
CHECK_KEYS = [c[0] for c in CHECKS]


def query_sql(ctx, name):
    if name == "activity":
        floor = int(min(60, ctx.th["xact_warn_s"], ctx.th["idle_warn_s"]))
        return Q["activity"].replace("interval '60 seconds'", "interval '%d seconds'" % max(floor, 1))
    if name == "checkpoints" and ctx.major >= 17:
        return Q["checkpoints17"]
    if name == "receiver" and not ctx.replica:
        return None
    if name == "replication" and ctx.replica:
        return None
    return Q[name]


def evaluate(ctx, only=None):
    results = []
    for key, fn, _ in CHECKS:
        if only and key not in only:
            continue
        try:
            res = fn(ctx)
        except QueryError as e:
            res = Result(key, key)
            res.status, res.summary = "SKIP", "не удалось проверить: %s" % e
        except Exception as e:  # ошибка в разборе не должна прятать остальные проверки
            res = Result(key, key)
            res.status, res.summary = "SKIP", "ошибка разбора: %s: %s" % (type(e).__name__, mask(str(e)))
        if res.title == key:
            res.title = TITLES.get(key, key)
        results.append(res)
    return results


TITLES = {"server": "Сервер", "version": "Версия", "connections": "Соединения", "long_xact": "Долгие транзакции",
          "idle_in_xact": "Сессии idle in transaction", "locks": "Блокировки", "deadlocks": "Дедлоки (40P01)",
          "wal": "Каталог pg_wal", "archiver": "Архивация WAL", "slots": "Слоты репликации", "replication": "Реплики",
          "disk": "Диск и память сервера базы", "autovacuum": "Autovacuum и мёртвые строки",
          "wraparound": "Возраст транзакций (wraparound)", "checkpoints": "Чекпоинты", "cache": "Попадание в кэш",
          "statements": "Тяжёлые запросы (pg_stat_statements)", "tls": "Шифрование соединений", "settings": "Настройки",
          "sizes": "Размеры"}


def collect(ctx, psql, only=None, sleep=time.sleep, clock=time.time):
    """Выполняет запросы. Ошибка подключения на первом запросе поднимается наверх (код 2)."""
    rows = psql.run(Q["server"])
    ctx.server = rows[0]
    ctx.settings = dict((x["name"], x) for x in psql.run(Q["settings"]))
    t0 = clock()
    try:
        ctx.snap1 = psql.run(Q["dbstats"])
        ctx.q["dbstats"] = ctx.snap1
    except QueryError as e:
        ctx.q["dbstats"] = e
    needed = []
    for key, _, names in CHECKS:
        if only and key not in only:
            continue
        for n in names:
            if n not in needed:
                needed.append(n)
    for n in needed:
        if n == "@pgss":
            prefetch_statements(ctx, psql)
            continue
        sql = query_sql(ctx, n)
        if sql is None:
            continue
        try:
            ctx.q[n] = psql.run(sql)
        except QueryError as e:
            ctx.q[n] = e
    if ctx.window_s and ctx.snap1 is not None:
        rest = ctx.window_s - (clock() - t0)
        if rest > 0:
            sleep(rest)
        try:
            ctx.snap2 = psql.run(Q["dbstats"])
            ctx.window_real = clock() - t0
        except QueryError:
            ctx.snap2 = None


# --- Вывод ---

def render_text(ctx, results):
    s = ctx.server
    out = []
    ver = re.match(r"^(\S+ \S+)", s.get("version") or "")
    out.append("pg_check %s: %s, база %s, %s, работает %s" % (
        VERSION, ver.group(1) if ver else "PostgreSQL", s.get("db"), "реплика" if ctx.replica else "ведущий сервер",
        dur(s.get("uptime_s"))))
    out.append("время на сервере %s%s" % (s.get("now"), ", окно замера %d с" % int(ctx.window_real) if ctx.window_real else ""))
    main = [x for x in results if x.status in ("CRIT", "WARN")]
    if main:
        out.append("")
        out.append("Главное:")
        for x in sorted(main, key=lambda y: -RANK[y.status]):
            out.append("  [%s] %s: %s" % (x.status, x.title, x.summary))
    out.append("")
    for x in results:
        out.append("[%s]%s %s: %s" % (x.status, " " * (4 - len(x.status)), x.title, x.summary))
        for d in x.details:
            out.append("       " + d)
    counts = summary_counts(results)
    out.append("")
    out.append("Итог: " + ", ".join("%s %d" % (k, counts[k]) for k in ("CRIT", "WARN", "INFO", "OK", "SKIP") if counts[k]))
    return mask("\n".join(out)) + "\n"


def summary_counts(results):
    counts = dict((k, 0) for k in RANK)
    for x in results:
        counts[x.status] += 1
    return counts


def exit_code(results):
    return 1 if any(x.status in ("WARN", "CRIT") for x in results) else 0


def deep_mask(obj):
    """Маскирует каждую строку в структуре: правила маскировщика работают построчно."""
    if isinstance(obj, dict):
        return dict((k, deep_mask(v)) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return [deep_mask(v) for v in obj]
    if isinstance(obj, str):
        return mask(obj)
    return obj


def render_json(ctx, results, code):
    doc = {"tool": "pg_check", "version": VERSION, "server": deep_mask(ctx.server), "window_s": round(ctx.window_real, 1),
           "checks": [deep_mask(x.as_dict()) for x in results], "summary": summary_counts(results), "exit_code": code}
    return json.dumps(doc, ensure_ascii=False, indent=2, default=str) + "\n"  # строки уже замаскированы


# --- SQL для ручного запуска ---

PASTE = [
    ("server", "Сервер: версия, роль, время работы, похожа ли база на базу RX (rx_tables > 0)"),
    ("settings", "Настройки, которые важны для RX"),
    ("connections", "Соединения по приложениям и состояниям; норма: всего меньше 80 % от max_connections"),
    ("activity", "Транзакции дольше минуты; тревожно от 5 минут, idle in transaction смотреть по state_s"),
    ("prepared", "Подготовленные транзакции (2PC); норма: пусто"),
    ("locks", "Кто кого ждёт: waiting ждут, blocker держат; корень цепочки тот, у кого blocked_by пуст"),
    ("dbstats", "Статистика баз: deadlocks, попадание в кэш (blks_hit, blks_read), временные файлы"),
    ("wal_dir", "Размер pg_wal; норма: около max_wal_size + wal_keep_size"),
    ("archive_status", "Сегменты в очереди на архивацию (.ready); норма: единицы и не растёт"),
    ("archiver", "Архиватор: failing_now = t значит архивация падает сейчас"),
    ("slots", "Слоты репликации: неактивный слот с большим retained заполняет диск"),
    ("replication", "Реплики на ведущем: отставание в байтах"),
    ("receiver", "Только на реплике: приём и применение WAL"),
    ("vacuum_tables", "Мёртвые строки и давность vacuum и analyze"),
    ("vacuum_running", "Vacuum, который идёт сейчас"),
    ("autovacuum_off", "Таблицы с выключенным autovacuum; норма: пусто"),
    ("wrap_db", "Возраст транзакций баз; тревожно от 1 млрд, предел около 2,1 млрд"),
    ("wrap_tables", "Самые старые таблицы текущей базы"),
    ("xmin_holders", "Кто держит горизонт очистки"),
    ("checkpoints", "Чекпоинты: много req значит мал max_wal_size"),
    ("pgss", "Тяжёлые запросы из pg_stat_statements: по суммарному времени"),
    ("tls", "Шифрование соединений: ssl = f у соединений по сети"),
    ("pending_restart", "Параметры, которые ждут перезапуска"),
    ("file_errors", "Ошибки в файлах конфигурации (нужен суперпользователь)"),
    ("hba_errors", "Ошибки в pg_hba.conf (нужен суперпользователь)"),
    ("table_count", "Число таблиц: сравните с max_locks_per_transaction x (max_connections + max_prepared_transactions)"),
    ("db_sizes", "Размеры баз"),
    ("top_tables", "Самые большие таблицы текущей базы"),
]


def paste_sql():
    out = [
        "-- pg_check.sql: проверка PostgreSQL под Directum RX, только чтение.",
        "-- Сгенерирован командой: python3 pg_check.py --print-sql > pg_check.sql (руками не править).",
        "-- Запуск: psql -X -d <база RX> -f pg_check.sql",
        "-- Скрипт для psql: строки с обратной косой чертой это команды psql. В DBeaver или pgAdmin",
        "-- выполняйте запросы по одному и пропускайте такие строки.",
        "\\set ON_ERROR_STOP off",
        "\\pset pager off",
        "\\x auto",
        "SET default_transaction_read_only = on;",
        "SET statement_timeout = '10s';",
        "SET lock_timeout = '1s';",
        "SELECT current_setting('server_version_num')::int >= 170000 AS pg17,",
        "       pg_is_in_recovery() AS replica,",
        "       EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements') AS has_pgss \\gset",
    ]
    for key, hint in PASTE:
        out.append("")
        out.append("\\echo '== %s'" % hint.replace("'", ""))
        if key == "checkpoints":
            out += ["\\if :pg17", Q["checkpoints17"].strip() + ";", "\\else", Q["checkpoints"].strip() + ";", "\\endif"]
        elif key == "receiver":
            out += ["\\if :replica", Q["receiver"].strip() + ";", "\\endif"]
        elif key == "pgss":
            out += ["\\if :has_pgss",
                    "-- для расширения 1.8 и новее (PostgreSQL 13+); в старых версиях столбцы total_time, mean_time",
                    pgss_sql(None, {"total_exec_time"}, 10).strip() + ";",
                    "\\else", "\\echo '   pg_stat_statements в этой базе не установлен'", "\\endif"]
        else:
            out.append(Q[key].strip() + ";")
    out.append("")
    return "\n".join(out)


# --- Запуск ---

def emit(text, stream=None):
    """Вывод байтами UTF-8: в Python 3.6 при LANG=C обычный print падает на кириллице."""
    stream = stream or sys.stdout
    buf = getattr(stream, "buffer", None)
    if buf is None:
        stream.write(text)
        return
    try:
        data = text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        data = text.encode("utf-8", "replace")
    stream.flush()
    buf.write(data)
    buf.flush()


class Parser(argparse.ArgumentParser):
    """argparse печатает справку и ошибки тоже через emit."""

    def _print_message(self, message, file=None):
        if message:
            emit(message, file or sys.stderr)


def parse_thresholds(items):
    th = dict((k, v) for k, v, _ in THRESHOLDS)
    for it in items or []:
        if "=" not in it:
            raise ValueError("порог задаётся как ИМЯ=ЗНАЧЕНИЕ: %s" % it)
        k, v = it.split("=", 1)
        k = k.strip()
        if k not in th:
            raise ValueError("нет такого порога: %s (список: --list-thresholds)" % k)
        th[k] = type(th[k])(float(v)) if isinstance(th[k], int) else float(v)
    return th


def build_args(argv):
    p = Parser(
        prog="pg_check.py", description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Проверки: " + ", ".join(CHECK_KEYS) + "\n\n" + "\n".join(__doc__.split("\n")[2:]))
    p.add_argument("-d", "--dbname", help="имя базы RX или строка подключения psql (host=... dbname=... user=...)")
    p.add_argument("--psql", default="psql", help="путь к psql (по умолчанию psql из PATH)")
    p.add_argument("--psql-cmd", help="команда вместо psql целиком, например \"docker exec -i rx-postgres psql -U postgres\"")
    p.add_argument("--window", type=float, default=10, help="окно замера кэша, дедлоков и записи WAL, с (0 без окна)")
    p.add_argument("--timeout", type=int, default=10, help="statement_timeout для каждого запроса, с")
    p.add_argument("--top", type=int, default=10, help="сколько строк показывать в списках")
    p.add_argument("--only", help="только эти проверки через запятую")
    p.add_argument("--no-disk", action="store_true", help="не смотреть разделы и память этой машины")
    p.add_argument("--threshold", action="append", metavar="ИМЯ=ЗНАЧЕНИЕ", help="изменить порог, можно несколько раз")
    p.add_argument("--list-thresholds", action="store_true", help="показать пороги и выйти")
    p.add_argument("--json", action="store_true", help="вывод в JSON")
    p.add_argument("--print-sql", action="store_true", help="напечатать SQL для ручного запуска в psql и выйти")
    return p.parse_args(argv)


def main(argv=None):
    args = build_args(sys.argv[1:] if argv is None else argv)
    if args.print_sql:
        emit(paste_sql())
        return 0
    try:
        th = parse_thresholds(args.threshold)
    except ValueError as e:
        emit("pg_check: %s\n" % e, sys.stderr)
        return 2
    if args.list_thresholds:
        for k, v, desc in THRESHOLDS:
            emit("%-24s %-12s %s\n" % (k, th[k], desc))
        return 0
    only = None
    if args.only:
        only = set(x.strip() for x in args.only.split(",") if x.strip())
        bad = only - set(CHECK_KEYS)
        if bad:
            emit("pg_check: нет таких проверок: %s\n" % ", ".join(sorted(bad)), sys.stderr)
            return 2
    base = shlex.split(args.psql_cmd) if args.psql_cmd else [args.psql]
    psql = Psql(base, args.dbname, args.timeout)
    local = not args.psql_cmd and not args.no_disk
    ctx = Ctx(th, top=args.top, window_s=max(args.window, 0), local=local)
    try:
        collect(ctx, psql, only)
    except QueryError as e:
        msg = "pg_check: не удалось выполнить запрос к PostgreSQL: %s\n" % e
        msg += ("Проверьте адрес и порт, правило в pg_hba.conf, пароль (~/.pgpass или PGPASSWORD) или запустите "
                "на сервере базы: sudo -u postgres python3 pg_check.py -d <база RX>\n")
        emit(mask(msg), sys.stderr)
        if args.json:
            emit(json.dumps({"tool": "pg_check", "version": VERSION, "error": mask(str(e)), "exit_code": 2},
                                        ensure_ascii=False) + "\n")
        return 2
    results = evaluate(ctx, only)
    code = exit_code(results)
    emit(render_json(ctx, results, code) if args.json else render_text(ctx, results))
    return code


if __name__ == "__main__":
    sys.exit(main())
