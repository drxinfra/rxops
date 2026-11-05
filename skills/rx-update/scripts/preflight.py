#!/usr/bin/env python3
"""Проверки перед обновлением Directum RX и после него. Только чтение.

Скрипт ничего не перезапускает, не удаляет и не пишет на диск: результат идёт в stdout.
Смотрит место на дисках, память, синхронизацию времени, свежесть бэкапа базы, папку
Directum Launcher и его журналы, статус-файлы сервисов, контейнеры (docker, podman,
kubectl) и адреса проверки работоспособности. В конце печатает, что проверить руками.

Примеры:
  python3 preflight.py --path /srv=40 --backup-dir /backup/pg --launcher-dir /srv/DirectumLauncher
  python3 preflight.py --url https://rx.example.com/Client/api/health --status-dir /srv/logs/status
  python3 preflight.py --json [параметры] > ~/rx-before.json      снимок «до» (файл создаёт оболочка)
  python3 preflight.py --post --before ~/rx-before.json [параметры]  проверки после обновления

Код возврата: 0 норма, 1 есть WARN или CRIT, 2 часть проверок выполнить не удалось (ERROR).
Только стандартная библиотека, Python 3.6+.
"""
import argparse
import datetime
import email.utils
import fnmatch
import io
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

VERSION = "0.1.0"
TOOL = "rx-update/preflight"
OK, WARN, CRIT, INFO, SKIP, ERROR = "OK", "WARN", "CRIT", "INFO", "SKIP", "ERROR"
ORDER = [OK, WARN, CRIT, INFO, SKIP, ERROR]
GB = 1024.0 ** 3
SITE = "https://drxinfra.ru/errors/"

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
sys.dont_write_bytecode = True  # скрипт ничего не пишет на диск, в том числе __pycache__ рядом с собой
try:
    import mask_secrets  # копия shared/mask_secrets.py рядом со скриптом

    def mask(text):
        return mask_secrets.mask(text)[0]
except ImportError:  # маскировщика рядом нет: грубая замена, лучше спрятать лишнее
    def mask(text):
        return re.sub(r"(?i)(pass(?:word)?|pwd|secret|token)(\s*[:=]\s*)\S+", r"\1\2‹скрыто›", text)


# ---------------------------------------------------------------- общее

class Report(object):
    def __init__(self, mode):
        self.mode = mode
        self.checks = []
        self.inventory = {"containers": [], "pods": []}
        self.manual = []

    def add(self, cid, title, status, detail="", hint=""):
        self.checks.append({"id": cid, "title": title, "status": status,
                            "detail": mask(detail) if detail else "", "hint": hint})

    def summary(self):
        s = dict((k, 0) for k in ORDER)
        for c in self.checks:
            s[c["status"]] = s.get(c["status"], 0) + 1
        return s

    def exit_code(self):
        found = set(c["status"] for c in self.checks)
        if found & {WARN, CRIT}:
            return 1
        if ERROR in found:
            return 2
        return 0


def run(cmd, timeout=20, env=None):
    """Команда без оболочки. Возвращает (код, stdout, stderr); код None: нет команды или таймаут."""
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, env=env)
    except FileNotFoundError:
        return None, "", "команда не найдена: " + cmd[0]
    except subprocess.TimeoutExpired:
        return None, "", "нет ответа за %d с: %s" % (timeout, " ".join(cmd[:3]))
    except OSError as e:
        return None, "", str(e)
    return (p.returncode, p.stdout.decode("utf-8", "replace"),
            p.stderr.decode("utf-8", "replace"))


def first_line(text, limit=200):
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:limit]
    return ""


def fmt_num(x, digits=1):
    s = ("%." + str(digits) + "f") % x
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s.replace(".", ",")


def fmt_size(n):
    n = float(n)
    if n >= GB:
        return fmt_num(n / GB) + " ГБ"
    if n >= 1024 ** 2:
        return fmt_num(n / 1024 ** 2) + " МБ"
    return fmt_num(n / 1024) + " КБ"


def fmt_dur(seconds):
    seconds = abs(seconds)
    if seconds < 59.5:
        return "%d с" % int(round(seconds))
    m = int(round(seconds / 60.0))
    if m < 60:
        return "%d мин" % m
    h, m = divmod(m, 60)
    if h < 48:
        return "%d ч %d мин" % (h, m) if m else "%d ч" % h
    return "%d дн %d ч" % divmod(h, 24)


