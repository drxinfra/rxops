#!/usr/bin/env python3
"""Хук PostToolUse для Bash, Read и Grep: прячет пароли и токены до того, как вывод увидит модель.

Claude Code передаёт на stdin JSON с tool_response. Хук проходит по всем строкам
ответа маскировщиком (mask_secrets.py рядом) и, если что-то спрятал, возвращает
updatedToolOutput той же формы: модель получает текст с ‹скрыто› вместо значений,
а в additionalContext узнаёт, что значения спрятаны намеренно.

Выключить: переменная окружения RXOPS_MASK=off. Только стандартная библиотека.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mask_secrets  # noqa: E402

# Поля с путями и служебными значениями не трогаем: форма ответа должна остаться прежней.
SKIP_KEYS = {"filePath", "file_path", "path", "type", "mode"}


def walk(value, key=None):
    """Копия структуры с замаскированными строками и число спрятанных значений."""
    if isinstance(value, str):
        if key in SKIP_KEYS:
            return value, 0
        return mask_secrets.mask(value)
    if isinstance(value, list):
        out, total = [], 0
        for v in value:
            m, n = walk(v)
            out.append(m)
            total += n
        return out, total
    if isinstance(value, dict):
        out, total = {}, 0
        for k, v in value.items():
            m, n = walk(v, k)
            out[k] = m
            total += n
        return out, total
    return value, 0


def main():
    if os.environ.get("RXOPS_MASK", "").lower() in ("off", "0", "no", "false"):
        return 0
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
    except ValueError:
        return 0
    resp = data.get("tool_response")
    if resp is None:
        return 0
    masked, n = walk(resp)
    if not n:
        return 0
    note = ("rxops: в выводе %s скрыто значений: %d (пароли, токены, ключи). Значения спрятаны намеренно: "
            "не пытайтесь получить их другим способом. Если значение действительно нужно, попросите "
            "пользователя посмотреть его самому." % (data.get("tool_name", ""), n))
    out = json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                          "updatedToolOutput": masked,
                                          "additionalContext": note}}, ensure_ascii=False)
    sys.stdout.buffer.write(out.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
