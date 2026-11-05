"""Проверки pg_check.py скила rx-postgresql: python3 -m unittest discover -s tests

Без сети и без живой базы: строки результатов записаны с PostgreSQL 16 и 17 в контейнерах,
psql подменяется маленьким скриптом.
"""
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skills", "rx-postgresql", "scripts")
sys.path.insert(0, SCRIPTS)
sys.path.append(os.path.join(ROOT, "shared"))  # маскировщик, если tools/sync_shared.py ещё не запускали
import pg_check  # noqa: E402

GIB = 1024 ** 3


def settings(**over):
    base = {
        "max_connections": ("100", ""), "superuser_reserved_connections": ("3", ""), "reserved_connections": ("0", ""),
        "shared_buffers": ("524288", "8kB"), "work_mem": ("16384", "kB"), "max_wal_size": ("8192", "MB"),
        "wal_keep_size": ("0", "MB"), "max_slot_wal_keep_size": ("-1", "MB"), "archive_mode": ("on", ""),
        "archive_command": ("pg_probackup-16 archive-push -B /backup --instance rx --wal-file-name=%f", ""),
        "archive_library": ("", ""), "autovacuum": ("on", ""), "autovacuum_max_workers": ("3", ""),
        "autovacuum_freeze_max_age": ("200000000", ""), "fsync": ("on", ""), "full_page_writes": ("on", ""),
        "idle_in_transaction_session_timeout": ("1500000", "ms"), "statement_timeout": ("0", "ms"),
        "log_lock_waits": ("on", ""), "log_min_duration_statement": ("2000", "ms"),
        "shared_preload_libraries": ("pg_stat_statements", ""), "track_io_timing": ("on", ""),
        "random_page_cost": ("1.1", ""), "wal_level": ("replica", ""), "synchronous_standby_names": ("", ""),
        "wal_log_hints": ("on", ""), "data_checksums": ("off", ""), "max_locks_per_transaction": ("64", ""),
        "max_prepared_transactions": ("100", ""), "ssl": ("on", ""), "ssl_min_protocol_version": ("TLSv1.2", ""),
        "data_directory": ("/var/lib/postgresql/16/main", ""),
    }
    base.update(over)
    return dict((k, {"name": k, "setting": v[0], "unit": v[1], "source": "configuration file", "boot_val": v[0],
                     "pending_restart": False}) for k, v in base.items())


def ctx(**kw):
    c = pg_check.Ctx(pg_check.parse_thresholds(kw.pop("threshold", None)), top=10, window_s=0, local=False)
    c.server = {"version_num": 160015, "version": "PostgreSQL 16.15 (Debian 16.15-1.pgdg13+2) on aarch64",
                "db": "rx", "usr": "postgres", "replica": False, "uptime_s": 86400 * 3, "started_epoch": 1793898694,
                "now": "2026-11-05 17:16:10 UTC", "superuser": True, "pg_monitor": True, "rx_tables": 2500}
    c.server.update(kw.pop("server", {}))
    c.settings = settings(**kw.pop("settings", {}))
    c.q.update(kw.pop("q", {}))
    for k, v in kw.items():
        setattr(c, k, v)
    return c


# Строки, записанные на PostgreSQL 16 в контейнере: цепочка из трёх сессий.
LOCK_ROWS = [
    {"role": "waiting", "pid": 184, "wait_s": 25, "blocked_by": [182], "usr": "postgres", "app": "psql",
     "addr": "local", "state": "active", "backend_type": "client backend", "xact_s": 26, "state_s": 25,
     "exclusive_on": None, "query": "UPDATE sungero_content_edoc SET name = 'b' WHERE id = 1;"},
    {"role": "waiting", "pid": 188, "wait_s": 23, "blocked_by": [184], "usr": "postgres", "app": "psql",
     "addr": "local", "state": "active", "backend_type": "client backend", "xact_s": 23, "state_s": 23,
     "exclusive_on": None, "query": "UPDATE sungero_content_edoc SET name = 'c' WHERE id = 2;"},
    {"role": "blocker", "pid": 182, "wait_s": None, "blocked_by": [], "usr": "postgres", "app": "psql",
     "addr": "local", "state": "idle in transaction", "backend_type": "client backend", "xact_s": 26,
     "state_s": 26, "exclusive_on": None, "query": "UPDATE sungero_content_edoc SET name = name WHERE id = 1;"},
]


