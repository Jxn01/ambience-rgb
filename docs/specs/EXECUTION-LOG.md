# Execution log — 2026-09-09

Built and verified in one session, against
[the design](2026-09-09-ambience-rgb-design.md). What follows is what actually
happened, including the parts that went wrong.

## What shipped

| | |
|---|---|
| `ambience-rgb` | 140 unit tests + 4 integration checks, private repo, pushed |
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

`ambience-rgb devices` opened the Commander Core, forced fan mode and wrote black on
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

### A failed operation must drop the handle -- in BOTH sinks

Found by that injection, not by reading the code. `ResilientSink` catches a write
failure but cannot reach inside to clear the sink's handle, so after the server
died the daemon kept writing to the same broken pipe -- failing identically every
time. The only path that ever reconnected was the health check, six timeouts
later. Measured recovery **71 s**; dropping the handle at the point of failure
took it to **11 s**.

The sweep found the same shape in `CommanderCoreSink`, which had no equivalent
rescue at all: an AIO that was unplugged, or whose handle went bad, would have
been retried forever against the dead handle and never reopened. It has not been
seen in the wild only because the transport's drain happens to self-correct the
one failure mode encountered so far.

So the behaviour is a shared primitive, `released_on_failure`, used by both --
rather than the same fix written twice and drifting.

### 🚨 An unsolicited 0xF0 report, and a timeout that punished the wrong device

The twelve-minute soak was the first sustained streaming this had ever seen, and
it found what no readback could: `commander-core: write failed (No reply to
command 0x07)` roughly every 25-50 seconds.

Bisected rather than guessed, which mattered, because the first two theories were
both wrong:

| Test | Result |
|---|---|
| Commander alone at 5 Hz, 180 s | **0 failures** -- the device is fine |
| Both sinks in one process | fails, at *identical* times across runs |
| Same, health check disabled | identical -- not the health check |
| Every colour value 0-255 in isolation | **none fail** -- not the data |

Deterministic times pointed at instrumenting the exchange, and the signature was
the same every time: **the controller emits an unsolicited report beginning
`0xF0`**, in bursts lasting a few seconds. It answers nothing we sent; while it
is happening the acknowledgement never comes. The skip loop discarded it
correctly -- and then spent the **full 1000 ms read timeout** waiting for an ACK
that was never coming.

That timeout was the real defect, and it belonged to the whole daemon rather
than to this device: the loop is single-threaded, so three attempts x 1000 ms
stalled everything for three seconds, which is why the *motherboard* missed a
colour check in the same soak. **A fault in one device was surfacing as a fault
in another.**

Measured over 2020 exchanges: p50 3.0 ms, p99 3.0 ms, max 49 ms. The timeout is
now 250 ms -- five times the worst reply ever seen, and 4x cheaper to fail.

⚠️ **Called "the device's own behaviour" here, and that was wrong.** The bursts
stopped completely once the report size was corrected -- zero `commander-core`
events in live streaming since, against roughly one a minute before. They were
the device's response to being sent 97-byte reports it never asked for. The
measurements below stand as evidence of the *symptom*, not of a device quirk:

| | |
|---|---|
| longest gap with **no successful write** | **2.14 s** |
| successful writes over four minutes | 984, against 24 skips |

Comfortably inside the revert window, so the fans hold their colour rather than
flashing.

⚠️ **A partial soak is not a trend.** At t+150 s of the verification run this was
reported as fixed on the strength of zero failures so far. The failures were
simply clustered later; the final tally was the same rate as before. Reporting
an in-progress measurement as a result is how a regression gets called a fix.

### 🚨 The recovery path was turning the AIO off

Reported as *"the commander still doesn't light up"* while every other device
tracked the screen correctly.

`close()` blacks the device before dropping the handle, which is right for a
clean shutdown -- the controller has no off state, so going dark has to be an
explicit write. The failure path called the same method. So every transient
failure **explicitly darkened the hardware it was trying to rescue**, then
reopened it. With the `0xF0` bursts arriving every ~25 seconds, the AIO spent
its life being blacked and reopened.

Letting go of a broken handle and shutting down cleanly are different
operations that happened to share a name. They are now `release()` and
`close()`, and the failure path calls `release()`.

⚠️ **This was self-inflicted, and introduced the same day as the recovery
mechanism it lived in.** The mechanism was right -- a dead handle must not be
reused -- and the fix for it reached for the only method that dropped a handle
without checking what else that method did. Three tests now pin it: a release
puts nothing on the wire, a release is idempotent, and `close()` still blacks.

The route to it was the owner's own observation. The protocol had been exonerated
by then (a hand-driven write lit the device on demand, and the wire format has one
commit and had not changed since the night it first worked), so the fault had to
be in *when* we wrote rather than *what* -- and the only code that wrote without
being asked to was the failure path.

### 🚨🚨 The report size: why the AIO never lit, and why nothing could tell us

**The device's reports are 64 bytes. We were sending 97.**

Its own HID report descriptor says so -- `75 08 96 40 00`, Report Size 8 bits x
Report Count `0x0040` -- and OpenRGB agrees, selecting `PACKET_SIZE_V3 = 65` for
PID `0x0C32`, being those 64 bytes plus hidapi's report-id byte.

**The failure is silent by construction, and that is the whole story.** The
four-byte command header sits at the front of every packet, so it always arrived
intact. Every command was acknowledged. LED counts read back correctly. The
topology was right. The transport reported perfect health for an entire session.
Only the colour payload -- which lives *past* the header and spans many packets
-- was cut off past byte 64. No readback can see that. Only the hardware can, by
staying dark.

It also explains the shape that looked so strange:

