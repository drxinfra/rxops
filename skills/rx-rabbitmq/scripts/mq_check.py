#!/usr/bin/env python3
"""Проверка RabbitMQ под Directum RX. Только читает, ничего не меняет.

Что смотрит: тревоги по памяти и диску, память и диск к порогам, дескрипторы,
узлы кластера, очереди (сообщения, готовые, неподтверждённые, потребители),
очереди без потребителей, рост между двумя замерами, временные очереди
(GUID или хеш в имени), кворумные очереди, политики, соединения и TLS.

Откуда берёт данные:
  по умолчанию  API управления, только GET: адрес, имя и пароль из переменных
                RABBITMQ_URL (по умолчанию http://localhost:15672),
                RABBITMQ_USER, RABBITMQ_PASSWORD или из файла netrc (--netrc).
                Пароль в вывод не попадает.
  --ctl         rabbitmqctl и rabbitmq-diagnostics на этом сервере, только
                list_*, status, cluster_status; для контейнера
                --ctl-prefix "docker exec rabbitmq"
  --stdin       разбор вывода «rabbitmqctl list_queues ...» (таблица или
                --formatter json) или сохранённого ответа /api/queues

Примеры:
  RABBITMQ_URL=http://mq01:15672 RABBITMQ_USER=monitor RABBITMQ_PASSWORD=... \\
      python3 mq_check.py
  python3 mq_check.py --interval 60             два замера: что растёт
  python3 mq_check.py --json > before.json      сохранить замер
  python3 mq_check.py --compare before.json     сравнить с сохранённым
  python3 mq_check.py --match '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                                                какие очереди попадут под шаблон политики
  python3 mq_check.py --ctl --ctl-prefix "docker exec rabbitmq"
  rabbitmqctl list_queues -p / name messages messages_ready messages_unacknowledged consumers \\
      | python3 mq_check.py --stdin

Код возврата: 0 норма, 1 найдено отклонение, 2 не удалось проверить.
Python 3.6+, только стандартная библиотека.
"""
import argparse
import base64
import io
import json
import netrc
import os
import re
import shlex
import ssl
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

TOOL_VERSION = "0.1.0"

# Пороги по умолчанию. Абсолютные числа подбираются под инсталляцию по фону.
DEFAULTS = {
    "warn_messages": 5000,     # сообщений в одной очереди: стартовое значение
    "crit_messages": 50000,    # чек-лист вендора: в каждой очереди меньше 50 тысяч
    "warn_temp": 1000,         # временных очередей без потребителей: «тысячи» = утечка
    "growth_min": 100,         # прирост сообщений за интервал, который считаем ростом
}
MEM_WARN = 0.80                # память от порога vm_memory_high_watermark
DISK_FACTOR = 2.0              # свободного места меньше двух порогов disk_free_limit
DISK_LIMIT_MIN = 2 * 1024 ** 3 # порог диска меньше 2 ГБ: калькулятор drxinfra советует не меньше
FD_MIN = 65536                 # рекомендация документации RabbitMQ по лимиту файлов
USE_WARN, USE_CRIT = 0.80, 0.95
CHANNELS_WARN = 1000           # каналов на одно соединение (channel_max по умолчанию 2047)
CHURN_WARN = 30                # новых соединений в минуту при двух замерах

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
HEX_TAIL_RE = re.compile(r"([._\-])([0-9a-fA-F]{8,})$")
BAD_STATES = {"down", "crashed", "stopped", "terminated", "minority"}

Q_COLS = ("name,vhost,type,state,messages,messages_ready,messages_unacknowledged,consumers,"
          "policy,exclusive,auto_delete,durable,idle_since,members,online,leader,"
          "consumer_capacity,consumer_utilisation,message_stats.ack,message_stats.deliver_get,"
          "message_stats.publish")
N_COLS = ("name,running,type,mem_used,mem_limit,mem_alarm,disk_free,disk_free_limit,"
          "disk_free_alarm,fd_used,fd_total,sockets_used,sockets_total,proc_used,proc_total,"
          "partitions,uptime")
C_COLS = ("name,user,vhost,peer_host,ssl,ssl_protocol,channels,state,node,"
          "client_properties.connection_name,client_properties.product,user_provided_name")


class CheckError(Exception):
    """Проверку выполнить не удалось: недоступен источник, нет прав, не разобран вывод."""


# ---------------------------------------------------------------- форматирование

def hb(n):
    """Байты по-человечески: 1,5 ГБ."""
    if n is None:
        return "?"
    n = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024 or unit == "ТБ":
            s = ("%.0f" % n) if unit in ("Б", "КБ", "МБ") else ("%.1f" % n)
            return s.replace(".", ",") + " " + unit
        n /= 1024.0
    return str(n)


def pct(x):
    return ("%.0f %%" % (x * 100)).replace(".", ",")


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def qid(q):
    return "%s %s" % (q["vhost"], q["name"])


def nv(v):
    """Число или «?», если источник его не дал."""
    return "?" if v is None else str(v)


def shape(name):
    """Форма имени: GUID и хеш-хвост заменены, чтобы группировать однотипные очереди."""
    s = UUID_RE.sub("<guid>", name)
    m = HEX_TAIL_RE.search(s)
    if m and re.search(r"[a-fA-F]", m.group(2)) and re.search(r"[0-9]", m.group(2)):
        s = s[:m.start(2)] + "<hex>"
    return s


def temp_kind(name):
    """'guid', 'hex', 'server' (amq.gen-) или '' для обычной очереди."""
    if name.startswith("amq.gen-"):
        return "server"
    if UUID_RE.search(name):
        return "guid"
    m = HEX_TAIL_RE.search(name)
    if m and re.search(r"[a-fA-F]", m.group(2)) and re.search(r"[0-9]", m.group(2)):
        return "hex"
    return ""


def safe_url(url):
    """Адрес без имени и пароля."""
    p = urlsplit(url)
    netloc = p.hostname or ""
    if p.port:
        netloc += ":%d" % p.port
    return urlunsplit((p.scheme, netloc, p.path, "", ""))


# ---------------------------------------------------------------- нормализация

def norm_queue(q):
    ms = q.get("message_stats") or {}
    cap = q.get("consumer_capacity")
    if cap is None:
        cap = q.get("consumer_utilisation")
    try:
        cap = float(cap) if cap not in (None, "") else None
    except (TypeError, ValueError):
        cap = None
    members = q.get("members") or []
    online = q.get("online") or []
    if isinstance(members, str):
        members = [m for m in re.split(r"[\s,\[\]']+", members) if m]
    if isinstance(online, str):
        online = [m for m in re.split(r"[\s,\[\]']+", online) if m]
    return {
        "vhost": q.get("vhost") or "/",
        "name": q.get("name") or "",
        "type": (q.get("type") or "classic").lower(),
        "state": str(q.get("state") or ""),
        "messages": to_int(q.get("messages")),
        "ready": to_int(q.get("messages_ready")),
        "unacked": to_int(q.get("messages_unacknowledged")),
        "consumers": to_int(q.get("consumers")) or 0,
        "policy": q.get("policy") or "",
        "exclusive": bool(q.get("exclusive")) if q.get("exclusive") not in ("false", "") else False,
        "auto_delete": bool(q.get("auto_delete")) if q.get("auto_delete") not in ("false", "") else False,
        "idle_since": q.get("idle_since") or "",
        "members": list(members),
        "online": list(online),
        "leader": q.get("leader") or "",
        "capacity": cap,
        "ack": to_int(ms.get("ack")),
        "deliver": to_int(ms.get("deliver_get")),
        "publish": to_int(ms.get("publish")),
    }