class HelpersTest(unittest.TestCase):
    def test_parse_json_multiline(self):
        out = '[{"a":1}, \n {"a":2}]\n'
        self.assertEqual(pg_check.parse_json_output(out), [{"a": 1}, {"a": 2}])
        self.assertEqual(pg_check.parse_json_output("SET\n[]\n"), [])
        with self.assertRaises(pg_check.QueryError):
            pg_check.parse_json_output("что-то другое")

    def test_first_error(self):
        err = "psql:<stdin>:7: ERROR:  permission denied for function pg_ls_waldir\n"
        self.assertEqual(pg_check.first_error(err), "ERROR:  permission denied for function pg_ls_waldir")
        err = ('psql: error: connection to server at "db01" (10.20.0.5), port 5432 failed: '
               'FATAL:  password authentication failed for user "rx"\n')
        self.assertIn("password authentication failed", pg_check.first_error(err))

    def test_units_and_lsn(self):
        s = settings()
        self.assertEqual(pg_check.setting_bytes(s["shared_buffers"]), 4 * GIB)
        self.assertEqual(pg_check.setting_bytes(s["max_wal_size"]), 8 * GIB)
        self.assertEqual(pg_check.setting_bytes(s["max_slot_wal_keep_size"]), -1)
        self.assertEqual(pg_check.setting_ms(s["idle_in_transaction_session_timeout"]), 1500000)
        self.assertEqual(pg_check.lsn_int("1/0"), 1 << 32)
        self.assertEqual(pg_check.lsn_int("0/16B3748"), 0x16B3748)
        self.assertEqual(pg_check.wal_seg("000000010000000A000000FF"), (1, 0xA * 0x100 + 0xFF))
        self.assertIsNone(pg_check.wal_seg("00000002.history"))

    def test_mask_sql_password(self):
        out = pg_check.mask("ALTER ROLE rx PASSWORD 'S3cr3t''x' VALID UNTIL 'infinity'")
        self.assertNotIn("S3cr3t", out)
        self.assertIn("VALID UNTIL", out)
        out = pg_check.mask("host=db01 dbname=rx user=rx password=Qwerty123")
        self.assertNotIn("Qwerty123", out)
        out = pg_check.mask("env AWS_SECRET_ACCESS_KEY=abcd1234 WALG_S3_PREFIX=s3://b wal-g wal-push %p")
        self.assertNotIn("abcd1234", out)
        self.assertIn("wal-g wal-push %p", out)

    def test_human(self):
        self.assertEqual(pg_check.dur(45), "45 с")
        self.assertEqual(pg_check.dur(720), "12 мин")
        self.assertEqual(pg_check.dur(3 * 3600 + 300), "3 ч 5 мин")
        self.assertEqual(pg_check.dur(3 * 86400), "3 дн")
        self.assertEqual(pg_check.size(176 * 1024 * 1024), "176,0 МБ")
        self.assertEqual(pg_check.num(150000), "150 000")


