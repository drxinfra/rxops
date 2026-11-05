"""Проверки mq_check.py (скил rx-rabbitmq) без сети: python3 -m unittest discover -s tests

Фикстуры записаны с тестового RabbitMQ 3.13.7 в контейнере (очередь индексирования без
потребителя, потребитель без подтверждений, серия очередей с GUID, кворумная очередь,
политика ha-all) и сокращены; адреса заменены на документационные 192.0.2.0/24.
"""
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from urllib.parse import unquote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "skills", "rx-rabbitmq", "scripts"))
import mq_check as m  # noqa: E402

FX = json.loads(r"""{"overview":{"rabbitmq_version":"3.13.7","erlang_version":"26.2.5.16","cluster_name":"rabbit@rxops-mq-313","node":"rabbit@localhost","listeners":[{"node":"rabbit@localhost","protocol":"amqp","port":5672},{"node":"rabbit@localhost","protocol":"clustering","port":25672},{"node":"rabbit@localhost","protocol":"http","port":15672},{"node":"rabbit@localhost","protocol":"http/prometheus","port":15692}],"churn_rates":{"connection_closed":1282,"queue_deleted":0,"queue_created":48,"channel_created":1203,"channel_closed":1200,"queue_declared":48,"connection_created":1285}},"nodes":[{"partitions":[],"fd_total":1048576,"sockets_total":943629,"mem_limit":462007500,"mem_alarm":false,"disk_free_limit":50000000,"disk_free_alarm":false,"proc_total":1048576,"uptime":608658,"name":"rabbit@localhost","type":"disc","running":true,"mem_used":157569024,"fd_used":54,"sockets_used":3,"proc_used":678,"disk_free":540805513216}],"vhosts":[{"name":"/"},{"messages":0,"name":"ario_host"},{"messages":3000,"name":"rxhost"}],"queues_rxhost":[{"auto_delete":false,"consumer_capacity":0,"consumers":0,"durable":true,"exclusive":false,"idle_since":"2026-11-05T17:13:33.799+00:00","message_stats":{"publish":1200},"messages":1200,"messages_ready":1200,"messages_unacknowledged":0,"name":"directumrx_RxExchange_indexing_service","policy":"ha-all","state":"running","type":"classic","vhost":"rxhost"},{"auto_delete":false,"consumer_capacity":0,"consumers":0,"durable":true,"exclusive":false,"idle_since":"2026-11-05T17:13:33.131+00:00","messages":0,"messages_ready":0,"messages_unacknowledged":0,"name":"directumrx_RxExchange_indexing_service_initial_bulk","policy":"ha-all","state":"running","type":"classic","vhost":"rxhost"},{"auto_delete":false,"consumer_capacity":0,"consumers":0,"durable":true,"exclusive":false,"idle_since":"2026-11-05T17:13:33.344+00:00","messages":0,"messages_ready":0,"messages_unacknowledged":0,"name":"test_hextail_0a1b2c3d4e","policy":"ha-all","state":"running","type":"classic","vhost":"rxhost"},{"auto_delete":false,"consumer_capacity":0,"consumers":0,"durable":true,"exclusive":false,"idle_since":"2026-11-05T17:13:34.373+00:00","messages":0,"messages_ready":0,"messages_unacknowledged":0,"name":"test_idle_queue","policy":"ha-all","state":"running","type":"classic","vhost":"rxhost"},{"auto_delete":false,"consumer_capacity":0,"consumers":0,"durable":true,"exclusive":false,"idle_since":"2026-11-05T17:13:33.126+00:00","messages":0,"messages_ready":0,"messages_unacknowledged":0,"name":"test_instance_cache_052bf4c1-34c4-4983-bb5c-ab94a8364638","policy":"ha-all","state":"running","type":"classic","vhost":"rxhost"},{"auto_delete":false,"consumer_capacity":0,"consumers":0,"durable":true,"exclusive":false,"idle_since":"2026-11-05T17:13:33.711+00:00","messages":0,"messages_ready":0,"messages_unacknowledged":0,"name":"test_instance_cache_07a4bdd9-ce6e-4d18-a1a0-7348ee50a522","policy":"ha-all","state":"running","type":"classic","vhost":"rxhost"},{"auto_delete":false,"consumer_capacity":0,"consumers":0,"durable":true,"exclusive":false,"idle_since":"2026-11-05T17:13:33.186+00:00","messages":0,"messages_ready":0,"messages_unacknowledged":0,"name":"test_instance_cache_08507f27-6563-4153-b0f5-b7025472553c","policy":"ha-all","state":"running","type":"classic","vhost":"rxhost"},{"auto_delete":false,"consumer_capacity":1.0,"consumers":1,"durable":true,"exclusive":false,"idle_since":"2026-11-05T17:13:33.582+00:00","messages":0,"messages_ready":0,"messages_unacknowledged":0,"name":"test_instance_cache_0e026daa-cf84-4bee-a4d9-6d20166d8f22","policy":"ha-all","state":"running","type":"classic","vhost":"rxhost"},{"auto_delete":false,"consumer_capacity":0,"consumers":0,"durable":true,"exclusive":false,"idle_since":"2026-11-05T17:13:34.962+00:00","messages":0,"messages_ready":0,"messages_unacknowledged":0,"name":"test_instance_cache_16f85cfa-5207-4b0e-ad3e-bdd30b2381ff","policy":"ha-all","state":"running","type":"classic","vhost":"rxhost"},{"auto_delete":false,"consumer_capacity":0,"consumers":0,"durable":true,"exclusive":false,"idle_since":"2026-11-05T17:13:33.344+00:00","messages":0,"messages_ready":0,"messages_unacknowledged":0,"name":"test_job_6c315097-628d-441f-86c0-771cb9aa12e9","policy":"ha-all","state":"running","type":"classic","vhost":"rxhost"},{"auto_delete":false,"consumer_capacity":0,"consumers":0,"durable":true,"exclusive":false,"leader":"rabbit@localhost","members":["rabbit@localhost"],"messages":0,"messages_ready":0,"messages_unacknowledged":0,"name":"test_quorum","online":["rabbit@localhost"],"policy":null,"state":"running","type":"quorum","vhost":"rxhost"},{"auto_delete":false,"consumer_capacity":0.005543570969624327,"consumers":1,"durable":true,"exclusive":false,"message_stats":{"ack":0,"deliver_get":10,"publish":1805},"messages":1800,"messages_ready":1790,"messages_unacknowledged":10,"name":"test_work_queue","policy":"ha-all","state":"running","type":"classic","vhost":"rxhost"}],"connections":[{"channels":1,"client_properties":{"connection_name":"testhost.WebServer.Host.PublishersConnection","product":"Pika Python Client Library"},"name":"192.0.2.13:44054 -> 192.0.2.10:5672","node":"rabbit@localhost","peer_host":"192.0.2.13","ssl":false,"ssl_protocol":null,"state":"running","user":"rx","user_provided_name":"testhost.WebServer.Host.PublishersConnection","vhost":"rxhost"},{"channels":1,"client_properties":{"connection_name":"testhost.EntityCache.Host.ConsumersConnection","product":"Pika Python Client Library"},"name":"192.0.2.13:44056 -> 192.0.2.10:5672","node":"rabbit@localhost","peer_host":"192.0.2.13","ssl":false,"ssl_protocol":null,"state":"running","user":"rx","user_provided_name":"testhost.EntityCache.Host.ConsumersConnection","vhost":"rxhost"},{"channels":1,"client_properties":{"connection_name":"testhost.PreviewService.Host.ConsumersConnection","product":"Pika Python Client Library"},"name":"192.0.2.13:44066 -> 192.0.2.10:5672","node":"rabbit@localhost","peer_host":"192.0.2.13","ssl":false,"ssl_protocol":null,"state":"running","user":"rx","user_provided_name":"testhost.PreviewService.Host.ConsumersConnection","vhost":"rxhost"}],"consumers":[{"queue":{"name":"test_instance_cache_0e026daa-cf84-4bee-a4d9-6d20166d8f22","vhost":"rxhost"},"channel_details":{"connection_name":"192.0.2.13:44056 -> 192.0.2.10:5672"},"prefetch_count":0,"consumer_timeout":1800000},{"queue":{"name":"test_work_queue","vhost":"rxhost"},"channel_details":{"connection_name":"192.0.2.13:44066 -> 192.0.2.10:5672"},"prefetch_count":10,"consumer_timeout":1800000}],"policies":[{"vhost":"rxhost","name":"ha-all","pattern":"","apply-to":"all","definition":{"ha-mode":"all","ha-sync-mode":"automatic"},"priority":0}]}""")
FXC = json.loads(r"""{"status":{"alarms":[],"disk_free":540805484544,"disk_free_limit":50000000,"file_descriptors":{"total_used":8,"total_limit":1048479,"sockets_used":3,"sockets_limit":943629},"processes":{"used":680,"limit":1048576},"memory":{"total":{"erlang":93105678,"rss":158142464,"allocated":78588752},"strategy":"rss"},"vm_memory_high_watermark_limit":462007500,"vm_memory_calculation_strategy":"rss","rabbitmq_version":"3.13.7","erlang_version":"Erlang/OTP 26 [erts-14.2.5.12] [source] [64-bit] [smp:3:2] [ds:3:2:10] [async-threads:1] [jit]","uptime":633},"cluster_status":{"alarms":[],"cluster_name":"rabbit@rxops-mq-313","disk_nodes":["rabbit@localhost"],"ram_nodes":[],"running_nodes":["rabbit@localhost"],"versions":{"rabbit@localhost":{"erlang_version":"26.2.5.16","rabbitmq_name":"RabbitMQ","rabbitmq_version":"3.13.7"}},"partitions":{}},"connections":[{"name":"192.0.2.13:44054 -> 192.0.2.10:5672","user":"rx","vhost":"rxhost","peer_host":[0,0,0,0,0,65535,49152,525],"ssl":false,"ssl_protocol":"","channels":1,"state":"running","client_properties":[["product","longstr","Pika Python Client Library"],["connection_name","longstr","testhost.WebServer.Host.PublishersConnection"]]},{"name":"192.0.2.13:44066 -> 192.0.2.10:5672","user":"rx","vhost":"rxhost","peer_host":[0,0,0,0,0,65535,49152,525],"ssl":false,"ssl_protocol":"","channels":1,"state":"running","client_properties":[["product","longstr","Pika Python Client Library"],["connection_name","longstr","testhost.PreviewService.Host.ConsumersConnection"]]},{"name":"192.0.2.13:44056 -> 192.0.2.10:5672","user":"rx","vhost":"rxhost","peer_host":[0,0,0,0,0,65535,49152,525],"ssl":false,"ssl_protocol":"","channels":1,"state":"running","client_properties":[["product","longstr","Pika Python Client Library"],["connection_name","longstr","testhost.EntityCache.Host.ConsumersConnection"]]}],"policies":[{"vhost":"rxhost","name":"ha-all","pattern":"","apply-to":"all","definition":"{\"ha-mode\":\"all\",\"ha-sync-mode\":\"automatic\"}","priority":0}],"members":[{"name":"test_quorum","type":"quorum","members":["rabbit@localhost"],"online":["rabbit@localhost"],"leader":"rabbit@localhost"}]}""")
TABLE = """Timeout: 60.0 seconds ...
Listing queues for vhost rxhost ...
name	messages	messages_ready	messages_unacknowledged	consumers
test_quorum	0	0	0	0
test_work_queue	1916	1906	10	1
test_instance_cache_0e026daa-cf84-4bee-a4d9-6d20166d8f22	0	0	0	1
test_idle_queue	0	0	0	0
directumrx_RxExchange_indexing_service_initial_bulk	0	0	0	0
test_instance_cache_e270df42-f30b-4303-8aa3-f3319b4fabc0	0	0	0	0
test_instance_cache_fe4d5dbd-a105-4368-8287-f80cb0a991ac	0	0	0	0
test_instance_cache_81b87663-e430-41a0-bc87-ebf6669ae6a6	0	0	0	0
directumrx_RxExchange_indexing_service	1200	1200	0	0
"""


