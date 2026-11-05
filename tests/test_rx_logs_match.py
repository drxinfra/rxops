"""Проверки match_errors.py и базы известных ошибок: python3 -m unittest discover -s tests"""
import datetime
import gzip
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skills", "rx-logs", "scripts")
sys.path.insert(0, SCRIPTS)
import match_errors as M  # noqa: E402

BASE_FILE = os.path.join(SCRIPTS, "known_errors.json")
SCRIPT = os.path.join(SCRIPTS, "match_errors.py")
BASE = M.Base(BASE_FILE)
SLUG = {e["slug"]: i for i, e in enumerate(BASE.entries)}

# Записи, чей заголовок на сайте пересказывает ошибку, а не повторяет строку журнала, и собственные
# шаблоны его не находят. Сейчас таких нет: шаблоны пересказов («column ... does not exist») покрывают
# и заголовок, и настоящую строку. Если запись отсюда начала совпадать, уберите её из списка; новая
# запись, которая не совпадает, роняет тест.
TITLE_NOT_LITERAL = set()


def winner(line):
    """Слаг ошибки, которой засчитывается строка, или None."""
    rec = M.Record(line)
    lows = [t.lower() for t in rec.texts]
    if not any(BASE.passes(l) for l in lows):
        return None
    sc = BASE.scores(rec.texts, lows)
    return BASE.entries[BASE.winner(sc, lows)]["slug"] if sc else None


