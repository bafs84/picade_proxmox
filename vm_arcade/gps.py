"""Optional read-only GPS Observatory client. No Proxmox credentials are sent."""
import copy
import http.client
import json
import math
import threading
import time
from urllib.parse import urlsplit

from .telemetry import MonitorError


def number(value):
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def validate(config):
    if not isinstance(config, dict):
        raise ValueError('Invalid GPS configuration')
    url = urlsplit(config['url'])
    if (url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password
            or url.path not in ('', '/') or url.query or url.fragment):
        raise ValueError('Invalid GPS URL')
    if url.port is not None and not 1 <= url.port <= 65535:
        raise ValueError('Invalid GPS port')
    return config


def decode(payload):
    """Reject malformed nested data before it can reach the rendering loop."""
    if not isinstance(payload, dict) or payload.get('schema') != 1:
        raise ValueError('Unsupported GPS API')
    if any(number(payload.get(k)) is None for k in ('generated_at', 'sample_id', 'collector_age_s')):
        raise ValueError('GPS sample unavailable')
    for k in ('gps', 'ntp', 'pps', 'system'):
        if not isinstance(payload.get(k), dict):
            raise ValueError('Invalid GPS section')
    for section, key in (('gps', 'tpv'), ('gps', 'sky'), ('ntp', 'tracking')):
        if not isinstance(payload[section].get(key, {}), dict):
            raise ValueError('Invalid GPS report')
    containers = [(payload['gps'].get('sky', {}).get('satellites', []), 128),
                  (payload['ntp'].get('sources', []), 64), (payload.get('history', []), 600)]
    for items, limit in containers:
        if not isinstance(items, list) or len(items) > limit or any(not isinstance(i, dict) for i in items):
            raise ValueError('Invalid GPS collection')
    for source in payload['ntp'].get('sources', []):
        if not isinstance(source.get('stats', {}), dict):
            raise ValueError('Invalid source statistics')
    # JSON values intended for arithmetic must remain finite numbers or null.
    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()}
        if isinstance(value, list):
            return [clean(v) for v in value]
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    payload = clean(payload)
    # Normalise numeric optional fields, including malformed strings and booleans.
    dictionaries = [payload['gps'].get('tpv', {}), payload['gps'].get('sky', {}),
                    payload['ntp'].get('tracking', {}), payload['pps'], payload['system']]
    dictionaries += payload['gps'].get('sky', {}).get('satellites', [])
    dictionaries += payload['ntp'].get('sources', []) + payload.get('history', [])
    dictionaries += [s.get('stats', {}) for s in payload['ntp'].get('sources', [])]
    fields = set(('mode lat lon altMSL altHAE hdop vdop pdop tdop gdop PRN gnssid svid sigid az el ss '
                  'stratum reference_time system_offset_s last_offset_s rms_offset_s frequency_ppm '
                  'skew_ppm root_dispersion_s root_delay_s update_interval_s last_rx_s adjusted_offset_s '
                  'measured_offset_s jitter_s temperature_c power_bits offset_s rms_s').split())
    for item in dictionaries:
        for key in fields & item.keys():
            item[key] = number(item[key])
    for point in payload.get('history', []):
        point['time'] = number(point.get('time'))
    for key in ('generated_at', 'sample_id', 'collector_age_s'):
        payload[key] = number(payload[key])
    load = payload['system'].get('load')
    payload['system']['load'] = [number(v) for v in load[:3]] if isinstance(load, list) else []
    return payload


def current(snapshot, now):
    if not snapshot or snapshot.get('error'):
        return False
    age = now - snapshot.get('at', -1000)
    source_age = number(snapshot.get('data', {}).get('collector_age_s'))
    return (source_age is not None and 0 <= age < 4 and source_age + age < 4
            and 0 <= now - snapshot.get('advanced_at', -1000) < 4)


class GPSMonitor:
    def __init__(self, config):
        self.config = validate(config)
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.snapshot = {'error': 'Connecting to GPS'}
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()

    def fetch(self):
        url = urlsplit(self.config['url'])
        factory = http.client.HTTPSConnection if url.scheme == 'https' else http.client.HTTPConnection
        conn = factory(url.hostname, url.port, timeout=2)
        try:
            conn.request('GET', '/api/v1/status', headers={'Accept': 'application/json', 'Cache-Control': 'no-cache'})
            response = conn.getresponse()
            if response.status != 200:
                raise MonitorError('GPS API unavailable')
            raw = response.read(524289)
            if len(raw) > 524288:
                raise MonitorError('GPS response too large')
            return decode(json.loads(raw))
        finally:
            conn.close()

    def _worker(self):
        sample, advanced = None, 0
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                data = self.fetch()
                now = time.monotonic()
                if data['sample_id'] != sample:
                    sample, advanced = data['sample_id'], now
                snapshot = {'at': now, 'advanced_at': advanced, 'data': data}
            except Exception:
                snapshot = {'error': 'GPS unavailable / stale'}
            with self.lock:
                self.snapshot = snapshot
            self.stop.wait(max(.1, 1 - (time.monotonic() - started)))

    def read(self):
        with self.lock:
            return copy.deepcopy(self.snapshot)

    def close(self):
        self.stop.set()


class DemoGPS:
    """Explicit fictional preview only. Never used after a failed live fetch."""
    def read(self):
        now, wall = time.monotonic(), time.time()
        sample = math.floor(wall / 8) * 8
        t = {'reference': 'PPS', 'stratum': 1, 'leap': 'Normal', 'last_offset_s': math.sin(sample*.1)*.8e-6,
             'rms_offset_s': .6e-6, 'root_dispersion_s': 8e-6, 'frequency_ppm': -.12, 'update_interval_s': 8}
        sats = [{'PRN': i+1, 'az': (i*41+25)%360, 'el': 15+(i*19)%70,
                 'ss': 25+15*math.sin(now*.08+i), 'used': i<7} for i in range(10)]
        sources = [{'state': '*', 'name': 'PPS', 'reach': '377', 'last_rx_s': wall-sample,
                    'adjusted_offset_s': -.3e-6, 'stats': {'jitter_s': .7e-6}},
                   {'state': '?', 'name': 'NMEA', 'reach': '377', 'last_rx_s': 3,
                    'adjusted_offset_s': .14, 'stats': {'jitter_s': .001}}]
        data = {'schema': 1, 'sample_id': int(now), 'collector_age_s': 0, 'generated_at': wall,
                'state': 'gps_locked', 'gps': {'tpv_fresh': True, 'sky_fresh': True,
                    'tpv': {'mode': 3, 'altMSL': 100}, 'sky': {'satellites': sats, 'hdop': 1.1}},
                'ntp': {'fresh': True, 'tracking': t, 'sources': sources},
                'pps': {'fresh': True, 'sequence': int(now)},
                'system': {'fresh': True, 'temperature_c': 43, 'load': [.03, .02, .01], 'power_bits': 0},
                'history': [{'time': sample-i*8, 'offset_s': math.sin((sample-i*8)*.1)*.8e-6,
                             'jitter_s': .7e-6, 'reference': 'PPS'} for i in range(74,-1,-1)]}
        return {'at': now, 'advanced_at': now, 'data': data, 'demo': True}

    def close(self):
        pass
