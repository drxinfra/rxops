#!/usr/bin/env python3
"""Ищет в журналах известные ошибки из справочника drxinfra.ru/errors.

Использование:
  python3 match_errors.py ПУТЬ [ПУТЬ...]     файлы и папки (папки обходятся вглубь)
  КОМАНДА | python3 match_errors.py -       стандартный ввод: journalctl, docker logs, kubectl logs

Ключи:
  --since ВРЕМЯ   учитывать записи не раньше: 2026-11-05, "2026-11-05 10:15", 10:15 (сегодня),
                  2h, 30m, 1d (столько назад от текущего момента; можно 2ч, 30м, 1д)
  --until ВРЕМЯ   учитывать записи не позже, форматы те же
  --details       добавить к каждой ошибке «Как проверить» и «Что сделать»
  --json          вывод в JSON
  --sort ПОРЯДОК  count (по умолчанию, сначала частые) или time (по первому появлению)
  --base ФАЙЛ     другой файл базы, по умолчанию known_errors.json рядом со скриптом

Что понимает:
  - журналы Directum RX: строка JSON на запись (поля t, l, lg, mt, args, ex, tr, un), в том числе
    битые строки веб-агента с одиночным обратным слешем;
  - обёртки контейнеров: docker json-file (/var/lib/docker/containers) и CRI (/var/log/pods);
  - обычный текст: PostgreSQL, nginx, RabbitMQ, Elasticsearch, journalctl, dmesg и другие;
  - сжатые файлы .gz, .bz2, .xz.
  В папках пропускаются статус-файлы *.status, двоичные файлы и файлы journald (*.journal):
  их читают через journalctl ... | match_errors.py -

Как считает:
  - шаблоны те же, что у поиска на сайте, без учёта регистра;
  - если строка подходит под несколько ошибок, она засчитывается самой точной (длиннее совпадение),
    остальные показаны в «также подходит»;
  - голый код SQLSTATE (57014, 40P01) засчитывается, только если рядом есть признак PostgreSQL
    или после кода стоит двоеточие; имя проверки работоспособности со статусом healthy не считается;
  - время записи берётся из самой строки, строки стека наследуют время первой строки записи;
    время без часового пояса считается местным временем этой машины;
  - с --since файлы, которые по дате в имени или по времени изменения закончились раньше окна
    больше чем на сутки, пропускаются целиком.
  Пример строки проходит через mask_secrets.py: пароли и токены в вывод не попадают.

Коды возврата: 0 известных ошибок нет, 1 найдены, 2 не удалось проверить
(нет базы, ни один файл не прочитан, ошибка в ключах).
Только стандартная библиотека, Python 3.6+.
"""
import argparse
import bz2
import datetime
import gzip
import io
import json
import os
import re
import sys
import warnings

try:
    import lzma
except ImportError:  # в урезанных сборках Python модуля нет
    lzma = None

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
try:
    from mask_secrets import mask as _mask
except ImportError:
    _mask = None

DEFAULT_BASE = os.path.join(HERE, "known_errors.json")
MAX_LINE = 256 * 1024     # длиннее строку режем: поиск по мегабайтной строке медленный
SAMPLE_LEN = 400
UTC = datetime.timezone.utc

# ---------------------------------------------------------------- шаблоны

def js_to_py(p):
    """Шаблоны сайта пишутся для RegExp браузера с флагом i. Сборка сайта проверяет их через re
    Python, поэтому синтаксис совпадает; на всякий случай переводим то, что пишется по-разному."""
    p = re.sub(r"\(\?<(?=[A-Za-z_])", "(?P<", p)   # именованная группа
    return p.replace("[^]", r"[\s\S]")               # любой символ


def _class_end(p, i):
    """p[i] == '['. Индекс после закрывающей ']'."""
    j = i + 1
    if p[j:j + 1] == "^":
        j += 1
    if p[j:j + 1] == "]":
        j += 1
    while j < len(p) and p[j] != "]":
        j += 2 if p[j] == "\\" else 1
    return j + 1


def _group_end(p, i):
    """p[i] == '('. Индекс после парной ')'."""
    depth, j = 0, i
    while j < len(p):
        c = p[j]
        if c == "\\":
            j += 2
            continue
        if c == "[":
            j = _class_end(p, j)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return j


def split_top(p):
    """Ветки шаблона по | верхнего уровня."""
    out, start, depth, i = [], 0, 0, 0
    while i < len(p):
        c = p[i]
        if c == "\\":
            i += 2
            continue
        if c == "[":
            i = _class_end(p, i)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "|" and depth == 0:
            out.append(p[start:i])
            start = i + 1
        i += 1
    out.append(p[start:])
    return out