def norm_node(n):
    return {
        "name": n.get("name", "?"),
        "running": n.get("running", True),
        "mem_used": to_int(n.get("mem_used")),
        "mem_limit": to_int(n.get("mem_limit")),
        "mem_alarm": bool(n.get("mem_alarm")),
        "disk_free": to_int(n.get("disk_free")),
        "disk_free_limit": to_int(n.get("disk_free_limit")),
        "disk_free_alarm": bool(n.get("disk_free_alarm")),
        "fd_used": to_int(n.get("fd_used")),
        "fd_total": to_int(n.get("fd_total")),
        "sockets_used": to_int(n.get("sockets_used")),
        "sockets_total": to_int(n.get("sockets_total")),
        "proc_used": to_int(n.get("proc_used")),
        "proc_total": to_int(n.get("proc_total")),
        "partitions": list(n.get("partitions") or []),
        "uptime": to_int(n.get("uptime")),
    }


def peer(h):
    """Адрес клиента: строка из API или кортеж чисел из rabbitmqctl --formatter json."""
    if isinstance(h, list):
        if len(h) == 4:
            return ".".join(str(x) for x in h)
        if len(h) == 8:
            if h[:6] == [0, 0, 0, 0, 0, 65535]:
                return "%d.%d.%d.%d" % (h[6] >> 8, h[6] & 255, h[7] >> 8, h[7] & 255)
            return ":".join("%x" % x for x in h)
    return str(h or "")


def norm_conn(c):
    cp = c.get("client_properties") or {}
    name = c.get("user_provided_name") or cp.get("connection_name") or ""
    return {
        "name": c.get("name") or "",
        "user": c.get("user") or "",
        "vhost": c.get("vhost") or "",
        "peer_host": peer(c.get("peer_host")),
        "ssl": c.get("ssl") in (True, "true"),
        "ssl_protocol": c.get("ssl_protocol") or "",
        "channels": to_int(c.get("channels")) or 0,
        "state": str(c.get("state") or ""),
        "client": name,
        "product": cp.get("product") or "",
    }


def norm_policy(p):
    d = p.get("definition") or {}
    if isinstance(d, str):
        try:
            d = json.loads(d)
        except ValueError:
            d = {"?": d}
    return {
        "vhost": p.get("vhost") or "/",
        "name": p.get("name") or "",
        "pattern": p.get("pattern") or "",
        "apply_to": p.get("apply-to") or p.get("apply_to") or "all",
        "priority": to_int(p.get("priority")) or 0,
        "definition": d,
    }


# ---------------------------------------------------------------- источник: API

class Api(object):
    def __init__(self, url, user, password, cacert=None, insecure=False, timeout=15.0):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.headers = {"Accept": "application/json", "User-Agent": "rxops-mq_check/" + TOOL_VERSION}
        if user:
            token = base64.b64encode(("%s:%s" % (user, password or "")).encode("utf-8")).decode("ascii")
            self.headers["Authorization"] = "Basic " + token
        self.ctx = None
        if self.url.startswith("https://"):
            self.ctx = ssl.create_default_context(cafile=cacert) if cacert else ssl.create_default_context()
            if insecure:
                self.ctx.check_hostname = False
                self.ctx.verify_mode = ssl.CERT_NONE

    def get(self, path, allow=()):
        """Единственный метод запроса: GET. Коды из allow возвращают тело ответа, а не ошибку."""
        req = Request(self.url + path, headers=self.headers, method="GET")
        try:
            resp = urlopen(req, timeout=self.timeout, context=self.ctx) if self.ctx else \
                urlopen(req, timeout=self.timeout)
            body = resp.read()
        except HTTPError as e:
            if e.code not in allow:
                raise HttpFail(e.code, path)
            body = e.read()
        except URLError as e:
            raise CheckError(explain_net(self.url, getattr(e, "reason", e)))
        except (OSError, ValueError) as e:
            raise CheckError(explain_net(self.url, e))
        try:
            return json.loads(body.decode("utf-8"))
        except ValueError:
            raise CheckError("ответ %s не JSON: это точно API управления RabbitMQ (порт 15672)?" % path)


def explain_net(url, err):
    """Сетевая ошибка с подсказкой, куда смотреть."""
    s, low = str(err), str(err).lower()
    if "certificate_verify_failed" in low or "certificate verify failed" in low:
        return "сертификат %s не прошёл проверку: корень через --cacert или RABBITMQ_CA; %s" % (safe_url(url), s)
    if "refused" in low:
        return ("%s: соединение отклонено: API управления не запущено (плагин rabbitmq_management), другой порт "
                "(по умолчанию 15672) или панель отключена параметром management_panel_disabled; можно проверить "
                "через --ctl на сервере брокера" % safe_url(url))
    if "timed out" in low:
        return "%s: нет ответа: сеть, сетевой экран или брокер перегружен" % safe_url(url)
    return "нет связи с %s: %s" % (safe_url(url), s)


class HttpFail(CheckError):
    def __init__(self, code, path):
        self.code = code
        self.path = path
        hint = {
            401: "неверные имя или пароль, или у пользователя нет тега monitoring/management",
            403: "у пользователя нет прав на этот раздел",
            404: "раздела нет в этой версии или включён не тот плагин",
        }.get(code, "")
        CheckError.__init__(self, "HTTP %d на %s%s" % (code, path, (": " + hint) if hint else ""))


def collect_api(get, vhost=None):
    """Снимок через API. get(path, allow=()) возвращает разобранный JSON."""
    snap = {"source": "api", "notes": [], "taken_at": time.time()}
    ov = get("/api/overview")
    snap["broker"] = {
        "version": ov.get("rabbitmq_version", "?"),
        "erlang": ov.get("erlang_version", "?"),
        "cluster": ov.get("cluster_name", ""),
    }
    snap["listeners"] = [{"protocol": l.get("protocol"), "port": l.get("port"), "node": l.get("node")}
                         for l in ov.get("listeners") or []]
    churn = ov.get("churn_rates") or {}
    snap["churn"] = {k: to_int(churn.get(k)) for k in
                     ("connection_created", "connection_closed", "channel_created", "channel_closed",
                      "queue_created", "queue_deleted", "queue_declared")}
    snap["nodes"] = [norm_node(n) for n in get("/api/nodes?columns=" + N_COLS)]

    def optional(p, what):
        try:
            return get(p)
        except HttpFail as e:
            snap["notes"].append("%s не прочитаны (HTTP %d)" % (what, e.code))
            return None

    # Флаги тревог в /api/nodes обновляются раз в несколько секунд, проверка здоровья отвечает сразу.
    try:
        h = get("/api/health/checks/alarms", allow=(503,))
        for a in (h or {}).get("alarms") or []:
            for n in snap["nodes"]:
                if n["name"] == a.get("node"):
                    if "disk" in str(a.get("resource")):
                        n["disk_free_alarm"] = True
                    if "mem" in str(a.get("resource")):
                        n["mem_alarm"] = True
    except HttpFail:
        pass
    if vhost:
        vhosts = [{"name": vhost}]
    else:
        vhosts = get("/api/vhosts?columns=name,messages")
    snap["queues"] = []
    for v in vhosts:
        try:
            rows = get("/api/queues/%s?columns=%s" % (quote(v["name"], safe=""), Q_COLS))
        except HttpFail as e:
            if e.code not in (401, 403):
                raise
            snap.setdefault("hidden_vhosts", []).append({"name": v["name"], "messages": to_int(v.get("messages"))})
            continue
        snap["queues"] += [norm_queue(q) for q in rows]

    conns = optional("/api/connections?columns=" + C_COLS, "соединения")
    snap["connections"] = [norm_conn(c) for c in conns] if conns is not None else None
    cons = optional("/api/consumers", "потребители")
    snap["consumers"] = []
    for c in cons or []:
        q = c.get("queue") or {}
        ch = c.get("channel_details") or {}
        snap["consumers"].append({"vhost": q.get("vhost") or "/", "queue": q.get("name") or "",
                                  "connection": ch.get("connection_name") or "",
                                  "prefetch": to_int(c.get("prefetch_count")),
                                  "timeout": to_int(c.get("consumer_timeout"))})
    pol = optional("/api/policies", "политики")
    snap["policies"] = [norm_policy(p) for p in pol] if pol is not None else None
    users = optional("/api/users", "пользователи (нужен тег administrator)")
    snap["users"] = [u.get("name") for u in users] if users is not None else None
    return snap


