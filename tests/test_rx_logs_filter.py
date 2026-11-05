"""Проверки log_filter.py: python3 -m unittest discover -s tests"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "skills", "rx-logs", "scripts", "log_filter.py")


def rec(t, l, mt, **kw):
    d = {"t": "2026-10-22 %s.000+03:00" % t, "l": l, "lg": kw.pop("lg", "Sungero.Test"), "mt": mt}
    d.update(kw)
    return json.dumps(d, ensure_ascii=False)


LOG = "\n".join([
    rec("10:00:01", "Info", "Document {Id} loaded", args={"Id": 18234}, un="DOMAIN\\ivanova", tr="cl-4e326a73-7815ae"),
    rec("10:00:02", "Error", "Document {Id} not found", args={"Id": 18234}, un="DOMAIN\\ivanova", tr="cl-4e326a73-7815ae",
        ex={"type": "Sungero.Domain.Shared.Exceptions.RecordNotFoundException", "m": "Not found",
            "stack": "   at A.B()\n   at C.D()"}),
    rec("10:05:00", "Error", "Document {Id} not found", args={"Id": 90211}, un="DOMAIN\\petrov", tr="ws-11111111-222222",
        ex={"type": "Sungero.Domain.Shared.Exceptions.RecordNotFoundException", "m": "Not found"}),
    rec("10:06:00", "Info", "", lg="TraceSpan", span={"name": "Conversion", "durationMs": 4276, "status": "Ok"}),
    rec("10:07:00", "Warn", "Connection string Host=db;Password=Sup3rS3cret;", un="DOMAIN\\petrov"),
    "2026-10-22 10:08:00.000 UTC [77] ERROR:  canceling statement due to statement timeout",
    "2026-10-22 10:08:00.000 UTC [77] STATEMENT:  select 1",
]) + "\n"


class FilterTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rxops-logs-")
        self.path = os.path.join(self.dir, "srv01.WebServer.2026-10-22.log")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(LOG)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def run_filter(self, *args):
        p = subprocess.run([sys.executable, SCRIPT, self.path] + list(args),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return p.returncode, p.stdout.decode("utf-8")

    def test_level_json_and_text(self):
        code, out = self.run_filter("--level", "error,fatal")
        self.assertEqual(code, 0)
        self.assertIn("Записей под фильтры: 3", out)
        self.assertIn("canceling statement", out)
        self.assertNotIn("STATEMENT:  select 1", out)
        code, out = self.run_filter("--level", "warning")
        self.assertIn("Записей под фильтры: 1", out, "Warn и Warning одно и то же")

    def test_user_trace_slow_and_stack(self):
        code, out = self.run_filter("--user", "ivanova", "--level", "Error", "--stack")
        self.assertIn("Document 18234 not found", out)
        self.assertIn("    at C.D()", out)
        self.assertIn("Записей под фильтры: 1", out)
        code, out = self.run_filter("--trace", "cl-4e326a73")
        self.assertIn("Записей под фильтры: 2", out)
        code, out = self.run_filter("--slow", "3000")
        self.assertIn("Conversion", out)
        self.assertIn("4276 мс", out)
        self.assertIn("Записей под фильтры: 1", out)

    def test_window_and_mask(self):
        code, out = self.run_filter("--since", "2026-10-22 10:04+03:00", "--until", "2026-10-22 10:07:30+03:00")
        self.assertIn("Записей под фильтры: 3", out)
        self.assertNotIn("Sup3rS3cret", out)
        self.assertIn("‹скрыто›", out)

    def test_top_groups_by_shape(self):
        code, out = self.run_filter("--top", "3", "--level", "Error")
        self.assertEqual(code, 0)
        self.assertIn("×2 с 2026-10-22 10:00:02.000+03:00 по 2026-10-22 10:05:00.000+03:00", out)
        self.assertIn("Document {Id} not found", out)

    def test_nothing_found_and_usage(self):
        code, out = self.run_filter("--user", "nobody")
        self.assertEqual(code, 1)
        code, out = self.run_filter("--grep", "([")
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
