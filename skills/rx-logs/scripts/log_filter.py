#!/usr/bin/env python3
"""Показывает записи журналов Directum RX коротко и по фильтрам. Замена jq там, где его нет.

Использование:
  python3 log_filter.py ПУТЬ [ПУТЬ...] [фильтры]
  КОМАНДА | python3 log_filter.py - [фильтры]

Фильтры (действуют вместе):
  --since ВРЕМЯ, --until ВРЕМЯ  окно: 2026-11-05, "2026-11-05 10:15", 10:15 (сегодня), 2h, 30m, 1d
  --level СПИСОК                уровни через запятую: Error,Fatal (Warn и Warning одно и то же)
  --user ТЕКСТ                  учётная запись (поле un) содержит текст, без учёта регистра
  --trace НАЧАЛО                трасса (поле tr) начинается с текста, например cl-4e326a73
  --logger ТЕКСТ                логгер (поле lg) содержит текст
  --grep РЕГУЛЯРКА              в тексте записи есть совпадение, без учёта регистра
  --slow МС                     операции дольше МС миллисекунд (span.durationMs)

Вывод:
  по умолчанию   файл:строка, время, уровень, логгер, текст | исключение, un=, tr=
  --stack        добавить стек исключения (до 30 строк)
  --top N        вместо записей N самых частых видов сообщений: числа, GUID и строки в кавычках
                 заменены заглушками, у каждого счётчик и время первой и последней записи
  --max N        показать не больше N записей (по умолчанию 200), остальные только посчитать
  Всё выводимое проходит через mask_secrets.py.

Строки не в формате JSON (PostgreSQL, nginx, RabbitMQ) тоже показываются. Уровень для них
берётся из слов ERROR, FATAL, PANIC, WARNING и [error], [warn] в строке; фильтры по пользователю,
трассе, логгеру и длительности к ним не применяются, такие строки отбрасываются.
Время без часового пояса считается местным временем этой машины.

Коды возврата: 0 записи найдены, 1 под фильтры ничего не попало, 2 не удалось проверить.
Только стандартная библиотека, Python 3.6+. Нужен match_errors.py рядом.
"""
import argparse
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import match_errors as M  # noqa: E402

LINE_LEN = 1000
STACK_LINES = 30
_LEVEL_WORD = re.compile(r"\b(PANIC|FATAL|ERROR|WARNING|WARN)\b|\[(emerg|alert|crit|error|warn)\]", re.I)
_LEVELS = {"panic": "fatal", "emerg": "fatal", "alert": "fatal", "crit": "fatal", "critical": "fatal",
           "warn": "warning", "err": "error"}
_NORM = (
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I), "{guid}"),
    (re.compile(r"\"[^\"]{0,200}\"|'[^']{0,200}'|«[^»]{0,200}»"), "{s}"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "{hex}"),
    (re.compile(r"\d+"), "{n}"),
)


def norm_level(v):
    v = (v or "").strip().lower()
    return _LEVELS.get(v, v)


def text_level(s):
    """Уровень строки не в формате JSON по словам ERROR, [warn] и подобным."""
    m = _LEVEL_WORD.search(s, 0, 200)
    if not m:
        return ""
    w = m.group(1)
    if w and w != w.upper():      # error как слово в тексте сообщения не уровень
        return ""
    return norm_level(w or m.group(2))


def shape(text):
    """Общий вид сообщения: «Document 18234 not found» и «Document 90211 not found» совпадают."""
    for rx, rep in _NORM:
        text = rx.sub(rep, text)
    return text


def masked(text, limit=LINE_LEN):
    text = M._mask(text)[0] if M._mask else "‹скрыто: рядом нет mask_secrets.py›"
    text = text.replace("\n", " ⏎ ")
    return text if len(text) <= limit else text[:limit] + "…"


