"""Small SDL2 renderer, compatible with RetroPie's existing PySDL2 0.9.3."""
from collections import OrderedDict
import ctypes
from pathlib import Path
import time

import sdl2 as sdl
from sdl2 import sdlttf as ttf

from .telemetry import numeric, ratio, section_current, section_data

BG = (8, 17, 20)
PANEL = (12, 25, 30)
INK = (212, 232, 235)
MUTED = (138, 166, 174)
LINE = (37, 65, 73)
ACTIVE = (22, 46, 53)
CYAN = (104, 216, 229)
VIOLET = (182, 160, 211)
AMBER = (226, 186, 114)
TRACK = (27, 48, 55)
GIB = 1024 ** 3
MIB = 1024 ** 2


def fmt(value, decimals=1, suffix=""):
    value = numeric(value)
    return "--" if value is None else ("{:.%df}" % decimals).format(value) + suffix


def gib(value):
    value = numeric(value)
    return "--" if value is None else fmt(value / GIB)


def duration(value):
    value = numeric(value)
    if value is None:
        return "--"
    minutes = int(value) // 60
    return "{}d {:02d}h {:02d}m".format(minutes // 1440, minutes // 60 % 24, minutes % 60)


def guests_for(snapshot):
    return sorted(section_data(snapshot, "qemu", []) + section_data(snapshot, "lxc", []),
                  key=lambda g: (g["id"], g["kind"]))


def layout():
    return {"cpu": (12, 143, 380, 220), "memory": (402, 143, 188, 220),
            "network": (600, 143, 188, 220), "storage": (12, 373, 250, 197),
            "guests": (272, 373, 516, 197)}


class Display:
    def __init__(self, fullscreen=True, font=None):
        if sdl.SDL_Init(sdl.SDL_INIT_VIDEO | sdl.SDL_INIT_JOYSTICK) != 0 or ttf.TTF_Init() != 0:
            raise RuntimeError("SDL display initialization failed")
        flags = sdl.SDL_WINDOW_FULLSCREEN_DESKTOP if fullscreen else sdl.SDL_WINDOW_SHOWN
        self.window = sdl.SDL_CreateWindow(b"VM Arcade", sdl.SDL_WINDOWPOS_CENTERED,
                                           sdl.SDL_WINDOWPOS_CENTERED, 800, 600, flags)
        if not self.window:
            raise RuntimeError("SDL window unavailable")
        self.renderer = sdl.SDL_CreateRenderer(self.window, -1, sdl.SDL_RENDERER_ACCELERATED)
        if not self.renderer:
            self.renderer = sdl.SDL_CreateRenderer(self.window, -1, sdl.SDL_RENDERER_SOFTWARE)
        if not self.renderer:
            raise RuntimeError("SDL renderer unavailable")
        sdl.SDL_RenderSetLogicalSize(self.renderer, 800, 600)
        sdl.SDL_ShowCursor(sdl.SDL_DISABLE if fullscreen else sdl.SDL_ENABLE)
        candidates = [font] if font else []
        candidates += ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
                       "/System/Library/Fonts/Menlo.ttc", "/Library/Fonts/Courier New.ttf"]
        self.font_path = next((str(p) for p in candidates if p and Path(p).is_file()), None)
        if not self.font_path:
            raise RuntimeError("Monospace font unavailable")
        self.fonts = {}
        self.textures = OrderedDict()
        self.joysticks = {}
        from .metrics import Metrics
        self.metrics = Metrics()
        self.metric_flows = {}
        for index in range(sdl.SDL_NumJoysticks()):
            self.add_joystick(index)

    def add_joystick(self, index):
        joystick = sdl.SDL_JoystickOpen(index)
        if joystick:
            guid = ctypes.create_string_buffer(33)
            sdl.SDL_JoystickGetGUIDString(sdl.SDL_JoystickGetGUID(joystick), guid, 33)
            self.joysticks[sdl.SDL_JoystickInstanceID(joystick)] = (joystick, guid.value.decode())

    def remove_joystick(self, instance):
        value = self.joysticks.pop(instance, None)
        if value:
            sdl.SDL_JoystickClose(value[0])

    def color(self, value):
        sdl.SDL_SetRenderDrawColor(self.renderer, *value, 255)

    def rect(self, box, color, outline=False):
        self.color(color)
        rect = sdl.SDL_Rect(*map(int, box))
        (sdl.SDL_RenderDrawRect if outline else sdl.SDL_RenderFillRect)(self.renderer, ctypes.byref(rect))

    def line(self, x1, y1, x2, y2, color=LINE):
        self.color(color)
        sdl.SDL_RenderDrawLine(self.renderer, int(x1), int(y1), int(x2), int(y2))

    def text(self, text, x, y, size=12, color=INK, right=None, width=None):
        # Bound names and cache size; changing telemetry cannot grow GPU memory indefinitely.
        text = "".join(c for c in str(text) if c.isprintable())[:200]
        if width:
            limit = max(1, int(width / (size * .61)))
            text = text if len(text) <= limit else text[:max(0, limit - 2)] + ".."
        key = (text, size, color)
        if key not in self.textures:
            if size not in self.fonts:
                self.fonts[size] = ttf.TTF_OpenFont(self.font_path.encode(), size)
                if not self.fonts[size]:
                    raise RuntimeError("Font load failed")
            surface = ttf.TTF_RenderUTF8_Blended(self.fonts[size], text.encode(), sdl.SDL_Color(*color, 255))
            if not surface:
                return
            texture = sdl.SDL_CreateTextureFromSurface(self.renderer, surface)
            self.textures[key] = (texture, surface.contents.w, surface.contents.h)
            sdl.SDL_FreeSurface(surface)
            while len(self.textures) > 512:
                _, old = self.textures.popitem(last=False)
                sdl.SDL_DestroyTexture(old[0])
        texture, w, h = self.textures[key]
        self.textures.move_to_end(key)
        target = sdl.SDL_Rect(int(right - w if right is not None else x), int(y), w, h)
        sdl.SDL_RenderCopy(self.renderer, texture, None, ctypes.byref(target))

    def panel(self, box, heading, note=""):
        x, y, w, h = box
        self.rect(box, PANEL)
        self.rect(box, LINE, True)
        self.line(x + 10, y, x + 35, y, MUTED)
        self.text(heading, x + 10, y + 9, 11, MUTED)
        self.text(note, 0, y + 9, 11, MUTED, right=x + w - 10)

    def meter(self, x, y, width, value, total, color=CYAN, height=6):
        self.rect((x, y, width, height), TRACK)
        value, total = numeric(value), numeric(total)
        if value is not None and total:
            self.rect((x, y, width * min(1, value / total), height), color)

    def pair(self, label, value, x, y, width, color=INK, size=11):
        self.text(label, x, y, size, MUTED)
        self.text(value, 0, y, size, color, right=x + width)

    def chart(self, rows, fields, x, y, width, height, maximum, percent=False):
        left, top, plot_width, plot_height = x + 26, y + 4, width - 32, height - 23
        for fraction in (0, .5, 1):
            level = top + fraction * plot_height
            self.line(left, level, left + plot_width, level)
        self.text(fmt(maximum, 0), x, y, 11, MUTED)
        self.text("0", x + 12, top + plot_height - 8, 11, MUTED)
        self.text("-60m", left, y + height - 15, 11, MUTED)
        self.text("now", 0, y + height - 15, 11, MUTED, right=x + width - 4)
        now = time.time()
        for n, field in enumerate(fields):
            previous = None
            for row in rows:
                timestamp, value = numeric(row.get("time")), numeric(row.get(field))
                if timestamp is None or value is None or not now - 3660 <= timestamp <= now + 60:
                    previous = None
                    continue
                value = value * 100 if percent else value / MIB
                point = (left + max(0, min(1, (timestamp - now + 3600) / 3600)) * plot_width,
                         top + (1 - min(1, value / max(1, maximum))) * plot_height)
                if previous and timestamp - previous[2] <= 150:
                    if n == 0:
                        self.line(previous[0], previous[1], point[0], point[1], CYAN)
                    else:
                        # Dashes distinguish transmit from receive even without colour.
                        for step in range(0, 10, 4):
                            a, b = step / 10, min(1, (step + 2) / 10)
                            self.line(previous[0] + (point[0] - previous[0]) * a,
                                      previous[1] + (point[1] - previous[1]) * a,
                                      previous[0] + (point[0] - previous[0]) * b,
                                      previous[1] + (point[1] - previous[1]) * b, VIOLET)
                previous = (point[0], point[1], timestamp)
        if not rows:
            self.text("No history", left + 4, top + plot_height / 2 - 6, 11, MUTED)

    def draw(self, snapshots, selection, demo=False, stale_seconds=20, now=None):
        self.color(BG)
        sdl.SDL_RenderClear(self.renderer)
        now = time.monotonic() if now is None else now
        from .metrics import cluster, host_cpu
        from .network_view import rate
        hosts = self.metrics.observe(snapshots, now, stale_seconds)
        totals = cluster(snapshots, hosts, now, stale_seconds)
        snapshot = snapshots[selection.host]
        self.metric_host = hosts[selection.host]
        host = section_data(snapshot, "status", {})
        current = section_current(snapshot, "status", now, stale_seconds)
        self.text("VM/ARCADE", 12, 8, 20)
        self.text("DEMO / SIMULATED" if demo else "CLUSTER / READ ONLY", 12, 32, 10, AMBER if demo else MUTED)
        self.text("ALL HOSTS CPU", 250, 9, 10, MUTED)
        self.text(fmt(totals['cpu']) + '%', 250, 25, 18)
        self.meter(350, 34, 110, self.metrics.smooth('cluster/cpu', totals['cpu'], now), 100)
        self.text("CLUSTER RAM", 490, 9, 10, MUTED)
        self.text(gib(totals['mem']) + '/' + gib(totals['maxmem']) + ' GiB', 490, 25, 16)
        self.text('{}/{} UP'.format(totals['online'], totals['hosts']), 0, 12, 11,
                  CYAN if totals['online'] == totals['hosts'] else AMBER, right=788)
        self.line(12, 51, 788, 51)
        host_width = 776 / max(1, len(snapshots))
        for i, node in enumerate(snapshots):
            x = 12 + i * host_width
            data = section_data(node, "status", {})
            online = section_current(node, "status", now, stale_seconds)
            self.rect((x, 61, host_width - 8, 50), ACTIVE if i == selection.host else BG)
            self.rect((x, 61, host_width - 8, 50), LINE, True)
            if i == selection.host:
                self.rect((x, 61, 3, 50), CYAN)
            self.text(node["name"], x + 10, 67, 15, width=host_width - 95)
            self.text("ONLINE" if online else "UNKNOWN", 0, 69, 11, MUTED if online else AMBER, right=x + host_width - 18)
            memory = data.get("memory", {})
            self.text("CPU {}%  RAM {}/{}G".format(fmt(host_cpu(node, now, stale_seconds)[0], 0), gib(memory.get("used")), gib(memory.get("total"))),
                      x + 10, 91, 11, width=host_width - 24)
        self.text(snapshot["name"] + " / HOST", 12, 122, 11, MUTED)
        sampled = snapshot.get("sections", {}).get("status", {}).get("at")
        age = max(0, now - sampled) if sampled is not None else None
        state_note = "SAMPLE {}s AGO".format(int(age)) if current else "UNKNOWN / " + snapshot.get("errors", {}).get("status", "STALE")
        self.text(state_note, 0, 122, 11, MUTED if current else AMBER, right=788, width=500)
        self.host_panels(snapshot, host, now, stale_seconds)
        guests = guests_for(snapshot)
        selected = selection.resolve(guests)
        self.storage_panel(snapshot, host, selection, now, stale_seconds)
        self.guest_panel(snapshot, guests, selected, selection.detail, now, stale_seconds)
        self.line(12, 575, 788, 575)
        self.text('ALL VMs  RX ' + rate(totals['rx']), 12, 582, 11, CYAN)
        self.text('TX ' + rate(totals['tx']), 217, 582, 11, VIOLET)
        self.text('RUNNING {}/{}'.format(fmt(totals['running'], 0), fmt(totals['guests'], 0)), 396, 582, 11, MUTED)
        self.text('RAM AVAIL ' + gib(totals['available']) + 'G', 0, 582, 11, MUTED, right=788)
        sdl.SDL_RenderPresent(self.renderer)

    def host_panels(self, snapshot, host, now, stale_seconds):
        from .metrics import host_cpu, fraction, trace
        from .network_view import rate
        boxes, name = layout(), snapshot['name']
        cpu = host.get('cpuinfo', {})
        current = section_current(snapshot, 'status', now, stale_seconds)
        value, sampled = host_cpu(snapshot, now, stale_seconds)
        self.panel(boxes['cpu'], 'CPU / HOST', '{} THREADS'.format(fmt(cpu.get('cpus'), 0)))
        self.text(fmt(value, 1) + '%', 22, 171, 28)
        self.text('I/O wait {}%'.format(fmt(ratio(host.get('wait')) if current else None)), 165, 180, 11, MUTED)
        flash = max(0, 1 - (now - sampled) / .35) if value is not None else 0
        color = tuple(int(a + (b - a) * flash) for a, b in zip(TRACK, CYAN))
        self.rect((368, 183, 6, 6), color)
        self.text('sample', 319, 182, 10, MUTED)
        cores = section_data(snapshot, 'cores', {})
        fresh = section_current(snapshot, 'cores', now, 3)
        names = sorted((k for k in cores if k != 'cpu'), key=lambda k: int(k[3:]))
        self.text(('CPU THREADS / 2 Hz' if fresh else 'CPU THREADS / STALE') if names else 'CPU THREADS / NO SOURCE', 22, 209, 10, MUTED if fresh else AMBER)
        if names:
            count = min(32, len(names))
            columns = 3 if count <= 9 else 4 if count <= 16 else 8
            rows = (count + columns - 1) // columns
            cell_width, cell_height = 358 / columns, 62 / rows
            for i, core in enumerate(names[:count]):
                x, y = 22 + (i % columns) * cell_width, 226 + (i // columns) * cell_height
                load = cores[core] if fresh else None
                self.text(core[3:], x, y, 10, MUTED)
                self.text(fmt(load, 0) + '%', 0, y, 10, right=x + cell_width - 8)
                self.meter(x, y + cell_height - 6, cell_width - 10,
                           self.metrics.smooth(name + '/' + core, load, now), 100,
                           AMBER if load is not None and load >= 85 else CYAN, height=3)
        else:
            self.text('Per-thread readings unavailable', 22, 242, 11, MUTED, width=358)
        trace(self, self.metrics.history.read(name + '/cpu', now), 22, 293, 320, 16, now, 100)
        self.text('60s', 352, 296, 9, MUTED)
        self.line(22, 313, 380, 313)
        loads = host.get('loadavg', []) if current else []
        for i, label in enumerate(('1m', '5m', '15m')):
            self.text('LOAD ' + label, 22 + i * 120, 320, 11, MUTED)
            self.text(fmt(loads[i] if len(loads) > i else None, 2), 22 + i * 120, 337, 16)
        memory, swap = host.get('memory', {}) if current else {}, host.get('swap', {}) if current else {}
        self.panel(boxes['memory'], 'MEMORY')
        self.text(gib(memory.get('used')) + ' GiB', 412, 181, 24)
        self.text('/ {} GiB total'.format(gib(memory.get('total'))), 412, 211, 11, MUTED)
        used = fraction(memory.get('used'), memory.get('total'))
        self.meter(412, 234, 168, self.metrics.smooth(name + '/ram', used, now), 100)
        peak = self.metrics.peak(name + '/ram', now)
        self.line(412 + 168 * min(1, peak / 100), 232, 412 + 168 * min(1, peak / 100), 242, AMBER)
        self.pair('Avail', gib(memory.get('available')) + ' GiB', 412, 249, 168)
        trace(self, self.metrics.history.read(name + '/ram', now), 412, 273, 168, 32, now, 100,
              max_gap=max(3, stale_seconds))
        self.text('60s / peak ' + fmt(peak, 0) + '%', 412, 309, 10, MUTED)
        self.pair('Swap', '{}/{}G'.format(gib(swap.get('used')), gib(swap.get('total'))), 412, 331, 168)
        self.meter(412, 348, 168, swap.get('used'), swap.get('total'), VIOLET)
        self.panel(boxes['network'], 'VM TRAFFIC', 'SUM')
        traffic = self.metric_host['uplink']
        from .metrics import TrafficParticles
        flow = self.metric_flows.setdefault(name, TrafficParticles())
        lanes = flow.positions(traffic['rx'], traffic['tx'], now)
        for i, direction in enumerate(('rx', 'tx')):
            color, y = (CYAN if i == 0 else VIOLET), 178 + i * 46
            value = traffic[direction]
            self.text(direction.upper(), 610, y, 11, color)
            self.text(rate(value), 0, y - 1, 17, right=778)
            self.line(610, y + 27, 778, y + 27)
            # Rate changes affect new emissions. Dots already in flight finish
            # at the opposite endpoint; the bars show the current sample.
            self.meter(610, y + 31, 168, value, flow.scale, color, height=2)
            for position in lanes[i]:
                tail = max(0, min(1, position + (.025 if i else -.025)))
                self.line(610 + tail * 165, y + 27, 610 + position * 165, y + 27, color)
                self.rect((610 + position * 165, y + 25, 3, 4), color)
        trace(self, self.metrics.history.read(name + '/net', now), 610, 277, 168, 45, now,
              directions=(1, 2))
        self.text('60s / RX + TX', 610, 330, 10, MUTED)
        self.text(('FLOW LIMIT / ' if flow.limited else '2Hz / ') + '4KiB per dot' if self.metric_host['traffic_current'] else 'COUNTERS STALE', 610, 346, 9,
                  MUTED if self.metric_host['traffic_current'] else AMBER)

    def storage_panel(self, snapshot, host, selection, now, stale_seconds):
        self.panel(layout()["storage"], "STORAGE / HOST")
        storages = section_data(snapshot, "storage", [])
        if storages:
            index = selection.storage % len(storages)
            storage = storages[index]
            valid = section_current(snapshot, "storage", now, stale_seconds)
            self.text(storage.get("storage", "--"), 22, 405, 12, width=180)
            self.text("{}/{}".format(index + 1, len(storages)), 0, 405, 11, MUTED, right=250)
            self.text("{} / {} GiB".format(gib(storage.get("used")), gib(storage.get("total"))), 22, 430, 16)
            self.meter(22, 456, 228, storage.get("used"), storage.get("total"))
            self.pair("Available", gib(storage.get("avail")) + " GiB", 22, 473, 228)
            if not valid or not storage.get("active"):
                self.text("STALE" if not valid else "INACTIVE", 170, 389, 11, AMBER)
        else:
            self.text("Storage unavailable", 22, 425, 12, MUTED)
        self.line(22, 498, 250, 498)
        self.pair("Uptime", duration(host.get("uptime")), 22, 513, 228)
        self.pair("Hypervisor", "Proxmox VE", 22, 541, 228)

    def guest_panel(self, snapshot, guests, selected, detail, now, stale_seconds):
        self.panel(layout()["guests"], "VM / DETAIL" if detail else "VIRTUAL MACHINES", "{} GUESTS".format(len(guests)))
        if not guests:
            valid = all(section_current(snapshot, s, now, stale_seconds) for s in ("qemu", "lxc"))
            self.text("No VMs or containers" if valid else "Guest inventory unavailable", 284, 438, 16, MUTED)
            return
        guest = guests[selected]
        valid = section_current(snapshot, guest["kind"], now, stale_seconds)
        status = guest["status"] if valid else "unknown"
        if detail:
            self.text("{} / {}".format(guest["id"], guest["name"]), 284, 403, 16, width=380)
            self.text(status.upper(), 0, 405, 11, MUTED if valid else AMBER, right=776)
            self.pair("CPU", "{}% / {} vCPU".format(fmt(guest["cpu"], 0), fmt(guest["cpus"], 0)), 284, 433, 230)
            self.pair("RAM", "{}/{}G".format(gib(guest["mem"]), gib(guest["maxmem"])), 540, 433, 236)
            from .metrics import fraction
            self.meter(284, 450, 230, self.metrics.smooth(snapshot['name'] + '/' + guest['key'] + '/cpu', guest['cpu'] if valid else None, now), 100)
            self.meter(540, 450, 236, self.metrics.smooth(snapshot['name'] + '/' + guest['key'] + '/ram', fraction(guest['mem'], guest['maxmem']) if valid else None, now), 100, VIOLET)
            self.pair("Uptime", duration(guest["uptime"]), 284, 468, 230)
            self.pair("Root disk cap.", gib(guest["maxdisk"]) + " GiB", 540, 468, 236)
        else:
            for label, x in (("VM / NAME", 284), ("STATE", 502), ("vCPU", 570), ("CPU", 628), ("RAM GiB", 699)):
                self.text(label, x, 403, 11, MUTED)
            # Scroll the roster around selection without changing the rest of the screen.
            start = (selected // 2) * 2
            for row, index in enumerate(range(start, min(start + 2, len(guests)))):
                g = guests[index]
                y = 425 + row * 31
                if index == selected:
                    self.rect((282, y - 3, 496, 30), ACTIVE)
                current = section_current(snapshot, g["kind"], now, stale_seconds)
                self.text("{} {}".format(g["id"], g["name"]), 286, y, 12, width=208)
                self.text(g["status"] if current else "unknown", 502, y + 1, 11, MUTED if current else AMBER)
                self.text(fmt(g["cpus"], 0), 582, y, 12)
                self.text(fmt(g['cpu'] if current else None, 0) + '%', 628, y, 12)
                from .metrics import fraction
                self.meter(628, y + 19, 45, self.metrics.smooth(snapshot['name'] + '/' + g['key'] + '/cpu', g['cpu'] if current else None, now), 100)
                self.meter(699, y + 19, 75, self.metrics.smooth(snapshot['name'] + '/' + g['key'] + '/ram', fraction(g['mem'], g['maxmem']) if current else None, now), 100, VIOLET)
                self.text("{}/{}".format(gib(g["mem"]), gib(g["maxmem"])), 0, y, 11, right=774)
            self.text("{}/{}".format(selected + 1, len(guests)), 746, 484, 11, MUTED)
        from .metrics import trace, fraction
        from .network_view import rate
        key = snapshot['name'] + '/' + guest['key']
        self.text('SELECTED {} / 60s HISTORY'.format(guest['id']), 284, 489, 10, MUTED)
        self.line(284, 505, 776, 505)
        for i, label in enumerate(('NET RX', 'NET TX', 'DISK READ', 'DISK WRITE')):
            series = self.metrics.history.read(key + ('/net' if i < 2 else '/disk'), now)
            direction = i % 2 + 1
            max_age = 3 if i < 2 else stale_seconds
            value = series[-1][direction] if series and now - series[-1][0] <= max_age else None
            x, color = 284 + i * 124, CYAN if i % 2 == 0 else VIOLET
            self.text(label, x, 510, 10, MUTED)
            self.text(rate(value), x, 525, 13, color)
            trace(self, series, x, 546, 112, 16, now, directions=(direction,), max_gap=max_age)
        if not valid:
            self.text("CACHED", 680, 484, 11, AMBER)

    def save(self, path):
        # ReadPixels uses output pixels, not the 800x600 logical canvas. A
        # larger fullscreen framebuffer would overflow a logical-sized buffer.
        width, height = ctypes.c_int(), ctypes.c_int()
        if sdl.SDL_GetRendererOutputSize(self.renderer, ctypes.byref(width), ctypes.byref(height)) != 0 or min(width.value, height.value) <= 0:
            raise RuntimeError("Screenshot output size unavailable")
        # SDL_CreateRGBSurfaceWithFormat is absent from the Pi's PySDL2 0.9.3.
        surface = sdl.SDL_CreateRGBSurface(0, width.value, height.value, 32,
                                           0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
        if not surface:
            raise RuntimeError("Screenshot surface unavailable")
        try:
            if sdl.SDL_RenderReadPixels(self.renderer, None, sdl.SDL_PIXELFORMAT_ARGB8888,
                                        surface.contents.pixels, surface.contents.pitch) != 0:
                raise RuntimeError("Screenshot capture failed")
            sdl.SDL_SaveBMP(surface, str(path).encode())
        finally:
            sdl.SDL_FreeSurface(surface)

    def close(self):
        for texture, _, _ in self.textures.values():
            sdl.SDL_DestroyTexture(texture)
        for font in self.fonts.values():
            ttf.TTF_CloseFont(font)
        for joystick, _ in self.joysticks.values():
            sdl.SDL_JoystickClose(joystick)
        sdl.SDL_ShowCursor(sdl.SDL_ENABLE)
        sdl.SDL_DestroyRenderer(self.renderer)
        sdl.SDL_DestroyWindow(self.window)
        ttf.TTF_Quit()
        sdl.SDL_Quit()