def fake_api(fx=None, alarms=None, version=None, hidden=("/", "ario_host")):
    fx = copy.deepcopy(fx or FX)
    if version:
        fx["overview"]["rabbitmq_version"] = version

    def get(path, allow=()):
        p = path.split("?")[0]
        if p == "/api/overview":
            return fx["overview"]
        if p == "/api/nodes":
            return fx["nodes"]
        if p == "/api/health/checks/alarms":
            if alarms:
                if 503 in allow:
                    return {"status": "failed", "alarms": alarms}
                raise m.HttpFail(503, p)
            return {"status": "ok"}
        if p == "/api/vhosts":
            return fx["vhosts"]
        if p.startswith("/api/queues/"):
            vh = unquote(p[len("/api/queues/"):])
            if vh in hidden:
                raise m.HttpFail(401, p)
            return [q for q in fx["queues_rxhost"] if q["vhost"] == vh]
        if p == "/api/connections":
            return fx["connections"]
        if p == "/api/consumers":
            return fx["consumers"]
        if p == "/api/policies":
            return fx["policies"]
        if p == "/api/users":
            raise m.HttpFail(401, p)
        raise AssertionError("неожиданный запрос " + path)
    return get


def by_check(F, check, level=None):
    return [i for i in F.items if i["check"] == check and (level is None or i["level"] == level)]


class ApiSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.snap = m.collect_api(fake_api())
        self.F = m.analyze(self.snap)

    def test_queue_without_consumer_points_to_indexing_service(self):
        crit = by_check(self.F, "no_consumers", "CRIT")
        self.assertEqual(len(crit), 1)
        self.assertIn("directumrx_RxExchange_indexing_service: 1200", crit[0]["details"][0])
        self.assertIn("IndexingService", crit[0]["details"][0])
        self.assertEqual(self.F.worst(), "CRIT")

    def test_default_disk_limit_is_warned(self):
        w = by_check(self.F, "disk", "WARN")
        self.assertTrue(w and "Порог диска мал" in w[0]["summary"], w)

    def test_temp_queue_series_grouped_by_shape(self):
        info = by_check(self.F, "temp_queues")[0]
        self.assertEqual(info["level"], "INFO")
        joined = "\n".join(info["details"])
        self.assertIn("test_instance_cache_<guid>: всего 5, с потребителями 1, без потребителей 4", joined)

    def test_hidden_vhosts_reported(self):
        acc = by_check(self.F, "access")
        names = " ".join(i["summary"] for i in acc)
        self.assertIn("ario_host", names)
        self.assertTrue(all(i["level"] == "INFO" for i in acc))   # сообщений там 0 или неизвестно

    def test_hidden_vhost_with_messages_is_warning(self):
        fx = copy.deepcopy(FX)
        for v in fx["vhosts"]:
            if v["name"] == "ario_host":
                v["messages"] = 42
        F = m.analyze(m.collect_api(fake_api(fx)))
        w = by_check(F, "access", "WARN")
        self.assertTrue(w and "42" in w[0]["summary"])

    def test_quorum_and_connections(self):
        self.assertEqual(by_check(self.F, "quorum")[0]["level"], "OK")
        conn = by_check(self.F, "connections")[0]
        self.assertIn("Соединений: 3", conn["summary"])
        self.assertIn("без TLS: 3", conn["summary"])

    def test_consumer_names_resolved_from_connections(self):
        q = [x for x in self.snap["queues"] if x["name"] == "test_work_queue"][0]
        self.assertEqual(m.consumers_of(q, self.snap), ["testhost.PreviewService.Host.ConsumersConnection"])

    def test_ha_policy_info_on_313_and_warn_on_4(self):
        self.assertEqual(by_check(self.F, "policies")[0]["level"], "INFO")
        F4 = m.analyze(m.collect_api(fake_api(version="4.4.0")))
        w = by_check(F4, "policies", "WARN")
        self.assertTrue(w and "не действует" in w[0]["summary"])

    def test_alarm_from_health_endpoint(self):
        snap = m.collect_api(fake_api(alarms=[{"node": "rabbit@localhost", "resource": "disk space"}]))
        F = m.analyze(snap)
        crit = by_check(F, "alarms", "CRIT")
        self.assertTrue(crit and "диску" in crit[0]["details"][0])