def plural(n, one, few, many):
    """1 символ, 2 символа, 5 символов."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def fmt_ts(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")


# ---------------------------------------------------------------- диски, память, время

def parse_path_arg(value):
    """'/srv=50' -> ('/srv', 50.0); '/srv' -> ('/srv', None). Знак = не путается с C:\\ в Windows."""
    if "=" in value:
        path, _, gb = value.rpartition("=")
        try:
            return path, float(gb.replace(",", "."))
        except ValueError:
            pass
    return value, None


def disk_verdict(free, total, need_gb=None):
    pct = 100.0 * free / total if total else 0.0
    detail = "свободно %s из %s (%d %%)" % (fmt_size(free), fmt_size(total), int(pct))
    if need_gb is not None and free < need_gb * GB:
        return CRIT, detail + ", а нужно не меньше %s ГБ" % fmt_num(need_gb)
    if pct < 5:
        return CRIT, detail
    if pct < 10:
        return WARN, detail
    return OK, detail


def check_disks(rep, items):
    seen = set()
    for path, need, label in items:
        key = os.path.realpath(path)
        if key in seen:
            continue
        seen.add(key)
        cid, title = "disk:" + path, "Диск " + path + (" (%s)" % label if label else "")
        if not os.path.exists(path):
            rep.add(cid, title, ERROR, "нет такого пути", "проверьте путь в --path")
            continue
        try:
            du = shutil.disk_usage(path)
        except OSError as e:
            rep.add(cid, title, ERROR, str(e))
            continue
        status, detail = disk_verdict(du.free, du.total, need)
        hint = "" if status == OK else (
            "освободите место до окна: нужен запас под бэкап базы перед работами и под файлы "
            "двух версий сразу (образы, сборки в etc/_builds)")
        rep.add(cid, title, status, detail, hint)


def parse_meminfo(text):
    out = {}
    for line in text.splitlines():
        m = re.match(r"^(\w+):\s+(\d+)", line)
        if m:
            out[m.group(1)] = int(m.group(2)) * 1024
    return out


def memory_verdict(mi):
    total = mi.get("MemTotal", 0)
    if not total:
        return ERROR, "в /proc/meminfo нет MemTotal"
    avail = mi.get("MemAvailable")
    if avail is None:
        avail = mi.get("MemFree", 0) + mi.get("Buffers", 0) + mi.get("Cached", 0)
    pct = 100.0 * avail / total
    detail = "доступно %s из %s (%d %%)" % (fmt_size(avail), fmt_size(total), int(pct))
    swap = mi.get("SwapTotal", 0)
    if swap:
        detail += ", swap занят %s из %s" % (fmt_size(swap - mi.get("SwapFree", 0)), fmt_size(swap))
    if pct < 10:
        return CRIT, detail
    if pct < 20:
        return WARN, detail
    return OK, detail


def check_memory(rep):
    if not os.path.exists("/proc/meminfo"):
        rep.add("memory", "Память", SKIP, "нет /proc/meminfo: проверка памяти только для Linux")
        return
    try:
        with open("/proc/meminfo") as f:
            status, detail = memory_verdict(parse_meminfo(f.read()))
    except OSError as e:
        rep.add("memory", "Память", ERROR, str(e))
        return
    hint = "" if status == OK else (
        "посмотрите, что занимает память (ps aux --sort=-rss | head); во время обновления "
        "сборка образов и конвертация базы требуют запаса")
    rep.add("memory", "Память", status, detail, hint)


def parse_timedatectl(text):
    """Вывод `timedatectl show` или `timedatectl status` -> True, False или None."""
    m = re.search(r"^NTPSynchronized=(yes|no)\s*$", text, re.M)
    if m:
        return m.group(1) == "yes"
    m = re.search(r"(?:System clock synchronized|NTP synchronized):\s*(yes|no)", text)
    if m:
        return m.group(1) == "yes"
    return None


def parse_chronyc_tracking(text):
    """Вывод `chronyc tracking` -> (синхронизировано или None, смещение в секундах или None)."""
    leap = re.search(r"^Leap status\s*:\s*(.+?)\s*$", text, re.M)
    off = re.search(r"^System time\s*:\s*([\d.]+) seconds (fast|slow)", text, re.M)
    synced = (leap.group(1) == "Normal") if leap else None
    offset = float(off.group(1)) if off else None
    return synced, offset


def check_clock(rep):
    title = "Синхронизация времени"
    if not sys.platform.startswith("linux"):
        rep.add("clock", title, SKIP, "проверка для Linux; на Windows: w32tm /query /status")
        return
    env = dict(os.environ, LC_ALL="C")
    synced, src, offset = None, "", None
    if shutil.which("timedatectl"):
        for args in (["timedatectl", "show"], ["timedatectl", "status"]):
            code, out, _ = run(args, timeout=10, env=env)
            if code == 0:
                synced = parse_timedatectl(out)
                if synced is not None:
                    src = "timedatectl"
                    break
    if synced is None and shutil.which("chronyc"):
        code, out, _ = run(["chronyc", "tracking"], timeout=10, env=env)
        if code == 0:
            synced, offset = parse_chronyc_tracking(out)
            src = "chronyc"
    hint = ("включите синхронизацию с NTP до окна: расхождение часов ломает вход по Kerberos, "
            "проверку сертификатов и токенов и сопоставление журналов")
    extra = ", смещение %s с" % fmt_num(offset, 3) if offset is not None else ""
    if synced is None:
        rep.add("clock", title, ERROR, "не удалось определить: нет timedatectl и chronyc или они не ответили",
                "проверьте руками: timedatectl status или chronyc tracking")
    elif not synced:
        rep.add("clock", title, WARN, "часы не синхронизированы (%s)%s" % (src, extra), hint)
    elif offset is not None and offset > 1.0:
        rep.add("clock", title, WARN, "синхронизированы, но смещение больше секунды (%s)%s" % (src, extra), hint)
    else:
        rep.add("clock", title, OK, "часы синхронизированы (%s)%s" % (src, extra))


# ---------------------------------------------------------------- бэкап

def list_files(root, pattern="*", max_depth=3, limit=200000):
    """Файлы под root (до max_depth уровней), подходящие под шаблон: [(mtime, size, path)]."""
    out, seen = [], 0
    root = os.path.abspath(root)
    base_depth = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath.rstrip(os.sep).count(os.sep) - base_depth >= max_depth:
            dirnames[:] = []
        for name in filenames:
            seen += 1
            if seen > limit:
                return out, True
            if not fnmatch.fnmatch(name, pattern):
                continue
            p = os.path.join(dirpath, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append((st.st_mtime, st.st_size, p))
    return out, False


def backup_verdict(files, now, max_age_h):
    """files: [(mtime, size, path)]. Возвращает (статус, подробности, совет)."""
    if not files:
        return CRIT, "в каталоге нет файлов бэкапа", "проверьте каталог и шаблон или сделайте бэкап до окна"
    files = sorted(files, reverse=True)
    mtime, size, path = files[0]
    age = now - mtime
    detail = "самый свежий: %s, %s, %s назад (%s)" % (
        os.path.basename(path), fmt_size(size), fmt_dur(age), fmt_ts(mtime))
    if size == 0:
        return CRIT, detail, "файл пустой: бэкап не получился"
    if age < -300:
        return WARN, detail, "время изменения файла в будущем: проверьте часы на сервере"
    if age > max_age_h * 3600:
        return CRIT, detail + "; это старше %s ч" % fmt_num(max_age_h), (
            "без свежего бэкапа откатиться нельзя: сделайте копию и проверьте, что она разворачивается")
    if age < 120:
        return WARN, detail, "файл менялся меньше двух минут назад: дождитесь, пока бэкап допишется"
    ext = os.path.splitext(path)[1]
    prev = [f for f in files[1:] if os.path.splitext(f[2])[1] == ext]
    if prev and prev[0][1] > 0 and size < 0.5 * prev[0][1]:
        return WARN, detail + "; предыдущий %s был %s" % (os.path.basename(prev[0][2]), fmt_size(prev[0][1])), (
            "свежий бэкап вдвое меньше предыдущего: проверьте, что он дописан и полный")
    return OK, detail, ""


def check_backup(rep, args, now):
    title = "Свежий бэкап базы"
    if not args.backup_dir:
        rep.add("backup", title, SKIP, "не задан --backup-dir")
        return
    if not os.path.isdir(args.backup_dir):
        rep.add("backup", title, ERROR, "нет каталога " + args.backup_dir)
        return
    files, cut = list_files(args.backup_dir, args.backup_pattern)
    status, detail, hint = backup_verdict(files, now, args.backup_max_age)
    if cut:
        detail += " (просмотрена только часть файлов: их слишком много)"
    rep.add("backup", title, status, detail, hint)
    if args.data_dir:
        if not os.path.exists(args.data_dir):
            rep.add("backup-place", "Бэкап на другом разделе", ERROR, "нет каталога " + args.data_dir)
        elif os.stat(args.data_dir).st_dev == os.stat(args.backup_dir).st_dev:
            rep.add("backup-place", "Бэкап на другом разделе", WARN,
                    "бэкап и %s на одной файловой системе" % args.data_dir,
                    "копия должна лежать на другом хранилище: сбой диска унесёт и данные, и бэкап")
        else:
            rep.add("backup-place", "Бэкап на другом разделе", OK,
                    "бэкап и %s на разных файловых системах" % args.data_dir)


# ---------------------------------------------------------------- Directum Launcher

# Тексты ошибок из справочника drxinfra.ru: (шаблон, что это, страница).
KNOWN_ERRORS = [
    (r"root action .{0,80} already exists", "остался архив directumrx прошлой версии",
     "directum-launcher-root-action-already-exists"),
    (r"Permission denied: '/var/tmp/_HostOsInfo'|Directum Launcher: Отказано в доступе",
     "нет прав у пользователя Directum Launcher", "directum-launcher-permission-denied"),
    (r"Couldn't find a valid ICU package", "нет библиотеки ICU", "directum-launcher-icu-package"),
    (r"[Оо]шибка с кодом 503", "сервисы не ответили (503)", "directum-launcher-error-503"),
    (r"ario/deploy\.yml does not exist", "нет deploy.yml для Ario", "ario-deploy-yml-does-not-exist"),
    (r"docker\.errors\.BuildError", "не собрался образ сервиса", "docker-rootless-builderror-user-id"),
    (r"Cannot create protocol \"?http\"? binding with port", "порт IIS занят", "iis-cannot-create-binding-port-80"),
    (r"' - FAILED", "не восстановлены внешние ключи", "rx-restore-fk-failed"),
    (r"Метаданные на сервере отличаются от метаданных из пакета", "разная прикладная разработка",
     "rx-settings-metadata-mismatch"),
    (r"Базовое соединение закрыто: Непредвиденная ошибка при при[её]ме", "пакет без базовых решений",
     "rx-publish-connection-closed-unexpected"),
    (r"Ошибка обработки элемента\. Тип сущности", "настройки по умолчанию без внешних ссылок",
     "rx-default-settings-process-parameter-name"),
    (r"DeploymentToolCore.{0,80}(?:exit|код возврата|return) ?code ?[1-8]\b", "DeploymentToolCore вернул ошибку",
     "deploymenttoolcore-exit-codes"),
    (r"no pg_hba\.conf entry", "база не пускает адрес сервиса", "postgresql-no-pg-hba-conf-entry"),
    (r"password authentication failed", "неверный пароль к базе", "postgresql-password-authentication-failed"),
    (r"Method \S+ not found|MissingMethodException", "версии сборок разошлись", "rx-method-not-found-in-type"),
]
KNOWN_RX = [(re.compile(p), what, slug) for p, what, slug in KNOWN_ERRORS]
LEVEL_RX = re.compile(r"\b(ERROR|CRITICAL|FATAL)\b|Traceback \(most recent call last\)")
TS_RX = re.compile(r"^\[?(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")
CERT_EXT = (".pfx", ".p12", ".pem", ".key", ".crt", ".cer", ".jks", ".keystore")
OLD_ARCHIVES = ("directumrx.tar.gz", "directumrx.zip")


def launcher_path_problems(path):
    """Справка 25.2: путь без пробелов, кириллицы, запятых и спецсимволов, общий путь до 256 символов."""
    problems = []
    if " " in path:
        problems.append("пробелы")
    if re.search("[А-Яа-яЁё]", path):
        problems.append("кириллица")
    if "," in path:
        problems.append("запятые")
    rest = re.sub(r"[A-Za-z0-9/._\-\\: ,А-Яа-яЁё]", "", path)
    if rest:
        problems.append("спецсимволы «%s»" % "".join(sorted(set(rest))))
    return problems


def longest_path(root, limit=300000):
    best, n = root, 0
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            n += 1
            p = os.path.join(dirpath, name)
            if len(p) > len(best):
                best = p
            if n >= limit:
                return best, True
    return best, False


def read_tail(path, max_bytes=5 * 1024 * 1024):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
            f.readline()
        return f.read().decode("utf-8", "replace")


SHORT_P_RX = re.compile(r"((?:^|\s)-p\s+)(\\\"(?:[^\"\\]|\\.)*?\\\"|\"[^\"]*\"|'[^']*'|\S+)")


def mask_line(line):
    """Маскировщик плюс короткий ключ пароля DeploymentToolCore: dt run --command="-n ... -p ПАРОЛЬ"."""
    return SHORT_P_RX.sub(lambda m: m.group(1) + "‹скрыто›", mask(line.strip()))[:240]


def scan_log_text(text):
    """Известные ошибки и строки уровня ERROR. Строки в ответе уже без паролей."""
    known, errors, last = {}, 0, []
    for line in text.splitlines():
        for rx, what, slug in KNOWN_RX:
            if rx.search(line):
                k = known.setdefault(slug, {"what": what, "slug": slug, "count": 0, "line": ""})
                k["count"] += 1
                k["line"] = mask_line(line)
        if LEVEL_RX.search(line[:120]):
            errors += 1
            last.append(mask_line(line))
    return {"known": list(known.values()), "errors": errors, "last": last[-3:]}


def last_session(text, gap_minutes=240):
    """Начало и конец последнего куска журнала. Пауза больше gap_minutes (4 ч) отделяет прошлый запуск:
    конвертация может долго молчать, а репетиция и окно обычно в разные дни."""
    stamps = []
    for line in text.splitlines():
        m = TS_RX.match(line)
        if m:
            try:
                stamps.append(datetime.datetime.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                continue
    if not stamps:
        return None
    start = stamps[0]
    for prev, cur in zip(stamps, stamps[1:]):
        if (cur - prev).total_seconds() > gap_minutes * 60:
            start = cur
    return start, stamps[-1]


def current_user():
    if os.environ.get("SUDO_USER"):
        return os.environ["SUDO_USER"]
    try:
        import pwd
        return pwd.getpwuid(os.geteuid()).pw_name
    except (ImportError, KeyError, AttributeError):
        return os.environ.get("USER") or os.environ.get("USERNAME") or "?"


def owner_name(path):
    try:
        uid = os.stat(path).st_uid
    except OSError:
        return None
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):
        return str(uid)


def check_launcher(rep, dl, post):
    title = "Папка Directum Launcher"
    if not os.path.isdir(dl):
        rep.add("launcher", title, ERROR, "нет каталога " + dl)
        return
    dl = os.path.abspath(dl)
    if post:
        check_launcher_logs(rep, dl)
        return
    problems = launcher_path_problems(dl)
    if problems:
        rep.add("launcher-path", "Путь к Directum Launcher", WARN, "%s: %s" % (dl, ", ".join(problems)),
                "справка требует путь без пробелов, кириллицы, запятых и спецсимволов, например /srv/DirectumLauncher")
    else:
        rep.add("launcher-path", "Путь к Directum Launcher", OK, dl)
    best, cut = longest_path(dl)
    st = WARN if len(best) > 256 else OK
    rep.add("launcher-longpath", "Длина путей в Directum Launcher", st,
            "самый длинный путь %d %s%s" % (len(best), plural(len(best), "символ", "символа", "символов"),
                                            " (просмотрена часть)" if cut else ""),
            "справка: общий путь к файлам не длиннее 256 символов; перенесите папку ближе к корню" if st == WARN else "")
    try:
        names = sorted(os.listdir(dl))
    except OSError as e:
        rep.add("launcher", title, ERROR, str(e))
        return
    old = [n for n in names if n.lower() in OLD_ARCHIVES]
    if old:
        rep.add("launcher-old", "Архив directumrx прошлой версии", WARN, ", ".join(old),
                "с 25.1 компонент называется base: старый архив даёт ошибку «root action ... already exists»; "
                "порядок исправления: " + SITE + "directum-launcher-root-action-already-exists.html")
    archives = [n for n in names if n.endswith((".tar.gz", ".zip")) and os.path.isfile(os.path.join(dl, n))]
    if archives:
        parts = []
        for n in archives[:12]:
            stt = os.stat(os.path.join(dl, n))
            parts.append("%s (%s, %s)" % (n, fmt_size(stt.st_size), fmt_ts(stt.st_mtime)))
        rep.add("launcher-archives", "Архивы в корне Directum Launcher", INFO, "; ".join(parts),
                "сверьте, что это архивы нужной версии и что контрольные суммы совпали с дистрибутивом")
    certs = [n for n in names if n.lower().endswith(CERT_EXT) and os.path.isfile(os.path.join(dl, n))]
    if certs:
        rep.add("launcher-files", "Свои файлы в папке Directum Launcher", WARN, ", ".join(certs[:10]),
                "скрипт обновления удаляет в папке всё, кроме etc и log: перенесите сертификаты и ключи в другое место")
    else:
        rep.add("launcher-files", "Свои файлы в папке Directum Launcher", OK, "сертификатов и ключей в корне нет")
    hostinfo = "/var/tmp/_HostOsInfo"
    if os.path.exists(hostinfo):
        own, me = owner_name(hostinfo), current_user()
        if own and own != me and own != "root":
            rep.add("launcher-hostinfo", "Файл " + hostinfo, WARN, "владелец %s, проверку запустил %s" % (own, me),
                    "если Directum Launcher запускается не под %s, будет «Permission denied»: %s" % (
                        own, SITE + "directum-launcher-permission-denied.html"))
        else:
            rep.add("launcher-hostinfo", "Файл " + hostinfo, OK, "владелец %s" % (own or "?"))
    check_icu(rep)


def check_icu(rep):
    if not sys.platform.startswith("linux"):
        return
    ldconfig = shutil.which("ldconfig") or next(
        (p for p in ("/sbin/ldconfig", "/usr/sbin/ldconfig") if os.path.exists(p)), None)
    if not ldconfig:
        rep.add("icu", "Библиотека ICU для Directum Launcher", SKIP, "нет ldconfig")
        return
    code, out, err = run([ldconfig, "-p"], timeout=15)
    if code != 0:
        rep.add("icu", "Библиотека ICU для Directum Launcher", ERROR, first_line(err) or "ldconfig -p не сработал")
    elif "libicu" in out:
        rep.add("icu", "Библиотека ICU для Directum Launcher", OK, "libicu есть")
    else:
        rep.add("icu", "Библиотека ICU для Directum Launcher", WARN, "libicu не найдена",
                "без неё Directum Launcher не запустится: " + SITE + "directum-launcher-icu-package.html")


def check_launcher_logs(rep, dl):
    found = False
    for name in ("current.log", "dl.log", "DBConverter.log"):
        p = os.path.join(dl, "log", name)
        if not os.path.isfile(p):
            continue
        found = True
        try:
            text = read_tail(p)
        except OSError as e:
            rep.add("dl-log:" + name, "Журнал " + name, ERROR, str(e))
            continue
        res = scan_log_text(text)
        parts = ["%s (%d): %s%s.html" % (k["what"], k["count"], SITE, k["slug"]) for k in res["known"]]
        if res["known"]:
            rep.add("dl-log:" + name, "Журнал " + name, WARN,
                    "известные ошибки: " + "; ".join(parts) + ". Последняя: " + res["known"][-1]["line"],
                    "разберите по ссылке; если ошибка была на конвертации базы, перед повтором восстановите базу из бэкапа")
        elif res["errors"]:
            rep.add("dl-log:" + name, "Журнал " + name, WARN,
                    "строк с ошибками: %d. Последние: %s" % (res["errors"], " | ".join(res["last"])),
                    "прочитайте строки вокруг; обновление могло пройти после «Повторить», но причину стоит понять")
        else:
            rep.add("dl-log:" + name, "Журнал " + name, OK, "ошибок не найдено")
        if name in ("dl.log", "DBConverter.log"):
            sess = last_session(text)
            if sess:
                what = "обновление" if name == "dl.log" else "конвертация базы"
                rep.add("dl-time:" + name, "Длительность по " + name, INFO, "%s: %s – %s (%s)" % (
                    what, sess[0].strftime("%d.%m %H:%M"), sess[1].strftime("%d.%m %H:%M"),
                    fmt_dur((sess[1] - sess[0]).total_seconds())),
                    "оценка по отметкам времени: пауза в журнале больше 4 часов считается границей между "
                    "запусками; запишите в план окна")
    if not found:
        rep.add("dl-log", "Журналы Directum Launcher", ERROR, "нет файлов в %s" % os.path.join(dl, "log"))


# ---------------------------------------------------------------- статус-файлы сервисов

def split_status_name(fname):
    """'<компьютер>.<сервис>.status' -> (компьютер, сервис)."""
    base = fname[:-len(".status")] if fname.endswith(".status") else fname
    host, _, svc = base.rpartition(".")
    return (host, svc) if host else ("", base)


def _find_key(obj, needle):
    """Значение первого ключа, в имени которого (без регистра и знаков) есть needle."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if needle in re.sub(r"[^a-z]", "", str(k).lower()):
                return v
            r = _find_key(v, needle)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, needle)
            if r is not None:
                return r
    return None


