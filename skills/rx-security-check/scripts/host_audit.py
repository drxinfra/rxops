#!/usr/bin/env python3
"""Проверка Linux-сервера Directum RX на типовые дыры безопасности. Только чтение.

Использование:
  sudo python3 host_audit.py [--config-dir КАТАЛОГ ...] [--tls ХОСТ:ПОРТ ...] [--json]
  python3 host_audit.py --only sshd,ports      только выбранные проверки
  python3 host_audit.py --list                 список проверок

Ключи:
  --config-dir КАТАЛОГ   где искать файлы с паролями, доступные всем на чтение (можно несколько раз):
                         каталог etc в папке Directum Launcher, /etc/rabbitmq, /etc/postgresql и т.п.
  --tls [СХЕМА://]ХОСТ:ПОРТ   сертификат, цепочка, имя и старые протоколы (можно несколько раз).
                         pg:// включает STARTTLS PostgreSQL, ldap:// и smtp:// тоже STARTTLS,
                         без схемы обычный TLS (443, 5671, 636, 9200)
  --tls-ca ФАЙЛ          корневой сертификат своего центра для проверки цепочки
  --tls-warn-days N      WARN, если до конца срока меньше N дней (по умолчанию 30)
  --tls-crit-days N      CRIT, если меньше N дней (по умолчанию 7)
  --updates-days N       WARN, если пакеты не обновлялись дольше N дней (по умолчанию 90)
  --timeout СЕК          тайм-аут на каждую внешнюю команду и соединение (по умолчанию 5)
  --only СПИСОК          только эти проверки через запятую
  --skip СПИСОК          все, кроме этих
  --json                 вывод в JSON

Без root скрипт работает, но то, что без root не проверить, помечает ERROR с причиной.
Статусы: OK, WARN, CRIT, INFO (сведения), SKIP (не применимо или не задано), ERROR (не удалось).
Код возврата: 1 есть WARN или CRIT; иначе 2 есть ERROR; иначе 0.
Пароли и ключи не выводятся: про файлы печатаются путь, права и число найденных значений.
Ничего не меняет, не перезапускает и не пишет на диск. Python 3.6+, только стандартная библиотека.
"""
import datetime
import glob
import ipaddress
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys

sys.dont_write_bytecode = True  # импорт соседнего модуля не должен оставлять __pycache__
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import mask_secrets
except ImportError:  # pragma: no cover
    mask_secrets = None

try:
    import pwd
    import grp
except ImportError:  # pragma: no cover
    pwd = grp = None

VERSION = "0.1.0"
NEED_ROOT = "Нужен root: с согласия администратора запустите через sudo."
SBIN = ["/usr/local/sbin", "/usr/sbin", "/sbin", "/usr/local/bin", "/usr/bin", "/bin"]
ORDER = {"CRIT": 0, "WARN": 1, "ERROR": 2, "INFO": 3, "OK": 4, "SKIP": 5}


def write(stream, text):
    """Пишет UTF-8 байтами: в Python 3.6 при LANG=C текстовый поток ASCII и падает на кириллице."""
    try:
        data = text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        data = text.encode("utf-8", "backslashreplace")
    buf = getattr(stream, "buffer", None)
    if buf is not None:
        buf.write(data)
        buf.flush()
    else:
        stream.write(text)


def fix_arg(a):
    """Аргумент как UTF-8: в Python 3.6 при LANG=C русские буквы приходят суррогатами."""
    try:
        return os.fsencode(a).decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return a


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def res(cid, title, status, detail="", hint="", questions=()):
    return {"id": cid, "title": title, "status": status, "detail": detail, "hint": hint,
            "questions": list(questions)}


def worst(statuses):
    st = [s for s in statuses if s]
    return min(st, key=lambda s: ORDER[s]) if st else "OK"


def first_line(text):
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:200]
    return ""


def read_text(path, limit=4 * 1024 * 1024):
    """(текст, ошибка): ошибка None, 'missing', 'denied' или текст исключения."""
    try:
        with open(path, "rb") as f:
            data = f.read(limit)
    except FileNotFoundError:
        return None, "missing"
    except PermissionError:
        return None, "denied"
    except OSError as e:
        return None, str(e)
    return data.decode("utf-8", "replace"), None


class Runner:
    """Запуск внешних команд с тайм-аутом, C-локалью и sbin в PATH."""

    def __init__(self, timeout=5):
        self.timeout = timeout
        parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
        for p in SBIN:
            if p not in parts:
                parts.append(p)
        self.path = os.pathsep.join(parts)
        self.env = dict(os.environ, LC_ALL="C", LANG="C", PATH=self.path, SYSTEMD_PAGER="", PAGER="cat")

    def which(self, name):
        return shutil.which(name, path=self.path)

    def run(self, cmd, timeout=None, stdin_data=None):
        """(код, stdout, stderr). Код None: команды нет, тайм-аут или ошибка запуска."""
        exe = self.which(cmd[0])
        if not exe:
            return None, "", "нет команды %s" % cmd[0]
        kw = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "env": self.env,
              "timeout": timeout or self.timeout}
        if stdin_data is None:
            kw["stdin"] = subprocess.DEVNULL
        else:
            kw["input"] = stdin_data.encode("utf-8") if isinstance(stdin_data, str) else stdin_data
        try:
            p = subprocess.run([exe] + list(cmd[1:]), **kw)
        except subprocess.TimeoutExpired:
            return None, "", "тайм-аут %s с" % (timeout or self.timeout)
        except OSError as e:
            return None, "", str(e)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


class Ctx:
    def __init__(self, opts, runner=None, root=None, rootfs="/", now=None):
        self.opts = opts
        self.run = runner or Runner(opts.get("timeout", 5))
        self.root = (os.geteuid() == 0) if root is None else root
        self.rootfs = rootfs
        self.now = now or utcnow()
        self.cache = {}

    def p(self, path):
        """Путь в проверяемой системе (rootfs нужен тестам)."""
        if self.rootfs == "/":
            return path
        return os.path.join(self.rootfs, path.lstrip("/"))

    def unp(self, path):
        if self.rootfs != "/" and path.startswith(self.rootfs):
            return "/" + path[len(self.rootfs):].lstrip("/")
        return path

    def procs(self):
        """Имена всех процессов из /proc/*/comm."""
        if "procs" not in self.cache:
            names = set()
            for d in glob.glob(self.p("/proc/[0-9]*/comm")):
                t, _ = read_text(d, 256)
                if t:
                    names.add(t.strip())
            self.cache["procs"] = names
        return self.cache["procs"]

    def service(self, unit, proc=None):
        """active / inactive / failed ... или None, если узнать не удалось."""
        rc, out, err = self.run.run(["systemctl", "is-active", unit])
        state = out.strip().splitlines()[-1] if out.strip() else ""
        if rc is not None and state in ("active", "inactive", "failed", "activating", "deactivating",
                                        "reloading", "unknown"):
            return state
        if proc and self.procs():
            return "active" if proc in self.procs() else "inactive"
        return None


# ---------------------------------------------------------------- system

def os_release(ctx):
    text, _ = read_text(ctx.p("/etc/os-release"))
    info = {}
    for line in (text or "").splitlines():
        m = re.match(r"^([A-Z_]+)=(.*)$", line.strip())
        if m:
            info[m.group(1)] = m.group(2).strip().strip("\"'")
    return info


def in_container(ctx):
    if os.path.exists(ctx.p("/.dockerenv")) or os.path.exists(ctx.p("/run/.containerenv")):
        return True
    text, _ = read_text(ctx.p("/proc/1/cgroup"))
    if text and re.search(r"docker|kubepods|containerd|libpod|lxc", text):
        return True
    env, _ = read_text(ctx.p("/proc/1/environ"))
    return bool(env and "container=" in env)


def check_system(ctx):
    info = os_release(ctx)
    name = info.get("PRETTY_NAME") or "%s %s" % (info.get("NAME", "?"), info.get("VERSION", ""))
    if not info:
        name = "не Linux или нет /etc/os-release (%s)" % sys.platform
    astra, _ = read_text(ctx.p("/etc/astra_version"))
    cont = in_container(ctx)
    ctx.cache["container"] = cont
    detail = ["ОС: %s" % name.strip()]
    if astra:
        detail.append("Astra Linux, /etc/astra_version: %s" % first_line(astra))
    detail.append("ядро: %s" % os.uname().release)
    detail.append("запуск от root: %s" % ("да" if ctx.root else "нет"))
    if cont:
        detail.append("похоже на контейнер: часть проверок относится к хосту, а не к контейнеру")
    hint = "" if ctx.root else ("Запуск без root: проверки, которым нужен root, помечены ERROR. С согласия "
                                "администратора повторите через sudo.")
    return [res("system", "Сервер", "INFO", "\n".join(detail),
                hint + (" " if hint else "") + "Сверьте версии ОС и СУБД со сроками поддержки производителя.",
                ["q13"])]


# ---------------------------------------------------------------- sshd

SSH_KEYS = ("permitrootlogin", "passwordauthentication", "permitemptypasswords", "kbdinteractiveauthentication",
            "challengeresponseauthentication", "usepam", "authenticationmethods", "pubkeyauthentication")
SSH_DEFAULTS = {"permitrootlogin": "prohibit-password", "passwordauthentication": "yes",
                "permitemptypasswords": "no", "kbdinteractiveauthentication": "yes", "usepam": "no",
                "authenticationmethods": "any", "pubkeyauthentication": "yes"}


def parse_sshd_T(text):
    """Вывод `sshd -T`: ключи уже в нижнем регистре, значения действующие."""
    cfg = {}
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() in SSH_KEYS and parts[0].lower() not in cfg:
            cfg[parts[0].lower()] = parts[1].strip().lower()
    if "kbdinteractiveauthentication" not in cfg and "challengeresponseauthentication" in cfg:
        cfg["kbdinteractiveauthentication"] = cfg["challengeresponseauthentication"]
    return cfg


def parse_sshd_files(ctx, path="/etc/ssh/sshd_config"):
    """Разбор sshd_config и Include: первое значение побеждает, блоки Match пропускаются."""
    cfg, st = {}, {"files": [], "denied": [], "matches": 0, "missing": False}
    _sshd_file(ctx, path, cfg, st, 0, False)
    if "kbdinteractiveauthentication" not in cfg and "challengeresponseauthentication" in cfg:
        cfg["kbdinteractiveauthentication"] = cfg["challengeresponseauthentication"]
    return cfg, st