_QUANT = re.compile(r"\{(\d*)(?:,(\d*))?\}")
# В сыром JSON эти символы экранируются без \u: обязательную подстроку на них обрываем.
_BREAK = set('"\\/')


def literal_runs(branch):
    """Куски текста, без которых ветка шаблона совпасть не может. Группы, классы и
    необязательные символы пропускаются, поэтому результат всегда безопасно занижен."""
    runs, cur, i, b = [], [], 0, branch

    def cut():
        if cur:
            runs.append("".join(cur))
            del cur[:]
    while i < len(b):
        c = b[i]
        if c == "\\":
            n = b[i + 1:i + 2]
            if not n:
                break
            if n.isalnum():      # \b \d \w \s \x41 \u0041: не буквальный символ
                cut()
                i += 2 + {"x": 2, "u": 4, "c": 1}.get(n, 0)
                continue
            ch, i = n, i + 2
        elif c == "[":
            cut()
            i = _class_end(b, i)
            continue
        elif c == "(":
            cut()
            i = _group_end(b, i)
            continue
        elif c in ".^$|)":
            cut()
            i += 1
            continue
        elif c in "?*":
            if cur:
                cur.pop()
            cut()
            i += 1
            continue
        elif c == "+":
            cut()
            i += 1
            continue
        elif c == "{":
            m = _QUANT.match(b, i)
            if m and (m.group(1) or m.group(2)):
                if cur and m.group(1) in ("", "0"):
                    cur.pop()
                cut()
                i = m.end()
                continue
            ch, i = c, i + 1
        else:
            ch, i = c, i + 1
        if ch in _BREAK:
            cut()
            continue
        cur.append(ch)
    cut()
    return runs


def required_literals(p):
    """По одной обязательной подстроке (в нижнем регистре) на каждую ветку шаблона.
    None, если у какой-то ветки нет куска длиной от трёх символов: такой шаблон проверяется всегда."""
    out = []
    for b in split_top(p):
        runs = [r.lower() for r in literal_runs(b) if len(r.strip()) >= 3]
        if not runs:
            return None
        out.append(max(runs, key=len))
    return out


# Грубая частота символов в журналах: чем левее, тем чаще встречается.
_ORDER = ' e"t:a0o1i,2n-s.r3l4c5d6u7m8h9f{}pgbywvkx_jqz/()[]\'=+;|'


def _freq(c):
    i = _ORDER.find(c)
    if i >= 0:
        return len(_ORDER) - i + 10
    if "а" <= c <= "я" or c == "ё":
        return 18
    return 4


def choose_keys(literals, minlen=4):
    """Для быстрого отсева берём от каждой подстроки хвост, который начинается с одного
    из немногих редких символов: регулярке тогда почти не на чем останавливаться."""
    cands = {}
    for l in literals:
        need = min(minlen, len(l))
        cands[l] = {l[i] for i in range(len(l) - need + 1)}
    left, roots = set(literals), set()
    while left:
        counts = {}
        for l in left:
            for c in cands[l]:
                counts[c] = counts.get(c, 0) + 1
        best = min(sorted(counts), key=lambda c: _freq(c) / counts[c])
        roots.add(best)
        left = {l for l in left if best not in cands[l]}
    keys = set()
    for l in literals:
        need = min(minlen, len(l))
        keys.add(l[min(i for i in range(len(l) - need + 1) if l[i] in roots):])
    return sorted(keys)


def trie_pattern(words):
    """Одна регулярка «есть ли в строке хоть одно из слов», собранная деревом по префиксам."""
    trie = {}
    for w in words:
        d = trie
        for ch in w:
            d = d.setdefault(ch, {})
        d[""] = {}

    def build(d):
        if "" in d:
            return ""          # короткое слово уже найдено, продолжения не нужны
        alts = [re.escape(ch) + build(d[ch]) for ch in sorted(d)]
        return alts[0] if len(alts) == 1 else "(?:" + "|".join(alts) + ")"
    return build(trie)


_WORDS = re.compile(r"[0-9a-zа-яё_]{3,}")
_BARE_CODE = re.compile(r"^(?:\\b)?([0-9A-Z]{5})(?:\\b)?$")
_BARE_NAME = re.compile(r"^(?:\\b)?([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)(?:\\b)?$")
_PG_CONTEXT = re.compile(r"sqlstate|postgres|npgsql|psql|pg_|\bpq:", re.I)
_HEALTHY_AFTER = re.compile(r"""^[\s"':,{=]*(?:"?status"?[\s"':=]*)?healthy\b""", re.I)


def _code_ok(text, m):
    """Голый SQLSTATE в журнале легко совпадает с номером документа или длительностью."""
    return text[m.end():m.end() + 1] == ":" or _PG_CONTEXT.search(text) is not None


