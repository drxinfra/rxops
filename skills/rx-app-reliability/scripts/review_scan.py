#!/usr/bin/env python3
"""Ищет в исходниках решения Directum RX (Sungero, C#) рискованные места для быстродействия и надёжности.

Использование:
  python3 review_scan.py ПАПКА_ИЛИ_ФАЙЛ [ПАПКА ...] [--json] [--rule ID[,ID]] [--max N] [--no-snippet]
  python3 review_scan.py --list-rules

Что делает: читает файлы *.cs (кроме bin/, obj/, .git/ и *.g.cs), убирает из текста комментарии
и строковые литералы и ищет шаблоны: SQL.CreateConnection() без using, запросы и Remote-вызовы
в цикле, GetAll() без фильтра, фильтрацию после ToList(), Thread.Sleep, блокировки без снятия,
Locks.Lock в обработчиках, безусловный ExecuteAsync при сохранении, вложенные транзакции.
Отдельно считает объявления индексов (CREATE INDEX, CreateIndexOnTable) во всех текстовых файлах.

Это эвристика, а не компилятор: каждая находка требует взгляда человека. Ложные срабатывания
бывают (маленький справочник в цикле, намеренный GetAll() по таблице на десять строк),
пропуски тоже (запрос в функции, вызванной из цикла, скрипт не видит).

Ключи:
  --json         вывод одним JSON-объектом
  --rule СПИСОК  только эти правила (через запятую), список: --list-rules
  --max N        не больше N находок на правило (по умолчанию 30), остальные только посчитать
  --no-snippet   не показывать строку кода под находкой

Строки кода показываются через mask_secrets.py. Ничего не меняет и не запускает.
Коды возврата: 0 нет находок уровня WARN и CRIT, 1 есть, 2 не удалось проверить.
Только стандартная библиотека, Python 3.6+.
"""
import bisect
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
try:
    import mask_secrets as _ms  # копия shared/mask_secrets.py рядом со скриптом
except ImportError:  # pragma: no cover
    _ms = None

VERSION = "0.1.0"
SKIP_DIRS = {"bin", "obj", ".git", ".vs", "node_modules", "packages"}
TEXT_EXT = (".cs", ".sql", ".xml", ".mtd", ".resx", ".json")
RANK = {"INFO": 1, "WARN": 2, "CRIT": 3}

RULES = {
    "sql-connection-no-using": ("CRIT", "SQL.CreateConnection() без using: соединение не закроется при исключении, "
                                        "соединения копятся до too many clients; сборка RX 4.9+ такое не пропускает"),
    "query-in-loop": ("WARN", "запрос к базе на каждой итерации цикла (N+1): выберите всё одним запросом до цикла "
                              "(Where по списку Id, Select нужных свойств)"),
    "remote-in-loop": ("WARN", "Remote-вызов в цикле: на каждую итерацию отдельный запрос к веб-серверу; "
                               "объедините в одну серверную функцию"),
    "getall-unfiltered": ("WARN", "GetAll() без фильтра материализуется целиком: на большой таблице это память "
                                  "сервиса и долгий запрос; добавьте Where/Take или обрабатывайте порциями"),
    "tolist-then-filter": ("WARN", "фильтр после ToList(): из базы читается всё, отбор идёт в памяти; "
                                   "перенесите Where/Any/Count до ToList()"),
    "thread-sleep": ("WARN", "Thread.Sleep или Task.Delay().Wait() держит поток сервиса; в асинхронном обработчике "
                             "вместо ожидания ставят args.Retry и NextRetryTime"),
    "lock-no-unlock": ("WARN", "блокировка ставится, но в этой функции не снимается в finally: при исключении "
                               "запись останется заблокированной"),
    "lock-hard-in-handler": ("WARN", "Locks.Lock() в асинхронном обработчике или фоновом процессе бросает исключение, "
                                     "если запись занята; проверяйте GetLockInfo/TryLock и уходите на повтор"),
    "async-no-retry": ("INFO", "асинхронный обработчик не трогает args.Retry/RetryReason: при занятой записи он "
                               "упадёт исключением и уйдёт в системный повтор без понятной причины в логе"),
    "executeasync-on-save": ("WARN", "ExecuteAsync() в событии сохранения без условия: обработчик запускается на "
                                     "каждое сохранение, очередь RabbitMQ разрастается"),
    "nested-transaction": ("WARN", "Transactions.Execute() там, где платформа уже открыла транзакцию (Saving, Saved, "
                                   "Remote-функции, инициализация): вложенные транзакции запрещены"),
    "sql-concat": ("INFO", "текст SQL собирается сложением или интерполяцией: значения передавайте через "
                           "SQL.AddParameter, иначе риск инъекции и нового плана на каждое значение"),
    "no-index-declared": ("INFO", "в решении нет ни одного объявления индекса, хотя есть выборки с фильтром: "
                                  "проверьте планы частых запросов и списков"),
}

