"""Подсчёт самооценки (skills/rx-security-check/scripts/score.py) против оригинала на сайте.

Эталон снят 05.11.2026 с assets/seccheck.js и security-check.html сайта drxinfra.ru: исходный файл
выполнялся в node без изменений, с минимальной заглушкой DOM, ответы подавались через адрес ?a=...,
итог читался из разметки и из текста кнопки «Скопировать итог». Числа ниже вставлены из того прогона,
сам тест node не требует. Запуск: python3 -m unittest discover -s tests
"""
import datetime
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(ROOT, "skills", "rx-security-check")
SCRIPT = os.path.join(SKILL, "scripts", "score.py")


def load():
    spec = importlib.util.spec_from_file_location("rx_security_score", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sys.dont_write_bytecode = True
score = load()

# sha256 от json.dumps([[id, sev, вес, текст, что сделать, ссылка, подпись], ...]) по разметке страницы.
SITE_QUESTIONS_SHA256 = "2053239c50b86e135c39c22eca8ac02cbe477dbadf25cd61d9346d76ed54a243"

# (ответы, балл, зона, отвечено, (да, частично, нет, не знаю), открытых критичных, дыры по порядку,
#  строка ответов в адресе после ?a=)
GOLDEN = [
    ('yyyyyyyyyyyyyyyyyyyyyy', 100, 'green', 22, (22, 0, 0, 0), 0, '', 'yyyyyyyyyyyyyyyyyyyyyy'),
    ('nnnnnnnnnnnnnnnnnnnnnn', 0, 'red', 22, (0, 0, 22, 0), 5, 'q1n q2n q3n q4n q5n q6n q7n q8n q9n q10n q11n q12n q13n q14n q15n q16n q17n q18n q19n q20n q21n q22n', 'nnnnnnnnnnnnnnnnnnnnnn'),
    ('uuuuuuuuuuuuuuuuuuuuuu', 0, 'red', 22, (0, 0, 0, 22), 5, 'q1u q2u q3u q4u q5u q6u q7u q8u q9u q10u q11u q12u q13u q14u q15u q16u q17u q18u q19u q20u q21u q22u', 'uuuuuuuuuuuuuuuuuuuuuu'),
    ('pppppppppppppppppppppp', 50, 'yellow', 22, (0, 22, 0, 0), 0, 'q1p q2p q3p q4p q5p q6p q7p q8p q9p q10p q11p q12p q13p q14p q15p q16p q17p q18p q19p q20p q21p q22p', 'pppppppppppppppppppppp'),
    ('', None, '', 0, (0, 0, 0, 0), 0, '', ''),
    ('pyyyynnnnnnnnuuuupppy-', 42, 'red', 21, (5, 4, 8, 4), 0, 'q1p q6n q7n q8n q9n q10n q11n q12n q13n q14u q15u q16u q17u q18p q19p q20p', 'pyyyynnnnnnnnuuuupppy'),
    ('yyyyuyyyyyyyyyyyyyyyyy', 92, 'red', 22, (21, 0, 0, 1), 1, 'q5u', 'yyyyuyyyyyyyyyyyyyyyyy'),
    ('pyyyyyyyyyyyyyyyyyyyyy', 96, 'green', 22, (21, 1, 0, 0), 0, 'q1p', 'pyyyyyyyyyyyyyyyyyyyyy'),
    ('yyyyy', 100, 'green', 5, (5, 0, 0, 0), 0, '', 'yyyyy'),
    ('yyypp', 80, 'green', 5, (3, 2, 0, 0), 0, 'q4p q5p', 'yyypp'),
    ('-----n-------------yn-', 13, 'red', 3, (1, 0, 2, 0), 0, 'q6n q21n', '-----n-------------yn'),
    ('-----p-------------yp-', 56, 'yellow', 3, (1, 2, 0, 0), 0, 'q6p q21p', '-----p-------------yp'),
    ('yyyyyuuuuuuuuyyyyyyyyy', 60, 'yellow', 22, (14, 0, 0, 8), 0, 'q6u q7u q8u q9u q10u q11u q12u q13u', 'yyyyyuuuuuuuuyyyyyyyyy'),
    ('yyyyyyyyyyyyynnnnnnnnn', 82, 'green', 22, (13, 0, 9, 0), 0, 'q14n q15n q16n q17n q18n q19n q20n q21n q22n', 'yyyyyyyyyyyyynnnnnnnnn'),
    ('pp-yu-punpu-nppynnnyuy', 35, 'red', 19, (4, 6, 5, 4), 1, 'q5u q1p q2p q8u q9n q11u q13n q7p q10p q17n q18n q19n q14p q15p q21u', 'pp-yu-punpu-nppynnnyuy'),
    ('u-pup-uyun-uy--uyuuu-n', 28, 'red', 16, (3, 2, 2, 9), 2, 'q1u q4u q3p q5p q7u q9u q10n q12u q16u q18u q19u q20u q22n', 'u-pup-uyun-uy--uyuuu-n'),
    ('-ppypy--yp-yunuypu-n-n', 58, 'yellow', 16, (5, 5, 3, 3), 0, 'q2p q3p q5p q13u q10p q14n q15u q18u q17p q20n q22n', '-ppypy--yp-yunuypu-n-n'),
    ('n-nuny--pyyuyppy-nunpu', 36, 'red', 18, (5, 4, 5, 4), 4, 'q1n q3n q4u q5n q12u q9p q18n q19u q14p q15p q20n q22u q21p', 'n-nuny--pyyuyppy-nunpu'),
    ('nyypnyyuynuu-nypuu-yyp', 45, 'red', 20, (8, 3, 4, 5), 2, 'q1n q5n q4p q8u q10n q11u q12u q14n q17u q18u q16p q22p', 'nyypnyyuynuu-nypuu-yyp'),
    ('--npun-nunnuuupnnyyy-p', 15, 'red', 18, (3, 3, 7, 5), 2, 'q3n q5u q4p q6n q8n q9u q10n q11n q12u q13u q14u q16n q17n q15p q22p', '--npun-nunnuuupnnyyy-p'),
    ('u---n-u--unyp--n-nnpyn', 17, 'red', 13, (2, 2, 6, 3), 2, 'q1u q5n q7u q10u q11n q13p q16n q18n q19n q22n q20p', 'u---n-u--unyp--n-nnpyn'),
    ('-pn-pyy-uppnyuy-uppnnn', 44, 'red', 18, (4, 6, 5, 3), 1, 'q3n q2p q5p q9u q12n q10p q11p q14u q17u q18p q19p q20n q21n q22n', '-pn-pyy-uppnyuy-uppnnn'),
    ('nnunnuuy-npn--n-upu-y-', 12, 'red', 16, (2, 2, 7, 5), 5, 'q1n q2n q3u q4n q5n q6u q7u q10n q12n q11p q15n q17u q19u q18p', 'nnunnuuy-npn--n-upu-y'),
    ('u-pnp-nuuunuupn-nnpnp-', 14, 'red', 18, (0, 5, 7, 6), 2, 'q1u q4n q3p q5p q7n q8u q9u q10u q11n q12u q13u q15n q17n q18n q14p q19p q20n q21p', 'u-pnp-nuuunuupn-nnpnp'),
]

# Текст итога для 'pyyyynnnnnnnnuuuupppy-' между строкой «Дата» и строкой «Ответы».
GOLDEN_TEXT = [
    'Итог: 42 из 100, красная зона: начинать сейчас. Отвечено 21 из 22.',
    '',
    'Критично:',
    '- [Частично] Бэкап базы и хранилища документов делается автоматически и лежит на другом сервере',
    '  Что сделать: Настройте ежедневный бэкап базы и файлов хранилища по расписанию с копией на отдельное хранилище и уведомлением, если бэкап не сделался.',
    'Высокий:',
    '- [Нет] Веб-клиент работает только по HTTPS, TLS 1.0 и 1.1 выключены',
    '  Что сделать: Включите на прокси только TLS 1.2 и 1.3 и поставьте уведомление за 30 дней до окончания сертификата.',
    '- [Нет] У каждого администратора своя учётная запись',
    '  Что сделать: Заведите именные учётки на серверах, в СУБД и в RX, общие пароли смените после последнего ухода.',
    '- [Нет] На серверы заходят по ключу или через одну точку входа',
    '  Что сделать: Закройте вход по паролю, оставьте ключи и доступ через VPN или промежуточный сервер.',
    '- [Нет] Пароли и ключи хранятся в менеджере паролей или хранилище секретов',
    '  Что сделать: Перенесите пароли сервисов в хранилище, а те, что лежали открыто, смените.',
    '- [Нет] Учётка RX в базе данных не суперпользователь',
    '  Что сделать: Создайте для RX владельца базы без прав суперпользователя, а для мониторинга отдельную учётку только на чтение.',
    '- [Нет] Действия администраторов на серверах записываются в журнал, и он хранится вне сервера',
    '  Что сделать: Включите auditd или его аналог и отправляйте журналы в отдельное хранилище.',
    '- [Нет] Об аварии вы узнаёте из уведомления, а не от пользователей',
    '  Что сделать: Настройте мониторинг с уведомлениями в почту или мессенджер, начиная с диска, бэкапов и сертификатов.',
    '- [Нет] Версии ОС, СУБД, брокера и поиска ещё поддерживаются производителем',
    '  Что сделать: Составьте список версий с датами окончания поддержки и запланируйте переход до этих дат.',
    'Средний:',
    '- [Не знаю] Есть тестовый контур, обновления сначала проходят через него',
    '  Что сделать: Поднимите тестовый контур из бэкапа прода и проводите через него каждое обновление.',
    '- [Не знаю] Изменения настроек идут по описанной процедуре, а не руками на проде',
    '  Что сделать: Храните настройки серверов в репозитории и меняйте их оттуда. Минимум: журнал изменений с датой и автором.',
    '- [Не знаю] Пакеты и образы ставятся из своего зеркала или проверенного реестра с точными версиями',
    '  Что сделать: Заведите своё зеркало пакетов и реестр образов, фиксируйте версии и проверяйте контрольные суммы.',
    '- [Не знаю] На веб-клиенте включены защитные заголовки',
    '  Что сделать: Добавьте заголовки на прокси перед RX.',
    '- [Частично] Журналы RX собираются в одно место и хранятся не меньше 30 дней',
    '  Что сделать: Соберите журналы сервисов в Elasticsearch или аналог со сроком хранения.',
    '- [Частично] На самих серверах включён сетевой экран',
    '  Что сделать: Включите экран на серверах и разрешите только нужные порты с нужных адресов.',
    'Низкий:',
    '- [Частично] Есть актуальная схема: серверы, порты, учётки сервисов, кто за что отвечает',
    '  Что сделать: Опишите схему на одной странице и обновляйте её при каждом изменении.',
    '',
]


class GoldenTest(unittest.TestCase):
    def test_questions_match_site(self):
        qs = [[q[0], q[1], score.WEIGHT[q[1]], q[2], q[3], q[4], q[5]] for q in score.QUESTIONS]
        h = hashlib.sha256(json.dumps(qs, ensure_ascii=False).encode("utf-8")).hexdigest()
        self.assertEqual(h, SITE_QUESTIONS_SHA256, "вопросы разошлись со страницей security-check.html")
        self.assertEqual(len(score.QUESTIONS), 22)
        self.assertEqual(sum(score.WEIGHT[q[1]] for q in score.QUESTIONS), 119)

    def test_against_original_js(self):
        for ans, sc, zone, done, cnt, crit, gaps, tail in GOLDEN:
            with self.subTest(answers=ans):
                a = score.from_string(ans)
                r = score.calc(a)
                self.assertEqual(r["score"], sc)
                self.assertEqual(r["zone"], zone)
                self.assertEqual(r["done"], done)
                self.assertEqual(tuple(r["cnt"][k] for k in "ypnu"), cnt)
                self.assertEqual(r["crit_open"], crit)
                self.assertEqual(" ".join(q[0] + x for q, x in r["gaps"]), gaps)
                self.assertEqual(score.answer_string(a), tail)

    def test_text_like_copy_button(self):
        a = score.from_string("pyyyynnnnnnnnuuuupppy-")
        lines = score.to_text(a, score.calc(a), datetime.date(2026, 11, 5)).split("\n")
        self.assertEqual(lines[1], "Дата: 05.11.2026")
        end = [i for i, l in enumerate(lines) if l.startswith("Ответы:")][0]
        body = [l for l in lines[2:end] if not l.startswith("  Подробнее:")]
        self.assertEqual(body, GOLDEN_TEXT)
        self.assertEqual(lines[end], "Ответы: https://drxinfra.ru/security-check.html?a=pyyyynnnnnnnnuuuupppy")
        self.assertIn("Без ответа: q22. Балл посчитан только по отвеченным вопросам.", lines)

    def test_js_round_half_up(self):
        self.assertEqual(score.js_round(12.5), 13)
        self.assertEqual(score.js_round(37.5), 38)
        self.assertEqual(score.js_round(49.4), 49)
        self.assertEqual(score.js_round(79.5), 80)
        self.assertEqual(score.calc(score.from_string("-----n-------------yn"))["score"], 13)


class InputTest(unittest.TestCase):
    def test_words_and_url(self):
        a = score.from_pairs(["q1=да", "2=частично", "q3=Не знаю", "q4=нет", "q5=-"])
        self.assertEqual(a, {"q1": "y", "q2": "p", "q3": "u", "q4": "n"})
        self.assertEqual(score.from_string("https://drxinfra.ru/security-check.html?a=yn-p#calc"),
                         {"q1": "y", "q2": "n", "q4": "p"})
        self.assertEqual(score.from_json({"q1": "yes", "q22": "unknown", "q5": None}), {"q1": "y", "q22": "u"})
        self.assertEqual(score.from_json(["да", "нет", "", "частично"]), {"q1": "y", "q2": "n", "q4": "p"})
        self.assertEqual(score.from_json({"answers": "yy"}), {"q1": "y", "q2": "y"})

    def test_bad_input(self):
        for bad in ("yyz", "y" * 23):
            with self.assertRaises(score.InputError):
                score.from_string(bad)
        with self.assertRaises(score.InputError):
            score.from_pairs(["q23=да"])
        with self.assertRaises(score.InputError):
            score.from_pairs(["q1=может быть"])

    def run_cli(self, *args, stdin=None, env=None):
        # аргументы байтами: в Python 3.6 при LANG=C строку с кириллицей не передать
        cmd = [os.fsencode(sys.executable), b"-B", os.fsencode(SCRIPT)] + [a.encode("utf-8") for a in args]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, input=stdin, env=env)
        return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8")

    def test_c_locale(self):
        # Python 3.6 при LANG=C: ASCII в stdout и суррогаты в argv; скрипт пишет байтами UTF-8
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C", "LC_ALL": "C"}
        rc, out, err = self.run_cli("q1=да", "q3=не знаю", env=env)
        self.assertEqual(rc, 1, err)
        self.assertIn("- [Не знаю] Пароли по умолчанию заменены везде", out)
        rc, out, _ = self.run_cli("--file", "-", stdin='{"q1": "частично"}'.encode("utf-8"), env=env)
        self.assertIn("[Частично]", out)

    def test_cli_exit_codes(self):
        self.assertEqual(self.run_cli("--answers", "y" * 22)[0], 0)
        self.assertEqual(self.run_cli("--answers", "p" * 22)[0], 1)
        self.assertEqual(self.run_cli("--answers", "yyyyu")[0], 1)
        self.assertEqual(self.run_cli()[0], 2)
        self.assertEqual(self.run_cli("--answers", "xyz")[0], 2)
        rc, out, _ = self.run_cli("--help")
        self.assertEqual(rc, 0)
        self.assertIn("Код возврата", out)
        rc, out, _ = self.run_cli("--list")
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.strip().splitlines()), 22)

    def test_cli_json_and_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"q1": "да", "q2": "нет", "q6": "частично"}, f, ensure_ascii=False)
            rc, out, _ = self.run_cli("--file", p, "--json")
        self.assertEqual(rc, 1)
        doc = json.loads(out)
        for k in ("tool", "version", "host", "time", "checks", "summary", "score", "zone", "gaps"):
            self.assertIn(k, doc)
        self.assertEqual(doc["zone"], "red")
        self.assertEqual(doc["crit_open"], 1)
        self.assertEqual([g["id"] for g in doc["gaps"]], ["q2", "q6"])
        st = {c["id"]: c["status"] for c in doc["checks"]}
        self.assertEqual((st["q1"], st["q2"], st["q6"], st["q7"]), ("OK", "CRIT", "WARN", "SKIP"))
        self.assertEqual(doc["url"], "https://drxinfra.ru/security-check.html?a=yn---p")


class ReferenceTest(unittest.TestCase):
    def test_questionnaire_has_all_questions(self):
        with open(os.path.join(SKILL, "references", "questionnaire.md"), encoding="utf-8") as f:
            text = f.read()
        heads = re.findall(r"^### (q\d+)\. (.+)$", text, re.M)
        self.assertEqual([h[0] for h in heads], score.QIDS)
        for (qid, title), q in zip(heads, score.QUESTIONS):
            self.assertEqual(title.strip(), q[2], qid)
        for check in sorted(set(c for q in score.QUESTIONS for c in q[6])):
            self.assertIn("`%s`" % check, text, check)


if __name__ == "__main__":
    unittest.main()
