"""Configured network attachments and measured per-NIC traffic; no inferred flows."""
import re
import time

from .telemetry import Rates, numeric, section_current, section_data


def interfaces(config):
    """Discard all non-network fields, including any guest initialization secrets."""
    if not isinstance(config, dict):
        raise ValueError("Invalid guest network configuration")
    result = []
    for dev, value in sorted(config.items()):
        if not re.fullmatch(r"net[0-9]+", dev) or not isinstance(value, str):
            continue
        fields = dict(item.split("=", 1) for item in value.split(",") if "=" in item)
        result.append({"dev": dev, "bridge": fields.get("bridge"),
                       "tag": fields.get("tag"), "trunks": fields.get("trunks"),
                       "down": fields.get("link_down") == "1",
                       "firewall": fields.get("firewall") == "1"})
    return result


def network_rows(rows):
    if not isinstance(rows, list):
        raise ValueError("Invalid host network configuration")
    fields = ("iface", "type", "active", "cidr", "cidr6", "gateway", "gateway6",
              "bridge_ports", "bridge_vlan_aware", "bond_slaves", "vlan_raw_device", "vlan_id")
    return [{key: row[key] for key in fields if key in row} for row in rows]


class NetworkCollector:
    def __init__(self, client):
        self.client = client
        self.rates = Rates()
        self.cache = {}
        self.network = None
        self.network_error = None
        self.network_at = float("-inf")

    def poll(self, snapshots, now, fixed_time=False):
        # Import here to keep the general telemetry collector independent of graph code.
        from .telemetry import MonitorError
        prefix = "/nodes/" + self.client.name
        if now - self.network_at >= 60:
            self.network_at = now
            try:
                self.network = {"at": now, "data": network_rows(self.client.get(prefix + "/network"))}
                self.network_error = None
            except (MonitorError, ValueError, TypeError, KeyError):
                self.network_error = "Host network unavailable"
        guests = [g for kind in ("qemu", "lxc") for g in section_data(snapshots, kind, [])
                  if section_current(snapshots, kind, now, 20)]
        valid = {g["key"] for g in guests}
        self.cache = {k: v for k, v in self.cache.items() if k in valid}
        for guest in guests:
            key = guest["key"]
            cached = self.cache.get(key, {})
            if now - cached.get("attempt", float("-inf")) >= 60:
                try:
                    raw = self.client.get(prefix + "/{}/{}/config?current=1".format(guest["kind"], guest["id"]))
                    cached = {"at": now, "data": interfaces(raw)}
                except (MonitorError, ValueError, TypeError, KeyError):
                    cached = dict(cached, error="Guest network unavailable")
                cached["attempt"] = now
                self.cache[key] = cached
        traffic, sampled_at = sample_interfaces(self.client, snapshots, self.rates, now if fixed_time else None)
        return {"network": self.network, "network_error": self.network_error,
                "attachments": self.cache, "traffic": traffic, "sampled_at": sampled_at}


def sample_interfaces(client, snapshot, rates, fixed_time=None):
    counters = client.get("/nodes/" + client.name + "/netstat")
    if not isinstance(counters, list):
        raise ValueError("Invalid interface counters")
    sampled_at = time.monotonic() if fixed_time is None else fixed_time
    guests = [g for kind in ("qemu", "lxc") for g in section_data(snapshot, kind, [])
              if section_current(snapshot, kind, sampled_at, 20)]
    by_id = {g["id"]: g for g in guests}
    traffic, seen = {}, set()
    for row in counters:
        guest = by_id.get(int(row["vmid"]))
        if not guest or not re.fullmatch(r"net[0-9]+", str(row.get("dev", ""))):
            continue
        key = (guest["key"], row["dev"])
        seen.add(key)
        sample = dict(guest, netin=row.get("in"), netout=row.get("out"))
        rate = rates.sample(key, sample, sampled_at)
        traffic["/".join(key)] = {"rx": rate["netin"], "tx": rate["netout"]}
    rates.previous = {k: v for k, v in rates.previous.items() if k in seen}
    return traffic, sampled_at


def graph_host(snapshot, now, stale):
    """The graph never promotes old configuration or failed counters to live traffic."""
    data = section_data(snapshot, "topology", {})
    fresh = section_current(snapshot, "topology", now, stale)
    has_fast = "traffic" in snapshot.get("sections", {}) or "traffic" in snapshot.get("errors", {})
    traffic_current = section_current(snapshot, "traffic", now, 3) if has_fast else fresh
    traffic = section_data(snapshot, "traffic", {}) if has_fast else data.get("traffic", {})
    sampled_at = snapshot.get("sections", {}).get("traffic" if has_fast else "topology", {}).get("at", now)
    network = data.get("network") or {}
    network_live = (not data.get("network_error") and 0 <= now - network.get("at", -1000) <= 180)
    bridges = {r["iface"]: r for r in network.get("data", []) if r.get("type") in ("bridge", "OVSBridge")}
    gateways = sorted({r["gateway"] for r in network.get("data", []) if r.get("gateway")})
    guests = []
    for kind in ("qemu", "lxc"):
        for guest in section_data(snapshot, kind, []):
            current = fresh and traffic_current and section_current(snapshot, kind, now, stale)
            config = data.get("attachments", {}).get(guest["key"], {})
            configured = not config.get("error") and 0 <= now - config.get("at", -1000) <= 180
            nics = []
            for interface in config.get("data", []):
                nic = dict(interface)
                bridge = bridges.get(nic["bridge"])
                nic["segment"] = ("TRUNK " + nic["trunks"] + (" / PVID " + nic["tag"] if nic.get("tag") else "") if nic.get("trunks") else
                                  "VLAN " + nic["tag"] if nic.get("tag") else "UNTAGGED")
                nic["current"] = bool(current and configured and network_live and bridge and
                                      bridge.get("active") and not nic["down"] and guest["status"] == "running")
                rate = traffic.get(guest["key"] + "/" + nic["dev"], {})
                nic["rx"] = numeric(rate.get("rx")) if nic["current"] else None
                nic["tx"] = numeric(rate.get("tx")) if nic["current"] else None
                nics.append(nic)
            g = dict(guest, nics=nics, current=current, configured=configured)
            # Unknown NICs cannot be treated as zero; total is each NIC exactly once.
            for direction in ("rx", "tx"):
                values = [n[direction] for n in nics]
                g[direction] = sum(values) if values and all(v is not None for v in values) else None
            guests.append(g)
    inventory_current = fresh and traffic_current and all(section_current(snapshot, kind, now, stale) for kind in ("qemu", "lxc"))
    running = [g for g in guests if g["status"] == "running"]
    uplink = {}
    for direction in ("rx", "tx"):
        values = [g[direction] for g in running]
        uplink[direction] = sum(values) if inventory_current and all(v is not None for v in values) else None
    return {"name": snapshot["name"], "bridges": bridges, "gateways": gateways,
            "uplink": uplink, "sampled_at": sampled_at, "traffic_current": traffic_current,
            "network_current": network_live and fresh, "configuration_current": all(g["configured"] for g in guests),
            "guests": sorted(guests, key=lambda g: (g["id"], g["kind"]))}
