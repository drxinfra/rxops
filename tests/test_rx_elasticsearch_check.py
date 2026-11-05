"""Проверки es_check.py (скил rx-elasticsearch) без сети: python3 -m unittest discover -s tests

Фикстура записана с тестового Elasticsearch 7.17.23 (один узел) в контейнере: индексы
вида rxsearch_*, реплика без места (yellow), индекс с неразмещаемым основным шардом (red),
блокировки read_only и write, allocation explain. Адреса заменены на 192.0.2.0/24.
"""
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "skills", "rx-elasticsearch", "scripts"))
import es_check as e  # noqa: E402

FX = json.loads(r"""{"/":{"name":"rxops-es-7","cluster_name":"docker-cluster","cluster_uuid":"_M7WUYV5R-yifOPbsDqzjw","version":{"number":"7.17.23","build_flavor":"default","build_type":"docker","build_hash":"61d76462eecaf09ada684d1b5d319b5ff6865a83","build_date":"2024-07-25T14:37:42.448799567Z","build_snapshot":false,"lucene_version":"8.11.3","minimum_wire_compatibility_version":"6.8.0","minimum_index_compatibility_version":"6.0.0-beta1"},"tagline":"You Know, for Search"},"/_cluster/health":{"cluster_name":"docker-cluster","status":"red","timed_out":false,"number_of_nodes":1,"number_of_data_nodes":1,"active_primary_shards":5,"active_shards":5,"relocating_shards":0,"initializing_shards":0,"unassigned_shards":2,"delayed_unassigned_shards":0,"number_of_pending_tasks":0,"number_of_in_flight_fetch":0,"task_max_waiting_in_queue_millis":0,"active_shards_percent_as_number":71.42857142857143},"/_cat/nodes":[{"name":"rxops-es-7","ip":"192.0.2.20","node.role":"cdfhilmrstw","master":"*","heap.percent":"68","heap.max":"536870912","ram.percent":"70","ram.max":"1610612736","disk.used_percent":"0.14","disk.total":"541115342848","disk.avail":"540367179776","version":"7.17.23"}],"/_cat/allocation":[{"shards":"5","disk.indices":"32985810","disk.used":"748163072","disk.avail":"540367179776","disk.total":"541115342848","disk.percent":"0","host":"192.0.2.20","ip":"192.0.2.20","node":"rxops-es-7"},{"shards":"2","disk.indices":null,"disk.used":null,"disk.avail":null,"disk.total":null,"disk.percent":null,"host":null,"ip":null,"node":"UNASSIGNED"}],"/_cluster/settings":{"defaults":{"cluster":{"blocks":{"read_only_allow_delete":"false","read_only":"false"},"routing":{"allocation":{"disk":{"threshold_enabled":"true","watermark":{"flood_stage.frozen.max_headroom":"20GB","flood_stage":"95%","high":"90%","low":"85%","enable_for_single_data_node":"false","flood_stage.frozen":"95%"},"include_relocations":"true","reroute_interval":"60s"},"enable":"all"}},"max_shards_per_node":"1000"}}},"/_cat/indices":[{"health":"green","status":"open","index":"rx-logs-2026.11.05","pri":"1","rep":"0","docs.count":"0","store.size":"227","pri.store.size":"227"},{"health":"green","status":"open","index":".geoip_databases","pri":"1","rep":"0","docs.count":"33","store.size":"32980291","pri.store.size":"32980291"},{"health":"green","status":"open","index":"rxsearch_workflowassignment_dirrx_production","pri":"1","rep":"0","docs.count":"0","store.size":"227","pri.store.size":"227"},{"health":"red","status":"open","index":"rxsearch_document_redtest_production","pri":"1","rep":"0","docs.count":null,"store.size":null,"pri.store.size":null},{"health":"green","status":"open","index":"rxsearch_workflowtask_dirrx_production","pri":"1","rep":"0","docs.count":"0","store.size":"227","pri.store.size":"227"},{"health":"yellow","status":"open","index":"rxsearch_document_dirrx_production","pri":"1","rep":"1","docs.count":"3","store.size":"4838","pri.store.size":"4838"}],"/_all/_settings/index.blocks.*":{"rx-logs-2026.11.05":{"settings":{"index.blocks.read_only":"true"}},"rxsearch_workflowassignment_dirrx_production":{"settings":{"index.blocks.write":"true"}}},"/_nodes/stats/jvm,breaker,thread_pool":{"nodes":{"9IGQkXjlRpeSzfYQ0qouVw":{"name":"rxops-es-7","jvm":{"mem":{"heap_used_percent":68,"heap_max_in_bytes":536870912}},"thread_pool":{"search":{"queue":0,"rejected":0},"write":{"queue":0,"rejected":0}},"breakers":{"request":{"limit_size_in_bytes":322122547,"estimated_size_in_bytes":0,"tripped":0},"fielddata":{"limit_size_in_bytes":214748364,"estimated_size_in_bytes":0,"tripped":0},"parent":{"limit_size_in_bytes":510027366,"estimated_size_in_bytes":372226480,"tripped":0}}}}},"/_nodes":{"nodes":{"9IGQkXjlRpeSzfYQ0qouVw":{"name":"rxops-es-7","process":{"mlockall":false},"jvm":{"mem":{"heap_max_in_bytes":536870912},"using_compressed_ordinary_object_pointers":"true"}}}},"/_cat/shards":[{"index":"rx-logs-2026.11.05","shard":"0","prirep":"p","state":"STARTED","unassigned.reason":null,"node":"rxops-es-7"},{"index":"rxsearch_workflowassignment_dirrx_production","shard":"0","prirep":"p","state":"STARTED","unassigned.reason":null,"node":"rxops-es-7"},{"index":"rxsearch_document_dirrx_production","shard":"0","prirep":"p","state":"STARTED","unassigned.reason":null,"node":"rxops-es-7"},{"index":"rxsearch_document_dirrx_production","shard":"0","prirep":"r","state":"UNASSIGNED","unassigned.reason":"INDEX_CREATED","node":null},{"index":"rxsearch_workflowtask_dirrx_production","shard":"0","prirep":"p","state":"STARTED","unassigned.reason":null,"node":"rxops-es-7"},{"index":".geoip_databases","shard":"0","prirep":"p","state":"STARTED","unassigned.reason":null,"node":"rxops-es-7"},{"index":"rxsearch_document_redtest_production","shard":"0","prirep":"p","state":"UNASSIGNED","unassigned.reason":"INDEX_CREATED","node":null}],"/_cluster/allocation/explain":{"note":"No shard was specified in the explain API request, so this r","index":"rxsearch_document_redtest_production","shard":0,"primary":true,"current_state":"unassigned","unassigned_info":{"reason":"INDEX_CREATED","at":"2026-11-05T17:23:26.004Z","last_allocation_status":"no"},"can_allocate":"no","allocate_explanation":"cannot allocate because allocation is not permitted to any of the nodes","node_allocation_decisions":[{"node_id":"9IGQkXjlRpeSzfYQ0qouVw","node_name":"rxops-es-7","transport_address":"192.0.2.20:9300","node_decision":"no","weight_ranking":1,"deciders":[{"decider":"filter","decision":"NO","explanation":"node does not match index setting [index.routing.allocation.require] filters [_name:\"no-such-node\"]"}]}]}}""")
# Плагины из комплекта поставки RX (в официальном образе их нет, на стенде список был пуст).
FX["/_cat/plugins"] = [{"name": "rxops-es-7", "component": "analysis-morphology", "version": "7.17.23"},
                       {"name": "rxops-es-7", "component": "ingest-attachment", "version": "7.17.23"}]


