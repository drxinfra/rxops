#!/usr/bin/env python3
"""Проверка Elasticsearch под полнотекстовый поиск Directum RX. Только GET-запросы.

Что смотрит: версию (для поиска RX вендор поддерживает 7.17.23), статус кластера,
неназначенные шарды и причину (allocation explain, только если они есть), диск
узлов к порогам watermark, блокировки записи индексов (read_only_allow_delete),
кучу JVM, предохранители (circuit breakers), отклонённые запросы, шарды к лимиту
max_shards_per_node, индексы RX (rxsearch_*): статус, документы, размер шарда.

Подключение: ES_URL (по умолчанию http://localhost:9200), ES_USER, ES_PASSWORD,
ES_CA (корневой сертификат для https) или --netrc ФАЙЛ. Пароль в вывод не попадает.
Проверять лучше с сервера, где работает сервис индексирования RX, тем же адресом
и той же учёткой, что в ELASTICSEARCH_URL и ELASTICSEARCH_LOGIN.

Примеры:
  ES_URL=http://es01:9200 python3 es_check.py
  ES_URL=https://es01:9200 ES_USER=elastic ES_CA=/etc/es/http_ca.crt ES_PASSWORD=... python3 es_check.py
  python3 es_check.py --json > before.json; ... ; python3 es_check.py --compare before.json
  python3 es_check.py --interval 300          прирост документов в индексах RX за 5 минут

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
import ssl
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

TOOL_VERSION = "0.1.0"
RX_VERSION = "7.17.23"          # справка RX 25.2: поддерживаемая версия Elasticsearch
HEAP_WARN = 85                  # % кучи
SHARDS_WARN, SHARDS_CRIT = 0.80, 0.95
SHARDS_PER_GB_HEAP = 20         # ориентир документации Elasticsearch 7.x
SHARD_MAX_GB = 30               # справка RX: рекомендуемый размер индекса Lucene (шарда) 30 ГБ
DISK_EARLY = 80                 # наблюдение за диском до первого порога
RX_INDEX = re.compile(r"^(?:(?P<prefix>.+?)_)?rxsearch_(?P<kind>document|workflowtask|workflowassignment)"
                      r"(?P<props>_properties)?_(?P<rest>.+)$")


class CheckError(Exception):
    """Проверку выполнить не удалось."""


class HttpFail(CheckError):
    def __init__(self, code, path, body=""):
        self.code = code
        hint = {401: "нужны имя и пароль (ES_USER, ES_PASSWORD) или они неверные",
                403: "у учётки нет прав: нужны cluster monitor и для индексов monitor, view_index_metadata"}.get(code, "")
        CheckError.__init__(self, "HTTP %d на %s%s" % (code, path, (": " + hint) if hint else ""))


# ---------------------------------------------------------------- мелочи

def hb(n):
    if n is None:
        return "?"
    n = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024 or unit == "ТБ":
            s = ("%.0f" % n) if unit in ("Б", "КБ", "МБ") else ("%.1f" % n)
            return s.replace(".", ",") + " " + unit
        n /= 1024.0


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def safe_url(url):
    p = urlsplit(url)
    netloc = p.hostname or ""
    if p.port:
        netloc += ":%d" % p.port
    return urlunsplit((p.scheme, netloc, p.path.rstrip("/"), "", ""))


def parse_size(v):
    """'100gb', '500mb', '1.5tb' в байты; None, если это не размер."""
    m = re.match(r"^\s*([\d.]+)\s*(b|kb|mb|gb|tb|pb)\s*$", str(v or "").lower())
    if not m:
        return None
    mult = {"b": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3, "tb": 1024 ** 4, "pb": 1024 ** 5}[m.group(2)]
    return float(m.group(1)) * mult


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


# ---------------------------------------------------------------- HTTP

class Api(object):
    def __init__(self, url, user=None, password=None, cacert=None, insecure=False, timeout=15.0):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.headers = {"Accept": "application/json", "User-Agent": "rxops-es_check/" + TOOL_VERSION}
        self.auth = bool(user)
        if user:
            tok = base64.b64encode(("%s:%s" % (user, password or "")).encode("utf-8")).decode("ascii")
            self.headers["Authorization"] = "Basic " + tok
        self.ctx = None
        if self.url.startswith("https://"):
            self.ctx = ssl.create_default_context(cafile=cacert) if cacert else ssl.create_default_context()
            if insecure:
                self.ctx.check_hostname = False
                self.ctx.verify_mode = ssl.CERT_NONE

    def get(self, path):
        """Единственный метод запроса: GET."""
        req = Request(self.url + path, headers=self.headers, method="GET")
        try:
            resp = urlopen(req, timeout=self.timeout, context=self.ctx) if self.ctx else \
                urlopen(req, timeout=self.timeout)
            body = resp.read()
        except HTTPError as e:
            raise HttpFail(e.code, path.split("?")[0])
        except URLError as e:
            raise CheckError(explain_net(self.url, getattr(e, "reason", e)))
        except (OSError, ValueError) as e:
            raise CheckError(explain_net(self.url, e))
        try:
            return json.loads(body.decode("utf-8"))
        except ValueError:
            raise CheckError("ответ на %s не JSON: по этому адресу не Elasticsearch или прокси вернул страницу"
                             % path.split("?")[0])


def explain_net(url, err):
    s = str(err)
    low = s.lower()
    if "certificate_verify_failed" in low or "certificate verify failed" in low:
        return ("сертификат %s не прошёл проверку: укажите корень через ES_CA или --cacert "
                "(у Elasticsearch 8 это config/certs/http_ca.crt); %s" % (safe_url(url), s))
    if url.startswith("http://") and ("reset" in low or "remote end closed" in low or "eof" in low):
        return ("%s оборвал соединение: похоже, на порту включён TLS (Elasticsearch 8 по умолчанию), "
                "попробуйте https:// и ES_CA; в журнале ES будет «received plaintext http traffic on an https channel»"
                % safe_url(url))
    if "wrong version number" in low or "unknown protocol" in low:
        return "%s: по https ответил не TLS, похоже, порт без TLS: попробуйте http://" % safe_url(url)
    if "refused" in low:
        return "%s: соединение отклонено: Elasticsearch не запущен, другой порт или закрыт сетевым экраном" % safe_url(url)
    if "timed out" in low:
        return "%s: нет ответа за отведённое время: сеть, сетевой экран или узел перегружен" % safe_url(url)
    return "нет связи с %s: %s" % (safe_url(url), s)


# ---------------------------------------------------------------- сбор

NODE_H = ("name,ip,node.role,master,heap.percent,heap.max,ram.percent,ram.max,disk.used_percent,disk.total,"
          "disk.avail,version")
IDX_H = "health,status,index,pri,rep,docs.count,store.size,pri.store.size"


def collect(get):
    snap = {"taken_at": time.time(), "notes": []}
    root = get("/")
    ver = root.get("version") or {}
    snap["version"] = ver.get("number", "?")
    snap["distribution"] = ver.get("distribution", "elasticsearch")
    snap["cluster"] = root.get("cluster_name", "")
    snap["health"] = get("/_cluster/health")
    snap["nodes"] = get("/_cat/nodes?format=json&bytes=b&h=" + NODE_H)
    snap["allocation"] = get("/_cat/allocation?format=json&bytes=b")
    snap["settings"] = get("/_cluster/settings?include_defaults=true&filter_path=*.cluster.routing.allocation.disk,"
                           "*.cluster.routing.allocation.enable,*.cluster.max_shards_per_node,*.cluster.blocks")
    snap["indices"] = get("/_cat/indices?format=json&bytes=b&expand_wildcards=open&h=" + IDX_H)

    def optional(path, what):
        try:
            return get(path)
        except HttpFail as e:
            snap["notes"].append("%s: не прочитано (HTTP %d)" % (what, e.code))
            return None

    snap["blocks"] = optional("/_all/_settings/index.blocks.*?flat_settings=true", "блокировки индексов")
    snap["stats"] = optional("/_nodes/stats/jvm,breaker,thread_pool?filter_path=nodes.*.name,"
                             "nodes.*.jvm.mem.heap_used_percent,nodes.*.jvm.mem.heap_max_in_bytes,nodes.*.breakers,"
                             "nodes.*.thread_pool.write.rejected,nodes.*.thread_pool.search.rejected,"
                             "nodes.*.thread_pool.write.queue,nodes.*.thread_pool.search.queue", "статистика узлов")
    snap["info"] = optional("/_nodes?filter_path=nodes.*.name,nodes.*.process.mlockall,"
                            "nodes.*.jvm.using_compressed_ordinary_object_pointers,nodes.*.jvm.mem.heap_max_in_bytes",
                            "сведения об узлах")
    snap["plugins"] = optional("/_cat/plugins?format=json&h=name,component,version", "плагины")
    snap["unassigned"] = []
    snap["explain"] = None
    if (snap["health"].get("unassigned_shards") or 0) > 0:
        shards = optional("/_cat/shards?format=json&h=index,shard,prirep,state,unassigned.reason,node", "шарды")
        snap["unassigned"] = [s for s in shards or [] if s.get("state") == "UNASSIGNED"]
        # GET без тела объясняет первый попавшийся неназначенный шард
        snap["explain"] = optional("/_cluster/allocation/explain", "allocation explain")
    return snap


# ---------------------------------------------------------------- анализ

class Findings(object):
    def __init__(self):
        self.items = []

    def add(self, level, check, summary, details=None, hint=None):
        self.items.append({"level": level, "check": check, "summary": summary,
                           "details": list(details or []), "hint": hint or ""})

    def worst(self):
        w = 0
        for i in self.items:
            w = max(w, {"WARN": 1, "CRIT": 2}.get(i["level"], 0))
        return {0: "OK", 1: "WARN", 2: "CRIT"}[w]


def setting(snap, key, default=None):
    """Значение настройки кластера: transient важнее persistent, тот важнее значения по умолчанию."""
    for scope in ("transient", "persistent", "defaults"):
        node = (snap.get("settings") or {}).get(scope) or {}
        cur = node
        ok = True
        for part in key.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and not isinstance(cur, dict):
            return cur
        flat = node.get(key)
        if flat is not None:
            return flat
    return default


def rx_indices(snap):
    out = []
    for i in snap.get("indices") or []:
        m = RX_INDEX.match(i.get("index", ""))
        if m:
            out.append((i, m))
    return out


def check_version(snap, F):
    v, dist = snap.get("version", "?"), snap.get("distribution") or "elasticsearch"
    rx = rx_indices(snap)
    head = "%s %s, кластер %s" % ("OpenSearch" if dist == "opensearch" else "Elasticsearch", v, snap.get("cluster", ""))
    if dist == "opensearch":
        F.add("WARN" if rx else "INFO", "version", head,
              [], "для поиска RX вендор поддерживает только Elasticsearch %s (справка RX 25.2)" % RX_VERSION)
    elif v == RX_VERSION:
        F.add("OK", "version", head + ": эту версию вендор поддерживает для поиска RX")
    elif version_tuple(v) >= (8,):
        F.add("WARN" if rx else "INFO", "version", head + (": здесь индексы RX" if rx else ""),
              [], "для полнотекстового поиска RX вендор поддерживает только %s (справка 25.2, "
              "admin_elasticsearch_update_linux.htm); 8.x годится для журналов" % RX_VERSION)
    else:
        F.add("WARN", "version", head, [], "вендор рекомендует обновить до %s (справка RX 25.2, "
              "admin_elasticsearch_update_linux.htm); сверьтесь со справкой своей версии RX" % RX_VERSION)


def check_plugins(snap, F):
    """Справка RX 25.2 после обновления ставит плагины analysis-morphology и ingest-attachment из поставки."""
    plugins = snap.get("plugins")
    if plugins is None or version_tuple(snap.get("version")) >= (8,) or snap.get("distribution") == "opensearch":
        return
    names = sorted(set(p.get("component", "") for p in plugins))
    missing = [want for want, part in (("analysis-morphology", "morphology"), ("ingest-attachment", "attachment"))
               if not any(part in n for n in names)]
    if missing and rx_indices(snap):
        F.add("WARN", "plugins", "Не видно плагинов из комплекта поставки RX: %s" % ", ".join(missing),
              ["установлены: %s" % (", ".join(names) or "нет")],
              "справка RX ставит их при установке и обновлении Elasticsearch (admin_elasticsearch_update_linux.htm); "
              "без них индексирование содержимого и морфология могут не работать, сверьтесь со справкой своей версии")
    elif names:
        F.add("INFO", "plugins", "Плагины: %s" % ", ".join(names))


def check_health(snap, F):
    h = snap.get("health") or {}
    st = h.get("status", "?")
    line = ("узлов %s (данных %s), шардов активных %s, неназначенных %s, инициализируются %s, переезжают %s"
            % (h.get("number_of_nodes"), h.get("number_of_data_nodes"), h.get("active_shards"),
               h.get("unassigned_shards"), h.get("initializing_shards"), h.get("relocating_shards")))
    details = [line]
    un = snap.get("unassigned") or []
    if un:
        by = {}
        for s in un:
            k = (s.get("index"), "основной" if s.get("prirep") == "p" else "реплика", s.get("unassigned.reason"))
            by[k] = by.get(k, 0) + 1
        details += ["%s: %s, причина %s, шардов %d" % (k[0], k[1], k[2], n) for k, n in sorted(by.items())[:15]]
    ex = snap.get("explain") or {}
    if ex:
        details.append("allocation explain: %s шард %s (%s): %s" % (
            ex.get("index"), ex.get("shard"), "основной" if ex.get("primary") else "реплика",
            ex.get("allocate_explanation") or ex.get("can_allocate") or ""))
        for nd in (ex.get("node_allocation_decisions") or [])[:3]:
            for d in (nd.get("deciders") or [])[:2]:
                details.append("  %s: %s: %s" % (nd.get("node_name"), d.get("decider"), (d.get("explanation") or "")[:220]))
    data_nodes = h.get("number_of_data_nodes") or 0
    only_replicas = un and all(s.get("prirep") == "r" for s in un)
    if st == "red":
        reds = [i["index"] for i in snap.get("indices") or [] if i.get("health") == "red"]
        F.add("CRIT", "health", "Кластер red: часть основных шардов не размещена", details + (["красные индексы: " + ", ".join(reds[:15])] if reds else []),
              "поиск отвечает неполно или ошибкой, индексация в эти индексы падает; причина в allocation explain")
    elif st == "yellow":
        if data_nodes == 1 and only_replicas:
            F.add("WARN", "health", "Кластер yellow: на одном узле реплики разместить негде", details,
                  "для одного узла number_of_replicas 0 у индексов и в шаблоне (изменение, только с согласия); "
                  "иначе к жёлтому привыкают и пропускают настоящую проблему")
        else:
            F.add("WARN", "health", "Кластер yellow: не размещены реплики", details,
                  "данные доступны, но копий нет; причина в allocation explain")
    elif st == "green":
        F.add("OK", "health", "Кластер green: " + line)
    else:
        F.add("WARN", "health", "Статус кластера: %s" % st, details)
    enable = setting(snap, "cluster.routing.allocation.enable", "all")
    if str(enable) != "all":
        F.add("WARN", "health", "Размещение шардов ограничено: cluster.routing.allocation.enable = %s" % enable,
              [], "так оставляют после обслуживания узлов; пока не all, шарды после перезапуска не поднимутся")
    pending = h.get("number_of_pending_tasks") or 0
    if pending >= 50:
        F.add("WARN", "health", "Очередь задач мастера: %d" % pending, [], "мастер не успевает: много шардов или тяжёлые изменения")


def disk_thresholds(snap):
    t = {}
    for name in ("low", "high", "flood_stage"):
        t[name] = setting(snap, "cluster.routing.allocation.disk.watermark." + name)
    return t


def over(th, used_pct, avail):
    """True, если порог пройден. Порог в процентах занятого или в байтах свободного."""
    if th is None:
        return False
    s = str(th).strip()
    if s.endswith("%"):
        v = num(s[:-1])
        return v is not None and used_pct is not None and used_pct >= v
    b = parse_size(s)
    if b is not None and avail is not None:
        return avail <= b
    v = num(s)                      # доля, например 0.85
    if v is not None and v <= 1 and used_pct is not None:
        return used_pct >= v * 100
    return False


def check_disk(snap, F):
    t = disk_thresholds(snap)
    enabled = str(setting(snap, "cluster.routing.allocation.disk.threshold_enabled", "true")).lower()
    if enabled == "false":
        F.add("WARN", "disk", "Пороги диска выключены (threshold_enabled = false)", [],
              "Elasticsearch не защитит себя: диск заполнится до конца, и узел может повредить данные")
    rows = []
    worst = 0
    for a in snap.get("allocation") or []:
        if a.get("node") == "UNASSIGNED":
            continue
        used, total, avail = num(a.get("disk.used")), num(a.get("disk.total")), num(a.get("disk.avail"))
        pctv = num(a.get("disk.percent"))
        if used is not None and total:
            pctv = used * 100.0 / total
        if pctv is None:
            continue
        level = 0
        tag = ""
        if over(t["flood_stage"], pctv, avail):
            level, tag = 3, "выше flood_stage %s: индексы на узле закрываются на запись" % t["flood_stage"]
        elif over(t["high"], pctv, avail):
            level, tag = 2, "выше high %s: шарды уводятся с узла" % t["high"]
        elif over(t["low"], pctv, avail):
            level, tag = 1, "выше low %s: новые шарды на узел не размещаются" % t["low"]
        elif pctv >= DISK_EARLY:
            level, tag = 1, "больше %d %%: до первого порога немного" % DISK_EARLY
        worst = max(worst, level)
        rows.append("%s: занято %.0f %% (%s из %s, свободно %s), шардов %s%s"
                    % (a.get("node"), pctv, hb(used), hb(total), hb(avail), a.get("shards"), ("; " + tag) if tag else ""))
    th_line = "пороги: low %s, high %s, flood_stage %s" % (t["low"], t["high"], t["flood_stage"])
    if worst >= 2:
        F.add("CRIT", "disk", "Диск узлов за порогом watermark", rows + [th_line],
              "освободить или расширить диск; при flood_stage после освобождения проверить блокировки индексов")
    elif worst == 1:
        F.add("WARN", "disk", "Диск узлов подходит к порогам", rows + [th_line], "освободить место заранее")
    elif rows:
        F.add("OK", "disk", "Диск в норме; " + th_line, rows)


def check_blocks(snap, F):
    bl = snap.get("blocks")
    if bl is None:
        return
    crit, warn = [], []
    for idx, body in sorted(bl.items()):
        s = (body or {}).get("settings") or {}
        flags = {k.split("index.blocks.", 1)[-1]: v for k, v in s.items() if str(v).lower() == "true"}
        if not flags:
            continue
        is_rx = bool(RX_INDEX.match(idx))
        line = "%s: %s%s" % (idx, ", ".join(sorted(flags)), " (индекс RX)" if is_rx else "")
        if "read_only_allow_delete" in flags or "read_only" in flags or ("write" in flags and is_rx):
            crit.append(line)
        else:
            warn.append(line)
    for scope in ("read_only", "read_only_allow_delete"):
        if str(setting(snap, "cluster.blocks." + scope, "false")).lower() == "true":
            crit.append("весь кластер: cluster.blocks.%s" % scope)
    if crit:
        F.add("CRIT", "blocks", "Индексы закрыты на запись: %d" % len(crit), crit[:20],
              "новые и изменённые документы не попадают в поиск, очередь индексирования в RabbitMQ растёт; "
              "сначала освободить диск, затем снять блокировку (команда в references/disk-blocks.md, только с согласия)")
    if warn:
        F.add("WARN", "blocks", "Блокировки на индексах: %d" % len(warn), warn[:20],
              "проверьте, поставлены ли они намеренно")
    if not crit and not warn:
        F.add("OK", "blocks", "Блокировок записи на индексах нет")


def check_heap(snap, F):
    stats = ((snap.get("stats") or {}).get("nodes") or {})
    info = ((snap.get("info") or {}).get("nodes") or {})
    ram = {n.get("name"): num(n.get("ram.max")) for n in snap.get("nodes") or []}
    hot, sized, trips, rej = [], [], [], []
    for nid, n in sorted(stats.items(), key=lambda kv: kv[1].get("name", "")):
        name = n.get("name", nid)
        mem = (n.get("jvm") or {}).get("mem") or {}
        hp, hmax = mem.get("heap_used_percent"), mem.get("heap_max_in_bytes")
        if hp is not None and hp >= HEAP_WARN:
            hot.append("%s: куча занята на %d %% из %s" % (name, hp, hb(hmax)))
        r = ram.get(name)
        if hmax and r and hmax > r * 0.55:
            sized.append("%s: куча %s при памяти %s: файловому кэшу мало" % (name, hb(hmax), hb(r)))
        if hmax and hmax > 31 * 1024 ** 3:
            sized.append("%s: куча %s больше 31 ГБ: Java теряет сжатые указатели" % (name, hb(hmax)))
        for bname, b in sorted((n.get("breakers") or {}).items()):
            if b.get("tripped"):
                trips.append("%s: %s сработал %d раз с запуска узла" % (name, bname, b["tripped"]))
            lim, est = b.get("limit_size_in_bytes"), b.get("estimated_size_in_bytes")
            if bname == "parent" and lim and est and est >= 0.9 * lim:
                hot.append("%s: общий предохранитель parent занят на %.0f %%" % (name, est * 100.0 / lim))
        tp = n.get("thread_pool") or {}
        for pool in ("write", "search"):
            x = tp.get(pool) or {}
            if x.get("rejected"):
                rej.append("%s: пул %s отклонил %d запросов с запуска узла, очередь сейчас %s"
                           % (name, pool, x["rejected"], x.get("queue")))
    for nid, n in info.items():
        jvm = n.get("jvm") or {}
        if str(jvm.get("using_compressed_ordinary_object_pointers", "true")).lower() == "false":
            sized.append("%s: сжатые указатели выключены (куча слишком велика)" % n.get("name", nid))
    if hot:
        F.add("WARN", "heap", "Куча JVM почти заполнена", hot,
              "один замер может прийтись на пик до сборки мусора: повторите через минуту; "
              "при стабильно высокой куче запросы начнут отклоняться (circuit_breaking_exception)")
    if trips:
        F.add("WARN", "heap", "Предохранители срабатывали", trips,
              "узел отклонял запросы из-за нехватки кучи: Data too large; размер кучи и число шардов, references/heap-shards.md")
    if rej:
        F.add("WARN", "heap", "Отклонённые запросы в пулах потоков", rej, "узел не успевал: нагрузка, медленный диск, мало процессора")
    if sized:
        F.add("WARN", "heap", "Размер кучи вне рекомендаций", sized,
              "куча до половины памяти и не больше 31 ГБ, -Xms равно -Xmx")
    if stats and not (hot or trips or rej or sized):
        mx = max([(n.get("jvm") or {}).get("mem", {}).get("heap_used_percent") or 0 for n in stats.values()] or [0])
        F.add("OK", "heap", "Куча в норме: максимум %d %%, предохранители не срабатывали, отклонений нет" % mx)
    locked = [n.get("name", k) for k, n in info.items() if (n.get("process") or {}).get("mlockall") is False]
    if locked:
        F.add("INFO", "heap", "Память не закреплена (bootstrap.memory_lock выключен): %s" % ", ".join(locked[:5]),
              [], "если на сервере включён своп, куча может уйти в подкачку, и поиск будет отвечать секундами")


def check_shards(snap, F):
    h = snap.get("health") or {}
    data = h.get("number_of_data_nodes") or 0
    limit = num(setting(snap, "cluster.max_shards_per_node", 1000)) or 1000
    total = (h.get("active_shards") or 0) + (h.get("unassigned_shards") or 0)
    cap = limit * max(data, 1)
    r = total / cap if cap else 0
    line = "шардов %d из лимита %d (%d на узел данных x %d), %.0f %%" % (total, cap, limit, max(data, 1), r * 100)
    if r >= SHARDS_CRIT:
        F.add("CRIT", "shards", "Шарды у лимита", [line], "новые индексы не создадутся: «maximum shards open»; "
              "уборка старых индексов, references/heap-shards.md")
    elif r >= SHARDS_WARN:
        F.add("WARN", "shards", "Шарды подходят к лимиту", [line], "убрать старые индексы, настроить срок хранения журналов")
    else:
        F.add("OK", "shards", line[0].upper() + line[1:])
    heap_total = 0
    for n in (snap.get("stats") or {}).get("nodes", {}).values():
        heap_total += ((n.get("jvm") or {}).get("mem") or {}).get("heap_max_in_bytes") or 0
    if heap_total:
        per_gb = total / (heap_total / 1024.0 ** 3)
        if per_gb > SHARDS_PER_GB_HEAP:
            F.add("WARN", "shards", "Шардов на гигабайт кучи: %.0f (ориентир до %d)" % (per_gb, SHARDS_PER_GB_HEAP),
                  ["куча всех узлов %s, шардов %d" % (hb(heap_total), total)],
                  "каждый шард держит память кучи: укрупнять мелкие индексы или добавить кучи")


def check_rx(snap, F):
    rx = rx_indices(snap)
    if not rx:
        F.add("INFO", "rx", "Индексов RX (rxsearch_*) не найдено",
              [], "поиск RX сюда не пишет, первоначальное индексирование не выполнялось или задан префикс; "
              "сверьте ELASTICSEARCH_URL, ELASTICSEARCH_INDEX_PREFIX и POSTFIX")
        return
    systems = {}
    lines, big, bad = [], [], []
    for i, m in rx:
        systems.setdefault(((m.group("prefix") or ""), m.group("rest")), []).append(i["index"])
        pri = num(i.get("pri")) or 1
        pss = num(i.get("pri.store.size"))
        docs = i.get("docs.count")
        lines.append("%s: %s, документов %s, размер %s, шардов %s+%s реплик" % (
            i["index"], i.get("health"), "?" if docs is None else docs, hb(num(i.get("store.size"))),
            i.get("pri"), i.get("rep")))
        if pss and pss / pri > SHARD_MAX_GB * 1024 ** 3:
            big.append("%s: %s на основной шард" % (i["index"], hb(pss / pri)))
        if i.get("health") == "red":
            bad.append(i["index"])
    F.add("INFO", "rx", "Индексы RX: %d, систем (префикс, код и постфикс): %d" % (len(rx), len(systems)), lines[:20])
    if bad:
        F.add("CRIT", "rx", "Красные индексы RX: %d" % len(bad), bad, "поиск по ним неполный или с ошибкой")
    if big:
        F.add("WARN", "rx", "Шарды индексов RX больше %d ГБ" % SHARD_MAX_GB, big,
              "справка RX советует индекс Lucene до 30 ГБ: number_of_shards = объём документов / 90 ГБ, "
              "меняется только с переиндексацией (admin_nastroyka_indeksov_elasticsearch_linux.htm)")
    others = [i for i in snap.get("indices") or [] if not RX_INDEX.match(i.get("index", "")) and not i.get("index", "").startswith(".")]
    if others:
        F.add("INFO", "rx", "Другие индексы в кластере: %d" % len(others),
              ["%s: документов %s, размер %s" % (i["index"], i.get("docs.count"), hb(num(i.get("store.size"))))
               for i in sorted(others, key=lambda x: -(num(x.get("store.size")) or 0))[:5]],
              "журналы и поиск RX в одном кластере: всплеск журналов заполнит диск, и поиск встанет вместе с ними")


def check_growth(prev, snap, F):
    dt = max(1.0, snap["taken_at"] - prev.get("taken_at", 0))
    before = {i["index"]: num(i.get("docs.count")) for i in prev.get("indices") or []}
    lines, seen = [], 0
    for i, m in rx_indices(snap):
        b = before.get(i["index"])
        c = num(i.get("docs.count"))
        if b is None or c is None:
            continue
        seen += 1
        if c != b:
            lines.append("%s: %+d документов (было %d, стало %d)" % (i["index"], c - b, b, c))
    if seen:
        F.add("INFO", "growth", ("Изменение числа документов в индексах RX за %.0f с" % dt) if lines else
              ("За %.0f с число документов в индексах RX не изменилось" % dt), lines,
              "если документы в RX меняются, а здесь ноль, смотрите очередь индексирования и журнал сервиса индексирования")


def analyze(snap, prev=None, anonymous=False):
    F = Findings()
    check_version(snap, F)
    check_plugins(snap, F)
    check_health(snap, F)
    check_disk(snap, F)
    check_blocks(snap, F)
    check_heap(snap, F)
    check_shards(snap, F)
    check_rx(snap, F)
    if prev is not None:
        check_growth(prev, snap, F)
    if anonymous:
        F.add("INFO", "security", "Elasticsearch ответил без пароля", [],
              "любой, кто дотянулся до порта 9200, читает и удаляет индексы: закройте порт сетью или прокси "
              "с TLS и паролем (рекомендация вендора), учётку задайте в ELASTICSEARCH_LOGIN и ELASTICSEARCH_PASSWORD")
    for n in snap.get("notes") or []:
        F.add("INFO", "notes", n)
    return F


# ---------------------------------------------------------------- вывод

def render_text(F, head):
    out = [head]
    order = {"CRIT": 0, "WARN": 1, "OK": 2, "INFO": 3}
    for i in sorted(F.items, key=lambda x: order.get(x["level"], 9)):
        out.append("%-4s  %s" % (i["level"], i["summary"]))
        for d in i["details"]:
            out.append("      " + d)
        if i["hint"] and i["level"] != "OK":
            out.append("      > " + i["hint"])
    crit = sum(1 for i in F.items if i["level"] == "CRIT")
    warn = sum(1 for i in F.items if i["level"] == "WARN")
    out.append("Итог: %s, проблем %d, предупреждений %d" % (F.worst(), crit, warn))
    return "\n".join(out)


def load_prev(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        raise CheckError("не читается %s: %s" % (path, e))
    if d.get("tool") != "es_check":
        raise CheckError("%s не похож на вывод es_check.py --json" % path)
    return d


def connect(args):
    url = args.url or os.environ.get("ES_URL") or "http://localhost:9200"
    p = urlsplit(url)
    if p.scheme not in ("http", "https"):
        raise CheckError("адрес %s: нужен http(s)://сервер:9200" % safe_url(url))
    user = args.user or os.environ.get("ES_USER") or p.username
    password = os.environ.get("ES_PASSWORD") or p.password
    if args.netrc:
        try:
            auth = netrc.netrc(args.netrc).authenticators(p.hostname or "")
        except (OSError, netrc.NetrcParseError) as e:
            raise CheckError("не читается %s: %s" % (args.netrc, e))
        if auth:
            user = user or auth[0]
            password = password or auth[2]
    if user and password is None:
        raise CheckError("задано имя, но нет пароля: ES_PASSWORD или --netrc ФАЙЛ")
    return safe_url(url), Api(safe_url(url), user, password, args.cacert, args.insecure, args.timeout)



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
    ap.add_argument("--url", help="адрес, по умолчанию $ES_URL или http://localhost:9200")
    ap.add_argument("--user", help="имя, по умолчанию $ES_USER")
    ap.add_argument("--netrc", metavar="ФАЙЛ", help="файл netrc: machine ХОСТ login ИМЯ password ПАРОЛЬ")
    ap.add_argument("--cacert", metavar="ФАЙЛ", default=os.environ.get("ES_CA"), help="корневой сертификат, по умолчанию $ES_CA")
    ap.add_argument("--insecure", action="store_true", help="не проверять сертификат (только осознанно)")
    ap.add_argument("--timeout", type=float, default=15.0, help="тайм-аут запроса, с")
    ap.add_argument("--interval", type=int, default=0, metavar="С", help="второй замер через С секунд: прирост документов")
    ap.add_argument("--compare", metavar="ФАЙЛ", help="сравнить с сохранённым выводом --json")
    ap.add_argument("--json", action="store_true", help="вывод в JSON")
    args = ap.parse_args(argv)
    try:
        target, api = connect(args)
        snap = collect(api.get)
        prev = None
        if args.compare:
            prev = load_prev(args.compare)
        elif args.interval:
            prev = snap
            time.sleep(args.interval)
            snap = collect(api.get)
        F = analyze(snap, prev, anonymous=not api.auth)
    except CheckError as e:
        if args.json:
            print(json.dumps({"tool": "es_check", "status": "ERROR", "error": str(e)}, ensure_ascii=False))
        else:
            print("ERROR  " + str(e))
        return 2
    head = "Elasticsearch: %s; замер %s%s" % (target, time.strftime("%Y-%m-%d %H:%M:%S"),
                                             ", сравнение с предыдущим" if prev else "")
    if args.insecure and target.startswith("https"):
        head += "; ВНИМАНИЕ: сертификат не проверялся (--insecure)"
    if args.json:
        print(json.dumps({"tool": "es_check", "tool_version": TOOL_VERSION, "target": target,
                          "taken_at": snap["taken_at"], "version": snap.get("version"), "status": F.worst(),
                          "findings": F.items, "indices": snap.get("indices") or []}, ensure_ascii=False, indent=1))
    else:
        print(render_text(F, head))
    return 0 if F.worst() == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
