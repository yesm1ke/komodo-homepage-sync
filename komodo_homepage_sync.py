#!/usr/bin/env python3
"""Expose containers known to Komodo as a minimal, read-only Docker API for Homepage.

Homepage discovers services from `homepage.*` container labels by calling the
Docker API. This service answers those calls from Komodo's view of all servers,
and returns nothing but what Homepage needs: name, state, health, `homepage.*`
labels and optional stats. Environment variables, commands, mounts and every
other Docker endpoint are never exposed.
"""

import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

__version__ = "0.1.1"

log = logging.getLogger("komodo-homepage-sync")

LABEL_PREFIX = "homepage."
HEALTH_STATES = ("healthy", "unhealthy", "starting")
# Label keys whose literal values look like credentials. Homepage resolves
# {{HOMEPAGE_VAR_*}} / {{HOMEPAGE_FILE_*}} itself, so those references pass.
SECRET_KEY_RE = re.compile(r"(^|[._])(key|apikey|api_key|password|pass|secret|token)$", re.I)
API_PATH_RE = re.compile(r"^(?:/v[0-9.]+)?(/.*)$")
CONTAINER_PATH_RE = re.compile(r"^/containers/([^/]+)/(json|stats)$")
SIZE_RE = re.compile(r"^\s*([0-9.]+)\s*([a-zA-Z]*)\s*$")
UNITS = {
    "": 1, "b": 1,
    "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12,
    "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4,
}


def env(name, default=None):
    """Read NAME, or the file named by NAME_FILE (for Docker secrets)."""
    path = os.environ.get(f"{name}_FILE")
    if path:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return os.environ.get(name, default)


def parse_size(text):
    """'66.25MiB' -> bytes; returns None when unparsable."""
    m = SIZE_RE.match(text or "")
    if not m or m.group(2).lower() not in UNITS:
        return None
    return int(float(m.group(1)) * UNITS[m.group(2).lower()])


def health_from_status(status):
    """Docker's human status, e.g. 'Up 5 minutes (healthy)' -> 'healthy'."""
    for state in HEALTH_STATES:
        if f"({state})" in (status or "") or f"(health: {state})" in (status or ""):
            return state
    return None


def homepage_labels(labels):
    """Keep only homepage.* labels and drop literal secrets."""
    kept = {}
    for key, value in (labels or {}).items():
        if not key.startswith(LABEL_PREFIX):
            continue
        if SECRET_KEY_RE.search(key) and "{{" not in str(value):
            log.warning("dropping label %s: literal secret, use {{HOMEPAGE_VAR_*}} instead", key)
            continue
        kept[key] = value
    return kept


def docker_stats(stats):
    """Build the subset of a Docker stats response Homepage reads from Komodo's stats strings."""
    cpu = float((stats.get("cpu_perc") or "0").rstrip("%") or 0)
    result = {
        "cpu_stats": {"cpu_usage": {"total_usage": int(cpu * 1e6)}, "system_cpu_usage": int(1e8), "online_cpus": 1},
        "precpu_stats": {"cpu_usage": {"total_usage": 0}, "system_cpu_usage": 0},
    }
    used = parse_size((stats.get("mem_usage") or "").split("/")[0])
    if used:
        result["memory_stats"] = {"usage": used}
    net = (stats.get("net_io") or "").split("/")
    if len(net) == 2:
        rx, tx = parse_size(net[0]), parse_size(net[1])
        if rx is not None and tx is not None:
            result["networks"] = {"eth0": {"rx_bytes": rx, "tx_bytes": tx}}
    return result


