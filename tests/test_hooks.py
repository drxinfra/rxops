"""Проверки хуков guard.py и mask_output.py: python3 -m unittest discover -s tests"""
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOKS = os.path.join(ROOT, "hooks")
sys.path.insert(0, HOOKS)
import guard  # noqa: E402

ASK = [
    "sudo systemctl restart nginx",
    "systemctl --now disable rx-worker",
    "docker compose down",
    "docker compose -f /opt/rx/compose.yml up -d",
    "docker rm -f rx-web",
    "podman restart rx-pg",
    "kubectl -n rx delete pod web-0",
    "kubectl rollout restart deploy/web -n rx",
    "kubectl apply -f deploy.yaml",
    "helm upgrade rx ./chart -n rx",
    "psql -U postgres -c 'select pg_terminate_backend(123)'",
    "psql -c \"update users set x=1 where id=2\"",
    "docker exec rx-pg psql -c 'vacuum analyze'",
    "pg_ctl -D /var/lib/postgresql/data restart",
    "pg_probackup-16 delete -B /backup --instance rx --delete-expired",
    "rabbitmqctl purge_queue q1",
    "rabbitmqctl set_policy rx-orphan-expire '^[0-9a-f-]{36}$' '{\"expires\":3600000}' --apply-to queues",
    "curl -X DELETE http://es:9200/idx",
    "curl -XPUT localhost:9200/_cluster/settings -H 'Content-Type: application/json' -d '{}'",
    "curl -u guest:guest -d '{}' http://mq:15672/api/policies/%2f/p",
    "apt-get install -y nginx",
    "kill -9 1234",
    "pkill -f DirectumRX",
    "./do.sh rx up",
    "rm -rf /var/lib/postgresql/16/main/pg_wal/0000*",
    "find /var/log/rx -name '*.gz' -delete",
    "sed -i 's/a/b/' /etc/nginx/nginx.conf",
    "echo '10.0.0.5 db' >> /etc/hosts",
    "ansible-playbook site.yml -i inventory",
    "crontab -r",
    "umount /mnt/backup",
    "mount -o remount,rw /data",
    "ssh rx01 'sudo systemctl restart nginx'",
]
DENY = [
    "mkfs.ext4 /dev/sdb1",
    "dd if=/dev/zero of=/dev/sda bs=1M",
    "rm -rf /",
    "sudo rm -rf --no-preserve-root /",
]
SAFE = [
    "systemctl status nginx",
    "systemctl list-units --failed",
    "journalctl -u nginx --since -1h | tail -50",
    "docker ps -a",
    "docker logs rx-web --tail 100 2>&1 | grep -i error",
    "docker compose ps",
    "kubectl get pods -A",
    "kubectl -n rx describe pod web-0",
    "kubectl logs deploy/web --since=1h",
    "helm list -A",
    "psql -c 'select count(*) from pg_stat_activity'",
    "psql -c \"select pid, state from pg_stat_activity where state = 'idle in transaction'\"",
    "docker exec rx-pg psql -U postgres -c 'select 1'",
    "rabbitmqctl list_queues name messages consumers",
    "rabbitmq-diagnostics alarms",
    "curl -s http://es:9200/_cluster/health?pretty",
    "curl -s -XGET http://es:9200/_cat/indices?v",
    "curl -s -X POST http://es:9200/rx-docs/_search -H 'Content-Type: application/json' -d '{\"size\":0}'",
    "curl -s -u guest:guest http://mq:15672/api/queues",
    "rm -rf ./node_modules build/",
    "rm /tmp/x.log",
    "ls -la /etc > /tmp/etc.txt",
    "grep -ri password /etc/rx 2>/dev/null",
    "cat /etc/hosts",
    "kill -l",
    "mount | grep data",
    "df -h && free -m && ss -ltnp",
    "python3 skills/rx-sizing/scripts/sizing.py --docs 400",
    "ansible-playbook site.yml --check",
]


def run_hook(name, payload, env=None):
    e = dict(os.environ)
    e.update(env or {})
    p = subprocess.run([sys.executable, os.path.join(HOOKS, name)], input=json.dumps(payload).encode(),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e)
    out = p.stdout.decode()
    return p.returncode, (json.loads(out) if out.strip() else None)


class GuardTest(unittest.TestCase):
    def test_ask(self):
        for cmd in ASK:
            self.assertEqual(guard.classify(cmd)[0], "ask", cmd)

    def test_deny(self):
        for cmd in DENY:
            self.assertEqual(guard.classify(cmd)[0], "deny", cmd)

    def test_safe(self):
        for cmd in SAFE:
            self.assertEqual(guard.classify(cmd), (None, []), cmd)

    def test_hook_protocol(self):
        rc, out = run_hook("guard.py", {"tool_name": "Bash", "tool_input": {"command": "systemctl restart nginx"}})
        self.assertEqual(rc, 0)
        h = out["hookSpecificOutput"]
        self.assertEqual(h["hookEventName"], "PreToolUse")
        self.assertEqual(h["permissionDecision"], "ask")
        self.assertIn("rxops", h["permissionDecisionReason"])
        rc, out = run_hook("guard.py", {"tool_name": "Bash", "tool_input": {"command": "systemctl status nginx"}})
        self.assertEqual((rc, out), (0, None))
        rc, out = run_hook("guard.py", {"tool_name": "Bash", "tool_input": {"command": "systemctl restart x"}},
                           {"RXOPS_GUARD": "off"})
        self.assertEqual((rc, out), (0, None))


class MaskHookTest(unittest.TestCase):
    def test_bash_output_masked(self):
        resp = {"stdout": "DB_PASSWORD=hunter2\nport=5432\n", "stderr": "", "interrupted": False, "isImage": False}
        rc, out = run_hook("mask_output.py", {"tool_name": "Bash", "tool_response": resp})
        self.assertEqual(rc, 0)
        h = out["hookSpecificOutput"]
        self.assertEqual(h["hookEventName"], "PostToolUse")
        upd = h["updatedToolOutput"]
        self.assertEqual(set(upd), set(resp))
        self.assertNotIn("hunter2", upd["stdout"])
        self.assertIn("port=5432", upd["stdout"])
        self.assertIs(upd["interrupted"], False)

    def test_nested_read_shape(self):
        resp = {"type": "text", "file": {"filePath": "/etc/rx/.env", "content": "RABBITMQ_PASSWORD=qwe\n",
                                         "numLines": 1, "startLine": 1, "totalLines": 1}}
        rc, out = run_hook("mask_output.py", {"tool_name": "Read", "tool_response": resp})
        upd = out["hookSpecificOutput"]["updatedToolOutput"]
        self.assertEqual(upd["file"]["filePath"], "/etc/rx/.env")
        self.assertNotIn("qwe", upd["file"]["content"])
        self.assertEqual(upd["file"]["numLines"], 1)

    def test_clean_output_untouched(self):
        rc, out = run_hook("mask_output.py", {"tool_name": "Bash",
                                              "tool_response": {"stdout": "ok\n", "stderr": ""}})
        self.assertEqual((rc, out), (0, None))
        rc, out = run_hook("mask_output.py", {"tool_name": "Bash", "tool_response": {"stdout": "PASSWORD=x1"}},
                           {"RXOPS_MASK": "off"})
        self.assertEqual((rc, out), (0, None))


if __name__ == "__main__":
    unittest.main()
