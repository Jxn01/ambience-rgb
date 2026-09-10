"""The Katana sink: what reaches the soundbar, and how often.

Two facts about this device shape every test here. It HOLDS its colour -- unlike
the Commander Core it never reverts -- so nothing ever needs re-sending. And
whether each colour write is also saved to its flash is not known, so nothing is
sent that does not have to be: a change filter, a rate ceiling of its own, and
no periodic refresh.
"""

from __future__ import annotations

import logging
import unittest

from ambience_rgb.config import parse_config
from ambience_rgb.katana import KatanaError
from ambience_rgb.render import treat
from ambience_rgb.sinks import COLOUR_EPSILON, KATANA_KEEPALIVE_SECONDS, KatanaSink, build_sinks

NEAR = (10, 20, 30)
FAR = (40, 50, 60)
BLACK = (0, 0, 0)


class _Clock:
    def __init__(self) -> None:
        self.now = 500.0

    def __call__(self) -> float:
        return self.now


class _FakeKatana:
    model = "Sound Blaster Katana V2X"
    firmware = "1.3.230619.1820"
    path = "/dev/fake"

    def __init__(self) -> None:
        self.colours: list[tuple[int, int, int]] = []
        self.prepared = 0
        self.keepalives = 0
        self.released = 0
        self.closed = 0
        self.fail_keepalive = False

    def prepare(self) -> None:
        self.prepared += 1

    def set_colour(self, colour) -> None:
        self.colours.append(tuple(colour))

    def black(self) -> None:
        self.colours.append(BLACK)

    def keepalive(self) -> None:
        self.keepalives += 1
        if self.fail_keepalive:
            raise KatanaError("The soundbar stopped answering")

    def release(self) -> None:
        self.released += 1

    def close(self) -> None:
        self.closed += 1


def _sink(*, bias: float = 1.0, treatment: dict | None = None, **section):
    config = parse_config(
        {"socket": {"path": "/tmp/x.sock"}, "katana": {"enabled": True, **section}}
    )
    clock = _Clock()
    device = _FakeKatana()
    sink = KatanaSink(
        config.katana, logging.getLogger("test"), bias, treatment,
        device_factory=lambda: device, clock=clock,
    )
    return sink, device, clock


class RoutingTest(unittest.TestCase):
    def test_connecting_prepares_the_soundbar_once(self) -> None:
        sink, device, clock = _sink()
        sink.write(NEAR, FAR)
        clock.now += 1
        sink.write(NEAR, (90, 90, 90))
        self.assertEqual(device.prepared, 1)

    def test_a_far_soundbar_shows_the_far_colour(self) -> None:
        sink, device, _clock = _sink()
        sink.write(NEAR, FAR)
        self.assertEqual(device.colours, [FAR])

    def test_a_near_soundbar_shows_the_near_colour(self) -> None:
        sink, device, _clock = _sink(group="near")
        sink.write(NEAR, FAR)
        self.assertEqual(device.colours, [NEAR])

    def test_saturation_and_gamma_reach_the_soundbar(self) -> None:
        # The stage the integration checks model as cohere -> treat -> channel
        # order. The protocol does the soundbar's byte order itself, so here the
        # colour on the wire must be exactly the treated one.
        treatment = {"saturation": 1.6, "gamma": 1.4}
        sink, device, _clock = _sink(treatment=treatment)
        sink.write(NEAR, FAR)
        self.assertEqual(device.colours, [treat(FAR, **treatment)])
        self.assertNotEqual(device.colours, [FAR])

    def test_the_palette_is_used_when_the_bias_allows_it(self) -> None:
        sink, device, _clock = _sink(bias=0.0)
        sink.write(NEAR, FAR, [((255, 0, 0), 1.0)])
        self.assertEqual(device.colours, [(255, 0, 0)])


class WritesAsLittleAsPossibleTest(unittest.TestCase):
    def test_an_unchanged_colour_is_never_resent(self) -> None:
        # No refresh, deliberately: the soundbar holds its colour, and a write
        # that may reach flash is not free.
        sink, device, clock = _sink()
        sink.write(NEAR, FAR)
        for _ in range(10):
            clock.now += 60
            sink.write(NEAR, FAR)
        self.assertEqual(device.colours, [FAR])

    def test_a_jitter_below_the_epsilon_is_ignored(self) -> None:
        sink, device, clock = _sink()
        sink.write(NEAR, FAR)
        clock.now += 1
        sink.write(NEAR, tuple(channel + COLOUR_EPSILON - 1 for channel in FAR))
        self.assertEqual(device.colours, [FAR])

    def test_a_move_of_the_epsilon_is_sent(self) -> None:
        moved = tuple(channel + COLOUR_EPSILON for channel in FAR)
        sink, device, clock = _sink()
        sink.write(NEAR, FAR)
        clock.now += 1
        sink.write(NEAR, moved)
        self.assertEqual(device.colours, [FAR, moved])

    def test_slow_drift_is_measured_from_what_was_sent(self) -> None:
        # One unit a tick never crosses the epsilon tick-to-tick; measured from
        # the last colour actually SENT, it does.
        sink, device, clock = _sink()
        sink.write(NEAR, FAR)
        for step in range(1, COLOUR_EPSILON + 1):
            clock.now += 1
            sink.write(NEAR, (FAR[0] + step, FAR[1], FAR[2]))
        self.assertEqual(len(device.colours), 2)

    def test_the_rate_ceiling_holds_a_change_back_then_sends_the_latest(self) -> None:
        sink, device, clock = _sink(rate_hz=2)  # one write per 0.5 s
        sink.write(NEAR, FAR)
        clock.now += 0.1
        sink.write(NEAR, (100, 0, 0))
        clock.now += 0.1
        sink.write(NEAR, (0, 100, 0))
        self.assertEqual(device.colours, [FAR])
        clock.now += 0.4
        sink.write(NEAR, (0, 100, 0))
        self.assertEqual(device.colours, [FAR, (0, 100, 0)])

    def test_a_filtered_tick_does_not_use_up_the_rate(self) -> None:
        sink, device, clock = _sink(rate_hz=2)
        sink.write(NEAR, FAR)
        clock.now += 0.6
        sink.write(NEAR, FAR)  # unchanged: nothing sent
        clock.now += 0.1
        sink.write(NEAR, (0, 100, 0))
        self.assertEqual(device.colours, [FAR, (0, 100, 0)])


