#!/usr/bin/env python3
"""Маскирует пароли, токены и ключи в тексте конфигов и выводе команд.

Использование:
  python3 mask_secrets.py ФАЙЛ [ФАЙЛ...]   печатает файлы с масками
  команда 2>&1 | python3 mask_secrets.py    маскирует вывод команды
  python3 mask_secrets.py --check ФАЙЛ      код 1, если в файле есть секреты

Значение заменяется на ‹скрыто›, имя параметра остаётся: видно, что пароль
задан, но не видно какой. Только стандартная библиотека, Python 3.6+.
Канонический файл лежит в shared/, копии в скилах и хуках делает tools/sync_shared.py.
"""
import re
import sys

MASK = "‹скрыто›"

# Имена параметров, значения которых считаем секретами.
KEY = (
    r"[\w.\-]*(?:pass(?:word|wd|phrase)?|pwd|secret|token|api[_\-]?key|access[_\-]?key|"
    r"private[_\-]?key|client[_\-]?secret|credentials?|cookie|session[_\-]?id|license[_\-]?key|"
    r"secret[_\-]?key|signing[_\-]?key|encryption[_\-]?key|auth[_\-]?key)[\w.\-]*"
)
# Слова в имени параметра, при которых значение не секрет: путь, срок, длина, ссылка на секрет.
NOT_SECRET_WORDS = {
    "path", "file", "files", "dir", "directory", "folder", "url", "uri", "endpoint", "hint", "policy",
    "length", "len", "expiry", "expires", "expire", "expiration", "timeout", "ttl", "type", "mode",
    "enable", "enabled", "required", "min", "max", "count", "age", "history", "lifetime", "rotation",
    "header", "field", "claim", "tokenizer", "usage", "limit", "per", "name", "existing", "ref",
}

# Значения, которые не надо прятать: пусто, ссылки на переменные, шаблоны, заглушки.
PLACEHOLDER = re.compile(
    r"^(?:|null|none|nil|~|true|false|yes|no|\*+|‹скрыто›|<[^>]*>|\$\{[^}]*\}|\$[A-Za-z_]\w*|"
    r"\{\{.*\}\}|%[^%]+%|env\([^)]*\)|secret\[[^\]]*\]|change_?me|changeme|xxx+|\.\.\.)$", re.I)

RULES = []


def rule(pattern, flags=0):
    def deco(fn):
        RULES.append((re.compile(pattern, flags), fn))
        return fn
    return deco


def _value(v):
    """Значение без кавычек и хвостовых запятых, чтобы проверить на заглушку."""
    s = v.strip().rstrip(",;").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        s = s[1:-1]
    return s