LOOP_KW = re.compile(r"\b(foreach|for|while)\s*\(")
DO_KW = re.compile(r"\bdo\s*\{")
CONTROL = re.compile(r"^(if|else|switch|case|default|try|catch|finally|using|lock|fixed|checked|unchecked|"
                     r"unsafe|get|set|init|add|remove|do|for|foreach|while|return|new|namespace|class|struct|"
                     r"interface|enum|record)\b")
COND = re.compile(r"^(if|else|switch|case|default)\b")
METHOD = re.compile(r"^(?:\[[^\]]*\]\s*)*(?:(?:public|private|protected|internal|static|virtual|override|"
                    r"abstract|async|sealed|new|partial|extern)\s+)+[\w<>\[\],.?\s]*?\b(\w+)\s*\(")
SAVE_EVENTS = {"BeforeSave", "Saving", "Saved", "AfterSave"}
TX_EVENTS = {"Saving", "Saved"}
FILTERS = re.compile(r"\.\s*(Where|Take|Skip|Any|First|FirstOrDefault|Single|SingleOrDefault|Count|LongCount|Max|"
                     r"Min|Sum)\s*\(")
MATERIALIZE = re.compile(r"\.\s*(ToList|ToArray|ToDictionary|ToHashSet|ToLookup)\s*\(")


def _utf8_out(text):
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.flush()


def mask(text):
    if _ms is None:
        return re.sub(r"(?i)(password|pwd|secret|token)\s*=\s*\S+", r"\1=‹скрыто›", text)
    return _ms.mask(text)[0]


def strip_code(src):
    """Комментарии и содержимое строк заменяются пробелами; длина и переводы строк сохраняются."""
    out, i, n = [], 0, len(src)

    def blank(s):
        return "".join("\n" if c == "\n" else " " for c in s)

    while i < n:
        c = src[i]
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            out.append(blank(src[i:j]))
            i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(blank(src[i:j]))
            i = j
        elif c == '"' or (c in "@$" and i + 1 < n and src[i + 1] in '"@$'):
            k = i
            while k < n and src[k] in "@$":
                k += 1
            if k >= n or src[k] != '"':
                out.append(c)
                i += 1
                continue
            verbatim = "@" in src[i:k]
            out.append(src[i:k] + '"')
            j = k + 1
            while j < n:
                if verbatim and src[j] == '"' and j + 1 < n and src[j + 1] == '"':
                    j += 2
                    continue
                if not verbatim and src[j] == "\\":
                    j += 2
                    continue
                if src[j] == '"':
                    break
                if not verbatim and src[j] == "\n":
                    break
                j += 1
            out.append(blank(src[k + 1:j]))
            if j < n and src[j] == '"':
                out.append('"')
                j += 1
            i = j
        elif c == "'":
            m = re.match(r"'(\\.[^']*|[^'\\\n])'", src[i:i + 12])
            if m:
                out.append("'" + " " * (len(m.group(0)) - 2) + "'")
                i += len(m.group(0))
            else:
                out.append(c)
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _match_paren(code, i):
    """Индекс за закрывающей скобкой для '(' в позиции i."""
    depth = 0
    for j in range(i, len(code)):
        if code[j] == "(":
            depth += 1
        elif code[j] == ")":
            depth -= 1
            if depth == 0:
                return j + 1
    return len(code)