def _sshd_file(ctx, path, cfg, st, depth, in_match):
    if depth > 8:
        return
    text, err = read_text(ctx.p(path))
    if text is None:
        if err == "denied":
            st["denied"].append(path)
        elif depth == 0:
            st["missing"] = True
        return
    st["files"].append(path)
    match = in_match
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z][A-Za-z0-9]*)(?:\s*=\s*|\s+)(.*)$", line)
        if not m:
            continue
        key, val = m.group(1).lower(), m.group(2).strip()
        if key == "match":
            match = True
            st["matches"] += 1
            continue
        if key == "include":
            for pat in val.split():
                pat = pat.strip("\"'")
                if not pat.startswith("/"):
                    pat = "/etc/ssh/" + pat
                for inc in sorted(glob.glob(ctx.p(pat))):
                    _sshd_file(ctx, ctx.unp(inc), cfg, st, depth + 1, match)
            continue
        if match or key not in SSH_KEYS or key in cfg:
            continue
        cfg[key] = val.lower() if key == "authenticationmethods" else (val.split() or [""])[0].strip("\"'").lower()


def password_only_possible(am):
    """Можно ли войти одним паролем при данном AuthenticationMethods."""
    if am in ("", "any"):
        return True
    for lst in am.split():
        methods = [x.split(":")[0] for x in lst.split(",")]
        if "publickey" not in methods and ("password" in methods or "keyboard-interactive" in methods):
            return True
    return False


def sshd_results(cfg, source, note="", partial=False):
    def g(k):
        return cfg.get(k, SSH_DEFAULTS.get(k, ""))
    unset = [k for k in ("permitrootlogin", "passwordauthentication", "kbdinteractiveauthentication", "usepam")
             if k not in cfg]
    pra, pa, kbd, pam = g("permitrootlogin"), g("passwordauthentication"), g("kbdinteractiveauthentication"), g("usepam")
    empty, am = g("permitemptypasswords"), g("authenticationmethods")
    via = []
    if pa == "yes":
        via.append("PasswordAuthentication yes")
    if kbd == "yes" and pam == "yes":
        via.append("KbdInteractiveAuthentication yes при UsePAM yes")
    pw = bool(via) and password_only_possible(am)
    tail = ["источник: " + source]
    if unset and "sshd -T" not in source:
        tail.append("не заданы явно, взяты умолчания OpenSSH: " + ", ".join(
            "%s %s" % (k, SSH_DEFAULTS[k]) for k in unset))
    if note:
        tail.append(note)
    out = []
    if pra == "no":
        st, d, h = "OK", "PermitRootLogin no", ""
    elif pra in ("prohibit-password", "without-password", "forced-commands-only"):
        st, d = "WARN", "PermitRootLogin %s: под root можно войти по ключу" % pra
        h = ("Входите под именными учётками и повышайте права через sudo: тогда в журнале видно, кто работал. "
             "Целевое значение PermitRootLogin no; правка sshd_config и перезапуск sshd только с согласия, "
             "после проверки `sshd -t` и при открытой второй сессии.")
    elif pra == "yes":
        st = "CRIT" if pw else "WARN"
        d = "PermitRootLogin yes" + (": root входит и по паролю" if pw else "")
        h = ("Запретите вход под root (PermitRootLogin no) и заходите под именными учётками. "
             "Правка sshd_config и перезапуск sshd только с согласия, после `sshd -t`.")
    else:
        st, d, h = "INFO", "PermitRootLogin %s" % pra, ""
    out.append(res("sshd.root", "Вход под root по SSH", st, "\n".join([d] + tail), h, ["q7", "q8"]))
    if pw and empty == "yes":
        st, d = "CRIT", "PermitEmptyPasswords yes и вход по паролю разрешён: учётка с пустым паролем входит без пароля"
        h = "Поставьте PermitEmptyPasswords no и PasswordAuthentication no, проверьте учётки с пустым паролем."
    elif pw:
        st, d = "WARN", "Вход по паролю разрешён: " + ", ".join(via)
        h = ("Оставьте вход по ключам (PasswordAuthentication no, KbdInteractiveAuthentication no) и доступ "
             "через VPN или промежуточный сервер. Перед правкой убедитесь, что ключи у всех администраторов "
             "уже работают.")
    else:
        st, d, h = "OK", "Вход только по ключу" + (" (AuthenticationMethods %s)" % am if am not in ("", "any") else ""), ""
    out.append(res("sshd.password", "Вход по паролю по SSH", st, "\n".join([d] + tail), h, ["q8"]))
    if partial:
        for r in out:
            r["detail"] += "\nчасть файлов конфигурации не прочитана, итог неточен"
            r["status"] = "ERROR"
            r["hint"] = NEED_ROOT
    return out


def check_sshd(ctx):
    exe = ctx.run.which("sshd")
    conf = ctx.p("/etc/ssh/sshd_config")
    if not exe and not os.path.exists(conf):
        return [res("sshd", "SSH-сервер", "SKIP", "sshd не установлен", "", ["q8"])]
    note = ""
    if ctx.root and exe:
        rc, out, err = ctx.run.run(["sshd", "-T"])
        if rc == 0 and out.strip():
            return sshd_results(parse_sshd_T(out), "sshd -T (действующие значения)")
        note = "sshd -T не сработал (%s), разобраны файлы" % first_line(err or out or "нет вывода")
    cfg, st = parse_sshd_files(ctx)
    if not st["files"]:
        why = "нет прав на /etc/ssh/sshd_config" if st["denied"] else "нет /etc/ssh/sshd_config"
        return [res("sshd.root", "Вход под root по SSH", "ERROR", why, NEED_ROOT if st["denied"] else "", ["q7", "q8"]),
                res("sshd.password", "Вход по паролю по SSH", "ERROR", why, NEED_ROOT if st["denied"] else "", ["q8"])]
    src = "разбор " + ", ".join(st["files"][:5]) + (" и ещё %d" % (len(st["files"]) - 5) if len(st["files"]) > 5 else "")
    if st["matches"]:
        note = (note + "; " if note else "") + ("блоков Match: %d, для части пользователей и адресов значения "
                                                "могут отличаться" % st["matches"])
    if st["denied"]:
        note = (note + "; " if note else "") + "нет прав на: " + ", ".join(st["denied"][:5])
    return sshd_results(cfg, src, note, partial=bool(st["denied"]))


# ---------------------------------------------------------------- accounts

def parse_passwd(text):
    out = []
    for line in (text or "").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        f = line.split(":")
        if len(f) < 7:
            continue
        try:
            uid, gid = int(f[2]), int(f[3])
        except ValueError:
            continue
        out.append({"name": f[0], "uid": uid, "gid": gid, "home": f[5], "shell": f[6].strip()})
    return out


def parse_shadow_empty(text):
    """Учётки с пустым полем пароля: вход без пароля, если это разрешает PAM или sshd."""
    out = []
    for line in (text or "").splitlines():
        f = line.split(":")
        if len(f) >= 2 and f[0] and f[1] == "":
            out.append(f[0])
    return out


NOLOGIN = ("nologin", "false", "sync", "shutdown", "halt", "true")


def has_login_shell(shell):
    return bool(shell) and os.path.basename(shell) not in NOLOGIN


def count_keys(text):
    return sum(1 for l in (text or "").splitlines() if l.strip() and not l.strip().startswith("#"))


def check_accounts(ctx):
    text, err = read_text(ctx.p("/etc/passwd"))
    if text is None:
        return [res("accounts", "Учётные записи", "ERROR", "не прочитать /etc/passwd: %s" % err, "", ["q7"])]
    users = parse_passwd(text)
    out = []
    uid0 = [u["name"] for u in users if u["uid"] == 0 and u["name"] != "root"]
    if uid0:
        out.append(res("accounts.uid0", "UID 0 кроме root", "CRIT", "UID 0 у: " + ", ".join(uid0),
                       "Вторая учётка с UID 0 это второй root, часто след взлома или старой установки. "
                       "Выясните владельца; отключать только после этого.", ["q7"]))
    else:
        out.append(res("accounts.uid0", "UID 0 кроме root", "OK", "UID 0 только у root", "", ["q7"]))
    login = [u for u in users if has_login_shell(u["shell"])]
    keys, hidden = [], 0
    for u in login:
        for name in ("authorized_keys", "authorized_keys2"):
            t, e = read_text(ctx.p(os.path.join(u["home"] or "/nonexistent", ".ssh", name)))
            if t is not None:
                n = count_keys(t)
                if n:
                    keys.append("%s %d" % (u["name"], n))
            elif e == "denied":
                hidden += 1
                break
    d = ["учёток с оболочкой входа: %d: %s" % (len(login), ", ".join(u["name"] for u in login[:40]))]
    if keys:
        d.append("ключей SSH: " + ", ".join(keys))
    if hidden:
        d.append("ключи части учёток не видны без root: %d" % hidden)
    out.append(res("accounts.login", "Учётки со входом", "INFO", "\n".join(d),
                   "Сверьте список с реестром доступов: у каждого администратора своя учётка, учётки и ключи "
                   "ушедших отключены, общих учёток вида admin нет.", ["q7"]))
    sh, err = read_text(ctx.p("/etc/shadow"))
    if sh is None:
        if err == "denied":
            out.append(res("accounts.emptypw", "Пустые пароли", "ERROR", "нет прав на /etc/shadow", NEED_ROOT, ["q7"]))
        else:
            out.append(res("accounts.emptypw", "Пустые пароли", "SKIP", "нет /etc/shadow", "", ["q7"]))
    else:
        empty = parse_shadow_empty(sh)
        if empty:
            out.append(res("accounts.emptypw", "Пустые пароли", "CRIT", "пустой пароль у: " + ", ".join(empty),
                           "Заблокируйте пароль (passwd -l) или задайте его; сначала выясните, зачем учётка.",
                           ["q7"]))
        else:
            out.append(res("accounts.emptypw", "Пустые пароли", "OK", "учёток с пустым паролем нет", "", ["q7"]))
    return out


# ---------------------------------------------------------------- sudo

ALIAS_RE = re.compile(r"^(User_Alias|Runas_Alias|Host_Alias|Cmnd_Alias|Cmd_Alias)\b")


def logical_lines(text):
    """Строки sudoers с учётом переноса через обратную косую черту, с номером первой строки."""
    buf, start = "", 0
    for i, line in enumerate((text or "").splitlines(), 1):
        if not buf:
            start = i
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        yield start, buf
        buf = ""
    if buf:
        yield start, buf


