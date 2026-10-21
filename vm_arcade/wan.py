"""Optional, bounded read-only UniFi WAN metrics from an existing Prometheus."""
import copy
import http.client
import json
import threading
import time
from urllib.parse import urlencode, urlsplit

from .telemetry import MonitorError, numeric


def validate(config):
    if not isinstance(config, dict):
        raise ValueError("Invalid monitoring configuration")
    url = urlsplit(config["url"])
    if (url.scheme not in ("http", "https") or not url.hostname or url.username or url.password
            or url.path not in ("", "/") or url.query or url.fragment):
        raise ValueError("Invalid monitoring URL")
    for key in ("site", "instance", "device", "port"):
        if not isinstance(config.get(key), str) or not 1 <= len(config[key]) <= 128:
            raise ValueError("Invalid monitoring selector")
    return config


def query(config):
    scope = ','.join(k + '=' + json.dumps(v) for k, v in
                     (("job", "unpoller"), ("site", config["site"]), ("instance", config["instance"])))
    device = scope + ',name=' + json.dumps(config["device"]) + ',port=' + json.dumps(config["port"])
    return ('{__name__=~"unpoller_device_wan_(receive|transmit)_rate_bytes",' + device + '}'
            ' or on (__name__, instance, site, name, port) {__name__=~"unpoller_prometheus_cache_age_seconds|up",' + scope + '}'
            ' or on (__name__, instance, site, name, port) label_replace(timestamp(unpoller_device_wan_receive_rate_bytes{' + device + '}),'
            '"__name__","source_timestamp","",".*")')


def decode(payload, now, wall):
    if payload.get("status") != "success" or payload.get("data", {}).get("resultType") != "vector":
        raise MonitorError("WAN response unavailable")
    rows = payload["data"]["result"]
    values = {}
    for row in rows:
        name = row["metric"]["__name__"]
        if name in values:
            raise MonitorError("WAN source is ambiguous")
        values[name] = numeric(row["value"][1])
    names = ("unpoller_device_wan_receive_rate_bytes", "unpoller_device_wan_transmit_rate_bytes",
             "unpoller_prometheus_cache_age_seconds", "source_timestamp", "up")
    if any(values.get(k) is None for k in names) or values["up"] != 1:
        raise MonitorError("WAN source unavailable")
    timestamp = values["source_timestamp"]
    if timestamp > wall + 5:
        raise MonitorError("WAN source clock mismatch")
    age = max(0, wall - timestamp) + values["unpoller_prometheus_cache_age_seconds"]
    if age > 90:
        raise MonitorError("WAN source stale")
    return {"at": now, "source_age": age, "rx": values[names[0]], "tx": values[names[1]]}


def current(snapshot, now):
    return bool(snapshot and not snapshot.get("error") and 0 <= now - snapshot.get("at", -1000) <= 40
                and snapshot.get("source_age", 1000) + now - snapshot.get("at", -1000) <= 90)


class WanMonitor:
    def __init__(self, config):
        self.config = validate(config)
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.snapshot = {"error": "Connecting", "device": config["device"], "port": config["port"], "gateway": config.get("gateway")}
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()

    def fetch(self):
        url = urlsplit(self.config["url"])
        factory = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        conn = factory(url.hostname, url.port, timeout=3)
        try:
            conn.request("GET", "/api/v1/query?" + urlencode({"query": query(self.config)}),
                         headers={"Accept": "application/json"})
            response = conn.getresponse()
            if response.status != 200:
                raise MonitorError("WAN source unavailable")
            raw = response.read(65537)
            if len(raw) > 65536:
                raise MonitorError("WAN response too large")
            return decode(json.loads(raw), time.monotonic(), time.time())
        finally:
            conn.close()

    def _worker(self):
        while not self.stop.is_set():
            try:
                data = self.fetch()
                data.update(device=self.config["device"], port=self.config["port"], gateway=self.config.get("gateway"))
            except Exception:
                data = {"error": "WAN unavailable / stale", "device": self.config["device"], "port": self.config["port"], "gateway": self.config.get("gateway")}
            with self.lock:
                self.snapshot = data
            self.stop.wait(15)

    def read(self):
        with self.lock:
            return copy.deepcopy(self.snapshot)

    def close(self):
        self.stop.set()
