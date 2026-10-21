#!/usr/bin/env python3
"""Bounded read-only API + native-render smoke check. Writes private local evidence."""
import argparse
import copy
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vm_arcade.controls import Selection
from vm_arcade.telemetry import Fleet, load_config
from vm_arcade.__main__ import summarize


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--output", required=True)
    args = p.parse_args()
    os.umask(0o077)
    output = Path(args.output).expanduser()
    output.mkdir(parents=True, exist_ok=True)
    config = load_config(args.config)
    fleet = Fleet(config)
    from vm_arcade.wan import WanMonitor, current as wan_current
    wan = WanMonitor(config["wan_monitor"]) if config.get("wan_monitor") else None
    display = None
    try:
        time.sleep(15)
        snapshots = fleet.read()
        report = summarize(snapshots, config["stale_seconds"])
        if not all(s["current"] and len(s["sections"]) >= 7 and not s["errors"] for s in report):
            print(json.dumps(report, indent=2))
            return 1
        from vm_arcade.display import Display, guests_for
        display = Display(fullscreen=False)
        selection = Selection()
        for index, snapshot in enumerate(snapshots):
            selection.host = index
            selection.detail = False
            guests = guests_for(snapshot)
            selection.guest_key = next((g["key"] for g in guests if g["status"] == "running"), None)
            display.draw(snapshots, selection)
            display.save(output / (snapshot["name"] + "-overview.bmp"))
            selection.detail = True
            display.draw(snapshots, selection)
            display.save(output / (snapshot["name"] + "-detail.bmp"))
        from vm_arcade.network_view import NetworkView
        from vm_arcade.topology import graph_host
        network = NetworkView()
        now = time.monotonic()
        graphs = [graph_host(s, now, config["stale_seconds"]) for s in snapshots]
        if not all(h["network_current"] and h["configuration_current"] for h in graphs):
            print("Topology not current")
            return 1
        wan_snapshot = wan.read() if wan else None
        if wan and not wan_current(wan_snapshot, now):
            print("Configured WAN source not current")
            return 1
        # Collect successive real NIC samples so the selected history contains measured points.
        for _ in range(80):
            network.observe(snapshots, time.monotonic(), config["stale_seconds"])
            display.metrics.observe(snapshots, time.monotonic(), config["stale_seconds"])
            time.sleep(.1)
            snapshots = fleet.read()
            wan_snapshot = wan.read() if wan else None
        now = time.monotonic()
        network.draw(display, snapshots, selection, wan=wan_snapshot, now=now)
        display.save(output / "network.bmp")
        network.draw(display, snapshots, selection, wan=wan_snapshot, now=now + .3)
        display.save(output / "network-motion.bmp")
        if not all(h["uplink"]["rx"] is not None and h["uplink"]["tx"] is not None for h in graphs):
            print("Host uplink rates unavailable")
            return 1
        # Capture both views from advancing real samples (not synthetic future timestamps).
        frames, metrics = output / "frames", output / "metrics-frames"
        frames.mkdir(exist_ok=True)
        metrics.mkdir(exist_ok=True)
        selection.detail = False
        for i in range(80):
            sampled = time.monotonic()
            snapshots = fleet.read()
            wan_snapshot = wan.read() if wan else None
            network.draw(display, snapshots, selection, wan=wan_snapshot)
            display.save(frames / ("{:03d}.bmp".format(i)))
            display.draw(snapshots, selection)
            display.save(metrics / ("{:03d}.bmp".format(i)))
            time.sleep(max(0, .1 - (time.monotonic() - sampled)))
        stale = copy.deepcopy(snapshots)
        for snapshot in stale:
            snapshot["errors"] = {key: "Connection unavailable" for key in snapshot["sections"]}
            for section in snapshot["sections"].values():
                section["at"] -= 70
        selection.detail = False
        display.draw(stale, selection)
        display.save(output / "stale.bmp")
        network.draw(display, stale, selection, wan=dict(wan_snapshot, error="Disconnected") if wan_snapshot else None)
        display.save(output / "network-stale.bmp")
        result = {"wan_current": bool(wan and wan_current(wan_snapshot, time.monotonic())),
                  "nic_interfaces": sum(len(section["sections"]["traffic"]["data"]) for section in snapshots),
                  "guest_attachments": sum(len(g["nics"]) for h in graphs for g in h["guests"]),
                  "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "read_only": True, "native_canvas": [800, 600],
                  "nodes": report, "screenshots": len(list(output.glob("*.bmp")))}
        (output / "verification.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
        return 0
    finally:
        fleet.close()
        if wan:
            wan.close()
        if display:
            display.close()


if __name__ == "__main__":
    sys.exit(main())