def _name_ok(text, m):
    """Имя проверки (INDEXING_SERVICE) со статусом healthy: штатная запись о состоянии."""
    return _HEALTHY_AFTER.match(text[m.end():m.end() + 40]) is None


class Rule(object):
    __slots__ = ("entry", "rx", "ok")

    def __init__(self, entry, rx, ok):
        self.entry, self.rx, self.ok = entry, rx, ok

    def best(self, texts):
        """Самое длинное подходящее совпадение или пустая строка."""
        best = ""
        for t in texts:
            if self.ok is None:
                m = self.rx.search(t)
                if m and len(m.group(0)) > len(best):
                    best = m.group(0)
            else:
                for m in self.rx.finditer(t):
                    if len(m.group(0)) > len(best) and self.ok(t, m):
                        best = m.group(0)
        return best


class Base(object):
    """Известные ошибки и шаблоны к ним."""

    def __init__(self, path):
        with io.open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.entries = data["errors"] if isinstance(data, dict) else data
        self.bad, self.always, self.by_lit = [], [], {}
        for i, e in enumerate(self.entries):
            for p in e.get("patterns", []):
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        rx = re.compile(js_to_py(p), re.I | re.M)
                except re.error as ex:
                    self.bad.append("%s: %r: %s" % (e.get("slug"), p, ex))
                    continue
                ok = None
                m = _BARE_CODE.match(p)
                if m and sum(ch.isdigit() for ch in m.group(1)) >= 3:
                    ok = _code_ok
                elif _BARE_NAME.match(p):
                    ok = _name_ok
                rule = Rule(i, rx, ok)
                lits = required_literals(p)
                if lits is None:
                    self.always.append(rule)
                else:
                    for l in lits:
                        self.by_lit.setdefault(l, []).append(rule)
        self.lits = sorted(self.by_lit)
        keys = choose_keys(self.lits) if self.lits else []
        self.pre = re.compile(trie_pattern(keys)) if keys else None
        self.words = [set(_WORDS.findall(e.get("title", "").lower())) for e in self.entries]

    def winner(self, scores, lows):
        """Самая точная ошибка: длиннее совпадение; при равенстве больше совпавших шаблонов,
        затем большая доля слов её заголовка есть в строке."""
        if len(scores) == 1:
            return next(iter(scores))
        words = set()
        for low in lows:
            words.update(_WORDS.findall(low))

        def key(e):
            w = self.words[e]
            return scores[e][0], scores[e][1], (len(w & words) / float(len(w))) if w else 0.0, -e
        return max(scores, key=key)

    def passes(self, low):
        """Быстрый отсев: может ли строка (в нижнем регистре) совпасть хоть с одним шаблоном."""
        return bool(self.always) or (self.pre is not None and self.pre.search(low) is not None)

    def scores(self, texts, lows):
        """{номер ошибки: [длина лучшего совпадения, сколько шаблонов совпало, совпавший текст]}"""
        rules, seen = list(self.always), set()
        for l in self.lits:
            for low in lows:
                if l in low:
                    for r in self.by_lit[l]:
                        if id(r) not in seen:
                            seen.add(id(r))
                            rules.append(r)
                    break
        out = {}
        for r in rules:
            m = r.best(texts)
            if m:
                s = out.get(r.entry)
                if s is None:
                    out[r.entry] = [len(m), 1, m]
                else:
                    if len(m) > s[0]:
                        s[0], s[2] = len(m), m
                    s[1] += 1
        return out


# ---------------------------------------------------------------- время

_MON = {m: i + 1 for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split())}
_ISO = re.compile(r"(\d{4})[-/](\d\d)[-/](\d\d)[T ](\d\d):(\d\d):(\d\d)(?:[.,](\d{1,9}))?"
                  r"(?: ?(Z|[+-]\d\d(?::?\d\d)?|UTC|GMT)(?![A-Za-z]))?")
_CLF = re.compile(r"(\d\d)/([A-Z][a-z]{2})/(\d{4}):(\d\d):(\d\d):(\d\d)(?: ([+-]\d{4}))?")
_DMESG = re.compile(r"^\[[A-Z][a-z]{2} ([A-Z][a-z]{2}) {1,2}(\d{1,2}) (\d\d):(\d\d):(\d\d) (\d{4})\]")
_SYSLOG = re.compile(r"^([A-Z][a-z]{2}) {1,2}(\d{1,2}) (\d\d):(\d\d):(\d\d)")
_KLOG = re.compile(r"^[IWEF](\d\d)(\d\d) (\d\d):(\d\d):(\d\d)\.(\d+)")
_TZ = {}


