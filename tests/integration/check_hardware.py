"""Ask a human whether the lights actually lit.

🚨 **This is the only check in the repository that can fail for the right
reason.** Everything else -- unit tests, the policy harness, every readback --
inspects software's opinion of the hardware. That opinion was wrong for a full
day: a mismatched HID report size truncated the Commander Core's colour payload
while leaving the command header intact, so every packet was acknowledged, LED
counts read back correctly, the topology looked right, and the device sat dark.
No amount of automated verification could see it. A person glancing at the case
saw it instantly.

So this drives a deliberate, memorable sequence on each device we drive with our
own code -- the Commander Core, then the Katana soundbar -- and asks what you
saw. Run it whenever a transport, the packet framing, or the report size
changes, and before believing any claim that the lights work. On Windows, run it
with the venv's python from the repository, like every other command there.

Stop the daemon first; this says how, on either platform. Exactly one process
may drive each of these devices, so it refuses to run while the daemon is up.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ambience_rgb import hints  # noqa: E402
from ambience_rgb.commander_core import CommanderCore  # noqa: E402
from ambience_rgb.config import load_config  # noqa: E402
from ambience_rgb.katana import Katana  # noqa: E402
from ambience_rgb.transport import is_listening  # noqa: E402

HOLD_SECONDS = 6.0
SEQUENCE = [
    ("RED", (255, 0, 0)),
    ("GREEN", (0, 255, 0)),
    ("BLUE", (0, 0, 255)),
    ("WHITE", (255, 255, 255)),
]


def _hex(colour: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % colour


def _ask(question: str) -> bool:
    return input(f"{question} [y/N] ").strip().lower().startswith("y")


def _announce(what: str) -> None:
    print(what)
    for name, _colour in SEQUENCE:
        print(f"    {name} for {HOLD_SECONDS:.0f}s")


def _report_size_advice() -> str:
    """How to ask the device its report size -- the first suspect -- on this OS."""

    if sys.platform == "win32":
        return (
            "look at its HID report descriptor, e.g. with USB Device Tree Viewer:\n"
            "    Report Size 8 bits x Report Count 0x40 = 64 bytes\n"
            "(Windows adds the report-ID byte: the output report length it states is 65)"
        )
    return (
        "ask the device:\n"
        "    od -An -tx1 /sys/class/hidraw/hidraw20/device/report_descriptor\n"
        "    75 08 96 40 00  ->  Report Size 8 bits x Report Count 0x0040 = 64"
    )


def check_commander(config) -> bool | None:
    if not config.commander.enabled:
        print("commander.enabled is false; skipping the Commander Core\n")
        return None

    _announce(
        "Watch the AIO pump and fans. Nothing else will be driving them.\n"
        "You should see each colour held steadily, in this order:\n"
    )
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
    if _ask("Did you see all four colours on the AIO, in order?"):
        return True
    print(
        "Not confirmed.\n"
        "First thing to check is the HID report size -- it is the failure that\n"
        "looks exactly like success from every software vantage point. To see\n"
        f"what the device expects, {_report_size_advice()}\n"
        "and compare with REPORT_LENGTH in src/ambience_rgb/commander_protocol.py.\n"
        "Then make sure nothing else drives it: OpenRGB's Commander Core detector,\n"
        "CoolerControl's liquidctl on Linux, iCUE's Corsair Service on Windows.\n"
    )
    return False


def check_katana(config) -> bool | None:
    if not config.katana.enabled:
        print("katana.enabled is false; skipping the soundbar\n")
        return None

    _announce(
        "Watch the soundbar's light bar. Each colour is written ONCE and must hold\n"
        "on its own for the whole period -- unlike the Commander Core, the soundbar\n"
        "keeps its colour. In this order:\n"
    )
    print("\nStarting in 3s.\n")
    time.sleep(3)

    device = Katana(config.katana.port).open()
    print(f"open; {device.model} on {device.path}, firmware {device.firmware}")
    before = device.read_colour()
    print(f"its colour before the check: {_hex(before)}\n")
    try:
        device.prepare()
        for name, colour in SEQUENCE:
            device.set_colour(colour)
            # Software's opinion, printed so a mismatch between it and your eyes
            # is visible -- but it is not the evidence. Your eyes are.
            print(f"  holding {name}; the soundbar reports {_hex(device.read_colour())}", flush=True)
            time.sleep(HOLD_SECONDS)
        # The soundbar acknowledges every write (5A 02 0A 3A 00 ...), so about
        # one per colour set is normal -- more than that is worth a look.
        print(f"\n  ({device.unsolicited} frames arrived unasked; the soundbar ACKs every write)")
    finally:
        # Put back what it showed before, then let go WITHOUT blacking it.
        try:
            device.set_colour(before)
        finally:
            device.release()

    print(f"\nDone; the soundbar is back to {_hex(before)}.\n")
    if _ask("Did you see all four colours on the soundbar, in order?"):
        return True
    print(
        "Not confirmed.\n"
        "If the soundbar REPORTED each colour but did not show it, the protocol\n"
        "reached it and the lights did not follow: check its lighting is switched\n"
        "on at the device, and that nothing else (Creative's app) is driving it.\n"
    )
    return False


def main() -> int:
    config = load_config()
    if is_listening(config.endpoint):
        print(
            "ambience-rgb is running, and it drives these devices. Stop it first:\n"
            f"    {hints.stop_daemon()}\n"
            "and start it again afterwards:\n"
            f"    {hints.start_daemon()}"
        )
        return 2

    answers = [check_commander(config), check_katana(config)]
    asked = [answer for answer in answers if answer is not None]
    if not asked:
        print("Nothing to check.")
        return 0
    if all(asked):
        print("Confirmed by a human. That is the only evidence that counts here.")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