def parse_sudoers(text):
    """→ (nopasswd [(строка, кто, на все команды)], строки с !authenticate, include [(вид, путь)])."""
    nopw, noauth, inc = [], [], []
    for n, line in logical_lines(text):
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^[#@](include|includedir)\s+(.+)$", s)
        if m:
            inc.append((m.group(1), m.group(2).strip().strip('"')))
            continue
        if s.startswith("#") and not re.match(r"^#\d+\s", s):
            continue
        s = re.split(r"\s#(?!\d)", s, maxsplit=1)[0].strip()
        if ALIAS_RE.match(s):
            continue
        if s.startswith("Defaults"):
            if re.search(r"!\s*authenticate\b", s):
                noauth.append(n)
            continue
        if "NOPASSWD:" in s:
            seg = s.split("NOPASSWD:", 1)[1]
            seg = re.split(r"(?<!NO)PASSWD:", seg, maxsplit=1)[0]
            all_cmds = "ALL" in re.split(r"[,\s]+", seg)
            nopw.append((n, s.split()[0], all_cmds))
    return nopw, noauth, inc


def sudoers_scan(ctx, path="/etc/sudoers"):
    """Обходит sudoers и включаемые файлы. → (найдено, прочитанные файлы, нет прав, нет главного файла)."""
    found, files, denied = [], [], []
    seen = set()

    def visit(p, depth):
        if depth > 8 or p in seen:
            return
        seen.add(p)
        text, err = read_text(ctx.p(p))
        if text is None:
            if err == "denied":
                denied.append(p)
            return
        files.append(p)
        nopw, noauth, inc = parse_sudoers(text)
        for n, who, allc in nopw:
            found.append(("nopasswd", p, n, who, allc))
        for n in noauth:
            found.append(("noauth", p, n, "Defaults", True))
        for kind, target in inc:
            target = target.replace("%%", "%")
            if not target.startswith("/"):
                target = os.path.join(os.path.dirname(p), target)
            if kind == "includedir":
                try:
                    names = sorted(os.listdir(ctx.p(target)))
                except PermissionError:
                    denied.append(target)
                    continue
                except OSError:
                    continue
                for name in names:
                    if name.endswith("~") or "." in name:
                        continue  # sudo пропускает такие файлы в includedir
                    visit(os.path.join(target, name), depth + 1)
            else:
                visit(target, depth + 1)

    exists = os.path.exists(ctx.p(path))
    visit(path, 0)
    return found, files, denied, not exists


def group_members(ctx, names):
    text, _ = read_text(ctx.p("/etc/group"))
    users = parse_passwd(read_text(ctx.p("/etc/passwd"))[0] or "")
    out = {}
    for line in (text or "").splitlines():
        f = line.split(":")
        if len(f) >= 4 and f[0] in names:
            try:
                gid = int(f[2])
            except ValueError:
                gid = None
            mem = [m for m in f[3].strip().split(",") if m]
            mem += [u["name"] for u in users if u["gid"] == gid and u["name"] not in mem]
            if mem:
                out[f[0]] = mem
    return out


def check_sudo(ctx):
    out = []
    found, files, denied, missing = sudoers_scan(ctx)
    if missing:
        out.append(res("sudo.nopasswd", "sudo без пароля", "SKIP", "нет /etc/sudoers", "", ["q7"]))
    elif not files:
        out.append(res("sudo.nopasswd", "sudo без пароля", "ERROR", "нет прав на /etc/sudoers", NEED_ROOT, ["q7"]))
    else:
        bad = [f for f in found if f[4]]
        lines = ["%s:%d %s%s" % (f[1], f[2], f[3], "" if f[0] == "nopasswd" else " !authenticate") +
                 ("" if f[4] else " (только перечисленные команды)") for f in found]
        d = "\n".join(lines) if lines else "NOPASSWD и !authenticate не найдены"
        d += "\nпрочитано файлов: %d" % len(files)
        if denied:
            d += "\nнет прав на: " + ", ".join(denied)
        if bad:
            st = "WARN"
        elif found:
            st = "INFO"
        else:
            st = "ERROR" if denied else "OK"
        hint = ("sudo без пароля превращает украденный ключ или открытую сессию в root. Оставьте NOPASSWD только "
                "сервисным учёткам с узким списком команд." if found else (NEED_ROOT if denied else ""))
        out.append(res("sudo.nopasswd", "sudo без пароля", st, d, hint, ["q7"]))
    groups = group_members(ctx, ("sudo", "wheel", "admin", "docker", "lxd"))
    if groups:
        d = "\n".join("%s: %s" % (g, ", ".join(m)) for g, m in sorted(groups.items()))
        hint = "Участники группы docker фактически root на этом сервере: считайте их администраторами наравне с sudo."
        out.append(res("sudo.groups", "Группы с правами администратора", "INFO", d,
                       hint if "docker" in groups else "Сверьте участников с реестром доступов.", ["q7"]))
    return out


# ---------------------------------------------------------------- auditd

def parse_auditctl_s(text):
    d = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.match(r"^[a-z_]+$", parts[0]):
            try:
                d[parts[0]] = int(parts[1])
            except ValueError:
                d[parts[0]] = parts[1]
    return d


def parse_auditctl_l(text):
    rules = [l for l in (text or "").splitlines() if l.strip().startswith("-")]
    keyed = [l for l in rules if re.search(r"(\s-k\s|\skey=)", " " + l + " ")]
    return len(rules), len(keyed)


def check_auditd(ctx):
    exe = ctx.run.which("auditctl") or ctx.run.which("auditd")
    if not exe and not os.path.isdir(ctx.p("/etc/audit")):
        return [res("auditd", "Аудит действий (auditd)", "WARN", "auditd не установлен",
                    "Без подсистемы аудита на вопрос «кто и что менял под root» ответа нет. Установите auditd, "
                    "начните с коротких правил с ключами (вход, sudo, учётки, конфиги, ключи, время, сам аудит).",
                    ["q11"])]
    out = []
    state = ctx.service("auditd", proc="auditd")
    if state == "active":
        out.append(res("auditd.service", "Служба auditd", "OK", "служба работает", "", ["q11"]))
    elif state is None:
        out.append(res("auditd.service", "Служба auditd", "ERROR", "состояние службы узнать не удалось", "", ["q11"]))
    else:
        out.append(res("auditd.service", "Служба auditd", "WARN", "служба не работает: %s" % state,
                       "Включение службы меняет систему: только с согласия и после проверки места под журнал.",
                       ["q11"]))
    if not ctx.root:
        out.append(res("auditd.rules", "Правила аудита", "ERROR", "auditctl -s и -l требуют root", NEED_ROOT, ["q11"]))
        return out
    rc, s_out, s_err = ctx.run.run(["auditctl", "-s"])
    rc2, l_out, l_err = ctx.run.run(["auditctl", "-l"])
    if rc != 0 or rc2 != 0:
        out.append(res("auditd.rules", "Правила аудита", "ERROR",
                       "auditctl не ответил: %s" % first_line(s_err or l_err or s_out),
                       "В контейнере аудит ядра недоступен, проверяйте на хосте.", ["q11"]))
        return out
    s = parse_auditctl_s(s_out)
    n, keyed = parse_auditctl_l(l_out)
    d = ["правил загружено: %d, с ключами (-k): %d" % (n, keyed)]
    st, hint = "OK", ""
    if s.get("enabled") == 0:
        st, hint = "WARN", "Аудит выключен в ядре (enabled 0)."
        d.append("enabled 0")
    elif s.get("enabled") == 2:
        d.append("enabled 2: правила неизменяемы до перезагрузки")
    lost = s.get("lost")
    if isinstance(lost, int) and lost > 0:
        st = "WARN"
        d.append("потеряно событий: %d" % lost)
        hint = ("Ненулевой и растущий счётчик lost значит, что часть событий не записана. Лечится сокращением "
                "правил, а не увеличением буфера.")
    elif lost is not None:
        d.append("потеряно событий: 0")
    if n == 0:
        st = "WARN"
        hint = ("Служба работает, но правил нет: пишутся только стандартные события. Добавьте правила с ключами "
                "по категориям из статьи про auditd.")
    elif keyed < n:
        d.append("правила без ключей: искать по ним неудобно")
    out.append(res("auditd.rules", "Правила аудита", st, "\n".join(d), hint, ["q11"]))
    return out


# ---------------------------------------------------------------- logship

SHIPPERS = ("vector", "fluent-bit", "fluentd", "td-agent", "td-agent-bit", "filebeat", "auditbeat", "promtail",
            "alloy", "grafana-agent", "otelcol", "otelcol-contrib", "splunkd", "nxlog", "logstash")


def rsyslog_forwards(text):
    out = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if re.search(r"type\s*=\s*\"(omfwd|omrelp)\"", s) or re.search(r"(^|\s)@@?\(?[\w\[\].:-]+", s):
            out.append(s[:120])
    return out


def check_logship(ctx):
    found, denied = [], []
    for p in ("/etc/audit/plugins.d/au-remote.conf", "/etc/audisp/plugins.d/au-remote.conf"):
        t, e = read_text(ctx.p(p))
        if t is not None and re.search(r"^\s*active\s*=\s*yes", t, re.M):
            found.append("audisp-remote включён (%s)" % p)
        elif e == "denied":
            denied.append(p)
    for p in [ctx.p("/etc/rsyslog.conf")] + sorted(glob.glob(ctx.p("/etc/rsyslog.d/*.conf"))):
        t, e = read_text(p)
        if t is not None:
            for r in rsyslog_forwards(t):
                found.append("rsyslog пересылает: %s (%s)" % (r, ctx.unp(p)))
        elif e == "denied":
            denied.append(ctx.unp(p))
    for p in [ctx.p("/etc/syslog-ng/syslog-ng.conf")] + sorted(glob.glob(ctx.p("/etc/syslog-ng/conf.d/*.conf"))):
        t, _ = read_text(p)
        if t and re.search(r"^\s*destination\b[^{]*\{[^}]*\b(network|tcp|udp|syslog)\s*\(", t, re.M | re.S):
            found.append("syslog-ng: есть сетевой destination (%s)" % ctx.unp(p))
    procs = ctx.procs()
    for name in SHIPPERS:
        if name in procs:
            found.append("работает агент: %s" % name)
    if ctx.service("systemd-journal-upload") == "active":
        found.append("работает systemd-journal-upload")
    if found:
        return [res("logship", "Журналы уходят с сервера", "OK", "\n".join(found[:20]),
                    "Проверьте в хранилище, что события за сегодня с этого сервера там есть.", ["q11", "q18"])]
    if denied:
        return [res("logship", "Журналы уходят с сервера", "ERROR",
                    "пересылка не найдена, но нет прав на: " + ", ".join(denied), NEED_ROOT, ["q11", "q18"])]
    return [res("logship", "Журналы уходят с сервера", "WARN",
                "не найдены audisp-remote, пересылка rsyslog или syslog-ng, агенты сбора журналов",
                "Журнал, который лежит только на этом сервере, правит тот же администратор. Если журналы "
                "увозит другой механизм, отметьте это в ответе на вопрос q11.", ["q11", "q18"])]


# ---------------------------------------------------------------- firewall

def parse_nft(text):
    """Есть ли фильтрация входящих: базовая цепочка hook input с policy drop или правилами drop/reject."""
    chains, depth, cur, cur_depth, table = [], 0, None, 0, ""
    for line in (text or "").splitlines():
        s = line.strip()
        mt = re.match(r"^table\s+(\S+)\s+(\S+)\s*\{", s)
        if mt and depth == 0:
            table = mt.group(1) + " " + mt.group(2)
        mc = re.match(r"^chain\s+(\S+)\s*\{", s)
        if mc and depth == 1 and cur is None:
            cur = {"table": table, "name": mc.group(1), "input": False, "drop_policy": False, "drop_rules": 0}
            cur_depth = depth + 1
        elif cur is not None and depth >= cur_depth:
            if re.search(r"\btype\s+filter\s+hook\s+input\b", s):
                cur["input"] = True
            if re.search(r"\bpolicy\s+drop\b", s):
                cur["drop_policy"] = True
            elif not s.startswith("type ") and re.search(r"\b(drop|reject)\b", s):
                cur["drop_rules"] += 1
        depth += s.count("{") - s.count("}")
        if cur is not None and depth < cur_depth:
            chains.append(cur)
            cur = None
    inputs = [c for c in chains if c["input"]]
    filt = [c for c in inputs if c["drop_policy"] or c["drop_rules"]]
    return {"chains": len(chains), "inputs": inputs, "filtering": filt}


def parse_iptables_input(text):
    pol = re.search(r"^-P INPUT (\w+)", text or "", re.M)
    drops = re.findall(r"^-A INPUT\b.*-j (DROP|REJECT)\b", text or "", re.M)
    return bool(pol and pol.group(1) == "DROP") or bool(drops)


def check_firewall(ctx):
    found, empty, unknown = [], [], []
    if ctx.service("firewalld", proc="firewalld") == "active":
        found.append("firewalld активен")
    t, _ = read_text(ctx.p("/etc/ufw/ufw.conf"))
    if t and re.search(r"^\s*ENABLED\s*=\s*yes", t, re.M | re.I):
        if ctx.root and ctx.run.which("ufw"):
            rc, out, _ = ctx.run.run(["ufw", "status"])
            if rc == 0 and re.search(r"Status:\s*active", out):
                found.append("ufw активен")
            else:
                empty.append("ufw включён в ufw.conf, но ufw status не active")
        else:
            found.append("ufw включён (ENABLED=yes в /etc/ufw/ufw.conf)")
    nft = ctx.run.which("nft")
    if nft:
        if ctx.root:
            rc, out, err = ctx.run.run(["nft", "list", "ruleset"])
            if rc == 0:
                p = parse_nft(out)
                if p["filtering"]:
                    found.append("nftables фильтрует входящие: " + ", ".join(
                        "%s %s" % (c["table"], c["name"]) for c in p["filtering"]))
                else:
                    empty.append("nftables: цепочек %d, фильтрации входящих нет" % p["chains"])
            else:
                unknown.append("nft list ruleset: " + first_line(err))
        else:
            unknown.append("правила nftables без root не прочитать")
    ipt = ctx.run.which("iptables")
    if ipt:
        rc, ver, _ = ctx.run.run(["iptables", "-V"])
        legacy = "legacy" in ver
        if ctx.root and (legacy or not nft):
            names, _ = read_text(ctx.p("/proc/net/ip_tables_names"))
            if legacy and not (names and "filter" in names.split()):
                empty.append("iptables (legacy): таблица filter не загружена")
            else:
                cmd = ["iptables-legacy" if legacy and ctx.run.which("iptables-legacy") else "iptables", "-S", "INPUT"]
                rc, out, err = ctx.run.run(cmd)
                if rc == 0:
                    if parse_iptables_input(out):
                        found.append("iptables фильтрует INPUT")
                    else:
                        empty.append("iptables: в INPUT нет DROP или REJECT")
                else:
                    unknown.append("iptables -S: " + first_line(err))
        elif not ctx.root and not nft:
            unknown.append("правила iptables без root не прочитать")
    ctx.cache["firewall"] = True if found else (None if unknown else False)
    d = found + empty + unknown
    if found:
        return [res("firewall", "Сетевой экран на сервере", "OK", "\n".join(d),
                    "Проверьте, что порты базы, брокера и поиска открыты только серверам RX и администраторам.",
                    ["q19", "q4"])]
    if unknown:
        hint = NEED_ROOT if not ctx.root else ("Правила не прочитаны и под root: так бывает в контейнере без "
                                               "CAP_NET_ADMIN, проверяйте на хосте.")
        return [res("firewall", "Сетевой экран на сервере", "ERROR", "\n".join(d), hint, ["q19", "q4"])]
    return [res("firewall", "Сетевой экран на сервере", "WARN",
                "\n".join(d) if d else "nftables, iptables, firewalld и ufw не найдены или без правил",
                "Включите экран на сервере и разрешите только нужные порты с нужных адресов. Правила менять "
                "только с согласия и с открытой второй сессией, иначе можно потерять доступ.", ["q19", "q4"])]


# ---------------------------------------------------------------- ports

SENSITIVE = {
    5432: ("PostgreSQL", "q4"), 6432: ("pgbouncer", "q4"), 1433: ("Microsoft SQL Server", "q4"),
    5672: ("RabbitMQ AMQP", "q4"), 5671: ("RabbitMQ AMQP по TLS", "q4"),
    25672: ("RabbitMQ, связь узлов кластера", "q4"), 4369: ("epmd, имена узлов RabbitMQ", "q4"),
    15672: ("RabbitMQ, веб-интерфейс управления", "q21"), 15671: ("RabbitMQ, управление по TLS", "q21"),
    15692: ("RabbitMQ, метрики Prometheus", "q21"),
    9200: ("Elasticsearch", "q4"), 9300: ("Elasticsearch, связь узлов", "q4"), 5601: ("Kibana", "q21"),
    9000: ("S3-хранилище (MinIO) или другое", "q4"), 9001: ("консоль MinIO или другое", "q21"),
    2375: ("Docker API без TLS", "q21"), 2376: ("Docker API по TLS", "q21"),
    2379: ("etcd", "q4"), 2380: ("etcd, связь узлов", "q4"), 10250: ("kubelet API", "q21"),
    5000: ("Directum Launcher (порт по умолчанию) или другое", "q21"),
    6379: ("Redis", "q4"), 27017: ("MongoDB", "q4"),
}
# Порты сервисов RX по умолчанию (справка RX, «Используемые порты»); при установке занятый порт сдвигается.
RX_PORTS = {10056: "сервис хранилищ RX", 27002: "сервис интеграции RX", 36801: "сервис логирования RX",
            28013: "хранение файлов предпросмотра RX", 46130: "агент управления сервисами RX",
            48480: "Centrifugo", 20050: "сервис клиентских подключений RX"}


def split_hostport(s):
    if ":" not in s:
        return s, None
    host, port = s.rsplit(":", 1)
    host = host.strip("[]").split("%")[0]
    try:
        return host, int(port)
    except ValueError:
        return host, None


def parse_ss(text):
    out = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] not in ("tcp", "udp", "tcp6", "udp6"):
            continue
        proto, state, local = parts[0][:3], parts[1], parts[4]
        if proto == "tcp" and state != "LISTEN":
            continue
        if proto == "udp" and state not in ("UNCONN", "LISTEN"):
            continue
        host, port = split_hostport(local)
        if port is None:
            continue
        procs = sorted(set(re.findall(r'\("([^"]+)",pid=\d+', line)))
        out.append({"proto": proto, "addr": host, "port": port, "process": ",".join(procs)})
    return out


