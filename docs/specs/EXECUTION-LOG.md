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

## 2026-09-10, evening: the Katana V2X

The soundbar is a sink now: `katana_protocol.py` (pure; byte-exact tests against
the reference's literal arrays and a captured auth pair), `katana.py` (the serial
transport, tested against a simulated soundbar that checks the unlock with real
AES-GCM), `KatanaSink`, a `[katana]` block, a `devices` line, a `check_hardware`
phase and the udev rule. 293 unit tests. Five mutation passes over the new code
killed 37 of 38 mutants; the survivor is below.

### First contact, through the production driver

The soundbar was in text mode, and met the binary probe with **complete
silence** — no `Unknown command`, nothing. Then, byte for byte:

| Sent | Came back |
|---|---|
| `whoareyou` | the challenge in 0.4 ms: header `1E 04`, PID `83 32`, 32-byte nonce, CR LF |
| `unlock` + 64 bytes | `unlock_OK` in 0.7 ms — the key and the GCM construction right first time |
| `SW_MODE1`, then the firmware query | `5a 09 12 02 10 "1.3.230619.1820\0"`, byte-identical to the published capture |
| the colour query | a 0.2 ms round trip; the solo colour read back `#047803` |
| a solo colour | **an acknowledgement**: `5a 02 0a 3a 00 2b 00 01 01 01 00 00 00` |

The unlock runs once per *soundbar* power cycle, so that was its one real test
until the soundbar is next switched off. Every start since takes the
binary-mode path.

The root shutdown path ran live as well, exactly as `ambience-rgb-lights-off`
runs it (`sudo env AMBIENCE_RGB_CONFIG=… ambience-rgb -v off`, daemon stopped):
the probe answered, the soundbar was prepared and blacked, and it exited 0 with
no warning. Root ignores the udev ACL, and the user daemon's `flock` and
`TIOCEXCL` are gone once it has stopped, so nothing stands in its way.

### Is every colour saved to flash?

The soundbar keeps its lighting across a power cycle, so something is saved.
Whether it is *every* write matters for a device written at the screen's pace.
A NOR sector erase blocks for tens of milliseconds, so a synchronous save would
show as a slower ACK to a changed colour, and a debounced one as a stall some
seconds later. One unit of red — invisible — was toggled:

| | median | max |
|---|---|---|
| ACK, colour unchanged (20) | 0.31 ms | 1.2 ms |
| ACK, colour changed (20) | 0.31 ms | 2.2 ms |
| ACK, 20 Hz burst (50) | 0.30 ms | 3.0 ms |
| query round trip, 15 s after the last change (297) | 0.34 ms | 3.6 ms |

No stall anywhere, every write acknowledged with status `00`. Evidence against
per-write saving, not proof — an asynchronous save would not show — so the sink
sends nothing it need not (a change filter, no refresh) and caps at 2 Hz until a
power-cycle test says whether colour writes persist at all.

### Traps the reference does not warn about

- 🚨 **pyserial's `exclusive=True` is an advisory `flock`, not exclusivity.** It
  stops only programs that ask for the same lock. `TIOCEXCL` is set as well, and
  with the daemon running a second `open()` of the tty fails `EBUSY` — verified
  live.
- 🚨 **`TAG+="uaccess"` does nothing in a `99-*.rules` file.** It is acted on
  only by `73-seat-late.rules`, so the rule is named `60-`. The reference's `99-`
  works only because it uses `MODE="0666"`. The check is `getfacl`, not the file.
- **The reference reads the challenge as a line**, and the nonce is binary: a
  nonce containing CR LF cuts it short, and only its NotYet retry papers over
  that. Ours reads by length.
- **The reference treats `FFFFFFFF` as an unused colour slot** — and solo white
  *is* `FF FF FF FF` in ABGR, so white would read back as no colour at all.
- **The reference's mode probe accepts any frame**, while noting itself that text
  mode sends status frames. Ours probes with the firmware query, whose answer an
  unrelated frame cannot impersonate.
