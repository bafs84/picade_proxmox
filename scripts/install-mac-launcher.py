#!/usr/bin/env python3
"""Create a local Mac app shortcut; credentials remain outside the bundle."""
from pathlib import Path
import plistlib
import shlex


def main():
    project = Path(__file__).resolve().parents[1]
    app = Path.home() / "Downloads/VM Arcade.app"
    identifier = "local.vm-arcade"
    plist = app / "Contents/Info.plist"
    if app.exists():
        if not plist.is_file() or plistlib.loads(plist.read_bytes()).get("CFBundleIdentifier") != identifier:
            raise SystemExit("An unrelated VM Arcade.app already exists")
    executable = app / "Contents/MacOS/launch"
    executable.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps({
        "CFBundleName": "VM Arcade", "CFBundleDisplayName": "VM Arcade",
        "CFBundleIdentifier": identifier, "CFBundleExecutable": "launch",
        "CFBundlePackageType": "APPL", "CFBundleVersion": "1",
        "CFBundleShortVersionString": "0.1.0", "NSHighResolutionCapable": True,
    }))
    executable.write_text(
        '#!/bin/sh\nset -eu\numask 077\n'
        'mkdir -p "$HOME/.local/state/vm-arcade"\n'
        'exec >"$HOME/.local/state/vm-arcade/mac-launch.log" 2>&1\n'
        'unset SDL_VIDEODRIVER\n'
        'cd ' + shlex.quote(str(project)) + '\n'
        'exec .venv/bin/python -m vm_arcade --windowed '
        '--config "$HOME/.config/vm-arcade/config.json"\n'
    )
    executable.chmod(0o755)
    print(app)


if __name__ == "__main__":
    main()
