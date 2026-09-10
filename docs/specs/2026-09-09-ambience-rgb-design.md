# ambience-rgb — design

**Status:** approved 2026-09-09. Implemented in the same session.

Ambient screen-to-light sync for the RGB hardware in `jxn-garuda`, reusing
[matterlights](https://github.com/Jxn01/matterlights) as the capture and colour
engine rather than duplicating it.

## Why this is its own repo

It is **private, Linux-only and machine-specific**. It hard-codes the device set
of one rig, depends on that rig's OpenRGB build and a hand-written driver for one
AIO controller, and is useless anywhere else. Windows already has better tooling
for this. None of that belongs in matterlights, which is public, cross-platform
and general — but it still deserves version control, so it lives here.

## Scope

**In:** ambient sync only. The colours come from matterlights' `ambience` mode,
already computed for the Matter bulbs, and are mirrored onto local RGB hardware.

**Out:** zoned and shared-variant modes, custom colours and patterns, any
capture of its own, any Home Assistant contact, Windows, any other machine.

## The one-capture constraint

matterlights already captures the screen and renders an ambience palette five
times a second. Running a second capture would double the PipeWire load and let
the bulbs and the case disagree during a scene change.

So **matterlights publishes and ambience-rgb subscribes**. matterlights gains one
small, config-gated, fire-and-forget publisher; everything else lives here.

```
matterlights-sync
  capture -> ambience -> Matter bulbs
                      -> unix datagram socket ---.
                                                  |
ambience-rgb                                         |
  subscribe  <------------------------------------'
  map near/far -> OpenRGBSink      -> DRAM x2, GPU, Razer, AW3225QF, ASRock
               -> CommanderCoreSink -> pump 29 + fans 3x8   (our own driver)
```

### Wire format

One JSON object per datagram, UTF-8, on a `SOCK_DGRAM` unix socket.

| Field | Type | Meaning |
|---|---|---|
| `v` | int | Payload version. Currently `1`. |
| `near` | `[r,g,b]` | Colour for the near group. |
| `far` | `[r,g,b]` | Colour for the far group. |
| `brightness` | int 0-255 | Frame average brightness. |
| `active_ratio` | float | Fraction of sampled pixels above the dark threshold. |
| `screen_dark` | bool | matterlights' own dark-screen decision. |
| `display_on` | bool | Real display power state. |
| `lights_on` | bool | Dashboard master switch. |
| `rgb_on` | bool | Dashboard RGB switch (this project's own toggle). |

**ambience-rgb consumes decisions, never re-derives them.** `screen_dark` and
`lights_on` are matterlights' calls; mirroring the bulbs means obeying those
flags, not recomputing darkness from the palette. Only `rgb_on` is ours.

Datagram, not stream, on purpose: the publisher must never block the sync loop
and must not care whether anybody is listening. A `SOCK_DGRAM` send to a socket
with no reader fails immediately with `ENOENT`/`ECONNREFUSED`, which the
publisher swallows.

## Device mapping

Groups follow the bulbs' near/far model.

| Group | Devices |
|---|---|
| **near** | AW3225QF (Logo, Number, Power Button) · RTX 5090 · DDR5 x2 · ASRock *IO Cover* and *PCB* |
| **far** | ASRock *Addressable Header 1/2/3* and *RGB LED 1 Header* · Commander Core pump and fans · Razer Naga V2 Pro |
| **excluded** | Keychron K0 Max — left entirely alone, by explicit request |

The ASRock board is split **per zone**, not per device: its board-surface zones
sit behind the screen and read as near, while the addressable headers drive case
strips and fans and read as far.

## Sinks

```python
class Sink(Protocol):
    name: str
    def write(self, near: RGB, far: RGB) -> None: ...
    def black(self) -> None: ...
    def close(self) -> None: ...
```

Two implementations. Both are best-effort: a sink that raises is logged, marked
degraded and retried later, and never takes the daemon down. Losing the AIO must
not stop the motherboard from syncing.

### OpenRGBSink

Speaks the OpenRGB SDK protocol directly over TCP — no third-party client
library. The server is a separate user unit that owns the hardware, because
**exactly one process may hold a given HID device**; this was established the
hard way when a second process read every Commander Core zone as size 0.

Bound to `127.0.0.1`. OpenRGB's default is `0.0.0.0`, which exposes local
lighting control to the LAN.

### CommanderCoreSink

OpenRGB 1.0rc3 registers the Commander Core but reports every zone as 0 LEDs in
server mode, and `RESIZEZONE` on it crashes the server outright
(`std::out_of_range`). The newer git build fixes the pump only. liquidctl reads
the topology correctly but its `set_color` is `raise NotSupportedByDriver`.

So this project drives it directly. The device is removed from OpenRGB's
detector list so the two never contend for `/dev/hidraw20`.

## Commander Core protocol

Corsair Commander Core, PID `0x0c32`, 96-byte reports (97 with the report ID).
Framing follows liquidctl's proven Python implementation; the lighting write
follows OpenRGB's `CorsairCommanderCoreController`.

**Command envelope.** `buf[0] = 0x00` (report id), `buf[1] = 0x08`,
`buf[2:4] = command`, payload from `buf[4]`. Every reply echoes the command byte
at `res[1]`, which is a real device-level ACK and is asserted on every packet.

**Endpoint write** = open, write, close:

| Step | Command | Payload |
|---|---|---|
| Open | `0x0d 0x00` | endpoint id |
| First | `0x06 0x00` | `len+2` LE (2B), `0x00 0x00`, data type (2B), then up to 87 data bytes |
| More | `0x07 0x00` | up to 93 raw data bytes |
| Close | `0x05 0x01` | none |

⚠️ liquidctl's own `_write_data` begins with a read-back of the endpoint to check
the data type. The lighting endpoint is **write-only** and that read never
returns, so this project uses open/write/close with no read, matching OpenRGB.

| Purpose | Endpoint | Data type |
|---|---|---|
| LED counts (read) | `0x20` | `0x0f 0x00` |
| Fan mode (write) | `0x1e` | `0x0d 0x00` |
| Direct colour (write) | `0x22` | `0x12 0x00` |

**Colour buffer — always exactly 699 bytes:**

```
offset   0 .. 86    pump      29 LEDs x 3
offset  87 .. 188   port 1    34 slots x 3
offset 189 .. 290   port 2
offset 291 .. 392   port 3
offset 393 .. 494   port 4
offset 495 .. 596   port 5
offset 597 .. 698   port 6
                    87 + 6*102 = 699
```

Each port is padded to **34 slots** regardless of how many LEDs are attached.
This rig has 8 per port on ports 1-3 and nothing on 4-6.

⚠️ **Write the real LED count and pad the remainder black. Never fill all 34
with colour.** `SetFanMode` forces "6 QL fan" mode so the controller exposes 34
slots per port, after which a naive count read can report 34 rather than the 8
that physically exist. The authority for how many LEDs are real is the count read
from endpoint `0x20` *before* forcing fan mode — which is also what liquidctl
reports, giving an independent cross-check.

**Timing.** The device rate-limits direct colour to one commit per 33 ms (~30 Hz).
The 5 Hz stream is six times under that. If it stops being fed for 10 seconds the
controller reverts to its firmware pattern, so the sync stream is its own
keepalive; when idle, an explicit black must be written rather than simply
closing the handle.

## Lights off — three layers, independent of the bulbs

The devices have no "off" state. Ceasing to write returns them to firmware
defaults, which on this rig is green fans. So off means **explicitly writing
black**, and it must happen on every exit path:

1. **Display sleep or dark screen** — write black, keep running.
2. **SIGTERM / SIGINT** (logout stops the user unit) — write black, then exit.
3. **`ExecStop`** at shutdown — a system unit writing black, mirroring
   matterlights' own arrangement.

Every one runs in a `finally`, so a crash still blacks the devices.

## Configuration

`~/.config/ambience-rgb/config.toml`, with a documented example in the repo. Device
mapping, socket path, per-bus rate limits and the excluded-device list all live
there. Nothing about this rig is hard-coded in the source.

**Rate limiting is per bus, not global.** SMBus devices (DDR5, GPU) are updated
at a slower cadence than USB HID ones; SMBus is shared with sensors and hammering
it is the one genuinely risky thing this program does.

## The matterlights side

Three changes, all config-gated and inert by default, so the public repo gains a
feature that costs nothing when unused:

1. `RGB_EXTENSION_ENABLED` (bool, default false) and `RGB_PUBLISH_SOCKET` (path).
2. The publisher itself — one non-blocking datagram per tick.
3. A dashboard **RGB on/off** toggle, rendered **only** when the extension is
   enabled, writing `rgbOn` into the existing `.matterlights-control.json`
   beside `lightsOn`. One source of truth, no second IPC.

## Testing

- **Protocol framing is pure and tested byte-for-byte** against the reference:
  envelope layout, the 699-byte buffer offsets, port padding, and that a
  short LED list pads black rather than overrunning into the next port.
- **Sinks are tested against fakes**, including that a raising sink is isolated.
- **The publisher is tested to never raise** with no listener, a full buffer, or
  a deleted socket — it may not be able to break the bulbs.
- **The dashboard toggle** is tested for both states of the config flag.
- **Live end-to-end** on the real hardware, with OpenRGB devices verified by
  reading colours back over the SDK.

⚠️ **The Commander Core cannot be verified by readback** — its lighting endpoint
is write-only. Evidence for it is: every packet ACKed by the device, LED counts
unchanged afterwards (proving it is not wedged), byte-exact protocol tests, and a
known end colour for a human to confirm. This limitation is inherent, not an
omission.

## Known limitations

- Ambient mode only. Custom colours and patterns drive the bulbs but not RGB.
- Greyscale content produces white at full brightness — a property of the shared
  ambience model, identical on the bulbs, not introduced here.
- Requires a running `openrgb --server`. If it dies the daemon notices, drops
  the connection and reconnects on its own -- measured at 11 s from restart to
  driving again -- so the devices go black briefly rather than freezing. The
  Commander Core is unaffected either way; it has its own driver.
- **The per-zone write path stalls OpenRGB's ASRock controller.** Zones are
  contiguous ranges of one LED array, so every split controller is written as a
  single whole-device packet instead. Measured: six per-zone writes stalled
  within 1-2 frames; one whole-device write ran 400 ticks over 80 s clean. A
  device with an *excluded* zone cannot use that path and keeps the risk.
- ~~The Commander Core emits unsolicited `0xF0` reports~~ **-- resolved.** These
  were the device reacting to 97-byte reports when its descriptor declares 64.
  Zero occurrences since the report size was corrected. The defences added while
  chasing them (a 250 ms reply timeout, retrying a whole endpoint write, a short
  retry backoff) are kept: they are correct on their own terms and cost nothing,
  but they are no longer load-bearing.
