# Research: two more devices, and a Windows port

Three questions, answered with evidence gathered on `jxn-garuda` on 2026-09-10.
One is ruled out, two are planned.

| | Verdict | Confidence |
|---|---|---|
| Ducky One 3 TKL RGB | **Ruled out** — no host lighting protocol exists to speak | High |
| Sound Blaster Katana V2X | **Planned** — already reverse-engineered by someone else | High |
| Windows port | **Planned** — small hostile surface, pattern already proven next door | Moderate |

---

## 1. Ducky One 3 TKL RGB — ruled out

`3233:8311`, five HID interfaces, present and enumerated.

The hardware could not look more inviting. `hidraw3` is a **vendor-defined
interface** — usage page `0xFF00`, `75 08 95 40`, so 64-byte input *and* output
reports with no report ID. That is the exact shape of the Commander Core channel
this project already drives.

**And it is almost certainly not a lighting channel.**

### The evidence

- **The One 3 has no host lighting software at all.** Its RGB is configured
  entirely on the keyboard, through `Fn` combinations — modes (Wave, Aurora,
  Ripple, Breathe…), per-channel colour via `Fn+F5/F6/F7`, and a palette on
  `Fn+Space`. Per-key colours are stored in the keyboard's own profiles.
- **"Ducky Macro V2.0" is not desktop software.** It is the onboard macro system:
  key-combination programming, six profiles, stored in the keyboard.
- **DuckyHub**, Ducky's official WebHID tool, does **firmware updates only**, and
  lists **One X** and **Cushion 98** as supported. Not the One 3.
- **All the prior art is for the One 2**, which is a *different vendor*: DuckyAPI,
  DuckyCustomRGB and DuckySharp all target Holtek-based `04d9` boards. Different
  VID, different MCU, different firmware. Nothing transfers.
- **OpenRGB has no protocol data.** Issues #2194 and #5263 are unfilled request
  templates. `usedbytes/ducky-tools`' One 3 issue is a bug report about firmware
  extraction with no protocol details.

### Why that is fatal to the usual approach

The standard route for a device like this is: run the vendor's software on
Windows, capture the USB traffic, replay it. **There is no vendor software to
capture.** The vendor interface is consistent with firmware update and onboard
profile transfer — which is what DuckyHub and `ducky-tools` both use it for.

### 🚨 What we are deliberately NOT doing

**Fuzzing the vendor interface.** It is the firmware-update channel on a device
whose own vendor warns that a bad update renders the keyboard unusable. Bricking
it is realistic and unrecoverable, and no amount of curiosity justifies that on
someone's daily keyboard.

### The only honest path left, if it is ever wanted

Zero hardware risk, entirely offline, in order:

1. Download `Ducky_One3_V1.07.exe` from Ducky's downloads page.
2. Extract and decrypt the firmware image with `usedbytes/ducky-tools` (it exists
   precisely to do this, though it does not yet recognise V1.07's container).
3. **Statically analyse the image** for a host-command dispatch table on the
   vendor endpoint, and specifically for any command that writes LED state.
4. If such a command exists, implement it. If it does not, the only remaining
   route is custom firmware, which is a different project entirely.

Step 3 answers the question definitively and cannot harm the keyboard. Until
someone does it, the honest position is that the One 3's lighting is not
addressable from the host.

**Also needed regardless:** `/dev/hidraw3` is `crw------- root root`. Our user has
no access. `/dev/hidraw20` works only because OpenRGB's udev rules tag it (note
the `+` ACL). A one-line rule would fix it.

---

## 2. Sound Blaster Katana V2X — planned

`041e:3283`, present, and **someone has already done the hard part.**

### What is already known

Reverse-engineered and published in February 2026 (`blog.nns.ee`), with a working
MIT-licensed Rust implementation, `v2x` (`v2x-ctl` CLI), supporting **both the
Katana V2 and V2X**.

| | |
|---|---|
| Transport | **CDC ACM serial**, not HID — `/dev/ttyACM0` here |
| Framing | `5A` start marker, opcode byte, length byte, payload |
| Auth | **AES-256-GCM challenge-response**: device sends a 32-byte nonce, host replies with IV + ciphertext + tag using a device-specific hardcoded key |
| Tested firmware | `1.3.230619.1820` |

