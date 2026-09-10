"""Break things under a live daemon and check it comes back.

Every scenario here has actually happened on this rig: the OpenRGB server
restarted underneath the daemon, the daemon killed outright, and a clean stop
that must leave nothing lit.

⚠️ **A recovery check must not be satisfiable by doing nothing.** The first
version of the SIGKILL case waited for the hardware to show the expected colour
-- which it already did, because a killed daemon leaves its last frame standing.
It passed instantly and proved nothing. Each check here now blanks the device
first, so only a real write can satisfy it.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time

from _readback import REPO_ROOT, Reader, expected_colour, use_matterlights

if sys.platform == "win32":
    raise SystemExit(
        "check_failures.py restarts, stops and kills the systemd units, so it is Linux-only. "
        "On Windows the services are scheduled tasks; check_policy.py, check_soak.py and "
        "check_hardware.py run there as they are."
    )

use_matterlights()
from matterlights.rgb_publish import AmbiencePublisher  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "src"))
from ambience_rgb.config import load_config  # noqa: E402
from ambience_rgb.openrgb_client import OpenRGBClient  # noqa: E402

NEAR, FAR = (255, 0, 0), (0, 0, 255)
CONFIG = load_config()
# The device this check watches. Index 0 is whatever OpenRGB enumerated first.
WATCHED = 0


def expected_on(index: int):
    """The colour ``index`` will hold while NEAR/FAR are being published.

    NOT ``NEAR``. Cohesion, saturation, gamma and channel order all sit between
    what is published and what the LEDs are sent -- see ``expected_colour``.
    Comparing against the published value made this check fail its own baseline
    against a daemon that was working correctly.
    """

    reader = Reader(CONFIG.openrgb_host, CONFIG.openrgb_port)
    try:
        rule = CONFIG.rule_for(reader.controllers[index].name)
    finally:
        reader.close()
    return expected_colour(CONFIG, NEAR, FAR, rule.group, rule.channel_order)
stop = threading.Event()
results: list[bool] = []


def pump() -> None:
    publisher = AmbiencePublisher(str(CONFIG.endpoint))
    while not stop.is_set():
        publisher.publish(NEAR, FAR, brightness=255, active_ratio=0.5, screen_dark=False,
                          display_on=True, lights_on=True, rgb_on=True)
        time.sleep(0.2)
    publisher.close()


def sh(*args: str) -> None:
    subprocess.run(args, check=False, capture_output=True)


def show(prop: str) -> str:
    return subprocess.run(
        ["systemctl", "--user", "show", "ambience-rgb", "-p", prop, "--value"],
        capture_output=True, encoding="utf-8",
    ).stdout.strip()


def read(index: int):
    reader = Reader(CONFIG.openrgb_host, CONFIG.openrgb_port)
    try:
        return reader.colours(index)
    finally:
        reader.close()


def wait_for(index, want, timeout):
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        try:
            if read(index) == [want]:
                return time.monotonic() - started
        except Exception:
            pass
        time.sleep(1.0)
    return None


def report(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}", flush=True)
    results.append(ok)


def blank(index: int) -> None:
    """Make the device show something no recovery would produce."""
    client = OpenRGBClient(CONFIG.openrgb_host, CONFIG.openrgb_port).connect()
    try:
        controller = next(c for c in client.controllers if c.index == index)
        client.update_leds(index, [(7, 7, 7)] * controller.led_count)
    finally:
        client.close()


def main() -> int:
    worker = threading.Thread(target=pump, daemon=True)
    worker.start()
    time.sleep(3)
    print("Failure injection against the live daemon\n", flush=True)

    want = expected_on(WATCHED)
    report("baseline: a driven device shows the near colour", read(WATCHED) == [want])

    print("  ...restarting the OpenRGB server underneath it", flush=True)
    blank(WATCHED)
    sh("systemctl", "--user", "restart", "ambience-rgb-openrgb.service")
    took = wait_for(WATCHED, want, 120)
    report("recovers after the OpenRGB server restarts", took is not None,
           f"({took:.0f}s)" if took else "(never)")

    pid, before = show("MainPID"), show("NRestarts")
    print(f"  ...SIGKILLing the daemon (pid {pid})", flush=True)
    sh("kill", "-9", pid)
    time.sleep(2)
    blank(WATCHED)
    took = wait_for(WATCHED, want, 120)
    report("systemd restarts the daemon and it drives again",
           took is not None and show("ActiveState") == "active",
           f"({took:.0f}s, NRestarts {before}->{show('NRestarts')})" if took else "(never)")

    print("  ...stopping the daemon cleanly", flush=True)
    stop.set()
    time.sleep(1.0)
    sh("systemctl", "--user", "stop", "ambience-rgb.service")
    time.sleep(4)
    driven = sorted({t[0] for t in _driven_indices()})
    blacks = {i: read(i) for i in driven}
    report("a clean stop leaves every driven device black",
           all(v == [(0, 0, 0)] for v in blacks.values()),
           str({k: v[0] for k, v in blacks.items()}))

    sh("systemctl", "--user", "start", "ambience-rgb.service")
    time.sleep(8)
    print(f"\n{sum(results)}/{len(results)} passed", flush=True)
    return 0 if all(results) else 1


def _driven_indices():
    reader = Reader(CONFIG.openrgb_host, CONFIG.openrgb_port)
    try:
        out = []
        for controller in reader.controllers:
            rule = CONFIG.rule_for(controller.name)
            if rule is not None and rule.group != "excluded" and rule.static is None:
                out.append((controller.index,))
        return out
    finally:
        reader.close()


if __name__ == "__main__":
    raise SystemExit(main())