def _strings(obj):
    if isinstance(obj, dict):
        return [x for v in obj.values() for x in _strings(v)]
    if isinstance(obj, list):
        return [x for v in obj for x in _strings(v)]
    return [obj] if isinstance(obj, str) else []


def _bad_checks(statuses):
    bad = []
    if isinstance(statuses, dict):
        for name, v in statuses.items():
            s = v.get("Status") or v.get("status") if isinstance(v, dict) else v
            if isinstance(s, str) and s.lower() != "healthy":
                bad.append("%s %s" % (name, s))
    return bad


def parse_status_text(text):
    """Статус-файл сервиса RX (JSON) -> общий статус, готовность, проблемные проверки, состояние разработки."""
    data = json.loads(text.lstrip("\ufeff"))
    if not isinstance(data, dict):
        raise ValueError("не объект JSON")
    status = data.get("Status") or data.get("status")
    dp = _find_key(data, "deploymentpackage")
    if isinstance(dp, (dict, list)):
        # справка называет значения Actual и Not actual; где они лежат внутри, не описано
        found = _strings(dp)
        dp = next((x for x in found if "actual" in x.lower()), None) or json.dumps(dp, ensure_ascii=False)[:80]
    return {"status": str(status).lower() if status else None, "ready": data.get("IsReady"),
            "bad": _bad_checks(data.get("Statuses") or data.get("statuses")),
            "deployment_package": str(dp) if dp is not None else None}


