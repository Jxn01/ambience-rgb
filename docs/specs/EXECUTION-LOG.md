# Execution log — 2026-09-09

Built and verified in one session, against
[the design](2026-09-09-linux-rgb-design.md). What follows is what actually
happened, including the parts that went wrong.

## What shipped

| | |
|---|---|
| `linux-rgb` | 120 tests, private repo, pushed |
| `matterlights` | publisher + master-off publishing + LED rendering, 227 tests, pushed |
| LEDs driven | **207** across 7 controllers |
| Verified live | capture → bulbs → socket → OpenRGB + AIO, on real hardware |

## Verified on the machine

- **Frames flow at 5.1/s** from matterlights over the unix datagram socket.
- **Colours match end to end** — *in the plumbing only, and that distinction
  turned out to matter.* The colour read back from OpenRGB equals the colour in
  the published frame, tick for tick. That is a readback of the **server's own
  model**, not of any LED. It proved the transport and proved nothing about
  whether a single diode lit; three separate faults hid behind it for a day.
  See "The day after" below.
- **The per-zone split works.** Fed near=red / far=blue, the ASRock board's *IO
  Cover* and *PCB* read red while all four addressable headers read blue — from
  a single controller, exactly as configured. (Again: read back from the server.
  The board was in fact showing a firmware pattern at the time.)
- **The Commander Core lights.** Red, green and blue each held for two seconds;
  LED counts `[29, 8, 8, 8, None, None, None]` before and after, matching
  liquidctl as an independent implementation.
- **The dashboard toggle works**, both ways: `rgbOn=false` blacked all 207 LEDs,
  `rgbOn=true` restored them. Confirmed by reading the hardware back, not by
  reading the code.
- **The toggle is genuinely absent when the extension is off** — checked by
  looking at a 1280×720 screenshot in both states, not just by grepping the
  HTML. No console errors in either.
- **All three lights-off layers**, each through systemd as installed:
  display sleep, SIGTERM (`Signal SIGTERM; turning RGB off` → hardware black),
  and the system unit's `ExecStop`.
- **The Keychron K0 Max is never written to** and sits at white throughout.

## Traps

### 🚨 The PyPI package named `hid` is not the one you want

`pyproject.toml` declared `dependencies = ["hid"]`, so pip installed the PyPI
package of that name — **a different library with a different API** — into the
venv, where it shadowed the system `cython-hidapi`. The Commander Core then
failed with `module 'hid' has no attribute 'device'`.

The binding this needs is the distribution package (`python-hidapi` on Arch),
reached through `--system-site-packages`, exactly as matterlights reaches
PyGObject. **It must never be declared as a pip dependency**, and the file now
says so where the temptation is.

### 🚨 A correctly contained failure can still be a real failure

The shutdown unit opened the AIO seconds after the daemon released it and got
`returned data type (0, 0), expected (15, 0)` — which reads like a protocol
mismatch and is actually timing: waking is not enough while the device is still
settling from its previous owner.

Sink isolation did its job and swallowed it, so the shutdown logged **`RGB off`
while the AIO had in fact been left lit**, to drift back to firmware green a few
seconds later. The isolation was right; trusting its success message was not.
Found only by exercising the layer through systemd rather than by hand.

### 🚨 A 0 ms HID timeout BLOCKS, and cost the most time of anything here

Under sustained streaming the controller's replies drifted one behind and the
acknowledgement check started rejecting good packets. The fix is what liquidctl
does — clear enqueued reports before every command — but the obvious way to
write that, `read(n, 0)` in a loop, **hung the daemon outright**: in
cython-hidapi a 0 ms timeout means *blocking*, not *poll*.

It cost a wedged process and two wrong diagnoses (an unbounded loop, then a
settling problem) before the sequence was instrumented and measured, at which
point it was unambiguous: `read(n, 1)` returns empty in 0.001 s, `read(n, 0)`
never returns. Both the non-zero timeout and the loop bound are asserted by
tests now, because either one alone is still a hang.

The wider lesson repeated from earlier in the session: **when a process hangs,
instrument the sequence rather than reasoning about which line is guilty.** Both
wrong guesses were plausible and both were wrong.

### 🚨 A server can answer before it knows anything

The OpenRGB server binds its socket several seconds before device detection
finishes. A daemon starting alongside it connected successfully to a server that
reported **zero controllers**, cached "drive nothing", and stayed dark until
someone restarted it by hand. An empty plan is now a failure that retries.

### The bulbs' master switch reaches further than it looks

The owner chose "the lamps' switch does not silence the RGB". Implementing that
in the daemon was one line — but matterlights takes a master-off branch that
never reaches the capture, so **nothing was published at all** and the extension
went dark on its staleness timeout anyway. The daemon change alone would have
looked correct in review and been wrong on the machine.

