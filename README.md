# VM Arcade

A native, read-only Proxmox dashboard for a Raspberry Pi arcade cabinet.
It displays host and guest metrics alongside an animated network graph.
It uses SDL2 rather than a browser, Electron, or a dashboard VM.

The logical canvas is 800×600 and scales with the display's aspect ratio.
Both views target 30 frames per second; independent telemetry workers sample
host and guest counters at 2 Hz. Missing data remains unknown, and a failed live
connection never falls back to demonstration data.

## Features

- Default network graph: Internet and gateway, host containers, bridges,
  configured VLAN/untagged/trunk branches and individual VM/container nodes.
- Continuous bidirectional particle trails driven by measured per-NIC rates.
  Particle emission is proportional to bytes/s on a shared host/VM scale.
  Opposite directions use the same scale; a 10:1 rate produces about 10:1
  particle density. Existing particles keep moving smoothly when rates change.
  WAN uses its own paired scale. These are not packet traces.
  Stopped guests, missing counters and stale/failed sources do not animate.
- Scrolling RX/TX history: all hosts at the top, selected VM at bottom right.
- Per-VM RX/TX, busiest NIC sender, selected guest CPU/RAM, host addresses,
  and optional UniFi site WAN upload/download from existing Prometheus metrics.
- Stable guest selection, four guests per host page and three hosts per page;
  navigation brings the selected host and guest into view. Click a guest on Mac
  or use the cabinet joystick. Extra NICs remain separately attached.
- Independent host selection; host CPU use, I/O wait, 1/5/15-minute load,
  memory used/total/available, swap, uptime, storage and network history.
- QEMU VMs and LXC containers, with scrolling selection, state, vCPU,
  CPU percentage, reported memory and selected guest network/disk rates.
- Metrics motion: per-thread CPU meters, a 60-second CPU trace, RAM history
  and a recent peak marker, proportional network lanes, and four small histories
  for the selected guest's RX, TX, disk read and write rates. Numerical values
  remain the actual samples; bar interpolation only smooths their movement.
- Cluster totals above/below the selected host: capacity-weighted CPU, RAM
  used/total/available, running guests and summed guest RX/TX. Missing host data
  makes its affected cluster total unknown rather than silently shrinking it.
- Details view with uptime and root/boot disk capacity.
- Multiple storage entries; physical Y cycles through them.
- Explicit unknown/cached states, per-section failures, request timeouts,
  independent host workers and retry backoff. Empty VM inventories stay empty.
- Dedicated demo mode. A failed live connection never falls back to fake data.
- TLS CA and hostname verification, or an explicitly configured SHA-256
  certificate identity pin checked **before** transmitting the API token.
  The app never enrolls or updates a certificate pin automatically.
- GET-only API allowlist; no guest start/stop or host configuration changes.
  Optional SSH reads only `/proc/stat` over an existing pinned connection;
  it runs a fixed in-memory sampler and installs no files or services.

The standalone `design/approved-preview.html` is an interactive design preview
with fictional data. It makes no Proxmox requests. All example host names,
addresses and demo workloads in this public repository are illustrative.

## Run locally

Requirements: Python 3.7 or later, PySDL2, SDL2, SDL2_ttf, and a monospace font.
On RetroPie, the installer checks for the existing Python SDL2 bindings,
SDL2_ttf, and DejaVu Sans Mono. It does not install packages or upgrade the OS.