def status_dir_verdict(entries, now, post, stale_s=120):
    """entries: [(сервис, mtime, разобранный файл или None, ошибка)] по одному файлу на экземпляр."""
    by_svc = {}
    for svc, mtime, parsed, err in entries:
        by_svc.setdefault(svc, []).append((mtime, parsed, err))
    worst, lines, healthy, notes, dps = OK, [], 0, [], []
    rank = {OK: 0, WARN: 1, CRIT: 2}
    for svc in sorted(by_svc, key=str.lower):
        items = sorted(by_svc[svc], key=lambda x: -x[0])
        mtime, parsed, err = items[0]
        old = [i for i in items[1:] if now - i[0] > stale_s]
        if old:
            notes.append("%s: ещё %d %s давно не обновлял%s" % (
                svc, len(old), plural(len(old), "файл", "файла", "файлов"), "ся" if len(old) == 1 else "ись"))
        age = now - mtime
        if age > stale_s:
            st, text = (CRIT if post else WARN), "%s: файл не обновлялся %s" % (svc, fmt_dur(age))
        elif err:
            st, text = WARN, "%s: не разобрать (%s)" % (svc, err)
        elif parsed["status"] in ("unhealthy", "degraded"):
            st = CRIT if parsed["status"] == "unhealthy" else WARN
            text = "%s %s" % (svc, parsed["status"]) + (" (%s)" % ", ".join(parsed["bad"]) if parsed["bad"] else "")
        elif parsed["status"] == "healthy":
            st, text = OK, ""
            healthy += 1
        else:
            st, text = WARN, "%s: статус %s" % (svc, parsed["status"])
        if parsed and parsed.get("deployment_package"):
            dps.append((svc, parsed["deployment_package"]))
        if text:
            lines.append(text.strip())
        if rank[st] > rank[worst]:
            worst = st
    detail = "сервисов %d, healthy %d" % (len(by_svc), healthy)
    if lines:
        detail += "; " + "; ".join(lines)
    if notes:
        detail += ". Старые экземпляры: " + "; ".join(notes[:5])
    return worst, detail, dps


