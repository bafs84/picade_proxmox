"""Native 800x600 timing observatory. Motion represents observed telemetry."""
import math
import time

from .display import BG, CYAN, VIOLET, AMBER, INK, MUTED, LINE, TRACK, sdl
from .gps import current, number


def fmt(value, digits=2, suffix=''):
    n = number(value)
    return '--' if n is None else ('{:.%df}' % digits).format(n) + suffix


def micro(value):
    n = number(value)
    return '--' if n is None else fmt(n * 1e6, 2) + ' us'


def satellite_id(sat):
    return 'P' + fmt(sat['PRN'], 0) if 'PRN' in sat else fmt(sat.get('gnssid'), 0) + '/' + fmt(sat.get('svid'), 0)


def position(sat, x, y, radius):
    az, el = number(sat.get('az')), number(sat.get('el'))
    if az is None or el is None or not 0 <= el <= 90 or not 0 <= az <= 360:
        return None
    angle, r = math.radians(az), radius * (1 - el / 90)
    return x + math.sin(angle) * r, y - math.cos(angle) * r


def circle(d, x, y, radius, color):
    previous = (x + radius, y)
    for i in range(1, 49):
        angle = 2 * math.pi * i / 48
        point = (x + radius * math.cos(angle), y + radius * math.sin(angle))
        d.line(previous[0], previous[1], point[0], point[1], color)
        previous = point