class ChecksTest(unittest.TestCase):
    def test_locks_chain_root(self):
        c = ctx(q={"locks": LOCK_ROWS}, threshold=["lock_warn_s=5", "lock_crit_s=15"])
        r = pg_check.check_locks(c)
        self.assertEqual(r.status, "CRIT")
        self.assertEqual(r.data["roots"], [182])
        self.assertTrue(any("корень pid 182" in d and "idle in transaction" in d for d in r.details), r.details)
        c = ctx(q={"locks": LOCK_ROWS})  # пороги по умолчанию: 30 с и 5 мин
        self.assertEqual(pg_check.check_locks(c).status, "INFO")
        self.assertEqual(pg_check.check_locks(ctx(q={"locks": []})).status, "OK")

    def test_connections(self):
        rows = [{"app": "WebServer", "usr": "rx", "state": "idle", "addr": "10.20.0.11", "n": 70},
                {"app": "Worker", "usr": "rx", "state": "idle in transaction", "addr": "10.20.0.12", "n": 14}]
        r = pg_check.check_connections(ctx(q={"connections": rows}))
        self.assertEqual(r.status, "WARN")
        self.assertEqual(r.data["total"], 85)
        rows[0]["n"] = 81
        self.assertEqual(pg_check.check_connections(ctx(q={"connections": rows})).status, "CRIT")
        r = pg_check.check_connections(ctx(q={"connections": rows}, settings={"max_connections": ("400", "")}))
        self.assertEqual(r.status, "OK")
        # записано на PostgreSQL 13: max_connections 12, три места суперпользователя, девять сессий приложения
        full = [{"app": "psql", "usr": "rx_app", "state": "idle", "addr": "local", "n": 9}]
        r = pg_check.check_connections(ctx(q={"connections": full}, settings={"max_connections": ("12", "")}))
        self.assertEqual(r.status, "CRIT")
        self.assertIn("обычным ролям доступно 9, занято 9", r.summary)

    def test_long_and_idle(self):
        rows = [{"pid": 82, "usr": "rx", "app": "Worker", "addr": "10.20.0.12", "state": "idle in transaction",
                 "xact_s": 720, "state_s": 700, "wait_type": "Client", "wait_event": "ClientRead", "xmin_age": 50,
                 "query": "update sungero_wf_assignment set status = $1 where id = $2"},
                {"pid": 90, "usr": "rx", "app": "WebServer", "addr": "10.20.0.11", "state": "active",
                 "xact_s": 2000, "state_s": 1990, "wait_type": "Lock", "wait_event": "transactionid", "xmin_age": 60,
                 "query": "ALTER ROLE rx PASSWORD 'TopSecret1'"}]
        c = ctx(q={"activity": rows, "prepared": []})
        r = pg_check.check_long_xact(c)
        self.assertEqual(r.status, "CRIT")
        self.assertEqual(r.data["count"], 2)
        self.assertNotIn("TopSecret1", "\n".join(r.details))
        r = pg_check.check_idle(c)
        self.assertEqual(r.status, "WARN")
        self.assertEqual(r.data["count"], 1)
        prep = [{"gid": "rxops_test", "owner": "rx", "database": "rx", "age_s": 900, "xid_age": 10}]
        r = pg_check.check_long_xact(ctx(q={"activity": [], "prepared": prep}))
        self.assertEqual(r.status, "WARN")
        self.assertIn("2PC", r.summary)

    def test_activity_floor_follows_thresholds(self):
        c = ctx(threshold=["xact_warn_s=5"])
        self.assertIn("interval '5 seconds'", pg_check.query_sql(c, "activity"))
        self.assertIn("interval '60 seconds'", pg_check.query_sql(ctx(), "activity"))
        self.assertIn("pg_stat_checkpointer", pg_check.query_sql(ctx(server={"version_num": 170006}), "checkpoints"))
        self.assertIn("pg_stat_bgwriter", pg_check.query_sql(ctx(), "checkpoints"))
        self.assertIn("pg_stat_wal_receiver", pg_check.query_sql(ctx(server={"replica": True}), "receiver"))
        self.assertIsNone(pg_check.query_sql(ctx(server={"replica": True}), "replication"))
        self.assertIsNone(pg_check.query_sql(ctx(), "receiver"))

    def test_archiver(self):
        failing = [{"archived_count": 0, "failed_count": 22, "last_archived_wal": "", "archived_age_s": None,
                    "last_failed_wal": "000000010000000000000001", "failed_age_s": 18, "failing_now": True,
                    "current_wal": "00000001000000000000000D"}]
        c = ctx(q={"archiver": failing, "archive_status": [{"ready": 12, "done": 0}]})
        r = pg_check.check_archiver(c)
        self.assertEqual(r.status, "CRIT")
        self.assertEqual(r.data["ready"], 12)
        healthy = [{"archived_count": 900, "failed_count": 0, "last_archived_wal": "00000001000000000000000C",
                    "archived_age_s": 40, "last_failed_wal": "", "failed_age_s": None, "failing_now": False,
                    "current_wal": "00000001000000000000000D"}]
        denied = pg_check.QueryError("ERROR:  permission denied for function pg_ls_archive_statusdir")
        r = pg_check.check_archiver(ctx(q={"archiver": healthy, "archive_status": denied}))
        self.assertEqual(r.status, "OK")
        self.assertEqual(r.data["ready"], 0)
        self.assertTrue(r.data["ready_estimated"])
        r = pg_check.check_archiver(ctx(settings={"archive_mode": ("off", "")}))
        self.assertEqual(r.status, "INFO")
        r = pg_check.check_archiver(ctx(settings={"archive_command": ("", "")}))
        self.assertEqual(r.status, "CRIT")
        r = pg_check.check_archiver(ctx(q={"archiver": healthy, "archive_status": [{"ready": 0}]},
                                        settings={"archive_command": ("AWS_SECRET_ACCESS_KEY=abcd1234 wal-g wal-push %p", "")}))
        self.assertNotIn("abcd1234", json.dumps(r.as_dict(), ensure_ascii=False))

    def test_slots(self):
        inactive = [{"slot_name": "old_replica", "slot_type": "physical", "database": "", "active": False,
                     "active_pid": None, "wal_status": "reserved", "safe_wal_size": None, "invalidation_reason": None,
                     "retained_bytes": 176 * 1024 * 1024, "retained": "176 MB", "xmin_age": None,
                     "catalog_xmin_age": None}]
        r = pg_check.check_slots(ctx(q={"slots": inactive}))
        self.assertEqual(r.status, "WARN")
        lost = [dict(inactive[0], wal_status="lost", retained_bytes=None)]
        self.assertEqual(pg_check.check_slots(ctx(q={"slots": lost})).status, "CRIT")
        big = [dict(inactive[0], active=True, retained_bytes=12 * GIB)]
        self.assertEqual(pg_check.check_slots(ctx(q={"slots": big})).status, "CRIT")
        ok = [dict(inactive[0], active=True, retained_bytes=10 * 1024 * 1024)]
        r = pg_check.check_slots(ctx(q={"slots": ok}, settings={"max_slot_wal_keep_size": ("32768", "MB")}))
        self.assertEqual(r.status, "OK")

    def test_replication_primary_and_replica(self):
        slot = [{"slot_name": "replica17", "slot_type": "physical", "active": False, "retained_bytes": 0}]
        r = pg_check.check_replication(ctx(q={"replication": [], "slots": slot}))
        self.assertEqual(r.status, "WARN")
        rows = [{"pid": 300, "app": "walreceiver", "addr": "10.20.0.21", "state": "streaming", "sync_state": "sync",
                 "replay_lag_bytes": 0, "flush_lag_bytes": 0, "replay_lag_s": 0.2, "feedback": True}]
        r = pg_check.check_replication(ctx(q={"replication": rows}, settings={"synchronous_standby_names": ("replica1", "")}))
        self.assertEqual(r.status, "WARN")
        self.assertTrue(any("единственная реплика синхронная" in d for d in r.details))
        recv = [{"status": "streaming", "sender_host": "10.20.0.20", "sender_port": 5432, "slot_name": "replica17",
                 "last_msg_age_s": 1, "replay_backlog_bytes": 0, "last_replay_age_s": 30000, "paused": False}]
        r = pg_check.check_replication(ctx(server={"replica": True}, q={"receiver": recv}))
        self.assertEqual(r.status, "OK")
        recv[0].update(status="", paused=True, replay_backlog_bytes=11 * GIB)
        r = pg_check.check_replication(ctx(server={"replica": True}, q={"receiver": recv}))
        self.assertEqual(r.status, "CRIT")

    def test_deadlocks(self):
        snap = [{"datname": "rx", "current": True, "deadlocks": 1, "stats_age_s": 180, "was_reset": False,
                 "blks_hit": 100, "blks_read": 0, "temp_bytes": 0, "wal_lsn": "0/D000060"}]
        r = pg_check.check_deadlocks(ctx(snap1=snap))
        self.assertEqual(r.status, "INFO")
        self.assertIn("меньше суток", r.summary)
        many = [dict(snap[0], deadlocks=300, stats_age_s=86400 * 2)]
        self.assertEqual(pg_check.check_deadlocks(ctx(snap1=many)).status, "CRIT")
        later = [dict(snap[0], deadlocks=3)]
        r = pg_check.check_deadlocks(ctx(snap1=snap, snap2=later, window_real=10.0))
        self.assertEqual(r.status, "WARN")
        self.assertEqual(r.data["window"], 2)

    def test_cache_window(self):
        s1 = [{"datname": "rx", "current": True, "blks_hit": 1000, "blks_read": 0, "temp_bytes": 0, "deadlocks": 0}]
        s2 = [{"datname": "rx", "current": True, "blks_hit": 51000, "blks_read": 20000, "temp_bytes": 5 * 1024 * 1024,
               "deadlocks": 0}]
        r = pg_check.check_cache(ctx(snap1=s1, snap2=s2, window_real=10.0))
        self.assertEqual(r.status, "WARN")
        self.assertEqual(r.data["window_read"], 20000)
        r = pg_check.check_cache(ctx(snap1=s1))
        self.assertEqual(r.status, "OK")

    def test_autovacuum(self):
        rows = [{"rel": "public.sungero_wf_assignment", "live": 150000, "dead": 150000, "dead_pct": 50.0,
                 "heap_bytes": 2 * GIB, "vacuum_age_s": None, "analyze_age_s": None, "vacuums": 0}]
        r = pg_check.check_autovacuum(ctx(q={"vacuum_tables": rows, "vacuum_running": [],
                                             "autovacuum_off": [{"rel": "sungero_wf_assignment"}]}))
        self.assertEqual(r.status, "CRIT")
        rows[0].update(dead_pct=33.3, heap_bytes=19 * 1024 * 1024)
        r = pg_check.check_autovacuum(ctx(q={"vacuum_tables": rows, "vacuum_running": [], "autovacuum_off": []}))
        self.assertEqual(r.status, "WARN")
        r = pg_check.check_autovacuum(ctx(server={"replica": True}))
        self.assertEqual(r.status, "SKIP")

    def test_wraparound(self):
        rows = [{"datname": "rx", "xid_age": 1200000000, "mxid_age": 10}]
        r = pg_check.check_wraparound(ctx(q={"wrap_db": rows, "wrap_tables": [], "xmin_holders": []}))
        self.assertEqual(r.status, "WARN")
        rows[0]["xid_age"] = 1600000000
        self.assertEqual(pg_check.check_wraparound(ctx(q={"wrap_db": rows, "wrap_tables": [],
                                                          "xmin_holders": []})).status, "CRIT")

    def test_wal_ratio(self):
        r = pg_check.check_wal(ctx(q={"wal_dir": [{"files": 2000, "bytes": 2000 * 16 * 1024 * 1024}]}))
        self.assertEqual(r.status, "WARN")
        r = pg_check.check_wal(ctx(q={"wal_dir": [{"files": 5000, "bytes": 5000 * 16 * 1024 * 1024}]}))
        self.assertEqual(r.status, "CRIT")
        r = pg_check.check_wal(ctx(q={"wal_dir": [{"files": 13, "bytes": 13 * 16 * 1024 * 1024}]}))
        self.assertEqual(r.status, "OK")

    def test_version(self):
        self.assertEqual(pg_check.check_version(ctx(server={"version_num": 120020})).status, "WARN")
        self.assertEqual(pg_check.check_version(ctx(server={"version_num": 180001})).status, "OK")
        pro = ctx(server={"version_num": 160004, "version": "PostgreSQL 16.4 (Postgres Pro Standard 16.4.1)"})
        self.assertEqual(pg_check.check_version(pro).status, "INFO")

    def test_settings(self):
        r = pg_check.check_settings(ctx(q={"pending_restart": [], "table_count": [{"tables": 1200}]}))
        self.assertEqual(r.status, "OK")
        r = pg_check.check_settings(ctx(settings={"fsync": ("off", ""), "shared_buffers": ("16384", "8kB")},
                                        q={"pending_restart": [{"name": "shared_buffers", "setting": "524288"}],
                                           "table_count": [{"tables": 7000}]}))
        self.assertEqual(r.status, "CRIT")
        text = "\n".join(r.details)
        self.assertIn("fsync = off", text)
        self.assertIn("shared_buffers = 128,0 МБ", text)
        self.assertIn("ждут перезапуска: shared_buffers", text)
        self.assertIn("max_locks_per_transaction", text)
        r = pg_check.check_settings(ctx(settings={"max_prepared_transactions": ("0", "")},
                                        q={"pending_restart": [], "table_count": [{"tables": 100}]}))
        self.assertEqual(r.status, "WARN")
        self.assertIn("inst_podgotovka_k_ustanovke_linux.htm", "\n".join(r.details))

    def test_tls(self):
        rows = [{"app": "WebServer", "addr": "10.20.0.11", "usr": "rx", "ssl": False, "tls_version": "", "n": 12},
                {"app": "psql", "addr": "local", "usr": "postgres", "ssl": False, "tls_version": "", "n": 1},
                {"app": "Worker", "addr": "10.20.0.12", "usr": "rx", "ssl": True, "tls_version": "TLSv1.3", "n": 8}]
        r = pg_check.check_tls(ctx(q={"tls": rows}))
        self.assertEqual(r.status, "INFO")
        self.assertEqual(r.data["plain"], 12)
        self.assertEqual(r.data["tcp"], 20)

    def test_server_not_rx_database(self):
        c = ctx(server={"db": "postgres", "rx_tables": 0},
                q={"db_sizes": [{"datname": "rx", "bytes": 80 * GIB}, {"datname": "postgres", "bytes": 8 * 1024 ** 2}]})
        r = pg_check.check_server(c)
        self.assertEqual(r.status, "WARN")
        self.assertTrue(any("rx 80,0 ГБ" in d for d in r.details), r.details)

    def test_statements(self):
        rows = [{"queryid": "1", "calls": 200000, "total_ms": 1600000.0, "mean_ms": 8.0, "max_ms": 90.0, "rows": 1,
                 "shared_blks_read": 10, "shared_blks_hit": 9, "temp_blks_written": 0, "pct": 71.5, "rn_total": 1,
                 "rn_calls": 1, "rn_read": 2, "query": "select ... from sungero_core_accessrights where recipient = $1"},
                {"queryid": "2", "calls": 2, "total_ms": 6000.0, "mean_ms": 3000.0, "max_ms": 3100.0, "rows": 1,
                 "shared_blks_read": 5000, "shared_blks_hit": 9, "temp_blks_written": 0, "pct": 0.3, "rn_total": 2,
                 "rn_calls": 2, "rn_read": 1, "query": "select count(*) from sungero_content_edoc"}]
        r = pg_check.check_statements(ctx(q={"pgss_ext": [{"schema": "public"}], "pgss": rows}))
        self.assertEqual(r.status, "INFO")
        self.assertIn("200 000", r.summary)
        r = pg_check.check_statements(ctx(q={"pgss_ext": []}))
        self.assertIn("расширение в этой базе не создано", r.summary)
        r = pg_check.check_statements(ctx(q={"pgss_ext": []}, settings={"shared_preload_libraries": ("", "")}))
        self.assertIn("не подключён", r.summary)

    def test_limited_visibility(self):
        c = ctx(server={"superuser": False, "pg_monitor": False}, q={"activity": [], "prepared": [], "locks": []})
        r = pg_check.check_long_xact(c)
        self.assertEqual(r.status, "INFO")
        self.assertIn("pg_monitor", r.summary)
        self.assertEqual(pg_check.check_idle(c).status, "INFO")
        rows = [{"app": "psql", "usr": "postgres", "state": "", "addr": "local", "n": 5}]
        r = pg_check.check_connections(ctx(server={"superuser": False, "pg_monitor": False}, q={"connections": rows}))
        self.assertIn("нет прав 5", r.summary)
        del c.settings["shared_preload_libraries"]
        c.q.update({"pending_restart": [], "table_count": [{"tables": 10}]})
        self.assertFalse(any("shared_preload_libraries" in d for d in pg_check.check_settings(c).details))
        sql = pg_check.pgss_sql("public", {"total_time", "mean_time"}, 5)
        self.assertIn("total_time AS total_ms", sql)
        self.assertIn("public.pg_stat_statements", sql)

    def test_disk_skips_when_not_local(self):
        r = pg_check.check_disk(ctx())
        self.assertEqual(r.status, "SKIP")
        c = ctx(local=True)
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "postmaster.pid"), "w") as f:
                f.write("123\n%s\n%d\n5432\n" % (d, c.server["started_epoch"] + 100))
            c.settings["data_directory"]["setting"] = d
            r = pg_check.check_disk(c)
            self.assertEqual(r.status, "SKIP")
            self.assertIn("другому экземпляру", r.summary)
            with open(os.path.join(d, "postmaster.pid"), "w") as f:
                f.write("123\n%s\n%d\n5432\n" % (d, c.server["started_epoch"]))
            c.q["tablespaces"] = []
            r = pg_check.check_disk(c)
            self.assertNotEqual(r.status, "SKIP")
            self.assertIn("данные", r.data)


