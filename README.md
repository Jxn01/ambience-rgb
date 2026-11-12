# ambience-rgb

[![Platform: Linux](https://img.shields.io/badge/platform-Linux-FCC624?style=for-the-badge&logo=linux&logoColor=black)](https://www.kernel.org/)
[![Platform: Windows](https://img.shields.io/badge/platform-Windows%2011-0078D4?style=for-the-badge&logo=windows&logoColor=white)](https://www.microsoft.com/windows)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![OpenRGB](https://img.shields.io/badge/OpenRGB-SDK-E1301E?style=for-the-badge)](https://openrgb.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green?style=for-the-badge)](LICENSE)

Ambient screen-to-light sync for the RGB hardware **inside** your case, driven by the colours
[matterlights](https://github.com/Jxn01/matterlights) already computes for your room lights. The lamps and the case
show the same thing because they are shown the same frame. On my rig it drives 207 LEDs across seven controllers plus a
soundbar's light bar, on Linux and Windows, from one config file.

## Will it work on your machine?

If OpenRGB can see your hardware, so can this. Two devices are reached directly instead:

- the **Corsair Commander Core** (AIO pump and fans), which OpenRGB cannot drive fully — a `hidraw` driver of its own;
- the **Sound Blaster Katana V2X** light bar, through Home Assistant's [`sb_katana`](https://github.com/Jxn01/ha-katana)
  integration, which owns the soundbar over Bluetooth.

Without either, disable them in the config and the rest works the same. matterlights must be running: this program
does not capture the screen itself, so the lamps and the case can never disagree.

## How it fits together

```
matterlights sync loop
  capture -> ambience -> Matter bulbs
                      -> one datagram per tick ----------------------.
                         Linux:   unix socket, $XDG_RUNTIME_DIR       |
                         Windows: UDP, 127.0.0.1:45731                |
ambience-rgb                                                           |
  subscribe  <--------------------------------------------------------'
  near/far -> OpenRGBSink       -> RAM, GPU, mouse, monitor, motherboard
           -> CommanderCoreSink -> AIO pump + fans   (own HID driver)
           -> KatanaHASink      -> Home Assistant -> soundbar light bar (Bluetooth)
```

Every device belongs to a group: **near** is what sits around the screen, **far** is what lights the room. The link is
fire-and-forget (Windows has no unix datagram sockets, hence UDP on the loopback there), so the daemon can never slow
matterlights down.

| Device (my rig) | LEDs | Group | Driver |
|---|---|---|---|
| Alienware AW3225QF | 3 | near | OpenRGB |
| ASUS TUF RTX 5090 | 4 | near | OpenRGB |
| Corsair Dominator Titanium DDR5 ×2 | 24 | near | OpenRGB |
| ASRock X870E Taichi — IO cover, PCB | 24 | near | OpenRGB |
| ASRock X870E Taichi — addressable headers | 97 | far | OpenRGB |
| Razer Naga V2 Pro | 2 | far | OpenRGB |
| Corsair Commander Core — pump and 3 fans | 53 | far | own HID driver |
| Sound Blaster Katana V2X — light bar | 1 colour | far | Home Assistant |

## Install

### Linux

Needs [OpenRGB](https://openrgb.org/) and the distribution's `hidapi` binding (`python-hidapi` on Arch, `python3-hid`
on Debian/Ubuntu — not `pip install hid`, which is a different library).

```bash
scripts/install.sh
```

It builds the venv at `~/.local/share/venvs/ambience-rgb` from the system Python, configures OpenRGB (its Commander Core
detector off, the red/green-swapped zones from the config on), keeps ModemManager off the soundbar's USB serial port,
and installs three systemd units. Re-run it after pulling or changing the config. Then tell matterlights to publish —
`ambience-rgb devices` prints the exact line for its `.env`:

```ini
RGB_EXTENSION_ENABLED=true
RGB_PUBLISH_SOCKET=/run/user/1000/ambience-rgb.sock
```

### Windows

Needs Python 3.11+ from python.org, OpenRGB, and an elevated PowerShell. Here the `hidapi` binding comes from pip.

```powershell
scripts\windows\install.ps1                        # reports other RGB software
scripts\windows\install.ps1 -DisableCompetitors    # ...and stops and disables its services
```

`-OpenRgb <path>` points at `OpenRGB.exe` if it is not found; `-NoOpenRgbTask` skips the OpenRGB server task. The
installer builds `.venv`, configures OpenRGB, registers three scheduled tasks and prints the `.env` line for
matterlights (`RGB_PUBLISH_SOCKET=udp://127.0.0.1:45731`). Other RGB software (iCUE, SignalRGB) must not run beside it:
two programs on the Commander Core make it fall back to its firmware rainbow.

## Running it

| Command | What it does |
|---|---|
| `ambience-rgb run` | the daemon |
| `ambience-rgb off` | write black to everything once and exit |
| `ambience-rgb off --stop-daemon` | stop a running daemon cleanly (it blacks and releases every device), then black everything again |
| `ambience-rgb devices` | what would be driven, with groups and LED counts, and the `RGB_PUBLISH_SOCKET` value for matterlights — run this first when something looks wrong |

Options: `--config PATH`, `--log-file PATH`, `-v`. On Linux the commands live in the venv
(`~/.local/share/venvs/ambience-rgb/bin/ambience-rgb`); on Windows run `.venv\Scripts\python -m ambience_rgb …`.

| Linux unit | Windows task | Purpose |
|---|---|---|
| `ambience-rgb-openrgb.service` | `ambience-rgb-openrgb` | the OpenRGB SDK server on `127.0.0.1` |
| `ambience-rgb.service` | `ambience-rgb` | the daemon (under `pythonw` on Windows, so no console window) |
| `ambience-rgb-lights-off.service` | `ambience-rgb-lights-off` | black at shutdown (root / SYSTEM) |

The Windows shutdown task runs as SYSTEM, so it is only visible from an elevated PowerShell — start it to test with
`Start-ScheduledTask -TaskName ambience-rgb-lights-off` there; unelevated, Windows reports it as not existing.

## When the lights go off

RGB hardware has no off state — stop writing and it returns to its firmware pattern — so "off" means writing black.

- Black when the display sleeps, when the screen is dark, when the RGB switch is off, in matterlights' saved-colour
  modes, and 10 seconds after matterlights stops publishing.
- Black on every exit: SIGTERM/Ctrl+Break, logout, and shutdown (the system unit on Linux; on Windows a session-end
  hook at logoff and the SYSTEM task at shutdown).
- The lamps' own master switch does not affect it — the case has its own switch.

## Colour and depth

LEDs are not sent the bulbs' colours verbatim. A diffuse lamp and a point-source LED an arm's length away read the same
colour very differently, so `colour.saturation` and `colour.gamma` push the LEDs toward what *looks* like the lamps.
And where the bulbs get one colour per group, long strips get the frame's whole palette back as a gradient
(`spread.bias`), so a frame shows its actual colours instead of their average.

## The Commander Core driver

`commander_core.py` and `commander_protocol.py` drive the Corsair Commander Core (`1b1c:0c32`) over hidraw, because
nothing else could set all 53 of its LEDs (the OpenRGB SDK server reads its zones as size 0; liquidctl does not support
colour on it). The lighting endpoint is write-only, so the protocol is tested byte for byte. Two things worth knowing
before writing any HID driver:

- **Read the report size from the device** (`/sys/class/hidraw/*/device/report_descriptor`). With a wrong size the
  command header survives and every command acknowledges, while the colour payload is truncated and nothing lights.
- **One process per HID device.** hidraw lets a second program open it and their commands interleave silently, so
  OpenRGB's Commander Core detector and CoolerControl's liquidctl integration must be off.

## Configuration

See [docs/configuration.md](docs/configuration.md) for every key: the socket, OpenRGB, per-bus update rates, the device
list with groups, zones, channel order and static colours, colour tuning and the soundbar.

## Development

```bash
PYTHONPATH=src /usr/bin/python3 -m unittest discover -s tests
scripts/test-under-wine.sh            # the same suite on Windows CPython under Wine
```

No hardware or OpenRGB is needed: the protocol layers are pure and the transports are tested on the loopback. The
Windows-script tests also render the scheduled tasks with PowerShell when `pwsh` is available. With the hardware
present, `scripts/integration.sh` and `tests/integration/check_hardware.py` check the real devices — the last one asks
you what you saw, the only check that proves a light actually lit.

## License

MIT — see [LICENSE](LICENSE).
