#!/usr/bin/env python3
"""Сайзинг Directum RX: серверы и диски, рост хранилища документов, память PostgreSQL.

Порт формул открытых калькуляторов drxinfra.ru: те же входы, те же значения
по умолчанию и то же округление, что на страницах.
  servers   серверы, vCPU, память и диски по ролям для прода, теста и препрода
            (https://drxinfra.ru/sizing.html)
  storage   рост хранилища тел документов по годам, том с запасом, место под копии
            (https://drxinfra.ru/storage-growth.html)
  postgres  shared_buffers, work_mem, max_connections и другие расчётные
            параметры PostgreSQL (https://drxinfra.ru/postgresql-config.html)

Примеры:
  python3 sizing.py servers --users 300 --docs-year 60000 --doc-kb 500
  python3 sizing.py servers --preset big --no-test --json
  python3 sizing.py storage --docs 400 --size-mb 1,2 --years 5 --growth 10
  python3 sizing.py postgres --ram 32 --cpu 8 --users 250 --db-gb 120

Это оценка для бюджета и ТЗ, а не замена сайзингу по профилю нагрузки.
Ничего не читает с серверов и ничего не меняет. Код возврата: 0 посчитано,
2 неверные входные данные. Python 3.6+, только стандартная библиотека.
"""
import argparse
import io
import json
import math
import re
import sys

try:
    from urllib.parse import quote, unquote
except ImportError:  # pragma: no cover
    from urllib import quote, unquote

TOOL = "sizing.py"
VERSION = "0.1.0"

# ---------------------------------------------------------------- общие помощники


def js_round(x):
    """Math.round из JavaScript: половина округляется вверх, а не к чётному."""
    r = math.floor(x)
    return r + 1 if x - r >= 0.5 else r


def js_str(x):
    """Число так, как его печатает JavaScript: 20, 1.2, 0.30000000000000004."""
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, (int, float)) and float(x).is_integer() and abs(x) < 1e21:
        return str(int(x))
    return repr(float(x))


def clean_num(x):
    """Целые значения отдаём как int, чтобы в JSON было 20, а не 20.0."""
    if isinstance(x, float) and x.is_integer() and abs(x) < 1e15:
        return int(x)
    return x