def structure(code):
    """Снимки состояния: (смещение, стек блоков, открытых однострочных циклов) и список функций."""
    snaps = [(0, (), 0)]
    methods = []          # (имя, заголовок, начало, конец)
    stack = []            # элементы: (вид, имя, заголовок, начало)
    singles = []          # глубина стека, на которой ждём ';' однострочного цикла
    loop_heads = {}       # позиция '{' или первого символа тела -> True
    pending_single = set()
    for m in LOOP_KW.finditer(code):
        if m.group(1) == "while":
            before = code[:m.start()].rstrip()
            if before.endswith("}") and _is_do_tail(code, m.start()):
                continue
        end = _match_paren(code, m.end() - 1)
        k = end
        while k < len(code) and code[k] in " \t\r\n":
            k += 1
        if k < len(code) and code[k] == "{":
            loop_heads[k] = True
        elif k < len(code) and code[k] != ";":
            pending_single.add(k)
    for m in DO_KW.finditer(code):
        loop_heads[m.end() - 1] = True
    last = 0
    i, n = 0, len(code)
    while i < n:
        if i in pending_single:
            singles.append(len(stack))
            snaps.append((i, tuple(stack), len(singles)))
        c = code[i]
        if c == "{":
            header = "\n".join(l for l in code[last:i].split("\n") if not l.strip().startswith("#")).strip()
            header = re.sub(r"\s+", " ", header)
            if i in loop_heads:
                entry = ("loop", "", header, i)
            elif header.endswith("=>") or header.endswith("delegate") or re.search(r"=>\s*$", header):
                entry = ("lambda", "", header, i)
            elif COND.match(header):
                entry = ("cond", "", header, i)
            else:
                mm = METHOD.match(header) if "(" in header else None
                if mm and not CONTROL.match(header.split("]")[-1].strip()):
                    entry = ("method", mm.group(1), header, i)
                else:
                    entry = ("block", "", header, i)
            stack.append(entry)
            snaps.append((i, tuple(stack), len(singles)))
            last = i + 1
        elif c == "}":
            if stack:
                e = stack.pop()
                if e[0] == "method":
                    methods.append((e[1], e[2], e[3], i))
            while singles and singles[-1] > len(stack):
                singles.pop()
            if singles and singles[-1] == len(stack) and _ends_statement(code, i):
                singles.pop()
            snaps.append((i + 1, tuple(stack), len(singles)))
            last = i + 1
        elif c == ";":
            if singles and singles[-1] == len(stack):
                while singles and singles[-1] == len(stack):
                    singles.pop()
                snaps.append((i + 1, tuple(stack), len(singles)))
            last = i + 1
        i += 1
    return snaps, methods


def _is_do_tail(code, pos):
    """while после '}' блока do: ищем 'do' перед парной '{'."""
    j = code[:pos].rstrip()
    if not j.endswith("}"):
        return False
    depth, k = 0, len(j) - 1
    while k >= 0:
        if j[k] == "}":
            depth += 1
        elif j[k] == "{":
            depth -= 1
            if depth == 0:
                return j[:k].rstrip().endswith("do")
        k -= 1
    return False


def _ends_statement(code, i):
    """'}' закрывает тело однострочного цикла, если за ним не идёт продолжение выражения."""
    rest = code[i + 1:i + 40].lstrip()
    return not rest.startswith((")", ",", ".", ";"))