- **The reference falls back to any `ttyACM` port** and sends it `whoareyou`.
  Ours matches USB IDs or refuses.
- **Setters are acknowledged, and nothing waits for the ACKs.** Unread, 13 bytes
  a write fill a tty buffer in minutes; every write takes in what is waiting.
- **The reference waits out `NotYet` with up to six 1 s sleeps** — right for a
  CLI, wrong in a daemon, where the sleep is inside the tick and freezes every
  other sink, the Commander Core included, exactly when the soundbar has just
  been switched on. Caught in review, the same class as the 1000 ms HID read
  timeout. The daemon's sink opens with `not_yet_retries=0`, so `NotYet` is a
  failure and `ResilientSink`'s retry is the wait; the one-shot tools keep the
  patient default.

### Found on the way, fixed as classes

- `commander.pump_group = "excluded"` was accepted and then **driven with the far
  colour** — the opposite of what it says. Devices written whole now refuse
  `excluded` and point at `enabled = false`.
- Four config helpers raised a bare `ValueError`, which the CLI does not catch:
  a typo in `saturation` printed a traceback. They raise `ConfigError` now, and a
  test covers each.
- The OpenRGB change filter compared signatures with `zip()`, which stops at the
  shorter one: **a colour joining the palette was no change**, and waited for the
  2 s refresh. The comparison is one shared `_moved()` now, used by both
  filtering sinks, and a different count is a change.
- `ambience-rgb devices` returned from inside the Commander branch while the
  daemon was up, so any device listed after it would never have been printed.
- 🚨 **Stopping the daemon could leave the AIO lit.** Caught in the journal of the
  restart that deployed the Katana, to the microsecond: SIGTERM at `.553151`; the
  Commander write in flight fails at `.553378`, because hidapi reports the
  interrupted `poll()` as `read error`; `RGB off; stopped` at `.553560`.
  Blacking a HID device takes milliseconds, and this took 0.18 ms because it
  never happened: the failure had marked the sink degraded, and
  `ResilientSink.black()` went through the same retry backoff as a tick, so the
  shutdown's black was skipped without a word. A plain `systemctl --user stop`
  or a logout left the AIO to drift back to its firmware rainbow; only the
  system shutdown unit covered for it. `black()` is called from exit paths only,
  so it now always gets its one attempt, and a failed one is always logged.
  **Confirmed live, as an A/B:** three stops that afternoon, and SIGTERM
  interrupted a Commander read in *every one* — so this was the normal case, not
  an unlucky one. The two on the old code went from the failure to `RGB off` in
  0.2 ms and 1.5 ms, blacking nothing. The one on the fixed code reopened the
  Commander, logged `commander-core: recovered`, and stopped 493 ms after
  SIGTERM with the black delivered.

### The mutant that survived

Removing the drain before `whoareyou` passes every test. The auth parser looks
for the challenge first, so stale bytes matter only if an old `Unknown command`
arrives ahead of a slow challenge — which then fails `SW_MODE1` and heals on the
next retry. Kept as hygiene, mirroring the reference; not worth a timing model in
the fake.

### Still open

- **Eyes.** Only the owner can say the light bar follows the room colour.
- **Audio.** Colour writes share the soundbar's USB link with playback. Nothing
  is known to glitch, and nobody has listened for it yet.
- **Persistence.** Set a distinctive colour, power the soundbar off and on, read
  it back. That settles the flash question, and runs the unlock a second time.

## 2026-09-10, night: the Windows port

Built from Linux in one session, for a half of the machine that boots *instead*
of this one. Every blocker in the research doc's §3 now has code behind it;
nothing has run on Windows yet. The README's *Windows: what is verified, and
what is not* is the checklist for the first boot there.

### Read the other half's disk before guessing about it

The Windows install sits on this machine's own NVMe, so it can be *read* from
Linux: mounted read-only at `/mnt/windows-c` (its `noauto` fstab entry), the
registry hives parsed offline with `regipy` in a scratch venv. That turned four
design guesses into facts:

