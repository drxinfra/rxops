"""Проверки маскировщика секретов: python3 -m unittest discover -s tests"""
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))
import mask_secrets  # noqa: E402

M = mask_secrets.MASK


def masked(text):
    return mask_secrets.mask(text)[0]


class MaskTest(unittest.TestCase):
    def hidden(self, text, *secrets):
        out = masked(text)
        for s in secrets:
            self.assertNotIn(s, out, out)
        self.assertIn(M, out, out)
        return out

    def kept(self, text):
        self.assertEqual(masked(text), text)

    def test_yaml_and_env(self):
        out = self.hidden(
            "variables:\n"
            "  DATABASE_ENGINE: 'postgres'\n"
            "  CONNECTION_STRING: 'Host=db;Port=5432;Database=rx;Username=rx;Password=S3cr3t!x'\n"
            "  RABBITMQ_PASSWORD: \"Qwerty_123\"\n"
            "  STORAGE_SECRET_KEY: abcDEF123456  # ключ хранилища\n",
            "S3cr3t!x", "Qwerty_123", "abcDEF123456")
        self.assertIn("DATABASE_ENGINE: 'postgres'", out)
        self.assertIn("Username=rx;", out)
        self.assertIn("# ключ хранилища", out)
        self.hidden("export PGPASSWORD=hunter2\nPOSTGRES_PASSWORD=pa55\n", "hunter2", "pa55")

    def test_not_secrets(self):
        self.kept("password_policy: strict\nPASSWORD_MIN_LENGTH: 12\ntoken_lifetime: 3600\n"
                  "max_tokens: 2048\nkeystore_path: /etc/ssl/ks.jks\nTOKEN_URL: https://idp/token\n")
        self.kept("RABBITMQ_PASSWORD: ${RABBITMQ_PASSWORD}\nDB_PASSWORD: '{{ vault_db_password }}'\n"
                  "password: CHANGE_ME\nsecret:\n  name: rx-db\n")
        self.kept("select usename, passwd is not null from pg_shadow;\n")

    def test_json_xml(self):
        self.hidden('{"user": "rx", "password": "p@ss", "token_ttl": 60}', "p@ss")
        self.assertIn('"token_ttl": 60', masked('{"password": "x1", "token_ttl": 60}'))
        self.hidden('<var name="DATABASE_PASSWORD" value="Pa$$w0rd" />', "Pa$$w0rd")
        self.hidden('<add key="ServicePassword" value="zzz111"/>', "zzz111")
        self.hidden("<Password>topsecret</Password>", "topsecret")

    def test_conn_url_auth(self):
        self.hidden("Server=sql01;Database=rx;User Id=sa;Pwd=Sa_2024;", "Sa_2024")
        self.hidden("amqp://rx:guest123@mq:5672/", "guest123")
        out = self.hidden("curl -u elastic:ElAsTiC https://es:9200/_cluster/health", "ElAsTiC")
        self.assertIn("-u elastic:", out)
        self.hidden("Authorization: Basic cnhfdXNlcjpweA==", "cnhfdXNlcjpweA==")
        self.hidden("psql --password=abc123 -h db", "abc123")

    def test_tokens_pem_k8s(self):
        # Образцы собираются из частей, чтобы в файле не было строк, похожих на настоящие секреты.
        jwt = ".".join(["eyJ" + "hbGciOiJIUzI1NiJ9", "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0", "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"])
        self.hidden("token in log: " + jwt, jwt)
        gl = "glp" + "at-ABCDEFGHIJKLMNOPQRSTUV"
        self.hidden(gl, gl)
        kind = "RSA " + "PRIVATE KEY"
        pem = "-----BEGIN %s-----\nMIIEpAIBAAKCAQEA1\n-----END %s-----" % (kind, kind)
        self.hidden("key:\n" + pem, "MIIEpAIBAAKCAQEA1")
        out = self.hidden("apiVersion: v1\nkind: Secret\ndata:\n  db-user: cng=\n  db-pass: c2VjcmV0\n"
                          "metadata:\n  name: rx\n", "cng=", "c2VjcmV0")
        self.assertIn("name: rx", out)

    def test_inline_sql_mc(self):
        self.hidden("cd /backup && PGPASSWORD=Qx7pass pg_dump -h db rx", "Qx7pass")
        self.hidden("env AWS_SECRET_ACCESS_KEY=abcSECRET123 wal-g backup-push", "abcSECRET123")
        self.hidden("ALTER ROLE rx WITH PASSWORD 'p@ss''w';", "p@ss")
        out = self.hidden("mc alias set rx https://s3.example AKIAKEY0001 SeCrEt0001", "AKIAKEY0001", "SeCrEt0001")
        self.assertIn("https://s3.example", out)
        self.kept("PGPASSWORD=$PGPASS pg_dump rx\n")
        out = self.hidden("PGPASSWORD=Zz9 pg_dump -h db rx\n", "Zz9")
        self.assertIn("pg_dump -h db rx", out)

    def test_cli(self):
        script = os.path.join(ROOT, "shared", "mask_secrets.py")
        p = subprocess.run([sys.executable, script], input=b"PASSWORD=abc\nport=5432\n",
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(p.returncode, 0)
        self.assertIn(M, p.stdout.decode())
        self.assertIn("port=5432", p.stdout.decode())
        p = subprocess.run([sys.executable, script, "--check"], input=b"PASSWORD=abc\n",
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(p.returncode, 1)


if __name__ == "__main__":
    unittest.main()