def check_status_dir(rep, d, now, post):
    title = "Статус-файлы сервисов RX"
    if not os.path.isdir(d):
        rep.add("status-files", title, ERROR, "нет каталога " + d)
        return
    entries = []
    for name in sorted(os.listdir(d)):
        if not name.endswith(".status"):
            continue
        p = os.path.join(d, name)
        mtime, parsed, err = 0, None, None
        try:
            mtime = os.stat(p).st_mtime
            with open(p, "rb") as f:
                parsed = parse_status_text(f.read(1024 * 1024).decode("utf-8", "replace"))
        except (OSError, ValueError) as e:
            err = str(e)[:80]
        entries.append((split_status_name(name)[1], mtime, parsed, err))
    if not entries:
        rep.add("status-files", title, WARN, "в %s нет файлов *.status" % d,
                "статус-файлы лежат в подпапке status папки журналов (LOGS_PATH); их могли отключить в конфигураторе")
        return
    status, detail, dps = status_dir_verdict(entries, now, post)
    hint = "" if status == OK else ("откройте статус-файл сервиса: в Statuses видно, какая проверка не прошла; "
                                    "файл, который не обновляется, значит, что сервис не работает")
    rep.add("status-files", title, status, detail, hint)
    for svc, dp in dps:
        not_actual = "not actual" in dp.lower()
        rep.add("deployment-package:" + svc, "Прикладная разработка на %s" % svc,
                WARN if (not_actual and post) else INFO, "DeploymentPackage: %s" % dp,
                "разработка на веб-сервере не совпадает с опубликованным пакетом" if not_actual else "")


# ---------------------------------------------------------------- контейнеры

PS_FORMAT = "{{.Names}}\t{{.Image}}\t{{.Status}}"
RUNNING = ("running", "starting", "unhealthy", "paused", "restarting")


def container_state(status):
    low = status.strip().lower()
    if low.startswith("up"):
        for word in ("unhealthy", "health: starting", "paused"):
            if "(%s)" % word in low:
                return {"health: starting": "starting"}.get(word, word)
        return "running"
    if low.startswith("restarting"):
        return "restarting"
    m = re.match(r"exited \((-?\d+)\)", low)
    if m:
        return "exited:" + m.group(1)
    for word in ("created", "dead", "removing", "paused"):
        if low.startswith(word):
            return word
    return "other"


def parse_ps(text, runtime):
    rows = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        rows.append({"runtime": runtime, "name": parts[0], "image": parts[1], "image_id": "",
                     "status": parts[2], "state": container_state(parts[2])})
    return rows


def containers_verdict(rows, post):
    def pick(*states):
        return [r["name"] for r in rows if r["state"] in states]
    running = pick(*RUNNING)
    restarting, dead = pick("restarting"), pick("dead")
    unhealthy, starting = pick("unhealthy"), pick("starting")
    bad_exit = ["%s (%s)" % (r["name"], r["state"].split(":")[1]) for r in rows
                if r["state"].startswith("exited:") and r["state"] != "exited:0"]
    detail = "запущено %d из %d" % (len(running), len(rows))
    if restarting or dead:
        return CRIT, detail + "; перезапускаются или упали: " + ", ".join((restarting + dead)[:10])
    if unhealthy:
        return (CRIT if post else WARN), detail + "; unhealthy: " + ", ".join(unhealthy[:10])
    if post and not running:
        return CRIT, detail + "; ни одного запущенного контейнера"
    if bad_exit:
        return WARN, detail + "; завершились с ошибкой: " + ", ".join(bad_exit[:10])
    if starting:
        return (WARN if post else OK), detail + "; ещё запускаются: " + ", ".join(starting[:10])
    return OK, detail


def runtime_root(rt):
    fmt = "{{.DockerRootDir}}" if rt == "docker" else "{{.Store.GraphRoot}}"
    code, out, _ = run([rt, "info", "--format", fmt], timeout=15)
    path = out.strip()
    return path if code == 0 and path and os.path.isdir(path) else None


def check_runtime(rep, rt, post):
    title = "Контейнеры " + rt
    code, out, err = run([rt, "ps", "-a", "--format", PS_FORMAT], timeout=20)
    if code != 0:
        msg = first_line(err) or first_line(out) or "команда не выполнилась"
        if "denied" in msg.lower():
            hint = "запустите от пользователя, под которым работают контейнеры RX, или через sudo"
        else:
            hint = "проверьте, что служба %s работает; если контейнеров RX на этом узле нет, добавьте --no-containers" % rt
        rep.add("containers:" + rt, title, ERROR, msg, hint)
        return
    rows = parse_ps(out, rt)
    if not rows:
        rep.add("containers:" + rt, title, CRIT if post else INFO, "контейнеров нет")
        return
    names = [r["name"] for r in rows if r["state"] in RUNNING]
    if names:
        code, out2, _ = run([rt, "inspect", "--format", "{{.Name}}\t{{.Image}}"] + names[:200], timeout=20)
        if code == 0:
            ids = dict(line.lstrip("/").split("\t", 1) for line in out2.splitlines() if "\t" in line)
            for r in rows:
                r["image_id"] = ids.get(r["name"], "")
    status, detail = containers_verdict(rows, post)
    hint = "" if status == OK else "журнал контейнера: %s logs --tail 200 ИМЯ (через маскировщик)" % rt
    rep.add("containers:" + rt, title, status, detail, hint)
    live = [r for r in rows if r["state"] in RUNNING]
    rep.inventory["containers"].extend(live)
    images = sorted(set(r["image"] for r in live))
    if images:
        rep.add("images:" + rt, "Образы в работе (%s)" % rt, INFO, "%d: %s%s" % (
            len(images), ", ".join(images[:12]), " и ещё %d" % (len(images) - 12) if len(images) > 12 else ""),
            "номер версии обычно виден в тегах образов")