- **Python there is 3.12.10**, where this side runs 3.14. The suite now also
  runs under a standalone 3.12 unpacked into a scratch directory -- which at once
  caught a test that passed only where `hid` happened to be importable.
- 🚨 **iCUE's `CorsairService` and `SignalRgb.Service` are set to Automatic.**
  Only the two programs' *launchers* had been taken out of autostart, so the
  record that nothing else would drive the Commander Core on Windows was half
  true. The installer reports both and, with `-DisableCompetitors`, disables them.
- **OpenRGB is not installed there**, which reshaped the OpenRGB step (below).
- **matterlights' checkout there is 12 commits behind**, with no `RGB_` lines.

### Decisions

- **UDP on `127.0.0.1:45731`** where there are no unix datagram sockets;
  `socket.transport = "auto"` picks it. Not `AF_UNIX` + `SOCK_STREAM`: a
  connection is exactly what a publisher must never be able to block on. The
  port sits below 49152 because Windows' dynamic range is where Hyper-V and WSL
  reserve blocks at boot. `SO_EXCLUSIVEADDRUSE`, so nothing shares the port.
- **Liveness is a bind probe for UDP and a connect probe for the socket file.**
  A UDP port accepts no connection and has no file. The probe holds the port for
  an instant; a daemon starting in exactly that instant says so and exits.
- **One stop command**, `{"command": "stop"}`, because Windows sends a process no
  signal and the shutdown task needed the daemon to let go of `COM3`. A stop is
  exit 0, so Task Scheduler never records it as a crash.
- **The task names are the unit names**, held equal by a test across the units,
  the PowerShell and the advice the program prints.
- **The scheduled tasks are XML built by pure PowerShell functions**, so a real
  pwsh -- a portable one from PowerShell's releases, unpacked in scratch -- renders
  and parses every task from Linux. The XML that registers is the XML tested.
- **No self-restart.** Task Scheduler's restart-on-failure covers a failure to
  *start*; an exit with an error code does not count. That rests on forum
  reports alone -- Microsoft's page for the setting never says what a failure
  is -- so the Windows checklist tests it. A repetition trigger would restart a
  crashed daemon -- and would also undo every deliberate stop within minutes,
  while the daemon already keeps every sink failure to itself
  (`ResilientSink._guard` catches everything). Documented instead.

### Traps, each turned into a test

- 🚨 **The shipped config crashed Windows at load.** Its socket path is
  `$XDG_RUNTIME_DIR/...`; with that unset -- as it always is there -- the fallback
  called `os.getuid()`, which Windows does not have. Every entry point, the
  shutdown task included, would have died before touching a light. The unix path
  is no longer read at all for UDP, `getuid` is looked up with `getattr`, and any
  unexpanded `$VAR` is a named error. The load-bearing test loads the real file
  for `win32` with `os.getuid` rigged to explode.
- 🚨 **`pythonw` has no stderr.** `sys.stderr` is `None`, and a stream handler on
  it drops every line in silence. Without a console the log goes to a rotating,
  timestamped file in `%LOCALAPPDATA%\ambience-rgb`.
- 🚨 **Task Scheduler stops a task 72 hours after its trigger started it**
  unless told otherwise -- Microsoft's `ITaskSettings::ExecutionTimeLimit` page,
  which also says a task started on demand is exempt, so an install that starts
  its tasks by hand never shows it. Both logon tasks carry `PT0S`, read back out
  of the rendered XML by a test. matterlights' own logon tasks did not; see
  *2026-09-10, late* below.
- **`IgnoreNew` drops a start issued while a stop is still in flight** -- the trap
  matterlights hit restarting its own tasks. The installer and the Windows
  OpenRGB restart command stop, wait for the task to leave Running (bounded),
  then start.
- **Windows reports a UDP send to a closed port on the NEXT send**
  (`WSAECONNRESET`), after which the socket is officially unusable. CPython's
  `socket.ioctl` cannot turn that off -- `SIO_UDP_CONNRESET` is not among the
  three controls it accepts -- so matterlights' publisher replaces its socket and
  counts a listener as back only after two clean sends in a row. Otherwise a
  stopped extension would flap the log twice a second.
