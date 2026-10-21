"""Launch from RetroPie Ports or run a bounded local validation."""
import argparse
import ctypes
import json
from pathlib import Path
import signal
import sys
import time

from .controls import Controls, Selection
from .telemetry import Fleet, MonitorError, load_config, section_current, section_data


def parser():
    p = argparse.ArgumentParser(description="Native read-only Proxmox telemetry for RetroPie")
    p.add_argument("--config", default="~/.config/vm-arcade/config.json")
    p.add_argument("--demo", action="store_true", help="Explicit fictional data; never enabled automatically")
    p.add_argument("--windowed", action="store_true")
    p.add_argument("--node", help="Initially selected node name")
    p.add_argument("--font")
    p.add_argument("--view", choices=("network", "metrics", "gps"), default="network")
    p.add_argument("--input-config", default="/opt/retropie/configs/all/emulationstation/es_input.cfg")
    p.add_argument("--check", action="store_true", help="Check configured API reads, without opening SDL")
    p.add_argument("--seconds", type=float, default=0, help="Exit after this duration (zero: continuous)")
    p.add_argument("--screenshot", help="Save a BMP of the last rendered frame before exit")
    return p


def summarize(snapshots, stale):
    now = time.monotonic()
    return [{"name": s["name"], "current": section_current(s, "status", now, stale),
             "sections": sorted(s["sections"]), "errors": s["errors"],
             "qemu": len(section_data(s, "qemu", [])), "lxc": len(section_data(s, "lxc", [])),
             "storage": len(section_data(s, "storage", [])),
             "guest_rates_ready": sum(any(v is not None for v in g["rates"].values())
                                      for kind in ("qemu", "lxc") for g in section_data(s, kind, []))}
            for s in snapshots]


def main(argv=None):
    args = parser().parse_args(argv)
    if args.seconds < 0 or (args.screenshot and args.seconds <= 0):
        parser().error("--screenshot requires a positive --seconds")
    wan = gps = None
    config = {}
    if args.demo:
        from .demo import DemoFleet
        fleet, stale = DemoFleet(), 20
        from .gps import DemoGPS
        gps = DemoGPS()
    else:
        try:
            config = load_config(args.config)
        except MonitorError as exc:
            print("VM Arcade: " + str(exc), file=sys.stderr)
            return 2
        fleet, stale = Fleet(config), config["stale_seconds"]
        if config.get("wan_monitor"):
            from .wan import WanMonitor
            wan = WanMonitor(config["wan_monitor"])
        if config.get("gps_monitor"):
            from .gps import GPSMonitor
            gps = GPSMonitor(config["gps_monitor"])
    if args.view == 'gps' and not gps:
        print('VM Arcade: GPS view requires gps_monitor in the private configuration.', file=sys.stderr)
        fleet.close()
        if wan:
            wan.close()
        return 2
    started = time.monotonic()
    if args.check:
        try:
            time.sleep(args.seconds or 12)
            result = summarize(fleet.read(), stale)
            print(json.dumps(result, indent=2))
            return 0 if all(n["current"] and not n["errors"] and len(n["sections"]) >= (6 if args.demo else 7) for n in result) else 1
        finally:
            fleet.close()
            if wan:
                wan.close()
            if gps:
                gps.close()
    display = None
    try:
        from .display import Display, guests_for, sdl
        display = Display(fullscreen=not args.windowed, font=args.font)
        controls, selection = Controls(args.input_config, exit_key=config.get("exit_key")), Selection(gps_enabled=bool(gps))
        selection.view = args.view
        from .network_view import NetworkView
        network = NetworkView()
        from .gps_view import GPSView
        gps_view = GPSView()
        names = [s["name"] for s in fleet.read()]
        if args.node:
            if args.node not in names:
                raise RuntimeError("Requested node is not configured")
            selection.host = names.index(args.node)
        running, dirty, painted_at = True, True, 0
        snapshots, fetched_at = fleet.read(), 0
        wan_snapshot = gps_snapshot = None
        stop = []
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.append(True))
        event = sdl.SDL_Event()
        while running and not stop:
            if time.monotonic() - fetched_at >= .25:
                snapshots = fleet.read()
                wan_snapshot = fleet.wan() if args.demo else wan.read() if wan else None
                gps_snapshot = gps.read() if gps else None
                fetched_at = time.monotonic()
                network.observe(snapshots, fetched_at, stale)
                display.metrics.observe(snapshots, fetched_at, stale)
            guests = guests_for(snapshots[selection.host])
            while sdl.SDL_PollEvent(ctypes.byref(event)):
                action = None
                if event.type == sdl.SDL_QUIT:
                    running = False
                elif event.type == sdl.SDL_MOUSEBUTTONDOWN and event.button.button == 1 and selection.view == "network":
                    hit = network.hit(event.button.x, event.button.y)
                    if hit:
                        selection.host, selection.guest_key = hit
                        dirty = True
                elif event.type == sdl.SDL_KEYDOWN:
                    key = event.key.keysym.sym
                    action = controls.keys.get(key)
                    if event.key.repeat and action not in ("up", "down"):
                        action = None
                elif event.type == sdl.SDL_JOYDEVICEADDED:
                    display.add_joystick(event.jdevice.which)
                elif event.type == sdl.SDL_JOYDEVICEREMOVED:
                    display.remove_joystick(event.jdevice.which)
                elif event.type in (sdl.SDL_JOYBUTTONDOWN, sdl.SDL_JOYAXISMOTION, sdl.SDL_JOYHATMOTION):
                    if event.type == sdl.SDL_JOYBUTTONDOWN:
                        instance, kind, index, value = event.jbutton.which, "button", event.jbutton.button, 1
                    elif event.type == sdl.SDL_JOYAXISMOTION:
                        instance, kind, index, value = event.jaxis.which, "axis", event.jaxis.axis, event.jaxis.value
                    else:
                        instance, kind, index, value = event.jhat.which, "hat", event.jhat.hat, event.jhat.value
                    joystick = display.joysticks.get(instance)
                    if joystick:
                        action = controls.joystick(joystick[1], kind, index, value)
                if action:
                    running = selection.act(action, snapshots, guests)
                    guests = guests_for(snapshots[selection.host])
                    dirty = True
            now = time.monotonic()
            if dirty or now - painted_at >= (1 / 30):
                if selection.view == "network":
                    network.draw(display, snapshots, selection, args.demo, stale, wan_snapshot)
                elif selection.view == "gps":
                    gps_view.draw(display, gps_snapshot, now)
                else:
                    display.draw(snapshots, selection, args.demo, stale)
                dirty, painted_at = False, now
            if args.seconds and now - started >= args.seconds:
                if args.screenshot:
                    display.save(Path(args.screenshot).expanduser())
                break
            sdl.SDL_Delay(8)
        return 0
    except (ImportError, RuntimeError, OSError):
        print("VM Arcade: SDL2 display unavailable. Check PySDL2, SDL2_ttf, font and video driver.", file=sys.stderr)
        return 2
    finally:
        fleet.close()
        if wan:
            wan.close()
        if gps:
            gps.close()
        if display:
            display.close()


if __name__ == "__main__":
    sys.exit(main())
