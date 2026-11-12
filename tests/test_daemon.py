"""What colour the daemon decides, and what happens when a sink misbehaves.

``colours_for`` is where every policy decision lands, so it is a pure static
method and every branch is asserted here rather than inferred from a running
daemon and a look at the case.
"""

from __future__ import annotations

import logging
import unittest

from ambience_rgb.daemon import BLACK, Daemon
from ambience_rgb.sinks import RETRY_SECONDS, ResilientSink

FRAME = {
    "v": 1,
    "near": [10, 20, 30],
    "far": [40, 50, 60],
    "brightness": 128,
    "active_ratio": 0.5,
    "screen_dark": False,
    "display_on": True,
    "lights_on": True,
    "rgb_on": True,
}
NEAR = (10, 20, 30)
FAR = (40, 50, 60)


class ColourDecisionTest(unittest.TestCase):
    def test_a_normal_frame_is_mirrored(self) -> None:
        self.assertEqual(Daemon.colours_for(FRAME, stale=False), (NEAR, FAR))

    def test_the_lamp_master_switch_does_NOT_silence_the_rgb(self) -> None:
        """The RGB has its own switch.

        Turning the bulbs off at night must not also kill the case lighting.
        This is the one flag in the payload that is carried but not obeyed.
        """

        self.assertEqual(
            Daemon.colours_for({**FRAME, "lights_on": False}, stale=False), (NEAR, FAR)
        )

    def test_the_rgb_switch_does_silence_it(self) -> None:
        self.assertEqual(
            Daemon.colours_for({**FRAME, "rgb_on": False}, stale=False), (BLACK, BLACK)
        )

    def test_a_sleeping_display_blacks_it(self) -> None:
        self.assertEqual(
            Daemon.colours_for({**FRAME, "display_on": False}, stale=False), (BLACK, BLACK)
        )

    def test_a_dark_screen_blacks_it(self) -> None:
        """Mirrors the bulbs, using matterlights' verdict rather than our own."""

        self.assertEqual(
            Daemon.colours_for({**FRAME, "screen_dark": True}, stale=False), (BLACK, BLACK)
        )

    def test_no_frame_blacks_it(self) -> None:
        self.assertEqual(Daemon.colours_for(None, stale=False), (BLACK, BLACK))

    def test_a_stale_frame_blacks_it(self) -> None:
        """A frozen colour looks exactly like working sync. Go dark instead."""

        self.assertEqual(Daemon.colours_for(FRAME, stale=True), (BLACK, BLACK))

    def test_a_malformed_colour_blacks_rather_than_raising(self) -> None:
        for bad in ([1, 2], [], "red", None, [1, 2, 3, 4, 5]):
            with self.subTest(near=bad):
                result = Daemon.colours_for({**FRAME, "near": bad}, stale=False)
                self.assertEqual(len(result), 2)
                for colour in result:
                    self.assertEqual(len(colour), 3)


class _Sink:
    name = "fake"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.writes: list[tuple] = []
        self.blacks = 0
        self.closed = False

    def write(self, near, far, palette=None) -> None:
        if self.fail:
            raise RuntimeError("device fell off the bus")
        self.writes.append((near, far))

    def release(self) -> None:
        self.released = True

    def black(self) -> None:
        if self.fail:
            raise RuntimeError("device fell off the bus")
        self.blacks += 1

    def close(self) -> None:
        self.closed = True


class SinkIsolationTest(unittest.TestCase):
    """One failing sink must not stop the others, or the daemon."""

    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_a_failing_sink_does_not_raise(self) -> None:
        sink = ResilientSink(_Sink(fail=True))
        self.assertFalse(sink.write(NEAR, FAR))
        self.assertTrue(sink.degraded)

    def test_a_failing_sink_backs_off_rather_than_spinning(self) -> None:
        inner = _Sink(fail=True)
        sink = ResilientSink(inner)
        sink.write(NEAR, FAR)
        inner.fail = False
        # Still inside the retry window, so it should not even be attempted.
        self.assertFalse(sink.write(NEAR, FAR))
        self.assertEqual(inner.writes, [])

    def test_it_recovers_once_the_window_passes(self) -> None:
        inner = _Sink(fail=True)
        sink = ResilientSink(inner)
        sink.write(NEAR, FAR)
        inner.fail = False
        sink._failed_at -= RETRY_SECONDS + 1  # pretend the window elapsed
        self.assertTrue(sink.write(NEAR, FAR))
        self.assertFalse(sink.degraded)
        self.assertEqual(inner.writes, [(NEAR, FAR)])

    def test_closing_a_broken_sink_does_not_raise(self) -> None:
        class Exploding(_Sink):
            def close(self) -> None:
                raise RuntimeError("nope")

        ResilientSink(Exploding()).close()

    def test_a_healthy_sink_is_untouched_by_a_broken_neighbour(self) -> None:
        good, bad = _Sink(), _Sink(fail=True)
        sinks = [ResilientSink(good), ResilientSink(bad)]
        for sink in sinks:
            sink.write(NEAR, FAR)
        self.assertEqual(good.writes, [(NEAR, FAR)])