- **`socket.AF_UNIX` does not exist on Windows CPython**, and matterlights'
  publisher caught only `OSError` around it: an `AttributeError` at the first
  publish would have taken the sync loop down. Tested against a socket module
  with the attribute really deleted; here an AST guard keeps `AF_UNIX` inside
  `transport.py`.
- **`WM_ENDSESSION` may be the last thing the process sees.** matterlights' hook
  returns at once because its callback is fire-and-forget; this one holds the
  message -- up to 4 s -- until the daemon's `finished` event says the black is
  written and every device released.
- **A console program started from `pythonw` gets a window of its own.** The
  OpenRGB restart runs PowerShell, so it passes `CREATE_NO_WINDOW` -- on Windows
  only, since POSIX rejects the flag.
- **Windows PowerShell 5.1 reads a script without a byte-order mark as the ANSI
  code page.** The scripts are plain ASCII, and a test holds them to it.
- **`ConvertTo-Json` flattens anything past depth 2 by default**, and
  `OpenRGB.json` is deeper, so both installers share one Python module for that
  edit instead of a PowerShell copy.
- **Windows hidapi lists one entry per HID collection and may not know the
  interface number.** The Commander Core is found by its usage, `0xFF42/0x0001`
  -- read off this rig's own descriptors -- with interface 0 only as the
  fallback for a usage nobody could read.

### Found on the way, fixed as classes

- 🚨 **A second daemon used to unlink a LIVE socket** and take the path over,
  leaving the first deaf and the two fighting over the Commander Core, which
  hidraw does nothing to protect. Binding now probes first and refuses -- before
  a single sink is built, because a refused daemon's exit path would otherwise
  black the case underneath the first one.
- **The Linux shutdown unit read the repo's example config**, whatever the daemon
  used. Root cannot search the user's config directory, so both installers now
  resolve the daemon's file and hand it over, and a test fails the build if a
  unit gains a placeholder the installer does not fill.
- **`openrgb.restart_command = "systemctl ..."` was accepted, and would have run
  letter by letter** -- `tuple()` of a string. A string is refused now, and the
  setting takes a per-platform table.
- **Broken TOML escaped the CLI as a traceback**, not a `ConfigError`.
- **Linux-only advice was printed on every platform**: three `systemctl` lines,
  now all from `hints.py`, with an AST guard over `src/` and the integration
  checks so the next one cannot be added anywhere else.
- **OpenRGB grabbing the Commander Core was silent.** The installers disable its
  detector by name; the sink and `devices` now also say so whenever OpenRGB lists
  a Commander Core while our own driver is on, in case another version names the
  detector differently.
- **OpenRGB never having run meant the detector edit was skipped**, which on
  Windows would have left the conflict waiting for OpenRGB's first run. The shared
  module creates the file with the two entries already off.

### Verified live on Linux

On the real hardware, with the systemd unit stopped: the daemon started by hand
on UDP with every sink connected; a second daemon refused with exit 1 while the
first kept listening; `devices` printing `RGB_PUBLISH_SOCKET=udp://127.0.0.1:45731`
and staying off the held devices; `check_policy.py` **10/10** through
matterlights' own publisher over UDP, staleness, recovery and the palette
gradient included; `off --stop-daemon` stopping it -- the daemon's own exit code
0, the port freed, and `off`'s black written only after the daemon had let go.
Then the unit went back on the unix socket and `install.sh` was re-run: the
shutdown unit loaded with the resolved config, and the detector step reported
"already disabled" without writing a backup. matterlights' sync was restarted on
the new publisher and delivers to the socket as before.

Tests: 447 here, green on Python 3.14 and 3.12; matterlights 272, seven of them
Windows-only and skipped on Linux. Every new behaviour was mutated to prove its
test can fail: **41 of 41 mutants killed** across both repositories.

### Still open -- it needs the Windows half

The README's list, in order. The two that matter most: whether iCUE's service
fights for the AIO even with iCUE's window closed (disable it either way), and a
real restart for the event-1074 task, which is the only layer that blacks the
case at a Windows shutdown.