def _tz(z):
    if not z:
        return None
    if z not in _TZ:
        if z in ("Z", "UTC", "GMT"):
            _TZ[z] = UTC
        else:
            d = z[1:].replace(":", "")
            off = datetime.timedelta(hours=int(d[:2]), minutes=int(d[2:4] or 0))
            _TZ[z] = datetime.timezone(-off if z[0] == "-" else off)
    return _TZ[z]


def _aware(dt, tz):
    if tz is not None:
        return dt.replace(tzinfo=tz)
    try:
        return dt.astimezone()       # без пояса: местное время этой машины
    except (OverflowError, OSError, ValueError):
        return dt.replace(tzinfo=UTC)


def _mk(y, mo, d, h, mi, s, frac=None, tz=None):
    try:
        us = int((frac or "0")[:6].ljust(6, "0"))
        return _aware(datetime.datetime(int(y), int(mo), int(d), int(h), int(mi), min(int(s), 59), us), tz)
    except (ValueError, OverflowError):
        return None


def _no_year(mo, d, h, mi, s, frac=None):
    """syslog и klog пишут время без года: берём текущий, а если вышло будущее, прошлый."""
    now = datetime.datetime.now()
    dt = _mk(now.year, mo, d, h, mi, s, frac)
    if dt is not None and dt > datetime.datetime.now(UTC) + datetime.timedelta(days=2):
        dt = _mk(now.year - 1, mo, d, h, mi, s, frac)
    return dt


def line_time(s):
    """(время, как записано) из начала строки журнала или (None, None)."""
    m = _ISO.search(s, 0, 72)
    if m and m.start() <= 40:
        return _mk(*m.groups()[:7], tz=_tz(m.group(8))), m.group(0)
    m = _CLF.search(s, 0, 160)
    if m and m.group(2) in _MON:
        return _mk(m.group(3), _MON[m.group(2)], m.group(1), *m.groups()[3:6], tz=_tz(m.group(7))), m.group(0)
    m = _DMESG.match(s)
    if m and m.group(1) in _MON:
        return _mk(m.group(6), _MON[m.group(1)], *m.groups()[1:5]), m.group(0)[1:-1]
    m = _SYSLOG.match(s)
    if m and m.group(1) in _MON:
        return _no_year(_MON[m.group(1)], *m.groups()[1:5]), m.group(0)
    m = _KLOG.match(s)
    if m:
        return _no_year(*m.groups()), m.group(0)[1:]
    return None, None


def value_time(v):
    """Время из поля JSON: строка или число секунд и миллисекунд от 1970 года."""
    if isinstance(v, str):
        m = _ISO.match(v.strip())
        if m:
            return _mk(*m.groups()[:7], tz=_tz(m.group(8))), v
        return line_time(v)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        x = float(v)
        if 1e12 <= x < 1e14:
            x /= 1000.0
        if 1e9 <= x < 1e11:
            return datetime.datetime.fromtimestamp(x, UTC), str(v)
    return None, None


_REL = re.compile(r"^(\d+)\s*([smhdсмчд])$", re.I)
_UNITS = {"s": 1, "с": 1, "m": 60, "м": 60, "h": 3600, "ч": 3600, "d": 86400, "д": 86400}


def parse_bound(s, now=None):
    """Граница окна для --since и --until."""
    now = now or datetime.datetime.now(UTC).astimezone()
    s = s.strip()
    m = _REL.match(s)
    if m:
        return now - datetime.timedelta(seconds=int(m.group(1)) * _UNITS[m.group(2).lower()])
    m = re.match(r"^(\d{1,2}):(\d\d)(?::(\d\d))?$", s)
    if m:
        local = now.astimezone()
        dt = _mk(local.year, local.month, local.day, m.group(1), m.group(2), m.group(3) or 0)
        if dt:
            return dt
    m = re.match(r"^(\d{4})-(\d\d)-(\d\d)$", s)
    if m:
        dt = _mk(m.group(1), m.group(2), m.group(3), 0, 0, 0)
        if dt:
            return dt
    m = re.match(r"^(\d{4})-(\d\d)-(\d\d)[T ](\d{1,2}):(\d\d)(?::(\d\d)(?:[.,](\d{1,9}))?)?"
                 r"\s*(Z|[+-]\d\d(?::?\d\d)?|UTC)?$", s)
    if m:
        dt = _mk(m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6) or 0,
                 m.group(7), _tz(m.group(8)))
        if dt:
            return dt
    raise argparse.ArgumentTypeError(
        "не понял время %r: нужно 2026-11-05, \"2026-11-05 10:15\", 10:15, 2h, 30m или 1d" % s)


# ---------------------------------------------------------------- записи

