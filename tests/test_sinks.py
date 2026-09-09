"""How much the OpenRGB sink actually puts on the wire.

The write side of this protocol is fire-and-forget: ``sendall`` returns in
microseconds no matter what the server is doing with the bytes. So overrunning
the server is invisible from the sending end, and the only place the limit can
be enforced is here.
"""

from __future__ import annotations

import logging
import unittest

from linux_rgb.config import parse_config
from linux_rgb.openrgb_client import OpenRGBError
from linux_rgb.sinks import (
    COLOUR_EPSILON,
    HEALTH_CHECK_SECONDS,
    HEALTH_FAILURES_BEFORE_RECONNECT,
    REFRESH_SECONDS,
    OpenRGBSink,
)

NEAR = (10, 20, 30)
FAR = (40, 50, 60)


class _FakeClient:
    def __init__(self, lag: float = 0.0, answers: bool = True) -> None:
        self.writes: list[tuple[int, int | None, tuple[int, int, int], int]] = []
        self.lag = lag
        self.answers = answers
        self.probes = 0
        self.closed = False

    def round_trip_seconds(self, timeout: float) -> float:
        self.probes += 1
        if not self.answers:
            raise OpenRGBError("no answer")
        return self.lag

    def update_leds(self, device, colours) -> None:
        self.writes.append((device, None, tuple(colours)))

    def update_zone_leds(self, device, zone, colours) -> None:
        self.writes.append((device, zone, tuple(colours)))

    def close(self) -> None:
        self.closed = True


def _sink(targets) -> tuple[OpenRGBSink, _FakeClient]:
    sink = OpenRGBSink(parse_config({"socket": {"path": "/tmp/x.sock"}}))
    # Disable the per-bus limiter outright so these tests measure the change
    # filter alone. Merely configuring a high rate is not enough -- two calls in
    # a row can land inside any non-zero interval, which silently turns every
    # assertion here into a test of the clock. The limiter is a separate ceiling
    # with its own test below.
    sink._interval = lambda _bus: 0.0  # type: ignore[method-assign]
    sink._check_health = lambda _now: None  # type: ignore[method-assign]
    client = _FakeClient()
    sink._client = client
    sink._targets = targets
    return sink, client


# (device, segments, bus, per_zone)
_ONE = [(6, (("near", 2),), "usb", False)]
# One controller split across three zones -- written as ONE packet whose array
# carries each zone's colour in order.
_BOARD = [(6, (("far", 1), ("far", 2), ("near", 2)), "usb", False)]
# The excluded-zone fallback, which must still go zone by zone.
_PER_ZONE = [
    (6, ((0, "far", 1),), "usb", True),
    (6, ((3, "near", 2),), "usb", True),
]


