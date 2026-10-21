"""Animated native node/link graph. Motion represents sampled rates, not packets."""
import math
from collections import OrderedDict
import time

from .display import BG, PANEL, INK, MUTED, LINE, ACTIVE, CYAN, VIOLET, AMBER, sdl, fmt
from .telemetry import numeric, ratio, section_data, section_current
from .topology import graph_host
from .wan import current as wan_current
from .history import Histories, segments

GREEN = (138, 209, 164)
COLORS = (CYAN, VIOLET, AMBER, GREEN)


def rate(value):
    value = numeric(value)
    if value is None:
        return "--"
    for unit in ("B/s", "kB/s", "MB/s", "GB/s"):
        if value < 1000 or unit == "GB/s":
            return ("{:.0f}" if value >= 100 else "{:.1f}").format(value) + unit
        value /= 1000


def particle_positions(value, now, reverse=False, seed=0, scale=100000):
    """Linear density on a shared bytes/s scale. Low rates emit occasional particles."""
    if value is None or value <= 0:
        return []
    density = min(12, 10 * value / max(1, scale))
    phase = (now / 1.6 * density + seed) % 1
    positions = [(phase + i) / density for i in range(math.ceil(density)) if (phase + i) / density < 1]
    return [1 - p for p in positions] if reverse else positions


class FlowParticles:
    """Move existing particles smoothly while sampled rates change the emission rate."""
    def __init__(self):
        self.flows = OrderedDict()

    def positions(self, key, value, now, scale, reverse=False, seed=0):
        if value is None or value <= 0:
            self.flows.pop(key, None)
            return []
        density = min(12, 10 * value / max(1, scale))
        previous = self.flows.get(key)
        if previous is None or not 0 <= now - previous[0] <= 1:
            positions = particle_positions(value, now, seed=seed, scale=scale)
            credit = 0
        else:
            at, old, credit = previous
            elapsed = now - at
            positions = [p + elapsed / 1.6 for p in old if p + elapsed / 1.6 < 1]
            credit += elapsed * density / 1.6
            while credit >= 1:
                credit -= 1
                positions.append(min(.99, credit / max(.001, density)))
        self.flows[key] = (now, positions[-12:], credit)
        self.flows.move_to_end(key)
        while len(self.flows) > 512:
            self.flows.popitem(last=False)
        return [1 - p for p in positions] if reverse else positions


class VolumeParticles:
    """Byte-volume emission; never normalize light traffic to a full link."""
    def __init__(self, quantum=32 * 1024):
        self.quantum = quantum
        self.at = None
        self.lanes = [([], 0), ([], 0)]

    def positions(self, rx, tx, now):
        values = [numeric(rx), numeric(tx)]
        if any(value is None for value in values):
            self.at, self.lanes = None, [([], 0), ([], 0)]
            return [[], []]
        # Shared, labelled units only grow in powers of four for very high rates.
        # Keep the unit after a burst: subsequent traffic visibly gets quieter.
        quantum = self.quantum
        while max(values) / self.quantum > 24:
            self.quantum *= 4
        elapsed = now - self.at if self.at is not None else 0
        if not 0 <= elapsed <= 1 or quantum != self.quantum:
            elapsed, self.lanes = 0, [([], 0), ([], 0)]
        self.at = now
        result = []
        for i, value in enumerate(values):
            old, credit = self.lanes[i]
            positions = [p + elapsed / 1.6 for p in old if p + elapsed / 1.6 < 1]
            if value == 0:
                positions, credit = [], 0
            else:
                credit += value * elapsed
                while credit + 1e-9 * self.quantum >= self.quantum:
                    credit = max(0, credit - self.quantum)
                    positions.append(min(.99, credit / value / 1.6))
            self.lanes[i] = (positions[-64:], credit)
            result.append([1 - p for p in positions] if i else positions)
        return result

    def label(self):
        value, unit = (self.quantum / 1024, 'KiB') if self.quantum < 1024 ** 2 else (self.quantum / 1024 ** 2, 'MiB')
        return 'dot = {:g} {}'.format(value, unit)


def point_on_path(path, fraction):
    lengths = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(path, path[1:])]
    distance = fraction * sum(lengths)
    for i, length in enumerate(lengths):
        if distance <= length or i == len(lengths) - 1:
            f = min(1, distance / max(.001, length))
            a, b = path[i:i + 2]
            return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
        distance -= length
    return path[0]