def clean(obj):
    if isinstance(obj, dict):
        return dict((k, clean(v)) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    return clean_num(obj)


def up(v, step=1):
    return math.ceil(v / step - 1e-9) * step


def even(v):
    return up(v, 2)


def clamp(v, lo, hi):
    return min(hi, max(lo, v))


def to_int32(x):
    """Оператор x | 0 из JavaScript для разумных чисел: отбросить дробную часть."""
    return int(math.trunc(x))


def plural(n, one, few, many):
    a = abs(n) % 100
    b = a % 10
    if 10 < a < 20:
        return many
    if b == 1:
        return one
    if 2 <= b <= 4:
        return few
    return many


# ---------------------------------------------------------------- сайзинг (sizing.js)

VERSIONS = ["26.2", "26.1", "25.3", "25.2", "25.1", "4.12", "4.11", "4.10", "4.9"]

DEFAULTS = {
    "ver": "26.2",
    "users": 300, "active": 0, "mobile": 0,
    "docsYear": 60000, "docKB": 500, "years": 5, "historyGB": 0,
    "fts": 1, "dcs": 0, "ario": 0, "arioPages": 300,
    "os": "linux", "db": "pg", "pgKind": "pro",
    "place": "vm", "k8s": "ns", "ha": 0, "store": "s3",
    "monitoring": 1, "backup": 1, "logDays": 14,
    "test": 1, "preprod": 0, "preprodLikeProd": 0, "testInCluster": 1,
}

# Типовые профили со страницы калькулятора
PRESETS = {
    "small": {"users": 100, "docsYear": 15000, "store": "fs", "fts": 0},
    "mid": {"users": 500, "docsYear": 100000},
    "big": {"users": 2000, "docsYear": 400000, "mobile": 200, "ha": 1, "place": "k8s", "k8s": "ns"},
    "holding": {"users": 10000, "docsYear": 2000000, "mobile": 1000, "ha": 1, "place": "k8s", "k8s": "full",
                "preprod": 1, "preprodLikeProd": 1, "dcs": 1},
}


def fmt_gb(gb):
    """fmtGB из sizing.js: от 1000 ГБ в терабайтах с одним знаком."""
    if gb >= 1000:
        return js_str(js_round(gb / 100) / 10).replace(".", ",", 1) + " ТБ"
    return js_str(gb) + " ГБ"


def _node(role, n, cpu, ram, disk, sw, note, **extra):
    d = {"role": role, "n": n, "cpu": cpu, "ram": ram,
         "disk": [{"name": name, "gb": gb} for name, gb in disk], "sw": sw, "note": note}
    d.update(extra)
    return d


def size_contour(p, inp, kind):
    """Размер одного контура. p: U активных, T всего, M мобильных, storeGB, dbGB, ha, monitoring."""
    nodes, notes = [], []
    win = inp["os"] == "windows"
    k8s = inp["place"] == "k8s" and not win
    ha = bool(p["ha"])
    U, T, M = p["U"], p["T"], p["M"]
    os_disk = 100 if win else 60

    # --- приложение: веб-сервер, сервисы, воркеры RX ---
    app_cpu = even(3 + U / 45)
    app_ram = even(10 + U / 18)
    mobile_co = 0 < M < 100                      # NOMAD рядом с приложением
    if mobile_co:
        app_cpu += 2
        app_ram += 4
    rmq_co = not k8s and not ha and U <= 300     # RabbitMQ на узле приложения
    if rmq_co:
        app_cpu += 2
        app_ram += 4
    if win:
        app_ram += 4                             # ОС и антивирус

    app_log_day = max(0.3, js_round(U / 100 * 0.4 * 10) / 10)   # ГБ журналов приложения в день
    app_n = 0
    if not k8s:
        raw = max(1, math.ceil(app_cpu / 12), math.ceil(app_ram / 40))
        app_n = max(2, raw + 1) if ha else raw
        share = app_n - 1 if ha else app_n
        per_cpu, per_ram = even(app_cpu / share), even(app_ram / share)
        local_log = up(max(20, app_log_day * 7 / app_n), 10)
        nodes.append(_node(
            "Сервер приложений Directum RX", app_n, per_cpu, per_ram,
            [("ОС", os_disk), ("данные", 20), ("журналы 7 дней", local_log)],
            "Directum RX " + inp["ver"] + (", RabbitMQ" if rmq_co else "") + (", сервер NOMAD" if mobile_co else "")
            + (" (службы Windows)" if win else " (контейнеры)"),
            "при отказе одного узла остальные держат нагрузку" if ha else ""))

    # --- RabbitMQ ---
    rmq_cpu, rmq_ram = (2, 4) if U < 2000 else (4, 8)
    if not rmq_co and not k8s:
        nodes.append(_node(
            "RabbitMQ", 3 if ha else 1, rmq_cpu, rmq_ram, [("ОС", os_disk), ("очереди", 20)],
            "RabbitMQ" + (", кластер из трёх узлов, кворумные очереди" if ha else ""),
            "на этих же узлах можно держать арбитр переключения СУБД (etcd)" if ha and inp["db"] == "pg" else ""))

    # --- балансировщик (только ВМ + HA) ---
    if not k8s and ha:
        nodes.append(_node("Балансировщик и TLS", 2, 2, 4, [("ОС", 40)],
                           "nginx или HAProxy, keepalived (VIP), сертификаты",
                           "пара узлов с плавающим адресом; сюда же терминируется TLS"))

    # --- NOMAD (мобильные) ---
    if M >= 100:
        nomad_n = math.ceil(M / 1000) + (1 if ha else 0)
        if k8s:
            nodes.append(_node("Сервер NOMAD (в кластере)", nomad_n, 2, 4, [], "под NOMAD", "", k8s=True))
        else:
            nodes.append(_node("Сервер NOMAD", nomad_n, 4, 8, [("ОС", os_disk)], "сервер мобильных приложений", ""))

    # --- СУБД ---
    db_gb = p["dbGB"]
    db_cpu = even(max(4, U / 50))
    db_ram = clamp(even(max(8, U / 20 + db_gb / 25)), 8, 256)
    db_n = 2 if ha else 1
    if inp["db"] == "mssql":
        db_sw = "Microsoft SQL Server" + (", Always On (две реплики + свидетель)" if ha else "")
        db_note = "лицензии по ядрам, в расчёт не входят"
        if win:
            db_ram += 4
    else:
        db_sw = ("Postgres Pro" if inp["pgKind"] == "pro" else "PostgreSQL") + (", потоковая репликация, автопереключение" if ha else "")
        db_note = "реплика того же размера, что и основной узел" if ha else ""
    nodes.append(_node("СУБД", db_n, db_cpu, db_ram,
                       [("ОС", os_disk), ("данные", up(max(100, db_gb * 2), 50)), ("WAL / журнал", up(max(20, db_gb * 0.25), 10))],
                       db_sw, db_note))

    # --- тела документов ---
    store_gb = p["storeGB"]
    if inp["store"] == "s3":
        if ha:
            big = store_gb > 5000
            nodes.append(_node("Объектное хранилище S3", 4, 8 if big else 4, 32 if big else 16,
                               [("ОС", 50), ("данные (каждый узел)", up(store_gb * 2 / 4 * 1.1, 50))],
                               "MinIO, четыре узла, erasure coding", "полезный объём " + fmt_gb(store_gb) + ", сырой вдвое больше"))
        else:
            nodes.append(_node("Объектное хранилище S3", 1, 4 if store_gb > 2000 else 2, 16 if store_gb > 2000 else 8,
                               [("ОС", 50), ("данные", up(store_gb * 1.1, 50))], "MinIO, один узел", ""))
    else:
        shared = k8s or app_n > 1
        nodes.append(_node(
            "Файловое хранилище тел документов", 1 if shared else 0, 2 if shared else 0, 4 if shared else 0,
            [("тела документов", up(store_gb * 1.1, 50))],
            ("общая папка SMB или СХД" if win else "NFS или СХД, общий том для всех узлов") if shared else "том на сервере приложений",
            "один файловый сервер это точка отказа: лучше СХД с отказоустойчивостью или S3" if shared and ha else "",
            storageOnly=not shared))

    # --- полнотекстовый поиск ---
    if inp["fts"]:
        idx = up(max(20, store_gb * 0.1), 10)
        f_cpu = 4 if U < 500 else (8 if U < 3000 else 12)
        f_ram = 8 if idx < 50 else (16 if idx < 200 else 32)
        nodes.append(_node("Полнотекстовый поиск", 3 if ha else 1, f_cpu, f_ram,
                           [("ОС", 50), ("индексы", up(max(50, idx if ha else idx * 1.5), 10))],
                           "Elasticsearch или OpenSearch" + (", три узла, реплики индексов" if ha else ""),
                           "куча JVM не больше половины памяти узла"))

    # --- служба ввода ---
    if inp["dcs"]:
        nodes.append(_node("Служба ввода документов", 1, 4, 8, [("ОС", os_disk), ("буфер сканов", 100)],
                           "служба ввода (сканеры, почта, папки)", ""))

    # --- Ario ---
    if inp["ario"]:
        pages = max(50, p["arioPages"])
        a_cpu = max(4, math.ceil(pages / 80) + 2)
        a_n = math.ceil(a_cpu / 16)
        nodes.append(_node("Интеллектуальная обработка (Ario)", a_n, even(a_cpu / a_n), max(8, even(a_cpu / a_n * 1.5)),
                           [("ОС", os_disk), ("модели и очередь", 100)], "сервисы Ario",
                           "ориентир под " + js_str(pages) + " стр/ч; нужен быстрый процессор, точный расчёт по потоку"))

    # --- Kubernetes ---
    k8s_quota = None
    if k8s:
        q_cpu = app_cpu + (3 if ha else 1) * (1 if U < 2000 else 2)      # приложение + RabbitMQ
        q_ram = app_ram + (3 if ha else 1) * (2 if U < 2000 else 4)
        for n in nodes:
            if n.get("k8s"):
                q_cpu += n["n"] * n["cpu"]
                q_ram += n["n"] * n["ram"]
        k8s_quota = {
            "reqCpu": even(q_cpu), "reqRam": even(q_ram),
            "limCpu": even(q_cpu * 1.5), "limRam": even(q_ram * 1.3),
            "pv": 10 * (3 if ha else 1) + 20,
            "pods": "сервисы RX, RabbitMQ" + (", NOMAD" if M >= 100 else ""),
        }
        if inp["k8s"] == "full" and kind != "nsonly":
            extra = p.get("extraQuota") or {"cpu": 0, "ram": 0, "pv": 0}   # тест и препрод в этом же кластере
            plat = {"cpu": 5, "ram": 10, "pv": 70, "nodeCpu": 1.2, "nodeRam": 2}
            need_cpu = (k8s_quota["reqCpu"] + extra["cpu"] + plat["cpu"]) * 1.25
            need_ram = (k8s_quota["reqRam"] + extra["ram"] + plat["ram"]) * 1.25
            large = need_cpu > 48
            wsz = {"cpu": 16, "ram": 64} if large else {"cpu": 8, "ram": 32}
            cap_cpu = wsz["cpu"] - 1 - plat["nodeCpu"]
            cap_ram = wsz["ram"] - 2 - plat["nodeRam"]
            w_n = max(3 if ha else 2, math.ceil(need_cpu / cap_cpu), math.ceil(need_ram / cap_ram)) + (1 if ha else 0)
            pv_total = k8s_quota["pv"] + (extra.get("pv") or 0) + plat["pv"]
            lh_disk = up(max(100, pv_total * 3 / w_n * 1.5), 50)              # Longhorn: три реплики каждого тома
            m_n, m_big = (3 if ha else 1), w_n > 10
            nodes.append(_node(
                "Платформа кластера", 1, plat["cpu"], plat["ram"], [("тома Prometheus и служебные", plat["pv"])],
                "ingress-nginx, cert-manager, Flux, Prometheus, Alertmanager, Grafana, kube-state-metrics, metrics-server, CoreDNS",
                "плюс на каждом рабочем узле Vector, Longhorn, node-exporter и CNI: около " + js_str(plat["nodeCpu"])
                + " vCPU и " + js_str(plat["nodeRam"]) + " ГБ", k8s=True))
            w_note = ("с запасом на потерю одного узла" if ha else "") + \
                     ((("; " if ha else "") + "включая неймспейсы других контуров") if extra["cpu"] else "")
            nodes.insert(0, _node("Kubernetes: рабочие узлы", w_n, wsz["cpu"], wsz["ram"],
                                  [("ОС и образы", 100), ("Longhorn, тома ×3", lh_disk)],
                                  "kubelet, containerd, Longhorn, Vector, сервисы RX", w_note))
            nodes.insert(0, _node("Kubernetes: управляющие узлы", m_n, 4 if m_big else 2, 8 if m_big else 4,
                                  [("ОС", 50), ("etcd (SSD)", 20)], "kube-apiserver, etcd, планировщик",
                                  "три узла для кворума etcd, не совмещать с рабочими" if ha
                                  else "один узел: отказ управляющего слоя останавливает изменения, но не приложение"))

    # --- журналы и метрики: считаем всегда, узел ставим один на все контуры ---
    vm_count = sum(0 if (n.get("storageOnly") or n.get("k8s")) else n["n"] for n in nodes)
    log_day = max(0.5, js_round((app_log_day + vm_count * 0.1 + 0.3) * 10) / 10)   # приложение + системные + СУБД
    logs = {"perDay": log_day, "days": inp["logDays"], "gb": 0, "metricsGb": 0}
    if p["monitoring"]:
        others = 1 + (0.25 if inp["test"] else 0) + (0.5 if inp["preprod"] else 0)   # журналы теста и препрода тоже сюда
        logs["gb"] = up(log_day * others * inp["logDays"] * 1.3, 10)                   # ×1.3 на индексы
        logs["metricsGb"] = up(vm_count * others * 0.05 * 30 + 10, 10)                # 30 дней метрик
        heavy = log_day * others
        m_cpu = 4 if heavy < 5 else (8 if heavy < 20 else 12)
        m_ram = 8 if heavy < 5 else (16 if heavy < 20 else 32)
        disks = [("ОС", 50), ("журналы " + js_str(inp["logDays"]) + " дней", logs["gb"]), ("метрики 30 дней", logs["metricsGb"])]
        if inp["place"] == "k8s" and inp["k8s"] == "full" and not win:
            nodes.append(_node("Журналы и долгие метрики", 1, m_cpu, m_ram, disks,
                               "Elasticsearch или Loki, приём от Vector из кластера; Grafana и алерты живут в кластере",
                               "обслуживает все контуры"))
        else:
            nodes.append(_node("Мониторинг и журналы", 1, m_cpu, m_ram, disks,
                               "Prometheus, Grafana, Alertmanager, сбор журналов (ELK или Loki)",
                               "обслуживает все контуры; всегда на Linux"))

    # --- резервные копии ---
    backup_gb = 0
    if inp["backup"]:
        backup_gb = up(db_gb * 3 + store_gb + 50, 50) if kind == "prod" else up(db_gb + 20, 10)

    # --- итоги ---
    tot = {"vms": 0, "cpu": 0, "ram": 0, "disk": 0}
    for n in nodes:
        n["diskTotal"] = sum(d["gb"] for d in n["disk"])
        if n.get("k8s"):
            continue
        if not n.get("storageOnly"):
            tot["vms"] += n["n"]
            tot["cpu"] += n["n"] * n["cpu"]
            tot["ram"] += n["n"] * n["ram"]
        tot["disk"] += max(1, n["n"]) * n["diskTotal"]

    return {"kind": kind, "U": U, "T": T, "M": M, "ha": ha, "nodes": nodes, "k8sQuota": k8s_quota, "totals": tot,
            "backupGB": backup_gb, "dbGB": db_gb, "storeGB": store_gb, "logs": logs, "notes": notes}


def derive(inp):
    T = max(5, to_int32(inp["users"]))
    U = min(inp["active"], T) if inp["active"] > 0 else max(5, js_round(T * 0.3))
    docs_total = inp["docsYear"] * inp["years"] + (inp["historyGB"] * 1024 * 1024) / max(50, inp["docKB"])
    store_gb = up(max(20, (inp["docsYear"] * inp["years"] * inp["docKB"] / 1024 / 1024 + inp["historyGB"]) * 1.2), 10)
    db_gb = up(max(20, 20 + T * 0.05 + docs_total * 0.0001 + store_gb * 0.02), 10)
    return {"U": U, "T": T, "M": to_int32(inp["mobile"]), "storeGB": store_gb, "dbGB": db_gb, "docsTotal": js_round(docs_total)}


def compute(inp):
    d = derive(inp)
    win = inp["os"] == "windows"
    k8s = inp["place"] == "k8s" and not win
    contours = []
    same_cluster = k8s and inp["k8s"] == "full" and inp["testInCluster"]
    extra = {"cpu": 0, "ram": 0, "pv": 0}
    pp = tp = None
    if inp["preprod"]:
        pp = size_contour({"U": d["U"], "T": d["T"], "M": d["M"], "storeGB": d["storeGB"], "dbGB": d["dbGB"],
                           "ha": bool(inp["preprodLikeProd"] and inp["ha"]), "monitoring": False, "arioPages": inp["arioPages"]},
                          inp, "nsonly" if same_cluster else "preprod")
        pp["title"] = "Предпродуктивный контур"
        if same_cluster and pp["k8sQuota"]:
            extra["cpu"] += pp["k8sQuota"]["reqCpu"]
            extra["ram"] += pp["k8sQuota"]["reqRam"]
            extra["pv"] += pp["k8sQuota"]["pv"]
    if inp["test"]:
        t_u = max(10, js_round(d["U"] * 0.2))
        tp = size_contour({"U": t_u, "T": max(10, js_round(d["T"] * 0.2)), "M": min(d["M"], 20),
                           "storeGB": up(max(20, d["storeGB"] * 0.1), 10), "dbGB": up(max(20, d["dbGB"] * 0.2), 10),
                           "ha": False, "monitoring": False, "arioPages": min(inp["arioPages"], 100)},
                          inp, "nsonly" if same_cluster else "test")
        tp["title"] = "Тестовый контур"
        if same_cluster and tp["k8sQuota"]:
            extra["cpu"] += tp["k8sQuota"]["reqCpu"]
            extra["ram"] += tp["k8sQuota"]["reqRam"]
            extra["pv"] += tp["k8sQuota"]["pv"]

    prod = size_contour({"U": d["U"], "T": d["T"], "M": d["M"], "storeGB": d["storeGB"], "dbGB": d["dbGB"],
                         "ha": bool(inp["ha"]), "monitoring": bool(inp["monitoring"]), "arioPages": inp["arioPages"],
                         "extraQuota": extra}, inp, "prod")
    prod["title"] = "Продуктивный контур"
    contours.append(prod)
    if pp:
        contours.append(pp)
    if tp:
        contours.append(tp)

    grand = {"vms": 0, "cpu": 0, "ram": 0, "disk": 0, "backup": 0}
    for c in contours:
        for k in ("vms", "cpu", "ram", "disk"):
            grand[k] += c["totals"][k]
        grand["backup"] += c["backupGB"]
    return {"inp": inp, "d": d, "contours": contours, "grand": grand, "warnings": warnings(inp, d)}


def warnings(inp, d):
    w = []
    win = inp["os"] == "windows"
    k8s = inp["place"] == "k8s" and not win

    def add(lvl, t):
        w.append({"lvl": lvl, "text": t})

    if inp["ver"].startswith("4."):
        add("warn", "Версия " + inp["ver"] + " из линейки 4.x. Она поддерживается, но при новом внедрении или переезде разумнее сразу брать 25.x или 26.x: иначе обновление придётся планировать почти сразу после запуска.")
    if win and inp["db"] == "pg":
        add("info", "PostgreSQL на Windows работает, но хуже, чем на Linux: медленнее ввод-вывод, беднее инструменты копий и репликации. Обычно СУБД выносят на Linux даже в Windows-контуре.")
    if inp["db"] == "mssql":
        add("warn", "Microsoft SQL Server лицензируется по ядрам, и отказоустойчивая схема удваивает число лицензий. Стоимость лицензий в расчёт не входит, уточните у поставщика. PostgreSQL этой статьи расходов не имеет.")
    if win:
        add("warn", "Windows Server лицензируется по ядрам с минимумом на сервер плюс клиентские лицензии. В расчёт не входит. Сервисы RX на Windows работают как службы, контейнеры и Kubernetes для этого варианта не используются.")
    if inp["db"] == "pg" and inp["pgKind"] == "vanilla":
        add("info", "Ванильный PostgreSQL бесплатен, но не входит в реестр отечественного ПО. Если у вас есть требования по импортозамещению, потребуется Postgres Pro (платно, по ядрам или серверам).")
    if inp["db"] == "pg" and inp["pgKind"] == "pro":
        add("info", "Postgres Pro платный: лицензия по ядрам или серверам, в расчёт не входит. Взамен реестр отечественного ПО, поддержка вендора и инструменты копий и репликации из коробки.")
    if inp["place"] == "vm" and inp["ha"] and not win:
        add("warn", "Отказоустойчивость на виртуальных машинах это рабочая, но громоздкая схема: пара балансировщиков с плавающим адресом, сертификаты на них, кластер RabbitMQ из трёх узлов, реплика СУБД с арбитром, общее хранилище тел документов. Много ручных точек отказа. Если отказоустойчивость нужна всерьёз, посмотрите вариант с Kubernetes.")
    if win and inp["ha"]:
        add("warn", "Отказоустойчивость на Windows: балансировщик перед серверами приложений, кластер RabbitMQ из трёх узлов, Always On для MS SQL или реплика PostgreSQL. Каждый слой настраивается и проверяется отдельно.")
    if k8s and inp["k8s"] == "ns":
        add("info", "Считаем квоту неймспейса в вашем кластере. СУБД, объектное хранилище и полнотекстовый поиск оставлены на виртуальных машинах: их можно завести и в кластер, но только с быстрыми томами и своим оператором.")
    if k8s and inp["k8s"] == "full":
        add("info", "Кластер целиком: etcd на SSD, управляющие узлы отдельно от рабочих, динамический провижнер томов (Longhorn, CSI вашей СХД или NFS) и способ выдать внешний адрес ingress-контроллеру (MetalLB, keepalived или балансировщик заказчика).")
    if k8s and not inp["ha"]:
        add("info", "Kubernetes без отказоустойчивости: один управляющий узел и два рабочих. Отказ рабочего узла переживается, отказ управляющего оставляет приложение работать, но запрещает любые изменения до восстановления.")
    if inp["store"] == "fs" and (inp["ha"] or k8s):
        add("warn", "Тела документов на файловом хранилище при нескольких серверах приложений требуют общего тома (NFS, SMB, СХД). Такой том сам становится точкой отказа. Для отказоустойчивого контура рекомендуем S3.")
    if not inp["monitoring"]:
        add("warn", "Без мониторинга об аварии узнают пользователи. Это самая дешёвая виртуальная машина в расчёте, лучше её оставить.")
    if not inp["backup"]:
        add("warn", "Резервное копирование выключено: объём под копии не посчитан. Для продуктивного контура это не вариант.")
    if inp["backup"]:
        add("info", "Копии считаем как три объёма СУБД плюс один объём тел документов. Хранить их нужно вне контура, на другом хранилище, и раз в квартал разворачивать: копия, которую не восстанавливали, копией не считается.")
    if d["U"] > 3000:
        add("warn", "Больше трёх тысяч одновременно работающих пользователей. Здесь калькулятор даёт только порядок цифр: нужен профиль нагрузки по реальным сценариям и сайзинг по метрикам похожего контура.")
    if d["U"] < 20 and inp["ha"]:
        add("info", "Отказоустойчивость для контура такого размера обычно избыточна: проще быстрое восстановление из копии с проверенным регламентом.")
    if inp["historyGB"] > 0:
        add("info", "Исторические данные учтены в хранилище и в размере базы, но не в нагрузке: их импорт делается отдельной работой до запуска.")
    if inp["ario"]:
        add("info", "Ario посчитан ориентировочно по потоку страниц в час. Точный размер зависит от типов документов и качества сканов; при потоке от тысячи страниц в час считается отдельно.")
    if inp["mobile"] >= 100:
        add("info", "Сервер NOMAD публикуется наружу: нужен обратный прокси или балансировщик с внешним сертификатом и отдельный сегмент сети.")
    add("info", "Журналы считаем от активных пользователей и числа машин при обычном уровне логирования. Включённый Debug или Trace на сервисах RX увеличивает поток в разы: держите его только на время разбора.")
    if k8s and inp["k8s"] == "full":
        add("info", "В кластере учтён платформенный слой: ingress, cert-manager, Flux, Prometheus с Alertmanager и Grafana, Vector, Longhorn. Longhorn хранит три реплики каждого тома, поэтому локальные диски рабочих узлов больше суммы томов.")
    if inp["fts"]:
        add("info", "Полнотекстовый поиск индексирует тела документов: индекс примерно десятая часть их объёма. Первичная индексация большого архива занимает дни и грузит СУБД.")
    return w


def describe_inputs(res):
    i, d = res["inp"], res["d"]
    if i["os"] == "windows":
        place = "Windows Server, службы"
    elif i["place"] == "k8s":
        place = "Linux, Kubernetes (неймспейс в имеющемся кластере)" if i["k8s"] == "ns" else "Linux, Kubernetes (кластер целиком)"
    else:
        place = "Linux, контейнеры на ВМ"
    db = "Microsoft SQL Server" if i["db"] == "mssql" else ("Postgres Pro" if i["pgKind"] == "pro" else "PostgreSQL")
    yes = lambda x: "да" if x else "нет"  # noqa: E731
    return [
        ["Версия Directum RX", i["ver"]],
        ["Пользователей всего", d["T"]],
        ["Одновременно активных", js_str(d["U"]) + ("" if i["active"] > 0 else " (30% от общего числа)")],
        ["Мобильных пользователей", d["M"]],
        ["Документов в год", i["docsYear"]],
        ["Средний размер документа, КБ", i["docKB"]],
        ["Горизонт, лет", i["years"]],
        ["Исторические данные, ГБ", i["historyGB"]],
        ["Тела документов через " + js_str(i["years"]) + " лет", fmt_gb(d["storeGB"])],
        ["База данных через " + js_str(i["years"]) + " лет", fmt_gb(d["dbGB"])],
        ["Размещение", place],
        ["СУБД", db],
        ["Хранилище тел документов", "S3 (MinIO)" if i["store"] == "s3" else "файловое"],
        ["Отказоустойчивость", yes(i["ha"])],
        ["Полнотекстовый поиск", yes(i["fts"])],
        ["Служба ввода", yes(i["dcs"])],
        ["Ario", ("да, " + js_str(i["arioPages"]) + " стр/ч") if i["ario"] else "нет"],
        ["Мониторинг", yes(i["monitoring"])],
        ["Хранение журналов, дней", i["logDays"]],
        ["Журналы в день (прод)", js_str(res["contours"][0]["logs"]["perDay"]) + " ГБ"],
        ["Резервное копирование", yes(i["backup"])],
        ["Контуры", ", ".join(x for x in ["прод", "препрод" if i["preprod"] else "", "тест" if i["test"] else ""] if x)],
    ]


# ---------------------------------------------------------------- рост хранилища (storage.js)

STORAGE_DEFAULTS = {"docs": 400, "size": 1.2, "vers": 1.5, "pdf": 30, "days": 247, "years": 5, "growth": 10,
                    "have": 0, "reserve": 25, "redund": 1, "copies": 2}


def storage_read(raw):
    """Как read() на странице: отрицательное или нечисловое значение заменяется значением
    по умолчанию, доля PDF до 100 %, запас до 90 %, дней до 366, срок от 1 до 30 лет."""
    v = dict(STORAGE_DEFAULTS)
    for k, x in raw.items():
        if x is None:
            continue
        try:
            x = float(str(x).replace(",", ".", 1))
        except ValueError:
            continue
        if math.isfinite(x) and x >= 0:
            v[k] = x
    v["pdf"] = min(100, v["pdf"])
    v["reserve"] = min(90, v["reserve"])
    v["days"] = min(366, v["days"])
    v["years"] = max(1, min(30, js_round(v["years"])))
    v["redund"] = max(1, v["redund"])
    v["copies"] = js_round(v["copies"])
    return v


def storage_calc(v):
    r = {"years": []}
    cum = v["have"]
    per_doc = v["size"] * (v["vers"] + v["pdf"] / 100)      # МБ на документ с версиями и представлением
    for y in range(1, int(v["years"]) + 1):
        docs_day = v["docs"] * math.pow(1 + v["growth"] / 100, y - 1)
        add_gb = docs_day * per_doc * v["days"] / 1024
        cum += add_gb
        r["years"].append({"y": y, "docs": docs_day * v["days"], "add": add_gb, "cum": cum})
    r["dayGB"] = v["docs"] * per_doc / 1024
    r["firstYear"] = r["years"][0]["add"]
    r["data"] = cum
    r["disk"] = cum * v["redund"] / (1 - v["reserve"] / 100)
    r["backup"] = cum * v["copies"]
    r["lastDayGB"] = r["dayGB"] * math.pow(1 + v["growth"] / 100, v["years"] - 1)
    return r


def _num(x, d):
    """num() из storage.js: округлить до d знаков и поставить запятую."""
    p = math.pow(10, d)
    return js_str(js_round(x * p) / p).replace(".", ",", 1)


def vol(gb):
    """ГБ → «870 ГБ» или «1,4 ТБ» (здесь, как на странице роста хранилища, ТБ = 1024 ГБ)."""
    if gb >= 1024:
        return _num(gb / 1024, 0 if gb >= 10240 else 1) + "\u00a0ТБ"
    if gb >= 10:
        return _num(gb, 0) + "\u00a0ГБ"
    return _num(gb, 1) + "\u00a0ГБ"


def int_fmt(x):
    s = js_str(js_round(x))
    neg = s.startswith("-")
    s = s.lstrip("-")
    out = ""
    while len(s) > 3:
        out = " " + s[-3:] + out
        s = s[:-3]
    return ("-" if neg else "") + s + out


def storage_text(v, r):
    nb = "\u00a0"
    return ("При " + int_fmt(v["docs"]) + " новых документах в рабочий день, среднем файле " + _num(v["size"], 2) + nb + "МБ, "
            + _num(v["vers"], 1) + " версии на документ"
            + ((" и PDF-представлении у " + _num(v["pdf"], 0) + nb + "% документов") if v["pdf"] else "")
            + " хранилище растёт на " + vol(r["dayGB"]) + " в день и на " + vol(r["firstYear"]) + " за первый год. "
            + "Через " + js_str(v["years"]) + nb + plural(v["years"], "год", "года", "лет")
            + ((" при росте потока на " + _num(v["growth"], 0) + nb + "% в год") if v["growth"] else "")
            + " данных будет " + vol(r["data"]) + ((" с учётом уже накопленных " + vol(v["have"])) if v["have"] else "") + ". "
            + "Под них нужен том " + vol(r["disk"]) + ((" с учётом избыточности " + _num(v["redund"], 2) + "×") if v["redund"] > 1 else "")
            + " и запаса " + _num(v["reserve"], 0) + nb + "% свободного места"
            + ((", под резервные копии ещё " + vol(r["backup"]) + " (" + js_str(v["copies"]) + nb
                + plural(v["copies"], "полная копия", "полные копии", "полных копий") + ")") if v["copies"] else "")
            + ".")


def storage_notes(v, r):
    nb = "\u00a0"
    n = []
    if v["years"] < 3:
        n.append(("warn", "Документы хранятся годами и почти не удаляются. Том и бэкап считают хотя бы на три года вперёд, иначе расширять придётся почти сразу."))
    if v["reserve"] < 20:
        n.append(("warn", "Запаса меньше 20" + nb + "%: тревога на 80 % заполнения сработает, когда расширять том уже поздно."))
    if v["redund"] <= 1:
        n.append(("info", "Избыточность 1: при отказе диска документы восстанавливают из бэкапа, и всё, что появилось после него, теряется. Для своего хранилища закладывают RAID или erasure coding, у облачного S3 избыточность на стороне провайдера."))
    if v["copies"] < 2:
        n.append(("warn", "Меньше двух полных копий: если единственная повреждена или устарела, восстанавливаться не из чего. Держат минимум две, одну вне площадки."))
    if r["lastDayGB"] >= 20:
        n.append(("info", "Прирост к концу срока " + vol(r["lastDayGB"]) + " в день. На таком потоке файловый каталог на одном сервере упирается в диск и во время бэкапа, обычно переходят на объектное хранилище S3."))
    if v["size"] >= 5:
        n.append(("info", "Средний файл больше 5" + nb + "МБ похож на сканы в цвете или высоком разрешении. Если сканируете в цвете там, где хватит оттенков серого, объём падает в разы."))
    return n


# ---------------------------------------------------------------- PostgreSQL (pgconf.js)

PG_DEFAULTS = {"ram": 16, "cpu": 4, "disk": "ssd", "place": "own", "docker": 0, "users": 100, "dbgb": 50,
               "replica": 0, "archive": 0}


def _or_default(x, default):
    """Выражение +x || DEF из JavaScript: ноль, пусто и нечисловое значат «по умолчанию»."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return default
    return x if (x and not math.isnan(x)) else default


def pg_read(raw):
    """Как read() на странице: память не меньше 2 ГБ, ядер и пользователей не меньше одного."""
    v = dict(PG_DEFAULTS)
    v.update(dict((k, x) for k, x in raw.items() if x is not None))
    v["ram"] = max(2, _or_default(v["ram"], PG_DEFAULTS["ram"]))
    v["cpu"] = max(1, js_round(_or_default(v["cpu"], PG_DEFAULTS["cpu"])))
    v["users"] = max(1, js_round(_or_default(v["users"], PG_DEFAULTS["users"])))
    v["dbgb"] = max(1, _or_default(v["dbgb"], PG_DEFAULTS["dbgb"]))
    if v["disk"] not in ("ssd", "net", "hdd"):
        v["disk"] = PG_DEFAULTS["disk"]
    if v["place"] not in ("own", "shared"):
        v["place"] = PG_DEFAULTS["place"]
    for k in ("docker", "replica", "archive"):
        v[k] = 1 if v[k] else 0
    return v


def _round_to(x, step):
    return max(step, js_round(x / step) * step)


def _pow2(x, lo, hi):
    p = lo
    while p * 2 <= hi and p * 1.5 <= x:
        p *= 2
    return p


def pg_calc(v):
    r = {}
    ram_mb = v["ram"] * 1024
    # база на общем сервере получает треть памяти: остальное нужно сервисам RX, брокеру и поиску
    r["memDB"] = ram_mb * 0.35 if v["place"] == "shared" else ram_mb
    sb = r["memDB"] * 0.25
    sb = _round_to(sb, 256) if sb >= 1024 else _round_to(sb, 64)
    r["sb"] = clamp(sb, 128, 32 * 1024)
    r["ecs"] = _round_to(r["memDB"] * 0.7, 1024 if r["memDB"] >= 4096 else 128)
    r["conn"] = clamp(math.ceil((v["users"] * 1.5 + 100) / 50) * 50, 100, 1000)
    r["wm"] = _pow2((r["memDB"] - r["sb"]) * 0.5 / r["conn"], 4, 64)
    r["mwm"] = clamp(_round_to(r["memDB"] / 16, 64), 64, 2048)
    r["maxWal"] = 4 if v["disk"] == "hdd" else (4 if v["dbgb"] < 50 else (8 if v["dbgb"] < 200 else 16))
    r["minWal"] = max(1, r["maxWal"] / 4)
    r["ckpt"] = "15min" if r["maxWal"] >= 16 else "10min"
    r["rpc"] = "1.1" if v["disk"] == "ssd" else ("1.2" if v["disk"] == "net" else "4")
    r["eio"] = 200 if v["disk"] == "ssd" else (100 if v["disk"] == "net" else 2)
    r["workers"] = max(8, v["cpu"])
    r["par"] = clamp(v["cpu"], 2, 16)
    r["gather"] = 2 if v["cpu"] >= 4 else (1 if v["cpu"] >= 2 else 0)
    r["pmaint"] = 2 if v["cpu"] >= 4 else 1
    r["avw"] = 4 if v["cpu"] >= 8 else 3
    r["huge"] = r["sb"] >= 8192 and not v["docker"]
    r["hugePages"] = math.ceil(r["sb"] * 1.1 / 2)
    # под нагрузкой: общие буферы, 10 МБ на соединение и work_mem у четверти соединений разом
    r["peak"] = r["sb"] + r["conn"] * 10 + r["conn"] * r["wm"] * 0.25
    return r


def pg_mem(mb):
    """Память как в postgresql.conf: целые гигабайты через GB, остальное через MB."""
    mb = js_round(mb)
    return js_str(mb // 1024) + "GB" if mb >= 1024 and mb % 1024 == 0 else js_str(mb) + "MB"


def pg_human(mb):
    if mb >= 1024:
        return (js_str(js_round(mb / 102.4) / 10) + " ГБ").replace(".", ",", 1)
    return js_str(js_round(mb)) + " МБ"


def pg_container_limit_gb(r):
    """Нижняя граница лимита памяти контейнера из фрагмента калькулятора: пик плюс 1 ГБ."""
    return math.ceil((r["peak"] + 1024) / 1024)


def pg_notes(v, r):
    n = []
    if r["peak"] > r["memDB"] * 0.9:
        n.append(("warn", "Под нагрузкой база займёт около " + pg_human(r["peak"]) + " из " + pg_human(r["memDB"]) + ", запаса почти нет. Добавьте памяти или поставьте пул соединений."))
    if v["ram"] < 8 and v["place"] == "shared":
        n.append(("warn", "Меньше 8 ГБ на базу вместе с RX мало: сервисы и база будут вытеснять друг друга."))
    if r["conn"] > 600:
        n.append(("warn", "Больше 600 соединений: поставьте PgBouncer перед базой вместо роста лимита."))
    if v["disk"] == "hdd":
        n.append(("warn", "HDD под базой RX это главный тормоз системы. Перенос на SSD даёт больше, чем любая настройка."))
    if v["dbgb"] * 1024 > r["memDB"] * 4:
        n.append(("info", "База заметно больше памяти. Следите за долей чтения с диска: возможно, памяти нужно больше."))
    if v["place"] == "shared":
        n.append(("info", "База делит сервер с RX. При росте нагрузки первым шагом вынесите её на отдельный сервер."))
    if v["docker"]:
        n.append(("info", "Контейнеру нужен ключ --shm-size и лимит памяти с запасом, см. конец фрагмента."))
    if r["huge"]:
        n.append(("info", "При таком shared_buffers стоит включить huge pages, значение для sysctl есть во фрагменте."))
    if not v["replica"] and not v["archive"]:
        n.append(("info", "Нет ни реплики, ни архива журнала: восстановиться можно только на момент последнего бэкапа."))
    return n


def pg_settings(v, r):
    """Строки параметров для вывода; restart = нужен перезапуск базы."""
    s = [
        ("shared_buffers", pg_mem(r["sb"]), True),
        ("effective_cache_size", pg_mem(r["ecs"]), False),
        ("work_mem", pg_mem(r["wm"]), False),
        ("maintenance_work_mem", pg_mem(r["mwm"]), False),
        ("max_connections", js_str(r["conn"]), True),
    ]
    if r["huge"]:
        s.append(("huge_pages", "try", True))
    s += [
        ("max_wal_size", js_str(r["maxWal"]) + "GB", False),
        ("min_wal_size", js_str(r["minWal"]) + "GB", False),
        ("checkpoint_timeout", r["ckpt"], False),
        ("random_page_cost", r["rpc"], False),
        ("effective_io_concurrency", js_str(r["eio"]), False),
        ("max_worker_processes", js_str(r["workers"]), True),
        ("max_parallel_workers", js_str(r["par"]), False),
        ("max_parallel_workers_per_gather", js_str(r["gather"]), False),
        ("max_parallel_maintenance_workers", js_str(r["pmaint"]), False),
        ("autovacuum_max_workers", js_str(r["avw"]), True),
    ]
    return s


# ---------------------------------------------------------------- ссылки на расчёт с сайта

_FLOAT_PREFIX = re.compile(r"^\s*[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


def js_parse_float(text):
    """parseFloat из JavaScript: число из начала строки, иначе None (NaN)."""
    m = _FLOAT_PREFIX.match(text)
    return float(m.group(0)) if m else None


def js_number(text):
    """Унарный плюс из JavaScript: вся строка должна быть числом, пустая строка это 0."""
    t = text.strip()
    if not t:
        return 0.0
    try:
        x = float(t)
    except ValueError:
        return None
    return None if math.isnan(x) else x


def _url_part(url, sep):
    if sep == "#":
        return url.split("#", 1)[1] if "#" in url else (url.split("?", 1)[1] if "?" in url else url)
    part = url.split("#", 1)[0]
    return part.split("?", 1)[1] if "?" in part else part


def decode_servers_url(url):
    """decode() из sizing.js: параметры после # в ссылке «Скопировать ссылку на расчёт»."""
    out = {}
    for kv in _url_part(url, "#").split("&"):
        parts = kv.split("=")
        if len(parts) != 2 or parts[0] not in DEFAULTS:
            continue
        val = unquote(parts[1])
        if isinstance(DEFAULTS[parts[0]], str):
            out[parts[0]] = val
        else:
            x = js_parse_float(val)
            out[parts[0]] = x if x else 0.0
    if "ver" in out and out["ver"] not in VERSIONS:
        out["ver"] = DEFAULTS["ver"]
    return out


def decode_storage_url(url):
    """fromUrl() из storage.js: параметры после ? в адресе страницы роста хранилища."""
    out = {}
    for kv in _url_part(url, "?").split("&"):
        parts = kv.split("=")
        if parts[0] not in STORAGE_DEFAULTS or len(parts) < 2:
            continue
        x = js_parse_float(unquote(parts[1]))
        if x is not None and math.isfinite(x) and x >= 0:
            out[parts[0]] = x
    return out


def decode_postgres_url(url):
    """fromUrl() из pgconf.js: параметры после ? в адресе калькулятора PostgreSQL."""
    out = {}
    for kv in _url_part(url, "?").split("&"):
        parts = kv.split("=")
        key = parts[0]
        if (key not in PG_DEFAULTS and key != "ver") or len(parts) < 2:
            continue
        val = unquote(parts[1])
        if key in ("disk", "place"):
            out[key] = val if re.match(r"^(ssd|net|hdd|own|shared)$", val) else PG_DEFAULTS[key]
        elif key != "ver":
            out[key] = js_number(val) or 0
    return out


def _encode(values, defaults):
    """encode() со страниц: только значения, отличные от умолчаний, как key=value через &."""
    parts = []
    for k in defaults:
        if k not in values:
            continue
        a = values[k] if isinstance(values[k], str) else js_str(values[k])
        b = defaults[k] if isinstance(defaults[k], str) else js_str(defaults[k])
        if a != b:
            parts.append(k + "=" + quote(a, safe="-_.!~*'()"))
    return "&".join(parts)


def servers_url(inp):
    h = _encode(inp, DEFAULTS)
    return "https://drxinfra.ru/sizing.html" + ("#" + h if h else "")


def storage_url(v):
    q = _encode(v, STORAGE_DEFAULTS)
    return "https://drxinfra.ru/storage-growth.html" + ("?" + q if q else "")


def postgres_url(v):
    q = _encode(v, PG_DEFAULTS)
    return "https://drxinfra.ru/postgresql-config.html" + ("?" + q if q else "")


# ---------------------------------------------------------------- вывод


def _table(rows, aligns):
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    out = []
    for r in rows:
        cells = []
        for i, c in enumerate(r):
            c = c if isinstance(c, str) else js_str(c)
            cells.append(c.rjust(widths[i]) if aligns[i] == "r" else c.ljust(widths[i]))
        out.append("  " + "  ".join(cells).rstrip())
    return out


def render_servers(res, details):
    L = []
    d, prod = res["d"], res["contours"][0]
    L.append("Сайзинг Directum RX %s: оценка по модели https://drxinfra.ru/sizing.html" % res["inp"]["ver"])
    L.append("Активных %s из %s, тела документов через %s лет %s, база %s, журналы %s ГБ/день в проде" % (
        js_str(d["U"]), d["T"], js_str(res["inp"]["years"]), fmt_gb(d["storeGB"]), fmt_gb(d["dbGB"]),
        js_str(prod["logs"]["perDay"]).replace(".", ",")))
    for c in res["contours"]:
        t = c["totals"]
        L.append("")
        L.append("%s%s: %d ВМ, %d vCPU, %d ГБ ОЗУ, диски %s" % (c["title"], " (отказоустойчивый)" if c["ha"] else "",
                                                             t["vms"], t["cpu"], t["ram"], fmt_gb(t["disk"])))
        rows = [("Роль", "Шт.", "vCPU", "ОЗУ, ГБ", "Диски на узел, ГБ")]
        extra = []
        for n in c["nodes"]:
            disks = ", ".join("%s %s" % (x["name"], js_str(x["gb"])) for x in n["disk"])
            if len(n["disk"]) > 1:
                disks += " = " + js_str(n["diskTotal"])
            if n.get("k8s"):
                extra.append(n)
                continue
            if n.get("storageOnly"):
                rows.append((n["role"], "том", "-", "-", disks))
            else:
                rows.append((n["role"], n["n"], n["cpu"], n["ram"], disks))
        L += _table(rows, "lrrrl")
        if c["k8sQuota"]:
            q = c["k8sQuota"]
            L.append("  Квота неймспейса: requests %s vCPU / %s ГБ, limits %s vCPU / %s ГБ, тома %s ГБ (%s)" % (
                q["reqCpu"], q["reqRam"], q["limCpu"], q["limRam"], q["pv"], q["pods"]))
        for n in extra:
            L.append("  В кластере: %s: %s%s vCPU / %s ГБ" % (n["role"], ("%s × " % n["n"]) if n["n"] > 1 else "", js_str(n["cpu"]), js_str(n["ram"])))
        if c["backupGB"]:
            L.append("  Резервные копии вне контура: %s (%s)" % (
                fmt_gb(c["backupGB"]), "СУБД ×3, тела документов ×1" if c["kind"] == "prod" else "одна копия базы"))
        if details:
            for n in c["nodes"]:
                L.append("    %s: %s%s" % (n["role"], n["sw"], (". " + n["note"]) if n["note"] else ""))
    if len(res["contours"]) > 1:
        g = res["grand"]
        L.append("")
        L.append("Все контуры: %d ВМ, %d vCPU, %d ГБ ОЗУ, диски %s%s" % (
            g["vms"], g["cpu"], g["ram"], fmt_gb(g["disk"]), (", копии ещё " + fmt_gb(g["backup"])) if g["backup"] else ""))
    warn = [w for w in res["warnings"] if w["lvl"] == "warn"]
    info = [w for w in res["warnings"] if w["lvl"] != "warn"]
    if warn:
        L.append("")
        L.append("Важно:")
        L += ["  - " + w["text"] for w in warn]
    if info and details:
        L.append("")
        L.append("К сведению:")
        L += ["  - " + w["text"] for w in info]
    elif info:
        L.append("")
        L.append("Ещё %d %s к сведению: добавьте --details." % (len(info), plural(len(info), "замечание", "замечания", "замечаний")))
    L.append("")
    L.append("Тот же расчёт в браузере: " + servers_url(res["inp"]))
    if details:
        L.append("")
        L.append("Параметры расчёта:")
        L += ["  %s: %s" % (k, v if isinstance(v, str) else js_str(v)) for k, v in describe_inputs(res)]
    return "\n".join(L)


def render_storage(v, r, notes):
    L = ["Рост хранилища документов Directum RX: оценка по модели https://drxinfra.ru/storage-growth.html"]
    L += _table([
        ("Прирост в день", vol(r["dayGB"])),
        ("За первый год", vol(r["firstYear"])),
        ("Данных через %s %s" % (js_str(v["years"]), plural(v["years"], "год", "года", "лет")), vol(r["data"])),
        ("Том под хранилище", vol(r["disk"])),
        ("Под резервные копии", vol(r["backup"]) if v["copies"] else "-"),
    ], "ll")
    L.append("")
    rows = [("Год", "Документов за год", "Прирост", "Накоплено")]
    rows += [("%d-й" % x["y"], int_fmt(x["docs"]), vol(x["add"]), vol(x["cum"])) for x in r["years"]]
    L += _table(rows, "lrrr")
    L.append("")
    L.append(storage_text(v, r))
    L.append("Тот же расчёт в браузере: " + storage_url(v))
    if notes:
        L.append("")
        L.append("Замечания:")
        L += ["  - %s%s" % ("важно: " if lvl == "warn" else "", t) for lvl, t in notes]
    return "\n".join(L).replace("\u00a0", " ")


def render_postgres(v, r, notes):
    L = ["Расчётные параметры PostgreSQL под Directum RX: модель https://drxinfra.ru/postgresql-config.html"]
    L.append("Сервер: %s ГБ, ядер %s, диск %s, %s%s; одновременно %s чел., база %s ГБ" % (
        js_str(v["ram"]), v["cpu"], v["disk"], "только база" if v["place"] == "own" else "база вместе с RX",
        ", в контейнере" if v["docker"] else "", v["users"], js_str(v["dbgb"])))
    L += _table([("Памяти базе", pg_human(r["memDB"])), ("Под нагрузкой", pg_human(r["peak"]))], "ll")
    L.append("")
    rows = [(k, val, "перезапуск" if rs else "") for k, val, rs in pg_settings(v, r)]
    rows.append(("max_prepared_transactions", js_str(r["conn"]), "перезапуск; по справке RX равен max_connections"))
    L += _table(rows, "lll")
    if r["huge"]:
        L.append("  vm.nr_hugepages = %s (sysctl), потом проверить HugePages_Rsvd в /proc/meminfo" % r["hugePages"])
    if v["docker"]:
        L.append("  Контейнер: запуск с --shm-size=1g (в compose: shm_size: 1gb), иначе параллельные запросы падают;")
        L.append("  лимит памяти контейнера не меньше %d ГБ, данные на томе" % pg_container_limit_gb(r))
    L.append("  Полный фрагмент postgresql.conf с журналом, autovacuum и логированием: " + postgres_url(v))
    if notes:
        L.append("")
        L.append("Замечания:")
        L += ["  - %s%s" % ("важно: " if lvl == "warn" else "", t) for lvl, t in notes]
    return "\n".join(L)


# ---------------------------------------------------------------- аргументы


class InputError(Exception):
    pass


def num(text):
    """Число из аргумента: 1,2 и 1.2 одинаково; отрицательные и нечисловые отвергаем."""
    try:
        x = float(str(text).strip().replace(",", ".", 1))
    except ValueError:
        raise argparse.ArgumentTypeError("нужно число: %r" % text)
    if not math.isfinite(x) or x < 0:
        raise argparse.ArgumentTypeError("нужно неотрицательное число: %r" % text)
    return x


def pos(text):
    x = num(text)
    if x <= 0:
        raise argparse.ArgumentTypeError("нужно положительное число: %r" % text)
    return x


def flag_pair(p, name, dest, help_on, help_off):
    p.add_argument("--" + name, dest=dest, action="store_const", const=1, help=help_on)
    p.add_argument("--no-" + name, dest=dest, action="store_const", const=0, help=help_off)


def build_parser():
    ap = argparse.ArgumentParser(
        prog="sizing.py", description=__doc__.split("\n\n")[0],
        epilog="Подробности по подкоманде: sizing.py servers --help. Код возврата: 0 посчитано, 2 неверные входные данные.")
    sub = ap.add_subparsers(dest="cmd", metavar="{servers,storage,postgres}")

    s = sub.add_parser("servers", help="серверы и диски по ролям (sizing.html)",
                       description="Серверы, vCPU, память и диски по ролям для продуктивного, тестового и предпродуктивного контуров. "
                                   "Значения по умолчанию как на странице калькулятора: 300 пользователей, 60 000 документов в год по 500 КБ, 5 лет, "
                                   "Linux, Postgres Pro, контейнеры на ВМ, S3, поиск, мониторинг, копии, тестовый контур.")
    s.add_argument("--from-url", dest="url", metavar="URL",
                   help="ссылка с https://drxinfra.ru/sizing.html (кнопка «Скопировать ссылку на расчёт»); остальные ключи её дополняют")
    s.add_argument("--preset", choices=sorted(PRESETS), help="типовой профиль со страницы: small (100 чел.), mid (500), big (2000, k8s), holding (10 000)")
    s.add_argument("--rx-version", dest="ver", choices=VERSIONS, help="версия Directum RX (по умолчанию 26.2)")
    s.add_argument("--users", type=num, metavar="N", help="пользователей всего (300)")
    s.add_argument("--active", type=num, metavar="N", help="одновременно работающих; 0 или не задано: 30%% от всех")
    s.add_argument("--mobile", type=num, metavar="N", help="пользователей мобильного приложения (0)")
    s.add_argument("--docs-year", dest="docsYear", type=num, metavar="N", help="документов в год (60000)")
    s.add_argument("--doc-kb", dest="docKB", type=num, metavar="N", help="средний размер документа, КБ (500)")
    s.add_argument("--years", type=pos, metavar="N", help="горизонт планирования, лет (5; на странице 3, 5 или 7)")
    s.add_argument("--history-gb", dest="historyGB", type=num, metavar="N", help="архив из старой системы, ГБ (0)")
    s.add_argument("--os", choices=["linux", "windows"], help="операционная система (linux)")
    s.add_argument("--db", choices=["pg", "mssql"], help="СУБД (pg)")
    s.add_argument("--pg-kind", dest="pgKind", choices=["pro", "vanilla"], help="Postgres Pro или ванильный PostgreSQL (pro)")
    s.add_argument("--place", choices=["vm", "k8s"], help="приложение в контейнерах на ВМ или в Kubernetes (vm)")
    s.add_argument("--k8s", choices=["ns", "full"], help="ns: квота неймспейса в имеющемся кластере; full: кластер целиком (ns)")
    s.add_argument("--store", choices=["s3", "fs"], help="тела документов в S3 или в файловом хранилище (s3)")
    flag_pair(s, "ha", "ha", "отказоустойчивость", "без отказоустойчивости (по умолчанию)")
    flag_pair(s, "fts", "fts", "полнотекстовый поиск (по умолчанию)", "без полнотекстового поиска")
    flag_pair(s, "dcs", "dcs", "служба ввода документов", "без службы ввода (по умолчанию)")
    flag_pair(s, "ario", "ario", "Ario", "без Ario (по умолчанию)")
    s.add_argument("--ario-pages", dest="arioPages", type=num, metavar="N", help="поток страниц в час для Ario (300)")
    flag_pair(s, "monitoring", "monitoring", "мониторинг и журналы (по умолчанию)", "без мониторинга")
    s.add_argument("--log-days", dest="logDays", type=pos, metavar="N", help="сколько дней хранить журналы (14; на странице 14, 30 или 90)")
    flag_pair(s, "backup", "backup", "место под резервные копии (по умолчанию)", "не считать копии")
    flag_pair(s, "test", "test", "тестовый контур (по умолчанию)", "без тестового контура")
    flag_pair(s, "preprod", "preprod", "предпродуктивный контур", "без препрода (по умолчанию)")
    flag_pair(s, "preprod-like-prod", "preprodLikeProd", "препрод с той же отказоустойчивостью, что прод", "препрод без отказоустойчивости (по умолчанию)")
    s.add_argument("--test-in-cluster", dest="testInCluster", action="store_const", const=1,
                   help="при --k8s full тест и препрод неймспейсами в том же кластере (по умолчанию)")
    s.add_argument("--test-own-cluster", dest="testInCluster", action="store_const", const=0,
                   help="при --k8s full тесту и препроду свой маленький кластер")
    s.add_argument("--details", action="store_true", help="в таблице ещё ПО, заметки по ролям, все замечания и параметры")
    s.add_argument("--json", action="store_true", help="результат в JSON")

    st = sub.add_parser("storage", help="рост хранилища документов по годам (storage-growth.html)",
                        description="В день: документов × размер × (версий + доля с PDF). Поток растёт на заданный процент в год. "
                                    "Том: данные × избыточность / (1 − запас). Копии: данные × число полных копий.")
    st.add_argument("--from-url", dest="url", metavar="URL", help="адрес страницы https://drxinfra.ru/storage-growth.html с параметрами после ?")
    st.add_argument("--docs", type=num, metavar="N", help="новых документов в рабочий день (400)")
    st.add_argument("--size-mb", dest="size", type=num, metavar="N", help="средний размер файла, МБ (1,2)")
    st.add_argument("--versions", dest="vers", type=num, metavar="N", help="версий на документ (1,5); каждая версия хранится целиком")
    st.add_argument("--pdf", type=num, metavar="N", help="доля документов с PDF-представлением, %% (30)")
    st.add_argument("--days", type=num, metavar="N", help="рабочих дней в году (247)")
    st.add_argument("--years", type=num, metavar="N", help="срок расчёта, лет (5), от 1 до 30")
    st.add_argument("--growth", type=num, metavar="N", help="рост потока в год, %% (10)")
    st.add_argument("--have-gb", dest="have", type=num, metavar="N", help="уже накоплено, ГБ (0)")
    st.add_argument("--reserve", type=num, metavar="N", help="держать свободным, %% (25)")
    st.add_argument("--redundancy", dest="redund", type=num, metavar="N", help="избыточность: RAID 1 или зеркало 2, MinIO 4+4 тоже 2, облачный S3 1 (1)")
    st.add_argument("--copies", type=num, metavar="N", help="полных копий в бэкапе (2)")
    st.add_argument("--json", action="store_true", help="результат в JSON")

    pg = sub.add_parser("postgres", help="память и соединения PostgreSQL (postgresql-config.html)",
                        description="shared_buffers, effective_cache_size, work_mem, maintenance_work_mem, max_connections, huge pages "
                                    "и остальные расчётные параметры калькулятора PostgreSQL под Directum RX.")
    pg.add_argument("--from-url", dest="url", metavar="URL", help="адрес страницы https://drxinfra.ru/postgresql-config.html с параметрами после ?")
    pg.add_argument("--ram", type=pos, metavar="N", help="память сервера базы или лимит контейнера, ГБ (16)")
    pg.add_argument("--cpu", type=pos, metavar="N", help="ядер (4)")
    pg.add_argument("--disk", choices=["ssd", "net", "hdd"], help="диск под данные: ssd, net (СХД или облачный диск), hdd (ssd)")
    pg.add_argument("--place", choices=["own", "shared"], help="own: только база; shared: база вместе с RX (own)")
    pg.add_argument("--docker", action="store_const", const=1, help="база в контейнере")
    pg.add_argument("--users", type=pos, metavar="N", help="одновременно работающих (100)")
    pg.add_argument("--db-gb", dest="dbgb", type=pos, metavar="N", help="размер базы без тел документов, ГБ (50)")
    pg.add_argument("--replica", action="store_const", const=1, help="есть или будет реплика")
    pg.add_argument("--archive", action="store_const", const=1, help="есть архив WAL")
    pg.add_argument("--json", action="store_true", help="результат в JSON")
    return ap


CHOICES = {"os": ("linux", "windows"), "db": ("pg", "mssql"), "pgKind": ("pro", "vanilla"), "place": ("vm", "k8s"),
           "k8s": ("ns", "full"), "store": ("s3", "fs")}


def servers_inputs(url=None, preset=None, overrides=None):
    """Входы калькулятора: значения по умолчанию, затем профиль, ссылка с сайта и явные значения.
    Числа приводятся к float, как в JavaScript; недопустимые строковые значения дают InputError."""
    inp = dict(DEFAULTS)
    if preset:
        inp.update(PRESETS[preset])
    if url:
        inp.update(decode_servers_url(url))
    for k, val in (overrides or {}).items():
        if val is not None:
            inp[k] = val
    for k, allowed in CHOICES.items():
        if inp[k] not in allowed:
            raise InputError("недопустимое значение %s=%s, ожидается одно из: %s" % (k, inp[k], ", ".join(allowed)))
    if inp["ver"] not in VERSIONS:
        raise InputError("неизвестная версия %s, ожидается одна из: %s" % (inp["ver"], ", ".join(VERSIONS)))
    for k, val in list(inp.items()):
        if isinstance(DEFAULTS[k], (int, float)):
            inp[k] = float(val)
    if inp["users"] > 10000000 or inp["active"] > 10000000 or inp["mobile"] > 10000000:
        raise InputError("слишком много пользователей: калькулятор рассчитан на числа до десяти миллионов")
    return inp


def _utf8_stdout():
    """Python 3.6 с локалью C пишет в ascii и падает на кириллице: переключаем вывод на UTF-8."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        enc = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if enc != "utf8" and hasattr(stream, "buffer"):
            setattr(sys, name, io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace", line_buffering=True))


def main(argv=None):
    _utf8_stdout()
    ap = build_parser()
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    if not args.cmd:
        ap.print_help(sys.stderr)
        return 2
    try:
        if args.cmd == "servers":
            inp = servers_inputs(args.url, args.preset, dict((k, getattr(args, k, None)) for k in DEFAULTS))
            res = compute(inp)
            if args.json:
                out = {"tool": TOOL, "version": VERSION, "calculator": "servers", "source": "https://drxinfra.ru/sizing.html", "url": servers_url(inp),
                       "inputs": inp, "derived": res["d"], "contours": res["contours"], "grand": res["grand"],
                       "warnings": res["warnings"], "parameters": describe_inputs(res)}
                print(json.dumps(clean(out), ensure_ascii=False, indent=1))
            else:
                print(render_servers(res, args.details))
        elif args.cmd == "storage":
            raw = decode_storage_url(args.url) if args.url else {}
            raw.update(dict((k, getattr(args, k)) for k in STORAGE_DEFAULTS if getattr(args, k) is not None))
            v = storage_read(raw)
            r = storage_calc(v)
            notes = storage_notes(v, r)
            if args.json:
                out = {"tool": TOOL, "version": VERSION, "calculator": "storage", "source": "https://drxinfra.ru/storage-growth.html", "url": storage_url(v),
                       "inputs": v, "result": r, "text": storage_text(v, r).replace("\u00a0", " "),
                       "notes": [{"lvl": lvl, "text": t.replace("\u00a0", " ")} for lvl, t in notes]}
                print(json.dumps(clean(out), ensure_ascii=False, indent=1))
            else:
                print(render_storage(v, r, notes))
        else:
            raw = decode_postgres_url(args.url) if args.url else {}
            raw.update(dict((k, getattr(args, k)) for k in PG_DEFAULTS if getattr(args, k) is not None))
            v = pg_read(raw)
            r = pg_calc(v)
            notes = pg_notes(v, r)
            if args.json:
                settings = dict((k, val) for k, val, _ in pg_settings(v, r))
                settings["max_prepared_transactions"] = js_str(r["conn"])
                if r["huge"]:
                    settings["vm.nr_hugepages"] = js_str(r["hugePages"])
                container = {"shm_size": "1g", "min_memory_limit_gb": pg_container_limit_gb(r)} if v["docker"] else None
                out = {"tool": TOOL, "version": VERSION, "calculator": "postgres", "source": "https://drxinfra.ru/postgresql-config.html", "url": postgres_url(v),
                       "inputs": v, "result": r, "settings": settings, "container": container,
                       "restart_required": [k for k, _, rs in pg_settings(v, r) if rs] + ["max_prepared_transactions"],
                       "notes": [{"lvl": lvl, "text": t} for lvl, t in notes]}
                print(json.dumps(clean(out), ensure_ascii=False, indent=1))
            else:
                print(render_postgres(v, r, notes))
    except InputError as e:
        sys.stderr.write("sizing.py: %s\n" % e)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