class ChangeFilterTest(unittest.TestCase):
    """🚨 Regression guard: an unchanged colour must not be resent every tick.

    Writing all eleven targets on every tick offered the server ~43 packets a
    second. It applies them one at a time against real USB and SMBus devices,
    far slower than that, so the backlog grew without bound and the board
    displayed a colour from minutes earlier. Across a display-off period it went
    on displaying black long after the screen came back -- which reads exactly
    like "the RGB is dead", and did.
    """

    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_the_first_write_is_sent(self) -> None:
        sink, client = _sink(_ONE)
        sink.write(NEAR, FAR)
        self.assertEqual(len(client.writes), 1)

    def test_an_unchanged_colour_is_not_resent(self) -> None:
        sink, client = _sink(_ONE)
        for _ in range(20):
            sink.write(NEAR, FAR)
        self.assertEqual(len(client.writes), 1, "20 identical ticks must cost one packet")

    def test_a_changed_colour_is_sent(self) -> None:
        sink, client = _sink(_ONE)
        sink.write(NEAR, FAR)
        sink.write((99, 99, 99), FAR)
        self.assertEqual([w[2][0] for w in client.writes], [NEAR, (99, 99, 99)])

    def test_a_jitter_smaller_than_the_epsilon_is_ignored(self) -> None:
        """🚨 Exact equality is not enough: the ambient colour moves a unit or
        two every frame, which resent all eleven targets every tick and overran
        the server just as badly as no filter at all."""

        sink, client = _sink(_ONE)
        sink.write(NEAR, FAR)
        nudged = tuple(c + COLOUR_EPSILON - 1 for c in NEAR)
        sink.write(nudged, FAR)
        self.assertEqual(len(client.writes), 1)

    def test_a_move_of_the_epsilon_is_sent(self) -> None:
        sink, client = _sink(_ONE)
        sink.write(NEAR, FAR)
        moved = (NEAR[0] + COLOUR_EPSILON, NEAR[1], NEAR[2])
        sink.write(moved, FAR)
        self.assertEqual(len(client.writes), 2)

    def test_it_refreshes_after_the_window(self) -> None:
        """A re-detected controller must not be left sitting on a stale frame."""

        sink, client = _sink(_ONE)
        sink.write(NEAR, FAR)
        key = (6, None)
        colours, when = sink._sent[key]
        sink._sent[key] = (colours, when - REFRESH_SECONDS - 1)
        sink.write(NEAR, FAR)
        self.assertEqual(len(client.writes), 2)

    def test_a_split_controller_is_one_packet_carrying_every_zone(self) -> None:
        """🚨 Six zone writes stalled OpenRGB's ASRock controller outright; the
        same colours as one whole-device array ran clean."""

        sink, client = _sink(_BOARD)
        sink.write(NEAR, FAR)
        self.assertEqual(len(client.writes), 1, "a split device must still be one packet")
        device, zone, colours = client.writes[0]
        self.assertEqual((device, zone), (6, None))
        self.assertEqual(colours, (FAR, FAR, FAR, NEAR, NEAR))

    def test_an_excluded_zone_forces_the_per_zone_path(self) -> None:
        """A whole-device write must colour every LED, so it cannot skip a zone."""

        sink, client = _sink(_PER_ZONE)
        sink.write(NEAR, FAR)
        self.assertEqual([(d, z) for d, z, _c in client.writes], [(6, 0), (6, 3)])
        self.assertEqual([c[0] for _d, _z, c in client.writes], [FAR, NEAR])

    def test_a_move_in_either_colour_resends_the_device(self) -> None:
        """The array carries both groups, so a change in either must go out."""

        sink, client = _sink(_BOARD)
        sink.write(NEAR, FAR)
        client.writes.clear()
        sink.write(NEAR, (7, 7, 7))  # only far moves
        self.assertEqual(len(client.writes), 1)
        self.assertEqual(client.writes[0][2], ((7, 7, 7), (7, 7, 7), (7, 7, 7), NEAR, NEAR))

    def test_black_is_always_sent_even_when_unchanged(self) -> None:
        """Every exit path writes black; "we think it is already black" is not
        good enough when the alternative is leaving the machine lit."""

        sink, client = _sink(_ONE)
        sink.write((0, 0, 0), (0, 0, 0))
        client.writes.clear()
        sink.black()
        sink.black()
        self.assertEqual(len(client.writes), 2)
        self.assertTrue(all(set(w[2]) == {(0, 0, 0)} for w in client.writes))

    def test_a_reconnect_forgets_what_it_had_sent(self) -> None:
        """A restarted server shows nothing; believing otherwise leaves it dark."""

        sink, client = _sink(_ONE)
        sink.write(NEAR, FAR)
        sink._sent.clear()  # what connect() does
        sink.write(NEAR, FAR)
        self.assertEqual(len(client.writes), 2)


class BusRateLimitTest(unittest.TestCase):
    """The per-bus ceiling still applies on top of the change filter.

    SMBus is shared with the sensors that fan control and monitoring read, so
    its rate is the one genuinely risky thing this program does.
    """

    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_a_changed_colour_still_waits_for_the_bus_interval(self) -> None:
        sink = OpenRGBSink(
            parse_config(
                {"socket": {"path": "/tmp/x.sock"}, "rates": {"usb_hz": 5, "smbus_hz": 1}}
            )
        )
        client = _FakeClient()
        sink._client = client
        sink._targets = list(_ONE)
        sink.write(NEAR, FAR)
        sink.write((99, 99, 99), FAR)  # different colour, but far inside 1/5 s
        self.assertEqual(len(client.writes), 1)


