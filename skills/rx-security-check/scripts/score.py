#!/usr/bin/env python3
"""Подсчёт самооценки безопасности Directum RX: 22 вопроса, балл из 100 и зона.

Использование:
  python3 score.py --list                       вопросы с весами
  python3 score.py --answers ypnu-yyy...        ответы строкой, по символу на вопрос q1..q22
  python3 score.py --answers 'https://drxinfra.ru/security-check.html?a=ypn...'
  python3 score.py q1=да q2=частично q3=нет q4='не знаю'
  python3 score.py --file answers.json          {"q1": "да", ...}, {"answers": "ypn..."} или список
  добавьте --json для машинного вывода

Ответы: y да, p частично, n нет, u не знаю, - не отвечено.
Счёт как на странице https://drxinfra.ru/security-check.html: вес критичного вопроса 10,
высокого 6, среднего 3, низкого 1. «Да» даёт полный вес, «частично» половину, «нет» и «не знаю»
ноль. Балл = набранный вес / возможный по отвеченным вопросам * 100, округление как в браузере.
Красная зона: критичный вопрос с ответом «нет» или «не знаю», или балл ниже 50.
Жёлтая: от 50 до 79. Зелёная: 80 и выше без открытых критичных.

Код возврата: 0 зелёная зона, 1 жёлтая или красная, 2 нет ответов или ошибка во входных данных.
Только стандартная библиотека, Python 3.6+. Ничего не пишет на диск.
"""
import datetime
import json
import math
import os
import re
import socket
import sys

VERSION = "0.1.0"
SITE = "https://drxinfra.ru/"
PAGE = SITE + "security-check.html"

SEV = {"crit": "Критично", "high": "Высокий", "mid": "Средний", "low": "Низкий"}
WEIGHT = {"crit": 10, "high": 6, "mid": 3, "low": 1}
VAL = {"y": 1, "p": 0.5, "n": 0, "u": 0}
LABEL = {"y": "Да", "p": "Частично", "n": "Нет", "u": "Не знаю"}
ZONE = {"red": "Красная зона: начинать сейчас", "yellow": "Жёлтая зона: есть заметные дыры",
        "green": "Зелёная зона: база крепкая"}