def fake(fx=None, fail=()):
    fx = copy.deepcopy(fx or FX)

    def get(path):
        p = path.split("?")[0]
        if p in fail:
            raise e.HttpFail(403, p)
        if p not in fx:
            raise AssertionError("неожиданный запрос " + path)
        return fx[p]
    return get


def by_check(F, check, level=None):
    return [i for i in F.items if i["check"] == check and (level is None or i["level"] == level)]


def variant(**changes):
    fx = copy.deepcopy(FX)
    for path, fn in changes.items():
        fn(fx)
    return fx


def green(fx):
    """Убрать красный индекс и реплику: кластер зелёный."""
    fx["/_cluster/health"].update(status="green", unassigned_shards=0)
    fx["/_cat/indices"] = [i for i in fx["/_cat/indices"] if i["index"] != "rxsearch_document_redtest_production"]
    for i in fx["/_cat/indices"]:
        i["health"] = "green"
    fx["/_all/_settings/index.blocks.*"] = {}


class RedClusterTest(unittest.TestCase):
    def setUp(self):
        self.snap = e.collect(fake())
        self.F = e.analyze(self.snap)

    def test_red_with_explain(self):
        crit = by_check(self.F, "health", "CRIT")
        self.assertEqual(len(crit), 1)
        text = "\n".join(crit[0]["details"])
        self.assertIn("rxsearch_document_redtest_production: основной, причина INDEX_CREATED", text)
        self.assertIn("filter: node does not match index setting", text)
        self.assertIn("красные индексы: rxsearch_document_redtest_production", text)

    def test_blocks(self):
        crit = by_check(self.F, "blocks", "CRIT")
        self.assertTrue(crit)
        text = "\n".join(crit[0]["details"])
        self.assertIn("rxsearch_workflowassignment_dirrx_production: write (индекс RX)", text)
        self.assertIn("rx-logs-2026.11.05: read_only", text)

    def test_version_supported(self):
        v = by_check(self.F, "version")[0]
        self.assertEqual(v["level"], "OK")
        self.assertIn("7.17.23", v["summary"])

    def test_rx_indices_and_systems(self):
        info = by_check(self.F, "rx", "INFO")[0]
        self.assertIn("Индексы RX: 4, систем (префикс, код и постфикс): 2", info["summary"])
        self.assertTrue(by_check(self.F, "rx", "CRIT"))
        self.assertTrue(any("документов ?" in d for d in info["details"]))

    def test_anonymous_and_exit_level(self):
        F = e.analyze(self.snap, anonymous=True)
        self.assertTrue(by_check(F, "security", "INFO"))
        self.assertEqual(F.worst(), "CRIT")


