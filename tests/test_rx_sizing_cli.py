"""Командная строка skills/rx-sizing/scripts/sizing.py: справка, коды возврата, JSON, ссылки с сайта.

Без сети и без node: скрипт запускается тем же интерпретатором, что и тесты.
Запуск: python3 -m unittest discover -s tests -p 'test_rx_sizing_*.py'
"""
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "skills", "rx-sizing", "scripts", "sizing.py")


def run(*args, **env):
    e = dict(os.environ)
    e.update(env)
    # Аргументы байтами UTF-8: в Python 3.6 с локалью C строку с кириллицей subprocess не закодирует.
    cmd = [sys.executable, SCRIPT] + [a.encode("utf-8") for a in args]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e)
    return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8")


def js(*args):
    code, out, err = run(*(list(args) + ["--json"]))
    assert code == 0, err
    return json.loads(out)


class CliTest(unittest.TestCase):
    def test_help(self):
        for args in ([], ["servers"], ["storage"], ["postgres"]):
            with self.subTest(args):
                code, out, _ = run(*(args + ["--help"]))
                self.assertEqual(code, 0)
                self.assertIn("sizing.py", out)

    def test_bad_input_exit_2(self):
        cases = [[], ["servers", "--users", "-5"], ["servers", "--docs-year", "много"], ["servers", "--os", "solaris"],
                 ["servers", "--from-url", "https://drxinfra.ru/sizing.html#store=nfs"], ["storage", "--pdf", "abc"],
                 ["storage", "--years", "-1"], ["postgres", "--disk", "nvme"], ["postgres", "--ram", "0"],
                 ["servers", "--rx-version", "3.0"], ["servers", "--users", "20000000"]]
        for args in cases:
            with self.subTest(args):
                code, out, err = run(*args)
                self.assertEqual(code, 2, out + err)

    def test_json_contract(self):
        for sub in ("servers", "storage", "postgres"):
            with self.subTest(sub):
                d = js(sub)
                self.assertEqual(d["tool"], "sizing.py")
                self.assertEqual(d["version"], "0.1.0")
                self.assertEqual(d["calculator"], sub)
                self.assertTrue(d["source"].startswith("https://drxinfra.ru/"))

    def test_servers_defaults(self):
        d = js("servers")
        prod = d["contours"][0]
        self.assertEqual(prod["totals"], {"vms": 5, "cpu": 22, "ram": 52, "disk": 780})
        self.assertEqual(prod["backupGB"], 450)
        self.assertEqual(d["grand"]["vms"], 9)
        self.assertEqual(d["derived"]["U"], 90)
        self.assertEqual(d["inputs"]["users"], 300)

    def test_url_equals_flags(self):
        url = "https://drxinfra.ru/sizing.html#users=500&docsYear=100000&ha=1&store=fs&logDays=30&test=0"
        a = js("servers", "--from-url", url)
        b = js("servers", "--users", "500", "--docs-year", "100000", "--ha", "--store", "fs", "--log-days", "30", "--no-test")
        self.assertEqual(a["contours"], b["contours"])
        self.assertEqual(a["warnings"], b["warnings"])

    def test_flags_override_preset_and_url(self):
        d = js("servers", "--preset", "small", "--fts")
        self.assertEqual((d["inputs"]["users"], d["inputs"]["store"], d["inputs"]["fts"]), (100, "fs", 1))
        d = js("servers", "--from-url", "#users=800&ha=1", "--no-ha", "--active", "100")
        self.assertEqual((d["inputs"]["users"], d["inputs"]["ha"], d["derived"]["U"]), (800, 0, 100))

    def test_storage_comma_and_url(self):
        a = js("storage", "--size-mb", "1,2", "--versions", "1.5")
        b = js("storage")
        self.assertEqual(a["result"], b["result"])
        self.assertAlmostEqual(b["result"]["data"], 1272.3409968750002, places=6)
        c = js("storage", "--from-url", "https://drxinfra.ru/storage-growth.html?docs=1000&years=3", "--copies", "3")
        self.assertEqual((c["inputs"]["docs"], c["inputs"]["years"], c["inputs"]["copies"]), (1000, 3, 3))
        self.assertEqual(len(c["result"]["years"]), 3)

    def test_postgres_settings(self):
        d = js("postgres")
        s = d["settings"]
        self.assertEqual((s["shared_buffers"], s["effective_cache_size"], s["work_mem"], s["max_connections"]),
                         ("4GB", "11GB", "32MB", "250"))
        self.assertEqual(s["max_prepared_transactions"], s["max_connections"])
        self.assertIn("shared_buffers", d["restart_required"])
        big = js("postgres", "--ram", "64", "--cpu", "16", "--users", "900", "--db-gb", "600")
        self.assertEqual(big["settings"]["huge_pages"], "try")
        self.assertIn("vm.nr_hugepages", big["settings"])

    def test_site_url_round_trip(self):
        """Скрипт печатает ссылку на тот же расчёт на сайте, и по ней считается то же самое."""
        a = js("servers", "--users", "500", "--ha", "--store", "fs")
        self.assertEqual(a["url"], "https://drxinfra.ru/sizing.html#users=500&ha=1&store=fs")
        self.assertEqual(js("servers", "--from-url", a["url"])["contours"], a["contours"])
        self.assertEqual(js("servers")["url"], "https://drxinfra.ru/sizing.html")
        s = js("storage", "--docs", "1000", "--size-mb", "0,5")
        self.assertEqual(s["url"], "https://drxinfra.ru/storage-growth.html?docs=1000&size=0.5")
        self.assertEqual(js("storage", "--from-url", s["url"])["result"], s["result"])
        p = js("postgres", "--ram", "32", "--docker")
        self.assertEqual(p["url"], "https://drxinfra.ru/postgresql-config.html?ram=32&docker=1")
        self.assertEqual(js("postgres", "--from-url", p["url"])["result"], p["result"])

    def test_postgres_container(self):
        d = js("postgres", "--ram", "32", "--docker")
        self.assertEqual(d["container"], {"shm_size": "1g", "min_memory_limit_gb": 16})
        self.assertNotIn("huge_pages", d["settings"])
        self.assertIsNone(js("postgres")["container"])

    def test_tables(self):
        code, out, _ = run("servers")
        self.assertEqual(code, 0)
        self.assertIn("Продуктивный контур: 5 ВМ, 22 vCPU, 52 ГБ ОЗУ, диски 780 ГБ", out)
        self.assertIn("Резервные копии вне контура: 450 ГБ", out)
        code, out, _ = run("servers", "--details", "--preset", "holding")
        self.assertIn("Квота неймспейса", out)
        self.assertIn("Параметры расчёта:", out)
        code, out, _ = run("storage")
        self.assertIn("Том под хранилище", out)
        self.assertIn("1,7 ТБ", out)
        code, out, _ = run("postgres", "--place", "shared", "--ram", "8")
        self.assertIn("shared_buffers", out)
        self.assertIn("важно:", out)

    def test_ascii_locale(self):
        """С локалью C вывод не падает на кириллице (актуально для Python 3.6)."""
        code, out, err = run("servers", LANG="C", LC_ALL="C", PYTHONIOENCODING="ascii")
        self.assertEqual(code, 0, err)
        self.assertIn("Продуктивный контур", out)


if __name__ == "__main__":
    unittest.main()
