"""Measured dashboard state: bounded history, honest gaps and visual interpolation."""
from collections import OrderedDict
import math

from .history import Histories, segments
from .telemetry import numeric, ratio, section_current, section_data
from .topology import graph_host


def fraction(value, total):
    value, total = numeric(value), numeric(total)
    return 100 * value / total if value is not None and total else None


class TrafficParticles:
    """Emit measured byte volumes at the inlet; every dot completes its journey."""
    transit = 1.0
    quantum = 4096
    max_emissions = 512  # Bound work at extreme rates without deleting travelling dots.

    def __init__(self):
        self.at = None
        self.rates = [None, None]
        self.credit = [0.0, 0.0]
        self.particles = [[], []]  # (stable ID, birth timestamp)
        self.next_id = 0
        self.scale = 64 * 1024
        self.limited = False

    def positions(self, rx, tx, now):
        values = [numeric(rx), numeric(tx)]
        self.scale = 64 * 1024
        while max([v for v in values if v is not None] or [0]) > self.scale:
            self.scale *= 2
        self.limited = any(v is not None and v / self.quantum > self.max_emissions for v in values)
        elapsed = now - self.at if self.at is not None else 0
        continuous = self.at is not None and 0 <= elapsed <= 1
        result = []
        for direction, value in enumerate(values):
            dots = [(identity, birth) for identity, birth in self.particles[direction]
                    if 0 <= now - birth < self.transit]
            old_rate = self.rates[direction]
            if continuous and old_rate is not None and old_rate > 0:
                # Integrate the previously displayed rate up to this frame. The
                # new sample controls future emissions, never existing particles.
                per_second = min(self.max_emissions, old_rate / self.quantum)
                credit = self.credit[direction] + elapsed * per_second
                count = int(math.floor(credit + 1e-10))
                self.credit[direction] = max(0, credit - count)
                for index in range(count):
                    birth = now - (count - 1 - index + self.credit[direction]) / per_second
                    if now - birth < self.transit:
                        dots.append((self.next_id, birth))
                        self.next_id += 1
            if not continuous or value is None:
                # Never catch up unseen traffic after an outage or hidden view.
                self.credit[direction] = 0
            self.particles[direction] = dots
            positions = [(now - birth) / self.transit for _, birth in dots]
            result.append([1 - p for p in positions] if direction else positions)
        self.at, self.rates = now, values
        return result


def host_cpu(snapshot, now, stale):
    if section_current(snapshot, 'cores', now, 3):
        value = section_data(snapshot, 'cores', {}).get('cpu')
        if value is not None:
            return value, snapshot['sections']['cores']['at']
    section = snapshot.get('sections', {}).get('status', {})
    return (ratio(section.get('data', {}).get('cpu')) if section_current(snapshot, 'status', now, stale) else None,
            section.get('at', now))


