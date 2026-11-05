"""Проверки backup_inventory.py скила rx-backup-check: python3 -m unittest discover -s tests

Без сети и без живых серверов: каталоги копий собираются во временной папке, вывод pg_probackup
записан с настоящего pg_probackup 2.5.16 на PostgreSQL 16 в контейнере.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skills", "rx-backup-check", "scripts")
sys.path.insert(0, SCRIPTS)
sys.path.append(os.path.join(ROOT, "shared"))  # маскировщик, если tools/sync_shared.py ещё не запускали
import backup_inventory as bi  # noqa: E402

NOW = time.mktime((2026, 11, 5, 18, 0, 0, 0, 0, -1))
H = 3600

# pg_probackup-16 2.5.16: show --format=json после validate, одна копия испорчена (поля сокращены)
PB_SHOW_CORRUPT = json.loads(
    '[{"instance": "rx", "backups": [{"id": "TO1IEB", "parent-backup-id": "TO1IE6", "backup-mode": "DELTA", '
    '"wal": "ARCHIVE", "start-time": "2026-11-05 17:39:47+00", "end-time": "2026-11-05 17:39:49+00", '
    '"recovery-time": "2026-11-05 17:39:48+00", "data-bytes": 407865, "status": "CORRUPT"}, {"id": "TO1IE6", '
    '"backup-mode": "FULL", "wal": "ARCHIVE", "start-time": "2026-11-05 17:39:42+00", "end-time": '
    '"2026-11-05 17:39:46+00", "recovery-time": "2026-11-05 17:39:44+00", "data-bytes": 32543594, "status": "OK"}]}]')
# show --archive --format=json: из архива удалён один сегмент
PB_ARCHIVE_LOST = json.loads(
    '[{"instance": "rx", "timelines": [{"tli": 1, "parent-tli": 0, "switchpoint": "0/0", "min-segno": '
    '"000000010000000000000001", "max-segno": "000000010000000000000013", "n-segments": 18, "size": 301989888, '
    '"zratio": 1.0, "closest-backup-id": "", "status": "DEGRADED", "lost-segments": [{"begin-segno": '
    '"00000001000000000000000E", "end-segno": "00000001000000000000000E"}], "backups": []}]}]')
PB_EPOCH = 1793900389  # 2026-11-05 17:39:49 UTC


def touch(path, mtime, size=100):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(size)
    os.utime(path, (mtime, mtime))


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def p(self, *parts):
        return os.path.join(self.tmp, *parts)


class NamesTest(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(bi.normalize("rx_20261105_230000.dump"), "rx_#_#.dump")
        self.assertEqual(bi.normalize("base_2026-11-05"), "base_#-#-#")

    def test_pg_time(self):
        self.assertEqual(bi.parse_pg_time("2026-11-05 17:39:49+00"), PB_EPOCH)
        self.assertEqual(bi.parse_pg_time("2026-11-05 20:39:49+03"), PB_EPOCH)
        self.assertEqual(bi.parse_pg_time("2026-11-05 20:39:49+0300"), PB_EPOCH)
        self.assertEqual(bi.parse_pg_time("2026-11-05 20:39:49+03:00"), PB_EPOCH)
        self.assertIsNone(bi.parse_pg_time("вчера"))


class GroupsTest(TmpCase):
    def test_daily_dumps_shrink_stale_temp(self):
        for d in range(1, 6):
            touch(self.p("pg", "rx_2026110%d_230000.dump" % d), NOW - (6 - d) * 24 * H, 10 * 1024 * 1024)
            touch(self.p("pg", "globals_2026110%d_230000.sql" % d), NOW - (6 - d) * 24 * H, 700)
        touch(self.p("pg", "rx_20261106_230000.dump"), NOW - 2 * H, 300000)          # ужатая последняя
        touch(self.p("pg", ".rx_20261103_230000.inprogress"), NOW - 50 * H, 1000)    # оборванная
        found = bi.scan_dir(self.tmp)
        groups = bi.group_items(found["items"])
        res = dict((k[1], bi.eval_group(k, v, NOW, 26, 20)) for k, v in groups.items())
        st, line, notes = res["rx_#_#.dump"]
        self.assertEqual(st, "WARN")
        self.assertTrue(any("меньше обычного" in n for n in notes), notes)
        self.assertEqual(res["globals_#_#.sql"][0], "OK")
        self.assertEqual(res[".rx_#_#.inprogress"][0], "WARN")
        st, line, notes = bi.eval_group(("pg", "globals_#_#.sql"), groups[("pg", "globals_#_#.sql")], NOW + 30 * H, 26, 20)
        self.assertEqual(st, "WARN")
        st, _, _ = bi.eval_group(("pg", "globals_#_#.sql"), groups[("pg", "globals_#_#.sql")], NOW + 100 * H, 26, 20)
        self.assertEqual(st, "CRIT")

    def test_empty_single_and_undated(self):
        touch(self.p("a", "rx_20261105.dump"), NOW - H, 0)
        touch(self.p("b", "before_update_20250101.dump"), NOW - 300 * 24 * H, 5000)
        touch(self.p("c", "rx_latest.dump"), NOW - 72 * H, 5000)
        groups = bi.group_items(bi.scan_dir(self.tmp)["items"])
        st = dict((k[0], bi.eval_group(k, v, NOW, 26, 20)[0]) for k, v in groups.items())
        self.assertEqual(st["a"], "CRIT")   # пустая копия
        self.assertEqual(st["b"], "INFO")   # разовая копия с датой
        self.assertEqual(st["c"], "WARN")   # перезаписываемый файл давно не обновлялся

    def test_dump_directory_is_one_item(self):
        for i in range(30):
            touch(self.p("pg", "rx_20261105_230000.dump", "%d.dat.gz" % (3000 + i)), NOW - H, 1000)
        touch(self.p("pg", "rx_20261105_230000.dump", "toc.dat"), NOW - H, 500)
        items = bi.scan_dir(self.tmp)["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["files"], 31)
        self.assertEqual(items[0]["size"], 30500)

    def test_storage_mirror_tree(self):
        for i in range(60):
            touch(self.p("storage-mirror", "ab", "blob%d" % i), NOW - 3 * H, 2048)
        items = bi.scan_dir(self.tmp)["items"]
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].get("tree"))
        st, line, _ = bi.eval_group((items[0]["parent"], ""), items, NOW, 26, 20)
        self.assertEqual(st, "OK")
        self.assertIn("дерево файлов (60 файлов", line)

    def test_probackup_catalog_not_walked(self):
        touch(self.p("probackup", "backups", "rx", "pg_probackup.conf"), NOW, 10)
        touch(self.p("probackup", "wal", "rx", "000000010000000000000001"), NOW, 16)
        found = bi.scan_dir(self.tmp)
        self.assertEqual(found["probackup"], [self.p("probackup")])
        self.assertEqual(found["items"], [])
        self.assertEqual(bi.scan_dir(self.p("probackup"))["probackup"], [self.p("probackup")])

    def test_ignore(self):
        touch(self.p("pg", "rx_20261105.log"), NOW, 10)
        touch(self.p("pg", "rx_20261105.dump"), NOW, 10)
        items = bi.scan_dir(self.tmp, ignore=["*.log"])["items"]
        self.assertEqual([i["name"] for i in items], ["rx_20261105.dump"])


class WalTest(TmpCase):
    def wal(self, names, size=16 * 1024 * 1024, age=H):
        for n in names:
            touch(self.p("wal", n), NOW - age, size)
        return bi.scan_dir(self.tmp)["wal"][self.p("wal")]

    def test_gap_and_label(self):
        names = ["00000001000000000000000%X" % i for i in range(1, 16) if i != 0xE]
        names.append("00000001000000000000000C.00000028.backup")
        files = self.wal(names)
        r = bi.eval_wal(self.p("wal"), files, NOW, 2)
        self.assertEqual(r["status"], "CRIT")
        self.assertEqual(r["gaps"][0]["from"], "00000001000000000000000E")
        self.assertEqual(r["gaps"][0]["missing"], 1)
        self.assertTrue(any("pg_archivecleanup" in n for n in r["notes"]), r["notes"])

    def test_log_boundary_16mb(self):
        names = ["0000000100000000000000FE", "0000000100000000000000FF", "000000010000000100000000"]
        r = bi.eval_wal(self.p("wal"), self.wal(names), NOW, 2)
        self.assertEqual(r["status"], "OK")
        self.assertEqual(r["gaps"], [])

    def test_log_boundary_64mb_compressed(self):
        # сегменты по 64 МБ, сжатые: размер по файлам не узнать, берём по наблюдаемым номерам
        names = ["00000001000000000000003E.gz", "00000001000000000000003F.gz", "000000010000000100000000.gz",
                 "00000001000000010000003F.gz", "000000010000000200000000.gz"]
        names = [n for n in names] + ["0000000100000001000000%02X.gz" % i for i in range(1, 0x3F)]
        r = bi.eval_wal(self.p("wal"), self.wal(names, size=1000), NOW, 2)
        self.assertEqual(r["segment_size"], 64 * 1024 * 1024)
        self.assertEqual(r["gaps"], [])

    def test_stale_archive(self):
        r = bi.eval_wal(self.p("wal"), self.wal(["000000010000000000000001"], age=5 * H), NOW, 2)
        self.assertEqual(r["status"], "WARN")


class ToolsTest(unittest.TestCase):
    def test_probackup_corrupt_and_lost(self):
        st, lines, data = bi.eval_probackup(PB_SHOW_CORRUPT, PB_ARCHIVE_LOST, PB_EPOCH + 600, 26)
        self.assertEqual(st, "CRIT")
        text = "\n".join(l for _, l in lines)
        self.assertIn("TO1IEB CORRUPT", text)
        self.assertIn("последний запуск TO1IEB завершился статусом CORRUPT", text)
        self.assertIn("00000001000000000000000E..00000001000000000000000E", text)
        self.assertEqual(data[0]["newest_ok"], "TO1IE6")

    def test_probackup_stale(self):
        show = [{"instance": "rx", "backups": [dict(PB_SHOW_CORRUPT[0]["backups"][1])]}]
        st, _, _ = bi.eval_probackup(show, [], PB_EPOCH + 30 * H, 26)
        self.assertEqual(st, "WARN")
        st, _, _ = bi.eval_probackup([{"instance": "rx", "backups": []}], [], PB_EPOCH, 26)
        self.assertEqual(st, "CRIT")

    def test_pgbackrest(self):
        info = [{"name": "rx", "status": {"code": 0, "message": "ok"}, "archive": [{"min": "00000001000000000000000A",
                 "max": "000000010000000000000012"}], "backup": [
                 {"label": "20261105-180000F", "type": "full", "error": False,
                  "timestamp": {"start": PB_EPOCH - 100, "stop": PB_EPOCH - 60}}]}]
        st, lines = bi.eval_pgbackrest(info, PB_EPOCH + 60, 26)
        self.assertEqual(st, "OK")
        self.assertIn("20261105-180000F", lines[0][1])
        st, _ = bi.eval_pgbackrest(info, PB_EPOCH + 100 * H, 26)
        self.assertEqual(st, "CRIT")

    def test_pgbackrest_real_without_archive(self):
        # pgbackrest 2.60 info --output=json на PostgreSQL 17 в контейнере (поля сокращены), архива WAL нет
        info = json.loads(
            '[{"name": "rx", "status": {"code": 0, "lock": {"backup": {"held": false}}, "message": "ok"}, '
            '"archive": [{"id": "17-1", "min": null, "max": null}], "backup": ['
            '{"label": "20261105-174342F", "type": "full", "error": false, "timestamp": {"start": 1793900622, "stop": 1793900626}}, '
            '{"label": "20261105-174342F_20261105-174346D", "type": "diff", "error": false, '
            '"timestamp": {"start": 1793900626, "stop": 1793900627}}]}]')
        st, lines = bi.eval_pgbackrest(info, 1793900627 + 60, 26)
        self.assertEqual(st, "WARN")
        self.assertIn("20261105-174342F_20261105-174346D (diff)", lines[0][1])
        self.assertIn("архив WAL пуст", lines[0][1])

    def test_eval_pg(self):
        row = {"archive_mode": "on", "archive_command": "cp %p /backup/wal/%f", "archived_count": 10, "failed_count": 3,
               "last_archived_wal": "00000001000000000000000C", "archived_age_s": 60, "failed_age_s": 30,
               "failing_now": True, "replica": False}
        self.assertEqual(bi.eval_pg(row, 2)[0], "CRIT")
        self.assertEqual(bi.eval_pg(dict(row, failing_now=False), 2)[0], "OK")
        self.assertEqual(bi.eval_pg(dict(row, failing_now=False, archived_age_s=5 * H), 2)[0], "WARN")
        self.assertEqual(bi.eval_pg(dict(row, archive_mode="off"), 2)[0], "INFO")
        self.assertEqual(bi.eval_pg(dict(row, archive_command=""), 2)[0], "CRIT")


class ScheduleTest(TmpCase):
    def test_parse_cron(self):
        script = self.p("rx-backup.sh")
        with open(script, "w") as f:
            f.write("#!/bin/bash\nDIR=/backup/pg\npg_dump -d rx -Fc -f \"$DIR/rx.dump\"\nmc mirror --overwrite s3/rx-docs /backup/s3\n")
        text = ("PGPASSWORD=VerySecret123\nMAILTO=root\n"
                "0 23 * * * postgres %s >> /var/log/rx-backup.log 2>&1\n"
                "30 1 * * 0 postgres pg_probackup-16 backup -B /backup/probackup --instance rx -b FULL\n"
                "@daily root /usr/bin/find /tmp -mtime +7 -delete\n" % script)
        jobs = bi.parse_cron_text(text, "/etc/cron.d/rx-backup", system=True)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["user"], "postgres")
        self.assertEqual(sorted(jobs[0]["kinds"]), ["база", "файлы"])
        self.assertEqual(jobs[0]["scripts"][0]["path"], script)
        self.assertEqual(jobs[1]["when"], "30 1 * * 0")
        self.assertIn("/backup/probackup", bi.mentioned_paths(jobs[1]["text"]))
        self.assertIn("/backup/pg", bi.mentioned_paths(jobs[0]["text"]))
        self.assertNotIn("VerySecret123", json.dumps(jobs, ensure_ascii=False))

    def test_scan_cron_root(self):
        os.makedirs(self.p("etc", "cron.d"))
        os.makedirs(self.p("etc", "cron.daily"))
        with open(self.p("etc", "crontab"), "w") as f:
            f.write("17 * * * * root cd / && run-parts --report /etc/cron.hourly\n")
        with open(self.p("etc", "cron.d", "storage"), "w") as f:
            f.write("0 2 * * * root rsync -a /srv/rxdata/storageservice/ /backup/storage/\n")
        with open(self.p("etc", "cron.daily", "dpkg"), "w") as f:
            f.write("#!/bin/sh\n# backup of dpkg status\ncp /var/lib/dpkg/status /var/backups/dpkg.status\n")
        with open(self.p("etc", "cron.daily", "restic-docs"), "w") as f:
            f.write("#!/bin/sh\nrestic backup /srv/rxdata/storageservice\n")
        jobs, read, denied = bi.scan_cron(self.tmp)
        sources = [j["source"] for j in jobs]
        self.assertTrue(any(s.endswith("cron.d/storage:1") for s in sources), sources)
        self.assertTrue(any(s.endswith("restic-docs") for s in sources), sources)
        self.assertFalse(any(s.endswith("dpkg") for s in sources), "системный dpkg не копия RX")

    def test_timers_parsing(self):
        new = ("Thu 2026-11-05 23:00:00 MSK 4h 59min left Wed 2026-11-04 23:00:01 MSK 19h ago rx-backup.timer rx-backup.service\n"
               "n/a n/a Wed 2026-11-04 06:31:02 MSK 1 day ago apt-daily.timer apt-daily.service\n")
        self.assertEqual(bi.parse_list_timers(new), [("rx-backup.timer", "rx-backup.service"),
                                                     ("apt-daily.timer", "apt-daily.service")])
        old = "Thu 2026-11-05 23:00:00 MSK 4h left Wed 2026-11-04 23:00:01 MSK 19h ago pg-backup.timer\n"
        self.assertEqual(bi.parse_list_timers(old), [("pg-backup.timer", "pg-backup.service")])
        unit = "[Service]\nType=oneshot\nUser=postgres\nExecStart=/usr/local/sbin/rx-backup.sh\nExecStartPost=-/bin/true\n"
        self.assertEqual(bi.exec_lines(unit), ["/usr/local/sbin/rx-backup.sh", "/bin/true"])
        self.assertEqual(bi.parse_show("Result=exit-code\nExecMainStatus=1\n")["Result"], "exit-code")

    def test_bacula_filesets(self):
        path = self.p("bacula-dir.conf")
        with open(path, "w") as f:
            f.write('Director {\n  Name = dir\n  Password = "11111"\n}\n'
                    'FileSet {\n  Name = "Local-configuration"\n  Include {Options {signature = MD5} File = /etc}\n}\n'
                    'FileSet {\n  Name = "rx-storage"\n  Include {\n    Options { signature = MD5 }\n'
                    '    File = "/srv/rxdata/storageservice"\n  }\n}\n')
        files = bi.bacula_filesets(path)
        self.assertIn("/etc", files)
        self.assertIn("/srv/rxdata/storageservice", files)
        self.assertNotIn("11111", " ".join(files))


class ConfigTest(unittest.TestCase):
    CONFIG = ("variables:\n  home_path: '/srv/rxdata'\ncommon_config: &base\n  DATABASE_ENGINE: 'postgres'\n"
              "  CONNECTION_STRING: 'Server=db01;Port=5432;Database=rx;User Id=rx;Password=Pa55w0rd!'\n"
              "  DATA_PROTECTION_CERTIFICATE_FILE: 'certificate.cer'\n  PRIMARY_TENANT: 'demo'\n"
              "services_config:\n  StorageService:\n    <<: *base\n    STORAGE_PATH: '{{ home_path }}/storageservice'\n"
              "  S3Tool:\n    STORAGE_PATH: '{{ home_path }}/storageservice'\n    S3_STORAGE_SERVICE_ADDRESS: 'https://s3.example.local'\n"
              "  KeyDerivationService:\n    PORT: 1\nSungeroHaproxy:\n  ssl_cert: '{{ home_path }}/haproxy/rx.pem'\n")

    def test_parse_config(self):
        c = bi.parse_config(self.CONFIG)
        self.assertEqual(c["STORAGE_PATH"], ["/srv/rxdata/storageservice"])
        self.assertEqual(c["CONNECTION_STRING"], {"database": "rx", "host": "db01", "encrypted": False})
        self.assertEqual(c["ssl_cert"], "/srv/rxdata/haproxy/rx.pem")
        self.assertIn("KeyDerivationService", c)
        self.assertNotIn("Pa55w0rd", json.dumps(c))
        enc = bi.parse_config("common_config:\n  CONNECTION_STRING: 'gAAAAABl3q...=='\n")
        self.assertTrue(enc["CONNECTION_STRING"]["encrypted"])

    def test_covered(self):
        texts = ["rsync -a /srv/rxdata/ /backup/rxdata/", "tar czf /backup/etc.tgz /srv/DirectumLauncher/etc"]
        self.assertTrue(bi.covered("/srv/rxdata/storageservice", texts, []))
        self.assertTrue(bi.covered("/srv/DirectumLauncher/etc/config.yml", texts, []))
        self.assertFalse(bi.covered("/srv/rx", ["rsync -a /srv/rxdata/ /b/"], []))
        self.assertTrue(bi.covered("/srv/rxdata/storageservice", [], ["/srv/rxdata"]))
        self.assertFalse(bi.covered("/srv/rxdata2/x", [], ["/srv/rxdata"]))


class MainTest(TmpCase):
    def run_main(self, argv, jobs=()):
        out = io.StringIO()
        with mock.patch.object(bi, "scan_cron", return_value=(list(jobs), [], [])), \
                mock.patch.object(bi, "scan_timers", return_value=([], None)), \
                mock.patch.object(bi, "bacula_filesets", return_value=None), \
                mock.patch.object(bi, "find_launcher_dirs", return_value=[]), \
                mock.patch.object(bi, "find_probackup", return_value=[]), \
                mock.patch.object(bi, "which", return_value=None), \
                mock.patch("sys.stdout", out):
            code = bi.main(argv)
        return code, out.getvalue()

    def test_json_and_exit_code(self):
        for d in range(1, 4):
            touch(self.p("pg", "rx_2026110%d.dump" % d), time.time() - (4 - d) * 3600, 5000)
        job = bi.parse_cron_text("0 23 * * * postgres PGPASSWORD=Hunter22 pg_dump -d rx -f /x/rx.dump\n"
                                 "0 1 * * * root cd /tmp && RESTIC_PASSWORD='Sup3r S3cret' restic backup /srv\n"
                                 "0 2 * * * root mc alias set s3 https://s3.local AKIAKEY12345 SecretKey987 && "
                                 "mc mirror --overwrite s3/rx /backup/s3\n",
                                 "/etc/cron.d/rx", system=True)
        code, out = self.run_main(["--dir", self.tmp, "--no-pg", "--json"], jobs=job)
        doc = json.loads(out)
        for secret in ("Hunter22", "Sup3r", "AKIAKEY12345", "SecretKey987"):
            self.assertNotIn(secret, out)
        keys = [c["key"] for c in doc["checks"]]
        self.assertEqual(keys[0], "schedules")
        self.assertIn("dir", keys)
        self.assertEqual(keys[-1], "checklist")
        self.assertEqual(code, doc["exit_code"])
        self.assertEqual(code, 0)  # копии свежие, Directum Launcher не найден: только SKIP и INFO
        code, out = self.run_main(["--dir", self.tmp, "--no-pg"], jobs=job)
        for secret in ("Hunter22", "Sup3r", "AKIAKEY12345", "SecretKey987"):
            self.assertNotIn(secret, out)

    def test_text_output_and_missing_dir(self):
        code, out = self.run_main(["--dir", self.p("nope"), "--no-pg"])
        self.assertIn("каталога нет или нет прав", out)
        self.assertIn("Проверьте руками", out)
        self.assertEqual(code, 1)  # в расписаниях пусто: WARN

    def test_all_skip_is_2(self):
        results = [bi.Result("dir", "x"), bi.Result("checklist", "y")]
        results[0].status = "SKIP"
        results[1].status = "INFO"
        self.assertEqual(bi.exit_code(results), 2)


if __name__ == "__main__":
    unittest.main()