_CRI = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)) (?:stdout|stderr) [FP] ?(.*)$", re.S)
_LONE_BS = re.compile(r'\\(?![\\/"bfnrt]|u[0-9a-fA-F]{4})')
_PH = re.compile(r"\{([@$]?)([A-Za-z_][\w.]*|\d+)(?:[,:][^{}]*)?\}")
_TIME_KEYS = ("t", "@t", "@timestamp", "timestamp", "time", "ts", "date", "datetime")
_LEVEL_PREFIX = re.compile(r"^[\s\]\[|:,]*(?:(?:FATAL|PANIC|ERROR|ERR|WARN(?:ING)?|INFO|DEBUG|TRACE|LOG|CRIT(?:ICAL)?|NOTICE)"
                           r"\b\]?\s*[:|-]?\s*)?", re.I)
_START = set("0123456789[{<")


def loads(s):
    """JSON-объект из строки или None. Веб-агент RX пишет пути Windows с одиночным
    обратным слешем, такую строку чиним и разбираем снова."""
    try:
        v = json.loads(s)
    except ValueError:
        if "\\" not in s:
            return None
        try:
            v = json.loads(_LONE_BS.sub(r"\\\\", s))
        except ValueError:
            return None
    return v if isinstance(v, dict) else None


def _s(v):
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def render(mt, args):
    """Шаблон mt с подставленными args, как его видит человек."""
    if not isinstance(mt, str):
        return "" if mt is None else _s(mt)
    if "{" not in mt or not isinstance(args, (dict, list)):
        return mt

    def sub(m):
        k = m.group(2)
        if isinstance(args, dict) and k in args:
            return _s(args[k])
        if isinstance(args, list) and k.isdigit() and int(k) < len(args):
            return _s(args[int(k)])
        return m.group(0)
    return _PH.sub(sub, mt)


def _strings(v, out, depth=0):
    if depth > 8 or len(out) > 400:
        return
    if isinstance(v, str):
        if v:
            out.append(v)
    elif isinstance(v, dict):
        for k in v:
            _strings(v[k], out, depth + 1)
    elif isinstance(v, list):
        for x in v:
            _strings(x, out, depth + 1)


def _ex_head(ex):
    if isinstance(ex, dict):
        return ": ".join(x for x in (ex.get("type"), ex.get("m")) if isinstance(x, str) and x)
    if isinstance(ex, str):
        return ex.split("\n", 1)[0]
    return ""


def record_text(obj):
    """Всё текстовое из записи JSON, по полю в строке: так шаблоны с ^ и $ работают по полям,
    а экранирование JSON (\\", \\u0441) не мешает."""
    parts = [render(obj.get("mt"), obj.get("args")), _ex_head(obj.get("ex"))]
    _strings(obj, parts)
    return "\n".join(p for p in parts if p)


def compact(obj):
    """Запись RX одной строкой: время, уровень, логгер, текст, исключение, пользователь, трасса."""
    head = [obj[k] for k in ("t", "l", "lg") if isinstance(obj.get(k), str) and obj[k]]
    if not head:
        return json.dumps(obj, ensure_ascii=False)
    msg = render(obj.get("mt"), obj.get("args"))
    if msg:
        head.append(msg)
    span = obj.get("span")
    if isinstance(span, dict):
        bits = [_s(span[k]) for k in ("name", "status") if span.get(k)]
        if isinstance(span.get("durationMs"), (int, float)):
            bits.append("%s мс" % span["durationMs"])
        if bits:
            head.append("span: " + " ".join(bits))
    ex = _ex_head(obj.get("ex"))
    if ex:
        head.append("| " + ex)
    head += ["%s=%s" % (k, obj[k]) for k in ("un", "tr") if isinstance(obj.get(k), str) and obj[k]]
    return " ".join(head)


class Record(object):
    """Разобранная строка: тексты для шаблонов, время, запись JSON (если это JSON)."""
    __slots__ = ("texts", "when", "raw_when", "obj")

    def __init__(self, line):
        s = line.rstrip("\r\n")
        outer, obj = None, None
        m = _CRI.match(s)
        if m:
            outer, s = m.group(1), m.group(2)
        j = s.find("{")
        if j == 0 or (0 < j <= 80 and s[j:j + 2] == '{"'):
            obj = loads(s[j:])
            if obj is not None and isinstance(obj.get("log"), str) and ("stream" in obj or "time" in obj):
                if isinstance(obj.get("time"), str):      # docker json-file
                    outer = obj["time"]
                s = obj["log"].rstrip("\r\n")
                j = s.find("{")
                obj = loads(s[j:]) if j == 0 or (0 < j <= 80 and s[j:j + 2] == '{"') else None
        self.texts, self.obj = [s], obj
        self.when = self.raw_when = None
        if obj is not None:
            self.texts.append(record_text(obj))
            for k in _TIME_KEYS:
                if k in obj:
                    self.when, self.raw_when = value_time(obj[k])
                    if self.when:
                        break
        if self.when is None:
            self.when, self.raw_when = line_time(s)
            if obj is None and self.raw_when:
                # без времени и уровня в начале: так работают шаблоны с ^ вроде «^Задача уже отправлена»
                k = s.find(self.raw_when, 0, 120)
                if k >= 0:
                    rest = _LEVEL_PREFIX.sub("", s[k + len(self.raw_when):], count=1)
                    if rest and rest != s:
                        self.texts.append(rest)
        if self.when is None and outer:
            self.when, self.raw_when = value_time(outer)

    @property
    def display(self):
        """Запись одной строкой для примера: JSON RX коротко, остальное как есть."""
        return compact(self.obj) if self.obj is not None else self.texts[0]