class Komodo:
    def __init__(self, url, key, secret, timeout=15):
        self.url = url.rstrip("/")
        self.headers = {"Content-Type": "application/json", "X-Api-Key": key, "X-Api-Secret": secret}
        self.timeout = timeout

    def read(self, request, params):
        req = urllib.request.Request(
            f"{self.url}/read/{request}", data=json.dumps(params).encode(), headers=self.headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as res:
            return json.load(res)


class Inventory:
    """Periodically mirrors Komodo containers that carry homepage.* labels."""

    def __init__(self, komodo, name_format="{server}.{name}", servers=None):
        self.komodo = komodo
        self.name_format = name_format
        self.servers = servers or []
        self.labels_by_id = {}  # labels never change during a container's life
        self.lock = threading.Lock()
        self.containers = []  # Docker /containers/json items
        self.stats_by_id = {}
        self.ready = False

    def refresh(self):
        items = self.komodo.read("ListAllContainers", {"servers": self.servers, "limit": 0})
        containers, stats, seen = [], {}, set()
        for item in items:
            cid, server, name = item.get("id"), item.get("server_name") or item.get("server_id"), item.get("name")
            if not cid or not server or not name:
                continue
            seen.add(cid)
            if cid not in self.labels_by_id:
                inspect = self.komodo.read("InspectContainer", {"server": server, "container": name})
                self.labels_by_id[cid] = homepage_labels((inspect.get("Config") or {}).get("Labels"))
            labels = self.labels_by_id[cid]
            if not labels:
                continue
            containers.append({
                "Id": cid,
                "Names": ["/" + self.name_format.format(server=server, name=name)],
                "Image": item.get("image") or "",
                "Labels": labels,
                "State": item.get("state") or "",
                "Status": item.get("status") or "",
                "_health": health_from_status(item.get("status")),
            })
            if item.get("stats"):
                stats[cid] = docker_stats(item["stats"])
        self.labels_by_id = {k: v for k, v in self.labels_by_id.items() if k in seen}
        with self.lock:
            self.containers, self.stats_by_id, self.ready = containers, stats, True
        return len(containers)

    def list(self, all_=True, health=None):
        with self.lock:
            items = list(self.containers)
        if not all_:
            items = [c for c in items if c["State"] == "running"]
        if health:
            items = [c for c in items if c["_health"] in health]
        return [{k: v for k, v in c.items() if not k.startswith("_")} for c in items]

    def find(self, ref):
        """Container by id, id prefix or exposed name (with or without the leading slash)."""
        name = "/" + ref.lstrip("/")
        with self.lock:
            for c in self.containers:
                if c["Id"] == ref or name in c["Names"] or (len(ref) >= 12 and c["Id"].startswith(ref)):
                    return c
        return None

    def inspect(self, ref):
        """Minimal inspect for Homepage v1 status dots: state and health only."""
        c = self.find(ref)
        if c is None:
            return None
        state = {"Status": c["State"], "Running": c["State"] == "running"}
        if c["_health"]:
            state["Health"] = {"Status": c["_health"]}
        return {"Id": c["Id"], "Name": c["Names"][0], "State": state}

    def stats(self, ref):
        c = self.find(ref)
        with self.lock:
            return self.stats_by_id.get(c["Id"]) if c else None

    def run(self, interval):
        while True:
            try:
                count = self.refresh()
                log.info("refreshed: %d labelled containers", count)
            except (urllib.error.URLError, OSError, ValueError) as e:
                log.error("refresh failed, serving last known state: %s", e)
            time.sleep(interval)


def make_handler(inventory, token=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"komodo-homepage-sync/{__version__}"
        sys_version = ""

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        def send_json(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlparse(self.path)
            path = API_PATH_RE.match(url.path).group(1)
            query = parse_qs(url.query)

            # Liveness needs no token, like Docker's own /_ping: the image's
            # HEALTHCHECK can't know it.
            if path == "/_ping":
                return self.send_json(200, "OK")
            if token and self.headers.get("Authorization") != f"Bearer {token}":
                return self.send_json(401, {"message": "unauthorized"})
            if path == "/version":
                return self.send_json(200, {"Version": __version__, "ApiVersion": "1.41", "Os": "komodo"})
            if path == "/containers/json":
                if not inventory.ready:
                    return self.send_json(503, {"message": "not synced with Komodo yet"})
                all_ = query.get("all", ["0"])[0] in ("1", "true", "True")
                health = None
                if "filters" in query:
                    try:
                        health = json.loads(query["filters"][0]).get("health")
                    except ValueError:
                        return self.send_json(400, {"message": "invalid filters"})
                return self.send_json(200, inventory.list(all_, health))
            m = CONTAINER_PATH_RE.match(path)
            if m:
                ref, what = m.groups()
                body = inventory.inspect(ref) if what == "json" else inventory.stats(ref)
                if body is None:
                    return self.send_json(404, {"message": f"no such container: {ref}"})
                return self.send_json(200, body)
            return self.send_json(404, {"message": "not supported"})

        def do_POST(self):
            self.send_json(405, {"message": "read-only"})

        do_PUT = do_DELETE = do_HEAD = do_PATCH = do_POST

    return Handler


def main():
    logging.basicConfig(
        level=env("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout
    )
    url, key, secret = env("KOMODO_URL"), env("KOMODO_API_KEY"), env("KOMODO_API_SECRET")
    if not (url and key and secret):
        log.error("KOMODO_URL, KOMODO_API_KEY and KOMODO_API_SECRET are required")
        return 2
    servers = [s.strip() for s in (env("KOMODO_SERVERS") or "").split(",") if s.strip()]
    inventory = Inventory(Komodo(url, key, secret), env("NAME_FORMAT", "{server}.{name}"), servers)
    threading.Thread(target=inventory.run, args=(int(env("POLL_INTERVAL", "30")),), daemon=True).start()

    host, port = env("LISTEN_HOST", "0.0.0.0"), int(env("LISTEN_PORT", "2375"))
    httpd = ThreadingHTTPServer((host, port), make_handler(inventory, env("AUTH_TOKEN")))
    log.info("komodo-homepage-sync %s listening on %s:%d, Komodo at %s", __version__, host, port, url)
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
