"""Every decision the daemon makes, asserted against a live rig.

Publishes frames exactly as matterlights does -- same publisher class, same
socket, same payload -- and checks what the devices end up holding. Because the
colours are chosen here rather than sampled from a screen, each assertion is
exact, and every branch of ``Daemon.colours_for`` can be exercised on demand
including the ones that need the display asleep.

Run with the daemon running. See ``README.md`` for what this does and does not
prove.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from _readback import REPO_ROOT, Reader

sys.path.insert(0, "/home/jxn/Projects/matterlights/src")

from matterlights.rgb_publish import AmbiencePublisher  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "src"))
from ambience_rgb.config import load_config  # noqa: E402
from ambience_rgb.render import treat  # noqa: E402

NEAR = (255, 0, 0)
FAR = (0, 0, 255)


def _expectations(reader: Reader, config):
    """Work out, from the live config, what each device should show."""

    plan = []
    for controller in reader.controllers:
        rule = config.rule_for(controller.name)
        if rule is None or rule.group == "excluded":
            continue
        if rule.zones:
            for zone_name, count in reader.device(controller.index)[3]:
                if count:
                    plan.append((controller.index, zone_name, rule.group_for_zone(zone_name)))
        else:
            plan.append((controller.index, None, rule.group))
    return plan


def _observed(reader: Reader, plan):
    cache: dict[int, dict] = {}
    for index, zone, group in plan:
        if index not in cache:
            cache[index] = reader.zone_colours(index)
            cache[index][None] = reader.colours(index)
        yield index, zone, group, cache[index][zone]


def _publish(publisher, near, far, seconds=3.0, **flags):
    # Deliberately publishes NO palette. These cases are about the daemon's
    # decisions -- which group a device belongs to, and when everything must go
    # black -- and a gradient would make "the zone shows the far colour" a much
    # fuzzier assertion for no gain. The gradient has its own case at the end.
    options = dict(
        palette=[], brightness=255, active_ratio=0.5, screen_dark=False,
        display_on=True, lights_on=True, rgb_on=True,
    )
    options.update(flags)
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        publisher.publish(near, far, **options)
        time.sleep(0.2)


def main() -> int:
    config = load_config()
    reader = Reader(config.openrgb_host, config.openrgb_port)
    plan = _expectations(reader, config)
    statics = {
        controller.index: config.rule_for(controller.name).static
        for controller in reader.controllers
        if config.rule_for(controller.name) is not None
        and config.rule_for(controller.name).static
    }
    print(f"{len(plan)} write targets, {len(statics)} static device(s)\n")

    results: list[bool] = []

    def check(label, near, far, *, expect_black=False, **flags):
        publisher = AmbiencePublisher(config.socket_path)
        _publish(publisher, near, far, **flags)
        publisher.close()
        time.sleep(1.0)
        failures = []
        for index, zone, group, colours in _observed(reader, plan):
            base = (0, 0, 0) if expect_black else (near if group == "near" else far)
            # The LEDs are deliberately NOT sent the published colour: the sink
            # applies the configured saturation and gamma, because a bulb and a
            # strip showing identical numbers do not look alike. Expect what the
            # sink will actually write, or this fails on every mid-tone.
            want = treat(base, saturation=config.saturation, gamma=config.gamma)
            if colours != [tuple(want)]:
                failures.append(f"dev{index} {zone or 'whole'}: {colours} != [{tuple(want)}]")
        for index, colour in statics.items():
            if reader.colours(index) != [colour]:
                failures.append(f"dev{index} static drifted: {reader.colours(index)} != [{colour}]")
        print(f"  [{'PASS' if not failures else 'FAIL'}] {label}")
        for line in failures:
            print(f"          {line}")
        results.append(not failures)

    check("a normal frame lands near/far on the right devices", NEAR, FAR)
    check("a different frame tracks", (0, 200, 0), (200, 0, 200))
    check("the LAMP master switch does NOT silence the RGB", NEAR, FAR, lights_on=False)
    check("the RGB switch DOES silence it", NEAR, FAR, rgb_on=False, expect_black=True)
    check("a sleeping display blacks it", NEAR, FAR, display_on=False, expect_black=True)
    check("a dark screen blacks it", NEAR, FAR, screen_dark=True, expect_black=True)
    check("it recovers when frames resume", (255, 128, 0), (0, 128, 255))

    print("  [....] no frames for 6s (stale) ...")
    time.sleep(6.0)
    stale_ok = all(colours == [(0, 0, 0)] for _i, _z, _g, colours in _observed(reader, plan))
    print(f"  [{'PASS' if stale_ok else 'FAIL'}] a stale stream blacks it rather than freezing")
    results.append(stale_ok)

    check("and recovers again after staleness", (0, 255, 255), (255, 255, 0))

    # And the one case that needs a palette: a strip must show a gradient, not
    # one averaged tone. This is the seam that was silently disconnected once --
    # every layer correct, nothing joining them.
    publisher = AmbiencePublisher(config.socket_path)
    _publish(
        publisher, (255, 0, 0), (0, 0, 255),
        palette=[((255, 0, 0), 0.4), ((0, 0, 255), 0.35), ((0, 255, 0), 0.25)],
    )
    publisher.close()
    time.sleep(1.0)
    widest = 0
    for index, zone, _group in plan:
        colours = reader.zone_colours(index).get(zone) if zone else reader.colours(index)
        widest = max(widest, len(colours or []))
    spread_ok = widest > 3 or config.spread_bias >= 1.0
    print(
        f"  [{'PASS' if spread_ok else 'FAIL'}] a palette reaches the LEDs as a gradient"
        f"  (widest zone: {widest} distinct colours)"
    )
    results.append(spread_ok)

    reader.close()
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