The lighting API is exactly what a sink needs:

```
set_lighting(bool)      set_color(Color)        set_brightness(u8)
set_mode(AnimationMode) set_colors(&[Color])    set_speed_bpm(u8)
LightingState           // readback of current state
```

Confirmed on this machine: `/dev/ttyACM0`, `041e:3283`, `SB Katana V2X`,
`bcdDevice 10.30`, `cdc_acm` loaded. Owned `root:uucp`; our user is in `input`
and `wheel`, so access needs a udev rule or group membership.

### Plan

**Where it fits.** A `KatanaSink` alongside `OpenRGBSink` and
`CommanderCoreSink`, in the **far** group — it lights the desk and wall behind
the monitor, not the screen surround.

Everything the sink needs already exists and generalises: `ResilientSink` for
isolation, `released_on_failure` for handle hygiene, the change filter with
`COLOUR_EPSILON`, per-bus rate limiting, `black()` on every exit path, and the
`[commander]`-style config block pattern.

**Implementation, preferred option — port the protocol to Python.** The crate is
MIT, so it can be read and adapted with attribution. Python has `cryptography`
for AES-GCM, and `pyserial` (or plain `os.open` on the tty) for the transport.
Keeps one language, one process, no extra runtime.

**Fallback — a long-lived Rust helper** fed colours on stdin, if the handshake
proves fiddlier than it reads. Rejected as the default only because it adds a
build toolchain and a second process to supervise.

**Order of work:**

1. Grant access to `/dev/ttyACM0` (udev rule, matching on `041e:3283`).
2. Run `v2x-ctl status` first — **verify our `bcdDevice 10.30` against the tested
   `1.3.230619.1820`** before writing a line of code. A firmware mismatch could
   invalidate the key or the opcodes.
3. Establish the handshake in Python and read `LightingState` back. That readback
   is valuable: unlike the Commander Core, this device can tell us what it holds.
4. Add `KatanaSink` + `[katana]` config (`enabled`, `group`, `rate_hz`).
5. **Measure the sustainable update rate.** Unknown, and a soundbar's LEDs have no
   reason to accept 20 Hz. Start at 2–5 Hz. The per-bus rate limiter already
   handles this; the Katana simply becomes its own bus.
6. Add a Katana phase to `tests/integration/check_hardware.py` — the only check
   that can fail for the right reason.

**Risks, honestly:**

- **Firmware mismatch** is the big one, and step 2 exists to find it early.
- **It is an audio device.** Colour writes share a link with the audio path;
  verify that streaming colours does not glitch playback. This is the one risk
  with no precedent in this project.
- The hardcoded key is model-specific — fine for us, but it means the sink cannot
  be assumed to work on other Sound Blasters.

---

## 3. Windows port — planned

The hostile surface is genuinely small, and the pattern is already proven in the
sibling project.

### Already portable, untouched

`openrgb_client.py` (pure sockets), `render.py`, `commander_protocol.py`,
`config.py`, the daemon's decision logic, both sinks, and all 191 unit tests. The
`/dev/hidraw*` mentions in `commander_core.py` are **comments** — the code uses
`hid.enumerate()` and `open_path()`, which are portable.

### Blockers, each with a resolution

**1. The IPC is Linux-only, and this is the one real design decision.**

