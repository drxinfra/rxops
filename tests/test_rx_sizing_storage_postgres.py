"""Golden-тесты роста хранилища и памяти PostgreSQL: подкоманды storage и postgres
скрипта skills/rx-sizing/scripts/sizing.py.

Как получен эталон. Оригинальные assets/storage.js (https://drxinfra.ru/storage-growth.html)
и assets/pgconf.js (https://drxinfra.ru/postgresql-config.html) выполнялись в node через vm
с минимальной заглушкой DOM: значения ставились в поля формы, затем вызывались их же read(),
calc() и render(). Формулы не менялись, наружу выставлялись только ссылки на функции.
Сюда перенесены приведённые входы, все числа calc() и по годам, буквы уровней замечаний
(w: важно, i: к сведению), а тексты замечаний и итоговый абзац сведены в sha256
(16 знаков). Дробные числа сверяются с относительной точностью 1e-9: Math.pow в node
и math.pow в Python могут расходиться в последнем бите. Node для тестов не нужен.
Кроме этих сценариев порт при написании сверялся с node на 500 случайных наборах входов
каждого калькулятора, включая строки, под Python 3.15 и 3.6: расхождений не было.

Сценарии хранилища: по умолчанию, один год без роста, PDF и копий, большой поток на 10 лет,
значения за пределами полей (доля PDF 150 %, запас 95 %, 400 дней, 0,4 года, избыточность 0,5,
2,6 копии), маленький поток с запасом 10 %, сканы на 30 лет, ноль документов и запятая в числе.
Сценарии PostgreSQL: по умолчанию, база вместе с RX на HDD, 256 ГБ (упор в пределы),
контейнер, минимальные значения, дробная память, 64 ГБ с huge pages, нули как «по умолчанию».

Запуск: python3 -m unittest discover -s tests -p 'test_rx_sizing_*.py'
"""
import hashlib
import importlib.util
import json
import os
import sys
import unittest

try:
    from urllib.parse import unquote
except ImportError:  # pragma: no cover
    from urllib import unquote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "skills", "rx-sizing", "scripts", "sizing.py")