BAD_WAITING = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError",
               "CreateContainerError", "InvalidImageName", "RunContainerError"}


def pod_base_name(meta):
    owners = meta.get("ownerReferences") or []
    if owners:
        o = owners[0]
        name = o.get("name", "")
        h = (meta.get("labels") or {}).get("pod-template-hash")
        if o.get("kind") == "ReplicaSet" and h and name.endswith("-" + h):
            return name[:-len(h) - 1]
        return name
    return meta.get("name", "")


def parse_pods(data):
    pods = []
    for item in data.get("items", []) or []:
        meta, spec, st = item.get("metadata", {}), item.get("spec", {}), item.get("status", {})
        cstat = dict((c.get("name"), c) for c in (st.get("containerStatuses") or []))
        containers = []
        for c in spec.get("containers") or []:
            cs = cstat.get(c.get("name"), {})
            waiting = ((cs.get("state") or {}).get("waiting") or {}).get("reason")
            last = ((cs.get("lastState") or {}).get("terminated") or {}).get("reason")
            containers.append({"name": c.get("name"), "image": c.get("image", ""), "image_id": cs.get("imageID", ""),
                               "ready": bool(cs.get("ready")), "restarts": cs.get("restartCount", 0) or 0,
                               "waiting": waiting, "last_terminated": last})
        pods.append({"name": meta.get("name", ""), "namespace": meta.get("namespace", ""),
                     "base": pod_base_name(meta), "phase": st.get("phase", ""), "containers": containers})
    return pods


def pods_verdict(pods, post):
    crit, notready, oom, restarts = [], [], [], 0
    for p in pods:
        if p["phase"] == "Succeeded":
            continue
        cs = p["containers"]
        restarts += sum(c["restarts"] for c in cs)
        reasons = [c["waiting"] for c in cs if c["waiting"] in BAD_WAITING]
        if p["phase"] in ("Failed", "Unknown") or reasons:
            crit.append("%s (%s)" % (p["name"], ", ".join(reasons) or p["phase"]))
        elif p["phase"] == "Pending" or not all(c["ready"] for c in cs):
            notready.append(p["name"])
        if any(c["last_terminated"] == "OOMKilled" for c in cs):
            oom.append(p["name"])
    active = [p for p in pods if p["phase"] != "Succeeded"]
    detail = "подов %d, перезапусков всего %d" % (len(active), restarts)
    if crit:
        return CRIT, detail + "; не работают: " + ", ".join(crit[:10])
    if post and not active:
        return CRIT, detail + "; подов нет"
    if notready:
        return WARN, detail + "; не готовы: " + ", ".join(notready[:10])
    if oom:
        return WARN, detail + "; были OOMKilled: " + ", ".join(oom[:10])
    return OK, detail


def check_k8s(rep, namespaces, post):
    if not shutil.which("kubectl"):
        if namespaces:
            rep.add("pods", "Поды Kubernetes", ERROR, "задан --namespace, но kubectl не найден")
        return
    if not namespaces:
        rep.add("pods", "Поды Kubernetes", SKIP, "kubectl есть, но не задан --namespace с RX")
        return
    for ns in namespaces:
        title = "Поды Kubernetes, namespace " + ns
        code, out, err = run(["kubectl", "get", "pods", "-n", ns, "-o", "json", "--request-timeout=20s"], timeout=30)
        if code != 0:
            rep.add("pods:" + ns, title, ERROR, first_line(err) or "kubectl не ответил",
                    "проверьте контекст: kubectl config current-context")
            continue
        try:
            pods = parse_pods(json.loads(out))
        except ValueError as e:
            rep.add("pods:" + ns, title, ERROR, "не разобрать ответ kubectl: %s" % e)
            continue
        status, detail = pods_verdict(pods, post)
        hint = "" if status == OK else "kubectl describe pod ИМЯ -n %s и kubectl logs ИМЯ -n %s --previous" % (ns, ns)
        rep.add("pods:" + ns, title, status, detail, hint)
        rep.inventory["pods"].extend(pods)
        images = sorted(set(c["image"] for p in pods for c in p["containers"]))
        if images:
            rep.add("images:k8s:" + ns, "Образы в работе (%s)" % ns, INFO, "%d: %s%s" % (
                len(images), ", ".join(images[:12]), " и ещё %d" % (len(images) - 12) if len(images) > 12 else ""))


def check_containers(rep, runtimes, namespaces, post):
    if not runtimes and not shutil.which("kubectl"):
        rep.add("containers", "Контейнеры", SKIP, "docker, podman и kubectl не найдены")
    for rt in runtimes:
        check_runtime(rep, rt, post)
    check_k8s(rep, namespaces, post)


# ---------------------------------------------------------------- HTTP

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_probe(url, timeout=10.0, insecure=False):
    """GET без перенаправлений и без прокси из окружения. Тело читается до 64 КБ."""
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                         urllib.request.HTTPSHandler(context=ctx), _NoRedirect())
    res = {"url": url, "code": None, "ms": None, "error": None, "tls": False,
           "date": None, "location": None, "body": ""}
    t0 = time.time()
    resp = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "rxops-preflight/" + VERSION,
                                                   "Accept": "application/json, */*"})
        resp = opener.open(req, timeout=timeout)
    except ValueError as e:
        res["error"] = "неверный адрес: %s" % e
    except urllib.error.HTTPError as e:
        resp = e
    except urllib.error.URLError as e:
        reason = e.reason
        res["tls"] = isinstance(reason, ssl.SSLError) or "CERTIFICATE" in str(reason).upper()
        res["error"] = str(reason)
    except ssl.SSLError as e:
        res["tls"], res["error"] = True, str(e)
    except (socket.timeout, OSError) as e:
        res["error"] = str(e) or e.__class__.__name__
    res["ms"] = int((time.time() - t0) * 1000)
    if resp is not None:
        res["code"] = resp.getcode() if hasattr(resp, "getcode") else getattr(resp, "code", None)
        headers = getattr(resp, "headers", None)
        if headers is not None:
            res["date"], res["location"] = headers.get("Date"), headers.get("Location")
        try:
            res["body"] = resp.read(65536).decode("utf-8", "replace")
        except Exception:  # тело может быть недоступно у ответов с ошибкой
            res["body"] = ""
    return res


def parse_health_body(body):
    """JSON проверки работоспособности -> {'status', 'bad'} или None, если это не JSON."""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    status = data.get("Status") or data.get("status")
    bad = _bad_checks(data.get("Statuses") or data.get("statuses") or data.get("entries"))
    return {"status": str(status).lower() if status else None, "bad": bad}