class FakePsql(object):
    """Отвечает на запросы по узнаваемому фрагменту SQL."""

    def __init__(self, answers, fail=()):
        self.answers, self.fail, self.calls = answers, fail, []

    def run(self, sql):
        self.calls.append(sql)
        for key in self.fail:
            if key in sql:
                raise pg_check.QueryError("ERROR:  permission denied for %s" % key)
        for key, rows in self.answers:
            if key in sql:
                return rows
        return []


SERVER_ROW = [{"version_num": 170006, "version": "PostgreSQL 17.6", "db": "rx", "usr": "postgres", "replica": False,
               "uptime_s": 7200, "started_epoch": 1, "now": "2026-11-05", "superuser": True, "pg_monitor": True,
               "rx_tables": 3}]


class CollectTest(unittest.TestCase):
    def fake(self, fail=()):
        dbstats = [{"datname": "rx", "current": True, "deadlocks": 0, "stats_age_s": 7200, "was_reset": False,
                    "blks_hit": 10, "blks_read": 0, "temp_bytes": 0, "wal_lsn": "0/3000000"}]
        return FakePsql([
            ("pg_has_role(current_user, 'pg_monitor'", SERVER_ROW),
            ("FROM pg_settings\nWHERE name IN", list(settings().values())),
            ("FROM pg_stat_database", dbstats),
            ("FROM pg_ls_waldir()", [{"files": 3, "bytes": 3 * 16 * 1024 * 1024}]),
            ("FROM pg_stat_checkpointer", [{"timed": 10, "req": 1, "stats_age_s": 7200}]),
            ("FROM pg_database\nORDER BY 2 DESC", [{"datname": "rx", "xid_age": 1000, "mxid_age": 1}]),
        ], fail=fail)

    def test_collect_window_and_errors(self):
        c = pg_check.Ctx(pg_check.parse_thresholds(None), window_s=10, local=False)
        slept = []
        clock = iter([100.0, 103.0, 110.5]).__next__
        p = self.fake(fail=("pg_ls_waldir",))
        pg_check.collect(c, p, sleep=slept.append, clock=clock)
        self.assertEqual(slept, [7.0])
        self.assertAlmostEqual(c.window_real, 10.5)
        self.assertTrue(any("pg_stat_checkpointer" in s for s in p.calls), "на 17 нужен pg_stat_checkpointer")
        results = dict((r.key, r) for r in pg_check.evaluate(c))
        self.assertEqual(results["wal"].status, "SKIP")
        self.assertIn("permission denied", results["wal"].summary)
        self.assertEqual(results["checkpoints"].status, "OK")

    def test_only(self):
        c = pg_check.Ctx(pg_check.parse_thresholds(None), window_s=0, local=False)
        p = self.fake()
        pg_check.collect(c, p, only={"wal"})
        self.assertFalse(any("pg_stat_statements" in s for s in p.calls))
        self.assertEqual([r.key for r in pg_check.evaluate(c, {"wal"})], ["wal"])

    def test_main_connection_failure_is_2(self):
        class Broken(object):
            def __init__(self, *a, **kw):
                pass

            def run(self, sql):
                raise pg_check.QueryError('FATAL:  password authentication failed for user "rx"')

        err, out = io.StringIO(), io.StringIO()
        with mock.patch.object(pg_check, "Psql", Broken), mock.patch("sys.stderr", err), mock.patch("sys.stdout", out):
            self.assertEqual(pg_check.main(["-d", "rx", "--window", "0", "--json"]), 2)
        self.assertEqual(json.loads(out.getvalue())["exit_code"], 2)
        self.assertIn("pg_hba.conf", err.getvalue())

    def test_main_json_exit_code(self):
        fake = self.fake()
        out = io.StringIO()
        with mock.patch.object(pg_check, "Psql", lambda *a, **kw: fake), mock.patch("sys.stdout", out):
            code = pg_check.main(["-d", "rx", "--window", "0", "--json", "--only", "wal,checkpoints,wraparound"])
        doc = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(doc["exit_code"], 0)
        self.assertEqual([c["key"] for c in doc["checks"]], ["wal", "wraparound", "checkpoints"])

    def test_bad_arguments(self):
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            self.assertEqual(pg_check.main(["--only", "nosuch"]), 2)
            self.assertEqual(pg_check.main(["--threshold", "nosuch=1"]), 2)


