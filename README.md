# ambience-rgb

Ambient screen-to-light sync for the RGB hardware **inside** your case, driven by
the colours [matterlights](https://github.com/Jxn01/matterlights) has already
computed for your Matter bulbs. The lamps and the case show the same thing,
because they are shown the same frame.

It drives 207 LEDs here across seven controllers plus a soundbar's light bar,
and it does it by *reading a config file* -- which devices belong to the light around your screen, which light
the room behind it, which to leave alone. The table below is the author's rig,
not a requirement.

### Will this work on your machine?

**If OpenRGB can see your hardware, so can this.** That is the whole dependency
for everything except two devices, which this repository drives itself:

- the **Corsair Commander Core** (the AIO pump and its fans), which OpenRGB
  cannot size the zones of on this board -- a hand-written `hidraw` driver;
- the **Sound Blaster Katana V2X** (and V2) soundbar's light bar, which no RGB
  software on Linux drives at all -- a serial driver, adapted from the
  MIT-licensed [`v2x`](https://git.dog/xx/v2x) reverse engineering.

If you own neither, nothing is lost: disable them in the config and the rest
works unchanged.

You will need **matterlights** running, since this program deliberately does not
capture the screen itself. And today the transport between them is a unix
datagram socket, so this half is **Linux-only** until the Windows port lands --
the reasoning, and what that port actually requires, is in
[docs/specs/2026-09-10-devices-and-windows-research.md](docs/specs/2026-09-10-devices-and-windows-research.md).

The design reasoning is in
[docs/specs/2026-09-09-ambience-rgb-design.md](docs/specs/2026-09-09-ambience-rgb-design.md),
and the traps that cost the most time are in
[docs/specs/EXECUTION-LOG.md](docs/specs/EXECUTION-LOG.md) -- the HID report size
one in particular is worth reading before writing any USB HID driver.

---

## How it fits together

```
matterlights-sync
  capture -> ambience -> Matter bulbs
                      -> unix datagram socket ---.
                                                  |
ambience-rgb (this)                                  |
  subscribe  <------------------------------------'
  near/far -> OpenRGBSink       -> RAM x2, GPU, mouse, monitor, motherboard
           -> CommanderCoreSink -> AIO pump + fans   (our own hidraw driver)
           -> KatanaSink        -> soundbar light bar (our own serial driver)
```

MatterLights captures the screen; this does not. A second capture would double
the PipeWire load and let the lamps and the case disagree during a scene change.

## What it drives

207 LEDs across seven controllers on the author's rig, plus the soundbar. Yours
will differ; this table is what `config.example.toml` describes, and it is the
shipped default.

| Device | LEDs | Group | Bus | Driver |
|---|---|---|---|---|
| Alienware AW3225QF (Logo, Number, Power) | 3 | near | USB | OpenRGB |
| ASUS TUF RTX 5090 | 4 | near | SMBus | OpenRGB |
| Corsair Dominator Titanium DDR5 ×2 | 24 | near | SMBus | OpenRGB |
| ASRock X870E Taichi — *IO Cover*, *PCB* | 24 | near | USB | OpenRGB |
| ASRock X870E Taichi — *addressable headers* | 97 | far | USB | OpenRGB |
| Razer Naga V2 Pro (Logo, Numpad) | 2 | far | USB | OpenRGB |
| Corsair Commander Core — pump | 29 | far | USB HID | **ours** |
| Corsair Commander Core — fans (3 × 8) | 24 | far | USB HID | **ours** |
| Sound Blaster Katana V2X — light bar | one colour | far | USB serial | **ours** |
| Keychron K0 Max | — | **excluded** | — | — |

**near** is what sits around the screen; **far** is what lights the room. The
motherboard is split *per zone* because both are true of it at once.

## Install

**Prerequisites:** [OpenRGB](https://openrgb.org/) (for everything but the
Commander Core) and the *distribution* package for the `hidapi` Python binding --
`python-hidapi` on Arch, `python3-hid` on Debian/Ubuntu. Do **not** `pip install
hid`: the PyPI package under that name is a different library and shadows the
system one inside the venv.

The Katana driver also needs `pyserial` and `cryptography`. The distribution
packages (`python-pyserial`, `python-cryptography`) are used when present and
pip fetches them otherwise -- unlike `hid`, those PyPI packages *are* the same
libraries.

```bash
scripts/install.sh
```

It refuses a conda interpreter, checks `hid` is importable, marks the
distribution Python bindings it uses as explicitly installed (so an orphan sweep
cannot take them), builds the venv with `--system-site-packages`, disables
OpenRGB's Commander Core detector (backing the config up first), installs a udev
rule that gives you the Katana's serial port and keeps ModemManager off it, then
installs three systemd units and enables them.

Then tell MatterLights to publish, in its `.env`:

```ini
RGB_EXTENSION_ENABLED=true
RGB_PUBLISH_SOCKET=/run/user/1000/ambience-rgb.sock
```

and `systemctl --user restart matterlights-sync`.

## Commands

| Command | What it does |
|---|---|
| `ambience-rgb run` | The daemon. What the systemd unit runs. |
| `ambience-rgb off` | Write black to everything once and exit. What the shutdown unit runs. |
| `ambience-rgb devices` | Print what would be driven, with groups, buses and LED counts. **Run this first when something looks wrong.** Read-only: it never changes a colour, and skips the Commander Core and the Katana while the daemon holds them. |
| `ambience-rgb -v …` | Debug logging, including every colour decision. |

## Units

| Unit | Scope | Purpose |
|---|---|---|
| `ambience-rgb-openrgb.service` | user | The OpenRGB SDK server, bound to `127.0.0.1` |
| `ambience-rgb.service` | user | The daemon |
| `ambience-rgb-lights-off.service` | **system** | `ExecStop` writes black at shutdown |

The daemon only `Wants=` the OpenRGB server. A dead server should cost the
motherboard and RAM, not the AIO, which needs no server at all.

## Configuration

`~/.config/ambience-rgb/config.toml`, falling back to `config.example.toml` in the
repo. `$AMBIENCE_RGB_CONFIG` overrides both.

| Key | Meaning |
|---|---|
| `socket.path` | Must equal MatterLights' `RGB_PUBLISH_SOCKET`. `$XDG_RUNTIME_DIR` is expanded, with a working fallback when root runs it at shutdown. |
| `openrgb.host` / `openrgb.port` | SDK server. Keep the host on the loopback. |
| `openrgb.restart_command` | Run this when the server wedges beyond reconnecting. Empty = log only. |
| `rates.usb_hz` / `rates.smbus_hz` | Update rate **per bus**. |
| `commander.enabled` | Drive the AIO at all. |
| `commander.pump_group` / `commander.fan_group` | `near` or `far`. Not `excluded`: the device is written whole, so use `enabled = false`. |
| `katana.enabled` | Drive the soundbar's light bar. **Off unless set**, so a config that predates it never goes looking for a serial port. |
| `katana.group` | `near` or `far`. Not `excluded`, as above. |
| `katana.rate_hz` | The most colour writes per second -- a ceiling, not a rate. Default `2.0`; see *The Katana driver*. |
| `katana.port` | A serial port to use instead of finding the soundbar by its USB IDs (`041e:3283`, `041e:3260`). |
| `devices[].match` | Case-insensitive **substring** of the OpenRGB controller name. |
| `devices[].group` | `near`, `far`, or `excluded`. |
| `devices[].bus` | `usb` or `smbus`, selecting which rate applies. |
| `devices[].zones` | Per-zone group overrides, e.g. `{ "IO Cover" = "near" }`. |
| `devices[].static` | A fixed colour (`"#rrggbb"` or `[r, g, b]`) set once per connection instead of synced. |
| `colour.saturation` | Push each channel away from the mean of the three. `1.0` disables it. |
| `colour.gamma` | Spread the channels apart. Raises saturation; does **not** dim a colour already at peak. `1.0` disables it. |
| `colour.group_cohesion` | Pull the near and far group colours toward each other. `0.0` keeps the engine's intent, `1.0` is one colour everywhere. |
| `spread.bias` | How hard each LED is pulled back to its group's single colour. `1.0` = flat (original behaviour), `0.0` = raw palette gradient. |

⚠️ **`match` is a substring, not an exact name, deliberately.** OpenRGB renames
devices between versions — the Commander Core's ports went from `RGB Port 1` to
`Corsair Fan Header 1` between 1.0rc3 and the git build. An exact name would
silently stop matching and the device would just stop lighting.

⚠️ **`smbus_hz` should stay well below `usb_hz`.** SMBus is shared with the
sensors that fan control and monitoring read — `coolercontrold` runs on this rig
— and hammering it is the one genuinely risky thing this program does. It also
cannot be measured from here: the OpenRGB server acknowledges a packet long
before the device has applied it, so a round trip times liveness, not the bus.
Raise it a step at a time and watch fan control, not a benchmark.

### What the rates actually cost

Measured on the author's rig (Ryzen 7 9800X3D, RTX 5090), capturing 3840×2160 at `SAMPLE_STRIDE=121`:

| Sync rate | matterlights | `ambience-rgb` | `openrgb` |
|---|---|---|---|
| 5 Hz | 16.3% of one core | ~0% | — |
| 10 Hz | 32.4% | ~0% | — |
| 20 Hz | **44.9%** | 0.8% | 0.8% |

The cost is dominated by capture and sampling in matterlights; the daemon and
the server are noise. Scaling is sub-linear past 10 Hz, so the practical ceiling
is roughly **30–35 Hz** before one core saturates — and the capture stream is
capped at twice the sync rate, so 20 Hz already asks Mutter for 40 fps of 4K.

## Depth, and why the LEDs are not sent the bulbs' colours

Two deliberate divergences from "mirror the bulbs", both because a bulb and an
LED are different instruments.

**Colour.** MatterLights renders for Home Assistant: a colour plus a brightness,
shown by a diffuse lamp across a room. An addressable LED is a point source at
full output an arm's length away. Identical chromaticity, completely different
perception — a 6%-saturated pale green reads as tinted on a lamp and as plain
white on a strip. `colour.saturation` and `colour.gamma` push the LEDs somewhere
that *looks* like the lamps, which means sending them different numbers.

**Depth.** The ambience engine clusters each frame into a weighted palette of up
to four colours, then collapses it to one per group — because six bulbs cannot
show more than six colours. This rig has a 97-LED motherboard header and three
8-LED fan rings. `spread.bias` puts the palette back as a gradient across each
strip, so a frame shows its actual colours instead of their average, while still
respecting the near/far split.

Needs matterlights payload **v2** or later. With an older publisher the palette
is absent, every strip goes flat, and that is exactly the pre-v2 behaviour — the
fallback is the well-trodden path, not a special case.

⚠️ **`spread.bias = 1.0` disables the gradient completely**, which is the safe
default in `config.example.toml`'s absence and the thing to set when comparing
against old behaviour.

## When the lights go off

The hardware has no off state — stop writing and it reverts to its firmware
pattern, which on this rig is green fans. So "off" means *writing black*, and
the daemon writes on every tick rather than only on change.

| Trigger | Behaviour |
|---|---|
| Display sleeps | Black, daemon keeps running |
| Screen content dark | Black — mirrors the bulbs, using MatterLights' own verdict |
| **RGB** switch off (dashboard) | Black |
| **Lamp** master switch off | **Ignored — RGB keeps syncing.** It has its own switch. |
| Custom colour/pattern mode | Black — this syncs ambient mode only |
| MatterLights stops | Black after 10 s, rather than freezing on the last colour |
| SIGTERM / logout | Black, then exit |
| Shutdown | `ExecStop` writes black |

The soundbar is the exception to "no off state": it holds whatever it was last
sent, and has its own power supply. Black is still written on every exit path,
so it goes dark with everything else -- and stays dark, in the other OS too,
until something drives it again.

## The Commander Core driver

The Corsair Commander Core (`1b1c:0c32`) is driven directly over
`/dev/hidraw20`, because nothing else can:

| Tool | Reads the 53-LED topology | Sets colours |
|---|---|---|
| OpenRGB 1.0rc3, CLI | yes | yes |
| OpenRGB 1.0rc3, **SDK server** | **no** — every zone reads 0 | no; `RESIZEZONE` crashes it |
| OpenRGB git (0.9.2459) | pump only | pump only |
| liquidctl 1.16 | yes | **no** — `set_color` is `raise NotSupportedByDriver` |
| **ambience-rgb** | yes | **yes — all 53** |

Protocol details are in `commander_protocol.py`, tested byte-for-byte because
the lighting endpoint is **write-only**: a wrong offset produces no error, just
the wrong LEDs.

🚨 **The report size is 64 bytes, and it is the first thing to check if the AIO
goes dark while everything reports healthy.** The four-byte command header
survives a wrong report size, so commands still acknowledge, LED counts still
read back and the topology still looks right -- while the colour payload is
truncated and the LEDs never light. Read it from the device, never assume it:

```bash
od -An -tx1 /sys/class/hidraw/hidraw20/device/report_descriptor
# 75 08 96 40 00  ->  Report Size 8 bits x Report Count 0x0040 = 64 bytes
```

⚠️ **Exactly one process may drive a HID device — and hidraw will not enforce
it.** A second program opens `/dev/hidraw20` just as successfully as the first;
their commands and replies simply interleave. Two contenders are known:

- **OpenRGB**, whose Commander Core detector must stay disabled. Otherwise the
  symptoms are silent — zones reading as size 0, colours that never apply, no
  error anywhere. `scripts/install.sh` disables it; `scripts/uninstall.sh` tells
  you how to put it back.
- **CoolerControl**, whose liquidctl integration must be off. It is on by
  default but inert until liquidctl is installed — then it goes live at its next
  restart. `scripts/install.sh` warns if it is on. See *Troubleshooting*.

## The Katana driver

The Sound Blaster Katana V2X (`041e:3283`, and the V2 at `041e:3260`) exposes its
controls as a **USB CDC ACM serial port** — `/dev/ttyACM0` here, `COM3` on
Windows — not as HID. The protocol was reverse-engineered and published by
Rasmus Moorats ([write-up](https://blog.nns.ee/2026/02/20/katana-v2x-re/),
MIT-licensed [`v2x`](https://git.dog/xx/v2x) crate); `katana_protocol.py`
translates the lighting part of it and carries the crate's licence notice.

| | |
|---|---|
| Connect | Ask for the firmware version. An answer means binary mode already; silence means text mode, and then `whoareyou` → AES-256-GCM challenge → `unlock` → `SW_MODE1` |
| Frames | `5A op length payload`; lighting is opcode `0x3A`; colours travel as ABGR |
| Replies | Every setter is acknowledged (`5A 02 0A 3A 00 …`); those are read as they arrive |
| Tested on | firmware `1.3.230619.1820` |

⚠️ **The soundbar has its own power supply.** Once unlocked it stays in binary
mode until *it* is power-cycled — rebooting the PC does not reset it. So the
unlock runs once per soundbar power cycle (about 1.3 s to connect), and every
start after that finds it ready.

⚠️ **Power-cycled while its USB link stays up, it would ignore every colour in
silence**, because text mode drops binary frames without a word. The sink asks
it a question every 5 s and reconnects when one goes unanswered.

⚠️ **Exactly one process may drive it.** The port is opened with pyserial's
`exclusive=True` — which is only an advisory `flock` — *and* the kernel's
`TIOCEXCL`, which refuses every further `open()` to anyone but root: a second
opener gets `Device or resource busy`. A COM port on Windows is exclusive
anyway; Creative's own app is the contender there.

**It is written as little as possible.** Unlike the Commander Core it *holds*
its colour, so an unchanged colour is never re-sent, a change must exceed
`COLOUR_EPSILON`, and `katana.rate_hz` caps the rest at 2 Hz by default. The
soundbar remembers its lighting across a power cycle, and whether each colour
write reaches its flash is not documented. Measured here: an ACK to a changed
colour arrives as fast as one to an unchanged colour (0.3 ms median), 20 Hz is
sustained with every write acknowledged, and nothing stalls in the fifteen
seconds after a change — no sign of a synchronous or debounced save. That is
evidence, not proof, so the default stays low.

🚨 **Hold firmware updates until someone has checked them.** `1.30.260813.1100`
post-dates the published protocol and its hardcoded key, and a vendor whose
authentication has been published has every reason to change it.

**Permissions.** The port is created `root:uucp 0660`. `scripts/install.sh`
installs `udev/60-ambience-rgb-katana.rules`, which tags it `uaccess` and sets
`ID_MM_DEVICE_IGNORE`, so ModemManager stops probing it with AT commands.
⚠️ The rule's name must sort **before** `73-seat-late.rules`: `uaccess` is only
acted on there, so the same line in a `99-*.rules` file silently does nothing.
Check with `getfacl /dev/ttyACM0` — it must list your user.

Brightness, effects other than a solo colour, and the equaliser are left alone.
The reference can also upload firmware and read and write device memory; none
of that is ported, and a test fails if a builder for it appears here.

## Troubleshooting

**Nothing lights up.** `ambience-rgb devices`. If OpenRGB is unavailable, check
`systemctl --user status ambience-rgb-openrgb`. If the Commander Core is
unavailable, something else is holding `/dev/hidraw20` — usually an OpenRGB
whose detector got re-enabled, or CoolerControl (next entry). (`devices` opens the Commander read-only,
and skips it entirely while the daemon is running: exactly one process may hold
that device, and a diagnostic that fights the daemon for it produces protocol
errors in both.)

**The soundbar does not follow.** Stop the daemon (`systemctl --user stop
ambience-rgb`) and run `ambience-rgb devices`: it opens the soundbar read-only
and prints its model, firmware and current colour, or says why it could not.

- *No Sound Blaster Katana on USB* — it is off or unplugged. It is matched by
  USB IDs only, never by trying whatever `ttyACM` port exists.
- *Permission denied* — the udev rule is missing or did not apply; see
  *Permissions* under *The Katana driver*.
- *Device or resource busy* — another process has the port: `fuser -v
  /dev/ttyACM0`.
- *refused the unlock* — the firmware may have changed the key; compare its
  version with `1.3.230619.1820`.

Each connection logs `katana: Sound Blaster Katana V2X, firmware …, on …`.

**The AIO flickers between its colours and its firmware rainbow every second or
two.** Another program is driving the Commander Core. The journal says so
directly — `another process is driving the Commander Core`; older builds only
said `Reply 0x01 does not acknowledge command 0x0d`. Find it with
`sudo fuser -v /dev/hidrawN` (the path is in the daemon's `Commander Core open
on …` line; `sudo` matters, because a root service's handles are invisible to
you). The usual culprit is **CoolerControl**: liquidctl brackets every poll
with WAKE and SLEEP, and SLEEP is hardware mode — hence the rainbow. Fix:

```bash
sudo systemctl stop coolercontrold
sudo sed -i 's/^liquidctl_integration = true/liquidctl_integration = false/' /etc/coolercontrol/config.toml
sudo systemctl start coolercontrold
```

GPU and hwmon fan control are unaffected; only liquidctl devices leave
CoolerControl.

**A device runs its own firmware effect while OpenRGB says it is in Direct.**
Fixed, and worth knowing why. OpenRGB's `SETCUSTOMMODE` (1100) only assigns
`active_mode` on the server's *in-memory* controller; it never calls
`UpdateMode()`, so the device is never told and every readback happily reports
"Direct". Devices that honour raw per-LED writes anyway (Corsair DRAM, Razer)
work regardless; the ASUS GPU, the ASRock board and the Alienware monitor must
be *told*, and until they are they cycle their firmware pattern while accepting
and discarding every colour. `set_direct_mode` sends `UPDATE_MODE` (1101) with
the mode echoed back verbatim.

**Everything is frozen on an old colour and the logs are clean.** Two different
server faults look identical from here.

*It stopped reading its socket* — visible as a growing Recv-Q, and the daemon's
round-trip probe catches it.

*It answers normally and drives nothing* — round trips at 0.1 ms, new connections
served, controllers reported, and writes with no effect on any device. **No probe
can see this, because the server is live.** Reconnecting does not help; only a
restart does. The daemon now escalates to `openrgb.restart_command` after three
reconnects inside two minutes, at most once every five.

The original description follows: the OpenRGB server has stopped reading its
socket. It can wedge with a client's packets
unread and near-zero CPU, leaking `CLOSE-WAIT` connections with large receive
queues (`ss -tnp | grep 6742` shows them). The daemon now detects this itself —
it times a request/reply every 5 s and drops the connection when the server does
not answer within 2 s — but a wedged server usually needs
`systemctl --user restart ambience-rgb-openrgb`.

⚠️ **Restarting the server resets every device to whatever it reports.** Direct
mode does not persist, so a device nothing writes to comes back dark. That is
what `devices[].static` is for: the Keychron is `excluded` from syncing but
carries `static = "#ffffff"`, which is re-applied on every connection, including
after a server restart. Without it, "excluded" meant nothing ever put its white
back and it had to be fixed by hand each time.

**Colours are stale — the case lags the screen by seconds or shows an old
frame.** The write side is fire-and-forget: `sendall` returns in microseconds
regardless of what the server does with the bytes, so overrunning it is
invisible from here. Writing every target every tick offered the server ~43
packets a second against real USB and SMBus devices; it drained them far slower
and the backlog grew without bound. The sink sends only what
changed — by at least `COLOUR_EPSILON` per channel, because the ambient colour
jitters a unit or two every frame and exact equality still resent everything —
with a 2 s refresh. If you raise `usb_hz` or add targets, that budget is what
you are spending.

⚠️ **You cannot see this backlog locally.** On loopback our send queue reads
zero while the server sits on hundreds of kilobytes unread: the bytes just move
into its receive buffer. `ss -tnp | grep 6742` and read the *server* side's
Recv-Q; if it grows, we are overrunning it.

**Everything is white.** Expected on a greyscale screen: it is above the dark
cutoff but carries no saturation, so it normalises to white at full brightness.
A property of the shared ambience model, identical on the bulbs. Raise
`DARK_THRESHOLD` in MatterLights to cut such screens off instead.

**The fans are green.** Nothing is writing to them. Green is the Commander
Core's firmware fallback about ten seconds after the last commit.

**The fans flash green every minute or so.** Historically: a dropped
acknowledgement taking the sink out of service for longer than the AIO's
ten-second memory. In practice this was the wrong report size (see above) and has
not recurred since; check the report descriptor first. Under sustained
streaming this controller occasionally fails to answer a continuation packet
inside the read timeout -- about 0.4% of frames, measured over a twelve-minute
soak. The endpoint write is retried whole (never packet-by-packet: the sequence
is a transaction), and this sink's retry backoff is deliberately shorter than the
others for exactly this reason. If it comes back, look for
`commander-core: write failed` in the journal rather than suspecting the fans.

**Colours lag or stutter.** Lower `rates.smbus_hz` first.

**Everything blanks for a few seconds, every minute or two.** The publisher
stopped publishing for longer than `STALE_AFTER_SECONDS`, and going dark is the
correct response — a frozen frame is indistinguishable from working sync. The
cause is usually upstream and blocking rather than dead: matterlights' Home
Assistant call blocks the whole loop while a flaky bulb fails to answer, so one
unreachable Matter bulb stalls the screen capture too. That threshold must stay
comfortably above the publisher's worst blocking call; when it was 3 s and the
Home Assistant timeout was also 3 s, every bulb timeout blanked the case.
Correlate `journalctl --user -u matterlights-sync | grep ReadTimeout` against the
blackouts before suspecting anything here.

**The RGB is dark but the lamps are on.** Check the dashboard's *RGB* switch —
it is independent of the lamps' switch by design.

## Other hardware, and Windows

Researched 2026-09-10, written up in
[docs/specs/2026-09-10-devices-and-windows-research.md](docs/specs/2026-09-10-devices-and-windows-research.md):

| | |
|---|---|
| **Ducky One 3 TKL RGB** | **Ruled out.** Its RGB is onboard-only — no host lighting software exists, so there is nothing to capture and no protocol to speak. The vendor HID interface is for firmware. All the open-source prior art targets the One **2**, a different vendor ID. |
| **Sound Blaster Katana V2X** | **Driven since 2026-09-10** — see *The Katana driver*. Live on Linux; on Windows it is `COM3` through the inbox `usbser` driver, so the same code will open it once the port lands — the most portable device in the project, needing no OpenRGB server and no HID. ⚠️ If you own one, hold off on firmware `1.30.260813.1100`: it post-dates the published protocol and nobody has checked it. |
| **Windows** | **Planned.** The only real blocker is the IPC: Windows `AF_UNIX` is `SOCK_STREAM` only, so the datagram socket needs a UDP-on-loopback sibling. Everything else follows the per-platform package pattern matterlights already proves. |

## Development

```bash
PYTHONPATH=src /usr/bin/python3 -m unittest discover -s tests
```

No network, no hardware, no OpenRGB required — the protocol layers are pure and
the transports are tested against fakes.

### Integration checks

Everything above proves the logic. It cannot prove a light lit. That takes
hardware, and one of them takes a person:

```bash
scripts/integration.sh                        # policy + soak
python tests/integration/check_failures.py    # restarts/kills services
python tests/integration/check_hardware.py    # asks you what you saw: AIO, then soundbar
```

🚨 **`check_hardware.py` is the only check here that can fail for the reason
that matters.** The others read state back from the OpenRGB server's own model,
which proves a packet was accepted and nothing more. This project spent a day
with every command acknowledged, every LED count correct and every colour
reading back exactly as sent, while the AIO was completely dark — the report size
was wrong and the truncation fell past the command header. See
[tests/integration/README.md](tests/integration/README.md).
