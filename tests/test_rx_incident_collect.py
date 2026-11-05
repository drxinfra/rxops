"""Проверки collect.sh на заглушках docker, podman, kubectl и journalctl: python3 -m unittest discover -s tests

Настоящие docker, podman и kubectl тестом не вызываются: заглушки стоят первыми в PATH,
а KUBECONFIG указывает в никуда, чтобы случайно не обратиться к рабочему кластеру.
"""
import datetime
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "skills", "rx-incident", "scripts", "collect.sh")
BASH = shutil.which("bash")

DOCKER = r"""#!/bin/sh
case "$*" in
  "ps") echo "CONTAINER ID   IMAGE"; exit 0 ;;
  "ps -aq") printf 'a1\nb2\nc3\n'; exit 0 ;;
  "ps -a --format"*) printf 'NAMES\tSTATUS\tIMAGE\nrx-webserver\tUp 3 days\trx/web\nrx-worker\tRestarting (137) 5 seconds ago\trx/worker\nrx-postgres\tExited (1) 2 hours ago\tpostgres:16\n'; exit 0 ;;
  "inspect -f"*) printf '/rx-webserver status=running restarts=0 oom=false exit=0 started=2026-11-02T10:00:00Z\n/rx-worker status=restarting restarts=34 oom=true exit=137 started=2026-11-05T10:00:00Z\n/rx-postgres status=exited restarts=0 oom=false exit=1 started=2026-11-05T08:00:00Z\n'; exit 0 ;;
  "stats --no-stream --format"*) printf 'NAME\tCPU %%\tMEM USAGE / LIMIT\tMEM %%\nrx-webserver\t1.50%%\t50MiB / 100MiB\t50.00%%\nrx-elasticsearch\t50.00%%\t999MiB / 1GiB\t97.60%%\n'; exit 0 ;;
esac
exit 1
"""

KUBECTL = r"""#!/bin/sh
case "$*" in
  "get nodes --request-timeout=5s") echo "NAME STATUS"; exit 0 ;;
  "get nodes -o wide"*) printf 'NAME    STATUS     ROLES\nnode1   Ready      control-plane\nnode2   NotReady   <none>\n'; exit 0 ;;
  "get nodes --no-headers"*) printf 'node1   Ready      control-plane   10d   v1.30.4\nnode2   NotReady   <none>   10d   v1.30.4\n'; exit 0 ;;
  "get pods -A -o jsonpath="*)
    printf 'rx\trx-web-1\tRunning\tweb|true|0|||;\n'
    printf 'rx\trx-worker-2\tRunning\tworker|false|12|CrashLoopBackOff|OOMKilled|;\n'
    printf 'rx\trx-preview-3\tPending\t\n'
    printf 'rx\tjob-1\tSucceeded\tjob|false|0|||;\n'; exit 0 ;;
  "get events -A"*) printf 'NAMESPACE LAST SEEN TYPE REASON OBJECT MESSAGE\nrx 1m Warning BackOff pod/rx-worker-2 Back-off\n'; exit 0 ;;
  "top nodes"*|"top pods"*) echo "error: Metrics API not available" >&2; exit 1 ;;
esac
exit 1
"""

JOURNALCTL = r"""#!/bin/sh
case "$*" in
  *"-p err"*) echo "Nov 05 10:00:01 rx01 app[1]: connect failed Host=db;Password=Sup3rS3cret; timeout"; exit 0 ;;
  *"-k"*) echo "Nov 05 09:58:00 rx01 kernel: Memory cgroup out of memory: Killed process 4321 (dotnet)"; exit 0 ;;
esac
exit 1
"""

FAIL = "#!/bin/sh\nexit 1\n"


def stamp(t):
    return t.strftime("%Y-%m-%d %H:%M:%S.000+03:00")


