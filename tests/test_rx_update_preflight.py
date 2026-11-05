"""Проверки скрипта rx-update/scripts/preflight.py: python3 -m unittest discover -s tests

Без сети и без живой RX: разбор вывода команд на образцах, временные каталоги, HTTP-сервер на
127.0.0.1 внутри теста.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "skills", "rx-update", "scripts", "preflight.py")
sys.dont_write_bytecode = True  # не оставлять __pycache__ в папке скила
spec = importlib.util.spec_from_file_location("rx_update_preflight", SCRIPT)
pf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pf)

GB = 1024 ** 3


def write(path, text="", mtime=None, size=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(text.encode("utf-8") if size is None else b"x" * size)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class DiskMemoryClockTest(unittest.TestCase):
    def test_path_arg(self):
        self.assertEqual(pf.parse_path_arg("/srv=40"), ("/srv", 40.0))
        self.assertEqual(pf.parse_path_arg("/srv=1,5"), ("/srv", 1.5))
        self.assertEqual(pf.parse_path_arg("/srv"), ("/srv", None))
        self.assertEqual(pf.parse_path_arg("C:\\RX"), ("C:\\RX", None))
        self.assertEqual(pf.parse_path_arg("/data/a=b"), ("/data/a=b", None))

    def test_disk_verdict(self):
        self.assertEqual(pf.disk_verdict(50 * GB, 100 * GB)[0], pf.OK)
        self.assertEqual(pf.disk_verdict(9 * GB, 100 * GB)[0], pf.WARN)
        self.assertEqual(pf.disk_verdict(4 * GB, 100 * GB)[0], pf.CRIT)
        st, detail = pf.disk_verdict(30 * GB, 100 * GB, need_gb=40)
        self.assertEqual(st, pf.CRIT)
        self.assertIn("нужно не меньше 40 ГБ", detail)
        self.assertIn("30 ГБ", detail)

    def test_meminfo(self):
        mi = pf.parse_meminfo("MemTotal:       16000000 kB\nMemFree: 1000000 kB\n"
                              "MemAvailable:    1500000 kB\nSwapTotal: 4000000 kB\nSwapFree: 3000000 kB\n")
        self.assertEqual(mi["MemTotal"], 16000000 * 1024)
        st, detail = pf.memory_verdict(mi)
        self.assertEqual(st, pf.CRIT)  # 9 %
        self.assertIn("swap занят", detail)
        mi["MemAvailable"] = 3000000 * 1024
        self.assertEqual(pf.memory_verdict(mi)[0], pf.WARN)
        mi["MemAvailable"] = 8000000 * 1024
        self.assertEqual(pf.memory_verdict(mi)[0], pf.OK)
        old = pf.parse_meminfo("MemTotal: 1000 kB\nMemFree: 500 kB\nBuffers: 0 kB\nCached: 0 kB\n")
        self.assertEqual(pf.memory_verdict(old)[0], pf.OK)
        self.assertEqual(pf.memory_verdict({})[0], pf.ERROR)

    def test_timedatectl(self):
        self.assertTrue(pf.parse_timedatectl("Timezone=Europe/Moscow\nNTP=yes\nNTPSynchronized=yes\n"))
        self.assertFalse(pf.parse_timedatectl("NTPSynchronized=no\n"))
        self.assertTrue(pf.parse_timedatectl("      System clock synchronized: yes\n              NTP service: active\n"))
        self.assertFalse(pf.parse_timedatectl("NTP synchronized: no\n"))
        self.assertIsNone(pf.parse_timedatectl("System has not been booted with systemd as init system\n"))

    def test_chronyc(self):
        out = ("Reference ID    : C0000201 (192.0.2.1)\nStratum         : 3\n"
               "System time     : 0.000012345 seconds fast of NTP time\nLeap status     : Normal\n")
        self.assertEqual(pf.parse_chronyc_tracking(out), (True, 0.000012345))
        synced, _ = pf.parse_chronyc_tracking("Leap status     : Not synchronised\n")
        self.assertFalse(synced)
        self.assertEqual(pf.parse_chronyc_tracking("garbage"), (None, None))


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.now = time.time()

    def tearDown(self):
        shutil.rmtree(self.d)

    def verdict(self, pattern="*", max_age=26):
        files, _ = pf.list_files(self.d, pattern)
        return pf.backup_verdict(files, self.now, max_age)

    def test_empty_and_old(self):
        self.assertEqual(self.verdict()[0], pf.CRIT)
        write(os.path.join(self.d, "rx-old.dump"), size=1000, mtime=self.now - 40 * 3600)
        st, detail, hint = self.verdict()
        self.assertEqual(st, pf.CRIT)
        self.assertIn("старше 26 ч", detail)

    def test_fresh_ok_and_pattern(self):
        write(os.path.join(self.d, "rx-1.dump"), size=1000, mtime=self.now - 30 * 3600)
        write(os.path.join(self.d, "sub", "rx-2.dump"), size=1100, mtime=self.now - 3 * 3600)
        write(os.path.join(self.d, "notes.txt"), size=10, mtime=self.now - 600)
        st, detail, _ = self.verdict("*.dump")
        self.assertEqual(st, pf.OK, detail)
        self.assertIn("rx-2.dump", detail)
        self.assertIn("notes.txt", self.verdict()[1])

    def test_empty_file_writing_and_small(self):
        write(os.path.join(self.d, "a.dump"), size=1000, mtime=self.now - 7200)
        write(os.path.join(self.d, "b.dump"), size=0, mtime=self.now - 3600)
        self.assertEqual(self.verdict()[0], pf.CRIT)
        write(os.path.join(self.d, "b.dump"), size=400, mtime=self.now - 3600)
        st, detail, hint = self.verdict()
        self.assertEqual(st, pf.WARN)
        self.assertIn("вдвое меньше", hint)
        write(os.path.join(self.d, "c.dump"), size=1000, mtime=self.now - 30)
        self.assertIn("допишется", self.verdict()[2])

    def test_depth_limit(self):
        write(os.path.join(self.d, "1", "2", "3", "4", "deep.dump"), size=10, mtime=self.now - 60 * 60)
        files, _ = pf.list_files(self.d, "*", max_depth=3)
        self.assertEqual(files, [])


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_path_problems(self):
        self.assertEqual(pf.launcher_path_problems("/srv/DirectumLauncher"), [])
        self.assertEqual(pf.launcher_path_problems("C:\\DirectumLauncher"), [])
        probs = pf.launcher_path_problems("/home/админ/Directum Launcher,old#1")
        self.assertIn("пробелы", probs)
        self.assertIn("кириллица", probs)
        self.assertIn("запятые", probs)
        self.assertTrue(any("#" in p for p in probs), probs)

    def test_pre_checks(self):
        dl = os.path.join(self.d, "DL")
        write(os.path.join(dl, "directumrx.tar.gz"), size=10)
        write(os.path.join(dl, "platform.tar.gz"), size=20)
        write(os.path.join(dl, "web.pfx"), size=5)
        write(os.path.join(dl, "etc", "config.yml"), "x: 1\n")
        rep = pf.Report("pre")
        pf.check_launcher(rep, dl, post=False)
        by_id = dict((c["id"], c) for c in rep.checks)
        self.assertEqual(by_id["launcher-old"]["status"], pf.WARN)
        self.assertEqual(by_id["launcher-files"]["status"], pf.WARN)
        self.assertIn("web.pfx", by_id["launcher-files"]["detail"])
        self.assertIn("platform.tar.gz", by_id["launcher-archives"]["detail"])
        self.assertEqual(by_id["launcher-path"]["status"], pf.OK)

    def test_scan_log(self):
        text = ("2026-11-05 01:00:00,100 INFO start\n"
                "2026-11-05 01:05:00,100 ERROR root action {component_name} already exists: 'x'\n"
                "2026-11-05 01:06:00,100 INFO 'Region' - FAILED\n"
                "2026-11-05 01:07:00,100 INFO run dt run --command=\"-n \\\"Service User\\\" -p S3cr3tPw --restore-fk\"\n"
                "2026-11-05 01:08:00,100 ERROR something --password=Hunter22 broke\n")
        res = pf.scan_log_text(text)
        slugs = sorted(k["slug"] for k in res["known"])
        self.assertEqual(slugs, ["directum-launcher-root-action-already-exists", "rx-restore-fk-failed"])
        self.assertEqual(res["errors"], 2)
        joined = json.dumps(res, ensure_ascii=False)
        self.assertNotIn("Hunter22", joined)
        line = pf.mask_line("INFO dt run --command=\"-n \\\"Service User\\\" -p S3cr3tPw --restore-fk\"")
        self.assertNotIn("S3cr3tPw", line)
        self.assertIn("--restore-fk", line)

    def test_last_session(self):
        text = ("2026-11-04 10:00:00,1 INFO old run\n2026-11-04 10:05:00,1 INFO old end\n"
                "2026-11-05 01:00:00,1 INFO new\n2026-11-05 01:20:00,1 INFO conv\n2026-11-05 02:35:10,1 INFO done\n"
                "no timestamp line\n")
        start, end = pf.last_session(text)
        self.assertEqual(start.strftime("%d %H:%M"), "05 01:00")
        self.assertEqual(end.strftime("%H:%M:%S"), "02:35:10")
        self.assertIsNone(pf.last_session("no stamps"))

    def test_post_logs(self):
        dl = os.path.join(self.d, "DL")
        write(os.path.join(dl, "log", "dl.log"), "2026-11-05 01:00:00,1 INFO a\n2026-11-05 02:00:00,1 INFO b\n")
        write(os.path.join(dl, "log", "DBConverter.log"),
              "2026-11-05 01:10:00.123 Info start\n2026-11-05 01:50:00.123 Info Ошибка с кодом 503\n")
        rep = pf.Report("post")
        pf.check_launcher(rep, dl, post=True)
        by_id = dict((c["id"], c) for c in rep.checks)
        self.assertEqual(by_id["dl-log:dl.log"]["status"], pf.OK)
        self.assertEqual(by_id["dl-log:DBConverter.log"]["status"], pf.WARN)
        self.assertIn("directum-launcher-error-503", by_id["dl-log:DBConverter.log"]["detail"])
        self.assertIn("1 ч", by_id["dl-time:dl.log"]["detail"])
        self.assertIn("40 мин", by_id["dl-time:DBConverter.log"]["detail"])


class StatusFilesTest(unittest.TestCase):
    HEALTHY = {"Time": "2026-11-05 10:00:00.000+03:00", "Status": "healthy", "IsReady": True,
               "Statuses": {"PROCESS_MEMORY": {"Status": "healthy", "Duration": "00:00:00.001"}}}

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.now = time.time()

    def tearDown(self):
        shutil.rmtree(self.d)

    def put(self, name, data, age=5):
        write(os.path.join(self.d, name), json.dumps(data), mtime=self.now - age)

    def run_check(self, post):
        rep = pf.Report("post" if post else "pre")
        pf.check_status_dir(rep, self.d, self.now, post)
        return rep

    def test_split_name(self):
        self.assertEqual(pf.split_status_name("node01.example.com.WebServer.status"), ("node01.example.com", "WebServer"))
        self.assertEqual(pf.split_status_name("Worker.status"), ("", "Worker"))

    def test_parse(self):
        data = dict(self.HEALTHY, Status="degraded",
                    Statuses={"QUEUE": {"Status": "degraded"}, "THREAD_POOL": {"Status": "healthy"}},
                    DeploymentPackage="Not actual")
        p = pf.parse_status_text("\ufeff" + json.dumps(data))
        self.assertEqual(p["status"], "degraded")
        self.assertEqual(p["bad"], ["QUEUE degraded"])
        self.assertEqual(p["deployment_package"], "Not actual")
        nested = dict(self.HEALTHY, Statuses={"DEPLOYMENT_PACKAGE": {"Status": "healthy", "Data": {"State": "Actual"}}})
        self.assertEqual(pf.parse_status_text(json.dumps(nested))["deployment_package"], "Actual")
        self.assertIsNone(pf.parse_status_text(json.dumps(self.HEALTHY))["deployment_package"])

    def test_dir_verdicts(self):
        self.put("n1.WebServer.status", dict(self.HEALTHY, DeploymentPackage={"Status": "Actual"}))
        self.put("n1.Worker.status", self.HEALTHY)
        self.put("oldpod.Worker.status", self.HEALTHY, age=3600)  # старый экземпляр: не ошибка
        rep = self.run_check(post=True)
        st = rep.checks[0]
        self.assertEqual(st["status"], pf.OK, st["detail"])
        self.assertIn("Старые экземпляры", st["detail"])
        self.assertEqual(rep.checks[1]["detail"], "DeploymentPackage: Actual")
        self.put("n1.PreviewStorage.status", dict(self.HEALTHY, Status="unhealthy",
                                                  Statuses={"PROCESS_MEMORY": {"Status": "unhealthy"}}))
        self.assertEqual(self.run_check(post=True).checks[0]["status"], pf.CRIT)

    def test_stale(self):
        self.put("n1.IndexingService.status", self.HEALTHY, age=900)
        self.assertEqual(self.run_check(post=False).checks[0]["status"], pf.WARN)
        self.assertEqual(self.run_check(post=True).checks[0]["status"], pf.CRIT)

    def test_empty_and_broken(self):
        self.assertEqual(self.run_check(post=False).checks[0]["status"], pf.WARN)
        write(os.path.join(self.d, "n1.Log.status"), "{broken", mtime=self.now)
        rep = self.run_check(post=False)
        self.assertEqual(rep.checks[0]["status"], pf.WARN)
        self.assertIn("не разобрать", rep.checks[0]["detail"])


class ContainersTest(unittest.TestCase):
    PS = ("sungerowebserver\trx/webserver:25.2.0.40\tUp 2 hours (healthy)\n"
          "worker\trx/worker:25.2.0.40\tUp 5 minutes\n"
          "dtc\trx/dtc:25.2.0.40\tExited (2) 3 minutes ago\n"
          "init\trx/init:25.2.0.40\tExited (0) 1 hour ago\n")

    def test_state(self):
        self.assertEqual(pf.container_state("Up 3 days (healthy)"), "running")
        self.assertEqual(pf.container_state("Up 10 seconds (health: starting)"), "starting")
        self.assertEqual(pf.container_state("Up 2 minutes (unhealthy)"), "unhealthy")
        self.assertEqual(pf.container_state("Restarting (1) 5 seconds ago"), "restarting")
        self.assertEqual(pf.container_state("Exited (137) 2 hours ago"), "exited:137")
        self.assertEqual(pf.container_state("Created"), "created")

    def test_verdicts(self):
        rows = pf.parse_ps(self.PS, "docker")
        self.assertEqual(len(rows), 4)
        st, detail = pf.containers_verdict(rows, post=False)
        self.assertEqual(st, pf.WARN)
        self.assertIn("dtc (2)", detail)
        self.assertNotIn("init", detail)
        rows[1]["state"] = "unhealthy"
        self.assertEqual(pf.containers_verdict(rows, post=False)[0], pf.WARN)
        self.assertEqual(pf.containers_verdict(rows, post=True)[0], pf.CRIT)
        rows[0]["state"] = "restarting"
        self.assertEqual(pf.containers_verdict(rows, post=False)[0], pf.CRIT)
        stopped = pf.parse_ps("a\timg:1\tExited (0) 1 minute ago\n", "podman")
        self.assertEqual(pf.containers_verdict(stopped, post=True)[0], pf.CRIT)
        self.assertEqual(pf.containers_verdict(stopped, post=False)[0], pf.OK)


def pod(name, rs, h, image, ready=True, waiting=None, phase="Running", restarts=0, last=None):
    meta = {"name": name, "namespace": "rx", "labels": {"pod-template-hash": h} if h else {},
            "ownerReferences": [{"kind": "ReplicaSet" if h else "StatefulSet", "name": rs}]}
    cs = {"name": "main", "ready": ready, "restartCount": restarts, "imageID": "sha256:" + image[-2:],
          "state": {"waiting": {"reason": waiting}} if waiting else {"running": {}}}
    if last:
        cs["lastState"] = {"terminated": {"reason": last}}
    return {"metadata": meta, "spec": {"containers": [{"name": "main", "image": image}]},
            "status": {"phase": phase, "containerStatuses": [cs]}}


class K8sTest(unittest.TestCase):
    def test_parse_and_verdict(self):
        data = {"items": [pod("web-7d9f8b6c5d-abcde", "web-7d9f8b6c5d", "7d9f8b6c5d", "rx/web:25.2.0.40"),
                          pod("db-0", "db", None, "rx/db:1", ready=True),
                          pod("job-x", "job", None, "rx/job:1", ready=False, phase="Succeeded")]}
        pods = pf.parse_pods(data)
        self.assertEqual(pods[0]["base"], "web")
        self.assertEqual(pods[1]["base"], "db")
        self.assertEqual(pf.pods_verdict(pods, post=True)[0], pf.OK)
        data["items"].append(pod("w-1-x", "w-1", "1", "rx/w:2", ready=False, waiting="CrashLoopBackOff", restarts=7))
        st, detail = pf.pods_verdict(pf.parse_pods(data), post=False)
        self.assertEqual(st, pf.CRIT)
        self.assertIn("CrashLoopBackOff", detail)
        self.assertIn("перезапусков всего 7", detail)
        nr = pf.parse_pods({"items": [pod("a-1-b", "a-1", "1", "i:1", ready=False)]})
        self.assertEqual(pf.pods_verdict(nr, post=True)[0], pf.WARN)
        oom = pf.parse_pods({"items": [pod("a-1-b", "a-1", "1", "i:1", last="OOMKilled", restarts=1)]})
        self.assertIn("OOMKilled", pf.pods_verdict(oom, post=False)[1])
        self.assertEqual(pf.pods_verdict([], post=True)[0], pf.CRIT)

    def test_diff(self):
        before = {"containers": [{"runtime": "docker", "name": "web", "image": "rx/web:25.2.0.37", "image_id": "a"},
                                 {"runtime": "docker", "name": "old", "image": "rx/old:1", "image_id": "b"},
                                 {"runtime": "docker", "name": "same", "image": "rx/s:1", "image_id": "c"}],
                  "pods": pf.parse_pods({"items": [pod("api-5-x", "api-5", "5", "rx/api:1")]})}
        now = {"containers": [{"runtime": "docker", "name": "web", "image": "rx/web:25.2.0.40", "image_id": "d"},
                              {"runtime": "docker", "name": "same", "image": "rx/s:1", "image_id": "e"},
                              {"runtime": "docker", "name": "new", "image": "rx/n:1", "image_id": "f"}],
               "pods": pf.parse_pods({"items": [pod("api-6-y", "api-6", "6", "rx/api:2")]})}
        d = pf.diff_inventory(before, now)
        self.assertIn(("docker:web", "rx/web:25.2.0.37", "rx/web:25.2.0.40"), d["changed"])
        self.assertIn(("k8s:rx/api/main", "rx/api:1", "rx/api:2"), d["changed"])
        self.assertEqual(d["rebuilt"], ["docker:same"])
        self.assertEqual(d["gone"], ["docker:old"])
        self.assertEqual(d["new"], ["docker:new"])


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        routes = {
            "/ok": (200, {"Status": "Healthy"}),
            "/degraded": (200, {"Status": "degraded", "Statuses": {"QUEUE": {"Status": "degraded"}}}),
            "/down": (503, {"Status": "unhealthy"}),
            "/deny": (403, None),
            "/redir": (302, None),
        }
        code, body = routes.get(self.path, (404, None))
        self.send_response(code)
        if code == 302:
            self.send_header("Location", "/Account/Login")
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if body is not None:
            self.wfile.write(json.dumps(body).encode())

    def log_message(self, *args):
        pass


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def probe(self, path):
        return pf.classify_http(pf.http_probe(self.base + path, timeout=5))

    def test_codes(self):
        self.assertEqual(self.probe("/ok")[0], pf.OK)
        st, detail, _ = self.probe("/degraded")
        self.assertEqual(st, pf.WARN)
        self.assertIn("QUEUE degraded", detail)
        self.assertEqual(self.probe("/down")[0], pf.CRIT)
        self.assertEqual(self.probe("/deny")[0], pf.WARN)
        st, detail, _ = self.probe("/redir")
        self.assertEqual(st, pf.WARN)
        self.assertIn("/Account/Login", detail)
        self.assertEqual(self.probe("/missing")[0], pf.WARN)

    def test_errors(self):
        srv = HTTPServer(("127.0.0.1", 0), _Handler)
        port = srv.server_address[1]
        srv.server_close()  # порт свободен: соединение отклонят
        st, detail, _ = pf.classify_http(pf.http_probe("http://127.0.0.1:%d/x" % port, timeout=3))
        self.assertEqual(st, pf.CRIT)
        st, detail, _ = pf.classify_http(pf.http_probe("not a url", timeout=3))
        self.assertEqual(st, pf.CRIT)
        self.assertIn("неверный адрес", detail)
        res = {"error": "[SSL: CERTIFICATE_VERIFY_FAILED] certificate has expired", "tls": True,
               "code": None, "ms": 5, "body": "", "location": None, "date": None}
        self.assertIn("TLS", pf.classify_http(res)[1])

    def test_skew(self):
        now = time.time()
        self.assertIsNone(pf.clock_skew(None, now))
        self.assertIsNone(pf.clock_skew("garbage", now))
        from email.utils import formatdate
        self.assertLess(abs(pf.clock_skew(formatdate(now + 300, usegmt=True), now) - 300), 2)


class ReportCliTest(unittest.TestCase):
    def test_exit_codes(self):
        rep = pf.Report("pre")
        rep.add("a", "A", pf.OK)
        rep.add("b", "B", pf.SKIP)
        self.assertEqual(rep.exit_code(), 0)
        rep.add("c", "C", pf.ERROR)
        self.assertEqual(rep.exit_code(), 2)
        rep.add("d", "D", pf.WARN)
        self.assertEqual(rep.exit_code(), 1)

    def test_detail_masked(self):
        rep = pf.Report("pre")
        rep.add("x", "X", pf.WARN, "строка: Password=Sup3rSecret;")
        self.assertNotIn("Sup3rSecret", rep.checks[0]["detail"])

    def cli(self, *args):
        p = subprocess.run([sys.executable, SCRIPT] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8")

    def test_help(self):
        code, out, _ = self.cli("--help")
        self.assertEqual(code, 0)
        self.assertIn("--backup-dir", out)
        self.assertIn("Код возврата", out)

    def test_json_and_before(self):
        d = tempfile.mkdtemp()
        try:
            write(os.path.join(d, "b", "rx.dump"), size=100, mtime=time.time() - 3600)
            before = os.path.join(d, "before.json")
            with open(before, "w") as f:
                json.dump({"host": "other", "inventory": {"containers": [
                    {"runtime": "docker", "name": "web", "image": "rx/web:1", "image_id": ""}]}}, f)
            code, out, _ = self.cli("--json", "--no-containers", "--path", d, "--backup-dir", os.path.join(d, "b"))
            data = json.loads(out)
            for key in ("tool", "version", "host", "time", "mode", "checks", "summary", "inventory", "manual"):
                self.assertIn(key, data)
            self.assertEqual(data["mode"], "pre")
            self.assertIn(code, (0, 1, 2))
            ids = [c["id"] for c in data["checks"]]
            self.assertIn("backup", ids)
            backup = [c for c in data["checks"] if c["id"] == "backup"][0]
            self.assertEqual(backup["status"], "OK")
            code, out, _ = self.cli("--post", "--no-containers", "--before", before, "--path", d)
            self.assertIn("после обновления", out)
            self.assertIn("Проверьте руками", out)
            self.assertIn("контейнеры в этом запуске не смотрели", out)
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
