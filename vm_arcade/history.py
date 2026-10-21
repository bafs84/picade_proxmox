"""Bounded in-memory traffic histories. Missing samples remain gaps."""
from collections import OrderedDict
from .telemetry import numeric


class Histories:
    def __init__(self, window=300):
        self.window = window
        self.series = OrderedDict()
        self.host_version = None

    def add(self, key, timestamp, rx, tx, now):
        rows = self.series.setdefault(key, [])
        self.series.move_to_end(key)
        if not rows or timestamp > rows[-1][0]:
            rows.append((timestamp, numeric(rx), numeric(tx)))
        self.series[key] = [r for r in rows if now - self.window <= r[0] <= now][-1201:]
        while len(self.series) > 512:
            self.series.popitem(last=False)

    def read(self, key, now):
        return [r for r in self.series.get(key, []) if now - self.window <= r[0] <= now]

    def sample(self, snapshots, hosts, now, stale, wall=None):
        version = tuple((h["sampled_at"], h["traffic_current"]) for h in hosts)
        if version != self.host_version:
            self.host_version = version
            totals = []
            for direction in ("rx", "tx"):
                values = [h["uplink"][direction] for h in hosts]
                totals.append(sum(values) if values and all(v is not None for v in values) else None)
            self.add("hosts", now, totals[0], totals[1], now)
        for host in hosts:
            for guest in host["guests"]:
                key = host["name"] + "/" + guest["key"]
                self.add(key, host["sampled_at"], guest["rx"], guest["tx"], now)


def segments(rows, direction, max_gap):
    """Separate outages, counter resets and oversized sampling gaps."""
    result, run = [], []
    for row in rows:
        value = row[direction]
        if value is None or (run and row[0] - run[-1][0] > max_gap):
            if run:
                result.append(run)
            run = []
        if value is not None:
            run.append((row[0], value))
    if run:
        result.append(run)
    return result
