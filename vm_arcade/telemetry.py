"""TLS-verified GET-only collector. Python 3.7+, standard library only."""
import copy
import http.client
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import ssl
import stat
import threading
import time
from urllib.parse import urlsplit


class MonitorError(Exception):
    """Short, safe display text: never include a URL, token or response body."""


def numeric(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError):
        return None


def ratio(value):
    value = numeric(value)
    return value * 100 if value is not None else None


def private_text(path):
    path = Path(path).expanduser()
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise MonitorError("Credential file must have mode 0600")
    if info.st_uid != os.getuid():
        raise MonitorError("Credential file has a different owner")
    return path.read_text().strip()


def load_config(path):
    path = Path(path).expanduser()
    try:
        data = json.loads(private_text(path))
        if "exit_key" in data and (type(data["exit_key"]) is not int or not 0 < data["exit_key"] < 2**31):
            raise ValueError()
        nodes = data["nodes"]
        if not isinstance(nodes, list) or not 1 <= len(nodes) <= 12:
            raise ValueError()
        seen = set()
        for node in nodes:
            name = node["name"]
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) or name in seen:
                raise ValueError()
            seen.add(name)
            url = urlsplit(node["url"])
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                raise ValueError()
            if url.path not in ("", "/") or url.query or url.fragment:
                raise ValueError()
            if bool(node.get("ca_file")) == bool(node.get("tls_sha256")):
                raise ValueError()
            if node.get("tls_sha256") and not re.fullmatch(r"[a-fA-F0-9]{64}", node["tls_sha256"]):
                raise ValueError()
            for key in ("token_file",) + (("ca_file",) if node.get("ca_file") else ()):
                target = Path(node[key]).expanduser()
                node[key] = str(target if target.is_absolute() else path.parent / target)
            if node.get("cpu_ssh"):
                ssh = node["cpu_ssh"]
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", ssh["user"]):
                    raise ValueError()
                if ssh.get("control_dir"):
                    target = Path(ssh["control_dir"]).expanduser()
                    info = target.stat()
                    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
                        raise ValueError()
                    ssh["control_dir"] = str(target)
                if ssh.get("jump_host"):
                    known = {urlsplit(n["url"]).hostname for n in nodes}
                    if ssh["jump_host"] not in known or ssh["jump_host"] == url.hostname:
                        raise ValueError()
                for key in ("identity_file", "known_hosts_file"):
                    target = Path(ssh[key]).expanduser()
                    ssh[key] = str(target if target.is_absolute() else path.parent / target)
                    if not Path(ssh[key]).is_file():
                        raise ValueError()
            if not re.fullmatch(r"[^=\s\r\n]+@[^=\s\r\n]+![^=\s\r\n]+", node["token_id"]):
                raise ValueError()
        if data.get("wan_monitor"):
            from .wan import validate
            validate(data["wan_monitor"])
        if data.get("gps_monitor"):
            from .gps import validate
            validate(data["gps_monitor"])
        poll = numeric(data.get("poll_seconds", .5))
        if poll is None:
            raise ValueError()
        data["poll_seconds"] = max(.5, min(60, poll))
        traffic_poll = numeric(data.get("traffic_poll_seconds", .5))
        if traffic_poll is None:
            raise ValueError()
        data["traffic_poll_seconds"] = max(.25, min(1, traffic_poll))
        data["stale_seconds"] = max(data["poll_seconds"] * 3, int(data.get("stale_seconds", 20)))
        return data
    except MonitorError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise MonitorError("Configuration missing or invalid")


