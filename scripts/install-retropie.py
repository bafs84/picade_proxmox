#!/usr/bin/env python3
"""Local, unprivileged installer. Never updates packages or changes networking."""
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import sys
import time


def main():
    if sys.version_info < (3, 7):
        raise SystemExit("Python 3.7 or later is required")
    try:
        import sdl2
        from sdl2 import sdlttf
    except ImportError:
        raise SystemExit("Missing dependency: python3-sdl2 and libsdl2-ttf-2.0-0")
    if not Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf").is_file():
        raise SystemExit("Missing dependency: fonts-dejavu-core")
    home = Path.home()
    ports = home / "RetroPie/roms/ports"
    if not (home / "RetroPie/roms").is_dir():
        raise SystemExit("Run this installer as the RetroPie user")
    source = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted((source / "vm_arcade").glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    release_id = digest.hexdigest()[:12]
    base = home / ".local/share/vm-arcade"
    release = base / "releases" / release_id
    launcher = ports / "VM Arcade.sh"
    menu_launcher = home / "RetroPie/retropiemenu/VM Arcade.sh"
    binary = home / ".local/bin/vm-arcade"
    marker = "# Managed by VM Arcade installer"
    for target in (launcher, menu_launcher, binary):
        if target.exists() and marker not in target.read_text():
            raise SystemExit("Existing unrelated launcher: " + str(target))
    release.parent.mkdir(parents=True, exist_ok=True)
    if not release.exists():
        stage = release.with_name(release.name + ".staging-" + str(os.getpid()))
        (stage / "vm_arcade").mkdir(parents=True)
        for path in (source / "vm_arcade").glob("*.py"):
            shutil.copy2(str(path), str(stage / "vm_arcade" / path.name))
        shutil.copy2(str(source / "config.example.json"), str(stage / "config.example.json"))
        stage.rename(release)
    current = base / "current"
    if current.exists() and not current.is_symlink():
        raise SystemExit("Refusing to replace a non-symlink current directory")
    previous = os.readlink(str(current)) if current.is_symlink() else None
    link = base / ("current.new-" + str(os.getpid()))
    link.symlink_to(release)
    os.replace(str(link), str(current))
    if previous and (base / previous).resolve() != release.resolve():
        (base / "previous-release.txt").write_text(previous + "\n")
    ports.mkdir(parents=True, exist_ok=True)
    menu_launcher.parent.mkdir(parents=True, exist_ok=True)
    binary.parent.mkdir(parents=True, exist_ok=True)
    config = home / ".config/vm-arcade"
    config.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(str(config), 0o700)
    binary.write_text('#!/bin/sh\n' + marker + '\n'
                      'cd "$HOME/.local/share/vm-arcade/current" || exit 1\n'
                      'exec /usr/bin/python3 -m vm_arcade "$@"\n')
    # RetroPie's built-in menu runs .sh entries as the RetroPie user. Keep its
    # home explicit because the menu dispatcher itself is invoked through sudo.
    launch_text = ('#!/bin/sh\n' + marker + '\n'
                   'export HOME=' + shlex.quote(str(home)) + '\n'
                   'exec "$HOME/.local/bin/vm-arcade" --config "$HOME/.config/vm-arcade/config.json" "$@"\n')
    launcher.write_text(launch_text)
    menu_launcher.write_text(launch_text)
    binary.chmod(0o755)
    launcher.chmod(0o755)
    menu_launcher.chmod(0o755)
    print("Installed release " + release_id + " into the RetroPie menu and Ports")
    print("Configuration and verified read-only credentials are required before live use.")
    print("EmulationStation must rescan/restart to show newly added menu entries.")


if __name__ == "__main__":
    main()