# ---------------------------------------------------------------- источник: rabbitmqctl

def run_ctl(prefix, tool, args, timeout=60):
    cmd = shlex.split(prefix) + [tool] + args if prefix else [tool] + args
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.communicate(timeout=timeout)
    except OSError as e:
        raise CheckError("не запускается %s: %s" % (" ".join(cmd[:3]), e))
    except subprocess.TimeoutExpired:
        p.kill()
        raise CheckError("%s не ответил за %d с" % (tool, timeout))
    if p.returncode != 0:
        msg = (err or out).decode("utf-8", "replace").strip().splitlines()
        raise CheckError("%s %s: код %d: %s" % (tool, args[0], p.returncode, msg[-1] if msg else ""))
    return out.decode("utf-8", "replace")


def parse_json_output(text):
    """Вывод --formatter json: массив или объект; иногда несколько строк JSON."""
    text = text.strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except ValueError:
        rows = []
        for line in text.splitlines():
            line = line.strip().rstrip(",")
            if line.startswith("{"):
                rows.append(json.loads(line))
        return rows


def collect_ctl(prefix, vhost=None):
    ctl = lambda *a: parse_json_output(run_ctl(prefix, "rabbitmqctl", list(a) + ["--formatter", "json", "-q"]))
    snap = {"source": "ctl", "notes": [], "taken_at": time.time()}
    st = parse_json_output(run_ctl(prefix, "rabbitmq-diagnostics", ["status", "--formatter", "json", "-q"]))
    fdi = st.get("file_descriptors") or {}
    procs = st.get("processes") or {}
    mem = st.get("memory") or {}
    total = mem.get("total") if isinstance(mem.get("total"), (int, float)) else None
    if total is None and isinstance(mem.get("total"), dict):
        t = mem["total"]
        total = t.get(st.get("vm_memory_calculation_strategy") or "rss") or t.get("rss")
    alarms = st.get("alarms") or []
    alarm_text = json.dumps(alarms)
    node = {
        "name": st.get("node") or st.get("node_name") or "локальный узел",
        "running": True,
        "mem_used": to_int(total),
        "mem_limit": to_int(st.get("vm_memory_high_watermark_limit")),
        "mem_alarm": "memory" in alarm_text,
        "disk_free": to_int(st.get("disk_free")),
        "disk_free_limit": to_int(st.get("disk_free_limit")),
        "disk_free_alarm": "disk" in alarm_text,
        "fd_used": to_int(fdi.get("total_used")),
        "fd_total": to_int(fdi.get("total_limit")),
        "sockets_used": to_int(fdi.get("sockets_used")),
        "sockets_total": to_int(fdi.get("sockets_limit")),
        "proc_used": to_int(procs.get("used")),
        "proc_total": to_int(procs.get("limit")),
        "partitions": [],
        "uptime": to_int(st.get("uptime")),
    }
    snap["broker"] = {"version": st.get("rabbitmq_version", "?"), "erlang": st.get("erlang_version", "?"),
                      "cluster": ""}
    snap["nodes"] = [node]
    try:
        cs = parse_json_output(run_ctl(prefix, "rabbitmqctl", ["cluster_status", "--formatter", "json", "-q"]))
        snap["broker"]["cluster"] = cs.get("cluster_name", "")
        running = list(cs.get("running_nodes") or [])
        versions = cs.get("versions") or {}
        if len(running) == 1:
            node["name"] = running[0]
        local = versions.get(node["name"]) or (list(versions.values())[0] if versions else {})
        if local.get("erlang_version"):
            snap["broker"]["erlang"] = local["erlang_version"]
        for n in cs.get("disk_nodes", []) + cs.get("ram_nodes", []):
            if n not in running:
                snap["nodes"].append(dict(norm_node({"name": n}), running=False))
            elif n != node["name"]:
                snap["nodes"].append(dict(norm_node({"name": n}), running=True))
        for a in cs.get("alarms") or []:
            for n in snap["nodes"]:
                if n["name"] == a.get("node"):
                    n["mem_alarm"] = n["mem_alarm"] or "mem" in str(a.get("resource"))
                    n["disk_free_alarm"] = n["disk_free_alarm"] or "disk" in str(a.get("resource"))
        parts = cs.get("partitions") or {}
        if parts:
            if isinstance(parts, dict):
                for n in snap["nodes"]:
                    if parts.get(n["name"]):
                        n["partitions"] = list(parts[n["name"]])
            else:
                node["partitions"] = list(parts)
        if len(snap["nodes"]) > 1:
            snap["notes"].append("память, диск и дескрипторы показаны только для узла, где запущен rabbitmqctl")
    except CheckError as e:
        snap["notes"].append("cluster_status: %s" % e)
    vhosts = [vhost] if vhost else [v.get("name") for v in ctl("list_vhosts", "name")]
    snap["queues"] = []
    for vh in vhosts:
        try:
            rows = ctl("list_queues", "-p", vh, "name", "type", "state", "messages", "messages_ready",
                       "messages_unacknowledged", "consumers", "policy", "exclusive", "auto_delete")
        except CheckError:
            rows = ctl("list_queues", "-p", vh, "name", "state", "messages", "messages_ready",
                       "messages_unacknowledged", "consumers", "policy")
        for r in rows:
            r["vhost"] = vh
            snap["queues"].append(norm_queue(r))
        try:
            for r in ctl("list_queues", "-p", vh, "name", "type", "members", "online", "leader"):
                if (r.get("type") or "") == "quorum":
                    for q in snap["queues"]:
                        if q["vhost"] == vh and q["name"] == r.get("name"):
                            nq = norm_queue(dict(r, vhost=vh))
                            q.update(members=nq["members"], online=nq["online"], leader=nq["leader"])
        except CheckError:
            pass
    try:
        rows = ctl("list_connections", "name", "user", "vhost", "peer_host", "ssl", "ssl_protocol",
                   "channels", "state", "client_properties")
        snap["connections"] = [norm_conn(dict(r, client_properties=client_props(r.get("client_properties"))))
                               for r in rows]
    except CheckError as e:
        snap["connections"] = None
        snap["notes"].append("list_connections: %s" % e)
    snap["policies"] = []
    for vh in vhosts:
        try:
            snap["policies"] += [norm_policy(dict(p, vhost=p.get("vhost") or vh))
                                 for p in ctl("list_policies", "-p", vh)]
        except CheckError as e:
            snap["notes"].append("list_policies: %s" % e)
    try:
        snap["users"] = [u.get("user") or u.get("name") for u in ctl("list_users")]
    except CheckError:
        snap["users"] = None
    snap["consumers"] = []
    snap["churn"] = {}
    return snap