def run(*args, stdin=None):
    p = subprocess.run([sys.executable, SCRIPT] + list(args), input=stdin,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8")


def brute(line):
    """Те же правила без быстрого отсева: эталон для сравнения."""
    out = {}
    for i, e in enumerate(BASE.entries):
        for p in e["patterns"]:
            rx = re.compile(M.js_to_py(p), re.I | re.M)
            ok = None
            m = M._BARE_CODE.match(p)
            if m and sum(c.isdigit() for c in m.group(1)) >= 3:
                ok = M._code_ok
            elif M._BARE_NAME.match(p):
                ok = M._name_ok
            hit = M.Rule(i, rx, ok).best([line])
            if hit:
                s = out.setdefault(i, [0, 0])
                s[0] = max(s[0], len(hit))
                s[1] += 1
    return out


class BaseTest(unittest.TestCase):
    def test_base_loads(self):
        self.assertGreater(len(BASE.entries), 150)
        self.assertEqual(BASE.bad, [], "шаблоны, которые re не разобрал")
        self.assertEqual(BASE.always, [], "шаблоны без обязательной подстроки замедляют разбор")
        for e in BASE.entries:
            for k in ("slug", "title", "what", "component", "patterns", "url", "check", "fix"):
                self.assertIn(k, e)
            self.assertTrue(e["url"].startswith("https://drxinfra.ru/errors/"))
            self.assertNotIn("](", e["check"] + e["fix"], "markdown-ссылка не переведена в текст")

    def test_every_entry_matches_own_title(self):
        missing, fixed = [], []
        for e in BASE.entries:
            line = "2026-11-05 10:00:00.000+03:00 ERROR " + e["title"]
            got = winner(line)
            rec = M.Record(line)
            sc = BASE.scores(rec.texts, [t.lower() for t in rec.texts])
            own = SLUG[e["slug"]] in sc
            if not own and e["slug"] not in TITLE_NOT_LITERAL:
                missing.append(e["slug"])
            if own and e["slug"] in TITLE_NOT_LITERAL:
                fixed.append(e["slug"])
            if own:
                self.assertEqual(got, e["slug"], "строку с заголовком забрала другая ошибка")
        if TITLE_NOT_LITERAL:
            sys.stderr.write("\n  записи, чей заголовок не строка журнала: %d (%s)\n"
                             % (len(TITLE_NOT_LITERAL), ", ".join(sorted(TITLE_NOT_LITERAL))))
        self.assertEqual(missing, [], "заголовок не находит свою запись")
        self.assertEqual(fixed, [], "заголовок теперь совпадает: уберите из TITLE_NOT_LITERAL")

    def test_prefilter_never_loses_matches(self):
        corpus = []
        for e in BASE.entries:
            corpus.append(e["title"])
            corpus += [l for l in (e["check"] + "\n" + e["fix"]).split("\n") if l.strip()]
        for line in corpus:
            low = line.lower()
            fast = BASE.scores([line], [low]) if BASE.passes(low) else {}
            self.assertEqual({k: v[:2] for k, v in fast.items()}, brute(line), line)


class PatternTest(unittest.TestCase):
    def test_literals(self):
        self.assertEqual(M.required_literals(r"exit(ed with)? code 137"), [" code 137"])
        self.assertEqual(M.required_literals(r"\bOOMKilled\b"), ["oomkilled"])
        self.assertEqual(M.required_literals(r"[Оо]шибка с кодом 503"), ["шибка с кодом 503"])
        self.assertEqual(M.required_literals(r'relation ".*" does not exist'), [" does not exist"])
        self.assertEqual(M.required_literals(r"abcd{0,3}xyz"), ["abc"])
        self.assertEqual(M.required_literals(r"foo\x41bar|bazz+"), ["foo", "bazz"])
        self.assertIsNone(M.required_literals(r"\d+ \w+|ab"))
        self.assertEqual(M.split_top(r"a(b|c)|[|]|d\|e"), ["a(b|c)", "[|]", r"d\|e"])

    def test_js_to_py(self):
        self.assertEqual(M.js_to_py(r"(?<code>\d+)"), r"(?P<code>\d+)")
        self.assertEqual(M.js_to_py(r"(?<=a)(?<!b)"), r"(?<=a)(?<!b)")
        self.assertEqual(M.js_to_py("a[^]b"), r"a[\s\S]b")

    def test_choose_keys_cover_all(self):
        lits = BASE.lits
        keys = M.choose_keys(lits)
        rx = re.compile(M.trie_pattern(keys))
        for l in lits:
            self.assertIsNotNone(rx.search(l), l)


class RecordTest(unittest.TestCase):
    def test_rx_json(self):
        line = json.dumps({
            "t": "2026-10-22 10:15:03.412+03:00", "l": "Error", "lg": "RemoteFunctionExecutor",
            "mt": "Error while executing remote function {name}", "args": {"name": "GetDocs"},
            "ex": {"type": "Npgsql.PostgresException", "m": "57014: canceling statement due to statement timeout",
                   "stack": "   at Npgsql.NpgsqlConnector.ReadMessage()"},
            "un": "DOMAIN\\petrov", "tr": "cl-9a1b2c3d-81fa02"}) + "\n"
        rec = M.Record(line)
        self.assertEqual(rec.when, datetime.datetime(2026, 10, 22, 7, 15, 3, 412000, M.UTC))
        self.assertIn("GetDocs", rec.display)
        self.assertIn("tr=cl-9a1b2c3d-81fa02", rec.display)
        self.assertEqual(winner(line), "postgresql-canceling-statement-due-to-statement-timeout")

    def test_unicode_escapes_and_quotes(self):
        line = json.dumps({"t": "2026-10-22 10:15:03.412+03:00", "l": "Error",
                           "mt": "Публикация: Ошибка с кодом 503"}, ensure_ascii=True)
        self.assertIn("\\u", line)
        self.assertEqual(winner(line), "directum-launcher-error-503")
        line = json.dumps({"l": "Error", "mt": 'ERROR: relation "sungero_core_x" does not exist'})
        self.assertEqual(winner(line), "postgresql-relation-does-not-exist")

    def test_webagent_lone_backslash(self):
        line = '{"t":"2026-10-22 10:15:03.000+03:00","l":"Error","mt":"Cannot open C:\\Program Files\\x.docx: ' \
               'The remote certificate is invalid according to the validation procedure"}'
        with self.assertRaises(ValueError):
            json.loads(line)
        rec = M.Record(line)
        self.assertIsNotNone(rec.obj, "строку веб-агента с одиночным слешем надо разобрать")
        self.assertEqual(winner(line), "tls-remote-certificate-is-invalid")

    def test_container_wrappers(self):
        inner = json.dumps({"t": "2026-10-22 10:15:03.412+03:00", "l": "Error",
                            "mt": "RabbitMQ connection 'x' recovery failed.",
                            "ex": {"type": "RabbitMQ.Client.Exceptions.BrokerUnreachableException",
                                   "m": "None of the specified endpoints were reachable"}})
        docker = json.dumps({"log": inner + "\n", "stream": "stdout", "time": "2026-10-22T07:15:03.5Z"})
        rec = M.Record(docker)
        self.assertEqual(rec.when.astimezone(M.UTC).hour, 7)
        self.assertEqual(winner(docker), "rabbitmq-broker-unreachable-exception")
        cri = "2026-10-22T07:15:03.123456789Z stderr F Out of memory: Killed process 4321 (dotnet)"
        rec = M.Record(cri)
        self.assertEqual(rec.when, datetime.datetime(2026, 10, 22, 7, 15, 3, 123456, M.UTC))
        self.assertEqual(winner(cri), "linux-out-of-memory-killed-process")

    def test_specific_beats_generic(self):
        self.assertEqual(winner('could not write to file "pg_wal/xlogtemp.1": No space left on device'),
                         "postgresql-no-space-left-pg-wal")
        self.assertEqual(winner("write error: No space left on device"), "linux-no-space-left-on-device")
        self.assertEqual(winner("Memory cgroup out of memory: Killed process 77 (dotnet)"),
                         "container-oomkilled-exit-code-137")
        self.assertEqual(winner("[Thu Nov  5 10:00:00 2026] Out of memory: Killed process 77 (java)"),
                         "linux-out-of-memory-killed-process")

    def test_bare_codes_need_context(self):
        self.assertIsNone(winner('{"l":"Info","mt":"Document (id = 57014, ver = 1) was loaded"}'))
        self.assertIsNone(winner('{"l":"Info","span":{"name":"Conversion","durationMs":23505}}'))
        self.assertEqual(winner("Npgsql.PostgresException: SqlState 23505"), "postgresql-duplicate-key-unique-constraint")
        self.assertEqual(winner("PostgresException: 40P01: deadlock"), "postgresql-deadlock-detected-40p01")
        # шаблоны SQLSTATE сами требуют контекста, без поправок в match_errors.py
        for line in ("документ 57014 сохранён", "Задача 23505 выполнена за 42601 мс", "ERROR Document 40P01 not found"):
            self.assertIsNone(winner(line), line)
        self.assertEqual(winner("ERROR:  57014: canceling statement"), "postgresql-canceling-statement-due-to-statement-timeout")
        self.assertEqual(winner('{"l":"Error","ex":{"SqlState":"55P03"}}'), "postgresql-canceling-statement-lock-timeout")

    def test_healthy_status_is_not_error(self):
        self.assertIsNone(winner('"INDEXING_SERVICE": {"Status": "healthy", "Duration": "00:00:00.001"}'))
        self.assertEqual(winner("INDEXING_SERVICE: degraded"), "rx-healthcheck-degraded-statuses")
        for line in ("INDEXING_SERVICE healthy", "PREVIEW_STORAGE: Healthy",
                     '{"INDEXING_SERVICE": {"Status": "healthy"}, "PREVIEW_STORAGE": {"Status": "healthy"}}'):
            self.assertIsNone(winner(line), line)
        self.assertEqual(winner('"PREVIEW_STORAGE": {"Status": "Degraded"}'), "rx-healthcheck-degraded-statuses")

    def test_generic_words_need_context(self):
        for line in ("Binary data to transfer = 10. Processed = 10. Left = 0. Transfer fails = 0.",
                     "4 drives online, 0 drives offline", "at Sungero.Docflow.Server.Foo.get_Id()",
                     '{"l":"Info","lg":"PreviewCreator","mt":"Preview created"}',
                     "LDAP bind: invalid credentials"):
            self.assertIsNone(winner(line), line)

    def test_real_lines_for_paraphrased_titles(self):
        cases = {
            'ERROR:  column "statusx" does not exist at character 8': "postgresql-column-does-not-exist",
            "FATAL:  requested WAL segment 00000001000000A0000000FF has already been removed":
                "postgresql-requested-wal-segment-removed",
            "sshd[811]: error: connect_to 10.0.0.5 port 5432: failed.": "ssh-connect-to-port-failed",
            "RabbitMQ connection 'rx-worker' recovery failed.": "rx-rabbitmq-connection-recovery-failed",
            'MountVolume.SetUp failed for volume "cfg" : object "rx"/"rx-config" not registered':
                "kubernetes-failedmount-object-not-registered",
            'Readiness probe failed: command "rabbitmq-diagnostics ping" timed out': "kubernetes-exec-probe-timed-out",
        }
        for line, slug in cases.items():
            self.assertEqual(winner(line), slug, line)


class TimeTest(unittest.TestCase):
    def local(self, *a):
        return datetime.datetime(*a).astimezone()

    def test_line_formats(self):
        utc = M.UTC
        cases = [
            ('{"t":"2026-10-22 10:15:03.412+03:00","l":"Error"}', datetime.datetime(2026, 10, 22, 7, 15, 3, 412000, utc)),
            ("2026-10-22 10:15:03.345 UTC [123] ERROR:  x", datetime.datetime(2026, 10, 22, 10, 15, 3, 345000, utc)),
            ("2026-10-22 10:15:03.345 MSK [123] ERROR:  x", self.local(2026, 10, 22, 10, 15, 3, 345000)),
            ("2026/10/22 10:15:03 [error] 7#7: *1 upstream timed out", self.local(2026, 10, 22, 10, 15, 3)),
            ('10.0.0.1 - - [22/Oct/2026:10:15:03 +0300] "GET / HTTP/1.1" 504',
             datetime.datetime(2026, 10, 22, 7, 15, 3, tzinfo=utc)),
            ("[2026-10-22T10:15:03,123][WARN ][o.e.c.r.a.DiskThresholdMonitor] x", self.local(2026, 10, 22, 10, 15, 3, 123000)),
            ("[Thu Oct 22 10:15:03 2026] Out of memory", self.local(2026, 10, 22, 10, 15, 3)),
            ("2026-10-22T10:15:03+0300 host kernel: x", datetime.datetime(2026, 10, 22, 7, 15, 3, tzinfo=utc)),
        ]
        for line, want in cases:
            self.assertEqual(M.line_time(line)[0], want, line)
        when, raw = M.line_time("Oct 22 10:15:03 host kernel: x")
        self.assertEqual((when.month, when.day, when.hour), (10, 22, 10))
        self.assertEqual(M.line_time("   at Foo.Bar()"), (None, None))

    def test_bounds(self):
        now = datetime.datetime(2026, 11, 5, 12, 0, tzinfo=M.UTC)
        self.assertEqual(M.parse_bound("2h", now), datetime.datetime(2026, 11, 5, 10, 0, tzinfo=M.UTC))
        self.assertEqual(M.parse_bound("30м", now), datetime.datetime(2026, 11, 5, 11, 30, tzinfo=M.UTC))
        self.assertEqual(M.parse_bound("2026-11-05 10:15+03:00"), datetime.datetime(2026, 11, 5, 7, 15, tzinfo=M.UTC))
        self.assertEqual(M.parse_bound("2026-11-05"), self.local(2026, 11, 5))
        self.assertEqual(M.parse_bound("2026-11-05T10:15:30"), self.local(2026, 11, 5, 10, 15, 30))
        for bad in ("вчера", "2026-13-01", "25:00", ""):
            with self.assertRaises(Exception):
                M.parse_bound(bad)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rxops-logs-")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def write(self, rel, text, gz=False):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = text.encode("utf-8")
        with (gzip.open(path, "wb") if gz else open(path, "wb")) as f:
            f.write(data)
        return path

    def test_exit_codes_and_json(self):
        clean = self.write("ok.log", '{"t":"2026-10-22 10:00:00.000+03:00","l":"Info","mt":"Started"}\n')
        code, out, _ = run(clean)
        self.assertEqual(code, 0, out)
        self.assertIn("не найдено", out)
        bad = self.write("bad.log", "2026-10-22 10:00:00.000 UTC [1] FATAL:  sorry, too many clients already\n")
        code, out, _ = run(bad, "--json", "--details")
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertEqual(data["found"][0]["slug"], "postgresql-too-many-clients")
        self.assertTrue(data["found"][0]["fix"])
        self.assertEqual(data["found"][0]["sample"]["line"], 1)
        self.assertEqual(run(os.path.join(self.dir, "nope.log"))[0], 2)
        self.assertEqual(run(clean, "--base", os.path.join(self.dir, "none.json"))[0], 2)
        self.assertEqual(run(clean, "--since", "вчера")[0], 2)
        code, out, _ = run("--help")
        self.assertEqual(code, 0)
        self.assertIn("Коды возврата", out)

    def test_dir_gzip_window_and_masking(self):
        lines = [
            "2026-10-22 09:00:00.000+03:00 ERROR circuit_breaking_exception: [parent] Data too large",
            "2026-10-22 10:30:00.000+03:00 ERROR Npgsql.PostgresException: 53300: sorry, too many clients already",
            "   at Npgsql.NpgsqlConnector.Open() Password=Sup3rS3cret; Host=db",
            "2026-10-22 11:30:00.000+03:00 ERROR x509: certificate signed by unknown authority",
        ]
        self.write("rx/srv01.WebServer.2026-10-22.log", "\n".join(lines) + "\n")
        self.write("rx/old/srv01.Worker.2026-10-22.log.gz",
                   "2026-10-22 10:45:00.000+03:00 ERROR Elasticsearch cluster health status changed from [YELLOW] to [RED]\n", gz=True)
        self.write("rx/status/srv01.WebServer.status", '{"Status": "unhealthy", "INDEXING_SERVICE": "degraded"}\n')
        with open(os.path.join(self.dir, "rx", "blob.bin"), "wb") as f:
            f.write(b"\x00\x01\x02 OOMKilled")
        code, out, _ = run(os.path.join(self.dir, "rx"), "--since", "2026-10-22 10:00+03:00",
                           "--until", "2026-10-22 11:00+03:00", "--json")
        self.assertEqual(code, 1)
        data = json.loads(out)
        slugs = {f["slug"]: f for f in data["found"]}
        self.assertEqual(set(slugs), {"postgresql-too-many-clients", "elasticsearch-cluster-health-red"})
        pg = slugs["postgresql-too-many-clients"]
        self.assertEqual(pg["count"], 1)
        self.assertEqual(pg["first_raw"], "2026-10-22 10:30:00.000+03:00")
        reasons = " ".join(s["reason"] for s in data["scanned"]["skipped"])
        self.assertIn("двоичный", reasons)
        self.assertNotIn("status", json.dumps(pg["files"]))
        # строка стека наследует время первой строки записи; пароль в примере скрыт
        stack = self.write("stack.log",
                           "2026-10-22 10:30:00.000+03:00 ERROR Host=db;Password=Sup3rS3cret; too many clients already\n"
                           "   at Npgsql.NpgsqlConnector.Open(): 53300: sorry, too many clients already\n")
        code, out, _ = run(stack, "--since", "2026-10-22 10:00+03:00", "--until", "2026-10-22 11:00+03:00", "--json")
        data = json.loads(out)
        self.assertEqual(data["found"][0]["count"], 2)
        self.assertNotIn("Sup3rS3cret", out)
        self.assertIn("Password=‹скрыто›", data["found"][0]["sample"]["text"])

    def test_stdin_and_since_skips_old_files(self):
        code, out, _ = run("-", stdin="Oct 22 10:15:03 host kernel: dotnet invoked oom-killer: gfp_mask=0x100cca\n".encode())
        self.assertEqual(code, 1)
        self.assertIn("linux-out-of-memory-killed-process", out)
        old = self.write("srv.Worker.2026-01-01.log", "2026-01-01 10:00:00 ERROR OOMKilled\n")
        os.utime(old, (1767225600, 1767225600))
        code, out, _ = run(old, "--since", "2026-10-01")
        self.assertEqual(code, 0, out)
        self.assertIn("вне окна", out)


@unittest.skipUnless(os.path.isdir(os.path.join(os.path.dirname(ROOT), "landing", "errors", "posts")),
                     "нет исходников сайта рядом с репозиторием")
class SyncTest(unittest.TestCase):
    def test_base_is_fresh(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import sync_errors
        fresh = sync_errors.build(os.path.join(os.path.dirname(ROOT), "landing"))
        with io.open(BASE_FILE, encoding="utf-8") as f:
            self.assertEqual(f.read(), fresh, "выполните python3 tools/sync_errors.py")


class SyncUnitTest(unittest.TestCase):
    def test_plain(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import sync_errors as S
        md = ("1. См. [разбор](rx-keep-alive-failed.html) и [статью](../blog/rx-logs-vector.html).\n"
              "**Важно.** Ссылка [вовне](https://example.org/x).\n```\ndf -h\n```\n")
        text = S.plain(md)
        self.assertIn("разбор (https://drxinfra.ru/errors/rx-keep-alive-failed.html)", text)
        self.assertIn("статью (https://drxinfra.ru/blog/rx-logs-vector.html)", text)
        self.assertIn("вовне (https://example.org/x)", text)
        self.assertIn("Важно.", text)
        self.assertIn("    df -h", text)
        self.assertNotIn("```", text)


if __name__ == "__main__":
    unittest.main()