class PsqlProcessTest(unittest.TestCase):
    """Настоящий вызов подпроцесса, вместо psql маленький скрипт на Python."""

    def make(self, body):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "fake_psql")
        with open(path, "w") as f:
            f.write("#!%s\nimport os, sys, json\n%s\n" % (sys.executable, body))
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        self.addCleanup(lambda: (os.remove(path), os.rmdir(d)))
        return path

    def test_flags_env_and_readonly_preamble(self):
        path = self.make("script = sys.stdin.read()\n"
                         "print(json.dumps([{'argv': sys.argv[1:], 'app': os.environ.get('PGAPPNAME'),\n"
                         "  'ro': 'default_transaction_read_only = on' in script,\n"
                         "  'timeout': \"statement_timeout = '7s'\" in script}]))")
        rows = pg_check.Psql([path], "host=db01 dbname=rx", timeout_s=7).run("SELECT 1")
        self.assertEqual(rows[0]["app"], "rxops-pg_check")
        self.assertTrue(rows[0]["ro"])
        self.assertTrue(rows[0]["timeout"])
        for flag in ("-X", "-q", "-A", "-t", "-w", "ON_ERROR_STOP=1"):
            self.assertIn(flag, rows[0]["argv"])
        self.assertEqual(rows[0]["argv"][-2:], ["-d", "host=db01 dbname=rx"])

    def test_error_is_masked(self):
        path = self.make("sys.stdin.read()\n"
                         "sys.stderr.write('psql: error: connection to server failed: password=Hunter22 FATAL: no pg_hba.conf entry\\n')\n"
                         "sys.exit(2)")
        with self.assertRaises(pg_check.QueryError) as cm:
            pg_check.Psql([path]).run("SELECT 1")
        self.assertNotIn("Hunter22", str(cm.exception))

    def test_missing_psql(self):
        with self.assertRaises(pg_check.QueryError):
            pg_check.Psql(["/nonexistent/psql"]).run("SELECT 1")


class OutputTest(unittest.TestCase):
    def test_paste_sql_in_sync(self):
        with open(os.path.join(SCRIPTS, "pg_check.sql"), encoding="utf-8") as f:
            self.assertEqual(f.read(), pg_check.paste_sql(), "перегенерируйте: python3 pg_check.py --print-sql > pg_check.sql")
        sql = pg_check.paste_sql()
        self.assertIn("\\if :pg17", sql)
        self.assertIn("SET default_transaction_read_only = on;", sql)
        for word in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "CREATE ", "TRUNCATE ", "pg_terminate_backend"):
            self.assertNotIn(word, sql.upper() if word.isupper() else sql)

    def test_render_text_masks(self):
        c = ctx()
        r = pg_check.Result("long_xact", "Долгие транзакции")
        r.status, r.summary = "WARN", "1"
        r.details = ["pid 1: CREATE USER MAPPING FOR rx SERVER s OPTIONS (user 'a', password 'Pa55word')"]
        text = pg_check.render_text(c, [r])
        self.assertNotIn("Pa55word", text)
        self.assertIn("Главное:", text)
        self.assertEqual(pg_check.exit_code([r]), 1)


if __name__ == "__main__":
    unittest.main()
