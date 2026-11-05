#!/usr/bin/env python3
"""Инвентаризация резервных копий Directum RX на этом сервере. Только чтение.

Использование:
  sudo python3 backup_inventory.py --dir /backup/pg --dir /backup/storage
  sudo python3 backup_inventory.py --dir /backup --max-age 26 --json
  python3 backup_inventory.py --launcher-dir /srv/DirectumLauncher
  python3 backup_inventory.py --psql-cmd "docker exec -i rx-postgres psql -U postgres"

Что смотрит:
  каталоги копий (--dir): свежесть и размер последних копий по группам имён, внезапно
    уменьшившиеся копии, оборванные копии под временными именами, архив WAL (дыры,
    свежесть), каталоги pg_probackup; на каком разделе лежат копии и не там ли, где база;
  расписания: cron и таймеры systemd с pg_dump, pg_basebackup, pg_probackup, pgbackrest,
    wal-g, mc mirror, rclone, restic, rsync, а также наборы файлов Bacula;
  pg_probackup show и pgbackrest info, если программы установлены;
  архивацию WAL со стороны PostgreSQL через psql, если база доступна;
  config.yml Directum Launcher: где хранилище документов и сертификаты и видно ли их копирование.
В конце печатается список того, что скрипт проверить не может.

Без --dir скрипт берёт каталоги, которые упоминаются в найденных заданиях, и /backup.
Код возврата: 0 норма, 1 найдено отклонение (WARN или CRIT), 2 не удалось ничего проверить.
Только стандартная библиотека Python 3.6+.
"""
import argparse
import fnmatch
import glob
import json
import os
import re
import shlex
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
try:
    import mask_secrets as _ms  # копия shared/mask_secrets.py рядом со скриптом
except ImportError:  # pragma: no cover
    _ms = None

VERSION = "0.1.0"
MIB, GIB = 1024 ** 2, 1024 ** 3
RANK = {"SKIP": 0, "OK": 1, "INFO": 2, "WARN": 3, "CRIT": 4}

WAL_RE = re.compile(r"^([0-9A-F]{8})([0-9A-F]{8})([0-9A-F]{8})(\.partial)?(\.(?:gz|zst|zstd|lz4|xz|bz2|br|lzo))?$")
LABEL_RE = re.compile(r"^([0-9A-F]{24})\.[0-9A-F]{8}\.backup(\.\w+)?$")
HISTORY_RE = re.compile(r"^[0-9A-F]{8}\.history(\.\w+)?$")
TEMP_RE = re.compile(r"(?i)(\.inprogress|\.in-progress|\.tmp|\.temp|\.part|\.partial|~)$|^\..*(inprogress|tmp)")
SKIP_NAMES = {"lost+found", ".snapshot", ".zfs", "@eaDir", "__pycache__"}
NETWORK_FS = {"nfs", "nfs4", "cifs", "smb3", "smbfs", "fuse.sshfs", "glusterfs", "fuse.glusterfs", "ceph",
              "fuse.s3fs", "fuse.rclone", "fuse.goofys", "9p", "davfs", "fuse.davfs2", "virtiofs"}

# Что ищем в расписаниях и скриптах: (шаблон, вид копии).
KEYWORDS = [
    (r"\bpg_dumpall\b", "база"), (r"\bpg_dump\b", "база"), (r"\bpg_basebackup\b", "база"),
    (r"\bpg_probackup(-\d+)?\b", "база"), (r"\bpgbackrest\b", "база"), (r"\bwal-g\b", "база"),
    (r"\bbarman\b", "база"), (r"\bpg_receivewal\b", "база"),
    (r"\bmc(?:li)? (mirror|cp)\b", "файлы"), (r"\brclone\b", "файлы"), (r"\brestic\b", "файлы"),
    (r"\bborg\b", "файлы"), (r"\brsync\b", "файлы"), (r"\bbconsole\b", "файлы"),
    (r"(?i)\bbackup\b|бэкап|резервн", "неясно"),
]
KW_RE = [(re.compile(p), kind) for p, kind in KEYWORDS]

PATH_FLAGS = re.compile(r"(?:^|\s)(?:-B|-D|-f|--backup-path|--dir|--file|--repo|--pgdata|--target)[= ]+([^\s;|&]+)")
ABS_PATH = re.compile(r"(?<![\w.:-])(/[\w.@%+\-/]+)")


# --- Вспомогательное ---

FALLBACK = re.compile(r"(?i)\b([\w.\-]*(?:password|passwd|pwd|secret|token|access_key|secret_key)[\w.\-]*\s*[=:]\s*)([^\s;'\"]+)")
# Присваивание переменной внутри команды: cd /x && PGPASSWORD=... pg_dump, env AWS_SECRET_ACCESS_KEY=... wal-g
INLINE_ENV = re.compile(r"(?<![\w$])([A-Za-z_][A-Za-z0-9_]*(?:PASSWORD|PASSWD|PASS|PWD|SECRET|TOKEN|ACCESS_KEY|SECRET_KEY|"
                        r"password|secret|token)[A-Za-z0-9_]*=)(\"[^\"]*\"|'[^']*'|[^\s;|&\"']+)")
# mc alias set ИМЯ АДРЕС КЛЮЧ СЕКРЕТ: ключи идут позиционными аргументами
MC_ALIAS = re.compile(r"(\bmc(?:li)?\s+(?:alias\s+set|config\s+host\s+add)\s+\S+\s+\S+\s+)(\S+)(\s+)(\S+)")


def mask(text):
    if not text:
        return text
    text = MC_ALIAS.sub(lambda m: m.group(1) + "‹скрыто›" + m.group(3) + "‹скрыто›", text)
    text = INLINE_ENV.sub(lambda m: m.group(1) + ("‹скрыто›" if not m.group(2).startswith("$") else m.group(2)), text)
    if _ms is not None:
        return _ms.mask(text)[0]
    return FALLBACK.sub(lambda m: m.group(1) + "‹скрыто›", text)