# id, уровень, текст вопроса, что сделать, ссылка (относительно сайта), подпись ссылки,
# проверки host_audit.py, которые подтверждают или опровергают ответ.
QUESTIONS = [
    ("q1", "crit", "Бэкап базы и хранилища документов делается автоматически и лежит на другом сервере",
     "Настройте ежедневный бэкап базы и файлов хранилища по расписанию с копией на отдельное хранилище "
     "и уведомлением, если бэкап не сделался.", "postgresql-backup.html", "Команды бэкапа", []),
    ("q2", "crit", "Восстановление из бэкапа проверяли за последние три месяца",
     "Разверните бэкап на отдельном сервере, запустите RX и откройте пару документов. Запишите, сколько "
     "это заняло: это ваше время восстановления.", "blog/postgresql-backup-restore-check.html",
     "Как проверить восстановление", []),
    ("q3", "crit", "Пароли по умолчанию заменены везде",
     "Пройдите по всем компонентам и смените встроенные пароли. Пользователя guest в RabbitMQ удалите.",
     "rabbitmq-config.html", "Настройки RabbitMQ", []),
    ("q4", "crit", "База данных, брокер, поиск и хранилище не видны из интернета и из сети пользователей",
     "Закройте порты 5432 или 1433, 5672, 15672, 9200 и порт хранилища сетевым экраном. Открыт должен "
     "быть только веб-клиент.", None, None, ["ports", "firewall"]),
    ("q5", "crit", "Обновления безопасности ОС и СУБД ставятся хотя бы раз в квартал",
     "Заведите регулярное окно обновлений: сначала тестовый контур, через неделю прод.", None, None,
     ["updates"]),
    ("q6", "high", "Веб-клиент работает только по HTTPS, TLS 1.0 и 1.1 выключены",
     "Включите на прокси только TLS 1.2 и 1.3 и поставьте уведомление за 30 дней до окончания "
     "сертификата.", "nginx-config.html", "Настройки прокси", ["tls"]),
    ("q7", "high", "У каждого администратора своя учётная запись",
     "Заведите именные учётки на серверах, в СУБД и в RX, общие пароли смените после последнего ухода.",
     "blog/fstek-maturity-contractor.html", "Что записать в договор с подрядчиком",
     ["accounts", "sudo", "sshd"]),
    ("q8", "high", "На серверы заходят по ключу или через одну точку входа",
     "Закройте вход по паролю, оставьте ключи и доступ через VPN или промежуточный сервер.", None, None,
     ["sshd"]),
    ("q9", "high", "Пароли и ключи хранятся в менеджере паролей или хранилище секретов",
     "Перенесите пароли сервисов в хранилище, а те, что лежали открыто, смените.", None, None,
     ["secrets", "history"]),
    ("q10", "high", "Учётка RX в базе данных не суперпользователь",
     "Создайте для RX владельца базы без прав суперпользователя, а для мониторинга отдельную учётку "
     "только на чтение.", None, None, []),
    ("q11", "high", "Действия администраторов на серверах записываются в журнал, и он хранится вне сервера",
     "Включите auditd или его аналог и отправляйте журналы в отдельное хранилище.",
     "blog/auditd-security-events.html", "Журнал событий безопасности", ["auditd", "logship"]),
    ("q12", "high", "Об аварии вы узнаёте из уведомления, а не от пользователей",
     "Настройте мониторинг с уведомлениями в почту или мессенджер, начиная с диска, бэкапов и "
     "сертификатов.", "blog/rx-monitoring-alerts.html", "Какие алерты нужны первыми", []),
    ("q13", "high", "Версии ОС, СУБД, брокера и поиска ещё поддерживаются производителем",
     "Составьте список версий с датами окончания поддержки и запланируйте переход до этих дат.", None,
     None, ["system"]),
    ("q14", "mid", "Есть тестовый контур, обновления сначала проходят через него",
     "Поднимите тестовый контур из бэкапа прода и проводите через него каждое обновление.",
     "upgrade-checklist.html", "Чек-лист обновления", []),
    ("q15", "mid", "Изменения настроек идут по описанной процедуре, а не руками на проде",
     "Храните настройки серверов в репозитории и меняйте их оттуда. Минимум: журнал изменений с датой "
     "и автором.", None, None, []),
    ("q16", "mid", "Пакеты и образы ставятся из своего зеркала или проверенного реестра с точными версиями",
     "Заведите своё зеркало пакетов и реестр образов, фиксируйте версии и проверяйте контрольные суммы.",
     None, None, ["repos", "containers"]),
    ("q17", "mid", "На веб-клиенте включены защитные заголовки",
     "Добавьте заголовки на прокси перед RX.", "nginx-config.html", "Настройки прокси", []),
    ("q18", "mid", "Журналы RX собираются в одно место и хранятся не меньше 30 дней",
     "Соберите журналы сервисов в Elasticsearch или аналог со сроком хранения.",
     "blog/rx-logs-centralized.html", "Сбор журналов RX", ["logship"]),
    ("q19", "mid", "На самих серверах включён сетевой экран",
     "Включите экран на серверах и разрешите только нужные порты с нужных адресов.", None, None,
     ["firewall"]),
    ("q20", "low", "Есть актуальная схема: серверы, порты, учётки сервисов, кто за что отвечает",
     "Опишите схему на одной странице и обновляйте её при каждом изменении.", None, None, []),
    ("q21", "low", "Лишние службы и интерфейсы управления выключены или закрыты",
     "Закройте интерфейсы управления сетью или пускайте к ним только через VPN.", None, None, ["ports"]),
    ("q22", "low", "Время на всех серверах синхронизируется",
     "Включите синхронизацию времени с одним источником на всех серверах.",
     "errors/kerberos-clock-skew-too-great.html", "Ошибка Clock skew too great", ["time"]),
]
QIDS = [q[0] for q in QUESTIONS]

WORDS = {
    "y": "y", "yes": "y", "да": "y", "д": "y", "+": "y", "1": "y",
    "p": "p", "partial": "p", "partly": "p", "частично": "p", "ч": "p", "~": "p", "0.5": "p",
    "n": "n", "no": "n", "нет": "n", "н": "n", "0": "n",
    "u": "u", "unknown": "u", "?": "u", "не знаю": "u", "незнаю": "u", "нз": "u", "dontknow": "u",
    "-": "", "": "", "skip": "", "пропуск": "", "не отвечено": "",
}


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


class InputError(Exception):
    pass


def norm_answer(value):
    """Ответ в любом написании → y, p, n, u или '' (не отвечено)."""
    if value is None:
        return ""
    if value is True:
        return "y"
    if value is False:
        return "n"
    key = re.sub(r"\s+", " ", str(value).strip().lower().replace("ё", "е"))
    if key in WORDS:
        return WORDS[key]
    raise InputError("непонятный ответ: %r (ждём да, частично, нет, не знаю или y, p, n, u)" % (value,))