A tested Mac development environment uses Python 3.11, PySDL2 0.9.17 and
pysdl2-dll 2.32.0:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install PySDL2==0.9.17 pysdl2-dll==2.32.0
.venv/bin/python -m vm_arcade --demo --windowed
```

With a private configuration:

```sh
python3 -m vm_arcade --config ~/.config/vm-arcade/config.json --windowed
python3 -m vm_arcade --config ~/.config/vm-arcade/config.json --check --seconds 15
```

`--view network|metrics` chooses the starting view; `--node NAME` selects the
initial host. `--font FILE` overrides the font. Network is the default view.

Keyboard controls: **N** opens Network, **M** opens Metrics, **Space/Tab** toggles
views, arrows select hosts/guests, Enter or A opens guest details, and Escape or
B returns to the graph. Close the window to quit. On-screen button legends are
intentionally hidden.

On Mac, `python3 scripts/install-mac-launcher.py` creates
`~/Downloads/VM Arcade.app`. The shortcut uses the project's `.venv` and
`~/.config/vm-arcade/config.json`; credentials remain outside the application.
Run the script again if the checkout moves. An unrelated existing app is preserved.

## Private Proxmox configuration

```sh
umask 077
mkdir -p ~/.config/vm-arcade
cp config.example.json ~/.config/vm-arcade/config.json
chmod 700 ~/.config/vm-arcade
chmod 600 ~/.config/vm-arcade/config.json
```

Replace the documentation-only addresses and example node names in your private
copy. Node names must match Proxmox. Each node needs its API URL, a trusted CA
file or explicitly verified TLS fingerprint, token identifier, and token file.
Paths are relative to the configuration directory unless absolute. Token files
must be owned by the running user and have mode 0600. CA files are public
certificates; API token values and SSH private keys must remain private.

Use dedicated read-only accounts and privilege-separated API tokens. A typical
setup grants the `PVEAuditor` role to both the monitoring user and token at the
required scope. Verify effective Sys.Audit, VM.Audit and Datastore.Audit rights.
The app and installer do not create users, tokens, ACLs, firewall rules or routes.
Configure network access separately for the cabinet and your hosts.

API calls are GET-only and restricted to an endpoint allowlist. HTTPS uses CA
and hostname verification, or a preconfigured SHA-256 certificate pin checked
before sending credentials. Certificate pins are never enrolled automatically.

## Install on RetroPie

First verify stable power, network access and the required graphics dependencies.
Extract the source bundle, then run as the normal RetroPie user:

```sh
python3 scripts/install-retropie.py
```

The installer creates a versioned release under `~/.local/share/vm-arcade/`,
updates the `current` symlink and retains the previous release for rollback.
It adds **VM Arcade** to the existing **RetroPie** menu and to **Ports** where
that system is enabled. Restart/rescan EmulationStation after installation.
A cabinet without a Ports system can use **RetroPie → VM Arcade**.

The installer does not use sudo, change game controls, modify system XML,
install a boot service or replace the normal RetroPie boot menu. Menu launchers
run the dashboard as the RetroPie user and return to the menu on exit.

To build a source bundle:

```sh
python3 scripts/build-bundle.py /tmp/vm-arcade-retropie.tar.gz
```

The bundle contains the reviewed source, example configuration, documentation,
tests and design preview. Live configurations, credentials and captures are not
included. A SHA-256 companion file is generated next to the archive.

## Cabinet controls

The app reads `/opt/retropie/configs/all/emulationstation/es_input.cfg`.
Use `--input-config FILE` to select another mapping. Physical button positions
vary between cabinets; identify their key signals before remapping them.

| EmulationStation control | Dashboard action |
| --- | --- |
| Joystick left/right | Previous/next host |
| Joystick up/down | Previous/next guest |
| A | Guest details / Metrics |
| B | Network graph |
| X | Toggle Network / Metrics |
| Start | Metrics overview |
| Y | Next storage entry |
| Select | Exit to RetroPie |

The optional top-level `exit_key` setting overrides the keyboard Exit action.
For example, `"exit_key": 115` uses S. It removes all previous keyboard exit
bindings, including Select, while preserving other actions. Omit it to retain
the original behavior. The setting does not change EmulationStation or games.
USB gamepad buttons, axes and hats are also supported through the input mapping.

## Network graph and traffic

Guest attachments come from current Proxmox guest configurations. Bridges,
addresses and gateways come from the node network API. This is a logical map;
configured VLANs do not prove end-to-end isolation or reveal the physical switch
path. Trunk traffic is one NIC stream, not a separate total for every VLAN.

Each host branch sums the per-NIC RX/TX of all its running guests, including
those outside the visible page. Guest NIC RX/TX includes LAN traffic. Host sums
exclude host-only management/storage traffic and do not imply that every byte
crosses the gateway. Inter-guest transfers can be counted at both endpoints.
WAN measurements remain a separate data source.

Counters are sampled every 0.5 seconds by default. `traffic_poll_seconds` accepts
0.25–1 second; request latency can limit actual cadence. Status/inventory uses
`poll_seconds`, with a minimum of 0.5 seconds. Storage refreshes more slowly;
topology and historical RRD data are cached. Repeated cached API values are not
new source measurements. New/restarted interfaces need a baseline before a
rate is displayed. Missing or stale input stops new traffic animation.

Metrics traffic emits one particle per 4 KiB in either direction. Particles
travel for one second; changed rates affect future emissions without removing
particles already in flight. Zero/unknown rates drain existing particles.
Both directions use the same unit. Above 2 MiB/s per lane, rendering caps at
512 particles/second and shows FLOW LIMIT; numeric readings remain unchanged.
These are byte-volume symbols, not packet captures or measured transit times.

Network/WAN animation also uses shared directional scales. WAN starts at
32 KiB per dot and increases the unit for high rates. Low rates do not fill a
link merely because they are the higher direction. Overall and selected-guest
traffic histories retain up to five minutes of in-memory samples, with gaps
for unavailable data. Metrics histories use a 60-second window.

### Optional WAN source

An existing Prometheus exporter with UniFi WAN metrics can supply site traffic:

```json
"wan_monitor": {
  "url": "http://192.0.2.63:9090",
  "site": "example-site",
  "instance": "unifi-example",
  "device": "Example gateway",
  "port": "eth4",
  "gateway": "192.0.2.1"
}
```

Use this only when the source is appropriate for the configured Proxmox hosts.
The optional gateway mapping must match their configured route to animate the
WAN-to-cluster link. Without a source, WAN values remain unknown.

Queries select the exact site, exporter instance, device and port. Upload is
`unpoller_device_wan_transmit_rate_bytes`; download is
`unpoller_device_wan_receive_rate_bytes`. Collection polls every 15 seconds and
checks upstream timestamps, cache age, duplicate series and source availability.
This feed is not a 2 Hz WAN measurement. Samples older than 90 seconds, or not
retrieved for 40 seconds, stop animating.

The source may use HTTP on a trusted network or HTTPS with system CA validation.
URL credentials are rejected, and no Proxmox token is sent to the exporter.
The app does not install, modify or expose an exporter.

## Optional CPU thread readings

The standard API provides aggregate CPU usage. For per-thread values, add
`cpu_ssh` to a node in your private configuration:

```json
"cpu_ssh": {
  "user": "monitor",
  "identity_file": "~/.ssh/vm-arcade-monitor",
  "known_hosts_file": "~/.ssh/known_hosts"
}
```

This requires an already authorized key, a verified SSH host identity and
Python 3 on the host. The app reads `/proc/stat` using a fixed sampler every
0.5 seconds over a persistent connection. It does not install remote files,
require root, or enroll keys. Missing access shows unavailable meters.
On SMT systems each meter represents a logical thread; up to 32 are drawn.

Optional `jump_host` must name another configured Proxmox host. Both hops enforce
saved host keys. Optional `control_dir` enables connection reuse in an existing
owner-only directory; idle connections expire after 60 seconds.

## Metric semantics

| Display | API source and meaning |
| --- | --- |
| Host CPU and I/O wait | CPU uses the optional `/proc/stat` stream when current, otherwise API `cpu` × 100; I/O wait uses API `wait` × 100 |
| Host load | Same endpoint, `loadavg`, 1/5/15 minutes; never guest load |
| Host memory/swap/uptime | Same endpoint, byte counts and seconds |
| Per-thread CPU | Optional SSH `/proc/stat` deltas every 0.5 seconds; logical CPU IDs, including SMT threads where present |
| Dashboard histories | Actual samples collected in memory, 60-second window; unknown periods remain gaps |
| Host RX/TX | Sum of running guests' per-NIC byte rates, identical to the host network paths |
| Storage | `/nodes/{node}/storage`, individual entries; never summed because pools may overlap |
| QEMU inventory | `/nodes/{node}/qemu?full=1`, full status includes optional disk counters |
| LXC inventory | `/nodes/{node}/lxc` |
| Guest CPU | Proxmox `cpu` × 100, relative to allocated vCPU capacity |
| Guest RAM | Proxmox `mem`/`maxmem`; balloon information when available, not guaranteed guest application working set |
| Guest RX/TX and disk I/O | Counter deltas over monotonic elapsed time; first sample, resets, stops and long gaps show `--` |
| Root/boot disk capacity | `maxdisk`; not guest filesystem use/free space |

Host RRD history is fetched at most once per minute. Its age is shown
separately from status polling. Missing or unsupported fields are `--`.
No temperatures, per-core utilization or guest process lists are fabricated.
Guest CPU/memory values may repeat if Proxmox updates its underlying cache more
slowly than the polling interval; repeated reads are not new source measurements.
No telemetry, tokens or API responses are persistently logged by the app.

## Validation

```sh
.venv/bin/python -m unittest discover -s tests -v
SDL_VIDEODRIVER=dummy .venv/bin/python -m vm_arcade --demo --windowed \
  --seconds 1 --screenshot /tmp/vm-arcade-demo.bmp