def curve(a, b):
    """Small tessellated cubic; avoids new dependencies and old SDL incompatibilities."""
    middle = (a[1] + b[1]) / 2
    points = []
    for i in range(17):
        t, u = i / 16, 1 - i / 16
        points.append((u ** 3 * a[0] + 3 * u * u * t * a[0] + 3 * u * t * t * b[0] + t ** 3 * b[0],
                       u ** 3 * a[1] + 3 * u * u * t * middle + 3 * u * t * t * middle + t ** 3 * b[1]))
    return points


def edge(d, path, rx=None, tx=None, now=0, selected=False, dashed=False, seed=0, scale=None):
    base = (67, 109, 121) if selected else LINE
    for i, (a, b) in enumerate(zip(path, path[1:])):
        if not dashed or i % 3 != 1:
            d.line(*a, *b, base)
    for value, reverse, color, offset in ((rx, False, CYAN, -2), (tx, True, VIOLET, 2)):
        key = (tuple(path), reverse, seed)
        reference = scale if scale is not None else d.flow_scale
        for f in d.flow_state.positions(key, value, now, reference, reverse, seed):
            x, y = point_on_path(path, f)
            tail = point_on_path(path, max(0, min(1, f + (.035 if reverse else -.035))))
            # Separate opposite directions into two lanes, including horizontal WAN edges.
            near = point_on_path(path, min(1, f + .01))
            dx, dy = near[0] - x, near[1] - y
            length = max(.001, math.hypot(dx, dy))
            ox, oy = -dy / length * offset, dx / length * offset
            d.line(tail[0] + ox, tail[1] + oy, x + ox, y + oy, color)
            d.rect((x + ox - 1, y + oy - 1, 3, 3), color)


def circle(d, x, y, radius, color):
    last = None
    for step in range(41):
        theta = step * math.pi / 20
        point = x + math.cos(theta) * radius, y + math.sin(theta) * radius
        if last:
            d.line(*last, *point, color)
        last = point


def node(d, box, color=LINE, selected=False):
    x, y, w, h = box
    d.rect(box, ACTIVE if selected else PANEL)
    d.rect(box, color, True)
    for dx in (0, w - 9):
        d.line(x + dx, y, x + dx + 8, y, color)


def total(nics, direction):
    values = [n.get(direction) for n in nics]
    return sum(values) if values and all(v is not None for v in values) else None