def load():
    sys.dont_write_bytecode = True  # не оставлять __pycache__ в папке скила
    spec = importlib.util.spec_from_file_location("rx_sizing_script_sp", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


S = load()


def sha(parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def form_values(q):
    """Строка вида docs=10&size=1%2C5 в значения полей формы, как их ставила обвязка."""
    out = {}
    for kv in q.split("&") if q else []:
        k, v = kv.split("=", 1)
        out[k] = unquote(v)
    return out


STORAGE = [
    ('defaults',
     '', {
        "v": {'docs': 400, 'size': 1.2, 'vers': 1.5, 'pdf': 30, 'days': 247, 'years': 5, 'growth': 10,
              'have': 0, 'reserve': 25, 'redund': 1, 'copies': 2},
        "sum": [0.84375, 208.40625, 1272.3409968750002, 1696.4546625000003, 2544.6819937500004,
                1.2353343750000003],
        "cum": [208.40625, 437.65312500000005, 689.8246875000001, 967.2134062500002, 1272.3409968750002],
        "docs": [98800, 108680.00000000001, 119548.00000000001, 131502.80000000005, 144653.08000000005],
        "notes": 'i',
        "strings": '499bea697200e4e0',
    }),
    ('one_year_no_growth_no_pdf_no_copies',
     'years=1&growth=0&pdf=0&copies=0', {
        "v": {'docs': 400, 'size': 1.2, 'vers': 1.5, 'pdf': 0, 'days': 247, 'years': 1, 'growth': 0,
              'have': 0, 'reserve': 25, 'redund': 1, 'copies': 0},
        "sum": [0.7031249999999999, 173.67187499999997, 173.67187499999997, 231.56249999999997, 0,
                0.7031249999999999],
        "cum": [173.67187499999997],
        "docs": [98800],
        "notes": 'wiw',
        "strings": 'f1d8c186ddcbc071',
    }),
    ('large_flow',
     'docs=20000&size=3.5&vers=2&pdf=100&days=250&years=10&growth=25&have=5000&reserve=30&redund=2'
     '&copies=3', {
        "v": {'docs': 20000, 'size': 3.5, 'vers': 2, 'pdf': 100, 'days': 250, 'years': 10, 'growth': 25,
              'have': 5000, 'reserve': 30, 'redund': 2, 'copies': 3},
        "sum": [205.078125, 51269.53125, 1709860.7487231493, 4885316.424923284, 5129582.246169448,
                1527.9510989785194],
        "cum": [56269.53125, 120356.4453125, 200465.087890625, 300600.89111328125, 425770.64514160156,
                582232.837677002, 777810.5783462524, 1022282.7541828156, 1327872.9739785194,
                1709860.7487231493],
        "docs": [5000000, 6250000, 7812500, 9765625, 12207031.25, 15258789.0625, 19073486.328125,
                 23841857.91015625, 29802322.387695312, 37252902.98461914],
        "notes": 'i',
        "strings": '2d9cfb2dc508ca0c',
    }),
    ('clamped_inputs',
     'pdf=150&reserve=95&days=400&years=0.4&redund=0.5&copies=2.6', {
        "v": {'docs': 400, 'size': 1.2, 'vers': 1.5, 'pdf': 100, 'days': 366, 'years': 1, 'growth': 10,
              'have': 0, 'reserve': 90, 'redund': 1, 'copies': 3},
        "sum": [1.171875, 428.90625, 428.90625, 4289.062500000001, 1286.71875, 1.171875],
        "cum": [428.90625],
        "docs": [146400],
        "notes": 'wi',
        "strings": 'fb1ffaeb4f797ca2',
    }),
    ('small_low_reserve',
     'docs=10&size=0.2&vers=1&pdf=10&years=3&growth=5&reserve=10&redund=1.33&copies=1', {
        "v": {'docs': 10, 'size': 0.2, 'vers': 1, 'pdf': 10, 'days': 247, 'years': 3, 'growth': 5,
              'have': 0, 'reserve': 10, 'redund': 1.33, 'copies': 1},
        "sum": [0.0021484375, 0.5306640625000001, 1.6729184570312503, 2.47220171983507, 1.6729184570312503,
                0.0023686523437500004],
        "cum": [0.5306640625000001, 1.0878613281250002, 1.6729184570312503],
        "docs": [2470, 2593.5, 2723.175],
        "notes": 'ww',
        "strings": 'b0ed5940f22e3b9e',
    }),
    ('big_scans_30y',
     'docs=3000&size=6&vers=1.2&pdf=50&days=366&years=30&growth=3&have=120&reserve=20&redund=1.5'
     '&copies=4', {
        "v": {'docs': 3000, 'size': 6, 'vers': 1.2, 'pdf': 50, 'days': 366, 'years': 30, 'growth': 3,
              'have': 120, 'reserve': 20, 'redund': 1.5, 'copies': 4},
        "sum": [29.882812499999996, 10937.109374999998, 520457.5251411369, 975857.8596396316,
                2081830.1005645476, 70.42080516004594],
        "cum": [11057.109374999998, 22322.332031249996, 33925.51136718749, 45876.786083203115,
                58186.599040699206, 70865.70638692018, 83925.1869535278, 97376.45193713364,
                111231.25487024765, 125501.70189135509, 140200.26232309575,
                155339.77956778862, 170933.4823298223, 186994.99617471697,
                203538.35543495847, 220578.01547300722, 238128.86531219745,
                256206.24064656338, 274825.9372409603, 294004.2247331891, 313757.8608501848,
                334104.10605069035, 355060.73860721104, 376646.0701404274,
                398878.96161964023, 421778.83984322945, 445365.71441352635,
                469660.19522093213, 494683.5104525601, 520457.5251411369],
        "docs": [1098000, 1130940, 1164868.2, 1199814.246, 1235808.67338, 1272882.9335814002,
                 1311069.4215888423, 1350401.5042365075, 1390913.5493636027,
                 1432640.9558445108, 1475620.1845198462, 1519888.7900554417,
                 1565485.453757105, 1612450.0173698182, 1660823.5178909127,
                 1710648.22342764, 1761967.6701304696, 1814826.7002343836,
                 1869271.501241415, 1925349.6462786575, 1983110.1356670174,
                 2042603.439737028, 2103881.542929139, 2166997.989217013,
                 2232007.9288935238, 2298968.166760329, 2367937.2117631393,
                 2438975.3281160337, 2512144.587959515, 2587508.9255983],
        "notes": 'ii',
        "strings": 'd24e22ff89faaea5',
    }),
    ('comma_and_zero_docs',
     'docs=0&size=1%2C5&vers=0&years=2', {
        "v": {'docs': 0, 'size': 1.5, 'vers': 0, 'pdf': 30, 'days': 247, 'years': 2, 'growth': 10,
              'have': 0, 'reserve': 25, 'redund': 1, 'copies': 2},
        "sum": [0, 0, 0, 0, 0, 0],
        "cum": [0, 0],
        "docs": [0, 0],
        "notes": 'wi',
        "strings": 'eafb2a86847e1055',
    }),
]

STORAGE_DEFAULT_TEXT = ('При 400 новых документах в рабочий день, среднем файле 1,2\xa0МБ, 1,5 версии на '
                        'документ и PDF-представлении у 30\xa0% документов хранилище растёт на 0,8\xa0ГБ в '
                        'день и на 208\xa0ГБ за первый год. Через 5\xa0лет при росте потока на 10\xa0% в год '
                        'данных будет 1,2\xa0ТБ. Под них нужен том 1,7\xa0ТБ и запаса 25\xa0% свободного места, '
                        'под резервные копии ещё 2,5\xa0ТБ (2\xa0полные копии).')

POSTGRES = [
    ('defaults',
     '', {
        "v": {'ram': 16, 'cpu': 4, 'disk': 'ssd', 'place': 'own', 'docker': 0, 'users': 100, 'dbgb': 50,
              'replica': 0, 'archive': 0},
        "r": {'memDB': 16384, 'sb': 4096, 'ecs': 11264, 'conn': 250, 'wm': 32, 'mwm': 1024, 'maxWal': 8,
              'minWal': 2, 'ckpt': '10min', 'rpc': '1.1', 'eio': 200, 'workers': 8, 'par': 4,
              'gather': 2, 'pmaint': 2, 'avw': 3, 'huge': False, 'hugePages': 2253,
              'peak': 8596},
        "notes": 'i',
        "strings": '17f3910ea8155201',
        "sum": [['Памяти базе', '16 ГБ'], ['shared_buffers', '4 ГБ'], ['Соединений', '250'],
                ['work_mem', '32 МБ'], ['Под нагрузкой', '8,4 ГБ']],
    }),
    ('shared_small_hdd',
     'ram=8&cpu=2&disk=hdd&place=shared&users=300&dbgb=20', {
        "v": {'ram': 8, 'cpu': 2, 'disk': 'hdd', 'place': 'shared', 'docker': 0, 'users': 300, 'dbgb': 20,
              'replica': 0, 'archive': 0},
        "r": {'memDB': 2867.2, 'sb': 704, 'ecs': 2048, 'conn': 550, 'wm': 4, 'mwm': 192, 'maxWal': 4,
              'minWal': 1, 'ckpt': '10min', 'rpc': '4', 'eio': 2, 'workers': 8, 'par': 2,
              'gather': 1, 'pmaint': 1, 'avw': 3, 'huge': False, 'hugePages': 388,
              'peak': 6754},
        "notes": 'wwiii',
        "strings": 'f6ffc5df43ad078c',
        "sum": [['Памяти базе', '2,8 ГБ'], ['shared_buffers', '704 МБ'], ['Соединений', '550'],
                ['work_mem', '4 МБ'], ['Под нагрузкой', '6,6 ГБ']],
    }),
    ('big_net_clamped',
     'ram=256&cpu=32&disk=net&users=2000&dbgb=1500', {
        "v": {'ram': 256, 'cpu': 32, 'disk': 'net', 'place': 'own', 'docker': 0, 'users': 2000,
              'dbgb': 1500, 'replica': 0, 'archive': 0},
        "r": {'memDB': 262144, 'sb': 32768, 'ecs': 183296, 'conn': 1000, 'wm': 64, 'mwm': 2048,
              'maxWal': 16, 'minWal': 4, 'ckpt': '15min', 'rpc': '1.2', 'eio': 100,
              'workers': 32, 'par': 16, 'gather': 2, 'pmaint': 2, 'avw': 4, 'huge': True,
              'hugePages': 18023, 'peak': 58768},
        "notes": 'wiii',
        "strings": '29bc27dc12cf9b78',
        "sum": [['Памяти базе', '256 ГБ'], ['shared_buffers', '32 ГБ'], ['Соединений', '1000'],
                ['work_mem', '64 МБ'], ['Под нагрузкой', '57,4 ГБ']],
    }),
    ('docker_32',
     'ram=32&cpu=8&docker=1&users=500&dbgb=300', {
        "v": {'ram': 32, 'cpu': 8, 'disk': 'ssd', 'place': 'own', 'docker': 1, 'users': 500, 'dbgb': 300,
              'replica': 0, 'archive': 0},
        "r": {'memDB': 32768, 'sb': 8192, 'ecs': 22528, 'conn': 850, 'wm': 16, 'mwm': 2048, 'maxWal': 16,
              'minWal': 4, 'ckpt': '15min', 'rpc': '1.1', 'eio': 200, 'workers': 8, 'par': 8,
              'gather': 2, 'pmaint': 2, 'avw': 4, 'huge': False, 'hugePages': 4506,
              'peak': 20092},
        "notes": 'wiii',
        "strings": '41b2c9aa6be76f3d',
        "sum": [['Памяти базе', '32 ГБ'], ['shared_buffers', '8 ГБ'], ['Соединений', '850'],
                ['work_mem', '16 МБ'], ['Под нагрузкой', '19,6 ГБ']],
    }),
    ('minimal',
     'ram=2&cpu=1&users=1&dbgb=1', {
        "v": {'ram': 2, 'cpu': 1, 'disk': 'ssd', 'place': 'own', 'docker': 0, 'users': 1, 'dbgb': 1,
              'replica': 0, 'archive': 0},
        "r": {'memDB': 2048, 'sb': 512, 'ecs': 1408, 'conn': 150, 'wm': 4, 'mwm': 128, 'maxWal': 4,
              'minWal': 1, 'ckpt': '10min', 'rpc': '1.1', 'eio': 200, 'workers': 8, 'par': 2,
              'gather': 0, 'pmaint': 1, 'avw': 3, 'huge': False, 'hugePages': 282,
              'peak': 2162},
        "notes": 'wi',
        "strings": 'e2bfa6f048d9f731',
        "sum": [['Памяти базе', '2 ГБ'], ['shared_buffers', '512 МБ'], ['Соединений', '150'],
                ['work_mem', '4 МБ'], ['Под нагрузкой', '2,1 ГБ']],
    }),
    ('shared_fractional',
     'ram=6.5&cpu=3&place=shared&users=45&dbgb=120&disk=net', {
        "v": {'ram': 6.5, 'cpu': 3, 'disk': 'net', 'place': 'shared', 'docker': 0, 'users': 45, 'dbgb': 120,
              'replica': 0, 'archive': 0},
        "r": {'memDB': 2329.6, 'sb': 576, 'ecs': 1664, 'conn': 200, 'wm': 4, 'mwm': 128, 'maxWal': 8,
              'minWal': 2, 'ckpt': '10min', 'rpc': '1.2', 'eio': 100, 'workers': 8, 'par': 3,
              'gather': 1, 'pmaint': 1, 'avw': 3, 'huge': False, 'hugePages': 317,
              'peak': 2776},
        "notes": 'wwiii',
        "strings": '9d560cca00c84692',
        "sum": [['Памяти базе', '2,3 ГБ'], ['shared_buffers', '576 МБ'], ['Соединений', '200'],
                ['work_mem', '4 МБ'], ['Под нагрузкой', '2,7 ГБ']],
    }),
    ('mid_64_huge',
     'ram=64&cpu=16&users=900&dbgb=600', {
        "v": {'ram': 64, 'cpu': 16, 'disk': 'ssd', 'place': 'own', 'docker': 0, 'users': 900, 'dbgb': 600,
              'replica': 0, 'archive': 0},
        "r": {'memDB': 65536, 'sb': 16384, 'ecs': 46080, 'conn': 1000, 'wm': 32, 'mwm': 2048, 'maxWal': 16,
              'minWal': 4, 'ckpt': '15min', 'rpc': '1.1', 'eio': 200, 'workers': 16, 'par': 16,
              'gather': 2, 'pmaint': 2, 'avw': 4, 'huge': True, 'hugePages': 9012,
              'peak': 34384},
        "notes": 'wiii',
        "strings": '29bc27dc12cf9b78',
        "sum": [['Памяти базе', '64 ГБ'], ['shared_buffers', '16 ГБ'], ['Соединений', '1000'],
                ['work_mem', '32 МБ'], ['Под нагрузкой', '33,6 ГБ']],
    }),
    ('zero_means_default',
     'ram=0&cpu=0&users=0&dbgb=0', {
        "v": {'ram': 16, 'cpu': 4, 'disk': 'ssd', 'place': 'own', 'docker': 0, 'users': 100, 'dbgb': 50,
              'replica': 0, 'archive': 0},
        "r": {'memDB': 16384, 'sb': 4096, 'ecs': 11264, 'conn': 250, 'wm': 32, 'mwm': 1024, 'maxWal': 8,
              'minWal': 2, 'ckpt': '10min', 'rpc': '1.1', 'eio': 200, 'workers': 8, 'par': 4,
              'gather': 2, 'pmaint': 2, 'avw': 3, 'huge': False, 'hugePages': 2253,
              'peak': 8596},
        "notes": 'i',
        "strings": '17f3910ea8155201',
        "sum": [['Памяти базе', '16 ГБ'], ['shared_buffers', '4 ГБ'], ['Соединений', '250'],
                ['work_mem', '32 МБ'], ['Под нагрузкой', '8,4 ГБ']],
    }),
]


class StorageGoldenTest(unittest.TestCase):
    def close(self, a, b, msg):
        self.assertTrue(abs(a - b) <= 1e-9 * max(1.0, abs(b)), "%s: %r != %r" % (msg, a, b))

    def test_golden(self):
        for name, q, exp in STORAGE:
            with self.subTest(name):
                v = S.storage_read(form_values(q))
                self.assertEqual(v, exp["v"], name)
                r = S.storage_calc(v)
                got = [r["dayGB"], r["firstYear"], r["data"], r["disk"], r["backup"], r["lastDayGB"]]
                for i, (a, b) in enumerate(zip(got, exp["sum"])):
                    self.close(a, b, "%s: итог %d" % (name, i))
                self.assertEqual(len(r["years"]), len(exp["cum"]), name)
                for y, cum, docs in zip(r["years"], exp["cum"], exp["docs"]):
                    self.close(y["cum"], cum, "%s: накоплено за %d-й год" % (name, y["y"]))
                    self.close(y["docs"], docs, "%s: документов за %d-й год" % (name, y["y"]))
                notes = S.storage_notes(v, r)
                self.assertEqual("".join(lvl[0] for lvl, _ in notes), exp["notes"], name)
                self.assertEqual(sha([S.storage_text(v, r)] + [t for _, t in notes]), exp["strings"], name)

    def test_default_text(self):
        v = S.storage_read({})
        self.assertEqual(S.storage_text(v, S.storage_calc(v)), STORAGE_DEFAULT_TEXT)

    def test_url_like_page(self):
        """fromUrl() со страницы: parseFloat берёт начало строки, отрицательное отбрасывается."""
        d = S.decode_storage_url("https://drxinfra.ru/storage-growth.html?docs=1000&size=1,5&growth=-5&x=1#top")
        self.assertEqual(d, {"docs": 1000.0, "size": 1.0})


class PostgresGoldenTest(unittest.TestCase):
    def test_golden(self):
        for name, q, exp in POSTGRES:
            with self.subTest(name):
                raw = dict((k, val if k in ("disk", "place") else float(val)) for k, val in form_values(q).items())
                v = S.pg_read(raw)
                self.assertEqual(v, exp["v"], name)
                r = S.pg_calc(v)
                self.assertEqual(set(r), set(exp["r"]), name)
                for k in exp["r"]:
                    if isinstance(exp["r"][k], float):
                        self.assertAlmostEqual(r[k], exp["r"][k], places=6, msg="%s: %s" % (name, k))
                    else:
                        self.assertEqual(r[k], exp["r"][k], "%s: %s" % (name, k))
                notes = S.pg_notes(v, r)
                self.assertEqual("".join(lvl[0] for lvl, _ in notes), exp["notes"], name)
                self.assertEqual(sha([t for _, t in notes]), exp["strings"], name)
                rows = [["Памяти базе", S.pg_human(r["memDB"])], ["shared_buffers", S.pg_human(r["sb"])],
                        ["Соединений", S.js_str(r["conn"])], ["work_mem", S.pg_human(r["wm"])],
                        ["Под нагрузкой", S.pg_human(r["peak"])]]
                self.assertEqual(rows, exp["sum"], name)

    def test_conf_units(self):
        """Единицы как в postgresql.conf: целые гигабайты через GB, остальное через MB."""
        self.assertEqual([S.pg_mem(x) for x in (4096, 2816, 1024, 704.4)], ["4GB", "2816MB", "1GB", "704MB"])

    def test_url_like_page(self):
        d = S.decode_postgres_url("?ram=32&cpu=abc&disk=nvme&place=shared&docker=1&ver=15")
        self.assertEqual(d, {"ram": 32.0, "cpu": 0, "disk": "ssd", "place": "shared", "docker": 1.0})
        v = S.pg_read(d)
        self.assertEqual((v["ram"], v["cpu"], v["disk"]), (32.0, 4, "ssd"))


if __name__ == "__main__":
    unittest.main()
