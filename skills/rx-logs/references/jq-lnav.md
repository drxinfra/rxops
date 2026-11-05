# Отбор записей RX: jq, lnav, grep, PowerShell

Те же задачи, что у `log_filter.py`, но средствами, которые уже стоят у администратора.
Приёмы из статьи [Чем читать логи Directum RX](https://drxinfra.ru/blog/rx-logs-reading-tools.html).

## jq

Читайте журнал построчно (`-R`) и разбирайте каждую строку через `fromjson?`. Если открыть файл
как обычный JSON, jq остановится на первой битой строке (их пишет веб-агент) и молча потеряет
остаток файла.

```bash
cd <LOGS_PATH>

# ошибки за пять минут во всех журналах; время сравнивается строкой, пока у всех один пояс
jq -rR 'fromjson? | select(.l == "Error" and .t >= "2026-10-22 10:15" and .t < "2026-10-22 10:20")
  | [.t, .lg, (.ex.type // "-"), (.mt | tostring)] | @tsv' *.log | sort

# какие ошибки чаще всего за день
jq -rR 'fromjson? | select(.l == "Error") | "\(.ex.type // "-")  \(.ex.m // (.mt | tostring))"' \
  *.2026-10-22.log | sort | uniq -c | sort -rn | head -20

# всё, что делал пользователь
jq -rR 'fromjson? | select((.un // "") | test("ivanova"; "i"))
  | [.t, .l, .lg, (.span.name // (.mt | tostring))] | @tsv' *.2026-10-22.log | sort

# одно действие по трассе через все сервисы
grep -h '"tr":"cl-4e326a73' *.2026-10-22.log \
  | jq -rR 'fromjson? | [.t, .lg, .l, (.span.name // (.mt | tostring))] | @tsv' | sort

# операции дольше трёх секунд
jq -rR 'fromjson? | select(.span.durationMs? > 3000) | [.t, .span.name, .span.durationMs] | @tsv' \
  *.2026-10-22.log | sort -t$'\t' -k3 -rn | head

# стек исключения целиком
jq -rR 'fromjson? | select(.ex.type == "Npgsql.PostgresException") | .t, .ex.stack, ""' *.log | head -40
```

Если ищете проблему веб-агента, проверьте файл ещё и обычным `grep`: битые строки jq пропускает
без предупреждения.

## grep без программ

```bash
grep -h '"l":"Error"' *.2026-10-22.log | grep '"t":"2026-10-22 10:1' | head -50   # уровень и окно
grep -c '"l":"Error"' *.2026-10-22.log | sort -t: -k2 -rn | head                  # где больше всего ошибок
grep -h -i 'ivanova' *.2026-10-22.log remote/*.2026-10-22.log | head               # учётная запись
zgrep -h '"l":"Fatal"' *.log.gz | head                                             # сжатые журналы
```

Ищите `"l":"Error"` с кавычками, а не слово Error: оно встречается в именах методов и текстах
обычных записей, а записи уровня Warning с настоящей причиной теряются.

## lnav

[lnav](https://lnav.org) склеивает файлы разных сервисов в одну ленту по времени. Формат RX ему
описывают один раз файлом `~/.lnav/formats/installed/directum_rx.json`: готовый формат с полями
`t`, `l`, `mt`, `tr`, `ex/type`, `span/durationMs` есть в статье выше. После этого:

```bash
lnav <LOGS_PATH>/*.2026-10-22.log
```

Внутри: `e` и `E` к следующей и предыдущей ошибке, `:filter-in ivanova` только записи пользователя,
`:filter-out TraceSpan` убрать служебные замеры. Битые строки lnav, как и jq, пропускает.

## Windows и PowerShell

```powershell
Get-Content .\srv01.WebServer.2026-10-22.log |
  ForEach-Object { try { $_ | ConvertFrom-Json } catch {} } |
  Where-Object { $_.l -eq 'Error' } |
  Select-Object t, lg, @{n='ex'; e={$_.ex.type}}, mt
```

`try` нужен по той же причине, что `fromjson?` в jq. На рабочем месте журналы удобно смотреть
в Notepad++ с плагином JSTool (форматирование строки по `Ctrl+Alt+M`, поиск `"Error"` в кавычках
по всем открытым файлам), в LogExpert или LogViewPlus. Утилита вендора DirectumLogConverter с GitHub
переводит журнал в текст (`dlc файл.log`) или CSV для Excel (`dlc -c файл.log`), результат
в `<имя>_converted.log` (справка, «Лог-файлы во время работы»).