class Client:
    def __init__(self, node):
        self.name = node["name"]
        self.url = urlsplit(node["url"])
        self.pin = node.get("tls_sha256", "").lower()
        if self.pin:
            # Explicit, preconfigured identity pin; never auto-accept a changed certificate.
            self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            self.context.check_hostname = False
            self.context.verify_mode = ssl.CERT_NONE
        else:
            self.context = ssl.create_default_context(cafile=node["ca_file"])
        token = private_text(node["token_file"])
        if not token or any(c in token for c in "\r\n="):
            raise MonitorError("Invalid token file")
        self.authorization = "PVEAPIToken={}={}".format(node["token_id"], token)

    def get(self, path):
        # The renderer cannot supply arbitrary destinations or mutation methods.
        prefix = "/nodes/" + self.name
        allowed = (prefix + "/status", prefix + "/qemu?full=1",
                   prefix + "/lxc", prefix + "/storage", prefix + "/network", prefix + "/netstat",
                   prefix + "/rrddata?timeframe=hour&cf=AVERAGE")
        if path not in allowed and not re.fullmatch(re.escape(prefix) + r"/(qemu|lxc)/[1-9][0-9]*/config\?current=1", path):
            raise MonitorError("Unsupported telemetry endpoint")
        conn = http.client.HTTPSConnection(self.url.hostname, self.url.port or 8006,
                                           timeout=3, context=self.context)
        try:
            conn.connect()
            if self.pin and not hmac.compare_digest(
                    hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest(), self.pin):
                raise MonitorError("TLS certificate pin changed")
            conn.request("GET", "/api2/json" + path,
                         headers={"Authorization": self.authorization, "Accept": "application/json"})
            response = conn.getresponse()
            if response.status != 200:
                raise MonitorError({401: "Authentication failed", 403: "Permission denied",
                                    404: "Endpoint unavailable"}.get(response.status, "API unavailable"))
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise MonitorError("API response too large")
            return json.loads(raw)["data"]
        except MonitorError:
            raise
        except ssl.SSLError:
            raise MonitorError("TLS verification failed")
        except (OSError, http.client.HTTPException):
            raise MonitorError("Connection unavailable")
        except (ValueError, KeyError, TypeError):
            raise MonitorError("Invalid API response")
        finally:
            conn.close()


class Rates:
    """Counter deltas use monotonic time. Restarts/resets never become spikes."""
    def __init__(self):
        self.previous = {}

    def sample(self, key, guest, now):
        counters = {k: numeric(guest.get(k)) for k in ("netin", "netout", "diskread", "diskwrite")}
        uptime = numeric(guest.get("uptime"))
        previous = self.previous.get(key)
        self.previous[key] = (now, uptime, counters)
        result = dict.fromkeys(counters)
        if not previous or guest.get("status") != "running":
            return result
        then, old_uptime, old = previous
        elapsed = now - then
        if elapsed <= 0 or elapsed > 120 or uptime is None or old_uptime is None or uptime < old_uptime:
            return result
        for field, value in counters.items():
            if value is not None and old[field] is not None and value >= old[field]:
                result[field] = (value - old[field]) / elapsed
        return result


def normalize_guest(raw, kind, rates, now):
    vmid = int(raw["vmid"])
    return {
        "key": "{}:{}".format(kind, vmid), "id": vmid, "kind": kind,
        "name": str(raw.get("name") or "{} {}".format(kind.upper(), vmid)),
        "status": raw.get("status", "unknown"), "cpu": ratio(raw.get("cpu")),
        "cpus": numeric(raw.get("cpus")), "mem": numeric(raw.get("mem")),
        "maxmem": numeric(raw.get("maxmem")), "maxdisk": numeric(raw.get("maxdisk")),
        "uptime": numeric(raw.get("uptime")), "rates": rates.sample((kind, vmid), raw, now),
    }


class NodePoller:
    def __init__(self, client):
        self.client = client
        self.rates = Rates()
        self.sections = {}
        self.rrd_at = float("-inf")
        self.slow_at = float("-inf")
        self.errors = {}
        from .topology import NetworkCollector
        self.network = NetworkCollector(client)

    def poll(self, now=None):
        fixed_time = now
        now = time.monotonic() if now is None else now
        prefix = "/nodes/" + self.client.name
        errors = self.errors.copy()
        slow = now - self.slow_at >= 5
        # Update sections independently: failure cannot turn cached guests into a live inventory.
        endpoints = [("status", "/status"), ("qemu", "/qemu?full=1"),
                     ("lxc", "/lxc")]
        if slow:
            endpoints.append(("storage", "/storage"))
        if now - self.rrd_at >= 60:
            endpoints.append(("rrd", "/rrddata?timeframe=hour&cf=AVERAGE"))
        for section, endpoint in endpoints:
            try:
                raw = self.client.get(prefix + endpoint)
                sampled_at = time.monotonic() if fixed_time is None else fixed_time
                if section == "status":
                    if not isinstance(raw, dict) or "uptime" not in raw:
                        raise ValueError()
                elif not isinstance(raw, list):
                    raise ValueError()
                if section in ("qemu", "lxc"):
                    raw = [normalize_guest(g, section, self.rates, sampled_at)
                           for g in raw]
                    valid = {(section, g["id"]) for g in raw}
                    self.rates.previous = {k: v for k, v in self.rates.previous.items()
                                           if k[0] != section or k in valid}
                if section == "rrd":
                    raw = sorted((r for r in raw if isinstance(r, dict) and numeric(r.get("time")) is not None),
                                 key=lambda row: row["time"])
                    self.rrd_at = now
                self.sections[section] = {"at": sampled_at, "data": raw}
                errors.pop(section, None)
            except MonitorError as exc:
                errors[section] = str(exc)
            except (TypeError, ValueError, KeyError, OverflowError):
                errors[section] = "Invalid API response"
        # Slow topology/storage work remains bounded independently of 2 Hz metrics.
        if slow:
            self.slow_at = now
            try:
                sampled_at = time.monotonic() if fixed_time is None else fixed_time
                snapshot = {"sections": self.sections, "errors": errors}
                topology = self.network.poll(snapshot, sampled_at, fixed_time is not None)
                self.sections["topology"] = {"at": sampled_at, "data": copy.deepcopy(topology)}
                errors.pop("topology", None)
            except (MonitorError, ValueError, TypeError, KeyError, OverflowError):
                errors["topology"] = "Network telemetry unavailable"
        self.errors = errors
        return {"name": self.client.name, "sections": copy.deepcopy(self.sections), "errors": errors.copy()}