def client_props(cp):
    """client_properties из rabbitmqctl json: словарь или список пар."""
    if isinstance(cp, dict):
        return cp
    out = {}
    if isinstance(cp, list):
        for item in cp:
            if isinstance(item, (list, tuple)) and len(item) >= 3:
                out[str(item[0])] = item[2]
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                out[str(item[0])] = item[1]
    return out


# ---------------------------------------------------------------- источник: stdin

def parse_queue_text(text, vhost="/"):
    """Вывод rabbitmqctl list_queues: JSON, ответ /api/queues или таблица с заголовком."""
    s = text.strip()
    if not s:
        raise CheckError("на входе пусто")
    if s[0] in "[{":
        data = parse_json_output(s)
        if isinstance(data, dict):
            data = data.get("items") or [data]
        return [norm_queue(dict({"vhost": vhost}, **r)) for r in data if isinstance(r, dict)]
    cols = None
    out = []
    default = ["name", "messages", "messages_ready", "messages_unacknowledged", "consumers"]
    for line in s.splitlines():
        m = re.match(r"^Listing queues for vhost (.+?) \.\.\.", line)
        if m:
            vhost = m.group(1)
            continue
        if not line.strip() or line.startswith(("Timeout:", "Listing ")):
            continue
        parts = line.split("\t") if "\t" in line else line.split()
        low = [p.strip().lower() for p in parts]
        if "name" in low and ("messages" in low or "consumers" in low):
            cols = low
            continue
        use = cols or default[:len(parts)]
        if len(parts) < len(use):
            continue
        row = {"vhost": vhost}
        for k, v in zip(use, parts):
            row[k] = v.strip()
        if to_int(row.get("messages")) is None and to_int(row.get("consumers")) is None:
            continue
        out.append(norm_queue(row))
    if not out:
        raise CheckError("не нашёл строк очередей: нужен вывод rabbitmqctl list_queues name messages ... consumers")
    return out


# ---------------------------------------------------------------- анализ

class Findings(object):
    def __init__(self):
        self.items = []

    def add(self, level, check, summary, details=None, hint=None):
        self.items.append({"level": level, "check": check, "summary": summary,
                           "details": list(details or []), "hint": hint or ""})

    def worst(self):
        order = {"OK": 0, "INFO": 0, "WARN": 1, "CRIT": 2}
        w = 0
        for i in self.items:
            w = max(w, order.get(i["level"], 0))
        return {0: "OK", 1: "WARN", 2: "CRIT"}[w]


def service_hint(q, snap):
    """Кто должен читать очередь: по справке, по потребителям или по похожим очередям."""
    n = q["name"].lower()
    if "indexing_service" in n:
        return "сервис индексирования IndexingService (имя очереди по справке RX)"
    if n.startswith("ario-") or q["vhost"].lower().startswith("ario"):
        return "сервисы Directum Ario"
    conns = {c["name"]: c for c in snap.get("connections") or []}
    by_queue = {}
    for c in snap.get("consumers") or []:
        cl = conns.get(c["connection"], {}).get("client") or c["connection"]
        by_queue.setdefault((c["vhost"], c["queue"]), set()).add(cl)
    sh = shape(q["name"])
    for other in snap.get("queues") or []:
        if other is q or other["vhost"] != q["vhost"] or not other["consumers"]:
            continue
        if shape(other["name"]) == sh and sh != q["name"]:
            who = sorted(by_queue.get((other["vhost"], other["name"]), []))
            return "такие же очереди читает %s" % (", ".join(who[:2]) if who else "живой экземпляр сервиса")
    return "сервис по имени не определён, сверьте со списком сервисов и логами"


def consumers_of(q, snap):
    conns = {c["name"]: c for c in snap.get("connections") or []}
    out = set()
    for c in snap.get("consumers") or []:
        if c["vhost"] == q["vhost"] and c["queue"] == q["name"]:
            out.add(conns.get(c["connection"], {}).get("client") or c["connection"])
    return sorted(out)


def check_nodes(snap, F):
    nodes = snap.get("nodes") or []
    if not nodes:
        return
    down = [n["name"] for n in nodes if n.get("running") is False]
    parts = [(n["name"], n["partitions"]) for n in nodes if n.get("partitions")]
    if down:
        F.add("CRIT", "nodes", "Узлы кластера не работают: %d из %d" % (len(down), len(nodes)), down,
              "вернуть узлы; очереди на них недоступны, кворумные очереди могут потерять большинство")
    if parts:
        F.add("CRIT", "nodes", "Разделение сети в кластере (partitions)",
              ["%s не видит %s" % (n, ", ".join(p)) for n, p in parts],
              "разбор: references/cluster-quorum.md")
    if nodes and not down and not parts:
        F.add("OK", "nodes", "Узлы: работают %d из %d" % (len(nodes), len(nodes)))
    alarms = []
    for n in nodes:
        if n.get("mem_alarm"):
            alarms.append("%s: тревога по памяти (memory resource limit alarm)" % n["name"])
        if n.get("disk_free_alarm"):
            alarms.append("%s: тревога по диску (disk resource limit alarm)" % n["name"])
    if alarms:
        F.add("CRIT", "alarms", "Брокер заблокировал публикацию: активна тревога", alarms,
              "пока тревога горит, сервисы RX не могут отправить ни одного сообщения; разбор: references/alarms-limits.md")
    else:
        F.add("OK", "alarms", "Тревог по памяти и диску нет")
    for n in nodes:
        if n.get("running") is False:
            continue
        nm = n["name"]
        mu, ml = n.get("mem_used"), n.get("mem_limit")
        if mu is not None and ml:
            r = float(mu) / ml
            line = "%s: память %s из порога %s (%s)" % (nm, hb(mu), hb(ml), pct(r))
            if r >= MEM_WARN and not n.get("mem_alarm"):
                F.add("WARN", "memory", "Память брокера близко к порогу", [line],
                      "найти очередь, где копятся сообщения, или тысячи очередей и соединений; "
                      "в контейнере проверить, что порог посчитан от лимита контейнера")
            else:
                F.add("OK", "memory", line)
        df, dl = n.get("disk_free"), n.get("disk_free_limit")
        if df is not None and dl is not None:
            line = "%s: диск свободно %s, порог %s" % (nm, hb(df), hb(dl))
            if not n.get("disk_free_alarm") and dl and df < dl * DISK_FACTOR:
                F.add("WARN", "disk", "Свободного места меньше двух порогов disk_free_limit", [line],
                      "освободить или расширить том брокера, пока он не заблокировал публикацию")
            elif dl < DISK_LIMIT_MIN:
                F.add("WARN", "disk", "Порог диска мал: %s" % hb(dl), [line],
                      "с таким порогом брокер остановит приём, когда места почти нет; "
                      "калькулятор drxinfra советует не меньше 2 ГБ и не меньше полутора порогов памяти")
            else:
                F.add("OK", "disk", line)
        fu, ft = n.get("fd_used"), n.get("fd_total")
        if fu is not None and ft:
            r = float(fu) / ft
            line = "%s: открытых файлов %d из %d (%s)" % (nm, fu, ft, pct(r))
            if r >= USE_CRIT:
                F.add("CRIT", "fd", "Дескрипторы на исходе", [line], "поднять лимит LimitNOFILE / --ulimit nofile")
            elif r >= USE_WARN:
                F.add("WARN", "fd", "Дескрипторы близко к лимиту", [line], "поднять лимит, искать утечку соединений")
            elif ft < FD_MIN:
                F.add("WARN", "fd", "Лимит открытых файлов меньше %d" % FD_MIN, [line],
                      "у службы LimitNOFILE, у контейнера --ulimit nofile; limits.conf на службы systemd не действует")
            else:
                F.add("OK", "fd", line)
        for used, total, what in (("sockets_used", "sockets_total", "сокетов"), ("proc_used", "proc_total", "процессов Erlang")):
            u, t = n.get(used), n.get(total)
            if u is not None and t:
                r = float(u) / t
                if r >= USE_WARN:
                    F.add("WARN", "limits", "Близко к лимиту %s" % what, ["%s: %d из %d (%s)" % (nm, u, t, pct(r))])