class Source(object):
    def __init__(self, path, rel, raw):
        self.path, self.rel, self.raw = path, rel, raw
        self.code = strip_code(raw)
        self.lines = raw.split("\n")
        self.starts = [0]
        for ln in self.lines[:-1]:
            self.starts.append(self.starts[-1] + len(ln) + 1)
        self.snaps, self.methods = structure(self.code)
        self.snap_pos = [s[0] for s in self.snaps]
        low = rel.replace("\\", "/").lower()
        self.client = ".clientbase/" in low or ".client/" in low or "clientfunctions" in low
        self.init = os.path.basename(low).endswith("initializer.cs")

    def line_of(self, off):
        return bisect.bisect_right(self.starts, off)

    def state(self, off):
        k = bisect.bisect_right(self.snap_pos, off) - 1
        return self.snaps[max(k, 0)]

    def in_loop(self, off):
        _, stack, singles = self.state(off)
        return singles > 0 or any(e[0] == "loop" for e in stack)

    def method_at(self, off):
        _, stack, _ = self.state(off)
        for e in reversed(stack):
            if e[0] == "method":
                return e
        return None

    def cond_inside_method(self, off):
        _, stack, _ = self.state(off)
        inside = False
        for e in stack:
            if e[0] == "method":
                inside = True
            elif inside and e[0] in ("cond", "loop"):
                return True
        return False

    def statement(self, off):
        """Текст оператора от off до ближайшей ';' или '{' (без строк и комментариев)."""
        m = re.search(r"[;{]", self.code[off:])
        return self.code[off:off + (m.start() if m else 400)]


def handler_kind(src, method):
    """'async', 'job' или ''."""
    if method is None:
        return ""
    if "AsyncHandlerInvokeArgs" in method[2]:
        return "async"
    if os.path.basename(src.rel).lower().endswith("jobs.cs"):
        return "job"
    return ""


def scan_source(src):
    found = []
    code = src.code

    def add(rule, off, extra=""):
        found.append((rule, src.rel, src.line_of(off), extra))

    for m in re.finditer(r"\bSQL\s*\.\s*CreateConnection\s*\(", code):
        head = code[max(0, m.start() - 160):m.start()]
        if not re.search(r"using\s*\(\s*(?:[\w.<>]+\s+\w+\s*=\s*)?$", head) and \
                not re.search(r"using\s+(?:var|[\w.]+)\s+\w+\s*=\s*$", head):
            add("sql-connection-no-using", m.start())

    seen = set()
    for m in re.finditer(r"\.\s*(GetAll|Get)\s*\(|\bSQL\s*\.\s*CreateConnection\s*\(|"
                         r"\.\s*Execute(Reader|Scalar|NonQuery)\s*\(", code):
        if src.in_loop(m.start()):
            ln = src.line_of(m.start())
            if ("query-in-loop", ln) not in seen:
                seen.add(("query-in-loop", ln))
                add("query-in-loop", m.start())
    for m in re.finditer(r"\.\s*ToList\s*\(\s*\)", code):
        line_text = src.code[src.starts[src.line_of(m.start()) - 1]:m.start()]
        if src.in_loop(m.start()) and re.search(r"\.\s*Where\s*\(", line_text):
            ln = src.line_of(m.start())
            if ("query-in-loop", ln) not in seen:
                seen.add(("query-in-loop", ln))
                add("query-in-loop", m.start())

    for m in re.finditer(r"\.\s*Remote\s*\.\s*\w+\s*\(", code):
        if src.in_loop(m.start()):
            add("remote-in-loop", m.start(), "клиентский код" if src.client else "")

    for m in re.finditer(r"\.\s*GetAll\s*\(\s*\)", code):
        rest = src.statement(m.end())
        head = code[max(0, m.start() - 200):m.start()]
        in_foreach = re.search(r"\bforeach\s*\([^;{]*\bin\s+[\w.]*$", head) is not None
        if in_foreach and not FILTERS.search(rest.split(")")[0] if rest else ""):
            add("getall-unfiltered", m.start())
        elif MATERIALIZE.search(rest) and not FILTERS.search(rest[:MATERIALIZE.search(rest).start()]):
            add("getall-unfiltered", m.start())

    for m in re.finditer(r"\.\s*ToList\s*\(\s*\)\s*\.\s*(Where|Any|Count|First|FirstOrDefault|Single|"
                         r"SingleOrDefault|OrderBy|OrderByDescending)\s*\(", code):
        add("tolist-then-filter", m.start())

    for m in re.finditer(r"\bThread\s*\.\s*Sleep\s*\(|\bTask\s*\.\s*Delay\s*\([^;]*?\)\s*\.\s*(Wait|GetAwaiter)\b",
                         code):
        kind = handler_kind(src, src.method_at(m.start()))
        add("thread-sleep", m.start(), {"async": "в асинхронном обработчике", "job": "в фоновом процессе"}.get(kind, ""))

    for name, header, start, end in src.methods:
        body = code[start:end]
        locks = list(re.finditer(r"\bLocks\s*\.\s*(Lock|TryLock)\s*\(", body))
        if locks:
            has_unlock = re.search(r"\bLocks\s*\.\s*Unlock\s*\(", body)
            has_finally = re.search(r"\bfinally\b", body)
            if not (has_unlock and has_finally):
                add("lock-no-unlock", start + locks[0].start(),
                    "нет Locks.Unlock" if not has_unlock else "Unlock не в finally")
        kind = handler_kind(src, ("method", name, header, start))
        if kind:
            for lm in re.finditer(r"\bLocks\s*\.\s*Lock\s*\(", body):
                add("lock-hard-in-handler", start + lm.start(),
                    "асинхронный обработчик" if kind == "async" else "фоновый процесс")
        if kind == "async" and not re.search(r"\bargs\s*\.\s*(Retry|RetryReason|NextRetryTime)\b", body):
            add("async-no-retry", max(code.rfind(name, 0, start), 0), name)

    for m in re.finditer(r"\.\s*ExecuteAsync\s*\(", code):
        meth = src.method_at(m.start())
        if meth and meth[1] in SAVE_EVENTS and not src.cond_inside_method(m.start()):
            add("executeasync-on-save", m.start(), meth[1])

    for m in re.finditer(r"\bTransactions\s*\.\s*Execute\s*\(", code):
        meth = src.method_at(m.start())
        why = ""
        if src.init:
            why = "инициализация модуля"
        elif meth and meth[1] in TX_EVENTS:
            why = "событие " + meth[1]
        elif meth and re.search(r"\[\s*(?:[\w.]+\.)?Remote\b", meth[2]):
            why = "Remote-функция"
        if why:
            add("nested-transaction", m.start(), why)

    for m in re.finditer(r"\bCommandText\s*(?:=|\+=)", code):
        stmt = src.statement(m.end())
        if re.search(r'\$\s*"|@\$|\$@', stmt) or re.search(r'"\s*\+|\+\s*"', stmt) or "+=" in m.group(0):
            add("sql-concat", m.start())
    return found