def classify_http(res):
    """Результат http_probe -> (статус, подробности, совет)."""
    if res["error"]:
        err = res["error"]
        if res["tls"]:
            return CRIT, "ошибка TLS: " + err, ("сертификат истёк, не тот или цепочка неполная (openssl s_client "
                                               "-connect ХОСТ:443 -servername ХОСТ); для узла с самоподписанным "
                                               "сертификатом можно --insecure")
        if "refused" in err.lower():
            return CRIT, "соединение отклонено: " + err, "сервис не слушает порт: запущен ли веб-сервер или прокси"
        if "timed out" in err.lower() or "timeout" in err.lower():
            return CRIT, "нет ответа за отведённое время", "проверьте сеть, прокси и нагрузку на узел"
        return CRIT, err, "проверьте адрес и имя в DNS"
    code, ms = res["code"], res["ms"]
    detail = "HTTP %s за %d мс" % (code, ms)
    if code is not None and 200 <= code < 300:
        health = parse_health_body(res["body"])
        if health and health["status"] == "unhealthy":
            return CRIT, detail + ", Status unhealthy " + ", ".join(health["bad"]), "смотрите статус-файлы и журналы сервиса"
        if health and health["status"] == "degraded":
            return WARN, detail + ", Status degraded " + ", ".join(health["bad"]), "какая проверка degraded, видно в ответе"
        if health and health["status"]:
            detail += ", Status " + health["status"]
        if ms > 5000:
            return WARN, detail, "ответ дольше 5 с: узел перегружен или ждёт зависимость"
        return OK, detail, ""
    if code in (301, 302, 303, 307, 308):
        return WARN, detail + ", перенаправление на " + (res["location"] or "?"), (
            "адрес проверки не должен перенаправлять: уточните путь (часто это страница входа)")
    if code == 503:
        return CRIT, detail, ("503: сервис недоступен; сразу после обновления так бывает, пока сервисы не запустились, "
                              "повторите через пару минут")
    if code == 403:
        return WARN, detail, ("доступ закрыт; справка советует закрывать адрес проверки на внешнем прокси, "
                              "проверяйте узел напрямую")
    if code == 401:
        return WARN, detail, "адрес требует входа: для проверки работоспособности нужен другой путь"
    if code == 404:
        return WARN, detail, "такого пути нет: сверьте адрес со справкой своей версии"
    if code is not None and code >= 500:
        return CRIT, detail, "ошибка на стороне сервера или прокси: смотрите журналы веб-сервера"
    return WARN, detail, ""


def clock_skew(date_header, now_ts):
    """Разница часов сервера (заголовок Date) и этой машины, секунды, или None."""
    if not date_header:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(date_header)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp() - now_ts


def check_urls(rep, urls, timeout, insecure):
    if not urls:
        rep.add("url", "Адрес проверки работоспособности", SKIP, "не задан --url")
        return
    for url in urls:
        res = http_probe(url, timeout, insecure)
        status, detail, hint = classify_http(res)
        rep.add("url:" + url, "Адрес " + url, status, detail, hint)
        skew = clock_skew(res["date"], time.time())
        if skew is not None and abs(skew) > 60:
            rep.add("clock-skew:" + url, "Часы сервера " + url, WARN,
                    "%s этой машины на %s" % ("спешат относительно" if skew > 0 else "отстают от", fmt_dur(skew)),
                    "сверьте синхронизацию времени на узлах RX: Kerberos допускает расхождение до 5 минут")


# ---------------------------------------------------------------- сравнение со снимком «до»

def inventory_map(inv):
    m = {}
    for c in inv.get("containers", []) or []:
        m["%s:%s" % (c.get("runtime"), c.get("name"))] = (c.get("image", ""), c.get("image_id", ""))
    for p in inv.get("pods", []) or []:
        for c in p.get("containers", []) or []:
            key = "k8s:%s/%s/%s" % (p.get("namespace"), p.get("base"), c.get("name"))
            m[key] = (c.get("image", ""), c.get("image_id", ""))
    return m


def diff_inventory(before, now):
    b, n = inventory_map(before), inventory_map(now)
    common = sorted(set(b) & set(n))
    return {"changed": [(k, b[k][0], n[k][0]) for k in common if b[k][0] != n[k][0]],
            "rebuilt": [k for k in common if b[k][0] == n[k][0] and b[k][1] and n[k][1] and b[k][1] != n[k][1]],
            "gone": sorted(set(b) - set(n)), "new": sorted(set(n) - set(b))}