def check_queues(snap, F, th):
    qs = snap.get("queues") or []
    total = sum(q["messages"] or 0 for q in qs)
    F.add("INFO", "queues", "Очередей: %d, сообщений всего: %d, потребителей: %d"
          % (len(qs), total, sum(q["consumers"] for q in qs)))
    broken = [q for q in qs if q["state"].lower() in BAD_STATES or (q["messages"] is None and q["state"])]
    if broken:
        F.add("CRIT", "queue_state", "Очереди в нерабочем состоянии: %d" % len(broken),
              ["%s: %s" % (qid(q), q["state"] or "нет данных") for q in broken[:20]],
              "для кворумных: rabbitmq-queues quorum_status; см. references/cluster-quorum.md")
    orphan_work = [q for q in qs if not q["consumers"] and (q["messages"] or 0) > 0
                   and not temp_kind(q["name"]) and not q["exclusive"]]
    if orphan_work:
        orphan_work.sort(key=lambda q: -(q["messages"] or 0))
        F.add("CRIT", "no_consumers",
              "Очереди с сообщениями и без потребителей: %d, сообщений в них: %d"
              % (len(orphan_work), sum(q["messages"] or 0 for q in orphan_work)),
              ["%s: %d (готовых %s, в обработке %s); %s"
               % (qid(q), q["messages"], nv(q["ready"]), nv(q["unacked"]), service_hint(q, snap))
               for q in orphan_work[:15]],
              "сообщения никто не забирает: найти сервис, проверить, жив ли он и подключён ли к брокеру")
    idle_empty = [q for q in qs if not q["consumers"] and not q["messages"] and not temp_kind(q["name"])
                  and not q["exclusive"]]
    if idle_empty:
        F.add("INFO", "no_consumers", "Пустые очереди без потребителей (не временные): %d" % len(idle_empty),
              [qid(q) for q in idle_empty[:10]] + (["..."] if len(idle_empty) > 10 else []),
              "очередь ноль при нуле потребителей тоже повод проверить, должен ли её кто-то читать")
    big = [q for q in qs if (q["messages"] or 0) >= th["warn_messages"] and q["consumers"]]
    big.sort(key=lambda q: -(q["messages"] or 0))
    if big:
        crit = [q for q in big if q["messages"] >= th["crit_messages"]]
        lines = []
        for q in big[:15]:
            who = consumers_of(q, snap)
            cap = (", загрузка потребителей %s" % pct(q["capacity"])) if q["capacity"] is not None else ""
            lines.append("%s: %d (готовых %s, в обработке %s), потребителей %d%s%s"
                         % (qid(q), q["messages"], nv(q["ready"]), nv(q["unacked"]), q["consumers"], cap,
                            ("; " + ", ".join(who[:2])) if who else ""))
        F.add("CRIT" if crit else "WARN", "backlog",
              "Накопление в очередях с потребителями: %d (порог %d, критично от %d)"
              % (len(big), th["warn_messages"], th["crit_messages"]), lines,
              "обработчик не успевает: база (блокировки, долгие запросы), ошибка на каждом сообщении, ресурсы сервиса; "
              "рост покажет второй замер (--interval)")
    unacked = [q for q in qs if (q["unacked"] or 0) >= th["warn_messages"]]
    if unacked:
        F.add("WARN", "unacked", "Очереди с большим числом неподтверждённых сообщений: %d" % len(unacked),
              ["%s: в обработке %d, потребителей %d" % (qid(q), q["unacked"], q["consumers"]) for q in unacked[:10]],
              "потребитель взял сообщения и не подтверждает: завис обработчик или долгая операция")
    if not orphan_work and not big and not broken:
        F.add("OK", "backlog", "Накоплений нет: ни одна очередь не дошла до %d сообщений, у очередей с сообщениями есть потребители"
              % th["warn_messages"])
    top = sorted([q for q in qs if q["messages"]], key=lambda q: -(q["messages"] or 0))[:10]
    if top:
        F.add("INFO", "top", "Самые большие очереди",
              ["%s: %d (готовых %s, в обработке %s), потребителей %d, тип %s"
               % (qid(q), q["messages"], nv(q["ready"]), nv(q["unacked"]), q["consumers"], q["type"]) for q in top])


def temp_groups(qs):
    groups = {}
    for q in qs:
        k = temp_kind(q["name"])
        if not k or k == "server":
            continue
        g = groups.setdefault((q["vhost"], shape(q["name"])), {"total": 0, "with_consumers": 0,
                                                                "idle": 0, "messages": 0, "with_messages": 0})
        g["total"] += 1
        if q["consumers"]:
            g["with_consumers"] += 1
        else:
            g["idle"] += 1
            if q["messages"]:
                g["with_messages"] += 1
                g["messages"] += q["messages"]
    return groups


def check_temp(snap, F, th):
    qs = snap.get("queues") or []
    groups = temp_groups(qs)
    if not groups:
        F.add("OK", "temp_queues", "Очередей с GUID или хешем в имени нет")
        return
    idle = sum(g["idle"] for g in groups.values())
    total = sum(g["total"] for g in groups.values())
    with_msg = sum(g["messages"] for g in groups.values())
    rows = sorted(groups.items(), key=lambda kv: -kv[1]["idle"])
    lines = ["%s %s: всего %d, с потребителями %d, без потребителей %d, из них с сообщениями %d (%d сообщ.)"
             % (vh, sh, g["total"], g["with_consumers"], g["idle"], g["with_messages"], g["messages"])
             for (vh, sh), g in rows[:10]]
    many = [(k, g) for k, g in rows if g["total"] >= 3 and g["idle"] >= 2]
    if idle >= th["warn_temp"]:
        F.add("WARN", "temp_queues", "Временные очереди без потребителей: %d из %d (порог %d)" % (idle, total, th["warn_temp"]),
              lines, "похоже на утечку очередей экземпляров: политика с expires только после проверки шаблона "
              "(--match) и согласия, см. references/temp-queues-policy.md")
    else:
        F.add("INFO", "temp_queues", "Очередей с GUID или хешем в имени: %d, без потребителей: %d" % (total, idle),
              lines if many else [], "однотипные очереди, у которых живая только одна, остаются после перезапусков сервисов"
              if many else "")
    # Одиночная очередь с GUID (не серия копий) с сообщениями и без потребителя похожа на очередь
    # фонового процесса или обработчика, которую перестали читать, а не на остаток старого экземпляра.
    single = [q for q in qs if temp_kind(q["name"]) in ("guid", "hex") and not q["consumers"] and q["messages"]
              and groups.get((q["vhost"], shape(q["name"])), {}).get("total") == 1]
    if single:
        F.add("WARN", "temp_queues", "Одиночные очереди с GUID: есть сообщения, нет потребителей: %d" % len(single),
              ["%s: %d" % (qid(q), q["messages"]) for q in sorted(single, key=lambda x: -(x["messages"] or 0))[:10]],
              "очередь с GUID может принадлежать фоновому процессу или асинхронному обработчику (справка RX, "
              "monitoring_sostoyanie_rabbitmq.htm): сверьте GUID с Sungero_Core_Job.JobId и проверьте Worker")
    if with_msg >= th["warn_messages"]:
        F.add("WARN", "temp_queues", "В очередях с GUID без потребителей лежат сообщения: %d" % with_msg,
              [l for l in lines if "с сообщениями 0 " not in l][:10],
              "старые очереди экземпляров могут хранить остатки, но столько сообщений стоит разобрать: "
              "сверить GUID с Sungero_Core_Job.JobId, прежде чем считать мусором")