```

The automated suite covers counter resets, direction, missing data, history,
particle continuity, TLS pins, credential-file permissions, exit replacement,
menu installation and screenshot allocation. See `VERIFICATION.txt` for the
public validation summary.

`scripts/validate-live.py` and `scripts/validate-metrics.py` perform bounded
read-only checks against your own private configuration. Their captures and
reports can contain infrastructure details: keep output under ignored `build/`
or outside the checkout. No live captures or deployment records are distributed.

## Rollback and private files

Stop the dashboard before rolling back. The previous release is recorded in
`~/.local/share/vm-arcade/previous-release.txt`; restore the `current` symlink to
that release. To remove menu entries, remove `VM Arcade.sh` from
`~/RetroPie/retropiemenu/` and `~/RetroPie/roms/ports/`, then rescan the menu.
Separately revoke monitoring credentials when retiring an installation.

Keep real configuration, API tokens, private keys, environment files and capture
output outside Git. Common sensitive paths are ignored, but ignore rules do not
remove files already tracked. Review the staged diff and Git history before
publishing. Examples use reserved documentation addresses and dummy test values.

## References

- [Proxmox API documentation](https://pve.proxmox.com/pve-docs/api-viewer/)
- [Proxmox users, tokens and roles](https://github.com/proxmox/pve-docs/blob/master/pveum.adoc)
- [Linux CPU counters](https://docs.kernel.org/filesystems/proc.html)
- [PySDL2](https://pysdl2.readthedocs.io/)
- Visual inspiration: [bpytop](https://github.com/aristocratos/bpytop) and [eDEX-UI](https://github.com/GitSquared/edex-ui).
