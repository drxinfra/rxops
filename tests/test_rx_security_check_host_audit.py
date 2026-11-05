"""Проверки host_audit.py (skills/rx-security-check/scripts): разбор вывода команд и файлов.

Без сети, без root и без живых служб: файлы кладутся во временный «корень», команды отвечают заготовками.
Запуск: python3 -m unittest discover -s tests
"""
import datetime
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skills", "rx-security-check", "scripts")
SCRIPT = os.path.join(SCRIPTS, "host_audit.py")
sys.dont_write_bytecode = True


def load():
    spec = importlib.util.spec_from_file_location("rx_security_host_audit", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ha = load()
NOW = datetime.datetime(2026, 11, 5, 12, 0, 0)


class FakeRunner:
    """Вместо subprocess: ответы по точной команде или по имени программы."""

    def __init__(self, responses=None, tools=()):
        self.responses = responses or {}
        self.tools = set(tools) | set(k if isinstance(k, str) else k[0] for k in self.responses)
        self.timeout = 5
        self.calls = []

    def which(self, name):
        return "/usr/bin/" + name if name in self.tools else None

    def run(self, cmd, timeout=None, stdin_data=None):
        self.calls.append(list(cmd))
        for k in (tuple(cmd), cmd[0]):
            if k in self.responses:
                v = self.responses[k]
                return v(cmd, stdin_data) if callable(v) else v
        if cmd[0] not in self.tools:
            return None, "", "нет команды %s" % cmd[0]
        return 1, "", "нет заготовки"


class FakeRoot:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name

    def write(self, rel, text, mode=0o644):
        p = os.path.join(self.path, rel.lstrip("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(text.encode("utf-8") if isinstance(text, str) else text)
        os.chmod(p, mode)
        return p

    def ctx(self, runner=None, root=True, **opts):
        o = {"timeout": 5, "tls_warn_days": 30, "tls_crit_days": 7, "updates_days": 90}
        o.update(opts)
        return ha.Ctx(o, runner=runner or FakeRunner(), root=root, rootfs=self.path, now=NOW)

    def close(self):
        self.tmp.cleanup()


def by_id(results):
    return {r["id"]: r for r in results}


class SshdTest(unittest.TestCase):
    def st(self, cfg):
        r = by_id(ha.sshd_results(cfg, "test"))
        return r["sshd.root"]["status"], r["sshd.password"]["status"]

    def test_effective_values(self):
        cfg = ha.parse_sshd_T("port 22\npermitrootlogin no\npasswordauthentication no\n"
                              "kbdinteractiveauthentication no\nusepam yes\nauthenticationmethods any\n")
        self.assertEqual(self.st(cfg), ("OK", "OK"))
        self.assertEqual(self.st({"permitrootlogin": "yes", "passwordauthentication": "yes"}), ("CRIT", "WARN"))
        self.assertEqual(self.st({"permitrootlogin": "yes", "passwordauthentication": "no",
                                  "kbdinteractiveauthentication": "no"}), ("WARN", "OK"))
        self.assertEqual(self.st({"permitrootlogin": "prohibit-password", "passwordauthentication": "no",
                                  "kbdinteractiveauthentication": "no"}), ("WARN", "OK"))
        # пароль после ключа не даёт войти одним паролем
        self.assertEqual(self.st({"permitrootlogin": "no", "passwordauthentication": "yes",
                                  "authenticationmethods": "publickey,password"}), ("OK", "OK"))
        # PAM с keyboard-interactive тоже вход по паролю
        self.assertEqual(self.st({"permitrootlogin": "no", "passwordauthentication": "no",
                                  "kbdinteractiveauthentication": "yes", "usepam": "yes"}), ("OK", "WARN"))
        self.assertEqual(self.st({"permitrootlogin": "no", "passwordauthentication": "yes",
                                  "permitemptypasswords": "yes"}), ("OK", "CRIT"))
        # старое имя ключа
        cfg = ha.parse_sshd_T("challengeresponseauthentication yes\nusepam yes\npasswordauthentication no\n")
        self.assertEqual(cfg["kbdinteractiveauthentication"], "yes")

    def test_files_first_value_wins(self):
        fr = FakeRoot()
        try:
            fr.write("/etc/ssh/sshd_config", "# comment\nInclude /etc/ssh/sshd_config.d/*.conf\n"
                     "PasswordAuthentication yes\nPermitRootLogin=yes\nMatch User backup\n  PasswordAuthentication yes\n")
            fr.write("/etc/ssh/sshd_config.d/50-hardening.conf", "PasswordAuthentication no\nPermitRootLogin no\n"
                     "KbdInteractiveAuthentication no\n")
            fr.write("/etc/ssh/sshd_config.d/README", "PermitRootLogin yes\n")  # не *.conf
            ctx = fr.ctx(root=False)
            cfg, st = ha.parse_sshd_files(ctx)
            self.assertEqual(cfg["passwordauthentication"], "no")
            self.assertEqual(cfg["permitrootlogin"], "no")
            self.assertEqual(st["matches"], 1)
            r = by_id(ha.check_sshd(ctx))
            self.assertEqual(r["sshd.root"]["status"], "OK")
            self.assertEqual(r["sshd.password"]["status"], "OK")
            self.assertIn("Match", r["sshd.password"]["detail"])
        finally:
            fr.close()

    def test_sshd_T_under_root(self):
        fr = FakeRoot()
        try:
            fr.write("/etc/ssh/sshd_config", "PermitRootLogin no\n")
            runner = FakeRunner({("sshd", "-T"): (0, "permitrootlogin yes\npasswordauthentication yes\n", "")})
            r = by_id(ha.check_sshd(fr.ctx(runner, root=True)))
            self.assertEqual(r["sshd.root"]["status"], "CRIT")
            self.assertIn("sshd -T", r["sshd.root"]["detail"])
        finally:
            fr.close()

    @unittest.skipIf(os.geteuid() == 0, "под root права на чтение не ограничить")
    def test_unreadable_include_is_error(self):
        fr = FakeRoot()
        try:
            fr.write("/etc/ssh/sshd_config", "Include /etc/ssh/sshd_config.d/*.conf\nPasswordAuthentication no\n")
            fr.write("/etc/ssh/sshd_config.d/10-secret.conf", "PasswordAuthentication yes\n", mode=0o000)
            r = by_id(ha.check_sshd(fr.ctx(root=False)))
            self.assertEqual(r["sshd.password"]["status"], "ERROR")
        finally:
            fr.close()


class AccountsSudoTest(unittest.TestCase):
    def test_accounts(self):
        fr = FakeRoot()
        try:
            fr.write("/etc/passwd", "root:x:0:0:root:/root:/bin/bash\ntoor:x:0:0::/root:/bin/sh\n"
                     "daemon:x:1:1::/usr/sbin:/usr/sbin/nologin\nadmin:x:1000:1000::/home/admin:/bin/bash\n")
            fr.write("/etc/shadow", "root:$6$abc:19000::::::\ntoor::19000::::::\nadmin:!:19000::::::\n", 0o640)
            fr.write("/home/admin/.ssh/authorized_keys", "ssh-ed25519 AAAA admin@host\n# old\n\nssh-rsa BBBB x\n")
            r = by_id(ha.check_accounts(fr.ctx()))
            self.assertEqual(r["accounts.uid0"]["status"], "CRIT")
            self.assertIn("toor", r["accounts.uid0"]["detail"])
            self.assertEqual(r["accounts.emptypw"]["status"], "CRIT")
            self.assertIn("toor", r["accounts.emptypw"]["detail"])
            self.assertNotIn("admin", r["accounts.emptypw"]["detail"])
            self.assertIn("admin 2", r["accounts.login"]["detail"])
            self.assertNotIn("daemon", r["accounts.login"]["detail"])
        finally:
            fr.close()

    def test_sudoers_includedir(self):
        fr = FakeRoot()
        try:
            fr.write("/etc/sudoers", "Defaults env_reset\n# %wheel ALL=(ALL) NOPASSWD: ALL\nroot ALL=(ALL) ALL\n"
                     "%admins ALL=(ALL) \\\n    NOPASSWD: ALL\n@includedir /etc/sudoers.d\n", 0o440)
            fr.write("/etc/sudoers.d/deploy", "deploy ALL=(root) NOPASSWD: /usr/bin/systemctl restart nginx\n", 0o440)
            fr.write("/etc/sudoers.d/old.bak", "olduser ALL=(ALL) NOPASSWD: ALL\n", 0o440)
            fr.write("/etc/sudoers.d/tmp~", "tmpuser ALL=(ALL) NOPASSWD: ALL\n", 0o440)
            fr.write("/etc/sudoers.d/ops", "Cmnd_Alias SVC = /usr/bin/systemctl\nDefaults:ops !authenticate\n", 0o440)
            fr.write("/etc/group", "sudo:x:27:admin\ndocker:x:999:admin,deploy\nusers:x:100:\n")
            fr.write("/etc/passwd", "admin:x:1000:1000::/home/admin:/bin/bash\n")
            r = by_id(ha.check_sudo(fr.ctx()))
            nopw = r["sudo.nopasswd"]
            self.assertEqual(nopw["status"], "WARN")
            self.assertIn("%admins", nopw["detail"])
            self.assertIn("deploy (только перечисленные команды)", nopw["detail"])
            self.assertIn("!authenticate", nopw["detail"])
            self.assertNotIn("olduser", nopw["detail"])
            self.assertNotIn("tmpuser", nopw["detail"])
            self.assertNotIn("wheel", nopw["detail"])
            self.assertIn("docker: admin, deploy", r["sudo.groups"]["detail"])
            self.assertIn("docker", r["sudo.groups"]["hint"])
        finally:
            fr.close()

    def test_parse_sudoers_limited(self):
        nopw, noauth, inc = ha.parse_sudoers("svc ALL=(root) NOPASSWD: /bin/ls, PASSWD: ALL\n#includedir /etc/sudoers.d\n")
        self.assertEqual(nopw, [(1, "svc", False)])
        self.assertEqual(inc, [("includedir", "/etc/sudoers.d")])


class AuditFirewallTest(unittest.TestCase):
    S_OK = "enabled 1\nfailure 1\npid 545\nrate_limit 0\nbacklog_limit 8192\nlost 0\nbacklog 0\n"
    L_OK = "-w /etc/passwd -p wa -k identity\n-a always,exit -F arch=b64 -S adjtimex -F key=time-change\n-w /etc/sudoers -p wa\n"

    def audit(self, s_out, l_out, root=True):
        fr = FakeRoot()
        try:
            runner = FakeRunner({("systemctl", "is-active", "auditd"): (0, "active\n", ""),
                                 ("auditctl", "-s"): (0, s_out, ""), ("auditctl", "-l"): (0, l_out, "")})
            return by_id(ha.check_auditd(fr.ctx(runner, root=root)))
        finally:
            fr.close()

    def test_auditd(self):
        r = self.audit(self.S_OK, self.L_OK)
        self.assertEqual(r["auditd.service"]["status"], "OK")
        self.assertEqual(r["auditd.rules"]["status"], "OK")
        self.assertIn("правил загружено: 3, с ключами (-k): 2", r["auditd.rules"]["detail"])
        self.assertEqual(self.audit(self.S_OK, "No rules\n")["auditd.rules"]["status"], "WARN")
        self.assertEqual(self.audit(self.S_OK.replace("lost 0", "lost 17"), self.L_OK)["auditd.rules"]["status"], "WARN")
        self.assertEqual(self.audit(self.S_OK, self.L_OK, root=False)["auditd.rules"]["status"], "ERROR")

    NFT_DROP = """table inet filter {
	set admins {
		type ipv4_addr
		elements = { 192.0.2.10 }
	}
	chain input {
		type filter hook input priority filter; policy drop;
		ct state established,related accept
		tcp dport 22 ip saddr @admins accept
	}
	chain forward {
		type filter hook forward priority filter; policy accept;
	}
}
"""
    NFT_DEBIAN_DEFAULT = """table inet filter {
	chain input {
		type filter hook input priority filter; policy accept;
	}
	chain output {
		type filter hook output priority filter; policy accept;
	}
}
"""
    NFT_DOCKER_KUBE = """table ip nat {
	chain DOCKER {
		iifname "docker0" counter packets 0 bytes 0 return
	}
}
table ip filter {
	chain INPUT {
		type filter hook input priority filter; policy accept;
		counter packets 10 bytes 100 jump KUBE-FIREWALL
	}
	chain KUBE-FIREWALL {
		ip saddr != 127.0.0.0/8 ip daddr 127.0.0.0/8 counter packets 0 bytes 0 drop
	}
}
"""

    def test_parse_nft(self):
        self.assertTrue(ha.parse_nft(self.NFT_DROP)["filtering"])
        self.assertFalse(ha.parse_nft(self.NFT_DEBIAN_DEFAULT)["filtering"])
        self.assertFalse(ha.parse_nft(self.NFT_DOCKER_KUBE)["filtering"])
        reject = self.NFT_DEBIAN_DEFAULT.replace("policy accept;\n\t}\n\tchain output",
                                                 "policy accept;\n\t\ttcp dport 5432 reject\n\t}\n\tchain output", 1)
        self.assertTrue(ha.parse_nft(reject)["filtering"])

    def test_parse_iptables(self):
        self.assertTrue(ha.parse_iptables_input("-P INPUT DROP\n-A INPUT -i lo -j ACCEPT\n"))
        self.assertTrue(ha.parse_iptables_input("-P INPUT ACCEPT\n-A INPUT -p tcp -j REJECT --reject-with tcp-reset\n"))
        self.assertFalse(ha.parse_iptables_input("-P INPUT ACCEPT\n-A INPUT -j KUBE-FIREWALL\n"))

    def test_check_firewall(self):
        fr = FakeRoot()
        try:
            runner = FakeRunner({("nft", "list", "ruleset"): (0, self.NFT_DROP, "")})
            ctx = fr.ctx(runner)
            r = ha.check_firewall(ctx)[0]
            self.assertEqual(r["status"], "OK")
            self.assertTrue(ctx.cache["firewall"])
            runner = FakeRunner({("nft", "list", "ruleset"): (0, self.NFT_DEBIAN_DEFAULT, "")})
            self.assertEqual(ha.check_firewall(fr.ctx(runner))[0]["status"], "WARN")
            self.assertEqual(ha.check_firewall(fr.ctx(FakeRunner(tools=["nft"]), root=False))[0]["status"], "ERROR")
            runner = FakeRunner({("nft", "list", "ruleset"): (1, "", "Operation not permitted (you must be root)\n")})
            ctx = fr.ctx(runner, root=True)
            r = ha.check_firewall(ctx)[0]
            self.assertEqual(r["status"], "ERROR")
            self.assertIn("CAP_NET_ADMIN", r["hint"])
            self.assertIsNone(ctx.cache["firewall"])
            fr.write("/etc/ufw/ufw.conf", "ENABLED=yes\nLOGLEVEL=low\n")
            self.assertEqual(ha.check_firewall(fr.ctx(FakeRunner(tools=["nft"]), root=False))[0]["status"], "OK")
        finally:
            fr.close()


class PortsTest(unittest.TestCase):
    SS = """Netid State  Recv-Q Send-Q Local Address:Port  Peer Address:Port Process
udp   UNCONN 0      0          127.0.0.53%lo:53         0.0.0.0:*     users:(("systemd-resolve",pid=500,fd=13))
tcp   LISTEN 0      4096             0.0.0.0:22         0.0.0.0:*     users:(("sshd",pid=800,fd=3))
tcp   LISTEN 0      244              0.0.0.0:5432       0.0.0.0:*     users:(("postgres",pid=900,fd=6))
tcp   LISTEN 0      244                 [::]:5432          [::]:*     users:(("postgres",pid=900,fd=7))
tcp   LISTEN 0      128            127.0.0.1:15672      0.0.0.0:*     users:(("beam.smp",pid=1000,fd=40))
tcp   LISTEN 0      4096                   *:9200             *:*     users:(("docker-proxy",pid=1100,fd=4))
tcp   LISTEN 0      4096         192.0.2.5:5672       0.0.0.0:*
tcp   LISTEN 0      4096         [::ffff:127.0.0.1]:2375 [::]:*
tcp   LISTEN 0      4096             0.0.0.0:27002      0.0.0.0:*
"""

    def test_parse_ss(self):
        items = ha.parse_ss(self.SS)
        self.assertEqual(len(items), 9)
        self.assertEqual(items[0], {"proto": "udp", "addr": "127.0.0.53", "port": 53, "process": "systemd-resolve"})
        self.assertIn({"proto": "tcp", "addr": "::", "port": 5432, "process": "postgres"}, items)
        self.assertEqual(ha.scope("::ffff:127.0.0.1"), "loopback")
        self.assertEqual(ha.scope("::"), "all")
        self.assertEqual(ha.scope("*"), "all")
        self.assertEqual(ha.scope("192.0.2.5"), "addr")

    def test_proc_net(self):
        tcp = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
               "   0: 00000000:1538 00000000:0000 0A 00000000:00000000 00:00000000 00000000   999        0 1 1\n"
               "   1: 0100007F:3D98 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 2 1\n"
               "   2: 0100007F:1F90 0100007F:9C40 01 00000000:00000000 00:00000000 00000000     0        0 3 1\n")
        items = ha.parse_proc_net(tcp, "tcp")
        self.assertEqual([(i["addr"], i["port"]) for i in items], [("0.0.0.0", 5432), ("127.0.0.1", 15768)])
        tcp6 = ("  sl  local_address                         remote_address                        st\n"
                "   0: 00000000000000000000000000000000:23F0 00000000000000000000000000000000:0000 0A x\n"
                "   1: 0000000000000000FFFF00000100007F:094F 00000000000000000000000000000000:0000 0A x\n")
        items = ha.parse_proc_net(tcp6, "tcp")
        self.assertEqual(items[0]["addr"], "::")
        self.assertEqual(items[0]["port"], 9200)
        self.assertEqual(ha.scope(items[1]["addr"]), "loopback")

    def test_classify(self):
        items = ha.parse_ss(self.SS)
        st, lines, docker = ha.classify_ports(items, firewall=False)
        self.assertEqual(st, "WARN")
        self.assertTrue(docker)
        joined = "\n".join(lines)
        self.assertEqual(joined.count("*:5432"), 1)  # IPv4 и IPv6 на всех адресах одной строкой
        self.assertIn("WARN *:9200 Elasticsearch", joined)
        self.assertIn("INFO 192.0.2.5:5672", joined)
        self.assertNotIn("15672", joined)  # только локально
        self.assertNotIn("2375", joined)   # IPv4-mapped loopback
        self.assertIn("сервис интеграции RX", joined)
        st, _, _ = ha.classify_ports(items, firewall=True)
        self.assertEqual(st, "INFO")
        st, lines, _ = ha.classify_ports([{"proto": "tcp", "addr": "0.0.0.0", "port": 2375, "process": "dockerd"}], True)
        self.assertEqual(st, "CRIT")

    def test_check_ports_uses_firewall_result(self):
        fr = FakeRoot()
        try:
            runner = FakeRunner({("ss", "-H", "-tulpn"): (0, "\n".join(self.SS.splitlines()[1:]), "")})
            ctx = fr.ctx(runner)
            ctx.cache["firewall"] = False
            r = by_id(ha.check_ports(ctx))
            self.assertEqual(r["ports.exposed"]["status"], "WARN")
            self.assertIn("DOCKER-USER", r["ports.exposed"]["hint"])
            self.assertIn("tcp *:22 sshd", r["ports.list"]["detail"])
        finally:
            fr.close()


class SecretsTest(unittest.TestCase):
    SECRET = "S3cr3t-Value_42"

    def test_world_readable_secrets(self):
        with tempfile.TemporaryDirectory() as d:
            def w(name, text, mode):
                p = os.path.join(d, name)
                with open(p, "wb") as f:
                    f.write(text if isinstance(text, bytes) else text.encode("utf-8"))
                os.chmod(p, mode)
            w("config.yml", "common_config:\n  CONNECTION_STRING: 'Server=db;Port=5432;Database=rx;User Id=rx;"
                            "Password=%s'\n" % self.SECRET, 0o644)
            w("private.yml", "RABBITMQ_PASSWORD: '%s'\n" % self.SECRET, 0o600)
            w("plain.txt", "nothing here\nport: 5432\n", 0o644)
            w("rx.pfx", b"\x30\x82\x00\x00\x00binary", 0o644)
            w("other.p12", b"text-like key container without zero bytes", 0o644)
            with mock.patch.object(ha, "others_traverse", lambda p, c: True):
                r = ha.scan_dir(d)
                self.assertEqual(r["files"], 5)
                self.assertEqual(r["secret_files"], 4)
                world = sorted(os.path.basename(x[0]) for x in r["world"])
                self.assertEqual(world, ["config.yml", "other.p12", "rx.pfx"])
                self.assertTrue(any("config_password" in n for n in r["notes"]))
                fr = FakeRoot()
                try:
                    res = ha.check_secrets(fr.ctx(config_dir=[d]))
                finally:
                    fr.close()
            self.assertEqual(res[0]["status"], "WARN")
            text = ha.render_text({"host": "h", "time": "t", "root": True}, res)
            self.assertNotIn(self.SECRET, text)
            self.assertIn("config.yml права 0644", text)
            self.assertIn("файл ключа", text)

    def test_traverse_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            inner = os.path.join(d, "locked")
            os.mkdir(inner, 0o700)
            p = os.path.join(inner, "f")
            open(p, "w").close()
            self.assertFalse(ha.others_traverse(p, {}))

    def test_no_dirs_and_root_dir(self):
        fr = FakeRoot()
        try:
            self.assertEqual(ha.check_secrets(fr.ctx())[0]["status"], "SKIP")
            self.assertEqual(ha.check_secrets(fr.ctx(config_dir=["/"]))[0]["status"], "ERROR")
            self.assertEqual(ha.check_secrets(fr.ctx(config_dir=["/nonexistent-rx"]))[0]["status"], "ERROR")
        finally:
            fr.close()

    def test_history_and_pgpass(self):
        fr = FakeRoot()
        try:
            fr.write("/root/.bash_history", "ls\nexport PGPASSWORD=hunter2xyz\npsql -h db\n", 0o600)
            fr.write("/home/ops/.bash_history", "uptime\n", 0o600)
            fr.write("/home/ops/.pgpass", "db:5432:*:rx:pw\n", 0o644)
            r = ha.check_history(fr.ctx())[0]
            self.assertEqual(r["status"], "WARN")
            self.assertIn("/root/.bash_history значений: 1", r["detail"])
            self.assertIn("/home/ops/.pgpass права 0644", r["detail"])
            self.assertNotIn("hunter2xyz", r["detail"])
        finally:
            fr.close()


S_CLIENT = """CONNECTED(00000003)
depth=0 CN = rx.example.com
verify error:num=20:unable to get local issuer certificate
---
Certificate chain
 0 s:CN = rx.example.com
   i:CN = Example Issuing CA
-----BEGIN CERTIFICATE-----
MIIBszCCAVmgAwIBAgIUleaf
-----END CERTIFICATE-----
---
New, TLSv1.3, Cipher is TLS_AES_256_GCM_SHA384
Protocol: TLSv1.3
SSL-Session:
    Protocol  : TLSv1.3
    Cipher    : TLS_AES_256_GCM_SHA384
    Verify return code: 21 (unable to verify the first certificate)
---
DONE
"""
X509 = """Certificate:
    Data:
        Issuer: CN = Example Issuing CA
        Validity
            Not Before: Nov  1 00:00:00 2026 GMT
            Not After : Nov 15 12:00:00 2026 GMT
        Subject: CN = rx.example.com
        Subject Public Key Info:
            Public Key Algorithm: id-ecPublicKey
        X509v3 extensions:
            X509v3 Subject Alternative Name:
                DNS:rx.example.com, DNS:*.apps.example.com, IP Address:192.0.2.10, IP Address:2001:DB8:0:0:0:0:0:1
"""
OLD_ACCEPTED = "New, TLSv1.0, Cipher is ECDHE-RSA-AES128-SHA\nProtocol: TLSv1.1\n    Protocol  : TLSv1.1\n"
OLD_REFUSED = ("error:0A00042E:SSL routines:ssl3_read_bytes:tlsv1 alert protocol version\n"
               "New, (NONE), Cipher is (NONE)\nProtocol: TLSv1.1\n    Cipher    : 0000\n")
OLD_CLIENT = "error:0A0000BF:SSL routines:tls_setup_handshake:no protocols available\nNew, (NONE), Cipher is (NONE)\n"


class TlsTest(unittest.TestCase):
    def test_targets(self):
        self.assertEqual(ha.parse_target("rx.example.com:443"), ("rx.example.com", 443, None))
        self.assertEqual(ha.parse_target("pg://db.example.com:5432"), ("db.example.com", 5432, "postgres"))
        self.assertEqual(ha.parse_target("[2001:db8::1]:5671"), ("2001:db8::1", 5671, None))
        for bad in ("rx.example.com", "ftp://x:21", "x:70000"):
            with self.assertRaises(ValueError):
                ha.parse_target(bad)
        cmd = ha.s_client_cmd("192.0.2.10", 5432, "postgres", ["-showcerts"])
        self.assertNotIn("-servername", cmd)
        self.assertIn("postgres", cmd)

    def test_parse(self):
        sc = ha.parse_s_client(S_CLIENT)
        self.assertEqual(len(sc["certs"]), 1)
        self.assertEqual(sc["verify"], (21, "unable to verify the first certificate"))
        self.assertEqual(sc["protocol"], "TLSv1.3")
        x = ha.parse_x509_text(X509)
        self.assertEqual(x["not_after"], datetime.datetime(2026, 11, 15, 12, 0, 0))
        self.assertEqual(x["subject"], "CN = rx.example.com")
        self.assertEqual(x["issuer"], "CN = Example Issuing CA")
        self.assertTrue(ha.host_in_san("rx.example.com", x["san"]))
        self.assertTrue(ha.host_in_san("crm.apps.example.com", x["san"]))
        self.assertFalse(ha.host_in_san("a.b.apps.example.com", x["san"]))
        self.assertTrue(ha.host_in_san("192.0.2.10", x["san"]))
        self.assertTrue(ha.host_in_san("2001:db8::1", x["san"]))
        self.assertFalse(ha.host_in_san("db.example.com", x["san"]))

    def test_old_protocol(self):
        self.assertEqual(ha.old_protocol(OLD_ACCEPTED), "accepted")
        self.assertEqual(ha.old_protocol(OLD_REFUSED), "refused")
        self.assertEqual(ha.old_protocol(OLD_CLIENT), "unsupported")

    def test_verdict(self):
        sc = ha.parse_s_client(S_CLIENT)
        x = ha.parse_x509_text(X509)
        st, d = ha.tls_verdict("rx.example.com", x, sc, NOW, 30, 7)  # 10 дней, цепочка неполная
        self.assertEqual(st, "WARN")
        joined = "\n".join(d)
        self.assertIn("до конца срока 10 дн.", joined)
        self.assertIn("без промежуточных", joined)
        st, _ = ha.tls_verdict("rx.example.com", x, sc, datetime.datetime(2026, 11, 12), 30, 7)
        self.assertEqual(st, "CRIT")
        st, d = ha.tls_verdict("rx.example.com", x, sc, datetime.datetime(2026, 12, 1), 30, 7)
        self.assertEqual(st, "CRIT")
        self.assertIn("истёк 15 дн. назад", "\n".join(d))
        good = dict(sc, verify=(0, "ok"), certs=sc["certs"] * 2)
        st, _ = ha.tls_verdict("rx.example.com", x, good, datetime.datetime(2026, 9, 1), 30, 7)
        self.assertEqual(st, "OK")

    def test_check_tls_with_fake_openssl(self):
        def s_client(cmd, stdin):
            if "-tls1_1" in cmd:
                return 1, OLD_ACCEPTED, ""
            if "-tls1" in cmd:
                return 1, OLD_REFUSED, ""
            return 0, S_CLIENT, ""
        runner = FakeRunner({"openssl": lambda cmd, stdin: (0, X509, "") if cmd[1] == "x509" else s_client(cmd, stdin)})
        fr = FakeRoot()
        try:
            r = ha.check_tls(fr.ctx(runner, tls=["rx.example.com:443", "bad"]))
        finally:
            fr.close()
        self.assertEqual(r[0]["id"], "tls:rx.example.com:443")
        self.assertEqual(r[0]["status"], "WARN")
        self.assertIn("принимает устаревшие протоколы: TLS 1.1", r[0]["detail"])
        self.assertEqual(r[0]["questions"], ["q6"])
        self.assertEqual(r[1]["status"], "ERROR")


class MiscTest(unittest.TestCase):
    def test_time(self):
        fr = FakeRoot()
        try:
            runner = FakeRunner({("timedatectl", "show"): (0, "Timezone=UTC\nNTP=yes\nNTPSynchronized=no\n", "")})
            self.assertEqual(ha.check_time(fr.ctx(runner))[0]["status"], "WARN")
            runner = FakeRunner({("chronyc", "-n", "tracking"): (0, "Reference ID    : C0000201 (192.0.2.1)\n"
                                 "System time     : 0.000012345 seconds slow of NTP time\nLeap status     : Normal\n", "")})
            r = ha.check_time(fr.ctx(runner))[0]
            self.assertEqual(r["status"], "OK")
            self.assertIn("-0.000", r["detail"])
            self.assertEqual(ha.check_time(fr.ctx(FakeRunner()))[0]["status"], "ERROR")
            runner = FakeRunner({("chronyc", "-n", "tracking"): (1, "", "506 Cannot talk to daemon\n")})
            r = ha.check_time(fr.ctx(runner))[0]
            self.assertEqual(r["status"], "WARN")
            self.assertIn("Cannot talk to daemon", r["detail"])
        finally:
            fr.close()

    def test_mac_and_astra(self):
        fr = FakeRoot()
        try:
            fr.write("/sys/module/apparmor/parameters/enabled", "Y\n")
            fr.write("/sys/kernel/security/lsm", "lockdown,capability,landlock,yama,apparmor\n")
            r = by_id(ha.check_mac(fr.ctx()))
            self.assertEqual(r["mac"]["status"], "OK")
            fr.write("/etc/astra_version", "1.7.5\n")
            runner = FakeRunner({("astra-mic-control", "status"): (0, "INACTIVE\n", ""),
                                 ("astra-modeswitch", "get"): (0, "1\n", "")})
            r = by_id(ha.check_mac(fr.ctx(runner)))
            self.assertEqual(r["mac.astra"]["status"], "WARN")
            self.assertIn("astra-modeswitch get: 1", r["mac.astra"]["detail"])
            self.assertTrue(ha.astra_state("АКТИВНО"))
            self.assertFalse(ha.astra_state("НЕАКТИВНО"))
            self.assertIsNone(ha.astra_state("2"))
        finally:
            fr.close()

    def test_containers(self):
        items = [
            {"Name": "/rx-web", "Config": {"User": "", "Image": "registry.example.com/rx/web:25.2"},
             "HostConfig": {"Privileged": False, "NetworkMode": "bridge"}, "Mounts": []},
            {"Name": "/agent", "Config": {"User": "1000", "Image": "vector"},
             "HostConfig": {"Privileged": True, "PidMode": "host", "CapAdd": ["CAP_SYS_ADMIN"],
                            "SecurityOpt": ["seccomp=unconfined"]},
             "Mounts": [{"Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock"}]},
            {"Name": "/node", "Config": {"Image": "quay.io/prometheus/node-exporter:v1.8.2"},
             "HostConfig": {"NetworkMode": "host"}},
        ]
        warn, info = ha.analyze_containers(items)
        joined = "\n".join(warn)
        for s in ("agent: запущен с --privileged", "agent: внутри сокет", "pid host", "SYS_ADMIN", "seccomp=unconfined"):
            self.assertIn(s, joined)
        self.assertNotIn("rx-web", joined)
        info = "\n".join(info)
        self.assertIn("node: сеть хоста", info)
        self.assertIn("от root работают: 2 из 3", info)
        self.assertIn("docker.io 1", info)
        self.assertIn("quay.io 1", info)
        self.assertIn("без версии или с latest: agent", info)
        self.assertEqual(ha.image_registry("postgres:16"), "docker.io")
        self.assertEqual(ha.image_registry("registry.example.com:5000/a/b:1"), "registry.example.com:5000")

    def test_container_runtime_errors(self):
        fr = FakeRoot()
        try:
            runner = FakeRunner({("docker", "ps", "-q"): (1, "", "permission denied while trying to connect to the "
                                                                "Docker daemon socket at unix:///var/run/docker.sock")})
            self.assertEqual(ha.check_containers(fr.ctx(runner, root=False))[0]["status"], "ERROR")
            self.assertEqual(ha.check_containers(fr.ctx(FakeRunner()))[0]["status"], "SKIP")
        finally:
            fr.close()

    def test_repos(self):
        t, h = ha.parse_apt_list("deb [trusted=yes arch=amd64] http://mirror.example.com/debian trixie main\n"
                                 "deb http://deb.debian.org/debian trixie main\n# deb [trusted=yes] http://old/ x\n")
        self.assertEqual(len(t), 1)
        self.assertEqual(h, {"mirror.example.com", "deb.debian.org"})
        t, h = ha.parse_deb822("Types: deb\nURIs: https://repo.example.com/apt\nSuites: x\nTrusted: yes\n\n"
                               "Types: deb\nURIs: http://deb.debian.org/debian\nEnabled: no\nTrusted: yes\n")
        self.assertEqual(len(t), 1)
        self.assertEqual(h, {"repo.example.com"})
        bad, h = ha.parse_yum_repo("[base]\nbaseurl=https://mirror.example.com/el9/\ngpgcheck=1\n"
                                   "[local]\nbaseurl=http://repo.example.com/x\ngpgcheck=0\n"
                                   "[off]\nenabled=0\ngpgcheck=0\n")
        self.assertEqual(bad, ["local"])
        self.assertEqual(h, {"mirror.example.com", "repo.example.com"})
        fr = FakeRoot()
        try:
            fr.write("/etc/apt/sources.list", "deb http://deb.debian.org/debian trixie main\n")
            fr.write("/etc/apt/apt.conf.d/99insecure", 'APT::Get::AllowUnauthenticated "true";\n')
            r = ha.check_repos(fr.ctx())[0]
            self.assertEqual(r["status"], "WARN")
            self.assertIn("AllowUnauthenticated", r["detail"])
        finally:
            fr.close()

    def test_updates(self):
        fr = FakeRoot()
        try:
            p = fr.write("/var/lib/dpkg/info/libc6.list", "/lib\n")
            old = (NOW - datetime.datetime(1970, 1, 1)).total_seconds() - 200 * 86400
            os.utime(p, (old, old))
            r = ha.check_updates(fr.ctx())[0]
            self.assertEqual(r["status"], "WARN")
            self.assertIn("200 дн. назад", r["detail"])
            fresh = old + 190 * 86400
            os.utime(p, (fresh, fresh))
            fr.write("/run/reboot-required", "")
            os.utime(p, (fresh, fresh))
            r = ha.check_updates(fr.ctx())[0]
            self.assertEqual(r["status"], "OK")
            self.assertIn("reboot-required", r["detail"])
        finally:
            fr.close()

    def test_logship(self):
        fr = FakeRoot()
        try:
            self.assertEqual(ha.check_logship(fr.ctx())[0]["status"], "WARN")
            fr.write("/etc/rsyslog.d/90-remote.conf", "# comment @@x\n*.* @@logs.example.com:514\n")
            r = ha.check_logship(fr.ctx())[0]
            self.assertEqual(r["status"], "OK")
            self.assertIn("logs.example.com", r["detail"])
            self.assertEqual(ha.rsyslog_forwards('action(type="omfwd" target="192.0.2.7" port="514")'),
                             ['action(type="omfwd" target="192.0.2.7" port="514")'])
        finally:
            fr.close()


class CliTest(unittest.TestCase):
    def run_cli(self, *args):
        p = subprocess.run([sys.executable, "-B", SCRIPT] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8")

    def test_help_list_errors(self):
        rc, out, _ = self.run_cli("--help")
        self.assertEqual(rc, 0)
        self.assertIn("Код возврата", out)
        rc, out, _ = self.run_cli("--list")
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.strip().splitlines()), len(ha.CHECKS))
        self.assertEqual(self.run_cli("--bogus")[0], 2)
        self.assertEqual(self.run_cli("--only", "nosuch")[0], 2)
        self.assertEqual(self.run_cli("--tls-warn-days", "x")[0], 2)

    def test_json_shape(self):
        rc, out, _ = self.run_cli("--only", "system", "--json")
        self.assertEqual(rc, 0)
        doc = json.loads(out)
        for k in ("tool", "version", "host", "time", "checks", "summary"):
            self.assertIn(k, doc)
        self.assertEqual(doc["tool"], "host_audit")
        self.assertEqual(doc["checks"][0]["id"], "system")
        for k in ("id", "title", "status", "detail", "hint", "questions"):
            self.assertIn(k, doc["checks"][0])
        self.assertEqual(doc["summary"], {"INFO": 1})

    def test_c_locale_output(self):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C", "LC_ALL": "C"}
        p = subprocess.run([sys.executable, "-B", SCRIPT, "--only", "system"], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, env=env)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn("запуск от root", p.stdout.decode("utf-8"))

    def test_no_pycache_written(self):
        self.run_cli("--only", "system")
        self.assertFalse(os.path.exists(os.path.join(SCRIPTS, "__pycache__", "host_audit.cpython-%d%d.pyc"
                                                     % sys.version_info[:2])))

    def test_exit_code_and_safety_net(self):
        r = [ha.res("a", "a", "OK"), ha.res("b", "b", "ERROR")]
        self.assertEqual(ha.exit_code(r), 2)
        self.assertEqual(ha.exit_code(r + [ha.res("c", "c", "WARN")]), 1)
        self.assertEqual(ha.exit_code([ha.res("a", "a", "INFO"), ha.res("b", "b", "SKIP")]), 0)
        text = ha.render_text({"host": "h", "time": "t", "root": False},
                              [ha.res("x", "x", "WARN", "PasswordAuthentication yes\nDB_PASSWORD=Leak123")])
        out = ha.safe(text)
        self.assertNotIn("Leak123", out)
        self.assertIn("PasswordAuthentication yes", out)


if __name__ == "__main__":
    unittest.main()