class Filter(object):
    def __init__(self, a):
        self.a = a
        self.levels = {norm_level(x) for x in a.level.split(",") if x.strip()} if a.level else None
        self.grep = re.compile(a.grep, re.I | re.M) if a.grep else None
        self.json_only = bool(a.user or a.trace or a.logger or a.slow is not None)

    def ok(self, rec, when):
        a, obj = self.a, rec.obj
        if a.since is not None and (when is None or when < a.since):
            return False
        if a.until is not None and (when is None or when > a.until):
            return False
        if obj is None:
            if self.json_only:
                return False
            if self.levels is not None and text_level(rec.texts[0]) not in self.levels:
                return False
        else:
            if self.levels is not None and norm_level(obj.get("l") if isinstance(obj.get("l"), str) else "") not in self.levels:
                return False
            if a.user and a.user.lower() not in str(obj.get("un", "")).lower():
                return False
            if a.trace and not str(obj.get("tr", "")).startswith(a.trace):
                return False
            if a.logger and a.logger.lower() not in str(obj.get("lg", "")).lower():
                return False
            if a.slow is not None:
                span = obj.get("span")
                d = span.get("durationMs") if isinstance(span, dict) else None
                if not isinstance(d, (int, float)) or isinstance(d, bool) or d < a.slow:
                    return False
        if self.grep is not None and not any(self.grep.search(t) for t in rec.texts):
            return False
        return True


def group_key(rec):
    obj = rec.obj
    if obj is None:
        s = rec.texts[0]
        if rec.raw_when and rec.raw_when in s:
            s = s.split(rec.raw_when, 1)[1]
        return shape(s.strip())[:300]
    mt = obj.get("mt")
    mt = mt if isinstance(mt, str) else M._s(mt) if mt is not None else ""
    ex = obj.get("ex")
    ex_type = ex.get("type", "") if isinstance(ex, dict) else ""
    ex_m = ex.get("m", "") if isinstance(ex, dict) else (ex.split("\n", 1)[0] if isinstance(ex, str) else "")
    span = obj.get("span")
    if not mt and isinstance(span, dict) and span.get("name"):
        mt = "span " + M._s(span.get("name"))
    parts = [str(obj.get("l", "")), str(obj.get("lg", "")), shape(mt)]
    if ex_type or ex_m:
        parts.append("| %s: %s" % (ex_type, shape(M._s(ex_m))))
    return " ".join(p for p in parts if p)[:300]


def stack_of(obj):
    ex = obj.get("ex") if obj else None
    st = ex.get("stack") if isinstance(ex, dict) else None
    if not isinstance(st, str) or not st:
        return []
    lines = st.split("\n")
    out = lines[:STACK_LINES]
    if len(lines) > STACK_LINES:
        out.append("… ещё %d строк стека" % (len(lines) - STACK_LINES))
    return out