def check_quorum(snap, F):
    qq = [q for q in snap.get("queues") or [] if q["type"] == "quorum"]
    if not qq:
        return
    bad, warn = [], []
    for q in qq:
        m, o = len(q["members"]), len(q["online"])
        if not m:
            continue
        if o <= m // 2 or not q["leader"]:
            bad.append("%s: в строю %d из %d, лидер %s" % (qid(q), o, m, q["leader"] or "нет"))
        elif o < m:
            warn.append("%s: в строю %d из %d" % (qid(q), o, m))
    if bad:
        F.add("CRIT", "quorum", "Кворумные очереди без большинства или без лидера: %d" % len(bad), bad[:15],
              "rabbitmq-queues quorum_status --vhost <vhost> <очередь>; вернуть узлы, см. references/cluster-quorum.md")
    if warn:
        F.add("WARN", "quorum", "Кворумные очереди с недоступными копиями: %d" % len(warn), warn[:15])
    if not bad and not warn:
        F.add("OK", "quorum", "Кворумных очередей: %d, у всех есть большинство копий" % len(qq))


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


def check_policies(snap, F):
    ver = version_tuple(snap.get("broker", {}).get("version"))
    pols = snap.get("policies")
    qs = snap.get("queues") or []
    if pols is None:
        return
    ha = [p for p in pols if any(k.startswith("ha-") for k in p["definition"])]
    if ha:
        names = ["%s %s: шаблон '%s', %s" % (p["vhost"], p["name"], p["pattern"], json.dumps(p["definition"], ensure_ascii=False))
                 for p in ha]
        if ver >= (4,):
            F.add("WARN", "policies", "Политика зеркалирования (ha-mode) на RabbitMQ %s не действует" % snap["broker"]["version"],
                  names, "в 4.0 зеркалирование классических очередей удалено, очереди хранятся в одной копии")
        else:
            F.add("INFO", "policies", "Зеркалирование классических очередей (ha-mode)", names,
                  "так настраивает кластер справка RX 25.2 (admin_cluster_rmq.htm); в RabbitMQ 4.0 зеркалирование удалено, учесть перед обновлением")
    for p in pols:
        if "expires" in p["definition"]:
            try:
                rx = re.compile(p["pattern"])
                n = sum(1 for q in qs if q["vhost"] == p["vhost"] and rx.search(q["name"]))
            except re.error:
                n = -1
            eff = sum(1 for q in qs if q["policy"] == p["name"] and q["vhost"] == p["vhost"])
            F.add("INFO", "policies", "Политика срока жизни очередей %s %s: expires %s мс, шаблон '%s'"
                  % (p["vhost"], p["name"], p["definition"]["expires"], p["pattern"]),
                  ["подходит по шаблону очередей: %s, действует на: %d" % (n if n >= 0 else "?", eff)])
    lossy = [p for p in pols if any(k in p["definition"] for k in ("max-length", "max-length-bytes", "message-ttl"))]
    if lossy:
        F.add("INFO", "policies", "Политики, которые могут удалять сообщения (max-length, message-ttl)",
              ["%s %s: '%s' %s" % (p["vhost"], p["name"], p["pattern"], json.dumps(p["definition"], ensure_ascii=False))
               for p in lossy])
    ario = [q for q in qs if q["name"].startswith("ario-")]
    if ario and ver < (3, 13):
        F.add("WARN", "version", "Очереди Directum Ario на RabbitMQ %s" % snap["broker"]["version"],
              ["очередей ario-*: %d" % len(ario)],
              "по справке RX сервисам Ario с версии 2024.6.1.0 нужен RabbitMQ 3.13 и выше")


def check_connections(snap, F, expect_tls=False):
    conns = snap.get("connections")
    if conns is None:
        return
    if not conns:
        F.add("WARN", "connections", "Соединений с брокером нет",
              [], "сервисы RX не подключены: брокер пуст для них или смотрят на другой адрес/vhost")
        return
    blocked = [c for c in conns if c["state"] in ("blocked", "blocking")]
    if blocked:
        F.add("CRIT", "connections", "Соединения заблокированы брокером: %d" % len(blocked),
              ["%s %s %s" % (c["peer_host"], c["user"], c["client"]) for c in blocked[:10]],
              "брокер не принимает публикации: тревога по памяти или диску")
    flow = [c for c in conns if c["state"] == "flow"]
    if flow:
        F.add("WARN", "connections", "Соединения в режиме flow control: %d" % len(flow),
              ["%s %s" % (c["peer_host"], c["client"]) for c in flow[:10]],
              "брокер притормаживает публикацию: не успевает писать на диск или в очередь")
    many_ch = [c for c in conns if c["channels"] >= CHANNELS_WARN]
    if many_ch:
        F.add("WARN", "connections", "Соединения с тысячей каналов и больше: %d" % len(many_ch),
              ["%s %s: каналов %d" % (c["peer_host"], c["client"], c["channels"]) for c in many_ch[:10]],
              "похоже на утечку каналов; лимит channel_max по умолчанию 2047")
    by = {}
    for c in conns:
        k = (c["user"], c["peer_host"])
        by[k] = by.get(k, 0) + 1
    top = sorted(by.items(), key=lambda kv: -kv[1])[:8]
    tls = sum(1 for c in conns if c["ssl"])
    F.add("INFO", "connections", "Соединений: %d, каналов: %d, по TLS: %d, без TLS: %d"
          % (len(conns), sum(c["channels"] for c in conns), tls, len(conns) - tls),
          ["%s с %s: %d" % (u or "?", h or "?", n) for (u, h), n in top])
    if expect_tls and tls < len(conns):
        F.add("WARN", "tls", "Клиенты без TLS: %d" % (len(conns) - tls),
              sorted(set("%s %s %s" % (c["peer_host"], c["user"], c["client"]) for c in conns if not c["ssl"]))[:20],
              "до закрытия порта 5672 этот список должен быть пустым хотя бы сутки")
    users = snap.get("users")
    if users and "guest" in users:
        F.add("WARN", "users", "Есть пользователь guest",
              [], "его пароль знают все; справка RX советует удалить guest после создания своего администратора")
    if any(c["user"] == "guest" for c in conns):
        F.add("WARN", "users", "Клиенты подключены под guest", sorted(set(c["peer_host"] for c in conns if c["user"] == "guest")))