class NetworkView:
    def __init__(self):
        self.hitboxes = []
        self.history = Histories()
        self.flows = FlowParticles()
        self.wan_particles = VolumeParticles()

    def hit(self, x, y):
        for box, host, guest in reversed(self.hitboxes):
            bx, by, w, h = box
            if bx <= x <= bx + w and by <= y <= by + h:
                return host, guest
        return None

    def observe(self, snapshots, now, stale):
        hosts = [graph_host(s, now, stale) for s in snapshots]
        self.history.sample(snapshots, hosts, now, stale)
        return hosts

    def draw(self, d, snapshots, selection, demo=False, stale=20, wan=None, now=None):
        now = time.monotonic() if now is None else now
        self.hitboxes = []
        hosts = self.observe(snapshots, now, stale)
        d.flow_state = self.flows
        d.flow_scale = max([1] + [h["uplink"][k] for h in hosts for k in ("rx", "tx") if h["uplink"][k] is not None])
        selected_host = hosts[selection.host]
        selection.resolve(selected_host["guests"])
        selected = next((g for g in selected_host["guests"] if g["key"] == selection.guest_key), None)
        d.color(BG)
        sdl.SDL_RenderClear(d.renderer)
        # Fine grid stays still so that measured flow is the only moving layer.
        for x in range(16, 800, 24):
            for y in range(70, 511, 24):
                d.rect((x, y, 1, 1), (23, 40, 46))
        d.text("NETWORK / FLOW", 16, 10, 21)
        d.text("VM/ARCADE   /   PROXMOX FABRIC", 16, 36, 10, MUTED)
        fresh_wan = wan_current(wan, now)
        all_current = all(section_current(s, "topology", now, stale) and h["network_current"]
                          and h["configuration_current"] and h["traffic_current"] for s, h in zip(snapshots, hosts))
        all_current = all_current and (wan is None or fresh_wan)
        mode = "DEMO / SIMULATED" if demo else "LIVE / READ ONLY" if all_current else "PARTIAL / STALE"
        d.rect((625, 17, 5, 5), CYAN if all_current and not demo else AMBER)
        d.text(mode, 0, 13, 11, AMBER if demo or not all_current else CYAN, right=784)
        d.text("RX >  " + "CYAN    TX <  VIOLET", 0, 36, 10, MUTED, right=784)
        d.line(16, 57, 784, 57)

        rx, tx = ((wan.get("rx"), wan.get("tx")) if fresh_wan else (None, None))
        gateways = sorted({g for h in hosts for g in h["gateways"]})
        gateway = gateways[0] if len(gateways) == 1 else "MULTIPLE ROUTES" if gateways else "UNKNOWN ROUTE"
        mapped = bool(wan and wan.get("gateway") == gateway)
        # An optional verified mapping links the site WAN source to this cluster's route.
        d.line(176, 113, 304, 113, LINE)
        d.line(176, 121, 304, 121, LINE)
        lanes = self.wan_particles.positions(rx if mapped else None, tx if mapped else None, now)
        for i, lane in enumerate(lanes):
            for fraction in lane:
                x, y = 176 + fraction * 128, 113 + i * 8
                color = VIOLET if i else CYAN
                d.line(x, y, x + (4 if i else -4), y, color)
                d.rect((x - 1, y - 1, 3, 3), color)
        d.text(self.wan_particles.label(), 182, 157, 9, MUTED, width=120)
        node(d, (16, 83, 160, 70), CYAN)
        circle(d, 41, 109, 12, CYAN)
        d.line(29, 109, 53, 109, CYAN)
        d.line(41, 97, 41, 121, CYAN)
        d.text("INTERNET", 62, 97, 15)
        d.text("DELAYED RATE", 62, 122, 10, AMBER)
        d.text("download >", 196, 91, 10, CYAN)
        d.text("< upload", 203, 134, 10, VIOLET)
        node(d, (304, 83, 192, 70), CYAN if mapped else LINE)
        d.text(wan.get("device", "GATEWAY") if mapped else "GATEWAY", 316, 94, 14, width=168)
        d.text(gateway, 316, 119, 13, MUTED)
        d.text("UPLOAD", 526, 83, 10, VIOLET)
        d.text(rate(tx), 526, 99, 20, VIOLET)
        d.text("DOWNLOAD", 658, 83, 10, CYAN)
        d.text(rate(rx), 658, 99, 20, CYAN)
        wan_note = ("{} / {}s old".format(wan.get("port", "WAN"),
                    int(wan.get("source_age", 0) + now - wan["at"])) if fresh_wan else
                    "WAN unavailable" if wan else "WAN source not configured")
        d.text("WAN delayed / " + wan_note, 526, 67, 9, MUTED if fresh_wan else AMBER, width=258)
        self.spark(d, "hosts", (526, 126, 258, 52), now, "ALL VMs", max_gap=3)
        senders = [(g["tx"], h["name"], g) for h in hosts for g in h["guests"] if g["tx"] is not None]
        if senders:
            amount, name, guest = max(senders, key=lambda item: item[0])
            d.text("TOP NIC TX {} / {} {}".format(name, guest["name"], rate(amount)), 16, 173, 10, VIOLET, width=370)
        # Routes are configuration evidence, not measured guest-to-Internet paths.
        edge(d, [(400, 153 + i * 4) for i in range(15)], dashed=True)
        d.text("HOST LINKS = SUM OF VMs", 16, 187, 10, MUTED)
        d.text("2Hz / proportional particle density", 0, 187, 10, MUTED, right=784)
        d.line(140, 211, 660, 211)

        page = selection.host // 3
        visible_hosts = list(enumerate(hosts))[page * 3:page * 3 + 3]
        if len(hosts) > 3:
            d.text("PAGE {}/{}".format(page + 1, math.ceil(len(hosts) / 3)), 0, 166, 10, MUTED, right=784)
        for column, (index, host) in enumerate(visible_hosts):
            hx = 140 + column * 260
            uplink = host["uplink"]
            attached = gateway in host["gateways"] and host["network_current"]
            # Logical route overlay: sum of measured guest NIC activity, not WAN attribution.
            path = [(400, 153), (400, 211), (hx, 211), (hx, 230)]
            edge(d, path, uplink["rx"] if attached else None, uplink["tx"] if attached else None,
                 now, index == selection.host, dashed=not attached, seed=index * .31)
            self.host(d, snapshots[index], host, index, column, selection, now, stale)
        # Selected guest telemetry stays separate from route lines: no implied Internet attribution.
        node(d, (16, 520, 768, 55), LINE)
        if selected:
            d.text("{} / {} {}".format(selected_host["name"], selected["kind"].upper(), selected["id"]), 26, 527, 10, MUTED)
            d.text(selected["name"], 26, 544, 15, width=235)
            d.text("CPU {}%".format(fmt(selected.get("cpu") if selected["current"] else None, 0)), 284, 529, 11)
            mem = numeric(selected.get("mem")) if selected["current"] else None
            d.text("RAM {}G".format(fmt(mem / 1024 ** 3 if mem is not None else None)), 284, 548, 11, MUTED)
            d.text("RX " + rate(selected["rx"]), 402, 529, 13, CYAN)
            d.text("TX " + rate(selected["tx"]), 402, 548, 13, VIOLET)
            self.spark(d, selected_host["name"] + "/" + selected["key"], (552, 527, 222, 43), now,
                       "VM RX/TX" if selected["current"] else "VM STALE", max_gap=3)
        else:
            d.text("Waiting for guest inventory", 26, 539, 13, MUTED)
        d.text("NIC RX/TX includes LAN / 2Hz", 16, 585, 10, MUTED)
        d.text("VLAN config / isolation not tested", 0, 585, 10, MUTED, right=784)
        sdl.SDL_RenderPresent(d.renderer)

    def spark(self, d, key, box, now, title, max_gap):
        x, y, width, height = box
        rows = self.history.read(key, now)
        values = [v for row in rows for v in row[1:] if v is not None]
        span = min(300, max(30, now - rows[0][0] if rows else 30))
        title += " / {}s".format(int(span)) if span < 60 else " / {:.1f}m".format(span / 60)
        peak = max(values) if values else None
        scale = max(1, peak or 0)
        d.text(title, x, y, 9, MUTED, width=width - 72)
        d.text(rate(peak), 0, y, 9, MUTED, right=x + width)
        top, bottom = y + 15, y + height
        for fraction in (0, .5, 1):
            level = top + (bottom - top) * fraction
            d.line(x, level, x + width, level, LINE)
        for fraction in (.25, .5, .75):
            px = x + width * fraction
            d.line(px, top, px, bottom, (22, 40, 46))
        for direction, color in ((1, CYAN), (2, VIOLET)):
            for run in segments(rows, direction, max_gap):
                previous = None
                for stamp, value in run:
                    point = (x + width * (1 - (now - stamp) / span), bottom - value / scale * (bottom - top))
                    if previous:
                        d.line(*previous, *point, color)
                    previous = point
                if previous:
                    d.rect((previous[0] - 1, previous[1] - 1, 2, 2), color)
        if len([r for r in rows if any(v is not None for v in r[1:])]) < 2:
            d.text("collecting samples", x + 4, top + 3, 9, MUTED, width=width - 8)

    def host(self, d, snapshot, host, index, column, selection, now, stale):
        x = 16 + column * 260
        chosen = index == selection.host
        guests = host["guests"]
        position = next((i for i, g in enumerate(guests) if chosen and g["key"] == selection.guest_key), 0)
        page = position // 4
        visible = guests[page * 4:page * 4 + 4]
        d.rect((x, 230, 248, 278), PANEL)
        d.rect((x, 230, 248, 278), ACTIVE if chosen else LINE, True)
        d.text(host["name"], x + 10, 239, 16, CYAN if chosen else INK, width=70)
        status = section_data(snapshot, "status", {})
        cpu = ratio(status.get("cpu")) if section_current(snapshot, "status", now, stale) else None
        d.text("CPU {}%".format(fmt(cpu, 0)), 0, 243, 10, MUTED, right=x + 238)
        if len(guests) > 4:
            d.text("{}/{}".format(page + 1, math.ceil(len(guests) / 4)), x + 80, 244, 10, MUTED)
        bridge_names = sorted({n.get("bridge") or "UNKNOWN" for g in visible for n in g["nics"]}) or list(host["bridges"])[:1] or ["UNKNOWN"]
        if host["bridges"]:
            info = next(iter(host["bridges"].values()))
            d.text("{} / {}".format(info.get("cidr", "NO ADDRESS"), info.get("bridge_ports", "NO PORT")),
                   x + 10, 259, 9, MUTED, width=228)
        bridge_pos = {}
        for i, bridge in enumerate(bridge_names):
            bx = x + (i + .5) * 248 / len(bridge_names)
            bridge_pos[bridge] = bx, 294
            bw = min(210, 236 / len(bridge_names))
            node(d, (bx - bw / 2, 272, bw, 25), LINE)
            d.text(bridge, bx - bw / 2 + 8, 278, 11, MUTED, width=55 if len(bridge_names) == 1 else bw - 16)
            if len(bridge_names) == 1:
                d.text("R " + rate(host["uplink"]["rx"]) + " T " + rate(host["uplink"]["tx"]), 0, 280, 9,
                       MUTED, right=bx + bw / 2 - 6, width=bw - 60)
        groups = sorted({(n.get("bridge") or "UNKNOWN", n["segment"]) for g in visible for n in g["nics"]})
        segment_pos = {}
        for i, group in enumerate(groups):
            sx = x + (i + .5) * 248 / len(groups)
            segment_pos[group] = sx, 351
            color = GREEN if group[1] == "UNTAGGED" else COLORS[sum(ord(c) for c in group[1]) % 3]
            members = [n for g in visible for n in g["nics"] if (n.get("bridge") or "UNKNOWN", n["segment"]) == group
                       and g["status"] == "running"]
            highlighted = chosen and any(g["key"] == selection.guest_key and
                        any((n.get("bridge") or "UNKNOWN", n["segment"]) == group for n in g["nics"]) for g in visible)
            edge(d, curve(bridge_pos[group[0]], (sx, 331)), total(members, "rx"), total(members, "tx"), now, highlighted, seed=i * .2)
            sw = min(100, 240 / max(1, len(groups)))
            node(d, (sx - sw / 2, 331, sw, 23), color)
            d.text(group[1], sx - sw / 2 + 5, 337, 9, color, width=sw - 10)
        boxes = []
        for i, guest in enumerate(visible):
            gx, gy = x + 8 + (i % 2) * 120, 390 + (i // 2) * 61
            box = gx, gy, 112, 48
            active = chosen and guest["key"] == selection.guest_key
            for j, nic in enumerate(guest["nics"]):
                start = segment_pos[(nic.get("bridge") or "UNKNOWN", nic["segment"])]
                # Route around the first row when a lower-row VM is connected.
                end = gx + 56, gy
                path = curve(start, end) if i < 2 else [start, (gx - 3, 372), (gx - 3, gy - 7), (gx + 56, gy - 7), end]
                edge(d, path, nic["rx"], nic["tx"], now, active, not nic["current"], i * .27 + j * .13)
            boxes.append((box, guest, active))
        for box, guest, active in boxes:
            gx, gy, _, _ = box
            running = guest["current"] and guest["status"] == "running"
            color = CYAN if active else (67, 109, 121) if running else LINE
            node(d, box, color, active)
            self.hitboxes.append((box, index, guest["key"]))
            d.rect((gx + 6, gy + 8, 4, 4), CYAN if running else MUTED)
            d.text(str(guest["id"]) + " " + guest["name"], gx + 14, gy + 4, 10, INK if running else MUTED, width=93)
            if running:
                d.text("R " + rate(guest["rx"]), gx + 6, gy + 20, 9, CYAN)
                d.text("T " + rate(guest["tx"]), gx + 6, gy + 33, 9, VIOLET)
            else:
                label = guest["status"].upper() if guest["current"] else "STALE"
                d.text(label, gx + 6, gy + 27, 10, MUTED if guest["current"] else AMBER)
        if not visible:
            d.text("No guest inventory", x + 25, 407, 12, MUTED)
