#!/usr/bin/env python3
"""Раскладывает общие файлы из shared/ по скилам и хукам.

Каждый скил должен работать сам по себе (его можно загрузить в Claude Desktop
отдельным архивом), поэтому общий маскировщик лежит копией в scripts/ каждого
скила и в hooks/. Канон — shared/; копии руками не править.

  python3 tools/sync_shared.py          разложить
  python3 tools/sync_shared.py --check  код 1, если какая-то копия отличается
"""
import filecmp
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHARED = ["mask_secrets.py"]


def targets():
    for name in sorted(os.listdir(os.path.join(ROOT, "skills"))):
        if os.path.isfile(os.path.join(ROOT, "skills", name, "SKILL.md")):
            yield os.path.join(ROOT, "skills", name, "scripts")


def main(check):
    bad = 0
    for d in targets():
        for f in SHARED:
            src, dst = os.path.join(ROOT, "shared", f), os.path.join(d, f)
            if os.path.exists(dst) and filecmp.cmp(src, dst, shallow=False):
                continue
            rel = os.path.relpath(dst, ROOT)
            if check:
                print("отличается или нет: " + rel)
                bad += 1
                continue
            os.makedirs(d, exist_ok=True)
            shutil.copy2(src, dst)
            print("обновлено: " + rel)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main("--check" in sys.argv[1:]))