def from_string(s):
    """Строка вида ypnu-... или адрес страницы с ?a=... → словарь ответов."""
    s = s.strip()
    m = re.search(r"[?&]a=([ypnu-]+)", s)
    if m:
        s = m.group(1)
    if not re.match(r"^[ypnu-]*$", s):
        raise InputError("строка ответов должна состоять из y, p, n, u и -, по символу на вопрос")
    if len(s) > len(QIDS):
        raise InputError("ответов %d, а вопросов %d" % (len(s), len(QIDS)))
    return {QIDS[i]: c for i, c in enumerate(s) if c != "-"}


def from_pairs(pairs):
    out = {}
    for p in pairs:
        if "=" not in p:
            raise InputError("ожидаю qN=ответ, получено %r" % p)
        k, v = p.split("=", 1)
        k = k.strip().lower()
        if not re.match(r"^q?\d+$", k):
            raise InputError("непонятный номер вопроса: %r" % k)
        k = "q" + k.lstrip("q")
        if k not in QIDS:
            raise InputError("нет вопроса %s, вопросы q1..q%d" % (k, len(QIDS)))
        a = norm_answer(v)
        if a:
            out[k] = a
        else:
            out.pop(k, None)
    return out


def from_json(data):
    if isinstance(data, dict) and "answers" in data:
        data = data["answers"]
    if isinstance(data, str):
        return from_string(data)
    if isinstance(data, list):
        if len(data) > len(QIDS):
            raise InputError("ответов %d, а вопросов %d" % (len(data), len(QIDS)))
        out = {}
        for i, v in enumerate(data):
            a = norm_answer(v)
            if a:
                out[QIDS[i]] = a
        return out
    if isinstance(data, dict):
        return from_pairs(["%s=%s" % (k, "" if v is None else v) for k, v in data.items()])
    raise InputError("JSON: ожидаю объект {\"q1\": \"да\"}, строку или список")


def js_round(x):
    """Math.round из JavaScript: половина округляется вверх (в Python round() банковский)."""
    f = math.floor(x)
    return int(f + 1 if x - f >= 0.5 else f)


def calc(answers):
    """Тот же расчёт, что calc() в assets/seccheck.js."""
    got, mx, done, crit_open = 0, 0, 0, 0
    cnt = {"y": 0, "p": 0, "n": 0, "u": 0}
    gaps = []
    for q in QUESTIONS:
        qid, sev = q[0], q[1]
        a = answers.get(qid, "")
        if not a:
            continue
        w = WEIGHT[sev]
        done += 1
        cnt[a] += 1
        mx += w
        got += w * VAL[a]
        if a != "y":
            gaps.append((q, a))
        if sev == "crit" and a in ("n", "u"):
            crit_open += 1
    gaps.sort(key=lambda g: (-WEIGHT[g[0][1]], VAL[g[1]]))  # sort стабильный, как в браузере
    score = js_round(got / mx * 100) if mx else None
    if score is None:
        zone = ""
    elif crit_open or score < 50:
        zone = "red"
    elif score < 80:
        zone = "yellow"
    else:
        zone = "green"
    return {"score": score, "zone": zone, "done": done, "cnt": cnt, "crit_open": crit_open, "gaps": gaps,
            "got": got, "max": mx}


def answer_string(answers):
    return "".join(answers.get(q, "") or "-" for q in QIDS).rstrip("-")


def link(href):
    return SITE + href if href else None


def to_text(answers, r, today=None):
    """Текст итога: те же строки, что кнопка «Скопировать итог» на странице, плюс ссылки."""
    d = today or datetime.date.today()
    s = answer_string(answers)
    L = ["Самооценка безопасности инфраструктуры Directum RX", "Дата: " + d.strftime("%d.%m.%Y")]
    total = len(QUESTIONS)
    if r["score"] is None:
        L.append("Итог: нет ответов. Отвечено %d из %d." % (r["done"], total))
    else:
        L.append("Итог: %d из 100, %s. Отвечено %d из %d." % (r["score"], ZONE[r["zone"]].lower(), r["done"], total))
    L.append("")
    last = ""
    for q, a in r["gaps"]:
        if q[1] != last:
            L.append(SEV[q[1]] + ":")
            last = q[1]
        L.append("- [%s] %s" % (LABEL[a], q[2]))
        L.append("  Что сделать: " + q[3])
        if q[4]:
            L.append("  Подробнее: " + link(q[4]))
    if not r["gaps"] and r["done"]:
        L.append("Открытых пунктов нет.")
    L.append("")
    L.append("Ответы: " + PAGE + ("?a=" + s if s else ""))
    extra = []
    if r["crit_open"]:
        extra.append("Открытых критичных: %d." % r["crit_open"])
    missing = [q for q in QIDS if not answers.get(q)]
    if missing and r["done"]:
        extra.append("Без ответа: %s. Балл посчитан только по отвеченным вопросам." % ", ".join(missing))
    if extra:
        L.append("")
        L.extend(extra)
    return "\n".join(L)