# ---------------------------------------------------------------- чтение

class Skip(Exception):
    pass


def open_text(path):
    with open(path, "rb") as f:
        head = f.read(4096)
    kw = {"encoding": "utf-8", "errors": "replace"}
    if head[:2] == b"\x1f\x8b":
        return gzip.open(path, "rt", **kw)
    if head[:3] == b"BZh":
        return bz2.open(path, "rt", **kw)
    if head[:6] == b"\xfd7zXZ\x00":
        if lzma is None:
            raise Skip("сжато xz, а модуля lzma в этом Python нет: xz -dc ФАЙЛ | match_errors.py -")
        return lzma.open(path, "rt", **kw)
    if head[:4] == b"(\xb5/\xfd":
        raise Skip("сжато zstd: zstd -dc ФАЙЛ | match_errors.py -")
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return io.open(path, "r", encoding="utf-16", errors="replace")
    if b"\x00" in head:
        raise Skip("двоичный файл")
    return io.open(path, "r", encoding="utf-8-sig", errors="replace")


_NAME_DATE = re.compile(r"(?<![0-9A-Za-z])(20\d\d)(-?)(\d\d)\2(\d\d)(?![0-9A-Za-z])")


def outside_since(path, since):
    """Файл целиком раньше окна (с запасом в сутки на часовые пояса)? Причина или None."""
    if since is None:
        return None
    edge = since - datetime.timedelta(days=1)
    m = _NAME_DATE.search(os.path.basename(path))
    if m:
        try:
            d = datetime.date(int(m.group(1)), int(m.group(3)), int(m.group(4)))
            if d < edge.astimezone().date():
                return "по дате в имени раньше окна"
        except ValueError:
            pass
    try:
        if os.path.getmtime(path) < edge.timestamp():
            return "не менялся с начала окна"
    except OSError:
        pass
    return None


def iter_inputs(paths, skipped):
    """(путь, показываемое имя) для каждого файла, папки обходятся вглубь."""
    for p in paths:
        if p == "-":
            yield "-", "<stdin>"
        elif os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs.sort()
                for name in sorted(files):
                    full = os.path.join(root, name)
                    if name.endswith(".status"):
                        continue
                    if name.endswith((".journal", ".journal~")):
                        skipped.append((full, "журнал journald: journalctl --file ФАЙЛ | match_errors.py -"))
                        continue
                    yield full, full
        elif os.path.exists(p):
            yield p, p
        else:
            skipped.append((p, "нет такого файла или папки"))


class Hit(object):
    __slots__ = ("count", "first", "first_raw", "last", "last_raw", "files", "sample", "matched", "also")

    def __init__(self):
        self.count = 0
        self.first = self.first_raw = self.last = self.last_raw = None
        self.files, self.also, self.sample, self.matched = {}, {}, None, ""