class KeepaliveTest(unittest.TestCase):
    def test_it_is_not_asked_every_tick(self) -> None:
        sink, device, clock = _sink()
        for _ in range(20):
            clock.now += 0.1
            sink.write(NEAR, FAR)
        self.assertEqual(device.keepalives, 0)

    def test_it_runs_on_its_own_timer_even_on_a_static_screen(self) -> None:
        # The change filter must not starve it: a static screen is exactly when
        # nothing else would notice the soundbar has stopped listening.
        sink, device, clock = _sink()
        sink.write(NEAR, FAR)
        clock.now += KATANA_KEEPALIVE_SECONDS
        sink.write(NEAR, FAR)
        self.assertEqual(device.keepalives, 1)

    def test_a_failed_keepalive_lets_go_without_blacking(self) -> None:
        sink, device, clock = _sink()
        sink.write(NEAR, FAR)
        device.fail_keepalive = True
        clock.now += KATANA_KEEPALIVE_SECONDS
        with self.assertRaises(KatanaError):
            sink.write(NEAR, FAR)
        self.assertEqual((device.released, device.closed), (1, 0))
        self.assertEqual(device.colours, [FAR], "a recovery path must not darken the soundbar")

    def test_the_colour_is_sent_again_after_a_reconnect(self) -> None:
        sink, device, clock = _sink()
        sink.write(NEAR, FAR)
        device.fail_keepalive = True
        clock.now += KATANA_KEEPALIVE_SECONDS
        with self.assertRaises(KatanaError):
            sink.write(NEAR, FAR)
        device.fail_keepalive = False
        clock.now += 1
        sink.write(NEAR, FAR)
        self.assertEqual(device.prepared, 2)
        self.assertEqual(device.colours, [FAR, FAR])


class BlackTest(unittest.TestCase):
    def test_black_is_sent_even_when_already_black(self) -> None:
        sink, device, _clock = _sink()
        sink.black()
        sink.black()
        self.assertEqual(device.colours, [BLACK, BLACK])

    def test_a_colour_after_black_is_sent_again(self) -> None:
        sink, device, clock = _sink()
        sink.write(NEAR, FAR)
        sink.black()
        clock.now += 1
        sink.write(NEAR, FAR)
        self.assertEqual(device.colours, [FAR, BLACK, FAR])


class NeverSleepsInTheTickTest(unittest.TestCase):
    def test_the_daemon_opens_the_soundbar_without_notyet_retries(self) -> None:
        # 🚨 A NotYet retry sleeps a second at a time, up to six times, and in
        # the daemon that sleep is inside the tick: every other sink freezes
        # with it, the Commander Core included -- which reverts to its firmware
        # pattern after ten seconds unwritten. So the daemon's sink takes NotYet
        # as a failure, and ResilientSink's retry is the wait.
        from unittest import mock

        seen: dict = {}

        class Recorder:
            def __init__(self, *args, **kwargs) -> None:
                seen.update(kwargs)

            def open(self):
                raise KatanaError("stop here")

        config = parse_config({"socket": {"path": "/tmp/x.sock"}, "katana": {"enabled": True}})
        sink = KatanaSink(config.katana, logging.getLogger("test"))
        with mock.patch("ambience_rgb.katana.Katana", Recorder):
            with self.assertRaises(KatanaError):
                sink.connect()
        self.assertEqual(seen.get("not_yet_retries"), 0)


class BuildSinksTest(unittest.TestCase):
    def test_no_katana_sink_unless_enabled(self) -> None:
        config = parse_config({"socket": {"path": "/tmp/x.sock"}})
        self.assertNotIn("katana", [sink.name for sink in build_sinks(config)])

    def test_an_enabled_katana_gets_a_sink(self) -> None:
        config = parse_config({"socket": {"path": "/tmp/x.sock"}, "katana": {"enabled": True}})
        self.assertIn("katana", [sink.name for sink in build_sinks(config)])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