## 2026-09-10, late: the suite on Windows CPython

A review of the port before it was reported found a test that would error on
Windows, and with it the question the port had skirted: nothing had ever run
with `sys.platform == "win32"`. Wine 11.15 was already installed here, so the
suite went onto the real Windows build of CPython -- python.org's embeddable
3.12.10, the dependencies as Windows wheels, a Wine prefix of its own, nothing
installed. That is `scripts/test-under-wine.sh` now.

### First, show it can see the class

Two failures were written down before the first run: the `$XDG_RUNTIME_DIR`
fallback test, which needs `os.getuid`, and its twin, a Linux load of the
shipped config inside a restart-command test. It went red on both -- and on a
third nobody predicted, a unix endpoint compared with a forward-slash string,
which on Windows prints with backslashes. All three were the tests'
assumptions, not the product's: Windows never parses a unix path (it is UDP
there) and never prints a unix endpoint. Each now tests the same thing on
either host, and the run is green: **455 tests, 20 skipped** -- the unix-socket
tests, the PowerShell ones (no pwsh inside Wine) and the hygiene sweep (no git).

One failure along the way was Wine's own. `Z:` is the live Linux tree, so a
POSIX runtime path -- relative on Windows, joined to the config's directory and
resolved -- landed on the running daemon's real socket: WinError 433. Real
Windows would have found nothing there. The test now hands the Linux load a
runtime directory that is absolute on whichever host runs it.

What it cannot vouch for: the UDP and session-end tests ran against Wine's
Winsock and `user32`. Their passing is a good sign, not proof, which is why the
suite on Windows itself is item 2 of the README's checklist.

### The code pages, off the Windows disk

ANSI 1252 and OEM 850, from `ControlSet001\Control\Nls\CodePage` in the SYSTEM
hive -- the pair Wine picked too. That settles what an unlabelled read does
there: cp1252 has no character for `81 8D 8F 90 9D`, and raises. `⚠️` ends in
`8F`.

### Every text-mode file and pipe names its encoding

`python -X warn_default_encoding` over both suites found nothing here and eight
sites in matterlights' tests. It sees only what a test executes, so an AST guard
now reads the source of both repositories -- `tests/test_text_encoding.py`,
identical in each, with cases proving it flags every shape and leaves
`device.open()` alone. Its first run found five more: the pwsh runner in this
suite and `check_failures.py`; in matterlights, two `systemctl` calls and -- the
one that mattered -- the dashboard's PowerShell poll, which decoded Windows
PowerShell's cp850 as cp1252. cp850 writes `ü` as `81`, and cp1252 raises on
it. That poll now decodes as `"oem"` and cannot raise.

### Also fixed

- **The repo-hygiene test ran nowhere.** Both repositories' copies were bare
  pytest functions, and the documented command, `unittest discover`, never
  collects those: zero tests ran from that file, and the suite said OK. They are
  `unittest.TestCase`s now, with a positive control proving each pattern still
  catches the file and the token shape that actually leaked.
- 🚨 **matterlights' logon tasks had the 72-hour limit.** Their stored XML on the
  Windows disk has no `ExecutionTimeLimit` at all -- the PT72H default -- because
  its `install-autostart.ps1` never set one, so a sync left running through three
  days logged in would have been stopped by the scheduler. The script sets
  `PT0S` now; the installed tasks keep the old limit until it is re-run, which
  is on the checklist.
  🚨 **And re-running that script, as first advised, would have undone a fix.**
  The Windows half's own memory records that the live tasks were moved to
  `pythonw.exe` by hand on 2026-07-19, because Windows Terminal ignores
  `-WindowStyle Hidden`, while the script went on registering
  `powershell -WindowStyle Hidden -File start-*.ps1`. Found by reading that
  memory before writing the Windows handoff. The script -- and the start
  scripts' background mode, the same class -- now launch `pythonw` directly,
  and a matterlights test fails if `-WindowStyle Hidden` or a PowerShell host
  comes back into any script's code.