| Observed | Why |
|---|---|
| daemon running -> AIO **dark** | wake and fan-mode DO work, so it leaves its firmware effect and waits for direct colour that never arrives |
| daemon stopped -> AIO **rainbow** | nothing driving it, so it times out back to hardware mode |
| commands ACK, counts correct | the header always fit in 64 bytes |

Confirmed on the hardware: a five-phase colour sequence that showed *nothing*
before the change produced red, blue, green and white on cue after it.

### What this cost, and the lesson

Six earlier findings this session were real bugs, and every one of them was found
while chasing this one's symptoms. The `0xF0` bursts, the recovery path blacking
the device, the 1000 ms timeout stalling the loop -- all genuine, all fixed, none
of them the reason the lights were off.

**Four separate readbacks said "healthy" while the device sat dark**, and the
answer was in `/sys/class/hidraw/*/device/report_descriptor` the whole time.

> Ask the device what shape it expects before auditing your implementation of the
> shape you assumed.

The owner's observations were what broke it open, twice: *"the gpu is rotating
between colours"* found the mode bug, and *"it lit up with white for a second,
then dark, then the rainbow"* -- reported precisely, and initially argued with --
was the evidence that the device was entering software mode and then receiving
nothing usable.

## Afterwards: retiring the scaffolding

With the root cause fixed, the defences built while chasing its symptoms were
measured rather than left in place on faith. A 300-second streaming run:

| | |
|---|---|
| HID exchanges | 18,364 |
| endpoint writes | 1,312 |
| **retries fired** | **0** |
| health checks | 60 |
| **health misses** | **0** |

So the endpoint-write retry is kept but documented as not load-bearing, and the
health check's tolerance came *down* from 6 consecutive misses to 3. Six was
tuned for a server that seemed to go quiet for tens of seconds -- which was never
the server, but our own loop stalling on a 1000 ms HID timeout and the per-zone
path stalling OpenRGB. At 0 misses in 60, six only meant half a minute of stale
colour before a real wedge was noticed.

The four harnesses that found the session's bugs moved out of the scratchpad into
`tests/integration/`, named `check_*.py` so unit discovery leaves them alone.
`check_hardware.py` is the important one: **the only check in the repository that
can fail for the reason that cost a day**, because it asks a person rather than
asking software about software. Its failure path prints the report-descriptor
command instead of leaving the next person to rediscover it.

## 2026-09-10, afternoon: a second driver on the Commander Core

After a reboot through Windows the AIO flickered between its colours and its
firmware rainbow every 1–3 s. Nothing in ambience-rgb had changed. The previous
boot had run 34 hours with 3 acknowledgement misses; this one logged 57 in seven
minutes, every one `Reply 0x01 does not acknowledge command 0x0d`.

**The cause was a session of ours, a day earlier.** CoolerControl has run here
since May, only for the GPU fan curve. Its liquidctl integration defaults to on
but had been inert for months — the previous boot logged `liquidctl system
Python package not found`. On 2026-09-09 a research session installed liquidctl
system-wide to read its Commander driver. CoolerControl looks for it only at
start, so nothing happened until the next boot, when it logged `Connecting to
LC #2 CommanderCore` and began polling.

liquidctl's `_wake_device_context` sends WAKE before and SLEEP after **every**
operation (read from the installed 1.16.0 source: four call sites). SLEEP is
hardware mode — the rainbow — and both replies carry command byte `0x01`.
**hidraw has no exclusive open**: `sudo fuser` showed CoolerControl's liquidctl
helper and our daemon both holding `/dev/hidraw20`, each receiving the other's
replies — CoolerControl was logging `response does not match command` too.

| | |
|---|---|
| Fix | `liquidctl_integration = false` in `/etc/coolercontrol/config.toml`, daemon stopped while editing |
| Result | our daemon the only holder; 0 misses and 0 reopens, where the old rate predicted about six in the first 40 s |
| Cost | none — GPU fans go through NVML/nvapi, the PSU falls back to hwmon `corsairpsu`, as for the months before |

Fixed as a class, not only on this machine:

- **The error names the cause.** A `0x01` reply to anything but a wake/sleep is
  another process's reply; `CommanderCore._mismatch` now says so, lists the
  known culprits and points at `sudo fuser` — `sudo`, because an unprivileged
  daemon cannot see a root service's handles and would report no contender.
  Kept narrow: every other mismatch keeps the plain message. Mutation-checked.
- **The installer checks for it.** `install.sh` warns when CoolerControl's
  liquidctl integration is on, and distinguishes ACTIVE (liquidctl importable)
  from ARMED (inert until liquidctl appears). Armed is how this sat for months.
- **The binding is pinned.** `python-hidapi`, which the Commander driver
  imports, was held by pacman only as liquidctl's dependency, so removing
  liquidctl would have uninstalled it. The installer now marks it explicit, and
  it was marked on this machine.

The docs said "exactly one process may hold this device" and named only OpenRGB.
Both halves were incomplete: hidraw lets any number of processes hold it — the
rule is about *driving* it — and a second driver was waiting for a package.

## Deliberately not done

- **Custom colours and patterns** drive the bulbs only; the RGB goes black in
  those modes. Ambient-only was the agreed scope.
- **The webcam was not used to verify colours.** It points into the owner's room
  and they were asleep. The Commander Core's lighting endpoint is write-only, so
  its evidence is device ACKs, unchanged LED counts, byte-exact protocol tests
  and a known end state — not a photograph.

## Left running

`ambience-rgb-openrgb`, `ambience-rgb` and `matterlights-sync`/`-dashboard` are
enabled and active; `ambience-rgb-lights-off` is armed. The RGB follows the screen
while the lamps stay off, which is the state the owner left the lamps in.