def _secret_key(k):
    """True, если имя параметра похоже на секрет и в нём нет слов вроде path, ttl, min."""
    words = re.findall(r"[a-z]+", re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", k).lower())
    return not any(w in NOT_SECRET_WORDS for w in words)


# 1. PEM-ключи целиком.
@rule(r"-----BEGIN ([A-Z0-9 ]*PRIVATE KEY)-----.*?-----END \1-----", re.S)
def _pem(m):
    return "-----BEGIN %s-----\n%s\n-----END %s-----" % (m.group(1), MASK, m.group(1))


# 2. Учётка в адресе: scheme://user:password@host
@rule(r"(\b[a-z][a-z0-9+.\-]*://[^\s/:@'\"]+:)([^\s@/'\"]+)(@)", re.I)
def _url(m):
    if PLACEHOLDER.match(m.group(2)):
        return m.group(0)
    return m.group(1) + MASK + m.group(3)


# 3. Строки подключения: Password=...; Pwd=...
@rule(r"(\b(?:password|pwd|user ?password)\s*=\s*)(\"[^\"]*\"|'[^']*'|[^;\"'\s<>]+)", re.I)
def _conn(m):
    if PLACEHOLDER.match(_value(m.group(2))):
        return m.group(0)
    return m.group(1) + MASK


# 4. Заголовки авторизации и curl -u user:pass
@rule(r"(\bAuthorization\s*[:=]\s*[\"']?(?:Basic|Bearer|Negotiate|ApiKey|Token)\s+)([A-Za-z0-9+/=._~\-]+)", re.I)
def _auth(m):
    return m.group(1) + MASK


@rule(r"((?:^|\s)(?:-u|--user)\s+[\"']?[^:\s\"']+:)([^\s\"']+)")
def _curl_user(m):
    if PLACEHOLDER.match(m.group(2)):
        return m.group(0)
    return m.group(1) + MASK


# 5. JSON: "password": "..."
@rule(r"(\"(" + KEY + r")\"\s*:\s*)(\"(?:[^\"\\]|\\.)*\")", re.I)
def _json(m):
    if not _secret_key(m.group(2)) or PLACEHOLDER.match(_value(m.group(3))):
        return m.group(0)
    return m.group(1) + '"' + MASK + '"'


# 6. XML: <var name="DATABASE_PASSWORD" value="..."/> и <add key="..." value="..."/>
@rule(r"(<[^>]*\b(?:name|key)\s*=\s*\"(" + KEY + r")\"[^>]*?\bvalue\s*=\s*\")([^\"]*)(\")", re.I)
def _xml_attr(m):
    if not _secret_key(m.group(2)) or PLACEHOLDER.match(m.group(3)):
        return m.group(0)
    return m.group(1) + MASK + m.group(4)


# 7. XML-элемент: <Password>...</Password>
@rule(r"(<(" + KEY + r")(?:\s[^>]*)?>)([^<]+)(</\2>)", re.I)
def _xml_el(m):
    if not _secret_key(m.group(2)) or PLACEHOLDER.match(m.group(3).strip()):
        return m.group(0)
    return m.group(1) + MASK + m.group(4)


# 8. YAML, INI, .env, properties, аргументы: KEY: value, KEY=value, --password value
# Перед именем допускается префикс вывода grep («файл:12:») или cat -n и Read («12→», «  12\t»).
@rule(r"(^[ \t]*(?:[^\s:]+:\d+[:\-]|\d+(?:[:\t\-]|→))?[ \t]*(?:export[ \t]+|-[ \t]+)?[\"']?(" + KEY + r")[\"']?[ \t]*[:=][ \t]*)([^\n#]*?)([ \t]+#.*)?$", re.I | re.M)
def _kv(m):
    v = m.group(3)
    if not _secret_key(m.group(2)) or PLACEHOLDER.match(_value(v)) or v.strip() in ("|", ">", "|-", ">-"):
        return m.group(0)
    if v.strip().startswith(("{", "[")) and not v.strip().startswith(("{{", "[[")):
        return m.group(0)  # вложенная структура: значения разберут другие правила
    s = v.strip()
    if m.group(1).rstrip().endswith("=") and s[:1] not in ("'", '"') and len(s.split()) > 1:
        # Присваивание перед командой: PGPASSWORD=xxx pg_dump rx. Прячем только значение.
        first, rest = s.split(None, 1)
        if PLACEHOLDER.match(first):
            return m.group(0)
        return m.group(1) + MASK + " " + rest + (m.group(4) or "")
    return m.group(1) + MASK + (m.group(4) or "")


@rule(r"((?:^|\s)--?(" + KEY + r")(?:=|\s+))(\"[^\"]*\"|'[^']*'|[^\s\"']+)", re.I)
def _flag(m):
    if not _secret_key(m.group(2)) or PLACEHOLDER.match(_value(m.group(3))) or m.group(3).startswith("-"):
        return m.group(0)
    return m.group(1) + MASK


# 8а. Присваивание внутри команды: cd /x && PGPASSWORD=... pg_dump, env AWS_SECRET_ACCESS_KEY=...
@rule(r"((?:^|[\s;&|(`])(?:export\s+)?(" + KEY + r")=)(\"[^\"]*\"|'[^']*'|[^\s;&|\"'`]+)", re.I)
def _inline(m):
    if not _secret_key(m.group(2)) or PLACEHOLDER.match(_value(m.group(3))):
        return m.group(0)
    return m.group(1) + MASK


# 8б. SQL: CREATE/ALTER ROLE ... PASSWORD '...'
@rule(r"(\bPASSWORD\s+)('(?:[^']|'')*')", re.I)
def _sql_pw(m):
    return m.group(1) + "'" + MASK + "'"


# 8в. mc alias set ИМЯ АДРЕС КЛЮЧ СЕКРЕТ
@rule(r"(\bmc\s+alias\s+set\s+\S+\s+\S+\s+)(\S+)(\s+)(\S+)")
def _mc_alias(m):
    return m.group(1) + MASK + m.group(3) + MASK


# 9. Известные форматы токенов и JWT в любом месте текста.
TOKENS = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|glpat-[A-Za-z0-9_\-]{20,}|"
    r"xox[abprs]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9_\-]{20,}|"
    r"eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,})")


@rule(TOKENS.pattern)
def _token(m):
    return MASK


def _k8s_secret_data(text):
    """В манифесте kind: Secret прячет все значения под data: и stringData:."""
    if not re.search(r"^\s*kind:\s*Secret\s*$", text, re.M):
        return text, 0
    out, n, block = [], 0, None
    for line in text.split("\n"):
        m = re.match(r"^(\s*)(data|stringData):\s*$", line)
        if m:
            block = len(m.group(1))
            out.append(line)
            continue
        if block is not None:
            kv = re.match(r"^(\s*)([^\s:#][^:]*):\s*(.*)$", line)
            if kv and len(kv.group(1)) > block:
                if kv.group(3) and kv.group(3) != MASK:
                    line = "%s%s: %s" % (kv.group(1), kv.group(2), MASK)
                    n += 1
            elif line.strip() and len(line) - len(line.lstrip()) <= block:
                block = None
        out.append(line)
    return "\n".join(out), n


def mask(text):
    """Возвращает (текст с масками, сколько значений спрятано)."""
    total = 0
    text, n = _k8s_secret_data(text)
    total += n
    for rx, fn in RULES:
        counter = [0]

        def sub(m, fn=fn, counter=counter):
            r = fn(m)
            if r != m.group(0):
                counter[0] += 1
            return r
        text = rx.sub(sub, text)
        total += counter[0]
    return text, total


def _write(stream, text):
    """Пишет UTF-8 байтами: в Python 3.6 при LANG=C текстовый поток ASCII и падает на кириллице."""
    buf = getattr(stream, "buffer", None)
    if buf is not None:
        buf.write(text.encode("utf-8"))
        buf.flush()
    else:
        stream.write(text)


def main(argv):
    check = "--check" in argv
    files = [a for a in argv if a != "--check"]
    if any(a in ("-h", "--help") for a in files):
        _write(sys.stdout, __doc__.strip() + "\n")
        return 0
    found = 0
    sources = files or ["-"]
    for name in sources:
        if name == "-":
            data = sys.stdin.buffer.read()
        else:
            with open(name, "rb") as f:
                data = f.read()
        text = data.decode("utf-8", "replace")
        masked, n = mask(text)
        found += n
        if not check:
            if len(sources) > 1:
                _write(sys.stdout, "==> %s <==\n" % name)
            if masked and not masked.endswith("\n"):
                masked += "\n"
            _write(sys.stdout, masked)
    if found:
        _write(sys.stderr, "mask_secrets: скрыто значений: %d\n" % found)
    return 1 if (check and found) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