class GPSView:
    def __init__(self):
        self.sequence = None
        self.pulse_at = -100
        self.signals = {}
        self.painted_at = None

    def draw(self, d, snapshot, now=None):
        now = time.monotonic() if now is None else now
        live = current(snapshot, now)
        data = snapshot.get('data', {}) if snapshot else {}
        gps, ntp = data.get('gps', {}), data.get('ntp', {})
        t = ntp.get('tracking', {}) if live and ntp.get('fresh') else {}
        tpv = gps.get('tpv', {}) if live and gps.get('tpv_fresh') else {}
        sky = gps.get('sky', {}) if live and gps.get('sky_fresh') else {}
        sats = sky.get('satellites', [])
        pps = data.get('pps', {}) if live else {}
        system = data.get('system', {}) if live else {}
        source = next((s for s in ntp.get('sources', []) if s.get('state') == '*'), {}) if t else {}
        state = data.get('state', 'unavailable') if live else 'unavailable'
        labels = {'gps_locked': 'GPS / PPS LOCKED', 'holdover': 'HOLDOVER / REFERENCE LOST',
                  'network_sync': 'NETWORK TIME / FALLBACK', 'unsynchronised': 'UNSYNCHRONISED',
                  'unavailable': 'GPS TELEMETRY UNAVAILABLE'}
        color = CYAN if state == 'gps_locked' else AMBER
        d.color(BG)
        sdl.SDL_RenderClear(d.renderer)
        d.text('TIME/ARCADE', 12, 8, 22)
        demo = snapshot and snapshot.get('demo')
        d.text('DEMO / SIMULATED' if demo else 'GPS + PPS / READ ONLY', 12, 35, 10, AMBER if demo else MUTED)
        wall = number(data.get('generated_at'))
        utc = time.strftime('%H:%M:%S', time.gmtime(wall + now - snapshot['at'])) if live and wall is not None else '--:--:--'
        d.text(utc, 0, 6, 28, right=788)
        d.text('PI UTC / DISPLAY ESTIMATE', 0, 37, 9, MUTED, right=788)
        d.line(12, 53, 788, 53)
        d.text(labels.get(state, 'UNKNOWN'), 12, 66, 13, color)
        mode = number(tpv.get('mode'))
        fix = '3D' if mode == 3 else '2D' if mode == 2 else 'NONE' if mode == 1 else '--'
        d.text('FIX ' + fix + '  /  STRATUM ' + fmt(t.get('stratum'), 0), 0, 67, 11, MUTED, right=788)
        values = [('REFERENCE', str(t.get('reference', '--')), t.get('leap', 'Waiting for source')),
                  ('LAST CLOCK OFFSET', micro(t.get('last_offset_s')), 'RMS ' + micro(t.get('rms_offset_s'))),
                  ('SOURCE JITTER', micro(source.get('stats', {}).get('jitter_s')), 'Sample standard deviation'),
                  ('SATELLITES / USED', '{}/{}'.format(sum(s.get('used') is True for s in sats), len(sats)) if sky else '--/--', 'Used / visible')]
        for i, (title, value, note) in enumerate(values):
            x = 12 + i * 197
            d.panel((x, 94, 185, 78), title)
            d.text(value, x + 10, 119, 21, CYAN if i == 0 else INK, width=165)
            d.text(note, x + 10, 151, 9, MUTED, width=165)
        self.sky(d, sats, sky)
        self.strength(d, sats, now)
        self.stability(d, data, source, t, now, snapshot, live)
        d.panel((12, 431, 448, 137), 'TIME SOURCES', 'REACH / AGE / OFFSET / JITTER')
        for i, s in enumerate((ntp.get('sources', []) if t else [])[:6]):
            y = 457 + i * 17
            col = CYAN if s.get('state') == '*' else MUTED
            d.text(str(s.get('state', '?')) + ' ' + str(s.get('name', '?')), 22, y, 10, col, width=137)
            d.text(str(s.get('reach', '--')), 163, y, 10, col)
            d.text(fmt(s.get('last_rx_s'), 0) + 's', 196, y, 10, col, width=42)
            d.text(micro(s.get('adjusted_offset_s')), 242, y, 10, col, width=104)
            d.text(micro(s.get('stats', {}).get('jitter_s')), 0, y, 10, col, right=450)
        d.panel((470, 431, 318, 137), 'RECEIVER / SYSTEM')
        d.pair('Lat / Lon', fmt(tpv.get('lat'), 5) + ' / ' + fmt(tpv.get('lon'), 5), 480, 457, 298, size=10)
        d.pair('Altitude / HDOP', fmt(tpv.get('altMSL'), 1, 'm') + ' / ' + fmt(sky.get('hdop')), 480, 478, 298, size=10)
        d.pair('Temp / 1m load', fmt(system.get('temperature_c'), 1, 'C') + ' / ' + fmt((system.get('load') or [None])[0]), 480, 499, 298, size=10)
        bits = system.get('power_bits')
        power = 'UNKNOWN' if bits is None else 'UNDERVOLTAGE' if system.get('undervoltage_now') else 'THROTTLED' if system.get('throttled_now') else 'NORMAL'
        d.pair('Power now', power, 480, 520, 298, color=AMBER if power != 'NORMAL' else CYAN, size=10)
        d.text('Boot history: ' + ('unknown' if bits is None else 'undervoltage recorded' if system.get('undervoltage_since_boot') else 'no undervoltage'), 480, 544, 9, MUTED, width=298)
        d.line(12, 577, 788, 577)
        if pps.get('fresh') and pps.get('sequence') != self.sequence:
            if self.sequence is not None:
                self.pulse_at = now
            self.sequence = pps.get('sequence')
        age = now - self.pulse_at
        if pps.get('fresh') and 0 <= age < .7:
            circle(d, 19, 589, 3 + age * 7, CYAN)
        else:
            circle(d, 19, 589, 3, LINE)
        d.text('PPS ' + ('#' + str(pps.get('sequence')) if pps.get('fresh') else 'STALE') + ' / RECEIVED EVENTS', 34, 583, 10, MUTED)
        d.text('1Hz API / LOCAL TIMING DIAGNOSTICS', 0, 583, 10, MUTED, right=788)
        sdl.SDL_RenderPresent(d.renderer)

    def sky(self, d, sats, sky):
        d.panel((12, 182, 274, 239), 'SKY / AZIMUTH + ELEVATION', 'N UP')
        x, y, r = 149, 302, 87
        for f in (1, 2 / 3, 1 / 3):
            circle(d, x, y, r * f, LINE)
        d.line(x-r, y, x+r, y)
        d.line(x, y-r, x, y+r)
        for label, dx, dy in (('N', -3, -102), ('S', -3, 89), ('W', -102, -5), ('E', 95, -5)):
            d.text(label, x+dx, y+dy, 10, MUTED)
        for sat in sats:
            point = position(sat, x, y, r)
            if point:
                sx, sy = point
                col = CYAN if sat.get('used') else MUTED
                circle(d, sx, sy, 4, col)
                if sat.get('used'):
                    d.rect((sx-1, sy-1, 3, 3), col)
                d.text(satellite_id(sat), sx+6, sy-5, 9, col)
        if not sky:
            d.text('NO FRESH GPS SKY', 78, 298, 11, AMBER)
        d.text('Used: cyan / horizon: rim / centre: 90deg', 22, 405, 9, MUTED, width=254)

    def strength(self, d, sats, now):
        d.panel((296, 182, 242, 239), 'SIGNAL STRENGTH', 'dB-Hz')
        for value in (0, 20, 40, 60):
            y = 366 - value * 2.2
            d.line(320, y, 528, y)
            d.text(str(value), 302, y-4, 9, MUTED)
        displayed = sorted(sats, key=lambda s: (not s.get('used'), -(number(s.get('ss')) or 0), satellite_id(s)))[:14]
        width = 207 / max(1, len(displayed))
        elapsed = max(0, min(1, now - self.painted_at)) if self.painted_at is not None else 1
        self.painted_at = now
        valid_keys = set()
        for i, sat in enumerate(displayed):
            key = satellite_id(sat) + '/' + str(sat.get('sigid', ''))
            valid_keys.add(key)
            target = number(sat.get('ss'))
            value = self.signals.get(key, target)
            value = None if target is None else target if value is None else value + (target-value) * (1-math.exp(-elapsed*8))
            self.signals[key] = value
            x, col = 322 + i * width, CYAN if sat.get('used') else MUTED
            if value is not None:
                height = min(60, max(0, value)) * 2.2
                d.rect((x, 366-height, width-4, height), col)
            d.text(fmt(target, 0), x, 217, 9, col)
            d.text(fmt(sat.get('PRN', sat.get('svid')), 0), x, 374 + i%2*10, 9, MUTED, width=width+3)
        self.signals = {k: v for k, v in self.signals.items() if k in valid_keys}
        d.text(('Strongest {} / {} visible'.format(len(displayed), len(sats))) if sats else 'NO FRESH REPORT', 306, 405, 9, MUTED)

    def stability(self, d, data, source, t, now, snapshot, live):
        d.panel((548, 182, 240, 239), 'CLOCK STABILITY', '10 MIN')
        wall = number(data.get('generated_at'))
        wall = wall + max(0, now-snapshot.get('at', now)) if wall is not None and snapshot else time.time()
        points = [p for p in data.get('history', []) if number(p.get('time')) is not None and wall-600 <= p['time'] <= wall+1]
        scale = max([1e-6] + [abs(number(p.get(k)) or 0) for p in points for k in ('offset_s', 'jitter_s')]) * 1.15
        d.text('+/- ' + micro(scale), 558, 211, 10, MUTED)
        d.line(558, 275, 778, 275)
        for key, col in (('offset_s', CYAN), ('jitter_s', VIOLET)):
            previous = None
            for point in points:
                v = number(point.get(key))
                if v is None:
                    previous = None
                    continue
                x, y = 558 + (point['time']-wall+600)/600*220, 275-v/scale*43
                if previous and point['time']-previous[2] <= max(30,(number(t.get('update_interval_s')) or 8)*2.5) and point.get('reference') == previous[3]:
                    d.line(previous[0], previous[1], x, y, col)
                d.rect((x-1, y-1, 2, 2), col)
                previous = x, y, point['time'], point.get('reference')
        d.text('OFFSET', 558, 325, 9, CYAN)
        d.text('JITTER', 643, 325, 9, VIOLET)
        d.pair('Interval', fmt(t.get('update_interval_s'), 0, 's'), 558, 347, 220, size=10)
        d.pair('Dispersion', micro(t.get('root_dispersion_s')), 558, 367, 220, size=10)
        d.pair('Freq / ppm', fmt(t.get('frequency_ppm'), 3), 558, 387, 220, size=10)
