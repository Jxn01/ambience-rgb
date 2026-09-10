"""Stream for a while and check the rig is still following.

The unit tests run in milliseconds against fakes. Three of this project's worst
bugs only appeared under *sustained* streaming and were invisible in any shorter
test: the OpenRGB server falling behind until the case showed a colour from
minutes earlier, a per-zone write path that stalled the server after a frame or
two, and a read timeout long enough that one device's hiccup starved the other.

Defaults to five minutes. Give it longer when changing anything about rates,
timeouts or packet framing.

    python tests/integration/check_soak.py [seconds]
"""

from __future__ import annotations

import sys
import time

from _readback import REPO_ROOT, Reader

sys.path.insert(0, "/home/jxn/Projects/matterlights/src")
from matterlights.rgb_publish import AmbiencePublisher  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "src"))
from ambience_rgb.config import load_config  # noqa: E402

CHECK_EVERY = 30.0
HOLD = ((200, 30, 30), (30, 30, 200))


def main() -> int:
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 300.0
    config = load_config()
    reader = Reader(config.openrgb_host, config.openrgb_port)
    publisher = AmbiencePublisher(config.socket_path)

    targets = []
    for controller in reader.controllers:
        rule = config.rule_for(controller.name)
        if rule is None or rule.group == "excluded":
            continue
        if rule.zones:
            for zone_name, count in reader.device(controller.index)[3]:
                if count:
                    targets.append((controller.index, zone_name, rule.group_for_zone(zone_name)))
        else:
            targets.append((controller.index, None, rule.group))

    print(f"soaking {duration:.0f}s at 5 Hz over {len(targets)} targets", flush=True)
    start = time.monotonic()
    frames = checks = mismatches = 0
    last_check = 0.0

    def publish(near, far):
        publisher.publish(near, far, brightness=255, active_ratio=0.5, screen_dark=False,
                          display_on=True, lights_on=True, rgb_on=True)

    while time.monotonic() - start < duration:
        elapsed = time.monotonic() - start
        value = int(elapsed * 8) % 200 + 40
        publish((value, 0, 0), (0, 0, value))
        frames += 1

        if elapsed - last_check >= CHECK_EVERY:
            last_check = elapsed
            # Hold one colour long enough for the daemon to settle before reading.
            for _ in range(15):
                publish(*HOLD)
                time.sleep(0.2)
            cache: dict[int, dict] = {}
            bad = []
            for index, zone, group in targets:
                if index not in cache:
                    cache[index] = reader.zone_colours(index)
                    cache[index][None] = reader.colours(index)
                want = HOLD[0] if group == "near" else HOLD[1]
                if cache[index][zone] != [want]:
                    bad.append(f"dev{index} {zone or 'whole'}={cache[index][zone]}")
            checks += 1
            if bad:
                mismatches += 1
                print(f"  t+{elapsed:6.0f}s MISMATCH {'; '.join(bad)}", flush=True)
            else:
                print(f"  t+{elapsed:6.0f}s ok  frames={frames}", flush=True)
        time.sleep(0.2)

    reader.close()
    publisher.close()
    print(f"\n{frames} frames, {checks} checks, {mismatches} mismatches", flush=True)
    print(
        "Reminder: this proves the SERVER accepted every frame. It cannot prove a\n"
        "single LED lit -- run check_hardware.py for that.",
        flush=True,
    )
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