@unittest.skipIf(BASH is None, "нет bash")
class CollectTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rxops-incident-")
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        for name, body in (("docker", DOCKER), ("kubectl", KUBECTL), ("journalctl", JOURNALCTL), ("podman", FAIL)):
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write(body)
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.logs = os.path.join(self.dir, "logs")
        os.makedirs(os.path.join(self.logs, "status"))
        msk = datetime.timezone(datetime.timedelta(hours=3))
        now = datetime.datetime.now(msk)
        self.status("rx01.WebServer.status", now, "healthy", {"DATABASE": "healthy"})
        self.status("rx01.IndexingService.status", now, "degraded", {"INDEXING_SERVICE": "degraded"})
        self.status("rx01.Worker.status", now - datetime.timedelta(minutes=30), "unhealthy", {"QUEUE": "unhealthy"},
                    last=now - datetime.timedelta(minutes=50))

    def tearDown(self):
        shutil.rmtree(self.dir)

    def status(self, name, t, status, checks, last=None):
        data = {"Time": stamp(t), "LastNotUnhealthyTime": stamp(last or t), "Status": status,
                "Statuses": {k: {"Status": v, "Duration": "00:00:00.0010000"} for k, v in checks.items()}}
        with open(os.path.join(self.logs, "status", name), "w") as f:
            json.dump(data, f)

    def run_collect(self, *args):
        env = dict(os.environ)
        env["PATH"] = self.bin + os.pathsep + env.get("PATH", "")
        env["KUBECONFIG"] = os.path.join(self.dir, "no-kubeconfig")
        env["LC_ALL"] = env["LANG"] = "C.UTF-8"
        p = subprocess.run([BASH, SCRIPT] + list(args), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=300)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")

    def test_help_and_bad_args(self):
        code, out, _ = self.run_collect("--help")
        self.assertEqual(code, 0)
        self.assertIn("Коды возврата", out)
        self.assertEqual(self.run_collect("--bogus")[0], 2)
        self.assertEqual(self.run_collect("--lines", "много")[0], 2)
        self.assertEqual(self.run_collect("--since")[0], 2)

    def test_deviations_from_stubs(self):
        code, out, err = self.run_collect("--lines", "5", "--logs", self.logs)
        self.assertEqual(code, 1, out + err)
        summary = out.split("=== Итог ===", 1)[1]
        for want in ("rx-worker: перезапускается по кругу", "rx-worker: перезапусков 34",
                     "rx-postgres: остановлен с кодом 1", "rx-elasticsearch: 97.60% памяти от лимита",
                     "узлы Kubernetes не в Ready: node2 NotReady", "подов не в порядке: 2",
                     "убийства процессов по памяти в журнале ядра: 1",
                     "rx01.IndexingService.status: статус degraded", "rx01.Worker.status: статус unhealthy",
                     "rx01.Worker.status: не обновлялся"):
            self.assertIn(want, summary)
        self.assertNotIn("rx-webserver", summary)
        self.assertNotIn("rx01.WebServer.status", summary)
        self.assertIn("rx/rx-worker-2 phase=Running ready=0/1 restarts=12 waiting=CrashLoopBackOff last=OOMKilled", out)
        self.assertIn("rx/rx-preview-3 phase=Pending", out)
        self.assertNotIn("job-1", out)
        self.assertIn("не в норме: INDEXING_SERVICE=degraded", out)
        self.assertIn("podman есть, но не отвечает", out)

    def test_output_is_masked(self):
        code, out, _ = self.run_collect("--lines", "5")
        self.assertNotIn("Sup3rS3cret", out)
        self.assertIn("Password=‹скрыто›", out)

    def test_missing_logs_dir_is_a_deviation(self):
        code, out, _ = self.run_collect("--lines", "5", "--logs", os.path.join(self.dir, "nope"))
        self.assertEqual(code, 1)
        self.assertIn("не найдена", out.split("=== Итог ===", 1)[1])


if __name__ == "__main__":
    unittest.main()