def to_json(answers, r):
    checks = []
    for q in QUESTIONS:
        a = answers.get(q[0], "")
        if not a:
            st = "SKIP"
        elif a == "y":
            st = "OK"
        elif a == "p":
            st = "WARN"
        else:
            st = "CRIT" if q[1] == "crit" else "WARN"
        checks.append({"id": q[0], "title": q[2], "status": st,
                       "detail": "%s, вес %d: %s" % (SEV[q[1]], WEIGHT[q[1]], LABEL.get(a, "не отвечено")),
                       "hint": "" if a == "y" else q[3]})
    summary = {}
    for c in checks:
        summary[c["status"]] = summary.get(c["status"], 0) + 1
    s = answer_string(answers)
    return {
        "tool": "score", "version": VERSION, "host": socket.gethostname(),
        "time": datetime.datetime.now().replace(microsecond=0).isoformat(),
        "score": r["score"], "zone": r["zone"] or None, "zone_title": ZONE.get(r["zone"]),
        "answered": r["done"], "total": len(QUESTIONS), "counts": r["cnt"], "crit_open": r["crit_open"],
        "answers": s, "url": PAGE + ("?a=" + s if s else ""),
        "unanswered": [q for q in QIDS if not answers.get(q)],
        "gaps": [{"id": q[0], "severity": q[1], "weight": WEIGHT[q[1]], "answer": a, "text": q[2], "fix": q[3],
                  "link": link(q[4]), "host_checks": q[6]} for q, a in r["gaps"]],
        "checks": checks, "summary": summary,
    }


def list_questions():
    L = []
    for q in QUESTIONS:
        hc = ", ".join(q[6]) if q[6] else "вручную"
        L.append("%-4s %-9s вес %-2d %s  [проверка: %s]" % (q[0], SEV[q[1]], WEIGHT[q[1]], q[2], hc))
    return "\n".join(L)


def parse_args(argv):
    opts = {"json": False, "list": False, "answers": None, "file": None, "pairs": []}
    it = iter(argv)
    for a in it:
        if a in ("-h", "--help"):
            opts["help"] = True
        elif a == "--json":
            opts["json"] = True
        elif a == "--list":
            opts["list"] = True
        elif a in ("--answers", "-a"):
            opts["answers"] = next(it, None)
            if opts["answers"] is None:
                raise InputError("после --answers нужна строка ответов")
        elif a.startswith("--answers="):
            opts["answers"] = a.split("=", 1)[1]
        elif a in ("--file", "-f"):
            opts["file"] = next(it, None)
            if opts["file"] is None:
                raise InputError("после --file нужен путь к JSON")
        elif a.startswith("--file="):
            opts["file"] = a.split("=", 1)[1]
        elif a.startswith("-") and a != "-":
            raise InputError("неизвестный ключ %s, см. --help" % a)
        else:
            opts["pairs"].append(a)
    return opts


def main(argv):
    argv = [fix_arg(a) for a in argv]
    try:
        opts = parse_args(argv)
    except InputError as e:
        write(sys.stderr, "score.py: %s\n" % e)
        return 2
    if opts.get("help"):
        write(sys.stdout, __doc__.strip() + "\n")
        return 0
    if opts["list"]:
        write(sys.stdout, list_questions() + "\n")
        return 0
    try:
        answers = {}
        if opts["file"]:
            if opts["file"] == "-":
                data = json.loads(sys.stdin.buffer.read().decode("utf-8"))
            else:
                with open(opts["file"], encoding="utf-8") as f:
                    data = json.load(f)
            answers.update(from_json(data))
        if opts["answers"] is not None:
            answers.update(from_string(opts["answers"]))
        answers.update(from_pairs(opts["pairs"]))
    except (InputError, ValueError, OSError) as e:
        write(sys.stderr, "score.py: %s\n" % e)
        return 2
    r = calc(answers)
    if opts["json"]:
        write(sys.stdout, json.dumps(to_json(answers, r), ensure_ascii=False, indent=1) + "\n")
    else:
        write(sys.stdout, to_text(answers, r) + "\n")
    if r["score"] is None:
        if not opts["json"]:
            write(sys.stderr, "score.py: нет ни одного ответа, см. --help\n")
        return 2
    return 0 if r["zone"] == "green" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