def walk(paths):
    """Пары (полный путь, путь для вывода) для текстовых файлов."""
    for p in paths:
        if os.path.isfile(p):
            yield p, os.path.basename(p)
            continue
        for root, dirs, files in os.walk(p):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for f in sorted(files):
                if f.lower().endswith(TEXT_EXT):
                    full = os.path.join(root, f)
                    yield full, os.path.relpath(full, p)


def read(path):
    with open(path, "rb") as fh:
        data = fh.read()
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def scan(paths, rules=None):
    found, stats = [], {"cs_files": 0, "index_declarations": 0, "filtered_queries": 0, "errors": []}
    sources = {}
    for full, rel in walk(paths):
        try:
            raw = read(full)
        except OSError as e:
            stats["errors"].append("%s: %s" % (rel, e))
            continue
        stats["index_declarations"] += len(re.findall(
            r"(?i)\bcreate\s+(?:unique\s+)?(?:nonclustered\s+|clustered\s+)?index\b|\bCreateIndex\w*\s*\(", raw))
        if not full.lower().endswith(".cs") or full.lower().endswith(".g.cs"):
            continue
        stats["cs_files"] += 1
        src = Source(full, rel, raw)
        sources[rel] = src
        stats["filtered_queries"] += len(re.findall(r"\.\s*GetAll\s*\(\s*\w+\s*=>|\.\s*GetAll\s*\([^)]*\)\s*\.\s*Where\s*\(",
                                                    src.code))
        found.extend(scan_source(src))
    if stats["cs_files"] and stats["index_declarations"] == 0 and stats["filtered_queries"] > 0:
        found.append(("no-index-declared", "(решение)", 0, "выборок с фильтром: %d" % stats["filtered_queries"]))
    if rules:
        found = [f for f in found if f[0] in rules]
    found.sort(key=lambda f: (-RANK[RULES[f[0]][0]], f[0], f[1], f[2]))
    return found, stats, sources