def hex_ip(h):
    if len(h) == 8:
        return socket.inet_ntop(socket.AF_INET, bytes.fromhex(h)[::-1])
    b = b"".join(bytes.fromhex(h[i:i + 8])[::-1] for i in range(0, 32, 8))
    return str(ipaddress.ip_address(b))


def parse_proc_net(text, proto):
    out = []
    for line in (text or "").splitlines()[1:]:
        f = line.split()
        if len(f) < 4 or ":" not in f[1]:
            continue
        st = f[3]
        if (proto == "tcp" and st != "0A") or (proto == "udp" and st != "07"):
            continue
        h, p = f[1].split(":")
        try:
            out.append({"proto": proto, "addr": hex_ip(h), "port": int(p, 16), "process": ""})
        except (ValueError, OSError):
            continue
    return out


def scope(addr):
    if addr in ("*", ""):
        return "all"
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return "addr"
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_unspecified:
        return "all"
    if ip.is_loopback:
        return "loopback"
    return "addr"


def listeners(ctx):
    rc, out, err = ctx.run.run(["ss", "-H", "-tulpn"])
    if rc == 0:
        return parse_ss(out), "ss"
    if rc is not None:  # старый ss без -H
        rc, out, err = ctx.run.run(["ss", "-tulpn"])
        if rc == 0:
            return parse_ss(out), "ss"
    items, any_file = [], False
    for name, proto in (("tcp", "tcp"), ("tcp6", "tcp"), ("udp", "udp"), ("udp6", "udp")):
        t, _ = read_text(ctx.p("/proc/net/" + name))
        if t is not None:
            any_file = True
            items += parse_proc_net(t, proto)
    return (items, "/proc/net") if any_file else (None, first_line(err) or "нет ss и /proc/net")


def classify_ports(items, firewall):
    """→ (статус, строки находок, есть ли docker-proxy среди открытых)."""
    lines, statuses, docker = [], [], False
    seen = set()
    for it in sorted(items, key=lambda x: (x["port"], x["proto"], x["addr"])):
        sc = scope(it["addr"])
        key = (it["proto"], "*" if sc == "all" else it["addr"], it["port"])
        if key in seen or it["proto"] != "tcp":
            continue
        seen.add(key)
        proc = " (%s)" % it["process"] if it["process"] else ""
        where = "%s:%d" % ("*" if sc == "all" else it["addr"], it["port"])
        if it["port"] in SENSITIVE and sc != "loopback":
            name, q = SENSITIVE[it["port"]]
            if it["port"] == 2375:
                st = "CRIT"
            elif sc == "all":
                st = "INFO" if firewall else "WARN"
            else:
                st = "INFO"
            statuses.append(st)
            lines.append("%s %s %s, %s%s" % (st, where, name, q, proc))
            if "docker-proxy" in it["process"]:
                docker = True
        elif it["port"] in RX_PORTS and sc != "loopback":
            lines.append("INFO %s %s%s" % (where, RX_PORTS[it["port"]], proc))
    return worst(statuses) if statuses else "OK", lines, docker


