#!/usr/bin/env python3
"""Собирает базу известных ошибок для скила rx-logs из исходников сайта drxinfra.ru.

Источник: errors/posts/*.md репозитория сайта (фронт-маттер title, what, component,
match и разделы «Как проверить», «Что сделать»). Результат:
skills/rx-logs/scripts/known_errors.json, его читает match_errors.py.

  python3 tools/sync_errors.py                 сайт лежит в ../landing рядом с репозиторием
  python3 tools/sync_errors.py ПУТЬ_К_САЙТУ    другой путь
  python3 tools/sync_errors.py --check         код 1, если база устарела

Страницы ошибок в репозиторий не копируются: в базе только шаблоны, короткие
пояснения и шаги проверки, а полный разбор по ссылке на сайте. Вывод
детерминированный: записи по алфавиту, без дат сборки.
"""
import io
import json
import os
import re
import sys
import warnings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "skills", "rx-logs", "scripts", "known_errors.json")
SITE = "https://drxinfra.ru/"
ERRORS_URL = SITE + "errors/"
REQUIRED = ("title", "what", "component", "match")
SECTIONS = {"Как проверить": "check", "Что сделать": "fix"}


def parse_post(path):
    """Фронт-маттер и тело, как их читает build.py сайта."""
    with io.open(path, encoding="utf-8") as f:
        text = f.read().replace("\r\n", "\n")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
    if not m:
        raise ValueError("нет фронт-маттера ---")
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip().lower()] = v.strip()
    for k in REQUIRED:
        if not meta.get(k):
            raise ValueError("во фронт-маттере нет поля " + k)
    return meta, m.group(2)


def sections(body):
    """Разделы второго уровня: {заголовок: текст}."""
    out, name, buf = {}, None, []
    for line in body.split("\n"):
        h = re.match(r"^##\s+(.+?)\s*$", line)
        if h and not line.startswith("###"):
            if name is not None:
                out[name] = "\n".join(buf)
            name, buf = h.group(1), []
        elif name is not None:
            buf.append(line)
    if name is not None:
        out[name] = "\n".join(buf)
    return out


def absolute(url):
    """Относительная ссылка страницы ошибки в полный адрес сайта."""
    url = url.strip()
    if re.match(r"^[a-z][a-z0-9+.\-]*:", url, re.I):
        return url
    if url.startswith("#"):
        return ""
    base = ERRORS_URL
    while url.startswith("../"):
        url = url[3:]
        base = SITE
    if url.startswith("./"):
        url = url[2:]
    if url.startswith("/"):
        return SITE + url.lstrip("/")
    return base + url


def plain(md):
    """Markdown раздела в простой текст: ссылки «текст (адрес)», код с отступом, без жирного."""
    lines, out, code = md.strip("\n").split("\n"), [], False
    for line in lines:
        if line.strip().startswith("```"):
            code = not code
            continue
        if code:
            out.append("    " + line)
            continue

        def link(m):
            url = absolute(m.group(2))
            return "%s (%s)" % (m.group(1), url) if url else m.group(1)
        line = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)[^)]*\)", r"\1", line)
        line = re.sub(r"\[([^\]]+)\]\(([^)\s]+)[^)]*\)", link, line)
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
        line = re.sub(r"^#{3,}\s+", "", line)
        out.append(line.rstrip())
    text = "\n".join(out).strip("\n")
    return re.sub(r"\n{3,}", "\n\n", text)


def build(site_root):
    posts = os.path.join(site_root, "errors", "posts")
    if not os.path.isdir(posts):
        raise SystemExit("нет папки %s: укажите путь к репозиторию сайта" % posts)
    entries, errors = [], []
    for name in sorted(os.listdir(posts)):
        if not name.endswith(".md") or name.startswith("_"):
            continue
        slug = name[:-3]
        try:
            meta, body = parse_post(os.path.join(posts, name))
        except ValueError as e:
            errors.append("%s: %s" % (name, e))
            continue
        patterns = [p.strip() for p in meta["match"].split("||") if p.strip()]
        for p in patterns:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    re.compile(p, re.I)
            except re.error as e:
                errors.append("%s: шаблон %r не компилируется: %s" % (name, p, e))
        sec = sections(body)
        entry = {
            "slug": slug,
            "title": meta["title"],
            "what": meta["what"],
            "component": meta["component"],
            "patterns": patterns,
            "url": ERRORS_URL + slug + ".html",
        }
        for head, key in SECTIONS.items():
            entry[key] = plain(sec.get(head, ""))
        entries.append(entry)
    if errors:
        raise SystemExit("ошибки в исходниках справочника:\n  " + "\n  ".join(errors))
    entries.sort(key=lambda e: e["slug"])
    data = {"source": ERRORS_URL, "count": len(entries), "errors": entries}
    return json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def main(argv):
    check = "--check" in argv
    args = [a for a in argv if a != "--check"]
    if any(a in ("-h", "--help") for a in args):
        print(__doc__.strip())
        return 0
    site = os.path.abspath(args[0]) if args else os.path.join(os.path.dirname(ROOT), "landing")
    text = build(site)
    old = None
    if os.path.exists(OUT):
        with io.open(OUT, encoding="utf-8") as f:
            old = f.read()
    rel = os.path.relpath(OUT, ROOT)
    if check:
        if old != text:
            print("устарело: %s (выполните python3 tools/sync_errors.py)" % rel)
            return 1
        print("актуально: %s" % rel)
        return 0
    if old == text:
        print("без изменений: %s" % rel)
        return 0
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with io.open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print("обновлено: %s (%d ошибок)" % (rel, json.loads(text)["count"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