def deep_mask(obj):
    """Маскирует каждую строку в структуре: правила маскировщика работают построчно."""
    if isinstance(obj, dict):
        return dict((k, deep_mask(v)) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return [deep_mask(v) for v in obj]
    if isinstance(obj, str):
        return mask(obj)
    return obj


def dur(s):
    if s is None:
        return "нет данных"
    s = int(s)
    if s < 0:
        return "в будущем"
    if s < 60:
        return "%d с" % s
    m = s // 60
    if m < 60:
        return "%d мин" % m
    h, m = divmod(m, 60)
    if h < 48:
        return "%d ч %d мин" % (h, m) if m else "%d ч" % h
    d, h = divmod(h, 24)
    return "%d дн %d ч" % (d, h) if h else "%d дн" % d


def size(b):
    if b is None:
        return "нет данных"
    b = float(b)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(b) < 1024 or unit == "ТБ":
            return "%d Б" % b if unit == "Б" else ("%.1f %s" % (b, unit)).replace(".", ",")
        b /= 1024.0


def pct(x):
    return ("%.0f" % x) + " %"


def short(text, n=160):
    text = mask(text or "").strip()
    return text if len(text) <= n else text[:n - 1] + "…"


def normalize(name):
    """Имя копии без дат и номеров: rx_20261105_230000.dump -> rx_#_#.dump."""
    return re.sub(r"\d+", "#", name)


def run(cmd, timeout=60, stdin=None):
    """(код, stdout, stderr) или None, если программы нет."""
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        return None
    try:
        out, err = p.communicate(stdin.encode("utf-8") if stdin is not None else None, timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        return (124, "", "превышено время ожидания %d с" % timeout)
    return (p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))


def as_owner(cmd, path, default_user="postgres"):
    """Под root запускает программу копий от владельца каталога (как требуют pgBackRest и pg_probackup)."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0 or not which("runuser"):
        return cmd
    import pwd
    user = default_user
    try:
        uid = os.stat(path).st_uid if path else None
        if uid:
            user = pwd.getpwuid(uid).pw_name
        elif uid == 0:
            return cmd
        pwd.getpwnam(user)
    except (OSError, KeyError):
        return cmd
    return ["runuser", "-u", user, "--"] + cmd


def first_error_line(res):
    """Первая строка с ERROR или FATAL из stderr и stdout (pgBackRest пишет ошибки в stdout)."""
    if not res:
        return "не запустился"
    lines = [l.strip() for l in (res[2] + "\n" + res[1]).splitlines() if l.strip()]
    for l in lines:
        if "ERROR" in l or "FATAL" in l:
            return l
    return lines[0] if lines else "код %d" % res[0]


def which(name, extra=()):
    for d in os.environ.get("PATH", "").split(os.pathsep) + list(extra):
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


class Result(object):
    def __init__(self, key, title):
        self.key, self.title = key, title
        self.status, self.summary, self.details, self.data = "OK", "", [], {}

    def bump(self, status):
        if RANK[status] > RANK[self.status]:
            self.status = status

    def as_dict(self):
        return {"key": self.key, "title": self.title, "status": self.status, "summary": self.summary,
                "details": self.details, "data": self.data}


# --- Разделы и файловые системы ---

def read_mounts(path="/proc/self/mounts"):
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                p = line.split()
                if len(p) >= 4:
                    out.append((p[0], p[1].replace("\\040", " "), p[2], p[3]))
    except OSError:
        pass
    return out


def mount_of(path, mounts):
    path = os.path.realpath(path)
    best = None
    for dev, mp, fstype, opts in mounts:
        if path == mp or path.startswith(mp.rstrip("/") + "/") or mp == "/":
            if best is None or len(mp) > len(best[1]):
                best = (dev, mp, fstype, opts)
    return best


def fs_info(path, mounts):
    try:
        st, vfs = os.stat(path), os.statvfs(path)
    except OSError:
        return None
    total = vfs.f_blocks * vfs.f_frsize
    free = vfs.f_bavail * vfs.f_frsize
    m = mount_of(path, mounts)
    return {"dev": st.st_dev, "free": free, "total": total, "free_pct": 100.0 * free / total if total else 0,
            "mount": m[1] if m else None, "fstype": m[2] if m else None, "source": m[0] if m else None}


# --- Обход каталогов ---

def tree_stats(path, limit=200000):
    """Число файлов, объём и самый свежий mtime внутри каталога; обход ограничен limit файлами."""
    files, total, newest, partial = 0, 0, 0.0, False
    for root, dirs, names in os.walk(path):
        dirs[:] = [d for d in dirs if d not in SKIP_NAMES]
        for n in names:
            try:
                st = os.lstat(os.path.join(root, n))
            except OSError:
                continue
            files += 1
            total += st.st_size
            newest = max(newest, st.st_mtime)
            if files >= limit:
                partial = True
                return files, total, newest, partial
    return files, total, newest, partial


def is_probackup_catalog(path):
    b = os.path.join(path, "backups")
    return os.path.isdir(b) and os.path.isdir(os.path.join(path, "wal")) and any(
        os.path.isfile(os.path.join(b, d, "pg_probackup.conf")) for d in safe_listdir(b))


def is_pgbackrest_repo(path):
    return os.path.isdir(os.path.join(path, "archive")) and os.path.isdir(os.path.join(path, "backup")) and any(
        os.path.isfile(os.path.join(path, "backup", d, "backup.info")) for d in safe_listdir(os.path.join(path, "backup")))


def safe_listdir(path):
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def scan_dir(root, max_depth=3, ignore=(), big_dir=50):
    """Разбирает каталог копий: артефакты с датами в именах, архивы WAL, каталоги pg_probackup и pgBackRest."""
    found = {"items": [], "wal": {}, "probackup": [], "pgbackrest": [], "errors": []}

    def add_item(path, rel_parent, name, is_dir):
        try:
            st = os.lstat(path)
        except OSError as e:
            found["errors"].append("%s: %s" % (path, e.strerror))
            return
        item = {"path": path, "parent": rel_parent, "name": name, "group": normalize(name), "dir": is_dir,
                "mtime": st.st_mtime, "size": st.st_size, "temp": bool(TEMP_RE.search(name))}
        if is_dir:
            files, total, newest, partial = tree_stats(path)
            item.update({"size": total, "files": files, "newest_inside": newest, "partial": partial})
        found["items"].append(item)

    def walk(path, rel, depth):
        try:
            entries = sorted(os.scandir(path), key=lambda e: e.name)
        except OSError as e:
            found["errors"].append("%s: %s" % (path, e.strerror))
            return
        dated = [e for e in entries if re.search(r"\d{4,}", e.name)]
        plain_dirs = 0
        for e in entries:
            if e.name in SKIP_NAMES or any(fnmatch.fnmatch(e.name, g) for g in ignore):
                continue
            full = os.path.join(path, e.name)
            try:
                is_dir = e.is_dir(follow_symlinks=False)
                is_file = e.is_file(follow_symlinks=False)
            except OSError:
                continue
            if is_file and (WAL_RE.match(e.name) or LABEL_RE.match(e.name) or HISTORY_RE.match(e.name)):
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                found["wal"].setdefault(path, []).append((e.name, st.st_size, st.st_mtime))
                continue
            if is_dir and is_probackup_catalog(full):
                found["probackup"].append(full)
                continue
            if is_dir and is_pgbackrest_repo(full):
                found["pgbackrest"].append(full)
                continue
            if is_dir and not re.search(r"\d{4,}", e.name) and depth < max_depth:
                plain_dirs += 1
                walk(full, os.path.join(rel, e.name) if rel else e.name, depth + 1)
                continue
            if is_dir or is_file:
                add_item(full, rel, e.name, is_dir)
        # большой каталог без дат в именах похож на зеркало хранилища: считаем его одним деревом
        if depth > 0 and len(entries) > big_dir and len(dated) < len(entries) * 0.2 and not found["wal"].get(path):
            mine = [i for i in found["items"] if os.path.dirname(i["path"]) == path]
            for i in mine:
                found["items"].remove(i)
            files, total, newest, partial = tree_stats(path)
            found["items"].append({"path": path, "parent": rel, "name": os.path.basename(path),
                                   "group": "", "dir": True, "tree": True,
                                   "mtime": newest, "size": total, "files": files, "newest_inside": newest,
                                   "partial": partial, "temp": False})

    if os.path.isdir(root):
        if is_probackup_catalog(root):
            found["probackup"].append(root)
        elif is_pgbackrest_repo(root):
            found["pgbackrest"].append(root)
        else:
            walk(root, "", 0)
    return found


def group_items(items):
    groups = {}
    for i in items:
        groups.setdefault((i["parent"], i["group"]), []).append(i)
    for v in groups.values():
        v.sort(key=lambda i: i["mtime"])
    return groups


def eval_group(key, items, now, max_age_h, shrink_pct):
    """Оценка одной группы копий: свежесть, размер, ужатие."""
    parent, group = key
    newest = items[-1]
    age = now - newest["mtime"]
    label = os.path.join(parent, group).rstrip("/") if parent else group
    st, notes = "OK", []
    dated = bool(re.search(r"#", group)) and not newest.get("tree")
    real = [i for i in items if not i["temp"]]
    temps = [i for i in items if i["temp"]]
    for t in temps:
        if now - t["mtime"] > 86400:
            st = max(st, "WARN", key=RANK.get)
            notes.append("оборванная копия под временным именем: %s, %s назад" % (t["name"], dur(now - t["mtime"])))
    if not real:
        return st, "%s: только временные файлы" % label, notes
    newest = real[-1]
    age = now - newest["mtime"]
    line = "%s: последняя %s, %s назад, %s" % (label, newest["name"], dur(age), size(newest["size"]))
    if newest.get("tree"):
        line = "%s: дерево файлов (%s файлов, %s), самый свежий файл %s назад" % (
            label, newest.get("files"), size(newest["size"]), dur(age))
        if newest.get("partial"):
            line += ", посчитано частично"
    if len(real) > 1:
        line += "; копий %d, старейшая %s назад" % (len(real), dur(now - real[0]["mtime"]))
    if newest["size"] == 0 or (newest.get("dir") and newest.get("files") == 0):
        st = "CRIT"
        notes.append("последняя копия пустая")
    stale_applies = len(real) > 1 or not dated
    if stale_applies and age > max_age_h * 3600 * 3:
        st = "CRIT"
        notes.append("последней копии больше %s при ожидаемых %d ч" % (dur(age), max_age_h))
    elif stale_applies and age > max_age_h * 3600:
        st = max(st, "WARN", key=RANK.get)
        notes.append("последней копии %s, ожидалось не больше %d ч" % (dur(age), max_age_h))
    elif not stale_applies and age > max_age_h * 3600:
        st = max(st, "INFO", key=RANK.get)
        notes.append("одна копия с датой в имени: похоже на разовую, свежесть не оцениваю")
    prev = [i["size"] for i in real[-6:-1]]
    if len(prev) >= 2 and newest["size"] > 0:
        med = statistics.median(prev)
        if med > 0 and newest["size"] < med * (1 - shrink_pct / 100.0):
            st = max(st, "WARN", key=RANK.get)
            notes.append("последняя копия меньше обычного на %s (%s против %s): проверьте, что она полная"
                         % (pct(100 - 100.0 * newest["size"] / med), size(newest["size"]), size(med)))
    return st, line, notes


def wal_segments_per_log(files):
    """Сегментов в одном логическом файле: по размеру несжатого сегмента или по наблюдаемым номерам."""
    sizes = [s for n, s, _ in files if WAL_RE.match(n) and not WAL_RE.match(n).group(4) and not WAL_RE.match(n).group(5)]
    seg_size = None
    if sizes:
        common = max(set(sizes), key=sizes.count)
        if common >= MIB and common & (common - 1) == 0:
            seg_size = common
    if seg_size:
        return 0x100000000 // seg_size, seg_size
    by_log = {}
    for n, _, _ in files:
        m = WAL_RE.match(n)
        if m:
            by_log.setdefault((m.group(1), int(m.group(2), 16)), set()).add(int(m.group(3), 16))
    logs = sorted(by_log)
    tops = [max(by_log[k]) for i, k in enumerate(logs[:-1]) if logs[i + 1][0] == k[0] and logs[i + 1][1] == k[1] + 1]
    if tops:
        top = max(tops) + 1
        if top & (top - 1) == 0 and top <= 0x100:
            return top, 0x100000000 // top
    return 0x100, 16 * MIB


def eval_wal(path, files, now, wal_max_age_h):
    """Архив WAL: число, объём, дыры в последовательности, свежесть."""
    r = {"path": path, "status": "OK", "notes": []}
    segs = []
    per_log, seg_size = wal_segments_per_log(files)
    labels, histories = [], []
    for name, sz, mt in files:
        m = WAL_RE.match(name)
        if m:
            tli, log, seg = int(m.group(1), 16), int(m.group(2), 16), int(m.group(3), 16)
            segs.append((tli, log * per_log + seg, name, sz, mt, bool(m.group(4))))
        elif LABEL_RE.match(name):
            labels.append((name, mt))
        else:
            histories.append(name)
    total = sum(f[1] for f in files)
    r.update({"segments": len(segs), "bytes": total, "segment_size": seg_size, "labels": len(labels),
              "histories": len(histories)})
    if not segs:
        r["line"] = "%s: сегментов WAL нет, только служебные файлы" % path
        return r
    segs.sort()
    newest = max(segs, key=lambda s: s[4])
    gaps = []
    by_tli = {}
    for s in segs:
        if not s[5]:
            by_tli.setdefault(s[0], []).append(s[1])
    for tli, nums in sorted(by_tli.items()):
        nums = sorted(set(nums))
        for a, b in zip(nums, nums[1:]):
            if b - a > 1:
                gaps.append((tli, a + 1, b - 1))

    def segname(tli, num):
        return "%08X%08X%08X" % (tli, num // per_log, num % per_log)

    age = now - newest[4]
    r.update({"first": segs[0][2], "last": segs[-1][2], "newest_age_s": int(age), "gaps": [
        {"timeline": g[0], "from": segname(g[0], g[1]), "to": segname(g[0], g[2]), "missing": g[2] - g[1] + 1}
        for g in gaps]})
    r["line"] = "%s: архив WAL, %d сегментов, %s, с %s по %s, последний пришёл %s назад" % (
        path, len(segs), size(total), segs[0][2], segs[-1][2], dur(age))
    if gaps:
        r["status"] = "CRIT"
        for g in r["gaps"][:5]:
            r["notes"].append("дыра в архиве на линии времени %d: нет %s..%s (%d сегм.): восстановление на момент "
                              "после неё невозможно, нужна новая полная копия" % (g["timeline"], g["from"], g["to"],
                                                                                  g["missing"]))
    if age > wal_max_age_h * 3600:
        r["status"] = max(r["status"], "WARN", key=RANK.get)
        r["notes"].append("новых сегментов нет %s: если база в это время писала, архивация стоит "
                          "(проверьте pg_stat_archiver)" % dur(age))
    if labels:
        labels.sort()
        oldest = LABEL_RE.match(labels[0][0]).group(1)
        older = [s for s in segs if s[2] < oldest and s[0] == int(oldest[:8], 16)]
        r["notes"].append("меток .backup %d, старейшая начинается с %s" % (len(labels), oldest))
        if older:
            r["notes"].append("раньше старейшей метки %d сегментов (%s): если копий старше этой метки нет, их убирает "
                              "pg_archivecleanup, а не удаление по дате" % (len(older), size(sum(s[3] for s in older))))
    return r


# --- Расписания ---

def keyword_hits(text, generic=True):
    """Виды копий и найденные слова. generic=False: без общих слов backup и бэкап (для системных скриптов)."""
    kinds, words = [], []
    for rx, kind in KW_RE:
        if kind == "неясно" and not generic:
            continue
        m = rx.search(text)
        if m:
            words.append(m.group(0).strip())
            if kind not in kinds:
                kinds.append(kind)
    if "база" in kinds or "файлы" in kinds:
        kinds = [k for k in kinds if k != "неясно"]
    return kinds, words


def referenced_files(command):
    out = []
    for p in ABS_PATH.findall(command or ""):
        p = p.rstrip(".,;")
        if os.path.isfile(p) and p not in out:
            out.append(p)
    return out


def read_text(path, limit=262144):
    try:
        if os.path.getsize(path) > limit:
            return None
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if b"\0" in data[:4096]:
        return None
    return data.decode("utf-8", "replace")


def mentioned_paths(text):
    paths = set(m.rstrip("/") for m in PATH_FLAGS.findall(text or "") if m.startswith("/"))
    for m in re.finditer(r"(?m)^\s*(?:DIR|BACKUP_DIR|BACKUP_PATH|TARGET|DEST|REPO)\s*=\s*[\"']?(/[^\"'\s]+)", text or ""):
        paths.add(m.group(1).rstrip("/"))
    return sorted(p for p in paths if "%" not in p and "$" not in p)


def inspect_command(command):
    """Ключевые слова в команде и в скриптах, на которые она ссылается."""
    kinds, words = keyword_hits(command)
    texts = [command]
    scripts = []
    for path in referenced_files(command):
        t = read_text(path)
        if t is None:
            continue
        k2, w2 = keyword_hits(t, generic=False)
        if k2:
            scripts.append({"path": path, "words": sorted(set(w2))})
            for k in k2:
                if k not in kinds:
                    kinds.append(k)
            words += w2
            texts.append(t)
    if "база" in kinds or "файлы" in kinds:
        kinds = [k for k in kinds if k != "неясно"]
    return {"kinds": kinds, "words": sorted(set(words)), "scripts": scripts, "text": "\n".join(texts)}


CRON_FIELDS = re.compile(r"^\s*(@\w+|(?:\S+\s+){4}\S+)\s+(.*)$")


def parse_cron_text(text, source, system):
    """Строки crontab с ключевыми словами. system=True: шестое поле это пользователь."""
    out = []
    for no, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*=", s):
            continue
        m = CRON_FIELDS.match(s)
        if not m:
            continue
        when, rest = m.group(1), m.group(2)
        user = ""
        if system:
            parts = rest.split(None, 1)
            if len(parts) < 2:
                continue
            user, rest = parts
        info = inspect_command(rest)
        if not info["kinds"]:
            continue
        info.update({"source": "%s:%d" % (source, no), "when": when, "user": user, "command": rest})
        out.append(info)
    return out


def scan_cron(root="/"):
    jobs, read, denied = [], [], []

    def j(p):
        return os.path.join(root, p.lstrip("/"))

    files = [("/etc/crontab", True)] + [(p, True) for p in sorted(glob.glob(j("/etc/cron.d/*")))]
    for d in ("/var/spool/cron/crontabs", "/var/spool/cron"):
        if os.path.isdir(j(d)) and not os.access(j(d), os.R_OK | os.X_OK):
            denied.append(d)
            continue
        for p in sorted(glob.glob(j(d) + "/*")):
            if os.path.isfile(p):
                files.append((p, False))
    for path, system in files:
        full = path if path.startswith(root) and root != "/" else j(path)
        if not os.path.exists(full):
            continue
        t = read_text(full)
        if t is None:
            denied.append(path)
            continue
        read.append(path)
        jobs += parse_cron_text(t, path, system)
    for period in ("hourly", "daily", "weekly", "monthly"):
        for p in sorted(glob.glob(j("/etc/cron.%s/*" % period))):
            t = read_text(p)
            if t is None:
                continue
            kinds, words = keyword_hits(t, generic=False)
            if kinds:
                jobs.append({"source": p, "when": "cron.%s" % period, "user": "root", "command": p, "kinds": kinds,
                             "words": sorted(set(words)), "scripts": [], "text": t})
    if root == "/":
        res = run(["crontab", "-l"], timeout=10)
        if res and res[0] == 0:
            jobs += parse_cron_text(res[1], "crontab -l", False)
    return jobs, read, denied


def parse_list_timers(text):
    """systemctl list-timers --all --no-legend: пары (таймер, служба)."""
    out = []
    for line in text.splitlines():
        toks = line.split()
        timer = next((t for t in toks if t.endswith(".timer")), None)
        if not timer:
            continue
        i = toks.index(timer)
        unit = toks[i + 1] if i + 1 < len(toks) and toks[i + 1].endswith(".service") else timer[:-6] + ".service"
        out.append((timer, unit))
    return out


def parse_show(text):
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def exec_lines(unit_text):
    out = []
    for line in unit_text.splitlines():
        m = re.match(r"^\s*Exec(?:Start|StartPre|StartPost)\s*=\s*[-@:+!]*(.*)$", line)
        if m:
            out.append(m.group(1).strip())
    return out


def scan_timers():
    if not which("systemctl"):
        return None, "systemctl нет: таймеры systemd не проверялись"
    res = run(["systemctl", "list-timers", "--all", "--no-pager", "--no-legend"], timeout=20)
    if not res or res[0] != 0:
        return None, "systemctl list-timers не ответил: %s" % short(res[2] if res else "", 120)
    jobs = []
    for timer, unit in parse_list_timers(res[1]):
        cat = run(["systemctl", "cat", "--no-pager", unit], timeout=10)
        if not cat or cat[0] != 0:
            continue
        cmds = exec_lines(cat[1])
        info = inspect_command("\n".join(cmds))
        if not info["kinds"]:
            continue
        t = parse_show((run(["systemctl", "show", timer, "-p", "LastTriggerUSec", "-p", "NextElapseUSecRealtime",
                             "-p", "TimersCalendar"], timeout=10) or (1, "", ""))[1])
        s = parse_show((run(["systemctl", "show", unit, "-p", "Result", "-p", "ExecMainStatus",
                             "-p", "ExecMainExitTimestamp", "-p", "ActiveState"], timeout=10) or (1, "", ""))[1])
        info.update({"source": timer, "unit": unit, "when": t.get("TimersCalendar", ""), "user": "",
                     "command": "; ".join(cmds), "last": t.get("LastTriggerUSec", ""), "result": s.get("Result", ""),
                     "exit_status": s.get("ExecMainStatus", ""), "next": t.get("NextElapseUSecRealtime", "")})
        jobs.append(info)
    return jobs, None


def bacula_filesets(path="/etc/bacula/bacula-dir.conf"):
    """Пути из блоков FileSet { Include { File = ... } }. Пароли из файла не читаются в вывод."""
    t = read_text(path, limit=4 * 1024 * 1024)
    if t is None:
        return None
    files = []
    for m in re.finditer(r"(?im)^\s*File\s*=\s*\"?([^\"\n}]+?)\"?\s*(?:}|$)", t):
        files.append(m.group(1).strip())
    for m in re.finditer(r"(?i)\bFile\s*=\s*\"?(/[^\"\s}]+)", t):
        if m.group(1) not in files:
            files.append(m.group(1))
    return files


# --- pg_probackup и pgBackRest ---

def parse_pg_time(s):
    """'2026-11-05 03:00:01+03' -> epoch."""
    if not s:
        return None
    m = re.match(r"^(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d):(\d\d)(?:\.\d+)?\s*(?:([+-])(\d\d):?(\d\d)?|Z)?$", s.strip())
    if not m:
        return None
    import calendar
    t = calendar.timegm(tuple(int(m.group(i)) for i in range(1, 7)) + (0, 0, 0))
    if m.group(7):
        off = int(m.group(8)) * 3600 + int(m.group(9) or 0) * 60
        t -= off if m.group(7) == "+" else -off
    return t


def eval_probackup(show, archive, now, max_age_h):
    """Разбор pg_probackup show --format=json и show --archive --format=json."""
    lines, status, data = [], "OK", []
    for inst in show or []:
        backups = inst.get("backups") or []
        name = inst.get("instance", "?")
        ok = [b for b in backups if b.get("status") in ("OK", "DONE")]
        bad = [b for b in backups if b.get("status") not in ("OK", "DONE", "RUNNING", "MERGING", "MERGED", "DELETING")]
        newest_ok = max(ok, key=lambda b: parse_pg_time(b.get("end-time") or b.get("start-time")) or 0) if ok else None
        fulls = [b for b in ok if b.get("backup-mode") == "FULL"]
        d = {"instance": name, "backups": len(backups), "ok": len(ok), "bad": [(b.get("id"), b.get("status")) for b in bad]}
        if not newest_ok:
            status = "CRIT"
            lines.append(("CRIT", "экземпляр %s: успешных копий нет (всего %d)" % (name, len(backups))))
        else:
            t = parse_pg_time(newest_ok.get("end-time") or newest_ok.get("start-time"))
            age = now - t if t else None
            d.update({"newest_ok": newest_ok.get("id"), "newest_ok_age_s": int(age) if age is not None else None})
            line = "экземпляр %s: копий %d, успешных %d; последняя успешная %s (%s, %s) %s назад, данных %s" % (
                name, len(backups), len(ok), newest_ok.get("id"), newest_ok.get("backup-mode"),
                newest_ok.get("wal", "?"), dur(age), size(newest_ok.get("data-bytes")))
            st = "OK"
            if age is not None and age > max_age_h * 3600 * 3:
                st = "CRIT"
            elif age is not None and age > max_age_h * 3600:
                st = "WARN"
            if fulls:
                ft = parse_pg_time(max(fulls, key=lambda b: b.get("end-time") or "").get("end-time"))
                line += "; последняя FULL %s назад" % dur(now - ft if ft else None)
            lines.append((st, line))
            status = max(status, st, key=RANK.get)
        if bad:
            status = max(status, "WARN", key=RANK.get)
            lines.append(("WARN", "экземпляр %s: копии со статусом не OK: %s" % (
                name, ", ".join("%s %s" % (i, s) for i, s in d["bad"][:8]))))
        if backups:
            latest = max(backups, key=lambda b: parse_pg_time(b.get("start-time")) or 0)
            if latest.get("status") not in ("OK", "DONE", "RUNNING"):
                status = max(status, "WARN", key=RANK.get)
                lines.append(("WARN", "последний запуск %s завершился статусом %s" % (latest.get("id"), latest.get("status"))))
        data.append(d)
    for inst in archive or []:
        for tl in inst.get("timelines") or []:
            lost = tl.get("lost-segments") or []
            st = tl.get("status", "")
            line = "архив WAL экземпляра %s, линия %s: %s сегм. %s..%s, %s, статус %s" % (
                inst.get("instance", "?"), tl.get("tli"), tl.get("n-segments"), tl.get("min-segno"),
                tl.get("max-segno"), size(tl.get("size")), st or "?")
            if lost or (st and st != "OK"):
                status = "CRIT"
                lines.append(("CRIT", line + "; потеряны: " + ", ".join(
                    "%s..%s" % (x.get("begin-segno"), x.get("end-segno")) for x in lost[:5])))
            else:
                lines.append(("OK", line))
    return status, lines, data


def eval_pgbackrest(info, now, max_age_h):
    lines, status = [], "OK"
    for st in info or []:
        name = st.get("name", "?")
        code = (st.get("status") or {}).get("code")
        msg = (st.get("status") or {}).get("message", "")
        backups = st.get("backup") or []
        good = [b for b in backups if not b.get("error")]
        if code not in (0, None):
            status = max(status, "WARN", key=RANK.get)
            lines.append(("WARN", "stanza %s: статус %s %s" % (name, code, msg)))
        if not good:
            status = "CRIT"
            lines.append(("CRIT", "stanza %s: успешных копий нет" % name))
            continue
        last = max(good, key=lambda b: (b.get("timestamp") or {}).get("stop", 0))
        age = now - (last.get("timestamp") or {}).get("stop", 0)
        stt = "CRIT" if age > max_age_h * 3600 * 3 else "WARN" if age > max_age_h * 3600 else "OK"
        status = max(status, stt, key=RANK.get)
        arch = (st.get("archive") or [{}])[-1]
        wal = "%s..%s" % (arch["min"], arch["max"]) if arch.get("min") and arch.get("max") else "пуст"
        lines.append((stt, "stanza %s: копий %d, последняя %s (%s) %s назад; архив WAL %s" % (
            name, len(backups), last.get("label"), last.get("type"), dur(age), wal)))
        if not (arch.get("min") and arch.get("max")):
            status = max(status, "WARN", key=RANK.get)
            lines.append(("WARN", "stanza %s: в репозитории нет архива WAL: восстановление только на момент копии" % name))
    return status, lines


def find_probackup():
    cands = []
    for d in os.environ.get("PATH", "").split(os.pathsep) + ["/usr/bin", "/usr/local/bin"] + glob.glob("/opt/pgpro/*/bin"):
        for p in glob.glob(os.path.join(d, "pg_probackup*")):
            if re.search(r"pg_probackup(-\d+)?$", p) and os.access(p, os.X_OK) and p not in cands:
                cands.append(p)
    return sorted(cands, key=lambda p: [int(x) for x in re.findall(r"\d+", os.path.basename(p))] or [0])


# --- PostgreSQL ---

PG_SQL = r"""
SELECT current_setting('server_version_num')::int AS version_num, pg_is_in_recovery() AS replica,
       (SELECT setting FROM pg_settings WHERE name = 'archive_mode') AS archive_mode,
       (SELECT setting FROM pg_settings WHERE name = 'archive_command') AS archive_command,
       (SELECT setting FROM pg_settings WHERE name = 'archive_library') AS archive_library,
       (SELECT setting FROM pg_settings WHERE name = 'data_directory') AS data_directory,
       a.archived_count, a.failed_count, coalesce(a.last_archived_wal, '') AS last_archived_wal,
       extract(epoch FROM now() - a.last_archived_time)::bigint AS archived_age_s,
       extract(epoch FROM now() - a.last_failed_time)::bigint AS failed_age_s,
       (a.last_failed_time IS NOT NULL
        AND (a.last_archived_time IS NULL OR a.last_failed_time > a.last_archived_time)) AS failing_now,
       (SELECT sum(pg_database_size(oid)) FROM pg_database
         WHERE datallowconn AND has_database_privilege(oid, 'CONNECT'))::bigint AS db_bytes
FROM pg_stat_archiver a
"""


def query_pg(base, dbname, timeout=10):
    script = ("SET default_transaction_read_only = on;\nSET statement_timeout = '%ds';\n"
              "SELECT coalesce(json_agg(q), '[]'::json) FROM (%s) q;\n" % (timeout, PG_SQL))
    cmd = list(base) + ["-X", "-q", "-A", "-t", "-w", "-v", "ON_ERROR_STOP=1", "-f", "-"]
    if dbname:
        cmd += ["-d", dbname]
    res = run(cmd, timeout=timeout + 20, stdin=script)
    if res is None:
        return None, "psql не найден"
    if res[0] != 0:
        lines = [l for l in res[2].splitlines() if l.strip()]
        err = next((l for l in lines if "FATAL" in l or "ERROR" in l), lines[0] if lines else "код %d" % res[0])
        return None, mask(re.sub(r"^psql:(<stdin>|[^:]*):\d+:\s*", "", err.strip()))
    m = re.search(r"(?m)^\[", res[1])
    if not m:
        return None, "неожиданный вывод psql"
    rows = json.loads(res[1][m.start():])
    return (rows[0] if rows else None), None


def eval_pg(row, wal_max_age_h):
    st, lines = "OK", []
    mode = row.get("archive_mode") or "off"
    if mode == "off":
        return "INFO", ["archive_mode = off: архива WAL нет, вернуться можно только на момент полной копии"]
    cmd = row.get("archive_command") or row.get("archive_library") or ""
    if not cmd.strip():
        return "CRIT", ["archive_mode = %s, а archive_command пуст: сегменты копятся в pg_wal" % mode]
    if row.get("replica") and mode != "always":
        return "INFO", ["это реплика: архивирует ведущий сервер, проверьте скрипт там"]
    lines.append("archive_command: %s" % short(cmd, 140))
    lines.append("заархивировано %s, ошибок %s; последний успех %s назад (%s)" % (
        row.get("archived_count"), row.get("failed_count"), dur(row.get("archived_age_s")), row.get("last_archived_wal") or "нет"))
    if row.get("failing_now"):
        st = "CRIT"
        lines.append("архивация падает сейчас (последняя ошибка %s назад): точка восстановления не двигается, "
                     "pg_wal растёт; разбор в скиле rx-postgresql" % dur(row.get("failed_age_s")))
    elif row.get("archived_age_s") is not None and row["archived_age_s"] > wal_max_age_h * 3600:
        st = "WARN"
        lines.append("последний успех архивации старше %d ч: если база писала, архив стоит" % wal_max_age_h)
    return st, lines


# --- Directum Launcher ---

CONFIG_KEYS = ["DATABASE_ENGINE", "home_path", "STORAGE_PATH", "S3_STORAGE_SERVICE_ADDRESS", "S3_STORAGE_BUCKET_NAME",
               "DATA_PROTECTION_CERTIFICATE_FILE", "DATA_PROTECTION_CERTIFICATE_THUMBPRINT", "ssl_cert",
               "config_password", "PRIMARY_TENANT", "KeyDerivationService", "SungeroPgBouncer", "CONNECTION_STRING",
               "OBSOLETE_DATA_LIFE_PERIOD", "DELETED_DATA_LIFE_PERIOD"]


def find_launcher_dirs(extra=()):
    out = []
    for p in list(extra) + glob.glob("/srv/*/etc/config.yml") + glob.glob("/opt/*/etc/config.yml") + \
            glob.glob("/srv/*/*/etc/config.yml") + glob.glob("/home/*/*/etc/config.yml"):
        d = p if os.path.isdir(p) else os.path.dirname(os.path.dirname(p))
        if os.path.isfile(os.path.join(d, "etc", "config.yml")) and os.path.exists(os.path.join(d, "do.sh")) \
                and d not in out:
            out.append(d)
    return out


def parse_config(text):
    """Нужные ключи из config.yml без YAML-парсера: первое значение каждого ключа, STORAGE_PATH все."""
    vals = {}
    for line in text.splitlines():
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$", line)
        if not m or m.group(1) not in CONFIG_KEYS:
            continue
        k, v = m.group(1), m.group(2)
        v = re.sub(r"\s+#.*$", "", v).strip().strip("'\"")
        if k == "STORAGE_PATH":
            vals.setdefault(k, [])
            if v and v not in vals[k]:
                vals[k].append(v)
        else:
            vals.setdefault(k, v)
    home = vals.get("home_path", "")

    def res(v):
        return re.sub(r"\{\{\s*home_path\s*\}\}", home, v) if home else v
    if "STORAGE_PATH" in vals:
        vals["STORAGE_PATH"] = [res(v) for v in vals["STORAGE_PATH"]]
    for k in ("DATA_PROTECTION_CERTIFICATE_FILE", "ssl_cert"):
        if vals.get(k):
            vals[k] = res(vals[k])
    if vals.get("CONNECTION_STRING"):
        cs = vals["CONNECTION_STRING"]
        db = re.search(r"(?i)(?:Database|Initial Catalog)\s*=\s*([^;]+)", cs)
        host = re.search(r"(?i)(?:Server|Host|Data Source)\s*=\s*([^;]+)", cs)
        vals["CONNECTION_STRING"] = {"database": db.group(1).strip() if db else None,
                                     "host": host.group(1).strip() if host else None,
                                     "encrypted": not (db or host)}
    return vals


def covered(path, texts, filesets):
    """Упоминается ли путь (или каталог над ним) в заданиях копирования."""
    if not path:
        return False
    path = path.rstrip("/")
    cands = [path]
    p = path
    while os.path.dirname(p) not in ("/", "", p):
        p = os.path.dirname(p)
        cands.append(p)
    for t in texts:
        for c in cands:
            if re.search(r"(?<![\w/.-])" + re.escape(c) + r"/?(?=$|[\s'\";|&)*])", t):
                return True
    for f in filesets or []:
        f = f.rstrip("/")
        if f and (path == f or path.startswith(f + "/")):
            return True
    return False


# --- Сборка отчёта ---

CHECKLIST = [
    "База и хранилище документов копируются согласованно: сначала база, потом хранилище, в одно окно. "
    "Хранилище держит заменённое содержимое в папке deleted 3 дня по умолчанию (OBSOLETE_DATA_LIFE_PERIOD, "
    "справка RX admin_nastroika_khranilishcha_linux.htm): разрыв между копиями держите заметно меньше.",
    "Копии лежат вне площадки или хотя бы на другом хранилище, чем база и документы; скрипт видит только этот сервер.",
    "Копии зашифрованы или лежат там, куда доступ есть только у тех, кому он нужен; удалить копии с сервера базы нельзя.",
    "Отдельно от копий данных сохранены: config.yml и папка etc Directum Launcher, сертификат защиты данных "
    "(DATA_PROTECTION_CERTIFICATE_*), сертификат HTTPS, пароль шифрования параметров config.yml, если он задан.",
    "Если включено шифрование документов: сертификаты и секреты сервиса ключей сохранены отдельно друг от друга "
    "(справка RX admin_zamena_kluchey_shifrovaniya_linux.htm). Без них копии документов не расшифровать.",
    "Известен код системы PRIMARY_TENANT: при восстановлении базы указывают код ранее развёрнутой системы "
    "(справка RX admin_minimalnye_nastroiki_linux.htm).",
    "Файл ключа лицензии сохранён (активированная лицензия хранится в базе, но файл пригодится при переносе).",
    "Пробное восстановление делалось не позже квартала назад, время записано в протокол: это фактическое RTO.",
    "Мониторинг тревожит по возрасту последней успешной копии и по архивации WAL, а не только по логу задания.",
    "Индекс Elasticsearch можно не копировать, но время его перестроения после восстановления известно.",
]


def collect(args, now=None):
    now = time.time() if now is None else now
    results = []
    mounts = read_mounts()

    # Расписания
    sched = Result("schedules", "Расписания копирования")
    jobs, cron_read, cron_denied = scan_cron()
    timers, timers_err = scan_timers()
    all_jobs = jobs + (timers or [])
    sched.data = {"jobs": [dict((k, v) for k, v in j.items() if k != "text") for j in all_jobs]}
    for j in all_jobs:
        line = "%s [%s] %s%s: %s" % (j["source"], j.get("when") or "?", "+".join(j["kinds"]),
                                     " (" + ", ".join(j["words"][:4]) + ")" if j.get("words") else "",
                                     short(j.get("command"), 140))
        if j.get("result"):
            line += "; последний запуск %s, результат %s" % (j.get("last") or "не было", j["result"])
            if j["result"] not in ("success", ""):
                sched.bump("WARN")
                line += " (код %s)" % j.get("exit_status", "?")
        sched.details.append(line)
        for s in j.get("scripts", []):
            sched.details.append("  скрипт %s: %s" % (s["path"], ", ".join(s["words"][:6])))
    kinds = set(k for j in all_jobs for k in j["kinds"])
    if not all_jobs:
        sched.bump("WARN")
        sched.summary = "в cron и таймерах systemd этого сервера заданий копирования не нашёл"
    else:
        sched.summary = "заданий %d: база %s, файлы %s" % (len(all_jobs), "есть" if "база" in kinds else "не видно",
                                                           "есть" if "файлы" in kinds else "не видно")
        if "база" not in kinds:
            sched.bump("INFO")
        if "файлы" not in kinds:
            sched.bump("INFO")
            sched.details.append("копирования файлов (хранилище документов, config.yml) в расписаниях этого сервера не видно")
    if cron_denied:
        sched.bump("INFO")
        sched.details.append("нет прав читать: %s (запустите под root)" % ", ".join(cron_denied))
    if timers_err:
        sched.details.append(timers_err)
    fs = bacula_filesets()
    if fs is not None:
        sched.details.append("Bacula Director: в наборах файлов %s; задания и их итоги: echo 'list jobs' | sudo bconsole"
                             % (", ".join(fs[:10]) or "путей не нашёл"))
    elif os.path.exists("/etc/bacula/bacula-fd.conf"):
        sched.details.append("установлен Bacula File: копирование с этого сервера ведёт Director на другом сервере")
    results.append(sched)
    texts = [j.get("text") or j.get("command") or "" for j in all_jobs]

    # Каталоги
    dirs = list(args.dir or [])
    auto = []
    if not dirs:
        for j in all_jobs:
            for p in mentioned_paths(j.get("text") or j.get("command")):
                d = p if os.path.isdir(p) else os.path.dirname(p)
                if os.path.isdir(d) and d not in auto and d != "/":
                    auto.append(d)
        if os.path.isdir("/backup") and "/backup" not in auto:
            auto.append("/backup")
        # вложенный каталог уже обойдён вместе с родителем
        auto = [d for d in auto if not any(d != o and d.startswith(o.rstrip("/") + "/") for o in auto)]
        dirs = auto
    pgdata = args.pgdata
    pg_row, pg_err = None, None
    if not args.no_pg:
        base = shlex.split(args.psql_cmd) if args.psql_cmd else [args.psql]
        pg_row, pg_err = query_pg(base, args.dbname)
        dd = (pg_row or {}).get("data_directory")
        if dd and not pgdata and os.path.isfile(os.path.join(dd, "PG_VERSION")):
            pgdata = dd  # каталог данных виден с этой машины: сверим разделы
    launcher = find_launcher_dirs([args.launcher_dir] if args.launcher_dir else [])
    cfg, cfg_path = {}, None
    if launcher:
        cfg_path = os.path.join(launcher[0], "etc", "config.yml")
        t = read_text(cfg_path, limit=8 * 1024 * 1024)
        cfg = parse_config(t) if t else {}
    storage_paths = list(args.storage_path or []) or [p for p in cfg.get("STORAGE_PATH", []) if p.startswith("/")]
    special = {}
    if pgdata:
        special["каталог данных PostgreSQL"] = fs_info(pgdata, mounts)
    for p in storage_paths:
        special["хранилище документов %s" % p] = fs_info(p, mounts)
    probackup_catalogs = list(args.probackup_catalog or [])
    pgbackrest_repos = []
    if not dirs:
        r = Result("dirs", "Каталоги копий")
        r.status, r.summary = "SKIP", "каталоги не заданы и не найдены в заданиях: укажите --dir"
        results.append(r)
    for d in dirs:
        r = Result("dir", "Каталог %s" % d)
        r.data["path"] = d
        if not os.path.isdir(d):
            r.status, r.summary = "SKIP", "каталога нет или нет прав"
            results.append(r)
            continue
        info = fs_info(d, mounts)
        found = scan_dir(d, max_depth=args.depth, ignore=args.ignore or [])
        probackup_catalogs += [c for c in found["probackup"] if c not in probackup_catalogs]
        pgbackrest_repos += found["pgbackrest"]
        groups = group_items(found["items"])
        r.summary = "%s%s; групп копий %d" % (
            "раздел %s (%s), свободно %s из %s" % (info["mount"], info["fstype"], size(info["free"]), size(info["total"]))
            if info else "раздел не определён", " [найден в заданиях]" if d in auto else "", len(groups))
        r.data.update({"fs": info, "groups": []})
        if info:
            if info["free_pct"] < 10:
                r.bump("WARN")
                r.details.append("на разделе с копиями свободно меньше 10 %%: следующая копия может не поместиться")
            if info["fstype"] in NETWORK_FS:
                r.details.append("сетевой ресурс %s: при обрыве задание пишет в пустую локальную папку, "
                                 "проверяйте монтирование в самом скрипте" % (info.get("source") or ""))
            for label, sp in special.items():
                if sp and sp["dev"] == info["dev"]:
                    r.bump("WARN")
                    r.details.append("копии на том же разделе, что и %s (%s): отказ диска заберёт и данные, и копии"
                                     % (label, sp["mount"]))
        for key, items in sorted(groups.items()):
            st, line, notes = eval_group(key, items, now, args.max_age, args.shrink)
            r.bump(st)
            r.details.append("[%s] %s" % (st, line))
            r.details += ["       " + n for n in notes]
            r.data["groups"].append({"group": key[1], "parent": key[0], "status": st, "count": len(items),
                                     "newest": items[-1]["name"], "newest_age_s": int(now - items[-1]["mtime"]),
                                     "newest_size": items[-1]["size"]})
        for wdir, files in sorted(found["wal"].items()):
            w = eval_wal(wdir, files, now, args.wal_max_age)
            r.bump(w["status"])
            r.details.append("[%s] %s" % (w["status"], w["line"]))
            r.details += ["       " + n for n in w["notes"]]
            r.data.setdefault("wal", []).append(dict((k, v) for k, v in w.items() if k not in ("line", "notes")))
        for c in found["probackup"]:
            r.details.append("каталог pg_probackup: %s (разбор ниже)" % c)
        for c in found["pgbackrest"]:
            r.details.append("репозиторий pgBackRest: %s (разбор ниже)" % c)
        for e in found["errors"][:5]:
            r.bump("INFO")
            r.details.append("не прочитано: %s" % e)
        if not groups and not found["wal"] and not found["probackup"] and not found["pgbackrest"]:
            r.bump("WARN")
            r.summary += "; копий не нашёл"
        results.append(r)

    # pg_probackup
    for j in all_jobs:
        for m in re.finditer(r"(?:-B|--backup-path)[= ]+(/[^\s;|&]+)", j.get("text") or j.get("command") or ""):
            if os.path.isdir(m.group(1)) and is_probackup_catalog(m.group(1)) and m.group(1) not in probackup_catalogs:
                probackup_catalogs.append(m.group(1))
    bins = find_probackup()
    if probackup_catalogs or bins:
        r = Result("pg_probackup", "pg_probackup")
        if not bins:
            r.status, r.summary = "SKIP", "каталог есть, а программы pg_probackup на этом сервере нет"
        elif not probackup_catalogs:
            r.status, r.summary = "INFO", "%s установлен, каталог копий не найден: укажите --probackup-catalog" % bins[-1]
        else:
            r.summary = "%s; каталогов %d" % (os.path.basename(bins[-1]), len(probackup_catalogs))
            for c in probackup_catalogs:
                res = run(as_owner([bins[-1], "show", "-B", c, "--format=json"], c), timeout=60)
                arc = run(as_owner([bins[-1], "show", "-B", c, "--archive", "--format=json"], c), timeout=60)
                if not res or res[0] != 0:
                    r.bump("SKIP" if r.status == "OK" else r.status)
                    r.details.append("%s: %s (каталог читают под его владельцем, обычно postgres)" % (
                        c, short(first_error_line(res), 160)))
                    continue
                try:
                    show = json.loads(res[1])
                    archive = json.loads(arc[1]) if arc and arc[0] == 0 else []
                except ValueError:
                    r.details.append("%s: не удалось разобрать вывод show" % c)
                    continue
                st, lines, data = eval_probackup(show, archive, now, args.max_age)
                r.bump(st)
                r.details.append("каталог %s:" % c)
                r.details += ["  [%s] %s" % (s, l) for s, l in lines]
                r.data[c] = data
                r.details.append("  целостность: %s validate -B %s (читает копии целиком, долго)" % (os.path.basename(bins[-1]), c))
        results.append(r)

    # pgBackRest
    if which("pgbackrest") and (pgbackrest_repos or os.path.exists("/etc/pgbackrest.conf")
                                or os.path.exists("/etc/pgbackrest/pgbackrest.conf")):
        r = Result("pgbackrest", "pgBackRest")
        res = run(as_owner(["pgbackrest", "info", "--output=json"], pgbackrest_repos[0] if pgbackrest_repos else None),
                  timeout=60)
        if not res or res[0] != 0:
            r.status = "SKIP"
            r.summary = "%s; запускайте под пользователем pgBackRest: sudo -u postgres pgbackrest info" % short(
                first_error_line(res), 160)
        else:
            try:
                info = json.loads(res[1])
                st, lines = eval_pgbackrest(info, now, args.max_age)
                r.bump(st)
                r.summary = "stanza: %d" % len(info)
                r.details += ["[%s] %s" % (s, l) for s, l in lines]
            except ValueError:
                r.status, r.summary = "SKIP", "не удалось разобрать pgbackrest info"
        results.append(r)
    other = [n for n in ("wal-g", "barman", "restic", "rclone", "mc", "mcli") if which(n)]
    if other:
        r = Result("tools", "Другие инструменты копирования")
        r.status = "INFO"
        r.summary = "установлены: " + ", ".join(other)
        hints = {"wal-g": "wal-g backup-list --detail (нужны переменные окружения задания)",
                 "barman": "barman list-backup all", "restic": "restic snapshots (нужны пароль и адрес репозитория)",
                 "rclone": "rclone lsl <назначение> | tail", "mc": "mc ls --recursive <алиас>/<бакет> | tail",
                 "mcli": "mcli ls --recursive <алиас>/<бакет> | tail"}
        r.details = ["%s: %s" % (n, hints[n]) for n in other]
        results.append(r)

    # PostgreSQL
    r = Result("postgresql", "Архивация WAL со стороны PostgreSQL")
    if args.no_pg:
        r.status, r.summary = "SKIP", "отключено ключом --no-pg"
    elif pg_row is None:
        r.status = "SKIP"
        r.summary = "база не ответила: %s; запустите под postgres или с --psql-cmd" % (pg_err or "нет данных")
    else:
        st, lines = eval_pg(pg_row, args.wal_max_age)
        r.bump(st)
        r.summary = lines[0]
        r.details = lines[1:]
        if pg_row.get("db_bytes"):
            r.details.append("объём баз сервера %s: сравните с размером последней копии" % size(pg_row["db_bytes"]))
        r.data = dict((k, v) for k, v in pg_row.items() if k != "archive_command")
    results.append(r)

    # Directum Launcher
    r = Result("rx_config", "Конфигурация Directum RX")
    if not launcher:
        r.status = "SKIP"
        r.summary = "папка Directum Launcher не найдена (ищу etc/config.yml рядом с do.sh в /srv и /opt): укажите --launcher-dir"
    elif not cfg:
        r.status, r.summary = "SKIP", "не удалось прочитать %s (нужны права)" % cfg_path
    else:
        r.data = {"launcher": launcher[0], "config": cfg_path, "storage_paths": storage_paths,
                  "s3": bool(cfg.get("S3_STORAGE_SERVICE_ADDRESS")), "encrypted_config": bool(cfg.get("config_password")),
                  "key_service": "KeyDerivationService" in cfg, "pgbouncer": "SungeroPgBouncer" in cfg}
        parts = ["Directum Launcher: %s" % launcher[0]]
        if cfg.get("DATABASE_ENGINE"):
            parts.append("СУБД %s" % cfg["DATABASE_ENGINE"])
        cs = cfg.get("CONNECTION_STRING")
        if isinstance(cs, dict) and not cs.get("encrypted"):
            parts.append("база %s на %s" % (cs.get("database"), cs.get("host")))
        r.summary = ", ".join(parts)
        if storage_paths:
            r.details.append("файловое хранилище: %s" % ", ".join(storage_paths))
        if cfg.get("S3_STORAGE_SERVICE_ADDRESS"):
            r.details.append("в конфигурации есть объектное хранилище S3 (%s, бакет %s): копию бакета делают версионированием "
                             "и репликацией или зеркалом mc mirror или rclone" % (
                                 short(cfg["S3_STORAGE_SERVICE_ADDRESS"], 60), cfg.get("S3_STORAGE_BUCKET_NAME") or "по коду системы"))
            if not any(re.search(r"\bmc(?:li)? (mirror|cp)\b|\brclone\b|\brestic\b", t) for t in texts):
                r.bump("WARN")
                r.details.append("на этом сервере не видно копирования бакета (mc mirror, rclone, restic): "
                                 "уточните, кто и как копирует S3")
        for p in storage_paths:
            if not covered(p, texts, fs):
                r.bump("WARN")
                r.details.append("не видно задания, которое копирует хранилище %s: без него после восстановления "
                                 "карточки будут без содержимого" % p)
        if not covered(os.path.join(launcher[0], "etc"), texts, fs) and not covered(cfg_path, texts, fs):
            r.bump("WARN")
            r.details.append("не видно копирования %s: без него восстановление превращается в переустановку" % cfg_path)
        if cfg.get("DATA_PROTECTION_CERTIFICATE_FILE") or cfg.get("DATA_PROTECTION_CERTIFICATE_THUMBPRINT"):
            r.details.append("сертификат защиты данных задан (%s): нужен в копии вместе с паролем" % (
                "файл" if cfg.get("DATA_PROTECTION_CERTIFICATE_FILE") else "отпечаток"))
        if cfg.get("ssl_cert"):
            r.details.append("сертификат HTTPS для HAProxy: %s" % cfg["ssl_cert"])
        if cfg.get("config_password"):
            r.bump("INFO")
            r.details.append("параметры config.yml зашифрованы (config_password): без пароля шифрования копия "
                             "config.yml бесполезна, храните его в хранилище паролей")
        if "KeyDerivationService" in cfg:
            r.bump("INFO")
            r.details.append("есть сервис ключей: сертификаты шифрования и секреты сервиса нужны отдельно от копий документов")
        if cfg.get("PRIMARY_TENANT"):
            r.details.append("код системы PRIMARY_TENANT задан: запишите его в паспорт восстановления")
        if cfg.get("OBSOLETE_DATA_LIFE_PERIOD") or cfg.get("DELETED_DATA_LIFE_PERIOD"):
            r.details.append("срок хранения удалённого содержимого изменён: %s" % (
                cfg.get("DELETED_DATA_LIFE_PERIOD") or cfg.get("OBSOLETE_DATA_LIFE_PERIOD")))
    results.append(r)

    # Что не видно
    r = Result("checklist", "Проверьте руками: этого скрипт не видит")
    r.status = "INFO"
    r.summary = "%d пунктов" % len(CHECKLIST)
    r.details = ["- " + c for c in CHECKLIST]
    results.append(r)
    return results


def render_text(results):
    out = ["backup_inventory %s: резервные копии Directum RX на этом сервере" % VERSION]
    main = [x for x in results if x.status in ("CRIT", "WARN")]
    if main:
        out += ["", "Главное:"] + ["  [%s] %s: %s" % (x.status, x.title, x.summary)
                                   for x in sorted(main, key=lambda y: -RANK[y.status])]
    out.append("")
    for x in results:
        out.append("[%s]%s %s: %s" % (x.status, " " * (4 - len(x.status)), x.title, x.summary))
        out += ["       " + d for d in x.details]
    counts = dict((k, sum(1 for x in results if x.status == k)) for k in RANK)
    out += ["", "Итог: " + ", ".join("%s %d" % (k, counts[k]) for k in ("CRIT", "WARN", "INFO", "OK", "SKIP") if counts[k])]
    return mask("\n".join(out)) + "\n"


def exit_code(results):
    real = [x for x in results if x.key != "checklist"]
    if real and all(x.status == "SKIP" for x in real):
        return 2
    return 1 if any(x.status in ("WARN", "CRIT") for x in results) else 0


def emit(text, stream=None):
    """Вывод байтами UTF-8: в Python 3.6 при LANG=C обычный print падает на кириллице."""
    stream = stream or sys.stdout
    buf = getattr(stream, "buffer", None)
    if buf is None:
        stream.write(text)
        return
    try:
        data = text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        data = text.encode("utf-8", "replace")
    stream.flush()
    buf.write(data)
    buf.flush()


class Parser(argparse.ArgumentParser):
    """argparse печатает справку и ошибки тоже через emit."""

    def _print_message(self, message, file=None):
        if message:
            emit(message, file or sys.stderr)


def build_args(argv):
    p = Parser(prog="backup_inventory.py", description=__doc__.split("\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="\n".join(__doc__.split("\n")[2:]))
    p.add_argument("--dir", action="append", help="каталог с копиями, можно несколько раз")
    p.add_argument("--max-age", type=float, default=26, help="ожидаемый интервал копий, ч (по умолчанию 26)")
    p.add_argument("--wal-max-age", type=float, default=2, help="сколько часов без новых сегментов WAL терпимо")
    p.add_argument("--shrink", type=float, default=20, help="на сколько %% последняя копия может быть меньше прошлых")
    p.add_argument("--depth", type=int, default=3, help="глубина обхода каталогов")
    p.add_argument("--ignore", action="append", help="шаблон имён, которые пропустить (например '*.log')")
    p.add_argument("--probackup-catalog", action="append", help="каталог pg_probackup (-B), если не найден сам")
    p.add_argument("--pgdata", help="каталог данных PostgreSQL для сверки разделов")
    p.add_argument("--storage-path", action="append", help="каталог файлового хранилища RX, если config.yml не найден")
    p.add_argument("--launcher-dir", help="папка Directum Launcher (в ней etc/config.yml и do.sh)")
    p.add_argument("-d", "--dbname", help="база или строка подключения psql для статуса архивации WAL")
    p.add_argument("--psql", default="psql", help="путь к psql")
    p.add_argument("--psql-cmd", help="команда вместо psql, например \"docker exec -i rx-postgres psql -U postgres\"")
    p.add_argument("--no-pg", action="store_true", help="не обращаться к PostgreSQL")
    p.add_argument("--json", action="store_true", help="вывод в JSON")
    return p.parse_args(argv)


def main(argv=None):
    args = build_args(sys.argv[1:] if argv is None else argv)
    results = collect(args)
    code = exit_code(results)
    if args.json:
        doc = {"tool": "backup_inventory", "version": VERSION, "checks": [deep_mask(x.as_dict()) for x in results],
               "exit_code": code}
        emit(json.dumps(doc, ensure_ascii=False, indent=2, default=str) + "\n")  # строки уже замаскированы
    else:
        emit(render_text(results))
    return code


if __name__ == "__main__":
    sys.exit(main())
