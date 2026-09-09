"""Ask a human whether the lights actually lit.

🚨 **This is the only check in the repository that can fail for the right
reason.** Everything else -- unit tests, the policy harness, every readback --
inspects software's opinion of the hardware. That opinion was wrong for a full
day: a mismatched HID report size truncated the Commander Core's colour payload
while leaving the command header intact, so every packet was acknowledged, LED
counts read back correctly, the topology looked right, and the device sat dark.
No amount of automated verification could see it. A person glancing at the case
saw it instantly.

So this drives a deliberate, memorable sequence and asks what you saw. Run it
whenever the transport, the packet framing, or the report size changes -- and
before believing any claim that the lights work.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from linux_rgb.commander_core import CommanderCore  # noqa: E402
from linux_rgb.config import load_config  # noqa: E402

HOLD_SECONDS = 6.0
SEQUENCE = [
    ("RED", (255, 0, 0)),
    ("GREEN", (0, 255, 0)),
    ("BLUE", (0, 0, 255)),
    ("WHITE", (255, 255, 255)),
]


def main() -> int:
    config = load_config()
    if not config.commander.enabled:
        print("commander.enabled is false; nothing to check")
        return 0

    print(
        "Watch the AIO pump and fans. Nothing else will be driving them.\n"
        "You should see each colour held steadily, in this order:\n"
    )
    for name, _colour in SEQUENCE:
        print(f"    {name} for {HOLD_SECONDS:.0f}s")
    print(
        "\nA rainbow or a fixed green means the firmware pattern has taken over,\n"
        "i.e. nothing of ours is reaching the LEDs. Starting in 3s.\n"
    )
    time.sleep(3)

    device = CommanderCore().open()
    print(f"open; pump {device.pump_leds} LEDs, ports {device.port_leds}\n")
    try:
        for name, colour in SEQUENCE:
            print(f"  holding {name} ...", flush=True)
            end = time.monotonic() + HOLD_SECONDS
            while time.monotonic() < end:
                device.set_colour(colour, colour)
                time.sleep(0.2)
    finally:
        # release(), not close(): close() writes black, which would make the end
        # of the sequence ambiguous with a failure to light at all.
        device.release()

    print(
        "\nDone. The AIO is now undriven and will return to its firmware pattern\n"
        "in about ten seconds -- seeing that is itself confirmation the device is\n"
        "alive and that we had control until we let go.\n"
    )
    answer = input("Did you see all four colours, in order? [y/N] ").strip().lower()
    if answer.startswith("y"):
        print("Confirmed by a human. That is the only evidence that counts here.")
        return 0
    print(
        "Not confirmed.\n"
        "First thing to check is the HID report size -- it is the failure that\n"
        "looks exactly like success from every software vantage point:\n"
        "    od -An -tx1 /sys/class/hidraw/hidraw20/device/report_descriptor\n"
        "    75 08 96 40 00  ->  Report Size 8 bits x Report Count 0x0040 = 64\n"
        "and compare with REPORT_LENGTH in src/linux_rgb/commander_protocol.py."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
