#!/usr/bin/env sh
# Страж утечек: в публичной репе не должно быть имён заказчиков, адресов наших стендов и учёток.
# Сам список шаблонов в репе не лежит: он в переменной LEAKCHECK_PATTERN (секрет CI)
# или в локальном файле .leakcheck (одна регулярка на строку, файл в .gitignore).
set -eu
cd "$(dirname "$0")/.."
pat="${LEAKCHECK_PATTERN:-}"
if [ -z "$pat" ] && [ -f .leakcheck ]; then
  pat=$(grep -v '^[[:space:]]*\(#\|$\)' .leakcheck | paste -sd'|' -)
fi
if [ -z "$pat" ]; then
  echo "leakcheck: шаблонов нет (LEAKCHECK_PATTERN или .leakcheck), проверка пропущена"
  exit 0
fi
# Отслеживаемые и новые файлы, кроме игнорируемых.
if git ls-files --cached --others --exclude-standard | tr '\n' '\0' | xargs -0 grep -EnI -i "$pat" --; then
  echo "leakcheck: найдены подозрительные строки, см. выше" >&2
  exit 1
fi
echo "leakcheck: чисто"
