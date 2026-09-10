"""How much the OpenRGB sink actually puts on the wire.

The write side of this protocol is fire-and-forget: ``sendall`` returns in
microseconds no matter what the server is doing with the bytes. So overrunning
the server is invisible from the sending end, and the only place the limit can
be enforced is here.
"""

from __future__ import annotations

import logging
import unittest

from ambience_rgb.config import parse_config
from ambience_rgb.openrgb_client import OpenRGBError
from ambience_rgb.sinks import (
    COLOUR_EPSILON,
    HEALTH_CHECK_SECONDS,
    HEALTH_FAILURES_BEFORE_RECONNECT,
    REFRESH_SECONDS,
    OpenRGBSink,
)

NEAR = (10, 20, 30)
FAR = (40, 50, 60)


class _FakeClient:
    def __init__(
        self, lag: float = 0.0, answers: bool = True, controllers: int = 0
    ) -> None:
        self.writes: list[tuple[int, int | None, tuple[int, int, int], int]] = []
        self.lag = lag
        self.answers = answers
        self.controllers_reported = controllers
        self.probes = 0
        self.closed = False

    def probe(self, timeout: float) -> tuple[float, int]:
        self.probes += 1
        if not self.answers:
            raise OpenRGBError("no answer")
        return self.lag, self.controllers_reported

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
_ONE = [(6, (("near", 2, "RGB"),), "usb", False)]
# One controller split across three zones -- written as ONE packet whose array
# carries each zone's colour in order.
_BOARD = [(6, (("far", 1, "RGB"), ("far", 2, "RGB"), ("near", 2, "RGB")), "usb", False)]
# The excluded-zone fallback, which must still go zone by zone.
_PER_ZONE = [
    (6, ((0, "far", 1, "RGB"),), "usb", True),
    (6, ((3, "near", 2, "RGB"),), "usb", True),
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

    def test_a_shrunken_device_list_is_re_planned_not_accepted(self) -> None:
        # The real incident: OpenRGB accepts connections before detection has
        # finished, so the daemon planned against 1 controller of 7 at login and
        # every later probe agreed it was healthy. It drove the RAM and nothing
        # else, indefinitely, with a clean log.
        client = _FakeClient(lag=0.01, controllers=7)
        sink = self._sink_with(client)
        sink._planned_controllers = 1
        with self.assertRaises(OpenRGBError):
            sink.write(NEAR, FAR)
        self.assertTrue(client.closed)

    def test_a_device_set_change_is_not_counted_as_a_wedge(self) -> None:
        # Escalation exists for a server that keeps going unresponsive. A device
        # list that legitimately changed is not that, and counting it would
        # restart OpenRGB three hot-plugs into a perfectly healthy run.
        client = _FakeClient(lag=0.01, controllers=7)
        sink = self._sink_with(client)
        sink._planned_controllers = 1
        with self.assertRaises(OpenRGBError):
            sink.write(NEAR, FAR)
        self.assertEqual(sink._reconnects, [])

    def test_an_unchanged_device_set_is_written_to_normally(self) -> None:
        client = _FakeClient(lag=0.01, controllers=7)
        sink = self._sink_with(client)
        sink._planned_controllers = 7
        sink.write(NEAR, FAR)
        self.assertEqual(len(client.writes), 1)
        self.assertFalse(client.closed)

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
        from ambience_rgb.sinks import ResilientSink

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


class ReleaseOnFailureTest(unittest.TestCase):
    """🚨 Both sinks must drop a handle whose last operation failed.

    ResilientSink retries on a timer but cannot reach inside to invalidate the
    handle, so without this a dead socket or HID handle is reused for every
    retry and fails identically forever. The OpenRGB side took 71 s to recover
    from a server restart because of it; the Commander side had no rescue at all
    and would never have reopened an unplugged AIO.
    """

    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    class _Sink:
        name = "fake"

        def __init__(self) -> None:
            self.closed = 0
            self.released = 0

        def release(self) -> None:
            self.released += 1

        def close(self) -> None:
            self.closed += 1

    def test_it_closes_on_failure_and_re_raises(self) -> None:
        from ambience_rgb.sinks import released_on_failure

        sink = self._Sink()
        with self.assertRaises(ValueError):
            with released_on_failure(sink):
                raise ValueError("device fell off the bus")
        self.assertEqual(sink.released, 1)
        self.assertEqual(sink.closed, 0, "🚨 close() writes black; a failure must not")

    def test_it_does_nothing_on_success(self) -> None:
        from ambience_rgb.sinks import released_on_failure

        sink = self._Sink()
        with released_on_failure(sink):
            pass
        self.assertEqual(sink.released, 0)
        self.assertEqual(sink.closed, 0)

    def test_a_failing_close_does_not_mask_the_real_error(self) -> None:
        from ambience_rgb.sinks import released_on_failure

        class Exploding(self._Sink):
            def release(self) -> None:
                raise RuntimeError("release also broken")

        with self.assertRaises(ValueError):
            with released_on_failure(Exploding()):
                raise ValueError("the real problem")

    def test_the_commander_sink_drops_its_device(self) -> None:
        from ambience_rgb.config import parse_config
        from ambience_rgb.sinks import CommanderCoreSink

        class Dead:
            blacked = 0
            pump_leds = 29
            port_leds = [8, 8, 8, 0, 0, 0]

            def set_direct(self, pump, ports):
                raise RuntimeError("Reply 0x01 does not acknowledge command 0x07")

            def set_colour(self, pump, fans):
                raise RuntimeError("Reply 0x01 does not acknowledge command 0x07")

            def black(self):
                type(self).blacked += 1
                raise RuntimeError("gone")

            def release(self):
                self.released = True

            def close(self):
                self.black()

        sink = CommanderCoreSink(parse_config({"socket": {"path": "/tmp/x.sock"}}).commander)
        sink._device = Dead()
        with self.assertRaises(RuntimeError):
            sink.write(NEAR, FAR)
        self.assertIsNone(sink._device, "an unplugged AIO must be reopened, not reused")

    def test_the_commander_sink_drops_it_on_black_too(self) -> None:
        from ambience_rgb.config import parse_config
        from ambience_rgb.sinks import CommanderCoreSink

        class Dead:
            def black(self):
                raise RuntimeError("gone")

            def release(self):
                self.released = True

            def close(self):
                self.black()

        sink = CommanderCoreSink(parse_config({"socket": {"path": "/tmp/x.sock"}}).commander)
        sink._device = Dead()
        with self.assertRaises(RuntimeError):
            sink.black()
        self.assertIsNone(sink._device)


class CommanderWritesEveryTickTest(unittest.TestCase):
    """🚨 The Commander Core must be written on EVERY tick, unfiltered.

    It has no off state and no memory: about ten seconds after the last commit
    it reverts to its firmware pattern, which on this rig is green fans. The
    change filter that makes the OpenRGB sink cheap would therefore turn the AIO
    green on any static screen. This test exists so that "why does this one
    resend the same colour?" is answered before someone optimises it.
    """

    class _Device:
        pump_leds = 29
        port_leds = [8, 8, 8, 0, 0, 0]

        def __init__(self) -> None:
            self.colours: list[tuple] = []

        def set_direct(self, pump, ports) -> None:
            self.colours.append((tuple(pump), tuple(tuple(p) for p in ports)))

        def set_colour(self, pump, fans) -> None:
            self.colours.append((pump, fans))

        def black(self) -> None:
            self.colours.append(((0, 0, 0), (0, 0, 0)))

        def release(self) -> None:
            pass

        def close(self) -> None:
            pass

    def _sink(self):
        from ambience_rgb.config import parse_config
        from ambience_rgb.sinks import CommanderCoreSink

        sink = CommanderCoreSink(parse_config({"socket": {"path": "/tmp/x.sock"}}).commander)
        device = self._Device()
        sink._device = device
        return sink, device

    def test_an_unchanged_colour_is_still_sent_every_time(self) -> None:
        sink, device = self._sink()
        for _ in range(10):
            sink.write(NEAR, FAR)
        self.assertEqual(len(device.colours), 10, "filtering this would let the fans go green")

    def test_black_is_also_sent_every_time(self) -> None:
        """"Off" is a colour you keep writing, not a state the device holds."""

        sink, device = self._sink()
        for _ in range(5):
            sink.black()
        self.assertEqual(len(device.colours), 5)


class PaletteReachesTheWireTest(unittest.TestCase):
    """🚨 The guard for a feature that was fully built and never connected.

    render.spread was correct and unit-tested, the config parsed, the daemon
    extracted the palette -- and OpenRGBSink.write called _send_colours without
    it, so every LED still got one flat colour. 163 unit tests passed throughout,
    because each layer was tested in isolation and nothing asserted that a
    palette handed to a SINK comes out as a varying array.

    Test the seam, not just the pieces either side of it.
    """

    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    PALETTE = [((255, 0, 0), 0.4), ((0, 0, 255), 0.35), ((0, 255, 0), 0.25)]

    def _sink(self, targets, bias):
        sink = OpenRGBSink(
            parse_config({"socket": {"path": "/tmp/x.sock"}, "spread": {"bias": bias}})
        )
        sink._interval = lambda _bus: 0.0  # type: ignore[method-assign]
        sink._check_health = lambda _now: None  # type: ignore[method-assign]
        client = _FakeClient()
        sink._client = client
        sink._targets = list(targets)
        return sink, client

    def test_a_palette_produces_a_VARYING_array_on_the_wire(self) -> None:
        sink, client = self._sink([(6, (("far", 40, "RGB"),), "usb", False)], 0.0)
        sink.write(NEAR, FAR, self.PALETTE)
        colours = client.writes[0][2]
        self.assertEqual(len(colours), 40)
        self.assertGreater(len(set(colours)), 3, "the palette never reached the wire")

    def test_no_palette_is_still_flat(self) -> None:
        sink, client = self._sink([(6, (("far", 40, "RGB"),), "usb", False)], 0.0)
        sink.write(NEAR, FAR, [])
        self.assertEqual(set(client.writes[0][2]), {FAR})

    def test_bias_one_is_flat_even_with_a_palette(self) -> None:
        """The off switch has to actually switch it off."""

        sink, client = self._sink([(6, (("far", 40, "RGB"),), "usb", False)], 1.0)
        sink.write(NEAR, FAR, self.PALETTE)
        self.assertEqual(set(client.writes[0][2]), {FAR})

    def test_the_per_zone_path_gets_the_palette_too(self) -> None:
        sink, client = self._sink([(6, ((3, "far", 40, "RGB"),), "usb", True)], 0.0)
        sink.write(NEAR, FAR, self.PALETTE)
        self.assertGreater(len(set(client.writes[0][2])), 3)

    def test_a_changed_palette_resends_even_when_near_and_far_hold_still(self) -> None:
        """Otherwise the gradient freezes whenever the two group colours do."""

        sink, client = self._sink([(6, (("far", 40, "RGB"),), "usb", False)], 0.0)
        sink.write(NEAR, FAR, self.PALETTE)
        client.writes.clear()
        moved = [((0, 255, 255), 0.4), ((255, 255, 0), 0.35), ((255, 0, 255), 0.25)]
        sink.write(NEAR, FAR, moved)
        self.assertEqual(len(client.writes), 1, "a moving palette must keep being written")

    def test_each_group_keeps_its_own_base(self) -> None:
        """near/far survives the gradient; it is a real choice about the room."""

        sink, client = self._sink(
            [(6, (("near", 20, "RGB"), ("far", 20, "RGB")), "usb", False)], 0.9
        )
        sink.write((255, 0, 0), (0, 0, 255), self.PALETTE)
        colours = client.writes[0][2]
        self.assertGreater(sum(c[0] for c in colours[:20]), sum(c[0] for c in colours[20:]))


class ChannelOrderReachesTheWireTest(unittest.TestCase):
    """The order must travel with the LEDs it applies to, per zone."""

    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def _sink(self, targets):
        sink = OpenRGBSink(parse_config({"socket": {"path": "/tmp/x.sock"}}))
        sink._interval = lambda _bus: 0.0  # type: ignore[method-assign]
        sink._check_health = lambda _now: None  # type: ignore[method-assign]
        client = _FakeClient()
        sink._client = client
        sink._targets = list(targets)
        return sink, client

    def test_a_grb_segment_is_pre_swapped(self) -> None:
        sink, client = self._sink([(6, (("near", 4, "GRB"),), "usb", False)])
        sink.write((255, 0, 96), (0, 0, 0))
        self.assertEqual(set(client.writes[0][2]), {(0, 255, 96)})

    def test_segments_on_one_device_can_differ(self) -> None:
        """🚨 The whole point: the ASRock wires its own LEDs differently from
        the fan headers hanging off it."""

        sink, client = self._sink(
            [(6, (("near", 2, "RGB"), ("near", 2, "GRB")), "usb", False)]
        )
        sink.write((255, 0, 96), (0, 0, 0))
        colours = client.writes[0][2]
        self.assertEqual(colours[:2], ((255, 0, 96), (255, 0, 96)))
        self.assertEqual(colours[2:], ((0, 255, 96), (0, 255, 96)))

    def test_the_per_zone_path_honours_it_too(self) -> None:
        sink, client = self._sink([(6, ((3, "near", 4, "GRB"),), "usb", True)])
        sink.write((255, 0, 96), (0, 0, 0))
        self.assertEqual(set(client.writes[0][2]), {(0, 255, 96)})

    def test_black_stays_black_under_reordering(self) -> None:
        sink, client = self._sink([(6, (("near", 4, "GRB"),), "usb", False)])
        sink.black()
        self.assertEqual(set(client.writes[0][2]), {(0, 0, 0)})


class WedgeEscalationTest(unittest.TestCase):
    """🚨 Reconnecting does not fix a server that answers but drives nothing.

    Observed 2026-09-10: round trips at 0.1 ms, new connections served, seven
    controllers reported -- and three consecutive colour writes with no effect on
    any device. The liveness probe cannot see it, because the server IS live.
    Only a restart recovered, and until now only a human could do that.
    """

    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def _sink(self, command=()):
        payload = {"socket": {"path": "/tmp/x.sock"}}
        if command:
            payload["openrgb"] = {"restart_command": list(command)}
        sink = OpenRGBSink(parse_config(payload))
        sink.close = lambda: None  # type: ignore[method-assign]
        return sink

    def test_a_few_reconnects_do_not_trigger_a_restart(self) -> None:
        """A dropped connection is normal; it must not restart a service."""

        import ambience_rgb.sinks as module

        calls = []
        sink = self._sink(("true",))
        original = module.OpenRGBSink._note_reconnect
        sink._note_reconnect(1.0)
        self.assertEqual(sink._reconnects, [1.0])

    def test_it_escalates_after_repeated_reconnects(self) -> None:
        import subprocess

        seen = []
        real_run = subprocess.run
        subprocess.run = lambda *a, **k: seen.append(a[0]) or real_run(["true"], capture_output=True)
        try:
            sink = self._sink(("echo", "restart"))
            for stamp in (1.0, 2.0, 3.0):
                sink._note_reconnect(stamp)
        finally:
            subprocess.run = real_run
        self.assertEqual(seen, [["echo", "restart"]])

    def test_without_a_command_it_only_logs(self) -> None:
        """Restarting another unit is a real side effect and is opt-in."""

        import subprocess

        seen = []
        real_run = subprocess.run
        subprocess.run = lambda *a, **k: seen.append(a[0])
        try:
            sink = self._sink()
            for stamp in (1.0, 2.0, 3.0):
                sink._note_reconnect(stamp)
        finally:
            subprocess.run = real_run
        self.assertEqual(seen, [])

    def test_the_cooldown_prevents_a_restart_loop(self) -> None:
        """A restart loop is worse than a frozen rig -- it also resets every
        device, including ones held at a static colour."""

        import subprocess
        from ambience_rgb.sinks import RESTART_COOLDOWN_SECONDS

        seen = []
        real_run = subprocess.run
        subprocess.run = lambda *a, **k: seen.append(a[0]) or real_run(["true"], capture_output=True)
        try:
            sink = self._sink(("echo", "x"))
            for stamp in (1.0, 2.0, 3.0):
                sink._note_reconnect(stamp)
            for stamp in (4.0, 5.0, 6.0):
                sink._note_reconnect(stamp)
        finally:
            subprocess.run = real_run
        self.assertEqual(len(seen), 1, "the second burst is inside the cooldown")

    def test_old_reconnects_fall_out_of_the_window(self) -> None:
        from ambience_rgb.sinks import WEDGE_WINDOW_SECONDS

        sink = self._sink()
        sink._note_reconnect(1.0)
        sink._note_reconnect(1.0 + WEDGE_WINDOW_SECONDS + 1)
        self.assertEqual(len(sink._reconnects), 1, "a reconnect an hour ago is not evidence")
