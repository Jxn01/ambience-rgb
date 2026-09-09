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
from linux_rgb.sinks import REFRESH_SECONDS, OpenRGBSink

NEAR = (10, 20, 30)
FAR = (40, 50, 60)


class _FakeClient:
    def __init__(self) -> None:
        self.writes: list[tuple[int, int | None, tuple[int, int, int], int]] = []

    def update_leds(self, device, colours) -> None:
        self.writes.append((device, None, colours[0], len(colours)))

    def update_zone_leds(self, device, zone, colours) -> None:
        self.writes.append((device, zone, colours[0], len(colours)))

    def close(self) -> None:
        pass


def _sink(targets) -> tuple[OpenRGBSink, _FakeClient]:
    sink = OpenRGBSink(parse_config({"socket": {"path": "/tmp/x.sock"}}))
    # Disable the per-bus limiter outright so these tests measure the change
    # filter alone. Merely configuring a high rate is not enough -- two calls in
    # a row can land inside any non-zero interval, which silently turns every
    # assertion here into a test of the clock. The limiter is a separate ceiling
    # with its own test below.
    sink._interval = lambda _bus: 0.0  # type: ignore[method-assign]
    client = _FakeClient()
    sink._client = client
    sink._targets = targets
    return sink, client


# (device, zone, group, leds, bus)
_ONE = [(6, 3, "near", 15, "usb")]
_BOARD = [
    (6, 0, "far", 1, "usb"),
    (6, 1, "far", 8, "usb"),
    (6, 3, "near", 15, "usb"),
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
        self.assertEqual([w[2] for w in client.writes], [NEAR, (99, 99, 99)])

    def test_it_refreshes_after_the_window(self) -> None:
        """A re-detected controller must not be left sitting on a stale frame."""

        sink, client = _sink(_ONE)
        sink.write(NEAR, FAR)
        colour, when = sink._sent[(6, 3)]
        sink._sent[(6, 3)] = (colour, when - REFRESH_SECONDS - 1)
        sink.write(NEAR, FAR)
        self.assertEqual(len(client.writes), 2)

    def test_each_target_is_tracked_separately(self) -> None:
        """A shared cache would let one zone's colour suppress another's."""

        sink, client = _sink(_BOARD)
        sink.write(NEAR, FAR)
        self.assertEqual(len(client.writes), 3)
        self.assertEqual(
            {(d, z): c for d, z, c, _n in client.writes},
            {(6, 0): FAR, (6, 1): FAR, (6, 3): NEAR},
        )

    def test_the_near_far_split_survives_the_filter(self) -> None:
        sink, client = _sink(_BOARD)
        sink.write(NEAR, FAR)
        client.writes.clear()
        sink.write(NEAR, (7, 7, 7))  # only far moves
        self.assertEqual([(d, z) for d, z, _c, _n in client.writes], [(6, 0), (6, 1)])

    def test_black_is_always_sent_even_when_unchanged(self) -> None:
        """Every exit path writes black; "we think it is already black" is not
        good enough when the alternative is leaving the machine lit."""

        sink, client = _sink(_ONE)
        sink.write((0, 0, 0), (0, 0, 0))
        client.writes.clear()
        sink.black()
        sink.black()
        self.assertEqual(len(client.writes), 2)
        self.assertTrue(all(w[2] == (0, 0, 0) for w in client.writes))

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
        sink._targets = [(6, 3, "near", 15, "usb")]
        sink.write(NEAR, FAR)
        sink.write((99, 99, 99), FAR)  # different colour, but far inside 1/5 s
        self.assertEqual(len(client.writes), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