class GrowthTest(unittest.TestCase):
    def test_growth_stuck_unacked_and_storm(self):
        prev = m.collect_api(fake_api())
        cur = copy.deepcopy(prev)
        cur["taken_at"] = prev["taken_at"] + 60
        for q in cur["queues"]:
            if q["name"] == "test_work_queue":
                q["messages"] += 300
                q["ready"] += 300
        cur["churn"]["connection_created"] += 120
        cur["consumers"] = prev["consumers"]
        F = m.analyze(cur, prev=prev)
        g = by_check(F, "growth", "WARN")
        self.assertTrue(g and "test_work_queue: +300" in g[0]["details"][0], g)
        self.assertIn("PreviewService", g[0]["details"][0])
        stuck = by_check(F, "unacked", "WARN")
        self.assertTrue(stuck and "в обработке 10" in stuck[0]["details"][0])
        self.assertIn("consumer_timeout у потребителей: 30 мин", "\n".join(stuck[0]["details"]))
        storm = [i for i in by_check(F, "connections", "WARN") if "Шторм" in i["summary"]]
        self.assertTrue(storm, F.items)

    def test_compare_with_saved_json(self):
        snap = m.collect_api(fake_api())
        F = m.analyze(snap)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "before.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(m.snapshot_json(snap, F, "test"), f, ensure_ascii=False)
            prev = m.load_prev(path)
        prev["taken_at"] -= 120
        F2 = m.analyze(snap, prev=prev)
        self.assertEqual(by_check(F2, "growth")[0]["level"], "OK")