class YellowAndDiskTest(unittest.TestCase):
    def test_single_node_replicas(self):
        def yellow(fx):
            fx["/_cluster/health"].update(status="yellow", unassigned_shards=1)
            fx["/_cat/indices"] = [i for i in fx["/_cat/indices"] if i["index"] != "rxsearch_document_redtest_production"]
            fx["/_cat/shards"] = [s for s in fx["/_cat/shards"] if s["index"] != "rxsearch_document_redtest_production"]
            fx["/_all/_settings/index.blocks.*"] = {}
        F = e.analyze(e.collect(fake(variant(x=yellow))))
        w = by_check(F, "health", "WARN")
        self.assertTrue(w and "реплики разместить негде" in w[0]["summary"])
        self.assertIn("number_of_replicas 0", w[0]["hint"])

    def test_green_ok_exit(self):
        F = e.analyze(e.collect(fake(variant(x=green))))
        self.assertEqual(by_check(F, "health")[0]["level"], "OK")
        self.assertEqual(F.worst(), "OK", [i for i in F.items if i["level"] != "OK" and i["level"] != "INFO"])

    def test_flood_stage_percent(self):
        def full(fx):
            green(fx)
            a = fx["/_cat/allocation"][0]
            a.update({"disk.used": "960000000000", "disk.total": "1000000000000", "disk.avail": "40000000000"})
        F = e.analyze(e.collect(fake(variant(x=full))))
        crit = by_check(F, "disk", "CRIT")
        self.assertTrue(crit and "flood_stage 95%" in crit[0]["details"][0])

    def test_absolute_watermarks(self):
        def absolute(fx):
            green(fx)
            fx["/_cluster/settings"]["transient"] = {"cluster": {"routing": {"allocation": {"disk": {"watermark": {
                "low": "530gb", "high": "520gb", "flood_stage": "510gb"}}}}}}
        F = e.analyze(e.collect(fake(variant(x=absolute))))
        crit = by_check(F, "disk", "CRIT")
        self.assertTrue(crit and "выше flood_stage 510gb" in crit[0]["details"][0], F.items)

    def test_early_disk_warning(self):
        def early(fx):
            green(fx)
            fx["/_cat/allocation"][0].update({"disk.used": "820", "disk.total": "1000", "disk.avail": "180"})
        F = e.analyze(e.collect(fake(variant(x=early))))
        self.assertTrue(by_check(F, "disk", "WARN"))

    def test_threshold_disabled_and_allocation_enable(self):
        def odd(fx):
            green(fx)
            fx["/_cluster/settings"]["persistent"] = {"cluster": {"routing": {"allocation": {
                "enable": "primaries", "disk": {"threshold_enabled": "false"}}}}}
        F = e.analyze(e.collect(fake(variant(x=odd))))
        texts = " ".join(i["summary"] for i in F.items if i["level"] == "WARN")
        self.assertIn("threshold_enabled", texts)
        self.assertIn("allocation.enable = primaries", texts)