class HealthCheckTest(unittest.TestCase):
    """🚨 A wedged server is invisible without a round trip.

    On loopback our own send queue reads zero while the server sits on hundreds
    of kilobytes of unread packets -- the bytes just move into its receive
    buffer. That happened for real: the server stopped reading, every log line
    stayed clean, and the case froze on an old frame.
    """

    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def _sink_with(self, client) -> OpenRGBSink:
        sink = OpenRGBSink(parse_config({"socket": {"path": "/tmp/x.sock"}}))
        sink._interval = lambda _bus: 0.0  # type: ignore[method-assign]
        sink._client = client
        sink._targets = list(_ONE)
        return sink

    def test_a_healthy_server_is_probed_and_written_to(self) -> None:
        client = _FakeClient(lag=0.01)
        sink = self._sink_with(client)
        sink.write(NEAR, FAR)
        self.assertEqual(client.probes, 1)
        self.assertEqual(len(client.writes), 1)

    def test_it_does_not_probe_on_every_tick(self) -> None:
        client = _FakeClient(lag=0.01)
        sink = self._sink_with(client)
        for _ in range(10):
            sink.write(NEAR, FAR)
        self.assertEqual(client.probes, 1)

    def test_it_probes_again_after_the_interval(self) -> None:
        client = _FakeClient(lag=0.01)
        sink = self._sink_with(client)
        sink.write(NEAR, FAR)
        sink._checked_at -= HEALTH_CHECK_SECONDS + 1
        sink.write(NEAR, FAR)
        self.assertEqual(client.probes, 2)

    def test_one_slow_reply_is_tolerated(self) -> None:
        """A healthy server answers in under 3 ms; dropping the connection over
        a single hiccup costs a visible blip for nothing."""

        client = _FakeClient(answers=False)
        sink = self._sink_with(client)
        sink.write(NEAR, FAR)  # must not raise
        self.assertFalse(client.closed)
        self.assertIsNotNone(sink._client)

    def test_a_silent_server_drops_the_connection_and_raises(self) -> None:
        """Reconnecting discards the backlog, which is every stale frame."""

        client = _FakeClient(answers=False)
        sink = self._sink_with(client)
        for _ in range(HEALTH_FAILURES_BEFORE_RECONNECT):
            sink._checked_at = 0.0
            try:
                sink.write(NEAR, FAR)
            except OpenRGBError:
                break
        self.assertTrue(client.closed)
        self.assertIsNone(sink._client)

    def test_a_recovered_server_clears_the_failure_count(self) -> None:
        client = _FakeClient(answers=False)
        sink = self._sink_with(client)
        sink.write(NEAR, FAR)
        self.assertEqual(sink._health_failures, 1)
        client.answers = True
        sink._checked_at = 0.0
        sink.write(NEAR, FAR)
        self.assertEqual(sink._health_failures, 0)

    def test_the_resilient_wrapper_absorbs_that_and_retries(self) -> None:
        from linux_rgb.sinks import ResilientSink

        client = _FakeClient(answers=False)
        sink = self._sink_with(client)
        sink._health_failures = HEALTH_FAILURES_BEFORE_RECONNECT - 1
        wrapped = ResilientSink(sink)
        self.assertFalse(wrapped.write(NEAR, FAR))
        self.assertTrue(wrapped.degraded)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class StaticColourTest(unittest.TestCase):
    """A `static` device is set once per connection, never on the tick loop.

    🚨 The reason it exists: OpenRGB's Direct mode does not persist, so every
    restart of the server left the excluded keyboard dark -- and "excluded"
    meant nothing would ever put it back. It had to be fixed by hand each time,
    including after our own recovery restarts.
    """

    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    class _Controller:
        def __init__(self, index, name, led_count):
            self.index, self.name, self.led_count, self.zones = index, name, led_count, []

    class _Client(_FakeClient):
        def __init__(self, controllers):
            super().__init__()
            self.controllers = controllers
            self.direct: list[int] = []

        def set_direct_mode(self, device):
            self.direct.append(device)
            return True

    def _sink(self, config_devices):
        config = parse_config({"socket": {"path": "/tmp/x.sock"}, "devices": config_devices})
        return OpenRGBSink(config)

    def test_a_static_device_is_written_its_colour(self) -> None:
        sink = self._sink([{"match": "Keychron", "group": "excluded", "static": "#ffffff"}])
        client = self._Client([self._Controller(4, "Keychron K0 Max", 26)])
        sink._apply_static(client)
        self.assertEqual(len(client.writes), 1)
        device, zone, colours = client.writes[0]
        self.assertEqual((device, zone), (4, None))
        self.assertEqual(set(colours), {(255, 255, 255)})
        self.assertEqual(len(colours), 26, "every LED must be covered")

    def test_it_pushes_direct_mode_first(self) -> None:
        """Without it the keyboard keeps its firmware effect and ignores us."""

        sink = self._sink([{"match": "Keychron", "group": "excluded", "static": "#ffffff"}])
        client = self._Client([self._Controller(4, "Keychron K0 Max", 26)])
        sink._apply_static(client)
        self.assertEqual(client.direct, [4])

    def test_a_device_with_no_static_is_left_alone(self) -> None:
        sink = self._sink([{"match": "Keychron", "group": "excluded"}])
        client = self._Client([self._Controller(4, "Keychron K0 Max", 26)])
        sink._apply_static(client)
        self.assertEqual(client.writes, [])

    def test_a_static_device_is_not_a_sync_target(self) -> None:
        """It must not join the tick loop, or it would follow the screen."""

        sink = self._sink([{"match": "Keychron", "group": "excluded", "static": "#ffffff"}])
        client = self._Client([self._Controller(4, "Keychron K0 Max", 26)])
        self.assertEqual(sink._plan(client), [])

    def test_a_zero_led_device_is_skipped(self) -> None:
        sink = self._sink([{"match": "Keychron", "group": "excluded", "static": "#ffffff"}])
        client = self._Client([self._Controller(4, "Keychron K0 Max", 0)])
        sink._apply_static(client)
        self.assertEqual(client.writes, [])


