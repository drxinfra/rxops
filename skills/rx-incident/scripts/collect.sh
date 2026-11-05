#!/usr/bin/env bash
# collect.sh: снимок сервера Directum RX для первого разбора инцидента. Только чтение.
# Справка: bash collect.sh --help

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SINCE="2 hours ago"
LINES=40
LOGS=""
NS=""
T=15          # сколько секунд ждать одну команду: зависший NFS или docker не должны вешать снимок

usage() {
cat <<'EOF'
Снимок сервера Directum RX для первого разбора инцидента. Только читает: ничего не перезапускает,
не удаляет и не меняет. Весь вывод проходит через mask_secrets.py из той же папки.

Использование:
  bash collect.sh [--since "2 hours ago"] [--lines 40] [--logs ПАПКА] [--namespace NS]

Ключи:
  --since ВРЕМЯ   окно для журнала ядра и ошибок journald в формате journalctl
                  (по умолчанию "2 hours ago"; можно "2026-11-05 10:00")
  --lines N       сколько последних строк журналов показывать (по умолчанию 40)
  --logs ПАПКА    папка LOGS_PATH Directum RX: свежие файлы и статус-файлы проверок работоспособности
  --namespace NS  пространство имён Kubernetes (по умолчанию все)
  -h, --help      эта справка

Что снимает: время работы, нагрузку и перезагрузки, синхронизацию времени, место и inode на дисках,
память и давление на ресурсы, крупные процессы и процессы в состоянии D, убийства по памяти
в журнале ядра, упавшие службы systemd, последние ошибки journald, контейнеры Docker или Podman
(состояние, перезапуски, OOMKilled, память к лимиту), узлы, поды и события Kubernetes, если kubectl
отвечает, слушающие порты. Чего нет на сервере, пропускается с пометкой. Каждая команда ждёт
не дольше 15 секунд. Для полной картины запускайте через sudo: без прав не видно журнал ядра,
чужие процессы и контейнеры.

Коды возврата: 0 отклонений не замечено, 1 есть отклонения (список в разделе «Итог»),
2 не удалось проверить (ошибка в ключах).
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --since) [ $# -ge 2 ] || { echo "collect.sh: --since без значения" >&2; exit 2; }; SINCE="$2"; shift 2 ;;
    --lines)
      if [ $# -lt 2 ] || ! [[ "$2" =~ ^[0-9]+$ ]]; then echo "collect.sh: --lines ждёт число" >&2; exit 2; fi
      LINES="$2"; shift 2 ;;
    --logs) [ $# -ge 2 ] || { echo "collect.sh: --logs без значения" >&2; exit 2; }; LOGS="$2"; shift 2 ;;
    --namespace) [ $# -ge 2 ] || { echo "collect.sh: --namespace без значения" >&2; exit 2; }; NS="$2"; shift 2 ;;
    *) echo "collect.sh: непонятный ключ $1 (справка: --help)" >&2; exit 2 ;;
  esac
done

have() { command -v "$1" >/dev/null 2>&1; }
TO=()
have timeout && TO=(timeout "$T")
DEVS=()
dev() { DEVS+=("$1"); }
section() { printf '\n=== %s ===\n' "$1"; }
count() { wc -l | tr -d ' '; }      # wc на macOS и busybox печатает число с пробелами
note() { printf '(%s)\n' "$1"; }