def section_current(snapshot, section, now, max_age=20):
    data = snapshot.get("sections", {}).get(section)
    return bool(data and section not in snapshot.get("errors", {})
                and 0 <= now - data["at"] <= max_age)


def section_data(snapshot, section, default=None):
    return snapshot.get("sections", {}).get(section, {}).get("data", default)


class Fleet:
    """Independent metrics, traffic and optional CPU streams; never block rendering."""
    def __init__(self, config, client_factory=Client):
        self.config = config
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.snapshots = [{"name": n["name"], "sections": {}, "errors": {"status": "Connecting", "traffic": "Connecting"}}
                          for n in config["nodes"]]
        self.threads = []
        for index, node in enumerate(config["nodes"]):
            thread = threading.Thread(target=self._worker, args=(index, node, client_factory), daemon=True)
            self.threads.append(thread)
            thread.start()
            traffic = threading.Thread(target=self._traffic_worker, args=(index, node, client_factory), daemon=True)
            self.threads.append(traffic)
            traffic.start()
            if node.get("cpu_ssh"):
                cores = threading.Thread(target=self._core_worker, args=(index, node), daemon=True)
                self.threads.append(cores)
                cores.start()

    def _worker(self, index, node, factory):
        failures = 0
        poller = None
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                if poller is None:
                    poller = NodePoller(factory(node))
                result = poller.poll()
            except Exception:
                # Never print exception bodies: TLS/HTTP libraries may include private input.
                result = {"name": node["name"], "sections": {}, "errors": {"status": "Check credentials / CA"}}
            with self.lock:
                current = self.snapshots[index]
                for section in ("traffic", "cores"):
                    if section in current["sections"]:
                        result["sections"][section] = current["sections"][section]
                    if section in current["errors"]:
                        result["errors"][section] = current["errors"][section]
                self.snapshots[index] = result
            failures = failures + 1 if "status" in result["errors"] else 0
            delay = min(60, self.config["poll_seconds"] * 2 ** min(failures, 3))
            self.stop.wait(max(0.1, delay - (time.monotonic() - started)))

    def _core_worker(self, index, node):
        from .cores import stream

        def publish(data, timestamp):
            with self.lock:
                snapshot = self.snapshots[index]
                if data is None:
                    snapshot["errors"]["cores"] = "CPU thread counters unavailable"
                else:
                    snapshot["sections"]["cores"] = {"at": timestamp, "data": data}
                    snapshot["errors"].pop("cores", None)
        stream(node, self.stop, publish)

    def _traffic_worker(self, index, node, factory):
        from .topology import sample_interfaces
        client, rates = None, Rates()
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                if client is None:
                    client = factory(node)
                with self.lock:
                    snapshot = copy.deepcopy(self.snapshots[index])
                data, sampled_at = sample_interfaces(client, snapshot, rates)
                with self.lock:
                    self.snapshots[index]["sections"]["traffic"] = {"at": sampled_at, "data": data}
                    self.snapshots[index]["errors"].pop("traffic", None)
                delay = self.config.get("traffic_poll_seconds", .5)
            except Exception:
                with self.lock:
                    self.snapshots[index]["errors"]["traffic"] = "Network counters unavailable"
                rates.previous.clear()
                delay = max(1, self.config.get("traffic_poll_seconds", .5))
            self.stop.wait(max(.01, delay - (time.monotonic() - started)))

    def read(self):
        with self.lock:
            return copy.deepcopy(self.snapshots)

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=.4)