class Metrics:
    def __init__(self):
        self.history = Histories()
        self.versions = OrderedDict()
        self.tweens = OrderedDict()

    def add(self, key, timestamp, values, now):
        valid = any(v is not None for v in values)
        version = (timestamp, valid)
        if self.versions.get(key) != version:
            old = self.versions.get(key)
            # An outage is an event, not another reading with an old timestamp.
            at = now if not valid and old and old[1] else timestamp
            self.history.add(key, at, values[0], values[1], now)
            self.versions[key] = version
            self.versions.move_to_end(key)
            while len(self.versions) > 512:
                self.versions.popitem(last=False)

    def observe(self, snapshots, now, stale):
        hosts = [graph_host(s, now, stale) for s in snapshots]
        for s, h in zip(snapshots, hosts):
            name = s['name']
            cpu, at = host_cpu(s, now, stale)
            self.add(name + '/cpu', at, (cpu, None), now)
            data = section_data(s, 'status', {})
            mem = data.get('memory', {})
            current = section_current(s, 'status', now, stale)
            self.add(name + '/ram', s.get('sections', {}).get('status', {}).get('at', now),
                     (fraction(mem.get('used'), mem.get('total')) if current else None, None), now)
            self.add(name + '/net', h['sampled_at'], (h['uplink']['rx'], h['uplink']['tx']), now)
            for g in h['guests']:
                key = name + '/' + g['key']
                self.add(key + '/net', h['sampled_at'], (g['rx'], g['tx']), now)
                valid = section_current(s, g['kind'], now, stale) and g['status'] == 'running'
                rates = g.get('rates', {})
                at = s['sections'][g['kind']]['at']
                self.add(key + '/disk', at, tuple(rates.get(k) if valid else None for k in ('diskread', 'diskwrite')), now)
        return hosts

    def smooth(self, key, value, now):
        value = numeric(value)
        old = self.tweens.get(key)
        if value is None:
            self.tweens.pop(key, None)
            return None
        if old and 0 <= now - old[0] < 1:
            value = old[1] + (value - old[1]) * (1 - math.exp(-(now - old[0]) / .12))
        self.tweens[key] = (now, value)
        self.tweens.move_to_end(key)
        while len(self.tweens) > 512:
            self.tweens.popitem(last=False)
        return value

    def peak(self, key, now, direction=1):
        return max([0] + [r[direction] for r in self.history.read(key, now)
                          if now - r[0] <= 60 and r[direction] is not None])


def cluster(snapshots, hosts, now, stale):
    result = {'online': 0, 'hosts': len(snapshots)}
    cpus, used, memory, capacity, available, running, guests = [], [], [], [], [], [], []
    for s in snapshots:
        status = section_data(s, 'status', {})
        current = section_current(s, 'status', now, stale)
        result['online'] += int(current)
        cpu, _ = host_cpu(s, now, stale)
        threads = numeric(status.get('cpuinfo', {}).get('cpus'))
        cpus.append(threads if current else None)
        used.append(cpu * threads if cpu is not None and threads and current else None)
        mem = status.get('memory', {})
        for target, field in ((memory, 'used'), (capacity, 'total'), (available, 'available')):
            target.append(numeric(mem.get(field)) if current else None)
        inventory = section_data(s, 'qemu', []) + section_data(s, 'lxc', [])
        fresh = all(section_current(s, kind, now, stale) for kind in ('qemu', 'lxc'))
        guests.append(len(inventory) if fresh else None)
        running.append(sum(g['status'] == 'running' for g in inventory) if fresh else None)
    def total(values):
        return sum(values) if values and all(v is not None for v in values) else None
    threads, busy = total(cpus), total(used)
    result.update(cpu=busy / threads if busy is not None and threads else None,
                  mem=total(memory), maxmem=total(capacity), available=total(available),
                  running=total(running), guests=total(guests),
                  rx=total([h['uplink']['rx'] for h in hosts]),
                  tx=total([h['uplink']['tx'] for h in hosts]))
    return result


def trace(d, rows, x, y, width, height, now, maximum=None, directions=(1,), window=60, max_gap=3):
    from .display import CYAN, VIOLET, LINE, MUTED
    d.line(x, y + height, x + width, y + height, LINE)
    d.line(x, y, x + width, y, LINE)
    rows = [r for r in rows if now - window <= r[0] <= now]
    maximum = maximum or max([1] + [r[i] for r in rows for i in directions if r[i] is not None])
    for direction in directions:
        color = CYAN if direction == 1 else VIOLET
        for run in segments(rows, direction, max_gap):
            previous = None
            for at, value in run:
                px = x + (1 - (now - at) / window) * width
                py = y + height * (1 - min(1, value / maximum))
                if previous:
                    d.line(*previous, px, py, color)
                previous = px, py
            if previous:
                d.rect((previous[0] - 1, previous[1] - 1, 3, 3), color)
    if not any(r[i] is not None for r in rows for i in directions):
        d.text('Awaiting samples', x + 2, y + max(0, height / 2 - 5), 9, MUTED, width=width - 4)
