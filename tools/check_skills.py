#!/usr/bin/env python3
"""Проверка оформления скилов: фронт-маттер, размеры, ссылки на файлы.

  python3 tools/check_skills.py            все скилы
  python3 tools/check_skills.py rx-logs    один скил

Правила описаны в docs/authoring.md. Код 1, если есть ошибки.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Только эти ключи понимают все места, куда грузят скилы (Claude Code, claude.ai, Skills API).
ALLOWED = {"name", "description", "license", "compatibility", "metadata"}
NAME = re.compile(r"^rx-[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_LINES = 300


def _forbidden():
    """Шаблоны запрещённых упоминаний: LEAKCHECK_PATTERN или файл .leakcheck (в репе их нет)."""
    pat = os.environ.get("LEAKCHECK_PATTERN", "")
    path = os.path.join(ROOT, ".leakcheck")
    if not pat and os.path.exists(path):
        lines = [l.strip() for l in open(path, encoding="utf-8")]
        pat = "|".join(l for l in lines if l and not l.startswith("#"))
    return re.compile(pat, re.I) if pat else None


FORBIDDEN = _forbidden()
RULES_MARK = "## Правила"


def frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return None, text
    fm, key = {}, None
    for line in m.group(1).split("\n"):
        if re.match(r"^[A-Za-z_-]+:", line):
            key, _, val = line.partition(":")
            key = key.strip()
            fm[key] = val.strip()
        elif key and line.startswith((" ", "\t")):
            fm[key] = (fm[key] + " " + line.strip()).strip()
    return fm, text[m.end():]


def check(name):
    errs = []
    d = os.path.join(ROOT, "skills", name)
    path = os.path.join(d, "SKILL.md")
    text = open(path, encoding="utf-8").read()
    fm, body = frontmatter(text)
    if fm is None:
        return ["нет фронт-маттера"]
    extra = set(fm) - ALLOWED
    if extra:
        errs.append("лишние ключи фронт-маттера: " + ", ".join(sorted(extra)))
    if fm.get("name") != name or not NAME.match(name) or len(name) > 64:
        errs.append("name должен совпадать с папкой и иметь вид rx-тема")
    desc = fm.get("description", "").strip("\"'")
    if not desc:
        errs.append("нет description")
    elif len(desc) > 1024:
        errs.append("description длиннее 1024 символов: %d" % len(desc))
    if len(fm.get("compatibility", "").strip("\"'")) > 500:
        errs.append("compatibility длиннее 500 символов")
    if fm.get("license") != "Apache-2.0":
        errs.append("license: Apache-2.0")
    lines = text.count("\n") + 1
    if lines > MAX_LINES:
        errs.append("SKILL.md длиннее %d строк: %d (подробности в references/)" % (MAX_LINES, lines))
    if RULES_MARK not in body:
        errs.append("нет общего раздела «Правила»")
    for ref in re.findall(r"(?<![\w/])((?:references|scripts)/[\w.\-/]+)", body):
        if not os.path.exists(os.path.join(d, ref.rstrip(".,"))):
            errs.append("нет файла " + ref)
    for root, _, files in os.walk(d):
        for f in files:
            p = os.path.join(root, f)
            try:
                t = open(p, encoding="utf-8").read()
            except (UnicodeDecodeError, OSError):
                continue
            m = FORBIDDEN.search(t) if FORBIDDEN else None
            if m:
                errs.append("%s: недопустимое упоминание «%s»" % (os.path.relpath(p, d), m.group(0)))
    return errs


def main(argv):
    names = argv or sorted(n for n in os.listdir(os.path.join(ROOT, "skills"))
                           if os.path.isfile(os.path.join(ROOT, "skills", n, "SKILL.md")))
    bad = 0
    for n in names:
        errs = check(n)
        print(("ОК  " if not errs else "ОШИБКИ ") + n)
        for e in errs:
            print("    - " + e)
        bad += bool(errs)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
