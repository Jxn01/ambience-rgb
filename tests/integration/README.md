# Integration checks

These need real hardware, a running OpenRGB server and (mostly) a running
daemon. They are **not** collected by `unittest discover`, which only picks up
`test*.py` — these are named `check_*.py` deliberately.

```bash
scripts/integration.sh            # policy + soak, the two safe ones
python tests/integration/check_policy.py
python tests/integration/check_soak.py 600
python tests/integration/check_failures.py    # restarts and kills services
python tests/integration/check_hardware.py    # asks you what you saw: AIO, then soundbar
```

## Why these exist

The unit tests are fast, pure and prove the logic. They cannot fail for the
reason that actually cost this project a day. Each of these was written after a
bug that no unit test could have caught:

| Check | The bug that motivated it |
|---|---|
| `check_policy.py` | Publishing the raw sampled colour instead of the colour the bulbs emit — correct in every unit test, invisible on the hardware |
| `check_soak.py` | The server falling minutes behind; the per-zone write path stalling it after a frame or two; a read timeout starving the other sink |
| `check_failures.py` | A dead socket reused forever, so recovery took 71 s; and the recovery path blacking the AIO it was rescuing |
| `check_hardware.py` | A wrong HID report size: every packet acknowledged, every readback healthy, every LED dark |

## 🚨 What a passing run does and does not mean

`check_policy.py`, `check_soak.py` and `check_failures.py` all read state back
from the **OpenRGB server's own model** of a device. That proves a packet
arrived and was accepted. It does **not** prove a single LED lit.

That distinction is not theoretical. For a full day this project had:

- every command acknowledged by the device,
- LED counts read back correctly,
- the zone topology exactly right,
- colours read back matching what was published, tick for tick,

while the Commander Core sat completely dark — because the HID report size was
96 bytes where the device's own descriptor declares 64. The four-byte command
header fits in either, so the acknowledgements were real. Only the colour
payload, which spans many packets, was being truncated.

**Only `check_hardware.py` can fail for that reason**, because it asks a person.
Run it whenever the transport, the packet framing or the report size changes, and
before believing any claim that the lights work.

It covers both devices this project drives with its own code: the Commander Core,
then the Katana soundbar. The soundbar phase writes each colour **once** -- the
device holds its colour, unlike the AIO -- prints what the soundbar reports back
alongside, and restores the colour it found. It refuses to run while the daemon
is up, because exactly one process may drive each of these devices.

## The first thing to check when the AIO is dark but everything looks healthy

```bash
od -An -tx1 /sys/class/hidraw/hidraw20/device/report_descriptor
#  75 08 96 40 00  ->  Report Size 8 bits x Report Count 0x0040 = 64 bytes
```

Compare with `REPORT_LENGTH` in `src/ambience_rgb/commander_protocol.py`. Ask the
device what shape it expects before auditing your implementation of the shape you
assumed.

## Notes

- `check_failures.py` restarts `ambience-rgb-openrgb`, SIGKILLs `ambience-rgb` and
  stops it. It puts everything back, but do not run it while you care about the
  lights holding steady.
- `check_policy.py` and `check_soak.py` publish frames themselves. If matterlights
  is also publishing (a screen is being captured), the two streams interleave and
  assertions become flaky — stop `matterlights-sync` first, or run them while the
  session is locked.