`AF_UNIX` + `SOCK_DGRAM`, in three places (daemon bind, the `devices` liveness
probe, matterlights' publisher). Microsoft's own AF_UNIX announcement is explicit:
Windows supports **`SOCK_STREAM` only**; `SOCK_DGRAM` is unsupported.

→ **Add a UDP-on-loopback transport.** It preserves the semantics that are
actually load-bearing: fire-and-forget, non-blocking, drops are fine, and the
publisher can never block the lights. `AF_UNIX` `SOCK_STREAM` would *not* — it is
connection-oriented, and a publisher that can block is precisely what this
project spent a night proving must not exist.

Keep the unix socket on Linux for its `0600` file permissions; select the
transport by config, behind one small abstraction, with both paths tested. A
colour is not a secret, but binding to `127.0.0.1` is still the right default.

⚠️ While there: `AmbiencePublisher._ensure_socket` catches only `OSError`. If
`socket.AF_UNIX` is absent entirely the attribute lookup raises `AttributeError`
and takes the loop down. Latent today only because the extension defaults off.

**2. Service management.** systemd user units and `install.sh` → Task Scheduler.
matterlights already ships `install-autostart.ps1`, `install-shutdown-task.ps1`,
`start-sync.ps1`, `start-dashboard.ps1` and `guided-setup.ps1`. Mirror them
rather than inventing anything.

**3. Paths.** `$XDG_RUNTIME_DIR` and the `/run/user/{uid}` fallback in
`config.py`. matterlights already solved this in `paths.py` — adopt it.
⚠️ That module is also where a `Path.home()` call once shipped a Windows-only
startup crash, so it gets tested on both platforms, not reasoned about.

**4. hidapi — and the rule inverts.** On Windows, `pip install hidapi`: it is
cython-hidapi, imports as `hid`, and ships wheels for cp38–cp314 including
`win_amd64`. **This is the exact opposite of the Linux rule**, where pip must
never be allowed near it (the PyPI `hid` package is a different library that
shadows the system binding — recorded in the KB after it cost a debugging
session). The two platforms need opposite instructions, so both belong in the
README side by side.

**5. Device contention — the open question.** The one-process-per-HID-device rule
is not OS-specific. On Windows the competitors are **iCUE** for the Commander
Core and **SignalRGB**, which this rig is recorded as using and which also holds
the DDC/CI channel. Both would have to be closed or disabled for the devices we
drive. OpenRGB's Windows build may additionally need administrator rights for
SMBus (RAM and GPU).

**This needs an answer before any work starts:** if iCUE or SignalRGB are
expected to run on the Windows half, the Commander Core sink cannot run there,
and the port is OpenRGB-devices-only.

**6. Lights-off at shutdown.** matterlights has `shutdown_hook/windows.py` and a
scheduled-task installer already; reuse both. Windows allows only a few seconds
at logoff, which that code already accounts for.

**7. Not blockers.** numpy has Windows wheels. The GRB channel-order fix is a
property of the board's wiring and carries over unchanged in config. The
integration checks work anywhere the daemon runs.

### Shape of the work

Follow matterlights exactly: a package-per-concern with a dispatching
`__init__.py`, as it does for `capture/`, `display_power/`, `shutdown_hook/` and
`service_control/`. For us that is one new package — `transport/` — plus a
`scripts/windows/` set of PowerShell installers.

| Step | Effort |
|---|---|
| `transport/` abstraction, unix + UDP, both tested | small |
| Path handling via matterlights' `paths.py` pattern | small |
| PowerShell installers mirroring matterlights' | moderate |
| Verification on the Windows half | needs a reboot, and eyes |

The last row is the honest cost. Everything else can be developed and unit-tested
from Linux; **none of it is proven until it has run over there**, and this project
has already established that a green test suite is not evidence about hardware.

---

## Sources

- OpenRGB Ducky One 3 requests: GitLab issues
  [#2194](https://gitlab.com/CalcProgrammer1/OpenRGB/-/issues/2194),
  [#5263](https://gitlab.com/CalcProgrammer1/OpenRGB/-/work_items/5263)
- One 2 prior art: [DuckyAPI](https://github.com/Latedi/DuckyAPI),
  [DuckyCustomRGB](https://github.com/simon-yeunglm/DuckyCustomRGB),
  [DuckySharp](https://github.com/voximity/DuckySharp)
- [ducky-tools](https://github.com/usedbytes/ducky-tools) and its
  [One 3 issue](https://github.com/usedbytes/ducky-tools/issues/2)
- [DuckyHub](https://duckyhub.io/) · [Ducky downloads](https://ducky.global/pages/downloads)
- Katana reverse engineering: [blog.nns.ee](https://blog.nns.ee/2026/02/20/katana-v2x-re/)
  · [`v2x` crate](https://lib.rs/crates/v2x) · [docs](https://docs.rs/v2x/latest/v2x/)
- [AF_UNIX comes to Windows](https://devblogs.microsoft.com/commandline/af_unix-comes-to-windows/)
- [PyPI `hidapi`](https://pypi.org/project/hidapi/) (cython-hidapi)