def snippet(sources, rel, line):
    src = sources.get(rel)
    if not src or line < 1 or line > len(src.lines):
        return ""
    return mask(src.lines[line - 1].strip())[:200]


def main(argv):
    if any(a in ("-h", "--help") for a in argv):
        _utf8_out(__doc__.strip() + "\n")
        return 0
    if "--list-rules" in argv:
        _utf8_out("".join("%-24s %-4s %s\n" % (k, v[0], v[1]) for k, v in RULES.items()))
        return 0
    paths, as_json, rules, limit, show = [], False, None, 30, True
    it = iter(argv)
    for a in it:
        if a == "--json":
            as_json = True
        elif a == "--no-snippet":
            show = False
        elif a == "--rule":
            rules = set(x.strip() for x in next(it, "").split(",") if x.strip())
        elif a == "--max":
            try:
                limit = int(next(it, "30"))
            except ValueError:
                _utf8_out("--max: нужно число\n")
                return 2
        elif a.startswith("--"):
            _utf8_out("неизвестный ключ %s, см. --help\n" % a)
            return 2
        else:
            paths.append(a)
    if not paths:
        _utf8_out("укажите папку с исходниками решения, см. --help\n")
        return 2
    bad = [p for p in paths if not os.path.exists(p)]
    if bad:
        _utf8_out("нет такого пути: %s\n" % ", ".join(bad))
        return 2
    if rules and rules - set(RULES):
        _utf8_out("неизвестные правила: %s, см. --list-rules\n" % ", ".join(sorted(rules - set(RULES))))
        return 2
    found, stats, sources = scan(paths, rules)
    if not stats["cs_files"]:
        _utf8_out("файлов *.cs не найдено: %s\n" % ", ".join(paths))
        return 2
    counts = {"CRIT": 0, "WARN": 0, "INFO": 0}
    for f in found:
        counts[RULES[f[0]][0]] += 1
    code = 1 if counts["CRIT"] or counts["WARN"] else 0
    if as_json:
        items = [{"rule": r, "level": RULES[r][0], "file": fl, "line": ln, "note": ex, "why": RULES[r][1],
                  "code": snippet(sources, fl, ln)} for r, fl, ln, ex in found]
        _utf8_out(json.dumps({"version": VERSION, "counts": counts, "cs_files": stats["cs_files"],
                              "index_declarations": stats["index_declarations"], "findings": items,
                              "errors": stats["errors"]}, ensure_ascii=False, indent=1) + "\n")
        return code
    out, per_rule, hidden = [], {}, 0
    for r, fl, ln, ex in found:
        per_rule[r] = per_rule.get(r, 0) + 1
        if per_rule[r] > limit:
            hidden += 1
            continue
        where = "%s:%d" % (fl, ln) if ln else fl
        out.append("%-4s %s  %s%s\n     %s\n" % (RULES[r][0], where, r, " (%s)" % ex if ex else "", RULES[r][1]))
        if show and ln:
            code_line = snippet(sources, fl, ln)
            if code_line:
                out.append("     > %s\n" % code_line)
    out.append("\nФайлов C#: %d, объявлений индексов: %d. Находок: CRIT %d, WARN %d, INFO %d%s.\n" % (
        stats["cs_files"], stats["index_declarations"], counts["CRIT"], counts["WARN"], counts["INFO"],
        ", не показано сверх --max: %d" % hidden if hidden else ""))
    for e in stats["errors"]:
        out.append("не прочитан: %s\n" % e)
    out.append("Это эвристика: каждую находку проверьте глазами, подтверждение даёт замер на тестовом контуре.\n")
    _utf8_out("".join(out))
    return code


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(2)