def check_version(snap, F):
    b = snap.get("broker") or {}
    nodes = snap.get("nodes") or []
    if not b:
        F.add("INFO", "version", "Узлы, тревоги, соединения и политики не проверялись: на входе только список очередей")
        return
    F.add("INFO", "version", "RabbitMQ %s, Erlang %s%s, узлов %d"
          % (b.get("version", "?"), b.get("erlang", "?"), (", кластер " + b["cluster"]) if b.get("cluster") else "",
             len(nodes)))
    if version_tuple(b.get("version")) < (3, 8) and b.get("version") not in (None, "?"):
        F.add("WARN", "version", "Старая версия RabbitMQ %s" % b.get("version"),
              [], "метрики Prometheus и часть проверок есть с 3.8; версии под вашу RX смотрите в типовых требованиях вендора")


def check_growth(prev, snap, F, th):
    dt = max(1.0, snap["taken_at"] - prev["taken_at"])
    before = {(q["vhost"], q["name"]): q for q in prev.get("queues") or []}
    grow, stuck, fresh = [], [], 0
    for q in snap.get("queues") or []:
        p = before.get((q["vhost"], q["name"]))
        if p is None:
            fresh += 1
            continue
        if q["messages"] is None or p["messages"] is None:
            continue
        d = q["messages"] - p["messages"]
        if d >= th["growth_min"]:
            grow.append((q, d))
        if (q["unacked"] or 0) > 0 and q["unacked"] == p["unacked"] and (q["ready"] or 0) >= (p["ready"] or 0) \
                and (q["ack"] is None or p.get("ack") is None or q["ack"] == p.get("ack")) and q["consumers"]:
            stuck.append(q)
    mins = dt / 60.0
    if grow:
        grow.sort(key=lambda x: -x[1])
        level = "CRIT" if any(not q["consumers"] for q, _ in grow) else "WARN"
        F.add(level, "growth", "Очереди растут: %d за %.0f с" % (len(grow), dt),
              ["%s: +%d (%.0f в минуту), сейчас %d, потребителей %d%s"
               % (qid(q), d, d / mins, q["messages"], q["consumers"],
                  ("; " + ", ".join(consumers_of(q, snap)[:2])) if consumers_of(q, snap) else "")
               for q, d in grow[:15]],
              "если рост не прекращается, обработчик не успевает или не работает")
    else:
        F.add("OK", "growth", "За %.0f с ни одна очередь не выросла на %d и больше" % (dt, th["growth_min"]))
    if stuck:
        tmo = sorted(set(c["timeout"] for c in snap.get("consumers") or [] if c.get("timeout")))
        F.add("WARN", "unacked", "Сообщения в обработке не двигаются: очередей %d" % len(stuck),
              ["%s: в обработке %d, готовых %d%s" % (qid(q), q["unacked"], q["ready"] or 0,
                                                     ("; " + ", ".join(consumers_of(q, snap)[:2])) if consumers_of(q, snap) else "")
               for q in stuck[:10]]
              + (["consumer_timeout у потребителей: %s мин" % ", ".join("%g" % (t / 60000.0) for t in tmo)] if tmo else []),
              "обработчик взял сообщения и не подтверждает: ищите долгую операцию или зависание; "
              "через consumer_timeout брокер закроет канал")
    if fresh:
        F.add("INFO", "growth", "Новых очередей за интервал: %d" % fresh)
    c0, c1 = prev.get("churn") or {}, snap.get("churn") or {}
    if c0.get("connection_created") is not None and c1.get("connection_created") is not None:
        n = c1["connection_created"] - c0["connection_created"]
        per_min = n / mins
        if per_min >= CHURN_WARN:
            F.add("WARN", "connections", "Шторм подключений: %d новых соединений за %.0f с (%.0f в минуту)" % (n, dt, per_min),
                  [], "клиент переподключается по кругу: учётка, TLS, таймаут простоя на балансировщике, перезапуски сервиса")
        else:
            F.add("OK", "connections", "Новых соединений за интервал: %d" % n)


def analyze(snap, th=None, prev=None, expect_tls=False):
    th = dict(DEFAULTS, **(th or {}))
    F = Findings()
    check_version(snap, F)
    check_nodes(snap, F)
    check_queues(snap, F, th)
    check_temp(snap, F, th)
    check_quorum(snap, F)
    check_policies(snap, F)
    check_connections(snap, F, expect_tls)
    if prev is not None:
        check_growth(prev, snap, F, th)
    for v in snap.get("hidden_vhosts") or []:
        F.add("WARN" if v["messages"] else "INFO", "access",
              "vhost %s: у учётки нет прав, очереди не видны%s" % (
                  v["name"], (", а сообщений в нём %d" % v["messages"]) if v["messages"] else ""),
              [], "учётке мониторинга нужны права \"^$\" \"^$\" \"^$\" на каждый vhost, тег monitoring их не заменяет")
    for n in snap.get("notes") or []:
        F.add("INFO", "notes", n)
    return F


def match_report(snap, pattern):
    """Какие очереди попадут под шаблон политики. Ничего не меняет."""
    F = Findings()
    try:
        rx = re.compile(pattern)
    except re.error as e:
        raise CheckError("шаблон не разбирается: %s" % e)
    qs = snap.get("queues") or []
    hit = [q for q in qs if rx.search(q["name"])]
    if not hit:
        F.add("INFO", "match", "Под шаблон '%s' не попала ни одна очередь из %d" % (pattern, len(qs)))
        return F
    plain = [q for q in hit if not temp_kind(q["name"])]
    busy = [q for q in hit if q["consumers"]]
    with_msg = [q for q in hit if q["messages"]]
    F.add("INFO", "match", "Под шаблон '%s' попадает очередей: %d из %d; с потребителями %d, с сообщениями %d"
          % (pattern, len(hit), len(qs), len(busy), len(with_msg)),
          ["%s %s: %d шт." % (vh, sh, n) for (vh, sh), n in sorted(
              count_by(hit, lambda q: (q["vhost"], shape(q["name"]))).items(), key=lambda kv: -kv[1])[:10]])
    if plain:
        F.add("WARN", "match", "Шаблон задевает очереди без GUID и хеша в имени: %d" % len(plain),
              [qid(q) for q in plain[:15]], "рабочие очереди сервисов под шаблон попадать не должны: сузьте шаблон")
    if with_msg:
        F.add("WARN", "match", "Под шаблон попадают очереди с сообщениями: %d" % len(with_msg),
              ["%s: %d, потребителей %d" % (qid(q), q["messages"], q["consumers"]) for q in with_msg[:15]],
              "expires удаляет очередь вместе с сообщениями, если её никто не использует дольше срока")
    singles = [k for k, n in count_by(hit, lambda q: (q["vhost"], shape(q["name"]))).items() if n == 1]
    if singles:
        F.add("WARN", "match", "Одиночные очереди с GUID (не серия копий): %d" % len(singles),
              ["%s %s" % k for k in singles[:10]],
              "одиночная очередь с GUID может быть очередью фонового процесса или обработчика: сверьте GUID с "
              "Sungero_Core_Job.JobId; если сервис может стоять дольше срока expires, очередь удалится")
    pols = count_by([q for q in hit if q["policy"]], lambda q: (q["vhost"], q["policy"]))
    if pols:
        F.add("WARN", "match", "На этих очередях уже действует политика",
              ["%s %s: очередей %d" % (vh, p, n) for (vh, p), n in pols.items()],
              "к очереди применяется одна политика, с наибольшим приоритетом: новая вытеснит старую "
              "(например ha-all), её ключи придётся повторить в новой")
    return F


