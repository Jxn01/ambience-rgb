# Execution log — 2026-09-09

Built and verified in one session, against
[the design](2026-09-09-linux-rgb-design.md). What follows is what actually
happened, including the parts that went wrong.

## What shipped

| | |
|---|---|
| `linux-rgb` | 74 tests, 7 commits, private repo, pushed |
| `matterlights` | +2 commits (publisher, master-off publishing), 210 tests, pushed |
| LEDs driven | **207** across 7 controllers |
| Verified live | capture → bulbs → socket → OpenRGB + AIO, on real hardware |

## Verified on the machine

- **Frames flow at 5.1/s** from matterlights over the unix datagram socket.
- **Colours match end to end.** The colour read back from OpenRGB equals the
  colour in the published frame, tick for tick.
- **The per-zone split works.** Fed near=red / far=blue, the ASRock board's *IO
  Cover* and *PCB* read red while all four addressable headers read blue — from
  a single controller, exactly as configured.
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