def check_ports(ctx):
    items, src = listeners(ctx)
    if items is None:
        return [res("ports", "Открытые порты", "ERROR", "не удалось получить список: %s" % src, "", ["q4", "q21"])]
    firewall = ctx.cache.get("firewall")
    st, lines, docker = classify_ports(items, bool(firewall))
    hint = []
    if st in ("WARN", "CRIT"):
        why = ("сетевой экран на сервере не найден" if firewall is False else
               "проверить сетевой экран не удалось")
        hint.append("Служебные порты слушают на всех адресах, а %s. Закройте их экраном или слушайте только "
                    "нужный адрес; к базе, брокеру и поиску ходят только серверы RX и администраторы." % why)
    if any(l.startswith("CRIT") for l in lines):
        hint.append("Docker API без TLS на сетевом адресе даёт root на хосте любому, кто достучится до порта.")
    if st == "INFO" and firewall:
        hint.append("Порты открыты на всех адресах, сетевой экран есть: проверьте, что они разрешены только "
                    "серверам RX и администраторам.")
    if docker:
        hint.append("Порты, опубликованные Docker, обходят правила ufw и firewalld: ограничивайте адрес при "
                    "публикации (127.0.0.1:порт) или правилами в цепочке DOCKER-USER.")
    d = lines or ["служебные порты не слушают на сетевых адресах"]
    out = [res("ports.exposed", "Служебные порты в сети", st, "\n".join(d), " ".join(hint), ["q4", "q21"])]
    allp = sorted(set((i["proto"], "*" if scope(i["addr"]) == "all" else i["addr"], i["port"], i["process"])
                      for i in items), key=lambda x: (x[2], x[0], x[1]))
    listing = ["%s %s:%d%s" % (p, a, port, " " + pr if pr else "") for p, a, port, pr in allp[:80]]
    if len(allp) > 80:
        listing.append("и ещё %d" % (len(allp) - 80))
    note = "" if ctx.root or src != "ss" else "Без root процессы чужих сокетов не показаны."
    out.append(res("ports.list", "Все слушающие сокеты (%s)" % src, "INFO", "\n".join(listing) or "слушающих сокетов нет",
                   note, ["q21"]))
    return out


# ---------------------------------------------------------------- secrets

KEY_EXT = (".key", ".pfx", ".p12", ".jks", ".keystore", ".p8")
KEY_STORE_EXT = (".pfx", ".p12", ".jks", ".keystore")  # контейнер ключа: секрет сам по себе
SKIP_DIRS = (".git", "node_modules", "__pycache__")


def mode_str(m):
    return "%04o" % stat.S_IMODE(m)


def owner(uid):
    try:
        return pwd.getpwuid(uid).pw_name if pwd else str(uid)
    except KeyError:
        return str(uid)


def others_traverse(path, cache):
    """Могут ли «все остальные» дойти до файла: o+x на всех каталогах выше."""
    d = os.path.dirname(os.path.abspath(path))
    while True:
        if d not in cache:
            try:
                cache[d] = bool(os.stat(d).st_mode & stat.S_IXOTH)
            except OSError:
                cache[d] = False
        if not cache[d]:
            return False
        nd = os.path.dirname(d)
        if nd == d:
            return True
        d = nd


def count_secrets(text):
    if mask_secrets is None:
        return 0
    return mask_secrets.mask(text)[1]


def scan_dir(top, limit_files=20000, limit_size=2 * 1024 * 1024):
    """Ищет файлы с секретами. Значения не возвращает, только счётчики."""
    r = {"files": 0, "secret_files": 0, "world": [], "unreadable": 0, "big": 0, "truncated": False, "notes": []}
    cache = {}
    try:
        top_dev = os.stat(top).st_dev
    except OSError:
        top_dev = None
    for root, dirs, files in os.walk(top, followlinks=False):
        keep = []
        for d in dirs:
            if d in SKIP_DIRS:
                continue
            try:
                if top_dev is not None and os.lstat(os.path.join(root, d)).st_dev != top_dev:
                    continue  # другая файловая система
            except OSError:
                continue
            keep.append(d)
        dirs[:] = keep
        for name in files:
            p = os.path.join(root, name)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            if r["files"] >= limit_files:
                r["truncated"] = True
                return r
            r["files"] += 1
            world = bool(st.st_mode & stat.S_IROTH) and others_traverse(p, cache)
            is_key = name.lower().endswith(KEY_EXT)
            if st.st_size > limit_size:
                r["big"] += 1
                continue
            try:
                with open(p, "rb") as f:
                    data = f.read()
            except (PermissionError, OSError):
                r["unreadable"] += 1
                continue
            if name.lower().endswith(KEY_STORE_EXT):
                n = 1
            elif b"\0" in data[:8192]:
                n = 1 if is_key else 0  # двоичный ключ не разобрать, но он секрет сам по себе
            else:
                text = data.decode("utf-8", "replace")
                n = count_secrets(text)
                if name == "config.yml" and "CONNECTION_STRING" in text and "config_password" not in text:
                    r["notes"].append("%s: нет config_password, шифрование параметров RX не настроено" % p)
            if n:
                r["secret_files"] += 1
                if world:
                    r["world"].append((p, mode_str(st.st_mode), owner(st.st_uid), n, is_key))
    return r


def check_secrets(ctx):
    dirs = ctx.opts.get("config_dir") or []
    if not dirs:
        return [res("secrets", "Файлы с паролями, доступные всем", "SKIP", "не задано --config-dir",
                    "Укажите каталоги с конфигурацией: папку etc в каталоге Directum Launcher (там config.yml), "
                    "/etc/rabbitmq, /etc/postgresql, /etc/elasticsearch, /etc/nginx, /etc/haproxy.", ["q9"])]
    if mask_secrets is None:
        return [res("secrets", "Файлы с паролями, доступные всем", "ERROR", "нет mask_secrets.py рядом со скриптом",
                    "", ["q9"])]
    out = []
    for top in dirs:
        cid = "secrets:%s" % top
        if os.path.abspath(top) == "/":
            out.append(res(cid, "Файлы с паролями, доступные всем", "ERROR", "корень / слишком широко",
                           "Укажите конкретные каталоги с конфигурацией.", ["q9"]))
            continue
        if not os.path.isdir(top):
            out.append(res(cid, "Файлы с паролями, доступные всем", "ERROR", "нет каталога %s" % top, "", ["q9"]))
            continue
        r = scan_dir(top)
        d = ["просмотрено файлов: %d, с секретами: %d, из них доступны всем на чтение: %d"
             % (r["files"], r["secret_files"], len(r["world"]))]
        for p, m, own, n, is_key in r["world"][:40]:
            d.append("%s права %s владелец %s, %s" % (p, m, own, "файл ключа" if is_key and n == 1
                                                     else "значений: %d" % n))
        if len(r["world"]) > 40:
            d.append("и ещё %d" % (len(r["world"]) - 40))
        if r["unreadable"]:
            d.append("не прочитано (нет прав): %d, такие файлы и так не доступны всем" % r["unreadable"])
        if r["big"]:
            d.append("пропущено файлов больше 2 МБ: %d" % r["big"])
        if r["truncated"]:
            d.append("остановлено на 20000 файлах: сузьте каталог")
        d += r["notes"]
        if r["world"]:
            st = "WARN"
            hint = ("Любая учётка на сервере читает эти пароли. Сузьте права (chmod 600 или 640 с группой сервиса) "
                    "после проверки, от какого пользователя сервис их читает, а пароли, которые давно лежали "
                    "открыто, смените. В RX строки подключения в config.yml можно зашифровать (справка "
                    "«Шифрование параметров»).")
        else:
            st, hint = "OK", ""
        out.append(res(cid, "Файлы с паролями, доступные всем", st, "\n".join(d), hint, ["q9"]))
    return out


# ---------------------------------------------------------------- history

HIST = (".bash_history", ".zsh_history", ".sh_history", ".ash_history", ".psql_history", ".mysql_history",
        ".rediscli_history")


def check_history(ctx):
    homes = [ctx.p("/root")] + sorted(glob.glob(ctx.p("/home/*")))
    found, denied, clean, pgpass = [], [], 0, []
    for h in homes:
        for name in HIST + (".pgpass",):
            p = os.path.join(h, name)
            try:
                st = os.lstat(p)
            except PermissionError:
                denied.append(ctx.unp(h))
                break
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            if name == ".pgpass":
                if st.st_mode & 0o077:
                    pgpass.append("%s права %s" % (ctx.unp(p), mode_str(st.st_mode)))
                continue
            t, e = read_text(p, 16 * 1024 * 1024)
            if t is None:
                if e == "denied":
                    denied.append(ctx.unp(p))
                continue
            n = count_secrets(t)
            if n:
                found.append("%s значений: %d" % (ctx.unp(p), n))
            else:
                clean += 1
    d = found + ["%s: libpq не читает .pgpass с правами шире 0600" % x for x in pgpass]
    if found or pgpass:
        hint = ("Пароль, попавший в историю команд, считается скомпрометированным: смените его. Историю потом "
                "можно очистить, но это изменение, только с согласия." if found else
                "Поставьте .pgpass права 0600.")
        return [res("history", "Пароли в истории команд", "WARN", "\n".join(d + (["нет прав на: " + ", ".join(denied)]
                                                                                if denied else [])), hint, ["q9"])]
    if denied:
        return [res("history", "Пароли в истории команд", "ERROR",
                    "проверено файлов без паролей: %d; нет прав на: %s" % (clean, ", ".join(sorted(set(denied))[:10])),
                    NEED_ROOT, ["q9"])]
    if not clean:
        return [res("history", "Пароли в истории команд", "SKIP", "файлов истории не найдено", "", ["q9"])]
    return [res("history", "Пароли в истории команд", "OK", "файлов истории без паролей: %d" % clean, "", ["q9"])]


# ---------------------------------------------------------------- tls

MONTHS = {m: i + 1 for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split())}
STARTTLS = {"pg": "postgres", "postgres": "postgres", "postgresql": "postgres", "ldap": "ldap", "smtp": "smtp",
            "https": None, "tls": None, "amqps": None, "ldaps": None}


def parse_target(s):
    """[схема://]хост:порт → (хост, порт, starttls)."""
    starttls = None
    m = re.match(r"^([A-Za-z]+)://(.*)$", s.strip())
    if m:
        scheme = m.group(1).lower()
        if scheme not in STARTTLS:
            raise ValueError("неизвестная схема %s:// (pg, ldap, smtp или без схемы)" % scheme)
        starttls, s = STARTTLS[scheme], m.group(2)
    s = s.strip().rstrip("/")
    m = re.match(r"^\[([^\]]+)\]:(\d+)$", s) or re.match(r"^([^:\[\]]+):(\d+)$", s)
    if not m:
        raise ValueError("ожидаю хост:порт, получено %r" % s)
    port = int(m.group(2))
    if not 0 < port < 65536:
        raise ValueError("порт вне диапазона: %d" % port)
    return m.group(1), port, starttls