class OpenRgbEmptyPlanTest(unittest.TestCase):
    """An empty controller list must not be cached as 'drive nothing'.

    The OpenRGB server binds its socket before device detection finishes, so a
    daemon that starts alongside it can connect to a server that knows about no
    devices yet. Treating that as success would leave the whole case dark until
    someone restarted the daemon by hand.
    """

    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_connect_raises_so_the_wrapper_retries(self) -> None:
        from ambience_rgb.config import parse_config
        from ambience_rgb.openrgb_client import OpenRGBError
        from ambience_rgb.sinks import OpenRGBSink
        import ambience_rgb.sinks as module

        class EmptyClient:
            controllers: list = []

            def __init__(self, *a, **k) -> None:
                pass

            def connect(self):
                return self

            def close(self) -> None:
                self.closed = True

        original = module.OpenRGBClient
        module.OpenRGBClient = EmptyClient
        try:
            sink = OpenRGBSink(parse_config({"socket": {"path": "/tmp/x.sock"}}))
            with self.assertRaises(OpenRGBError) as caught:
                sink.connect()
            self.assertIn("detection", str(caught.exception).lower())
        finally:
            module.OpenRGBClient = original


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class MalformedColourTest(unittest.TestCase):
    """A bad frame must be ignored, not passed on to the hardware.

    Checking the length alone is not enough: ``tuple("red")[:3]`` is
    ``('r', 'e', 'd')`` -- three items, passes a length check, and then fails
    inside the sink when the channels are packed. That degrades the sink on
    every tick, so one malformed frame takes the lights down rather than being
    skipped.
    """

    def test_strings_are_rejected_rather_than_split_into_characters(self) -> None:
        self.assertEqual(Daemon.colours_for({**FRAME, "near": "red"}, stale=False), (BLACK, BLACK))

    def test_every_channel_must_be_a_real_integer(self) -> None:
        for bad in ("red", ["r", "g", "b"], [1, 2, "3"], [1.5, 2, 3], [None, 0, 0], {"r": 1}):
            with self.subTest(near=bad):
                self.assertEqual(
                    Daemon.colours_for({**FRAME, "near": bad}, stale=False), (BLACK, BLACK)
                )

    def test_out_of_range_channels_are_clamped_not_rejected(self) -> None:
        near, _far = Daemon.colours_for({**FRAME, "near": [-5, 300, 128]}, stale=False)
        self.assertEqual(near, (0, 255, 128))

    def test_a_good_frame_still_passes_through_untouched(self) -> None:
        self.assertEqual(Daemon.colours_for(FRAME, stale=False), (NEAR, FAR))

    def test_every_result_is_three_integers(self) -> None:
        for bad in ([1, 2], [], "red", None, [1, 2, 3, 4, 5], [1, 2, "3"]):
            with self.subTest(near=bad):
                for colour in Daemon.colours_for({**FRAME, "near": bad}, stale=False):
                    self.assertEqual(len(colour), 3)
                    self.assertTrue(all(isinstance(c, int) for c in colour))


class StaleThresholdTest(unittest.TestCase):
    """The staleness window must clear the publisher's worst blocking call.

    matterlights' Home Assistant timeout is 3.0 s, so a bulb that stops
    answering blocks its loop for that long; a staleness window of 3.0 s would
    blank the entire case on every bulb timeout.

    A publisher stalled on a slow network call is not a dead publisher.
    """

    def test_it_clears_several_home_assistant_timeouts(self) -> None:
        from ambience_rgb.daemon import STALE_AFTER_SECONDS

        ha_timeout = 3.0
        self.assertGreater(
            STALE_AFTER_SECONDS, ha_timeout * 3,
            "must survive three sequential bulb timeouts without blanking",
        )

    def test_it_still_notices_a_genuinely_dead_publisher(self) -> None:
        """Holding a frozen colour forever is the failure this guards against."""

        from ambience_rgb.daemon import STALE_AFTER_SECONDS

        self.assertLessEqual(STALE_AFTER_SECONDS, 15.0)

    def test_a_stale_frame_still_blacks(self) -> None:
        self.assertEqual(Daemon.colours_for(FRAME, stale=True), (BLACK, BLACK))