class HeapShardsVersionTest(unittest.TestCase):
    def test_breakers_and_rejections(self):
        def trip(fx):
            n = list(fx["/_nodes/stats/jvm,breaker,thread_pool"]["nodes"].values())[0]
            n["breakers"]["request"]["tripped"] = 3
            n["thread_pool"]["write"]["rejected"] = 12
            n["jvm"]["mem"]["heap_used_percent"] = 91
        F = e.analyze(e.collect(fake(variant(x=trip))))
        heap = " ".join(i["summary"] for i in by_check(F, "heap", "WARN"))
        self.assertIn("Предохранители срабатывали", heap)
        self.assertIn("Отклонённые запросы", heap)
        self.assertIn("Куча JVM почти заполнена", heap)

    def test_shard_limit(self):
        def many(fx):
            fx["/_cluster/health"].update(active_shards=960, unassigned_shards=0, status="green")
        F = e.analyze(e.collect(fake(variant(x=many))))
        self.assertTrue(by_check(F, "shards", "CRIT"))
        self.assertTrue(any("на гигабайт кучи" in i["summary"] for i in by_check(F, "shards", "WARN")))

    def test_big_rx_shard(self):
        def big(fx):
            green(fx)
            for i in fx["/_cat/indices"]:
                if i["index"] == "rxsearch_document_dirrx_production":
                    i.update({"pri.store.size": str(45 * 1024 ** 3), "store.size": str(45 * 1024 ** 3)})
        F = e.analyze(e.collect(fake(variant(x=big))))
        w = by_check(F, "rx", "WARN")
        self.assertTrue(w and "30 ГБ" in w[0]["summary"])

    def test_versions(self):
        for ver, dist, level in (("8.15.3", "default", "WARN"), ("7.16.3", "default", "WARN"),
                                 ("2.11.0", "opensearch", "WARN")):
            def setv(fx, ver=ver, dist=dist):
                green(fx)
                fx["/"]["version"]["number"] = ver
                fx["/"]["version"]["distribution"] = dist
            F = e.analyze(e.collect(fake(variant(x=setv))))
            self.assertEqual(by_check(F, "version")[0]["level"], level, ver)

        def logs_only(fx):
            green(fx)
            fx["/"]["version"]["number"] = "8.15.3"
            fx["/_cat/indices"] = [i for i in fx["/_cat/indices"] if not i["index"].startswith("rxsearch")]
        F = e.analyze(e.collect(fake(variant(x=logs_only))))
        self.assertEqual(by_check(F, "version")[0]["level"], "INFO")
        self.assertIn("не найдено", by_check(F, "rx")[0]["summary"])

    def test_vendor_plugins(self):
        F = e.analyze(e.collect(fake(variant(x=green))))
        self.assertIn("analysis-morphology", by_check(F, "plugins")[0]["summary"])

        def no_plugins(fx):
            green(fx)
            fx["/_cat/plugins"] = []
        F = e.analyze(e.collect(fake(variant(x=no_plugins))))
        w = by_check(F, "plugins", "WARN")
        self.assertTrue(w and "ingest-attachment" in w[0]["summary"])

    def test_missing_privileges_are_notes(self):
        snap = e.collect(fake(fail=("/_all/_settings/index.blocks.*",)))
        F = e.analyze(snap)
        self.assertTrue(any("блокировки индексов" in i["summary"] for i in by_check(F, "notes")))


