import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import komodo_homepage_sync as khs

CONTAINERS = [
    {"id": "aaa", "server_name": "infra", "name": "core", "image": "komodo-core", "state": "running",
     "status": "Up 2 hours (healthy)",
     "stats": {"cpu_perc": "1.50%", "mem_usage": "64MiB / 2GiB", "net_io": "1.5kB / 2kB"}},
    {"id": "bbb", "server_name": "infra", "name": "mongo", "image": "mongo:8", "state": "running",
     "status": "Up 2 hours"},
    {"id": "ccc", "server_name": "media", "name": "jellyfin", "image": "jellyfin", "state": "exited",
     "status": "Exited (0) 1 minute ago"},
]
LABELS = {
    "aaa": {"homepage.group": "Infra", "homepage.name": "Komodo", "com.docker.compose.project": "komodo"},
    "bbb": {"com.docker.compose.project": "komodo"},
    "ccc": {"homepage.group": "Media", "homepage.name": "Jellyfin",
            "homepage.widget.key": "plain-secret", "homepage.widget.password": "{{HOMEPAGE_VAR_JF}}"},
}


class FakeKomodo:
    def __init__(self):
        self.inspected = []

    def read(self, request, params):
        if request == "ListAllContainers":
            return CONTAINERS
        if request == "InspectContainer":
            cid = next(c["id"] for c in CONTAINERS if c["name"] == params["container"])
            self.inspected.append(cid)
            return {"Id": cid, "Config": {"Labels": LABELS[cid], "Env": ["SECRET=leak"]}}
        raise AssertionError(request)


class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.komodo = FakeKomodo()
        self.inv = khs.Inventory(self.komodo)
        self.inv.refresh()

    def test_only_labelled_containers_with_prefixed_names(self):
        names = [c["Names"][0] for c in self.inv.list()]
        self.assertEqual(names, ["/infra.core", "/media.jellyfin"])

    def test_only_homepage_labels_and_no_literal_secrets(self):
        labels = {c["Names"][0]: c["Labels"] for c in self.inv.list()}
        self.assertEqual(labels["/infra.core"], {"homepage.group": "Infra", "homepage.name": "Komodo"})
        self.assertNotIn("homepage.widget.key", labels["/media.jellyfin"])
        self.assertEqual(labels["/media.jellyfin"]["homepage.widget.password"], "{{HOMEPAGE_VAR_JF}}")

    def test_no_env_anywhere(self):
        self.assertNotIn("leak", json.dumps(self.inv.list()))

    def test_health_and_running_filters(self):
        self.assertEqual([c["Id"] for c in self.inv.list(health=["healthy"])], ["aaa"])
        self.assertEqual([c["Id"] for c in self.inv.list(all_=False)], ["aaa"])

    def test_inspect_is_cached(self):
        self.inv.refresh()
        self.assertEqual(sorted(self.komodo.inspected), ["aaa", "bbb", "ccc"])

    def test_stats_shape_matches_homepage_math(self):
        s = self.inv.stats("aaa")
        cpu = (s["cpu_stats"]["cpu_usage"]["total_usage"] - s["precpu_stats"]["cpu_usage"]["total_usage"]) / (
            s["cpu_stats"]["system_cpu_usage"] - s["precpu_stats"]["system_cpu_usage"]) * 100
        self.assertAlmostEqual(cpu, 1.5)
        self.assertEqual(s["memory_stats"]["usage"], 64 * 1024**2)
        self.assertEqual(s["networks"]["eth0"], {"rx_bytes": 1500, "tx_bytes": 2000})


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        inv = khs.Inventory(FakeKomodo())
        inv.refresh()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), khs.make_handler(inv, token="t0k"))
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def get(self, path, token="t0k", method="GET"):
        req = urllib.request.Request(self.base + path, method=method)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, json.load(res)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_requires_token(self):
        self.assertEqual(self.get("/containers/json", token=None)[0], 401)

    def test_ping_without_token(self):
        self.assertEqual(self.get("/_ping", token=None)[0], 200)
        self.assertEqual(self.get("/version", token=None)[0], 401)

    def test_list_with_api_version_prefix(self):
        code, body = self.get("/v1.41/containers/json?all=1")
        self.assertEqual(code, 200)
        self.assertEqual(len(body), 2)

    def test_health_filter(self):
        f = urllib.request.quote(json.dumps({"health": ["healthy"]}))
        self.assertEqual([c["Id"] for c in self.get(f"/containers/json?all=1&filters={f}")[1]], ["aaa"])

    def test_minimal_inspect_by_name_has_state_only(self):
        code, body = self.get("/containers/infra.core/json")
        self.assertEqual(code, 200)
        self.assertEqual(body, {"Id": "aaa", "Name": "/infra.core",
                                "State": {"Status": "running", "Running": True, "Health": {"Status": "healthy"}}})
        self.assertEqual(self.get("/containers/infra.mongo/json")[0], 404)  # unlabelled stays hidden

    def test_logs_exec_and_writes_are_refused(self):
        self.assertEqual(self.get("/containers/aaa/logs")[0], 404)
        self.assertEqual(self.get("/containers/aaa/exec", method="POST")[0], 405)

    def test_stats(self):
        self.assertEqual(self.get("/containers/aaa/stats?stream=false")[0], 200)
        self.assertEqual(self.get("/containers/infra.core/stats?stream=false")[0], 200)
        self.assertEqual(self.get("/containers/ccc/stats?stream=false")[0], 404)


class HelpersTest(unittest.TestCase):
    def test_parse_size(self):
        self.assertEqual(khs.parse_size("2GiB"), 2 * 1024**3)
        self.assertEqual(khs.parse_size("648B"), 648)
        self.assertIsNone(khs.parse_size("n/a"))

    def test_health_from_status(self):
        self.assertEqual(khs.health_from_status("Up 3 seconds (health: starting)"), "starting")
        self.assertEqual(khs.health_from_status("Up 1 hour (unhealthy)"), "unhealthy")
        self.assertIsNone(khs.health_from_status("Up 1 hour"))


if __name__ == "__main__":
    unittest.main()
