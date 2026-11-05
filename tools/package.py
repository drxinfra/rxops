#!/usr/bin/env python3
"""Собирает архивы релиза в dist/.

  dist/rxops-<версия>.zip            весь плагин: для `claude --plugin-dir` и установки без GitHub
  dist/skills/<скил>-<версия>.zip    каждый скил отдельно: для загрузки в Claude Desktop и claude.ai
  dist/SHA256SUMS                    контрольные суммы всех архивов

Версия берётся из .claude-plugin/plugin.json. Только стандартная библиотека.
"""
import hashlib
import json
import os
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_FILES = [".claude-plugin", "skills", "README.md", "LICENSE", "NOTICE"]
SKIP_DIRS = {"__pycache__", ".git"}
SKIP_FILES = {".DS_Store"}
# Одинаковое время у всех файлов: архив с теми же исходниками даёт ту же сумму.
STAMP = (2026, 1, 1, 0, 0, 0)


def files_under(path):
    if os.path.isfile(path):
        yield path
        return
    for root, dirs, files in os.walk(path):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for f in sorted(files):
            if f not in SKIP_FILES and not f.endswith(".pyc"):
                yield os.path.join(root, f)


def write_zip(target, entries):
    """entries: [(путь на диске, путь в архиве)]."""
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for src, arc in entries:
            info = zipfile.ZipInfo(arc, STAMP)
            mode = 0o755 if os.access(src, os.X_OK) else 0o644
            info.external_attr = (mode | 0o100000) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(src, "rb") as f:
                z.writestr(info, f.read())


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    version = json.load(open(os.path.join(ROOT, ".claude-plugin", "plugin.json"), encoding="utf-8"))["version"]
    dist = os.path.join(ROOT, "dist")
    shutil.rmtree(dist, ignore_errors=True)
    built = []

    entries = []
    for item in PLUGIN_FILES:
        for src in files_under(os.path.join(ROOT, item)):
            entries.append((src, "rxops/" + os.path.relpath(src, ROOT).replace(os.sep, "/")))
    target = os.path.join(dist, "rxops-%s.zip" % version)
    write_zip(target, entries)
    built.append(target)

    skills_dir = os.path.join(ROOT, "skills")
    for name in sorted(os.listdir(skills_dir)):
        d = os.path.join(skills_dir, name)
        if not os.path.isfile(os.path.join(d, "SKILL.md")):
            continue
        entries = [(src, os.path.relpath(src, skills_dir).replace(os.sep, "/")) for src in files_under(d)]
        target = os.path.join(dist, "skills", "%s-%s.zip" % (name, version))
        write_zip(target, entries)
        built.append(target)

    with open(os.path.join(dist, "SHA256SUMS"), "w") as f:
        for p in built:
            f.write("%s  %s\n" % (sha256(p), os.path.relpath(p, dist).replace(os.sep, "/")))
    for p in built:
        print("%8.1f КБ  %s" % (os.path.getsize(p) / 1024.0, os.path.relpath(p, ROOT)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