def run(a, out):
    flt = Filter(a)
    skipped, groups = [], {}
    shown = total = files = 0

    window = a.since is not None or a.until is not None
    # Подстроки, без которых запись точно не подходит: проверяем до разбора JSON.
    needles = [x for x in (a.trace, a.user, a.logger) if x]
    needles = [x.lower() for x in needles if all(ord(c) < 128 for c in x)]

    def handle(f, name):
        nonlocal shown, total
        last_start, start_when = None, False
        for n, line in enumerate(f, 1):
            if len(line) > M.MAX_LINE:
                line = line[:M.MAX_LINE]
            if not line.strip():
                continue
            start = line[:1] in M._START or line[:3] in M._MON
            if window and start:
                quick = M.line_time(line)[0]
                if quick is not None and ((a.since is not None and quick < a.since) or
                                          (a.until is not None and quick > a.until)):
                    last_start, start_when = line, False
                    continue
            if needles and "\\u" not in line:
                low = line.lower()
                if not all(x in low for x in needles):
                    if start:
                        last_start, start_when = line, False
                    continue
            rec = M.Record(line)
            when = rec.when
            if when is None and not start and last_start is not None:
                if start_when is False:
                    start_when = M.line_time(last_start)
                when = start_when[0]
            if start:
                last_start, start_when = line, False
            if not flt.ok(rec, when):
                continue
            total += 1
            if a.top:
                k = group_key(rec)
                g = groups.get(k)
                if g is None:
                    g = groups[k] = [0, when, when, rec.raw_when, rec.raw_when, "%s:%d" % (name, n)]
                g[0] += 1
                if when is not None:
                    if g[1] is None or when < g[1]:
                        g[1], g[3] = when, rec.raw_when
                    if g[2] is None or when > g[2]:
                        g[2], g[4] = when, rec.raw_when
                continue
            if shown >= a.max:
                continue
            shown += 1
            out.write("%s:%d: %s\n" % (name, n, masked(rec.display)))
            if a.stack:
                for s in stack_of(rec.obj):
                    out.write("    %s\n" % masked(s.rstrip(), 300))

    for path, name in M.iter_inputs(a.paths, skipped):
        if path == "-":
            handle(io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8-sig", errors="replace"), name)
            files += 1
            continue
        why = M.outside_since(path, a.since)
        if why:
            skipped.append((name, "вне окна: " + why))
            continue
        try:
            with M.open_text(path) as f:
                handle(f, name)
            files += 1
        except M.Skip as e:
            skipped.append((name, str(e)))
        except EOFError:
            files += 1
            skipped.append((name, "сжатый файл обрезан, прочитано до обрыва"))
        except (OSError, IOError) as e:
            skipped.append((name, "не читается: %s" % (e.strerror or e)))

    if a.top:
        items = sorted(groups.items(), key=lambda kv: (-kv[1][0], kv[0]))[:a.top]
        for k, g in items:
            when = (" с %s по %s" % (g[3], g[4])) if g[3] else ""
            out.write("×%s%s, пример %s\n    %s\n" % (M._num(g[0]), when, g[5], masked(k, 400)))
    out.write("Записей под фильтры: %s%s. Файлов: %d.\n" % (
        M._num(total), "" if a.top or shown == total else ", показано %s (--max)" % M._num(shown), files))
    for p, r in skipped[:10]:
        out.write("Пропущено: %s: %s\n" % (p, r))
    if len(skipped) > 10:
        out.write("Пропущено ещё %d\n" % (len(skipped) - 10))
    return files, total, skipped


def main(argv):
    M._utf8_stdout()
    ap = argparse.ArgumentParser(prog="log_filter.py", add_help=False, usage="%(prog)s [ПУТЬ ...] [фильтры]")
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--since", type=M.parse_bound)
    ap.add_argument("--until", type=M.parse_bound)
    ap.add_argument("--level")
    ap.add_argument("--user")
    ap.add_argument("--trace")
    ap.add_argument("--logger")
    ap.add_argument("--grep")
    ap.add_argument("--slow", type=float)
    ap.add_argument("--stack", action="store_true")
    ap.add_argument("--top", type=int, default=0)
    ap.add_argument("--max", type=int, default=200)
    ap.add_argument("-h", "--help", action="store_true")
    a = ap.parse_args(argv)
    if a.help:
        print(__doc__.strip())
        return 0
    if a.grep:
        try:
            re.compile(a.grep)
        except re.error as e:
            sys.stderr.write("log_filter: ошибка в --grep: %s\n" % e)
            return 2
    if not a.paths:
        if sys.stdin.isatty():
            sys.stderr.write("log_filter: укажите файлы или папки, или передайте журнал на ввод: КОМАНДА | log_filter.py -\n")
            return 2
        a.paths = ["-"]
    files, total, skipped = run(a, sys.stdout)
    if files == 0 and not (skipped and all(r.startswith("вне окна") for _, r in skipped)):
        sys.stderr.write("log_filter: ни один файл не прочитан\n")
        return 2
    return 0 if total else 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(2)
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
        sys.exit(0)