# run КОМАНДА...: вывод команды; если вывода нет, а код не 0, пишет код; таймаут отмечает отдельно.
run() {
  local out rc
  out=$("${TO[@]}" "$@" 2>&1); rc=$?
  if [ "$rc" -eq 124 ] && [ ${#TO[@]} -gt 0 ]; then
    note "не ответило за ${T} с: $*"
    return "$rc"
  fi
  if [ -n "$out" ]; then
    printf '%s\n' "$out"
  elif [ "$rc" -ne 0 ]; then
    note "код $rc: $*"
  fi
  return "$rc"
}

# runl N КОМАНДА...: как run, но только первые N строк.
runl() {
  local n="$1"; shift
  local out rc total
  out=$("${TO[@]}" "$@" 2>&1); rc=$?
  if [ "$rc" -eq 124 ] && [ ${#TO[@]} -gt 0 ]; then note "не ответило за ${T} с: $*"; return "$rc"; fi
  if [ -z "$out" ]; then [ "$rc" -ne 0 ] && note "код $rc: $*"; return "$rc"; fi
  total=$(printf '%s\n' "$out" | count)
  printf '%s\n' "$out" | head -n "$n"
  [ "$total" -gt "$n" ] && note "ещё $((total - n)) строк не показано"
  return "$rc"
}

system_part() {
  section "Система"
  printf 'Хост: %s   Сейчас: %s   Пользователь: %s\n' "$(hostname 2>/dev/null)" "$(date '+%Y-%m-%d %H:%M:%S %z')" "$(id -un 2>/dev/null)"
  if [ -r /etc/os-release ]; then
    printf 'ОС: %s   Ядро: %s\n' "$(sed -n 's/^PRETTY_NAME=//p' /etc/os-release | tr -d '"')" "$(uname -r)"
  fi
  [ "$(id -u)" -ne 0 ] && note "запущено не от root: часть сведений будет неполной"
  if have uptime; then
    run uptime
  elif [ -r /proc/loadavg ]; then
    echo "Нагрузка за 1, 5, 15 минут: $(cut -d' ' -f1-3 /proc/loadavg)"
  fi
  local cores load5 up
  cores=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)
  echo "Ядер: $cores"
  if [ -r /proc/loadavg ]; then
    load5=$(cut -d' ' -f2 /proc/loadavg)
    if awk -v l="$load5" -v c="$cores" 'BEGIN { exit !(l > 2 * c) }'; then
      dev "нагрузка за 5 минут $load5 при $cores ядрах: очередь к процессору или ожидание диска"
    fi
  fi
  if [ -r /proc/uptime ]; then
    up=$(cut -d. -f1 /proc/uptime)
    if [ "$up" -lt 3600 ]; then
      dev "сервер работает $((up / 60)) мин: была перезагрузка, выясните почему (last -x, журнал прошлой загрузки journalctl -b -1)"
    fi
  fi
  if have last && last -x -F reboot >/dev/null 2>&1; then
    section "Последние перезагрузки"
    runl 5 last -x -F reboot shutdown || true
  fi
}

time_part() {
  section "Время"
  if have timedatectl; then
    local out
    out=$("${TO[@]}" timedatectl 2>&1)
    printf '%s\n' "$out"
    if printf '%s' "$out" | grep -q -i 'synchronized: no'; then
      dev "часы не синхронизированы: ломаются вход Kerberos, токены и сопоставление журналов"
    fi
  else
    note "нет timedatectl"
  fi
  if have chronyc; then
    runl 12 chronyc tracking || true
  fi
}

disk_part() {
  section "Диски: место"
  local x=(-x tmpfs -x devtmpfs -x overlay -x squashfs -x iso9660 -x efivarfs)
  df -hPT -x tmpfs / >/dev/null 2>&1 || x=()      # busybox df не знает -T и -x
  if [ ${#x[@]} -gt 0 ]; then run df -hPT "${x[@]}"; else run df -hP; fi
  local line linux=0
  [ "$(uname -s)" = "Linux" ] && linux=1     # колонки df у BSD и macOS другие
  [ "$linux" -eq 1 ] && while read -r line; do
    [ -n "$line" ] && dev "раздел $line"
  done < <("${TO[@]}" df -P "${x[@]}" 2>/dev/null | awk 'NR > 1 { p = $5; sub("%", "", p); if (p + 0 >= 90) printf "%s занят на %s %%\n", $6, p }')
  section "Диски: inode"
  if [ ${#x[@]} -gt 0 ]; then run df -hPi "${x[@]}"; else run df -Pi; fi
  [ "$linux" -eq 1 ] && while read -r line; do
    [ -n "$line" ] && dev "на разделе $line"
  done < <("${TO[@]}" df -Pi "${x[@]}" 2>/dev/null | awk 'NR > 1 { p = $5; sub("%", "", p); if (p != "-" && p + 0 >= 90) printf "%s заняты %s %% inode\n", $6, p }')
}

memory_part() {
  section "Память"
  if have free; then
    run free -h
  elif [ -r /proc/meminfo ]; then
    grep -E '^(MemTotal|MemAvailable|SwapTotal|SwapFree):' /proc/meminfo
  fi
  if [ -r /proc/meminfo ]; then
    local pct
    pct=$(awk '/^MemTotal:/ { t = $2 } /^MemAvailable:/ { a = $2 } END { if (t > 0 && a != "") printf "%d", a * 100 / t }' /proc/meminfo)
    if [ -n "$pct" ] && [ "$pct" -lt 10 ]; then
      dev "доступно (available) только $pct % памяти"
    fi
  fi
  if [ -r /proc/pressure/memory ]; then
    local r
    for r in cpu memory io; do
      [ -r "/proc/pressure/$r" ] && printf 'давление %s: %s\n' "$r" "$(head -1 "/proc/pressure/$r")"
    done
  fi
  if have vmstat; then
    if vmstat -w 1 1 >/dev/null 2>&1; then run vmstat -w 1 3; else run vmstat 1 3; fi
  fi
}

process_part() {
  if ! have ps; then
    section "Процессы"
    note "нет ps (пакет procps)"
    return 0
  fi
  if ! ps -eo pid --sort=-rss >/dev/null 2>&1; then
    section "Процессы"
    note "этот ps не умеет сортировать (busybox?), показываю как есть"
    runl 30 ps
    return 0
  fi
  section "Процессы: больше всего памяти"
  runl 12 ps -eo pid,user,rss,%mem,%cpu,etime,comm --sort=-rss
  section "Процессы: больше всего процессора"
  runl 12 ps -eo pid,user,%cpu,%mem,etime,comm --sort=-%cpu
  section "Процессы в состоянии D (ждут диск или сеть)"
  local d
  d=$("${TO[@]}" ps -eo stat,pid,wchan:32,comm 2>/dev/null | awk '$1 ~ /^D/')
  if [ -n "$d" ]; then
    printf '%s\n' "$d" | head -n 20
    local n
    n=$(printf '%s\n' "$d" | count)
    [ "$n" -ge 3 ] && dev "процессов в состоянии D: $n (смотрите wchan: nfs значит сетевой диск)"
  else
    echo "нет"
  fi
}

kernel_part() {
  section "Журнал ядра: память, диски, зависания (с $SINCE)"
  local out=""
  if have journalctl; then
    out=$("${TO[@]}" journalctl -k -q --no-pager --since "$SINCE" 2>/dev/null \
      | grep -i -E 'out of memory|oom-kill|killed process|memory cgroup|blocked for more than|i/o error|nfs: server|ext4-fs error|xfs.*error|remount.*read-only')
  fi
  if [ -z "$out" ] && have dmesg; then
    out=$("${TO[@]}" dmesg -T 2>/dev/null \
      | grep -i -E 'out of memory|oom-kill|killed process|memory cgroup|blocked for more than|i/o error|nfs: server|ext4-fs error|xfs.*error|remount.*read-only' | tail -n 200)
    [ -n "$out" ] && note "из dmesg, без фильтра по времени"
  fi
  if [ -n "$out" ]; then
    printf '%s\n' "$out" | tail -n "$LINES"
    local oom
    oom=$(printf '%s\n' "$out" | grep -c -i -E 'killed process|oom-kill')
    [ "$oom" -gt 0 ] && dev "убийства процессов по памяти в журнале ядра: $oom"
    printf '%s\n' "$out" | grep -q -i -E 'i/o error|ext4-fs error|xfs.*error|remount.*read-only' \
      && dev "в журнале ядра ошибки диска или файловой системы"
    printf '%s\n' "$out" | grep -q -i 'nfs: server' && dev "в журнале ядра NFS-сервер не отвечает"
    printf '%s\n' "$out" | grep -q -i 'blocked for more than' && dev "в журнале ядра зависшие задачи (blocked for more than)"
  else
    echo "записей нет или журнал ядра недоступен без root"
  fi
}

services_part() {
  if have systemctl; then
    section "Упавшие службы systemd"
    local f
    f=$("${TO[@]}" systemctl --failed --no-legend --plain 2>/dev/null)
    if [ -n "$f" ]; then
      printf '%s\n' "$f"
      dev "упавших служб systemd: $(printf '%s\n' "$f" | count) ($(printf '%s\n' "$f" | awk '{ print $1 }' | head -5 | paste -sd ' ' -))"
    else
      echo "нет"
    fi
  fi
  if have journalctl; then
    section "Ошибки journald (с $SINCE, последние $LINES)"
    runl "$LINES" journalctl -p err -q --no-pager --since "$SINCE" -n "$LINES" || true
  fi
}

container_part() {
  local engine found=0
  for engine in docker podman; do
    have "$engine" || continue
    found=1
    section "Контейнеры: $engine"
    if ! "${TO[@]}" "$engine" ps >/dev/null 2>&1; then
      note "$engine есть, но не отвечает или нет прав: запустите через sudo"
      continue
    fi
    runl 60 "$engine" ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
    local ids
    ids=$("${TO[@]}" "$engine" ps -aq 2>/dev/null | head -n 100)
    [ -z "$ids" ] && continue
    section "Контейнеры: перезапуски, OOMKilled, код выхода ($engine)"
    local info
    # shellcheck disable=SC2086
    info=$("${TO[@]}" "$engine" inspect -f '{{.Name}} status={{.State.Status}} restarts={{.RestartCount}} oom={{.State.OOMKilled}} exit={{.State.ExitCode}} started={{.State.StartedAt}}' $ids 2>&1)
    printf '%s\n' "$info" | sed 's#^/##'
    local line
    while read -r line; do
      [ -n "$line" ] && dev "контейнер $line"
    done < <(printf '%s\n' "$info" | sed 's#^/##' | awk '{
        name = $1; split($2, s, "="); split($3, r, "="); split($4, o, "="); split($5, e, "=")
        if (s[2] == "restarting") print name ": перезапускается по кругу"
        else if (s[2] == "exited" && e[2] != "0") print name ": остановлен с кодом " e[2] (e[2] == "137" ? " (убит, часто по памяти)" : "")
        else if (o[2] == "true") print name ": был убит по памяти (OOMKilled)"
        if (r[2] + 0 >= 5) print name ": перезапусков " r[2]
      }')
    section "Контейнеры: ресурсы ($engine)"
    local st
    st=$("${TO[@]}" "$engine" stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}' 2>&1)
    printf '%s\n' "$st" | head -n 60
    while read -r line; do
      [ -n "$line" ] && dev "контейнер $line"
    done < <(printf '%s\n' "$st" | awk 'NR > 1 { p = $NF; sub("%", "", p); if (p + 0 >= 90) print $1 ": " $NF " памяти от лимита" }')
  done
  [ "$found" -eq 0 ] && { section "Контейнеры"; note "нет docker и podman"; }
  return 0
}

kube_part() {
  have kubectl || return 0
  section "Kubernetes"
  if ! "${TO[@]}" kubectl get nodes --request-timeout=5s >/dev/null 2>&1; then
    note "kubectl есть, но кластер не отвечает или нет доступа (KUBECONFIG, права)"
    return 0
  fi
  local nsarg=(-A)
  [ -n "$NS" ] && nsarg=(-n "$NS")
  run kubectl get nodes -o wide --request-timeout=10s
  local nr
  nr=$("${TO[@]}" kubectl get nodes --no-headers --request-timeout=10s 2>/dev/null | awk '$2 !~ /^Ready/ { print $1 " " $2 }')
  [ -n "$nr" ] && dev "узлы Kubernetes не в Ready: $(printf '%s' "$nr" | paste -sd ',' -)"
  section "Kubernetes: поды не в порядке"
  local jp='{range .items[*]}{.metadata.namespace}{"\t"}{.metadata.name}{"\t"}{.status.phase}{"\t"}{range .status.containerStatuses[*]}{.name}{"|"}{.ready}{"|"}{.restartCount}{"|"}{.state.waiting.reason}{"|"}{.lastState.terminated.reason}{";"}{end}{"\n"}{end}'
  local pods bad
  pods=$("${TO[@]}" kubectl get pods "${nsarg[@]}" -o jsonpath="$jp" --request-timeout=20s 2>&1)
  bad=$(printf '%s\n' "$pods" | awk -F'\t' 'NF >= 3 {
      ns = $1; name = $2; phase = $3; nready = 0; total = 0; rs = 0; why = ""; last = ""
      n = split($4, cs, ";")
      for (i = 1; i <= n; i++) {
        if (cs[i] == "") continue
        split(cs[i], f, "|"); total++
        if (f[2] == "true") nready++
        rs += f[3]
        if (f[4] != "") why = why (why != "" ? "," : "") f[4]
        if (f[5] != "") last = last (last != "" ? "," : "") f[5]
      }
      if (phase == "Succeeded") next
      if (phase != "Running" || nready < total || why != "" || last ~ /OOMKilled/ || rs >= 5)
        printf "%s/%s phase=%s ready=%d/%d restarts=%d%s%s\n", ns, name, phase, nready, total, rs,
          (why != "" ? " waiting=" why : ""), (last != "" ? " last=" last : "")
    }')
  if [ -n "$bad" ]; then
    printf '%s\n' "$bad" | head -n 40
    local n
    n=$(printf '%s\n' "$bad" | count)
    dev "подов не в порядке: $n (CrashLoopBackOff, не Ready, OOMKilled, много перезапусков)"
  else
    echo "все поды Running и Ready, перезапусков меньше 5"
  fi
  section "Kubernetes: предупреждения (последние $LINES)"
  runl "$LINES" kubectl get events "${nsarg[@]}" --field-selector type=Warning --sort-by=.lastTimestamp --request-timeout=20s || true
  section "Kubernetes: потребление (если есть metrics-server)"
  runl 10 kubectl top nodes --request-timeout=10s || true
  runl 15 kubectl top pods "${nsarg[@]}" --sort-by=memory --request-timeout=20s || true
}

network_part() {
  have ss || { section "Сеть"; note "нет ss"; return 0; }
  section "Сеть: соединения"
  run ss -s
  local tw
  tw=$("${TO[@]}" ss -tan state time-wait 2>/dev/null | count)
  echo "TIME-WAIT: $tw"
  section "Сеть: слушающие порты"
  runl 60 ss -tlnp || true
}

rx_part() {
  [ -n "$LOGS" ] || return 0
  section "Журналы RX: $LOGS"
  if [ ! -d "$LOGS" ]; then
    note "нет такой папки"
    dev "папка журналов RX $LOGS не найдена"
    return 0
  fi
  runl 15 ls -lt "$LOGS" || true
  [ -d "$LOGS/status" ] || { note "нет подпапки status: статус-файлы отключены или лежат в другом месте"; return 0; }
  section "Статус-файлы проверок работоспособности"
  if ! have python3; then
    note "нет python3: показываю общий статус, без проверок по отдельности"
    local f s
    for f in "$LOGS"/status/*.status; do
      [ -f "$f" ] || continue
      s=$(grep -m1 -o '"Status": *"[a-z]*"' "$f" | sed 's/.*"\([a-z]*\)"$/\1/')
      printf '%s: %s, изменён %s\n' "$(basename "$f")" "${s:-?}" "$(date -r "$f" '+%Y-%m-%d %H:%M:%S' 2>/dev/null)"
      [ -n "$s" ] && [ "$s" != "healthy" ] && dev "$(basename "$f"): статус $s"
    done
    return 0
  fi
  local out line
  out=$(python3 - "$LOGS/status" <<'PY'
import datetime, glob, json, os, re, sys
now = datetime.datetime.now(datetime.timezone.utc)
def when(s):
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d):(\d\d)(?:\.\d+)?\s*(?:(Z)|([+-])(\d\d):?(\d\d))?", s or "")
    if not m:
        return None
    y, mo, d, h, mi, se, z, sign, oh, om = m.groups()
    dt = datetime.datetime(int(y), int(mo), int(d), int(h), int(mi), int(se))
    if z:
        return dt.replace(tzinfo=datetime.timezone.utc)
    if sign:
        off = datetime.timedelta(hours=int(oh), minutes=int(om))
        return dt.replace(tzinfo=datetime.timezone(-off if sign == "-" else off))
    return dt.astimezone()
for path in sorted(glob.glob(os.path.join(sys.argv[1], "*.status"))):
    name = os.path.basename(path)
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as e:
        print("%s: не читается (%s)" % (name, e))
        continue
    status = str(data.get("Status", "?"))
    t = when(data.get("Time"))
    age = int((now - t).total_seconds()) if t else None
    bad = []
    for check, v in sorted((data.get("Statuses") or {}).items()):
        st = v.get("Status") if isinstance(v, dict) else v
        if st and st != "healthy":
            bad.append("%s=%s" % (check, st))
    line = "%s: %s, обновлён %s назад" % (name, status, ("%d с" % age) if age is not None else "?")
    if bad:
        line += ", не в норме: " + ", ".join(bad)
    if data.get("LastNotUnhealthyTime") and data.get("LastNotUnhealthyTime") != data.get("Time"):
        line += ", последний раз не unhealthy: %s" % data.get("LastNotUnhealthyTime")
    print(line)
    if status != "healthy":
        print("DEV %s: статус %s" % (name, status))
    if age is not None and age > 180:
        print("DEV %s: не обновлялся %d с, сервис, похоже, не работает" % (name, age))
PY
)
  while IFS= read -r line; do
    case "$line" in
      DEV\ *) dev "${line#DEV }" ;;
      *) printf '%s\n' "$line" ;;
    esac
  done <<< "$out"
}

summary_part() {
  section "Итог"
  if [ ${#DEVS[@]} -eq 0 ]; then
    echo "Отклонений не замечено. Это только срез сервера: проверьте зависимости (база, брокер, поиск,"
    echo "хранилище, вход), проверки работоспособности сервисов RX и журналы."
    return 0
  fi
  echo "Отклонения (${#DEVS[@]}):"
  local d
  for d in "${DEVS[@]}"; do
    echo "- $d"
  done
  return 1
}

main() {
  echo "collect.sh: снимок сервера для разбора инцидента, только чтение. Окно журналов: с $SINCE."
  [ "$(uname -s)" = "Linux" ] || note "скрипт рассчитан на Linux: на $(uname -s) часть разделов пустая, а отклонения по дискам неточные"
  system_part
  time_part
  disk_part
  memory_part
  process_part
  kernel_part
  services_part
  container_part
  kube_part
  network_part
  rx_part
  summary_part
}

MASK="$DIR/mask_secrets.py"
if have python3 && [ -f "$MASK" ]; then
  main 2>&1 | PYTHONIOENCODING=utf-8 python3 "$MASK"
  rc=${PIPESTATUS[0]}
else
  echo "ВНИМАНИЕ: нет python3 или mask_secrets.py, пароли прячутся упрощённо" >&2
  main 2>&1 | sed -E 's/((pass(word)?|pwd|secret|token|api[_-]?key)[^=: ]{0,20}[=:] *)[^ ;,"]+/\1‹скрыто›/Ig'
  rc=${PIPESTATUS[0]}
fi
exit "$rc"