class Scan(object):
    def __init__(self, base, since=None, until=None):
        self.base, self.since, self.until = base, since, until
        self.hits, self.skipped = {}, []
        self.files = self.lines = self.untimed = 0

    def stream(self, f, name):
        base, since, until = self.base, self.since, self.until
        window = since is not None or until is not None
        last_start, start_when, n = None, False, 0
        for n, line in enumerate(f, 1):
            if len(line) > MAX_LINE:
                line = line[:MAX_LINE]
            start = line[:1] in _START or line[:3] in _MON
            rec = None
            if not base.passes(line.lower()):
                if "\\u" in line and "{" in line:      # JSON с \uXXXX: проверяем разобранный текст
                    rec = Record(line)
                    if not any(base.passes(t.lower()) for t in rec.texts):
                        rec = None
                if rec is None:
                    if start:
                        last_start, start_when = line, False
                    continue
            if rec is None:
                rec = Record(line)
            when, raw = rec.when, rec.raw_when
            if when is None and not start and last_start is not None:
                if start_when is False:
                    start_when = line_time(last_start)
                when, raw = start_when
            if start:
                last_start, start_when = line, False
            if window and when is not None:
                if (since is not None and when < since) or (until is not None and when > until):
                    continue
            lows = [t.lower() for t in rec.texts]
            scores = base.scores(rec.texts, lows)
            if not scores:
                continue
            if window and when is None:
                self.untimed += 1
            win = base.winner(scores, lows)
            h = self.hits.get(win)
            if h is None:
                h = self.hits[win] = Hit()
            h.count += 1
            h.files[name] = h.files.get(name, 0) + 1
            if h.sample is None:
                h.sample, h.matched = (name, n, rec.display), scores[win][2]
            if when is not None:
                if h.first is None or when < h.first:
                    h.first, h.first_raw = when, raw
                if h.last is None or when > h.last:
                    h.last, h.last_raw = when, raw
            for e in scores:
                if e != win:
                    h.also[e] = h.also.get(e, 0) + 1
        self.lines += n

    def path(self, path, name):
        if path == "-":
            stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8-sig", errors="replace")
            self.stream(stdin, name)
            self.files += 1
            return
        why = outside_since(path, self.since)
        if why:
            self.skipped.append((name, "вне окна: " + why))
            return
        try:
            with open_text(path) as f:
                self.stream(f, name)
            self.files += 1
        except Skip as e:
            self.skipped.append((name, str(e)))
        except EOFError:
            self.files += 1
            self.skipped.append((name, "сжатый файл обрезан, прочитано до обрыва"))
        except (OSError, IOError) as e:
            self.skipped.append((name, "не читается: %s" % (e.strerror or e)))


# ---------------------------------------------------------------- вывод

def _num(n):
    return "{:,}".format(n).replace(",", " ")


def _pl(n, one, few, many):
    """Склонение по числу: 1 файл, 2 файла, 5 файлов."""
    if n % 10 == 1 and n % 100 != 11:
        w = one
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        w = few
    else:
        w = many
    return "%s %s" % (_num(n), w)


def _iso(dt):
    if dt is None:
        return None
    return dt.isoformat(timespec="milliseconds" if dt.microsecond else "seconds")


def safe(text):
    """Пример строки: через маскировщик, потом обрезка (маска видит строку целиком)."""
    if _mask is None:
        return "‹пример скрыт: рядом нет mask_secrets.py›"
    text = _mask(text)[0].replace("\n", " ⏎ ")
    return text if len(text) <= SAMPLE_LEN else text[:SAMPLE_LEN] + "…"


def ordered(scan, how):
    items = list(scan.hits.items())
    if how == "time":
        far = datetime.datetime.max.replace(tzinfo=UTC)
        items.sort(key=lambda kv: (kv[1].first or far, -kv[1].count, kv[0]))
    else:
        items.sort(key=lambda kv: (-kv[1].count, kv[1].first or datetime.datetime.max.replace(tzinfo=UTC), kv[0]))
    return items


def as_json(scan, args):
    E = scan.base.entries
    found = []
    for i, h in ordered(scan, args.sort):
        e = E[i]
        item = {
            "slug": e["slug"], "title": e["title"], "what": e["what"], "component": e["component"],
            "url": e["url"], "count": h.count,
            "first": _iso(h.first), "last": _iso(h.last), "first_raw": h.first_raw, "last_raw": h.last_raw,
            "files": dict(sorted(h.files.items(), key=lambda kv: -kv[1])),
            "sample": {"file": h.sample[0], "line": h.sample[1], "text": safe(h.sample[2]),
                       "matched": safe(h.matched)},
            "also": [{"slug": E[j]["slug"], "what": E[j]["what"], "count": c, "url": E[j]["url"]}
                     for j, c in sorted(h.also.items(), key=lambda kv: (-kv[1], kv[0]))],
        }
        if args.details:
            item["check"], item["fix"] = e.get("check", ""), e.get("fix", "")
        found.append(item)
    return json.dumps({
        "scanned": {"files": scan.files, "lines": scan.lines,
                    "since": _iso(scan.since), "until": _iso(scan.until),
                    "matched_lines_without_time": scan.untimed,
                    "skipped": [{"path": p, "reason": r} for p, r in scan.skipped]},
        "base": {"entries": len(E), "bad_patterns": scan.base.bad,
                 "patterns_without_prefilter": len(scan.base.always)},
        "found": found,
    }, ensure_ascii=False, indent=1)


def _indent(text, pad="     "):
    return "\n".join(pad + l if l.strip() else "" for l in text.split("\n"))


