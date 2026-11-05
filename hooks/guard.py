#!/usr/bin/env python3
"""Хук PreToolUse для Bash: спрашивает человека перед командами, которые меняют систему.

Claude Code передаёт на stdin JSON с tool_input.command. Если команда похожа на
изменение (перезапуск службы, удаление, запись в базу, очистка очереди, kubectl apply…),
хук отвечает permissionDecision "ask": Claude Code покажет команду и причину и
выполнит её только после подтверждения. Команды, которые уничтожают диск или
файловую систему целиком, хук запрещает ("deny"): их человек выполняет сам.

Выключить: переменная окружения RXOPS_GUARD=off. Только стандартная библиотека.
"""
import json
import os
import re
import sys

I = re.I

# (причина, регулярка). Порядок не важен: собираются все совпадения.
DENY = [
    ("форматирование или затирание диска",
     re.compile(r"\b(?:mkfs(?:\.\w+)?|wipefs|sgdisk\s+--zap|blkdiscard)\b|\bdd\b[^\n;|&]*\bof=/dev/(?!null\b|zero\b|stdout\b|stderr\b)", I)),
    ("удаление корня файловой системы",
     re.compile(r"\brm\s+(?:-[a-zA-Z]*\s+)*-[a-zA-Z]*[rR][a-zA-Z]*\s+(?:-[a-zA-Z-]+\s+)*(?:--no-preserve-root\s+)?/(?:\*)?(?=\s|$|;|&|\|)")),
    ("fork-бомба", re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:")),
]

ASK = [
    ("управление службами и перезагрузка",
     re.compile(r"\bsystemctl\s+(?:--\S+\s+)*(?:start|stop|restart|reload|try-restart|reload-or-restart|enable|disable|mask|unmask|kill|isolate|daemon-reload|set-property|edit|revert)\b"
                r"|\bservice\s+\S+\s+(?:start|stop|restart|reload|force-reload)\b"
                r"|\b(?:reboot|poweroff|halt)\b|\bshutdown\s+(?:-[rhP]|now|\+)|\binit\s+[06]\b", I)),
    ("контейнеры: остановка, удаление, пересоздание",
     re.compile(r"\b(?:docker|podman|nerdctl)\s+(?:container\s+|image\s+|volume\s+|network\s+|system\s+)?"
                r"(?:stop|kill|rm|rmi|restart|pause|unpause|update|prune|start|run|create|load|import|tag|push)\b"
                r"|\b(?:docker|podman)[\s-]compose\s+(?:-f\s+\S+\s+|-p\s+\S+\s+|--\S+\s+)*(?:down|up|restart|stop|start|rm|kill|pull|create|run)\b"
                r"|\bdocker-compose\s+(?:-f\s+\S+\s+|-p\s+\S+\s+|--\S+\s+)*(?:down|up|restart|stop|start|rm|kill|pull|create|run)\b", I)),
    ("Kubernetes и Helm: изменение кластера",
     re.compile(r"\bkubectl\s+(?:-\S+\s+(?:\S+\s+)?)*?(?:delete|apply|create|replace|patch|edit|scale|autoscale|drain|cordon|uncordon|taint|label|annotate|set|expose|run)\b"
                r"|\bkubectl\s+(?:-\S+\s+(?:\S+\s+)?)*?rollout\s+(?:restart|undo|pause|resume)\b"
                r"|\bhelm\s+(?:-\S+\s+)*(?:install|upgrade|uninstall|delete|rollback)\b"
                r"|\bflux\s+(?:reconcile|suspend|resume|delete|create)\b|\bkustomize\s+build\b[^\n]*\|\s*kubectl\s+apply", I)),
    ("PostgreSQL: запись, DDL или управление сервером",
     re.compile(r"\b(?:pg_ctl|pg_ctlcluster\s+\S+\s+\S+)\s+(?:-\S+\s+(?:\S+\s+)?)*(?:stop|restart|promote|reload|kill)\b"
                r"|\b(?:pg_resetwal|pg_resetxlog|pg_rewind|pg_restore|dropdb|createdb|dropuser|createuser|pg_archivecleanup|pg_upgrade|pg_dropcluster|pg_createcluster)\b"
                r"|\bpg_probackup(?:-\d+)?\s+(?:restore|delete|merge|del-instance|add-instance|set-config|set-backup|init)\b", I)),
    ("RabbitMQ: удаление, очистка, политики, пользователи",
     re.compile(r"\brabbitmqctl\s+(?:-\S+\s+(?:\S+\s+)?)*(?:delete_queue|purge_queue|stop_app|start_app|stop|reset|force_reset|forget_cluster_node|set_policy|clear_policy|set_operator_policy|clear_operator_policy|set_permissions|clear_permissions|add_user|delete_user|change_password|clear_password|set_user_tags|close_connection|close_all_connections|set_vm_memory_high_watermark|set_disk_free_limit|eval|delete_vhost|add_vhost|join_cluster|update_cluster_nodes|rename_cluster_node|enable_feature_flag|set_parameter|clear_parameter|set_global_parameter|import_definitions)\b"
                r"|\brabbitmq-queues\s+(?:\S+\s+)*?(?:delete_member|add_member|grow|shrink|rebalance)\b"
                r"|\brabbitmq-plugins\s+(?:enable|disable)\b"
                r"|\brabbitmqadmin\s+(?:\S+\s+)*?(?:delete|purge|declare|close|publish|import)\b", I)),
    ("установка и удаление пакетов",
     re.compile(r"\b(?:apt|apt-get|aptitude)\s+(?:-\S+\s+)*(?:install|remove|purge|upgrade|dist-upgrade|full-upgrade|autoremove)\b"
                r"|\b(?:dnf|yum|zypper)\s+(?:-\S+\s+)*(?:install|remove|erase|update|upgrade|downgrade|reinstall|autoremove)\b"
                r"|\brpm\s+(?:-\S*[eiUF]\S*)\b|\bdpkg\s+(?:-i|-r|-P|--install|--remove|--purge)\b"
                r"|\bsudo\s+(?:-\S+\s+)*pip3?\s+install\b|\b(?:snap|flatpak)\s+(?:install|remove)\b", I)),
    ("пользователи, пароли и сетевой экран",
     re.compile(r"\b(?:useradd|userdel|usermod|groupadd|groupdel|gpasswd|chpasswd|passwd|chage|visudo)\b"
                r"|\b(?:iptables|ip6tables)\s+(?!-[LS]\b|--list\b)-\S"
                r"|\bnft\s+(?!list\b)(?:add|delete|flush|insert|replace|create|-f)\b"
                r"|\bfirewall-cmd\s+(?:\S+\s+)*?--(?:add|remove|reload|permanent|set|zone=\S+\s+--(?:add|remove))"
                r"|\bufw\s+(?:allow|deny|reject|limit|delete|enable|disable|reset|insert)\b", I)),
    ("остановка процессов",
     re.compile(r"(?:^|[\s;&|(])(?:kill|pkill|killall)\s+(?!-l\b|-0\b)", I)),
    ("установщик Directum RX и DeploymentTool",
     re.compile(r"(?:^|[\s/;&|(])(?:do\.sh|do\.bat|DirectumLauncher(?:\.exe)?|DeploymentToolCore(?:\.exe)?|dtc)\b", I)),
    ("Ansible: применение изменений",
     re.compile(r"\bansible-playbook\b(?![^\n;|&]*--(?:check|syntax-check|list-tasks|list-hosts)\b)"
                r"|\bansible\s+\S+\s+[^\n;|&]*-m\s+(?:shell|command|raw|service|systemd|systemd_service|file|copy|template|lineinfile|apt|dnf|yum|package|user|reboot)\b", I)),
    ("диски и монтирование",
     re.compile(r"\b(?:fdisk|parted|gdisk|sfdisk|lvremove|lvreduce|lvextend|lvresize|vgremove|pvremove|vgreduce|resize2fs|xfs_growfs|tune2fs|swapoff|umount)\b"
                r"|\bmount\s+(?:-[a-zA-Z]+\s+)*(?:/|UUID=|LABEL=|-a\b|-o\b|--bind\b|-t\s)", I)),
    ("cron и таймеры",
     re.compile(r"\bcrontab\s+(?:-u\s+\S+\s+)?(?:-r|-e|-i|\S+\.(?:txt|cron))\b", I)),
]

# Запись SQL: проверяется, только если в команде есть клиент СУБД.
SQL_CLIENT = re.compile(r"\b(?:psql|pgcli|sqlcmd|mysql|clickhouse-client)\b", I)
SQL_WRITE = re.compile(
    r"\b(?:insert\s+into|update\s+\S+\s+set|delete\s+from|truncate|drop\s+(?:table|database|schema|index|view|role|user|extension|function|trigger|owned)"
    r"|alter\s+(?:table|database|system|role|user|index|sequence|schema|extension|default)|create\s+(?:table|database|schema|index|role|user|extension|function|trigger|view|materialized)"
    r"|grant\s|revoke\s|vacuum\b|reindex\b|cluster\b|copy\s+\S+\s+from|refresh\s+materialized|reassign\s+owned"
    r"|pg_terminate_backend|pg_cancel_backend|pg_reload_conf|pg_drop_replication_slot|pg_create_\w+_replication_slot|pg_switch_wal|pg_promote|set_config\s*\(|lo_unlink)", I)

# Удаление и перезапись в системных каталогах.
SYS = r"(?:/(?:etc|opt|var|srv|usr|boot|home|root|data|mnt|lib|bin|sbin)(?:/|\b)|~/)"
FILES = [
    ("удаление файлов в системных каталогах",
     re.compile(r"(?:^|[\s;&|(])(?:sudo\s+)?(?:rm|shred|unlink|rmdir)\s+(?:-\S+\s+)*[\"']?" + SYS, I)),
    ("массовое удаление find",
     re.compile(r"\bfind\b[^\n;|&]*\s(?:-delete\b|-exec\s+(?:rm|shred|truncate)\b)", I)),
    ("перезапись или обрезка файла в системном каталоге",
     re.compile(r"(?:\btruncate\s+-s\s*\S+\s+[\"']?" + SYS +
                r"|\bsed\s+(?:-\S+\s+)*-i\S*\s+[^\n;|&]*?" + SYS +
                r"|\btee\s+(?:-a\s+)?[\"']?" + SYS +
                r"|(?:^|[^<>&0-9])>{1,2}\s*[\"']?" + SYS +
                r"|\b(?:mv|cp|install|ln)\s+(?:-\S+\s+)*\S+\s+[\"']?/etc/"
                r"|\bchmod\s+(?:-R\s+)?\S+\s+[\"']?" + SYS + r"|\bchown\s+(?:-R\s+)?\S+\s+[\"']?" + SYS + r")", I)),
]

# HTTP-запросы, которые меняют данные. Поиск Elasticsearch через POST (_search, _count…) не считаем записью.
HTTP_CLIENT = re.compile(r"\b(?:curl|wget|http|https|xh)\b", I)
HTTP_WRITE = re.compile(
    r"(?:-X\s*|--request[=\s]+|--method[=\s]+)['\"]?(?:POST|PUT|DELETE|PATCH)\b"
    r"|\s(?:-d|--data(?:-raw|-binary|-urlencode)?|-F|--form|-T|--upload-file|--post-data|--post-file|--body-data|--json)[\s=]"
    r"|\b(?:POST|PUT|DELETE|PATCH)\s+\S*://", I)
HTTP_READ_ONLY = re.compile(
    r"/_(?:search|msearch|count|sql|validate/query|analyze|explain|field_caps|termvectors|mtermvectors|render/template)\b"
    r"|/_cluster/allocation/explain\b|/_nodes/[^\s'\"]*hot_threads\b", I)


def http_write(text):
    for part in re.split(r"[;&|\n]+", text):
        if HTTP_CLIENT.search(part) and HTTP_WRITE.search(part) and not HTTP_READ_ONLY.search(part):
            return True
    return False


# Безопасные назначения перенаправления: /dev/null и вывод в /tmp не считаем записью в систему.
SAFE_REDIRECT = re.compile(r">{1,2}\s*/dev/(?:null|stdout|stderr)\b")


def classify(cmd):
    """Возвращает ("deny"|"ask"|None, [причины])."""
    text = SAFE_REDIRECT.sub(" ", cmd)
    deny = [why for why, rx in DENY if rx.search(text)]
    if deny:
        return "deny", deny
    ask = [why for why, rx in ASK if rx.search(text)]
    if http_write(text):
        ask.append("HTTP-запрос, который меняет данные (POST, PUT, DELETE, PATCH)")
    if SQL_CLIENT.search(text) and SQL_WRITE.search(text):
        ask.append("запрос к базе, который меняет данные или схему")
    ask += [why for why, rx in FILES if rx.search(text)]
    return ("ask", ask) if ask else (None, [])


def main():
    if os.environ.get("RXOPS_GUARD", "").lower() in ("off", "0", "no", "false"):
        return 0
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
    except ValueError:
        return 0
    if data.get("tool_name") != "Bash":
        return 0
    cmd = (data.get("tool_input") or {}).get("command") or ""
    decision, reasons = classify(cmd)
    if not decision:
        return 0
    if decision == "deny":
        reason = ("rxops: команда запрещена (%s). Такие команды администратор выполняет сам, "
                  "вне агента. Объясните пользователю, что и зачем нужно сделать." % "; ".join(reasons))
    else:
        reason = ("rxops: команда меняет систему (%s). Перед подтверждением проверьте, на каком сервере "
                  "она выполнится, что изменит и как это откатить." % "; ".join(reasons))
    out = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                          "permissionDecision": decision,
                                          "permissionDecisionReason": reason}}, ensure_ascii=False)
    sys.stdout.buffer.write(out.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