class DeadSocketTest(unittest.TestCase):
    """🚨 A connection error must drop the client, not just be reported.

    The resilient wrapper catches the exception but cannot clear the sink's
    client, so a dead socket would be reused for every later write -- failing
    identically each time. Only the health check ever reconnected, which costs
    six timeouts. Measured as ~30 s of darkness after an OpenRGB restart, on top
    of the retry backoff.
    """

    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    class _Broken(_FakeClient):
        def update_leds(self, device, colours) -> None:
            raise OpenRGBError("Send failed: [Errno 32] Broken pipe")

        def update_zone_leds(self, device, zone, colours) -> None:
            raise OpenRGBError("Send failed: [Errno 32] Broken pipe")

    def _sink(self, targets):
        sink = OpenRGBSink(parse_config({"socket": {"path": "/tmp/x.sock"}}))
        sink._interval = lambda _bus: 0.0  # type: ignore[method-assign]
        sink._check_health = lambda _now: None  # type: ignore[method-assign]
        sink._client = self._Broken()
        sink._targets = list(targets)
        return sink

    def test_a_broken_write_drops_the_client(self) -> None:
        sink = self._sink(_ONE)
        with self.assertRaises(OpenRGBError):
            sink.write(NEAR, FAR)
        self.assertIsNone(sink._client, "the next write must reconnect, not reuse this")

    def test_a_broken_black_drops_the_client(self) -> None:
        sink = self._sink(_ONE)
        with self.assertRaises(OpenRGBError):
            sink.black()
        self.assertIsNone(sink._client)

    def test_the_per_zone_path_drops_it_too(self) -> None:
        sink = self._sink(_PER_ZONE)
        with self.assertRaises(OpenRGBError):
            sink.write(NEAR, FAR)
        self.assertIsNone(sink._client)