class HelpersTest(unittest.TestCase):
    def test_parse_size_and_over(self):
        self.assertEqual(e.parse_size("100gb"), 100 * 1024 ** 3)
        self.assertTrue(e.over("85%", 86.0, 10))
        self.assertFalse(e.over("85%", 50.0, 10))
        self.assertTrue(e.over("100gb", 10.0, 50 * 1024 ** 3))
        self.assertTrue(e.over("0.9", 91.0, None))

    def test_net_errors_explained(self):
        self.assertIn("https://", e.explain_net("http://es01:9200", "Connection reset by peer"))
        self.assertIn("ES_CA", e.explain_net("https://es01:9200", "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"))
        self.assertIn("отклонено", e.explain_net("http://es01:9200", "[Errno 111] Connection refused"))

    def test_rx_index_names(self):
        m = e.RX_INDEX.match("directum_rxsearch_document_properties_dirrx_production")
        self.assertEqual((m.group("prefix"), m.group("kind"), m.group("props")), ("directum", "document", "_properties"))
        self.assertIsNone(e.RX_INDEX.match("rx-logs-2026.11.05"))

    def test_growth(self):
        snap = e.collect(fake())
        prev = {"taken_at": snap["taken_at"] - 300, "indices": copy.deepcopy(snap["indices"])}
        for i in snap["indices"]:
            if i["index"] == "rxsearch_document_dirrx_production":
                i["docs.count"] = "10"
        F = e.analyze(snap, prev)
        g = by_check(F, "growth")[0]
        self.assertIn("rxsearch_document_dirrx_production: +7 документов", "\n".join(g["details"]))


class CliTest(unittest.TestCase):
    def run_main(self, argv, env):
        old = dict(os.environ)
        for k in ("ES_URL", "ES_USER", "ES_PASSWORD", "ES_CA"):
            os.environ.pop(k, None)
        os.environ.update(env)
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = e.main(argv)
        finally:
            os.environ.clear()
            os.environ.update(old)
        return code, buf.getvalue()

    def test_user_without_password(self):
        code, out = self.run_main([], {"ES_URL": "http://es01:9200", "ES_USER": "elastic"})
        self.assertEqual(code, 2)
        self.assertIn("ES_PASSWORD", out)

    def test_password_not_printed_and_json_error(self):
        orig = e.Api.get

        def boom(self, path):
            raise e.CheckError(e.explain_net(self.url, "Connection refused"))
        e.Api.get = boom
        try:
            code, out = self.run_main(["--json"], {"ES_URL": "https://elastic:S3cretPw@es01:9200",
                                                    "ES_PASSWORD": "S3cretPw"})
        finally:
            e.Api.get = orig
        self.assertEqual(code, 2)
        self.assertNotIn("S3cretPw", out)
        self.assertEqual(json.loads(out)["status"], "ERROR")

    def test_ok_run_and_compare_file(self):
        orig = e.collect
        e.collect = lambda get: orig(fake(variant(x=green)))
        try:
            code, out = self.run_main(["--json"], {"ES_URL": "http://es01:9200", "ES_USER": "monitor",
                                                    "ES_PASSWORD": "x"})
            self.assertEqual(code, 0, out)
            with tempfile.TemporaryDirectory() as d:
                p = os.path.join(d, "before.json")
                with open(p, "w", encoding="utf-8") as f:
                    f.write(out)
                code, out2 = self.run_main(["--compare", p], {"ES_URL": "http://es01:9200", "ES_USER": "monitor",
                                                               "ES_PASSWORD": "x"})
        finally:
            e.collect = orig
        self.assertEqual(code, 0, out2)
        self.assertIn("сравнение с предыдущим", out2)


if __name__ == "__main__":
    unittest.main()