class TempAndMatchTest(unittest.TestCase):
    def test_leak_threshold(self):
        fx = copy.deepcopy(FX)
        tpl = [q for q in fx["queues_rxhost"] if q["name"].startswith("test_instance_cache") and not q["consumers"]][0]
        for i in range(1200):
            q = dict(tpl, name="test_instance_cache_%08x-0000-4000-8000-%012x" % (i, i))
            fx["queues_rxhost"].append(q)
        F = m.analyze(m.collect_api(fake_api(fx)))
        w = by_check(F, "temp_queues", "WARN")
        self.assertTrue(w and "порог 1000" in w[0]["summary"], w)

    def test_single_guid_queue_with_messages_warns(self):
        fx = copy.deepcopy(FX)
        for q in fx["queues_rxhost"]:
            if q["name"].startswith("test_job_"):
                q["messages"] = q["messages_ready"] = 7
        F = m.analyze(m.collect_api(fake_api(fx)))
        w = [i for i in by_check(F, "temp_queues", "WARN") if "Одиночные" in i["summary"]]
        self.assertTrue(w and ": 7" in w[0]["details"][0], F.items)
        # остаток в серии копий не тревожит
        fx = copy.deepcopy(FX)
        for q in fx["queues_rxhost"]:
            if q["name"].startswith("test_instance_cache") and not q["consumers"]:
                q["messages"] = 2
                break
        F = m.analyze(m.collect_api(fake_api(fx)))
        self.assertFalse(by_check(F, "temp_queues", "WARN"))

    def test_shape_and_kind(self):
        self.assertEqual(m.shape("a_0e026daa-cf84-4bee-a4d9-6d20166d8f22"), "a_<guid>")
        self.assertEqual(m.temp_kind("cache_0a1b2c3d4e"), "hex")
        self.assertEqual(m.temp_kind("jobs_20261105"), "")         # дата, а не хеш
        self.assertEqual(m.temp_kind("amq.gen-JzTY20BRgKO"), "server")

    def test_match_uuid_pattern(self):
        snap = m.collect_api(fake_api())
        F = m.match_report(snap, "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
        texts = "\n".join(i["summary"] for i in F.items)
        self.assertIn("Одиночные очереди с GUID", texts)
        self.assertIn("уже действует политика", texts)
        self.assertNotIn("без GUID", texts)

    def test_match_too_broad(self):
        snap = m.collect_api(fake_api())
        F = m.match_report(snap, "test_")
        w = [i for i in F.items if "без GUID" in i["summary"]]
        self.assertTrue(w and w[0]["level"] == "WARN")
        self.assertTrue(any("с сообщениями" in i["summary"] for i in F.items))

    def test_bad_regex(self):
        with self.assertRaises(m.CheckError):
            m.match_report({"queues": []}, "([")


class QuorumTest(unittest.TestCase):
    def test_minority(self):
        fx = copy.deepcopy(FX)
        for q in fx["queues_rxhost"]:
            if q["name"] == "test_quorum":
                q["members"] = ["rabbit@a", "rabbit@b", "rabbit@c"]
                q["online"] = ["rabbit@a"]
        F = m.analyze(m.collect_api(fake_api(fx)))
        self.assertTrue(by_check(F, "quorum", "CRIT"))

    def test_one_member_down(self):
        fx = copy.deepcopy(FX)
        for q in fx["queues_rxhost"]:
            if q["name"] == "test_quorum":
                q["members"] = ["rabbit@a", "rabbit@b", "rabbit@c"]
                q["online"] = ["rabbit@a", "rabbit@b"]
        F = m.analyze(m.collect_api(fake_api(fx)))
        self.assertTrue(by_check(F, "quorum", "WARN"))


class CtlTest(unittest.TestCase):
    def setUp(self):
        self.orig = m.run_ctl
        qs = [dict(q) for q in FX["queues_rxhost"]]

        def fake(prefix, tool, args, timeout=60):
            self.assertEqual(prefix, "docker exec rabbitmq")
            cmd = args[0]
            if tool == "rabbitmq-diagnostics" and cmd == "status":
                return json.dumps(FXC["status"])
            if cmd == "cluster_status":
                return json.dumps(FXC["cluster_status"])
            if cmd == "list_vhosts":
                return '[\n{"name":"rxhost"}\n]'
            if cmd == "list_queues":
                cols = [a for a in args[3:] if not a.startswith("-") and a != "json"]
                if "members" in cols:
                    return json.dumps(FXC["members"])
                return json.dumps([{c: q.get(c) for c in cols} for q in qs])
            if cmd == "list_connections":
                return json.dumps(FXC["connections"])
            if cmd == "list_policies":
                return json.dumps(FXC["policies"])
            if cmd == "list_users":
                return '[{"user":"rx","tags":["administrator"]},{"user":"guest","tags":["administrator"]}]'
            raise AssertionError(args)
        m.run_ctl = fake

    def tearDown(self):
        m.run_ctl = self.orig

    def test_collect_ctl(self):
        snap = m.collect_ctl("docker exec rabbitmq")
        self.assertEqual(snap["nodes"][0]["name"], "rabbit@localhost")
        self.assertEqual(snap["broker"]["erlang"], "26.2.5.16")
        self.assertEqual(snap["connections"][0]["peer_host"], "192.0.2.13")
        clients = sorted(c["client"] for c in snap["connections"])
        self.assertIn("testhost.PreviewService.Host.ConsumersConnection", clients)
        self.assertEqual(snap["policies"][0]["definition"]["ha-mode"], "all")
        q = [x for x in snap["queues"] if x["name"] == "test_quorum"][0]
        self.assertEqual(q["online"], ["rabbit@localhost"])
        F = m.analyze(snap)
        self.assertTrue(by_check(F, "users", "WARN"))          # guest на месте
        self.assertTrue(by_check(F, "no_consumers", "CRIT"))

    def test_alarm_in_status(self):
        st = copy.deepcopy(FXC["status"])
        st["alarms"] = [{"node": "rabbit@localhost", "type": "resource_limit", "resource": "disk"}]
        orig = m.run_ctl

        def fake2(prefix, tool, args, timeout=60):
            if tool == "rabbitmq-diagnostics":
                return json.dumps(st)
            return orig(prefix, tool, args, timeout)
        m.run_ctl = fake2
        try:
            F = m.analyze(m.collect_ctl("docker exec rabbitmq"))
        finally:
            m.run_ctl = orig
        self.assertTrue(by_check(F, "alarms", "CRIT"))


class StdinTest(unittest.TestCase):
    def test_table(self):
        qs = m.parse_queue_text(TABLE)
        self.assertEqual(qs[0]["vhost"], "rxhost")
        work = [q for q in qs if q["name"] == "test_work_queue"][0]
        self.assertEqual((work["messages"], work["ready"], work["unacked"], work["consumers"]), (1916, 1906, 10, 1))
        F = m.analyze({"source": "stdin", "queues": qs, "broker": {}, "nodes": [], "taken_at": 0,
                       "connections": None, "policies": None})
        self.assertFalse(by_check(F, "alarms"))                    # о тревогах по списку очередей не судим

    def test_json_variants(self):
        qs = m.parse_queue_text('[{"name":"q1","messages":5,"consumers":0}]', "rxhost")
        self.assertEqual(qs[0]["vhost"], "rxhost")
        F = m.analyze({"queues": qs, "broker": {}, "nodes": [], "taken_at": 0})
        self.assertIn("готовых ?", by_check(F, "no_consumers", "CRIT")[0]["details"][0])

    def test_empty_input(self):
        with self.assertRaises(m.CheckError):
            m.parse_queue_text("   ")


class CliTest(unittest.TestCase):
    def run_main(self, argv, env=None, stdin=None):
        old_env, old_stdin = dict(os.environ), sys.stdin
        os.environ.update(env or {})
        for k in ("RABBITMQ_USER", "RABBITMQ_PASSWORD", "RABBITMQ_URL"):
            if not env or k not in env:
                os.environ.pop(k, None)
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = m.main(argv)
        finally:
            os.environ.clear()
            os.environ.update(old_env)
            sys.stdin = old_stdin
        return code, buf.getvalue()

    def test_missing_user_is_exit_2(self):
        code, out = self.run_main([])
        self.assertEqual(code, 2)
        self.assertIn("RABBITMQ_USER", out)

    def test_amqp_url_rejected(self):
        code, out = self.run_main(["--url", "amqp://rx:Secr3t@mq:5672/"])
        self.assertEqual(code, 2)
        self.assertNotIn("Secr3t", out)

    def test_password_never_printed(self):
        orig = m.Api.get

        def boom(self, path, allow=()):
            raise m.CheckError("нет связи с %s: refused" % m.safe_url(self.url))
        m.Api.get = boom
        try:
            code, out = self.run_main(["--json"], env={"RABBITMQ_URL": "http://monitor:Pa55w0rd@mq01:15672",
                                                        "RABBITMQ_PASSWORD": "Pa55w0rd"})
        finally:
            m.Api.get = orig
        self.assertEqual(code, 2)
        self.assertNotIn("Pa55w0rd", out)
        self.assertEqual(json.loads(out)["status"], "ERROR")

    def test_stdin_mode_exit_codes(self):
        code, out = self.run_main(["--stdin"], stdin=TABLE)
        self.assertEqual(code, 1)
        self.assertIn("Итог: CRIT", out)
        ok_table = "name\tmessages\tmessages_ready\tmessages_unacknowledged\tconsumers\nq\t0\t0\t0\t1\n"
        code, out = self.run_main(["--stdin"], stdin=ok_table)
        self.assertEqual(code, 0, out)
        code, out = self.run_main(["--stdin"], stdin="")
        self.assertEqual(code, 2)

    def test_netrc(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "netrc")
            with open(p, "w") as f:
                f.write("machine mq01 login monitor password N3trcPass\n")
            os.chmod(p, 0o600)

            class A(object):
                url, user, netrc = "http://mq01:15672", None, p
            for k in ("RABBITMQ_USER", "RABBITMQ_PASSWORD", "RABBITMQ_URL"):
                os.environ.pop(k, None)
            url, user, pw = m.credentials(A())
        self.assertEqual((url, user, pw), ("http://mq01:15672", "monitor", "N3trcPass"))


if __name__ == "__main__":
    unittest.main()