def count_by(items, key):
    out = {}
    for i in items:
        k = key(i)
        out[k] = out.get(k, 0) + 1
    return out


# ---------------------------------------------------------------- вывод

def render_text(F, head):
    out = [head]
    order = {"CRIT": 0, "WARN": 1, "OK": 2, "INFO": 3}
    for i in sorted(F.items, key=lambda x: order.get(x["level"], 9)):
        out.append("%-4s  %s" % (i["level"], i["summary"]))
        for d in i["details"]:
            out.append("      " + d)
        if i["hint"] and i["level"] in ("CRIT", "WARN", "INFO"):
            out.append("      > " + i["hint"])
    crit = sum(1 for i in F.items if i["level"] == "CRIT")
    warn = sum(1 for i in F.items if i["level"] == "WARN")
    out.append("Итог: %s, проблем %d, предупреждений %d" % (F.worst(), crit, warn))
    return "\n".join(out)


def snapshot_json(snap, F, target):
    return {
        "tool": "mq_check", "tool_version": TOOL_VERSION, "source": snap.get("source"), "target": target,
        "taken_at": snap.get("taken_at"), "broker": snap.get("broker"), "status": F.worst(),
        "findings": F.items, "churn": snap.get("churn") or {},
        "queues": [{k: q[k] for k in ("vhost", "name", "type", "state", "messages", "ready", "unacked",
                                      "consumers", "policy", "ack")} for q in snap.get("queues") or []],
    }


def load_prev(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        raise CheckError("не читается %s: %s" % (path, e))
    if d.get("tool") != "mq_check" or "queues" not in d:
        raise CheckError("%s не похож на вывод mq_check.py --json" % path)
    return {"taken_at": d.get("taken_at") or 0, "churn": d.get("churn") or {}, "queues": d["queues"]}


def credentials(args):
    url = args.url or os.environ.get("RABBITMQ_URL") or "http://localhost:15672"
    p = urlsplit(url)
    if p.scheme not in ("http", "https"):
        raise CheckError("адрес %s не похож на API управления: нужен http(s)://брокер:15672, не amqp://" % safe_url(url))
    user = args.user or os.environ.get("RABBITMQ_USER") or p.username
    password = os.environ.get("RABBITMQ_PASSWORD") or p.password
    if args.netrc:
        try:
            auth = netrc.netrc(args.netrc).authenticators(p.hostname or "")
        except (OSError, netrc.NetrcParseError) as e:
            raise CheckError("не читается %s: %s" % (args.netrc, e))
        if auth:
            user = user or auth[0]
            password = password or auth[2]
    if not user:
        raise CheckError("не задано имя: RABBITMQ_USER или --user (лучше учётка с тегом monitoring)")
    if password is None:
        raise CheckError("не задан пароль: RABBITMQ_PASSWORD или --netrc ФАЙЛ")
    return safe_url(url), user, password



def read_stdin():
    """Ввод читаем байтами и декодируем как UTF-8: не зависит от локали."""
    buf = getattr(sys.stdin, "buffer", None)
    return buf.read().decode("utf-8", "replace") if buf is not None else sys.stdin.read()


def utf8_console():
    """Python 3.6 при LANG=C пишет в ASCII и падает на кириллице: переводим вывод на UTF-8."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        enc = (getattr(stream, "encoding", None) or "").lower().replace("-", "").replace("_", "")
        if enc and enc != "utf8" and hasattr(stream, "buffer"):
            setattr(sys, name, io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace", line_buffering=True))

def main(argv=None):
    utf8_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="адрес API управления, по умолчанию $RABBITMQ_URL или http://localhost:15672")
    ap.add_argument("--user", help="имя, по умолчанию $RABBITMQ_USER")
    ap.add_argument("--netrc", metavar="ФАЙЛ", help="файл netrc: machine ХОСТ login ИМЯ password ПАРОЛЬ")
    ap.add_argument("--cacert", metavar="ФАЙЛ", default=os.environ.get("RABBITMQ_CA"),
                    help="корневой сертификат для https, по умолчанию $RABBITMQ_CA")
    ap.add_argument("--insecure", action="store_true", help="не проверять сертификат API (только осознанно)")
    ap.add_argument("--timeout", type=float, default=15.0, help="тайм-аут запроса, с")
    ap.add_argument("--vhost", help="только этот виртуальный хост")
    ap.add_argument("--ctl", action="store_true", help="через rabbitmqctl на этом сервере")
    ap.add_argument("--ctl-prefix", default="", help='префикс команды, например "docker exec rabbitmq"')
    ap.add_argument("--stdin", action="store_true", help="разобрать вывод list_queues со стандартного ввода")
    ap.add_argument("--interval", type=int, default=0, metavar="С", help="второй замер через С секунд")
    ap.add_argument("--compare", metavar="ФАЙЛ", help="сравнить с сохранённым выводом --json")
    ap.add_argument("--match", metavar="РЕГВЫР", help="какие очереди попадут под шаблон политики")
    ap.add_argument("--expect-tls", action="store_true", help="считать отклонением клиентов без TLS")
    ap.add_argument("--warn-messages", type=int, default=DEFAULTS["warn_messages"])
    ap.add_argument("--crit-messages", type=int, default=DEFAULTS["crit_messages"])
    ap.add_argument("--warn-temp", type=int, default=DEFAULTS["warn_temp"])
    ap.add_argument("--growth-min", type=int, default=DEFAULTS["growth_min"])
    ap.add_argument("--json", action="store_true", help="вывод в JSON")
    args = ap.parse_args(argv)
    th = {"warn_messages": args.warn_messages, "crit_messages": args.crit_messages,
          "warn_temp": args.warn_temp, "growth_min": args.growth_min}
    try:
        if args.stdin:
            snap = {"source": "stdin", "notes": [], "taken_at": time.time(), "broker": {},
                    "queues": parse_queue_text(read_stdin(), args.vhost or "/"), "nodes": [],
                    "connections": None, "policies": None}
            target = "stdin"
            take = None
        elif args.ctl:
            target = "rabbitmqctl" + (" через " + args.ctl_prefix if args.ctl_prefix else "")
            take = lambda: collect_ctl(args.ctl_prefix, args.vhost)
            snap = take()
        else:
            target, user, password = credentials(args)
            api = Api(target, user, password, args.cacert, args.insecure, args.timeout)
            take = lambda: collect_api(api.get, args.vhost)
            snap = take()
        prev = None
        if args.compare:
            prev = load_prev(args.compare)
        elif args.interval and take:
            prev = snap
            time.sleep(args.interval)
            snap = take()
        if args.match:
            F = match_report(snap, args.match)
        else:
            F = analyze(snap, th, prev, args.expect_tls)
    except CheckError as e:
        if args.json:
            print(json.dumps({"tool": "mq_check", "status": "ERROR", "error": str(e)}, ensure_ascii=False))
        else:
            print("ERROR  " + str(e))
        return 2
    head = "RabbitMQ: %s; замер %s%s" % (target, time.strftime("%Y-%m-%d %H:%M:%S"),
                                        (", сравнение с предыдущим" if prev else ""))
    if args.insecure and target.startswith("https"):
        head += "; ВНИМАНИЕ: сертификат не проверялся (--insecure)"
    if args.json:
        print(json.dumps(snapshot_json(snap, F, target), ensure_ascii=False, indent=1))
    else:
        print(render_text(F, head))
    return 0 if F.worst() == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