def is_ip(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def parse_openssl_date(s):
    p = s.split()
    if len(p) < 4 or p[0] not in MONTHS:
        return None
    try:
        hh, mm, ss = (int(x) for x in p[2].split(":"))
        return datetime.datetime(int(p[3]), MONTHS[p[0]], int(p[1]), hh, mm, ss)
    except ValueError:
        return None


def parse_s_client(text):
    certs = re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", text or "", re.S)
    codes = re.findall(r"Verify return code:\s*(\d+)\s*\(([^)]*)\)", text or "")
    proto = re.search(r"^\s*Protocol\s*:\s*(\S+)", text or "", re.M)
    new = re.search(r"New, (\S+), Cipher is (\S+)", text or "")
    return {"certs": certs, "verify": (int(codes[-1][0]), codes[-1][1]) if codes else None,
            "protocol": proto.group(1) if proto else (new.group(1) if new else None),
            "cipher": new.group(2) if new else None}


def parse_x509_text(text):
    info = {"subject": "", "issuer": "", "not_after": None, "san": []}
    m = re.search(r"Not After\s*:\s*(.+)", text or "")
    if m:
        info["not_after"] = parse_openssl_date(m.group(1).strip())
    m = re.search(r"^\s*Subject:\s*(.*)$", text or "", re.M)
    info["subject"] = m.group(1).strip() if m else ""
    m = re.search(r"^\s*Issuer:\s*(.*)$", text or "", re.M)
    info["issuer"] = m.group(1).strip() if m else ""
    m = re.search(r"X509v3 Subject Alternative Name:[^\n]*\n\s*([^\n]+)", text or "")
    if m:
        info["san"] = [x.strip() for x in m.group(1).split(",") if x.strip()]
    return info


def host_in_san(host, sans):
    names = [s.split(":", 1)[1].strip().lower() for s in sans if s.upper().startswith("DNS:")]
    ips = [s.split(":", 1)[1].strip() for s in sans if s.upper().startswith(("IP ADDRESS:", "IP:"))]
    if is_ip(host):
        want = ipaddress.ip_address(host)
        for i in ips:
            try:
                if ipaddress.ip_address(i) == want:
                    return True
            except ValueError:
                continue
        return False
    h = host.lower().rstrip(".")
    for n in names:
        if n == h or (n.startswith("*.") and "." in h and h.split(".", 1)[1] == n[2:]):
            return True
    return False


def old_protocol(text):
    """accepted, refused или unsupported (местный openssl не умеет этот протокол).

    Клиенту разрешён только один старый протокол, поэтому согласованный шифр значит, что сервер его принял.
    Строку Protocol не используем: OpenSSL 3 и 4 печатают её и при отказе."""
    t = text or ""
    if re.search(r"no protocols available|unknown option|Unknown option|no cipher match", t):
        return "unsupported"
    m = re.search(r"Cipher is (\S+)", t)
    if m:
        return "accepted" if m.group(1) != "(NONE)" else "refused"
    m = re.search(r"^\s*Cipher\s*:\s*(\S+)", t, re.M)
    if m and m.group(1) not in ("0000", "(NONE)"):
        return "accepted"
    return "refused"


def s_client_cmd(host, port, starttls, extra=(), ca=None):
    connect = "[%s]:%d" % (host, port) if ":" in host else "%s:%d" % (host, port)
    cmd = ["openssl", "s_client", "-connect", connect]
    if not is_ip(host):
        cmd += ["-servername", host]
    if starttls:
        cmd += ["-starttls", starttls]
    if ca:
        cmd += ["-CAfile", ca]
    return cmd + list(extra)


def tls_verdict(host, x, sc, now, warn_days, crit_days):
    """По разобранному сертификату → (статус, строки)."""
    st, d = [], []
    if x["not_after"] is None:
        return "ERROR", ["срок действия не прочитан"]
    days = int((x["not_after"] - now).total_seconds() // 86400)
    if days < 0:
        st.append("CRIT")
        ago = int((now - x["not_after"]).total_seconds() // 86400)
        d.append("сертификат истёк %d дн. назад (%s UTC)" % (ago, x["not_after"].strftime("%Y-%m-%d")))
    elif days < crit_days:
        st.append("CRIT")
        d.append("до конца срока %d дн. (%s UTC)" % (days, x["not_after"].strftime("%Y-%m-%d")))
    elif days < warn_days:
        st.append("WARN")
        d.append("до конца срока %d дн. (%s UTC)" % (days, x["not_after"].strftime("%Y-%m-%d")))
    else:
        st.append("OK")
        d.append("до конца срока %d дн. (%s UTC)" % (days, x["not_after"].strftime("%Y-%m-%d")))
    d.append("кому выдан: %s; кем: %s" % (x["subject"] or "?", x["issuer"] or "?"))
    if x["san"]:
        if host_in_san(host, x["san"]):
            d.append("имя %s есть в SAN" % host)
        else:
            st.append("WARN")
            d.append("имени %s нет в SAN: %s" % (host, ", ".join(x["san"][:8])))
    else:
        st.append("WARN")
        d.append("в сертификате нет SAN: браузеры и клиенты .NET такое имя не примут")
    if sc["verify"] is not None:
        code, text = sc["verify"]
        if code == 0:
            d.append("цепочка проверена")
        else:
            st.append("WARN")
            d.append("цепочка не проверяется: код %d (%s)" % (code, text))
    if len(sc["certs"]) == 1 and x["subject"] and x["subject"] != x["issuer"]:
        st.append("WARN")
        d.append("сервер отдаёт только свой сертификат без промежуточных")
    if sc["protocol"]:
        d.append("протокол: %s%s" % (sc["protocol"], ", шифр %s" % sc["cipher"] if sc["cipher"] else ""))
    return worst(st), d


def check_tls(ctx):
    targets = ctx.opts.get("tls") or []
    if not targets:
        return [res("tls", "Сертификаты TLS", "SKIP", "не задано --tls",
                    "Пример: --tls rx.example.com:443 --tls pg://db.example.com:5432 --tls mq.example.com:5671",
                    ["q6"])]
    if not ctx.run.which("openssl"):
        return [res("tls", "Сертификаты TLS", "ERROR", "нет openssl", "", ["q6"])]
    out = []
    for t in targets:
        try:
            host, port, starttls = parse_target(t)
        except ValueError as e:
            out.append(res("tls:%s" % t, "Сертификат %s" % t, "ERROR", str(e), "", ["q6"]))
            continue
        cid, title = "tls:%s:%d" % (host, port), "Сертификат %s:%d%s" % (host, port, " (STARTTLS %s)" % starttls
                                                                          if starttls else "")
        q = ["q6"] if port in (443, 8443) else []
        rc, so, se = ctx.run.run(s_client_cmd(host, port, starttls, ["-showcerts"], ctx.opts.get("tls_ca")),
                                 stdin_data="")
        sc = parse_s_client(so)
        if not sc["certs"]:
            out.append(res(cid, title, "ERROR", "сертификат не получен: %s" % (first_line(se) or first_line(so) or
                                                                             "нет ответа"), "", q))
            continue
        rc2, xo, xe = ctx.run.run(["openssl", "x509", "-noout", "-text"], stdin_data=sc["certs"][0] + "\n")
        x = parse_x509_text(xo)
        st, d = tls_verdict(host, x, sc, ctx.now, ctx.opts.get("tls_warn_days", 30), ctx.opts.get("tls_crit_days", 7))
        old = []
        for flag, name in (("-tls1_1", "TLS 1.1"), ("-tls1", "TLS 1.0")):
            v = "unsupported"
            for extra in ([flag, "-cipher", "DEFAULT:@SECLEVEL=0"], [flag]):
                _, oo, oe = ctx.run.run(s_client_cmd(host, port, starttls, extra), stdin_data="")
                v = old_protocol(oo + "\n" + oe)
                if v != "unsupported":
                    break
            if v == "accepted":
                old.append(name)
            elif v == "unsupported":
                d.append("%s: проверить нельзя, местный openssl его не поддерживает" % name)
        if old:
            st = worst([st, "WARN"])
            d.append("сервер принимает устаревшие протоколы: " + ", ".join(old))
        hint = []
        if st in ("WARN", "CRIT"):
            hint.append("Продлевайте сертификат заранее и проверяйте теми же командами. Имя, по которому подключаются "
                        "клиенты, должно быть в SAN, в файле сертификата полная цепочка, на сервере только TLS 1.2 и 1.3.")
            if sc["verify"] and sc["verify"][0] != 0 and not ctx.opts.get("tls_ca"):
                hint.append("Для внутреннего центра сертификации передайте его корень через --tls-ca.")
        out.append(res(cid, title, st, "\n".join(d), " ".join(hint), q))
    return out


# ---------------------------------------------------------------- time

def kv_lines(text, sep="="):
    d = {}
    for line in (text or "").splitlines():
        if sep in line:
            k, v = line.split(sep, 1)
            d[k.strip()] = v.strip()
    return d


def parse_chrony(text):
    d = kv_lines(text, ":")
    off = None
    m = re.match(r"([0-9.]+) seconds (fast|slow)", d.get("System time", ""))
    if m:
        off = float(m.group(1)) * (1 if m.group(2) == "fast" else -1)
    return {"leap": d.get("Leap status", ""), "offset": off, "ref": d.get("Reference ID", "")}


def check_time(ctx):
    q = ["q22"]
    rc, out, _ = ctx.run.run(["timedatectl", "show"])
    if rc == 0 and "NTPSynchronized=" in out:
        d = kv_lines(out)
        if d.get("NTPSynchronized") == "yes":
            return [res("time", "Синхронизация времени", "OK", "timedatectl: NTPSynchronized=yes", "", q)]
        return [res("time", "Синхронизация времени", "WARN",
                    "timedatectl: NTPSynchronized=%s, NTP=%s" % (d.get("NTPSynchronized"), d.get("NTP", "?")),
                    "Включите синхронизацию с одним источником на всех серверах (chrony или systemd-timesyncd). "
                    "Kerberos ломается при расхождении больше 5 минут.", q)]
    rc, out, _ = ctx.run.run(["timedatectl", "status"])
    m = re.search(r"(System clock synchronized|NTP synchronized):\s*(\w+)", out or "")
    if rc == 0 and m:
        st = "OK" if m.group(2) == "yes" else "WARN"
        return [res("time", "Синхронизация времени", st, "timedatectl: %s: %s" % (m.group(1), m.group(2)),
                    "" if st == "OK" else "Включите синхронизацию времени с одним источником на всех серверах.", q)]
    rc, out, err = ctx.run.run(["chronyc", "-n", "tracking"])
    chrony_err = first_line(err or out) if rc is not None and rc != 0 else ""
    if rc == 0 and "Leap status" in out:
        c = parse_chrony(out)
        ok = c["leap"] == "Normal"
        d = "chronyc: Leap status %s%s" % (c["leap"], ", смещение %.3f с" % c["offset"] if c["offset"] is not None else "")
        if ok and c["offset"] is not None and abs(c["offset"]) > 60:
            ok = False
        return [res("time", "Синхронизация времени", "OK" if ok else "WARN", d,
                    "" if ok else "Проверьте источники chrony: chronyc sources.", q)]
    rc, out, _ = ctx.run.run(["ntpstat"])
    if rc in (0, 1):
        return [res("time", "Синхронизация времени", "OK" if rc == 0 else "WARN", "ntpstat: " + first_line(out),
                    "" if rc == 0 else "Включите синхронизацию времени.", q)]
    if chrony_err:
        return [res("time", "Синхронизация времени", "WARN", "chronyc не получил ответа от chronyd: %s" % chrony_err,
                    "Служба синхронизации не отвечает. Проверьте `systemctl status chronyd` (или chrony) и источники.",
                    q)]
    if ctx.cache.get("container"):
        return [res("time", "Синхронизация времени", "SKIP", "в контейнере время берётся с хоста", "Проверьте на хосте.", q)]
    return [res("time", "Синхронизация времени", "ERROR", "нет timedatectl, chronyc и ntpstat или они не ответили", "", q)]


# ---------------------------------------------------------------- mac

def astra_state(text):
    t = (text or "").strip().upper()
    if t in ("ACTIVE", "АКТИВНО"):
        return True
    if t in ("INACTIVE", "НЕАКТИВНО"):
        return False
    return None


def check_mac(ctx):
    out = []
    d, st = [], []
    lsm, _ = read_text(ctx.p("/sys/kernel/security/lsm"))
    if lsm:
        d.append("модули безопасности ядра: %s" % lsm.strip())
    se = None
    rc, o, _ = ctx.run.run(["getenforce"])
    if rc == 0 and o.strip():
        se = o.strip()
    else:
        t, _ = read_text(ctx.p("/sys/fs/selinux/enforce"))
        if t is not None:
            se = "Enforcing" if t.strip() == "1" else "Permissive"
    if se:
        d.append("SELinux: %s" % se)
    aa, _ = read_text(ctx.p("/sys/module/apparmor/parameters/enabled"))
    aa_on = bool(aa and aa.strip().upper().startswith("Y"))
    if aa is not None:
        d.append("AppArmor: %s" % ("включён" if aa_on else "выключен"))
        if aa_on:
            prof, err = read_text(ctx.p("/sys/kernel/security/apparmor/profiles"))
            if prof is not None:
                d.append("профилей AppArmor: enforce %d, complain %d" % (prof.count("(enforce)"), prof.count("(complain)")))
            elif err == "denied":
                d.append("число профилей AppArmor без root не видно")
    astra, _ = read_text(ctx.p("/etc/astra_version"))
    if se == "Enforcing" or aa_on:
        st.append("OK")
    elif se == "Permissive":
        st.append("WARN")
    elif not astra:
        st.append("WARN" if (lsm is not None or aa is not None or se) else "ERROR")
    hint = ""
    if "WARN" in st:
        hint = ("Модуль мандатного контроля не ограничивает процессы. На RHEL-совместимых и РЕД ОС ожидается SELinux "
                "Enforcing, на Debian и Ubuntu AppArmor. Включать только после прогона на тестовом стенде.")
    if "ERROR" in st:
        hint = "Состояние SELinux и AppArmor не определить: нет /sys/kernel/security и getenforce."
    if d or st:
        out.append(res("mac", "SELinux и AppArmor", worst(st) if st else "INFO", "\n".join(d) or "нет данных", hint, []))
    if astra:
        ad, ast = ["/etc/astra_version: %s" % first_line(astra)], "INFO"
        for cmd in (["astra-modeswitch", "get"], ["astra-mic-control", "status"], ["astra-mac-control", "status"]):
            if not ctx.run.which(cmd[0]):
                continue
            rc, o, e = ctx.run.run(cmd)
            ad.append("%s: %s" % (" ".join(cmd), first_line(o) or first_line(e) or "нет вывода"))
            if cmd[0] == "astra-mic-control":
                s = astra_state(first_line(o))
                if s is True:
                    ast = "OK"
                elif s is False:
                    ast = "WARN"
        cmdline, _ = read_text(ctx.p("/proc/cmdline"))
        m = re.search(r"parsec\.max_ilev=(\S+)", cmdline or "")
        if m:
            ad.append("параметр ядра parsec.max_ilev=%s" % m.group(1))
        hint = ("Значения сверяйте с документацией Astra Linux своей версии (wiki.astralinux.ru). "
                "Отключать МКЦ ради контейнеров нельзя: для сертифицированной ОС это нарушение условий применения.")
        out.append(res("mac.astra", "Режимы защиты Astra Linux", ast, "\n".join(ad), hint, []))
    return out


# ---------------------------------------------------------------- containers

PUBLIC_REGISTRIES = ("docker.io", "quay.io", "ghcr.io", "registry.k8s.io", "gcr.io", "mcr.microsoft.com",
                     "public.ecr.aws", "registry.gitlab.com", "docker.elastic.co")
DANGEROUS_CAPS = ("SYS_ADMIN", "NET_ADMIN", "SYS_PTRACE", "SYS_MODULE", "DAC_READ_SEARCH", "ALL")


def image_registry(image):
    first = image.split("/", 1)[0]
    if "/" not in image or not ("." in first or ":" in first or first == "localhost"):
        return "docker.io"
    return first


def analyze_containers(items):
    """Разбор docker inspect / podman inspect → (предупреждения, сведения)."""
    warn, info, root_users, regs, latest = [], [], 0, {}, []
    for c in items:
        name = (c.get("Name") or c.get("Id", "")[:12]).lstrip("/")
        hc = c.get("HostConfig") or {}
        cfg = c.get("Config") or {}
        if hc.get("Privileged"):
            warn.append("%s: запущен с --privileged" % name)
        for m in c.get("Mounts") or []:
            src = "%s %s" % (m.get("Source", ""), m.get("Destination", ""))
            if re.search(r"(docker|podman|containerd)\.sock\b", src):
                warn.append("%s: внутри сокет %s" % (name, m.get("Source") or m.get("Destination")))
                break
        if hc.get("PidMode") == "host":
            warn.append("%s: видит процессы хоста (pid host)" % name)
        if hc.get("NetworkMode") == "host":
            info.append("%s: сеть хоста (network host)" % name)
        caps = [x.upper().replace("CAP_", "") for x in (hc.get("CapAdd") or [])]
        bad = [x for x in caps if x in DANGEROUS_CAPS]
        if bad:
            warn.append("%s: добавлены привилегии %s" % (name, ", ".join(bad)))
        for so in hc.get("SecurityOpt") or []:
            if re.search(r"(seccomp|apparmor)[=:]unconfined|label[=:]disable", so):
                warn.append("%s: отключена изоляция %s" % (name, so))
        user = str(cfg.get("User") or "")
        if user in ("", "root", "0", "0:0"):
            root_users += 1
        image = str(cfg.get("Image") or c.get("ImageName") or "")
        if image and not image.startswith("sha256:"):
            r = image_registry(image)
            if r in PUBLIC_REGISTRIES:
                regs[r] = regs.get(r, 0) + 1
            tagpart = image.rsplit("/", 1)[-1]
            if "@" not in image and (":" not in tagpart or tagpart.endswith(":latest")):
                latest.append(name)
    if items:
        info.append("от root работают: %d из %d" % (root_users, len(items)))
    if regs:
        info.append("образы прямо с публичных реестров: " + ", ".join("%s %d" % kv for kv in sorted(regs.items())))
    if latest:
        info.append("образы без версии или с latest: " + ", ".join(latest[:10]))
    return warn, info


def check_containers(ctx):
    out = []
    for rt in ("docker", "podman"):
        if not ctx.run.which(rt):
            continue
        cid = "containers:%s" % rt
        rc, o, e = ctx.run.run([rt, "ps", "-q"])
        if rc != 0:
            msg = first_line(e) or first_line(o) or "нет ответа"
            if re.search(r"permission denied", msg, re.I):
                out.append(res(cid, "Контейнеры %s" % rt, "ERROR", msg,
                               "Нужен root или группа %s." % rt, ["q16"]))
            elif re.search(r"Cannot connect|Is the docker daemon running|no such file", msg, re.I):
                out.append(res(cid, "Контейнеры %s" % rt, "SKIP", "демон не запущен: %s" % msg, "", ["q16"]))
            else:
                out.append(res(cid, "Контейнеры %s" % rt, "ERROR", msg, "", ["q16"]))
            continue
        ids = o.split()
        if not ids:
            out.append(res(cid, "Контейнеры %s" % rt, "OK", "работающих контейнеров нет", "", ["q16"]))
            continue
        rc, o, e = ctx.run.run([rt, "inspect"] + ids[:200], timeout=ctx.run.timeout * 3)
        try:
            items = json.loads(o) if rc == 0 else None
        except ValueError:
            items = None
        if not isinstance(items, list):
            out.append(res(cid, "Контейнеры %s" % rt, "ERROR", "inspect не сработал: %s" % first_line(e), "", ["q16"]))
            continue
        warn, info = analyze_containers(items)
        d = ["контейнеров: %d" % len(items)] + ["WARN " + w for w in warn] + info
        hint = ("Уберите --privileged, сокет Docker и лишние привилегии из прикладных контейнеров: каждый такой "
                "контейнер равен root на хосте. Образы ставьте из своего реестра по дайджесту." if warn else
                ("Образы лучше ставить из своего реестра-зеркала и по дайджесту." if len(info) > 1 else ""))
        if rt == "podman" and not ctx.root:
            d.append("без root podman показывает только контейнеры текущего пользователя")
        out.append(res(cid, "Контейнеры %s" % rt, "WARN" if warn else "OK", "\n".join(d), hint, ["q16"]))
    return out or [res("containers", "Контейнеры", "SKIP", "docker и podman не найдены", "", ["q16"])]


# ---------------------------------------------------------------- repos

def parse_apt_list(text):
    """→ (строки с trusted=yes, хосты источников)."""
    trusted, hosts = [], set()
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith(("deb ", "deb-src ", "deb\t")):
            if re.search(r"\[[^\]]*trusted\s*=\s*yes", s):
                trusted.append(s[:120])
            m = re.search(r"\b[a-z+]+://([^/\s\]]+)", s)
            if m:
                hosts.add(m.group(1))
    return trusted, hosts


def parse_deb822(text):
    trusted, hosts = [], set()
    for stanza in re.split(r"\n\s*\n", text or ""):
        if re.search(r"^Enabled:\s*no\b", stanza, re.M | re.I):
            continue
        if re.search(r"^Trusted:\s*yes\b", stanza, re.M | re.I):
            m = re.search(r"^URIs:\s*(.+)$", stanza, re.M)
            trusted.append("Trusted: yes" + (" (%s)" % m.group(1).strip() if m else ""))
        for m in re.finditer(r"^URIs:\s*(.+)$", stanza, re.M):
            for u in m.group(1).split():
                h = re.match(r"[a-z+]+://([^/\s]+)", u)
                if h:
                    hosts.add(h.group(1))
    return trusted, hosts


def parse_yum_repo(text):
    """→ (секции с gpgcheck=0 при enabled, хосты)."""
    bad, hosts, sec, vals = [], set(), None, {}

    def flush():
        if sec is not None and vals.get("enabled", "1") not in ("0", "false", "no") and \
                vals.get("gpgcheck", "") in ("0", "false", "no"):
            bad.append(sec)
    for line in (text or "").splitlines():
        s = line.strip()
        m = re.match(r"^\[([^\]]+)\]", s)
        if m:
            flush()
            sec, vals = m.group(1), {}
            continue
        m = re.match(r"^(\w+)\s*=\s*(.*)$", s)
        if m and sec is not None:
            k, v = m.group(1).lower(), m.group(2).strip().lower()
            vals[k] = v
            if k in ("baseurl", "mirrorlist", "metalink"):
                for u in re.findall(r"[a-z+]+://([^/\s]+)", v):
                    hosts.add(u)
    flush()
    return bad, hosts


def check_repos(ctx):
    bad, hosts, seen = [], set(), False
    apt = [ctx.p("/etc/apt/sources.list")] + sorted(glob.glob(ctx.p("/etc/apt/sources.list.d/*.list")))
    for p in apt:
        t, _ = read_text(p)
        if t is not None:
            seen = True
            tr, h = parse_apt_list(t)
            bad += ["%s: %s" % (ctx.unp(p), x) for x in tr]
            hosts |= h
    for p in sorted(glob.glob(ctx.p("/etc/apt/sources.list.d/*.sources"))):
        t, _ = read_text(p)
        if t is not None:
            seen = True
            tr, h = parse_deb822(t)
            bad += ["%s: %s" % (ctx.unp(p), x) for x in tr]
            hosts |= h
    for p in sorted(glob.glob(ctx.p("/etc/apt/apt.conf.d/*"))):
        t, _ = read_text(p)
        for m in re.finditer(r"(AllowInsecureRepositories|AllowUnauthenticated)\s+\"?(true|1|yes)\"?", t or "", re.I):
            bad.append("%s: %s" % (ctx.unp(p), m.group(0)))
    for p in [ctx.p("/etc/yum.conf"), ctx.p("/etc/dnf/dnf.conf")] + sorted(glob.glob(ctx.p("/etc/yum.repos.d/*.repo"))):
        t, _ = read_text(p)
        if t is not None:
            seen = True
            b, h = parse_yum_repo(t)
            bad += ["%s: [%s] gpgcheck=0" % (ctx.unp(p), x) for x in b]
            hosts |= h
    if not seen:
        return [res("repos", "Источники пакетов", "SKIP", "нет настроек apt и yum/dnf", "", ["q16"])]
    d = bad + (["источники пакетов: " + ", ".join(sorted(hosts)[:20])] if hosts else [])
    if bad:
        return [res("repos", "Источники пакетов", "WARN", "\n".join(d),
                    "Пакеты из этих источников ставятся без проверки подписи. Подключите ключ репозитория "
                    "(signed-by или gpgkey) и уберите trusted=yes или gpgcheck=0.", ["q16"])]
    return [res("repos", "Источники пакетов", "OK", "\n".join(d) or "проверка подписи не отключена",
                "Своё зеркало вместо внешних источников делает установку повторяемой.", ["q16"])]


# ---------------------------------------------------------------- updates

def newest_mtime(paths):
    best = None
    for p in paths:
        try:
            m = os.stat(p).st_mtime
        except OSError:
            continue
        if best is None or m > best:
            best = m
    return best


def check_updates(ctx):
    q = ["q5"]
    last, src = None, ""
    info_dir = ctx.p("/var/lib/dpkg/info")
    if os.path.isdir(info_dir):
        last, src = newest_mtime(glob.glob(os.path.join(info_dir, "*.list"))), "dpkg"
    elif ctx.run.which("rpm"):
        rc, o, _ = ctx.run.run(["rpm", "-qa", "--qf", "%{INSTALLTIME}\\n"], timeout=max(30, ctx.run.timeout))
        times = [int(x) for x in (o or "").split() if x.isdigit()]
        if rc == 0 and times:
            last, src = max(times), "rpm"
    if last is None:
        return [res("updates", "Обновления пакетов", "SKIP", "dpkg и rpm не найдены", "", q)]
    when = datetime.datetime.fromtimestamp(last, datetime.timezone.utc).replace(tzinfo=None)
    days = int((ctx.now - when).total_seconds() // 86400)
    limit = ctx.opts.get("updates_days", 90)
    d = ["последняя установка или обновление пакета (%s): %s, %d дн. назад" % (src, when.strftime("%Y-%m-%d"), days)]
    if os.path.exists(ctx.p("/run/reboot-required")) or os.path.exists(ctx.p("/var/run/reboot-required")):
        d.append("есть /run/reboot-required: обновления ждут перезагрузки")
    if ctx.run.which("needs-restarting") and ctx.root:
        rc, o, _ = ctx.run.run(["needs-restarting", "-r"], timeout=max(15, ctx.run.timeout))
        if rc == 1:
            d.append("needs-restarting -r: нужна перезагрузка")
    st = "WARN" if days > limit else "OK"
    hint = ("Обновления безопасности ОС и СУБД ставьте хотя бы раз в квартал: сначала тестовый контур, потом прод."
            if st == "WARN" else "")
    return [res("updates", "Обновления пакетов", st, "\n".join(d), hint, q)]


# ---------------------------------------------------------------- main

CHECKS = [
    ("system", check_system, "ОС, ядро, root, контейнер"),
    ("sshd", check_sshd, "SSH: вход под root, по паролю, пустые пароли"),
    ("accounts", check_accounts, "UID 0 кроме root, учётки со входом и ключи, пустые пароли"),
    ("sudo", check_sudo, "NOPASSWD в sudoers, группы sudo, wheel, docker"),
    ("auditd", check_auditd, "служба аудита, число правил, потерянные события"),
    ("logship", check_logship, "пересылка журналов с сервера"),
    ("firewall", check_firewall, "nftables, iptables, firewalld, ufw"),
    ("ports", check_ports, "слушающие порты, служебные порты на всех адресах"),
    ("secrets", check_secrets, "файлы с паролями, доступные всем, в каталогах --config-dir"),
    ("history", check_history, "пароли в истории команд, права .pgpass"),
    ("tls", check_tls, "срок, SAN, цепочка и старые протоколы для --tls"),
    ("time", check_time, "синхронизация времени"),
    ("mac", check_mac, "SELinux, AppArmor, режимы Astra Linux"),
    ("containers", check_containers, "привилегии и образы контейнеров Docker и Podman"),
    ("repos", check_repos, "проверка подписи в источниках пакетов"),
    ("updates", check_updates, "давность обновления пакетов"),
]


def run_checks(ctx, only=None, skip=None):
    results = []
    for name, fn, _ in CHECKS:
        if (only and name not in only) or (skip and name in skip):
            continue
        try:
            results.extend(fn(ctx))
        except Exception as e:  # проверка не должна ронять остальные
            results.append(res(name, name, "ERROR", "сбой проверки: %s: %s" % (type(e).__name__, e)))
    return results


def exit_code(results):
    st = set(r["status"] for r in results)
    if st & {"WARN", "CRIT"}:
        return 1
    if "ERROR" in st:
        return 2
    return 0


def summary(results):
    s = {}
    for r in results:
        s[r["status"]] = s.get(r["status"], 0) + 1
    return s


def render_text(meta, results):
    L = ["host_audit %s · %s · %s · root: %s" % (VERSION, meta["host"], meta["time"], "да" if meta["root"] else "нет"), ""]
    for r in results:
        L.append("%-5s %-14s %s" % (r["status"], r["id"], r["title"]))
        for line in (r["detail"] or "").splitlines():
            L.append("      " + line)
        if r["hint"] and r["status"] not in ("OK", "SKIP"):
            L.append("      Что сделать: " + r["hint"])
        if r["questions"] and r["status"] not in ("OK", "SKIP", "INFO"):
            L.append("      Вопросы опросника: " + ", ".join(r["questions"]))
    s = summary(results)
    L.append("")
    L.append("Итог: " + ", ".join("%s %d" % (k, s[k]) for k in ("CRIT", "WARN", "ERROR", "OK", "INFO", "SKIP") if k in s)
             + ". Код возврата %d." % exit_code(results))
    return "\n".join(L)


def safe(text):
    """Страховка: даже если в выводе оказался секрет, он будет скрыт."""
    return mask_secrets.mask(text)[0] if mask_secrets else text


def parse_args(argv):
    o = {"config_dir": [], "tls": [], "tls_ca": None, "tls_warn_days": 30, "tls_crit_days": 7, "updates_days": 90,
         "timeout": 5, "only": None, "skip": None, "json": False, "list": False, "help": False}
    ints = {"--tls-warn-days": "tls_warn_days", "--tls-crit-days": "tls_crit_days", "--updates-days": "updates_days",
            "--timeout": "timeout"}
    i = 0
    while i < len(argv):
        a = argv[i]
        key, val = (a.split("=", 1) + [None])[:2] if a.startswith("--") and "=" in a else (a, None)

        def need():
            nonlocal i
            if val is not None:
                return val
            i += 1
            if i >= len(argv):
                raise ValueError("после %s нужно значение" % key)
            return argv[i]
        if key in ("-h", "--help"):
            o["help"] = True
        elif key == "--json":
            o["json"] = True
        elif key == "--list":
            o["list"] = True
        elif key == "--config-dir":
            o["config_dir"].append(need())
        elif key == "--tls":
            o["tls"].append(need())
        elif key == "--tls-ca":
            o["tls_ca"] = need()
        elif key in ints:
            v = need()
            try:
                o[ints[key]] = int(v)
            except ValueError:
                raise ValueError("%s ждёт целое число, получено %r" % (key, v))
        elif key in ("--only", "--skip"):
            names = [x.strip() for x in need().split(",") if x.strip()]
            unknown = [x for x in names if x not in [c[0] for c in CHECKS]]
            if unknown:
                raise ValueError("нет проверок: %s (см. --list)" % ", ".join(unknown))
            o[key[2:]] = names
        else:
            raise ValueError("неизвестный аргумент %s, см. --help" % a)
        i += 1
    return o


def main(argv):
    argv = [fix_arg(a) for a in argv]
    try:
        opts = parse_args(argv)
    except ValueError as e:
        write(sys.stderr, "host_audit.py: %s\n" % e)
        return 2
    if opts["help"]:
        write(sys.stdout, __doc__.strip() + "\n")
        return 0
    if opts["list"]:
        write(sys.stdout, "".join("%-10s %s\n" % (name, desc) for name, _, desc in CHECKS))
        return 0
    ctx = Ctx(opts)
    results = run_checks(ctx, opts["only"], opts["skip"])
    meta = {"host": socket.gethostname(), "time": datetime.datetime.now().replace(microsecond=0).isoformat(),
            "root": ctx.root}
    if opts["json"]:
        doc = {"tool": "host_audit", "version": VERSION, "host": meta["host"], "time": meta["time"],
               "root": meta["root"], "checks": results, "summary": summary(results)}
        write(sys.stdout, safe(json.dumps(doc, ensure_ascii=False, indent=1)) + "\n")
    else:
        write(sys.stdout, safe(render_text(meta, results)) + "\n")
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