def as_text(scan, args):
    E, out = scan.base.entries, []
    head = "Просмотрено: %s, %s" % (_pl(scan.files, "файл", "файла", "файлов"),
                                    _pl(scan.lines, "строка", "строки", "строк"))
    if scan.since or scan.until:
        head += ". Окно: с %s по %s" % (_iso(scan.since) or "начала", _iso(scan.until) or "конца")
    out.append(head + ".")
    if scan.skipped:
        out.append("Пропущено: %d." % len(scan.skipped))
        for p, r in scan.skipped[:10]:
            out.append("  %s: %s" % (p, r))
        if len(scan.skipped) > 10:
            out.append("  и ещё %d (список целиком в --json)" % (len(scan.skipped) - 10))
    if scan.base.bad:
        out.append("Шаблоны, которые не удалось разобрать: %d (список в --json)." % len(scan.base.bad))
    if not scan.hits:
        out.append("")
        out.append("Известных ошибок из справочника не найдено.")
        out.append("Справочник знает не все ошибки: посмотрите записи уровня Error и Fatal за то же время.")
        return "\n".join(out)
    total = sum(h.count for h in scan.hits.values())
    out.append("")
    out.append("Найдено известных ошибок: %d, %s с ними." % (len(scan.hits), _pl(total, "строка", "строки", "строк")))
    if scan.untimed:
        out.append("Из них без метки времени, окно к ним не применялось: %s." % _num(scan.untimed))
    for k, (i, h) in enumerate(ordered(scan, args.sort), 1):
        e = E[i]
        out.append("")
        out.append("%d. %s [%s]" % (k, e["what"], e["component"]))
        out.append("   Ошибка: %s" % e["title"])
        when = ""
        if h.first is not None:
            when = ", первая %s, последняя %s" % (h.first_raw, h.last_raw) if h.count > 1 else ", %s" % h.first_raw
        out.append("   Строк: %s%s" % (_num(h.count), when))
        out.append("   Совпало: %s" % safe(h.matched))
        files = sorted(h.files.items(), key=lambda kv: -kv[1])
        shown = ", ".join("%s (%s)" % (p, _num(c)) for p, c in files[:3])
        if len(files) > 3:
            shown += " и ещё %d" % (len(files) - 3)
        out.append("   Файлы: %s" % shown)
        out.append("   Пример, %s:%d:" % (h.sample[0], h.sample[1]))
        out.append("     " + safe(h.sample[2]))
        if h.also:
            also = sorted(h.also.items(), key=lambda kv: (-kv[1], kv[0]))
            out.append("   Также подходит: " + "; ".join(
                "%s, %s (%s)" % (E[j]["what"], E[j]["slug"], _num(c)) for j, c in also[:3]))
        out.append("   Разбор: %s" % e["url"])
        if args.details:
            for title, key in (("Как проверить", "check"), ("Что сделать", "fix")):
                if e.get(key):
                    out.append("   %s:" % title)
                    out.append(_indent(e[key]))
    return "\n".join(out)


def _utf8_stdout():
    """Python 3.6 с локалью C пишет в ASCII и падает на кириллице."""
    for name in ("stdout", "stderr"):
        s = getattr(sys, name)
        if (getattr(s, "encoding", "") or "").lower().replace("-", "") != "utf8" and hasattr(s, "buffer"):
            setattr(sys, name, io.TextIOWrapper(s.buffer, encoding="utf-8", errors="replace", line_buffering=True))


def main(argv):
    _utf8_stdout()
    ap = argparse.ArgumentParser(prog="match_errors.py", add_help=False, usage="%(prog)s [ПУТЬ ...] [ключи]")
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--since", type=parse_bound)
    ap.add_argument("--until", type=parse_bound)
    ap.add_argument("--details", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--sort", choices=("count", "time"), default="count")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("-h", "--help", action="store_true")
    args = ap.parse_args(argv)
    if args.help:
        print(__doc__.strip())
        return 0
    if args.since and args.until and args.since > args.until:
        sys.stderr.write("match_errors: --since позже --until\n")
        return 2
    if not args.paths:
        if sys.stdin.isatty():
            sys.stderr.write("match_errors: укажите файлы или папки, или передайте журнал на ввод: КОМАНДА | match_errors.py -\n")
            return 2
        args.paths = ["-"]
    try:
        base = Base(args.base)
    except (OSError, IOError, ValueError, KeyError) as e:
        sys.stderr.write("match_errors: не читается база %s: %s\n" % (args.base, e))
        return 2
    scan = Scan(base, args.since, args.until)
    for path, name in iter_inputs(args.paths, scan.skipped):
        scan.path(path, name)
    print(as_json(scan, args) if args.json else as_text(scan, args))
    if scan.files == 0:
        if scan.skipped and all(r.startswith("вне окна") for _, r in scan.skipped):
            return 0      # все файлы закончились до начала окна: ошибок в окне нет
        sys.stderr.write("match_errors: ни один файл не прочитан\n")
        return 2
    return 1 if scan.hits else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(2)
    except BrokenPipeError:     # вывод обрезали через head
        try:
            sys.stdout.close()
        except Exception:
            pass
        sys.exit(0)