- **"By its documentation" was an overclaim.** These notes said Task Scheduler's
  restart-on-failure ignores a non-zero exit by its documentation; Microsoft's
  page never defines a failure, and only forum reports say so. Reworded here, in
  the README and in `tasks.ps1`, and checklist item 10 tests it.

Tests: 455 here, green on Linux 3.14 and 3.12 and on Windows CPython under Wine;
matterlights 280 on Linux, and its RGB publisher tests on Windows CPython too.

## 2026-09-11: the first boot on Windows

Worked through from the handoff (`Desktop\ambience-rgb-windows-handoff.md` on
the Windows half) by an agent on the machine, the owner watching the case.
SignalRGB and iCUE were uninstalled and purged first, at the owner's request;
OpenRGB 1.0rc3.1 was installed; `install.ps1 -DisableCompetitors` registered the
three tasks; the suite ran green on the real Windows CPython, 455 tests with 11
skipped. The evidence for every step is in `Desktop\ambience-rgb-windows-results.md`
on that half. Then the owner looked at the case and found what no Linux or Wine
run could have shown.

### 🚨 The case ran behind the screen: a queue of absolute state, replayed

"The lights follow the screen correctly, but the rgb ... lag behind screen
changes like 2-3 minutes." Not stuck and not wrong: every colour arrived, in
order, late -- while the bulbs, fed by the same matterlights frames, kept up.

The daemon took ONE datagram per tick, the oldest waiting, and wrote every
device before taking the next. Harmless while a tick is shorter than
matterlights' frame interval (0.2 s); a steady leak otherwise. On Windows a tick
took 218 ms -- 4.44 a second against 5 frames, because of the Commander Core's
drain, fixed next. What bounds the leak is the socket's queue, and the two
halves differ by an order of magnitude:

- **Linux, unix datagram:** about ten datagrams, then the sender is refused --
  and Linux's ticks never fell behind anyway (an exchange takes 3 ms there).
- **Windows, UDP:** 1000 real-sized frames (358 bytes) sent to a socket that
  never reads -- it kept **184**, 65,872 bytes against the default 64 KB
  SO_RCVBUF, which ambience-rgb never changes, and dropped 816. Full, that is
  184 ticks behind: 41 s at the 218 ms the harness measured. The owner's
  estimate was minutes. The live daemon's tick before the fix was never
  measured, so whether its ticks ran slower than the harness's (one reply
  timeout alone is 250 ms) or the estimate was generous is open. The mechanism
  is not.

🚨 **Worse than lag.** A datagram arriving at a full UDP buffer is dropped
*without a trace*: netstat's UDP "Receive Errors" did not move for any of the
816. And the stop command is a datagram. `off --stop-daemon` at 02:05:13 was
still unanswered at 02:13:13, 480 s later; a queued stop is reached within 184
ticks, so it was almost certainly dropped. The shutdown task sends the same
datagram: at a real shutdown the case would have stayed lit and the daemon kept
the Katana's COM port.

**Fix:** `Daemon._receive` still waits for the first datagram, then takes
everything already queued without waiting -- at most 256, re-checking the stop
flag -- keeps only the newest frame, and acts on every command found on the way.
A frame is absolute state; once a newer one exists the older is worth nothing.
`tests/test_daemon_lifecycle.py::BacklogTest` builds a 40-frame backlog behind a
100 ms sink: before the fix, 38 stale frames were written ahead of the newest
and a queued stop took 3.9 s; after it, at most three, and the stop lands in
under half the replay time. Live, after this and the drain fix, `off
--stop-daemon` returned in 522 ms and the daemon's own black reached every sink.

**The class, swept:** this is the only consumer of frames in either repo that
reads a queue. matterlights' bulb worker already hands over through one
latest-wins slot (`light_worker._pending`, guarded by
`test_latest_wins_rather_than_queueing`) and its Linux capture keeps only
`_latest` -- which is why the bulbs never lagged.

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
while the lamps stay off, which is the state the owner left the lamps in. Since
the evening of 2026-09-10 the soundbar's light bar follows the far group too.
