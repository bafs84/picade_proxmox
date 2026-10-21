#!/usr/bin/env python3
"""Bounded read-only metrics/cadence capture; outputs stay outside the bundle."""
import argparse
import copy
import json
import os
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vm_arcade.telemetry import Fleet, load_config
from vm_arcade.display import Display
from vm_arcade.controls import Selection
from vm_arcade.topology import graph_host

p = argparse.ArgumentParser()
p.add_argument('--config', required=True)
p.add_argument('--output', required=True)
a = p.parse_args()
os.umask(0o077)
output = Path(a.output)
(output / 'frames').mkdir(parents=True, exist_ok=True)
config = load_config(a.config)
fleet, display, selection = Fleet(config), Display(False), Selection()
selection.host, selection.view = len(config['nodes']) - 1, 'metrics'
seen, frame, rendered = {}, 0, []
started = time.monotonic()
try:
    while time.monotonic() - started < 24:
        now = time.monotonic()
        snapshots = fleet.read()
        for snapshot in snapshots:
            for key in ('status', 'qemu', 'lxc', 'cores', 'traffic'):
                at = snapshot['sections'].get(key, {}).get('at')
                rows = seen.setdefault(snapshot['name'] + '/' + key, [])
                if at is not None and (not rows or at != rows[-1]):
                    rows.append(at)
            h = graph_host(snapshot, now, config['stale_seconds'])
            for direction in ('rx', 'tx'):
                active = [g[direction] for g in h['guests'] if g['status'] == 'running']
                if active and all(v is not None for v in active):
                    assert abs(h['uplink'][direction] - sum(active)) < .001
        before = time.monotonic()
        display.draw(snapshots, selection, stale_seconds=config['stale_seconds'])
        rendered.append(time.monotonic() - before)
        if now - started >= 16:
            display.save(output / 'frames' / ('%03d.bmp' % frame))
            frame += 1
        time.sleep(max(0, .1 - (time.monotonic() - now)))
    for index, snapshot in enumerate(snapshots):
        selection.host = index
        for detail in (False, True):
            selection.detail = detail
            display.draw(snapshots, selection, stale_seconds=config['stale_seconds'])
            display.save(output / ('%s-%s.bmp' % (snapshot['name'], 'detail' if detail else 'overview')))
    stale = copy.deepcopy(snapshots)
    for snapshot in stale:
        snapshot['errors'] = dict.fromkeys(snapshot['sections'], 'Disconnected')
    display.draw(stale, selection, stale_seconds=config['stale_seconds'])
    display.save(output / 'stale.bmp')
    report = {
        'nodes': [{'name': s['name'], 'sections': sorted(s['sections']), 'errors': s['errors'],
                   'core_count': len(s['sections'].get('cores', {}).get('data', {})) - 1}
                  for s in snapshots],
        'cadence': {key: {'samples': len(rows), 'median_seconds': round(statistics.median(
            [b-a for a,b in zip(rows,rows[1:])]), 4) if len(rows)>1 else None} for key,rows in seen.items()},
        'render_median_ms': round(statistics.median(rendered)*1000, 2),
        'frames': frame}
    (output / 'verification.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))
finally:
    fleet.close()
    display.close()