def check_before(rep, path, post):
    title = "Сравнение со снимком «до»"
    try:
        with open(path, encoding="utf-8") as f:
            before = json.load(f)
        inv = before["inventory"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        rep.add("before", title, ERROR, "не прочитать %s: %s" % (path, e), "нужен вывод этого скрипта с --json")
        return
    if not inventory_map(inv):
        rep.add("before", title, SKIP, "в снимке нет контейнеров и подов")
        return
    d = diff_inventory(inv, rep.inventory)
    where = " (снимок с узла %s)" % before.get("host") if before.get("host") not in (None, socket.gethostname()) else ""
    parts = []
    if d["changed"]:
        parts.append("поменялись: " + "; ".join("%s %s → %s" % (k, a, b) for k, a, b in d["changed"][:15]))
    if d["rebuilt"]:
        parts.append("тот же тег, другой образ: " + ", ".join(d["rebuilt"][:10]))
    if d["new"]:
        parts.append("новые: " + ", ".join(d["new"][:10]))
    if d["gone"]:
        parts.append("были, а сейчас не запущены: " + ", ".join(d["gone"][:10]))
    if d["gone"]:
        rep.add("before", title, WARN, "; ".join(parts) + where, "проверьте, должны ли эти сервисы работать в новой версии")
    elif post and not d["changed"] and not d["rebuilt"]:
        rep.add("before", title, WARN, "ни один образ не поменялся" + where,
                "обновление не дошло до сервисов, или снимок сделан уже после обновления")
    else:
        rep.add("before", title, INFO, ("; ".join(parts) or "изменений нет") + where)


# ---------------------------------------------------------------- ручные проверки

MANUAL_PRE = [
    "Версии сейчас и цель: платформа и прикладная разработка («О программе» в веб-клиенте, Directum Launcher, "
    "теги образов). Напрямую обновляются только с трёх последних версий, с более старых поэтапно.",
    "Требования новой версии к ОС, СУБД, RabbitMQ и Elasticsearch сверены с документом вендора "
    "«Типовые требования к аппаратному и программному обеспечению».",
    "Пакеты заказной разработки собраны на новой версии и прошли тестовый контур; все пакеты под рукой, "
    "публикуются за один раз.",
    "Копии config.yml, haproxy.cfg (если меняли) и папки данных RabbitMQ (если ставили скриптами "
    "развёртывания) лежат вне папки Directum Launcher.",
    "Записаны точка невозврата, условия отката и время отката; назначен один человек, который решает.",
    "Пользователи предупреждены; состояние режима обслуживания: ./do.sh platform get_maintenance_mode_status.",
    "Дистрибутив скачан, контрольные суммы совпали; доступы к серверам, базе и паролю шифрования config.yml проверены.",
    "В окне бэкап базы делается после остановки сервисов; после него повторите этот скрипт с --backup-max-age 1.",
]
MANUAL_POST = [
    "Вход в веб-клиент тем способом, каким входят пользователи, включая единый вход.",
    "В «О программе» новые версии платформы и прикладной разработки; в Directum Launcher, режим «Состояние», "
    "нет unhealthy.",
    "Задание создаётся, отправляется и приходит исполнителю; документ открывается, новая версия сохраняется.",
    "Поиск находит документ, созданный после обновления.",
    "Очереди RabbitMQ разбираются, у каждой есть потребитель.",
    "Интеграции отвечают; лицензии на месте; мониторинг видит новую версию, алерты не молчат и не шумят.",
    "Задачи, прекращённые с «Method not found ... get_Id()», возобновлены.",
    "Режим обслуживания выключен только после проверок; первые сутки смотрим время ответа, очереди и журналы.",
]


# ---------------------------------------------------------------- вывод

def render_text(rep, host, stamp):
    mode = "после обновления" if rep.mode == "post" else "перед обновлением"
    out = ["rxops %s %s · %s · %s · %s" % (TOOL, VERSION, mode, host, stamp), ""]
    for c in rep.checks:
        line = "[%-5s] %s" % (c["status"], c["title"])
        if c["detail"]:
            line += ": " + c["detail"]
        out.append(line)
        if c["hint"] and c["status"] not in (OK, SKIP):
            out.append("        → " + c["hint"])
    s = rep.summary()
    out += ["", "Итог: " + ", ".join("%s %d" % (k, s[k]) for k in ORDER), "", "Проверьте руками:"]
    out += ["  %d. %s" % (i, t) for i, t in enumerate(rep.manual, 1)]
    return "\n".join(out) + "\n"


def to_json(rep, host, stamp):
    return json.dumps({"tool": TOOL, "version": VERSION, "host": host, "time": stamp, "mode": rep.mode,
                       "checks": rep.checks, "summary": rep.summary(), "inventory": rep.inventory,
                       "manual": rep.manual}, ensure_ascii=False, indent=2) + "\n"


def build_parser():
    p = argparse.ArgumentParser(
        prog="preflight.py", formatter_class=argparse.RawDescriptionHelpFormatter, add_help=False,
        description="Проверки перед обновлением Directum RX и после него. Только чтение, вывод в stdout.",
        epilog="Код возврата: 0 норма, 1 есть WARN или CRIT, 2 часть проверок не выполнилась (ERROR).\n"
               "Пример: python3 preflight.py --path /srv=40 --backup-dir /backup/pg "
               "--launcher-dir /srv/DirectumLauncher --url https://rx.example.com/Client/api/health")
    p.add_argument("-h", "--help", action="help", help="показать эту справку")
    p.add_argument("--post", action="store_true", help="проверки после обновления: сервисы уже должны работать")
    p.add_argument("--json", action="store_true", help="вывод в JSON, например для снимка «до»")
    p.add_argument("--before", metavar="ФАЙЛ", help="JSON прошлого запуска: какие образы и теги поменялись")
    p.add_argument("--path", action="append", default=[], metavar="ПУТЬ[=ГБ]",
                   help="место на диске; после = сколько ГБ нужно свободными (можно несколько раз)")
    p.add_argument("--backup-dir", metavar="КАТАЛОГ", help="каталог с бэкапами базы: самый свежий файл")
    p.add_argument("--backup-pattern", default="*", metavar="ШАБЛОН", help="шаблон имени бэкапа, например '*.dump'")
    p.add_argument("--backup-max-age", type=float, default=26.0, metavar="ЧАСЫ",
                   help="самый свежий бэкап не старше стольких часов (по умолчанию 26)")
    p.add_argument("--data-dir", metavar="КАТАЛОГ", help="данные базы или хранилища: бэкап должен быть на другом разделе")
    p.add_argument("--url", action="append", default=[], metavar="URL",
                   help="адрес проверки работоспособности или веб-клиента (можно несколько раз)")
    p.add_argument("--insecure", action="store_true", help="не проверять сертификат TLS (самоподписанный на узле)")
    p.add_argument("--timeout", type=float, default=10.0, metavar="СЕК", help="таймаут HTTP-запроса (по умолчанию 10)")
    p.add_argument("--status-dir", metavar="КАТАЛОГ", help="статус-файлы сервисов RX: подпапка status в LOGS_PATH")
    p.add_argument("--launcher-dir", metavar="КАТАЛОГ", help="папка Directum Launcher")
    p.add_argument("-n", "--namespace", action="append", default=[], metavar="NS",
                   help="namespace Kubernetes с RX для kubectl (можно несколько раз)")
    p.add_argument("--no-containers", action="store_true", help="не смотреть docker, podman и kubectl")
    p.add_argument("--version", action="version", version="%(prog)s " + VERSION, help="показать версию")
    return p


def run_checks(args, now=None):
    now = time.time() if now is None else now
    rep = Report("post" if args.post else "pre")
    runtimes = [] if args.no_containers else [r for r in ("docker", "podman") if shutil.which(r)]
    disks = [parse_path_arg(p) + ("",) for p in args.path] or [(os.path.abspath(os.sep), None, "")]
    devices = set()
    for path, _, _ in disks:
        try:
            devices.add(os.stat(path).st_dev)
        except OSError:
            pass
    for rt in runtimes:
        root = runtime_root(rt)
        if root and os.stat(root).st_dev not in devices:  # тот же раздел уже в списке
            disks.append((root, None, rt))
    check_disks(rep, disks)
    check_memory(rep)
    check_clock(rep)
    check_backup(rep, args, now)
    if args.launcher_dir:
        check_launcher(rep, args.launcher_dir, args.post)
    else:
        rep.add("launcher", "Папка Directum Launcher", SKIP, "не задан --launcher-dir")
    if args.status_dir:
        check_status_dir(rep, args.status_dir, now, args.post)
    else:
        rep.add("status-files", "Статус-файлы сервисов RX", SKIP, "не задан --status-dir")
    if args.no_containers:
        rep.add("containers", "Контейнеры", SKIP, "отключено --no-containers")
    else:
        check_containers(rep, runtimes, args.namespace, args.post)
    check_urls(rep, args.url, args.timeout, args.insecure)
    if args.before and args.no_containers:
        rep.add("before", "Сравнение со снимком «до»", SKIP, "контейнеры в этом запуске не смотрели (--no-containers)")
    elif args.before:
        check_before(rep, args.before, args.post)
    rep.manual = list(MANUAL_POST if args.post else MANUAL_PRE)
    return rep


def _utf8_streams():
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if enc != "utf8" and hasattr(stream, "buffer"):
            setattr(sys, name, io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace",
                                                line_buffering=True))


def main(argv=None):
    _utf8_streams()
    args = build_parser().parse_args(argv)
    rep = run_checks(args)
    host = socket.gethostname()
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    sys.stdout.write(to_json(rep, host, stamp) if args.json else render_text(rep, host, stamp))
    return rep.exit_code()


if __name__ == "__main__":
    sys.exit(main())