### Off is a thing you keep writing

None of this hardware has an off state. Stop writing and the Commander Core
reverts to its firmware pattern after ~10 s and the ASRock headers to the
board's green. So the loop writes every tick rather than on change, "off" is a
repeated black frame, and every exit path writes black in a `finally`.

### Opcode 1102 is SAVEMODE, not RESIZEZONE

Resize is **1000**. Sending 1102 expecting a resize does nothing visible. Worse,
a genuine resize on a controller with no zones crashes OpenRGB 1.0rc3 outright
(`std::out_of_range`), which is how the Commander Core's zones were confirmed
genuinely empty rather than merely misparsed.

## The day after — three faults behind one symptom

Reported the next morning as "only the RAM and the mouse are working, everything
else is turned off", and separately "the RGB off switch doesn't work". Both
sentences were accurate. Neither was one bug.

The verification that let all three through was reading colours back from the
OpenRGB **server**. That confirms the packet arrived and nothing more. The one
check never made was a human looking at the hardware, which is precisely the
check that was declined on the night (the webcam points into the owner's room).
A readback that cannot fail for the reason you care about is not evidence.

### 🚨 SETCUSTOMMODE does not reach the device

The client sent `SETCUSTOMMODE` (1100) and the docstring claimed it "puts a
controller into its direct/custom mode so writes take effect". It does not.
OpenRGB's server handles it by assigning `active_mode` on its own in-memory
controller; it never calls `UpdateMode()`. The device is never told.

The split this produces is invisible from the server. Corsair DRAM and the Razer
mouse honour per-LED writes in any mode, so they worked. The ASUS GPU, the ASRock
board and the Alienware monitor must be *told* to enter direct mode — so they
cycled their firmware patterns for a day, accepting and discarding every colour,
while every readback reported "Direct". The tell was the owner saying *the GPU is
rotating between colours, like its default preset* — a sentence no readback could
have produced.

Fixed by sending `UPDATE_MODE` (1101) with the mode echoed back byte-for-byte,
which is why the parser now keeps each mode's raw slice.

### 🚨 A sampled colour is not the colour a bulb emits

The payload's `near`/`far` were the raw screen sample. Home Assistant takes
`rgb_color` and `brightness` separately and normalises the colour, so with
`BRIGHTNESS_FLOOR=255` a lamp fed `(24, 24, 23)` emits a bright warm white.
An addressable LED has no brightness channel — its brightness *is* the magnitude
of its triple — so it got a 9% grey, indistinguishable from off.

That also explains the second complaint exactly: the RGB switch worked, driving
every device `(23,23,23)` → `(0,0,0)` → `(24,24,24)`, and nothing visible
changed. **A control that works perfectly can still be reported as broken when
both of its states look identical.**

Fixed in matterlights, not here: the extension cannot reconstruct what a bulb
emits, because the payload's `brightness` was the raw average rather than the
effective brightness, and the floor lives over there.

### 🚨 Fire-and-forget hides an overrun completely

Writing every target every tick offered the server ~43 packets a second. It
applies them one at a time against real USB and SMBus hardware, far slower than
that, so the backlog grew without bound and the board displayed a frame from
minutes earlier. Across the 8.5 hours the display slept, the queue was full of
black — so the board stayed dark long after the screen came back.

Every `sendall` returned in 0.00 ms throughout. There is no backpressure, no
error and no log line; the only symptom is that the lights are wrong in a way
that looks like a dead device. The sink now writes only what changed, with a 2 s
refresh, and `black()` deliberately bypasses the filter.

### The diagnostic that caused a fault

`linux-rgb devices` opened the Commander Core, forced fan mode and wrote black on
close — while the daemon held it. It produced a real
`Reply 0x01 does not acknowledge command 0x07` in the daemon's log, from the
debugging itself. `devices` is now read-only and skips the device entirely while
the daemon is listening on the socket.

### 🚨 The protocol version is negotiated PER CLIENT, and defaults to 0

`REQUEST_CONTROLLER_DATA` carries the version it wants, so descriptions come
back correctly formatted whether or not the handshake ever happened. Everything
reads fine. But the server also stores a version *per client*, defaulting to 0,
and `UPDATE_MODE` is parsed at **that** version. A version-3 mode block read as
version 0 is 8 bytes longer than expected (the brightness fields), so the
remainder becomes the next packet's header.

The connection then never recovers: the server waits for a payload length it
invented, our packets pile up unread -- 238 kB were measured -- and every reply
we wait for never arrives. It presents as *"the OpenRGB server is wedged"*, and
it is not: only our stream is. Other clients were answered in 0.1 ms throughout,
which is what finally distinguished the two.

Fixed by sending `REQUEST_PROTOCOL_VERSION` (40) at connect, requesting every
description at the negotiated version and echoing every mode back at the same
one, so the two cannot drift apart.

⚠️ **This was correct and it did not fix the freeze.** The commit that landed it
said it did; the very next run still stalled at 36 s. Handshaking is right on its
own terms, but the cause was elsewhere -- see below. A plausible mechanism that
survives a code review is still a guess until the symptom actually goes away.

### 🚨 The per-zone write path stalls OpenRGB's ASRock controller

The real cause of the freeze. Writing the board's six zones as six
`UPDATE_ZONE_LEDS` packets stalls the server: it stops applying anything on that
connection, so the case sits on an old colour while writes pile up unread. It is
timing-dependent, which is what made it look like six different bugs -- a fresh
server survives a few frames, sometimes fifteen, then stops.

The fix needs no cooperation from OpenRGB. **Zones are contiguous ranges of one
LED array**, so the same colours can be written as a single whole-device
`UPDATE_LEDS` whose array carries each zone's colour in order. Identical
result, one packet instead of six, and it does not touch the path that breaks.

Measured on this rig, modes pushed, colours changing every tick:

| | |
|---|---|
| six per-zone writes | stalled within 1-2 frames |
| one whole-device write | **400 ticks over 80 s, zero failures** |

Targets fell from 11 to 6. A device with an *excluded* zone still needs the
per-zone path, since a whole-device write must colour every LED it covers; the
config here has none.

### The measurement that was worthless

A 75-second soak wrote 374 full frames at 5 Hz with a worst reply of 17 ms and
concluded the server handled the load comfortably. It did not: that test never
pushed direct mode, so **every write it measured was a no-op the device
ignored**. Timing a write that does nothing measures nothing. Once modes were
actually pushed the same sequence stalled within three frames.

Its sibling: a health check that reconnects on the first slow reply reconnects
about every twenty seconds, because this server also goes quiet for tens of
seconds while *still applying* the writes it is sent. Being unresponsive and
being broken are different states, and the check now needs six consecutive
misses before it acts.

### What was missing, structurally

`openrgb_client.py` and `sinks.py` had **no test files at all** — the two modules
that carried all three bugs. 99 tests now, up from 74; the new ones assert the
mode packet is 1101 and not 1100, that an unchanged colour is not resent, and
that a read-only close writes nothing.

## Verified without the owner present

Their session was locked, so there was no screen capture and no real frames. That
turned out to be better than watching the screen: publishing frames *as
matterlights does*, straight to the daemon's socket, means the colours are known
exactly and every policy branch can be asserted against what the hardware
actually holds.

**Policy, 9/9** -- near/far mapping across all seven controllers, the lamp master
switch being ignored (the owner's explicit choice), the RGB switch blacking
everything, display sleep, dark screen, staleness blacking rather than freezing,
and recovery from each. The Keychron asserted white throughout.

**Failure injection, 7/7** -- the OpenRGB server restarted underneath a running
daemon, the daemon SIGKILLed, and a clean stop:

| Injected | Result |
|---|---|
| OpenRGB server restart | drives again in **11 s**, Keychron's white restored automatically |
| `SIGKILL` the daemon | systemd restarts it, driving again in **4 s** |
| `systemctl stop` | every driven device black, static Keychron untouched |

⚠️ **The first version of the SIGKILL test passed instantly and proved nothing.**
It waited for the hardware to show the expected colour -- which it already did,
because a killed daemon leaves the last colour standing. Nothing had to recover
for it to pass. It now blanks the device first, so only a real write can satisfy
it. Third instance this session of a check that cannot distinguish two states.

### A connection error must drop the client

Found by that injection, not by reading the code. `ResilientSink` catches a write
failure but cannot clear the sink's client, so after the server died the daemon
kept writing to the same broken pipe -- failing identically every time. The only
path that ever reconnected was the health check, six timeouts later. Measured
recovery **71 s**; dropping the client at the point of failure took it to **11 s**.

## Deliberately not done

- **Custom colours and patterns** drive the bulbs only; the RGB goes black in
  those modes. Ambient-only was the agreed scope.
- **The webcam was not used to verify colours.** It points into the owner's room
  and they were asleep. The Commander Core's lighting endpoint is write-only, so
  its evidence is device ACKs, unchanged LED counts, byte-exact protocol tests
  and a known end state — not a photograph.

## Left running

`linux-rgb-openrgb`, `linux-rgb` and `matterlights-sync`/`-dashboard` are
enabled and active; `linux-rgb-lights-off` is armed. The RGB follows the screen
while the lamps stay off, which is the state the owner left the lamps in.
