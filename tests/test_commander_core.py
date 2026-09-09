"""Transport tests for the Commander Core, against a fake HID device.

The live device proves the happy path; these cover what live testing cannot
reach on demand -- a controller that answers with the wrong acknowledgement,
one that goes silent mid-write, an absent device, and the guarantee that
closing always writes black.
"""

from __future__ import annotations

import unittest

from linux_rgb.commander_core import CommanderCore, CommanderCoreError
from linux_rgb.commander_protocol import DATA_TYPE_LED_COUNT, PORT_SLOTS, REPORT_LENGTH


def reassemble_colour_payload(written: list[bytes]) -> bytes:
    """Rebuild the 699-byte colour buffer from the packets actually sent.

    A colour write spans one 0x06 packet carrying 87 bytes after its 6-byte
    header, then 0x07 continuation packets of 93 bytes each. Reading only the
    first packet -- as an earlier version of this test did -- sees offset 87
    as empty and reports a false failure.
    """

    start = max(i for i, p in enumerate(written) if p[2] == 0x06)
    payload = bytes(written[start][4:][6:][: REPORT_LENGTH - 9])
    for packet in written[start + 1 :]:
        if packet[2] != 0x07:
            break
        payload += bytes(packet[4:][: REPORT_LENGTH - 3])
    return payload[:699]


class FakeHid:
    """Answers like a Commander Core: echoes the command byte at index 1."""

    def __init__(self, led_counts: bytes | None = None) -> None:
        self.written: list[bytes] = []
        self.closed = False
        self._next: list[bytes] = []
        self._led_counts = led_counts if led_counts is not None else (
            bytes([7])
            + (2).to_bytes(2, "little") + (29).to_bytes(2, "little")
            + ((2).to_bytes(2, "little") + (8).to_bytes(2, "little")) * 3
            + ((1).to_bytes(2, "little") + (0).to_bytes(2, "little")) * 3
        )
        self._read_stage = 0

    def write(self, packet: bytes) -> int:
        self.written.append(bytes(packet))
        command = packet[2]
        reply = bytearray(REPORT_LENGTH)
        reply[0] = 0x00
        reply[1] = command
        if command == 0x08:  # a read; stage the LED-count payload
            stage = packet[4]
            if stage == 0x01:
                reply[3:5] = bytes(DATA_TYPE_LED_COUNT)
                reply[5 : 5 + len(self._led_counts)] = self._led_counts
        self._next.append(bytes(reply))
        return len(packet)

    def read(self, length: int, timeout_ms: int = -1) -> list[int]:
        # Matches cython-hidapi: read(max_length, timeout_ms). The driver drains
        # with timeout_ms=0 before every write, so the signature matters.
        return list(self._next.pop(0)) if self._next else []

    def close(self) -> None:
        self.closed = True


class _Opened(CommanderCore):
    """CommanderCore pre-attached to a fake, skipping hid enumeration."""

    def __init__(self, fake: FakeHid) -> None:
        super().__init__(path=b"/dev/fake")
        self._device = fake


class TransportTest(unittest.TestCase):
    def test_reads_this_rigs_topology(self) -> None:
        cc = _Opened(FakeHid())
        cc.led_counts = cc._read_led_counts()
        self.assertEqual(cc.led_counts, [29, 8, 8, 8, None, None, None])
        self.assertEqual(cc.pump_leds, 29)
        self.assertEqual(cc.port_leds, [8, 8, 8, 0, 0, 0])

    def test_a_wrong_acknowledgement_is_an_error_not_silence(self) -> None:
        """The ACK is the only confirmation this protocol offers."""

        fake = FakeHid()
        original = fake.write

        def bad_write(packet):
            result = original(packet)
            fake._next[-1] = bytes([0x00, 0xFF]) + bytes(REPORT_LENGTH - 2)
            return result

        fake.write = bad_write
        cc = _Opened(fake)
        with self.assertRaises(CommanderCoreError) as caught:
            cc.wake()
        self.assertIn("does not acknowledge", str(caught.exception))

    def test_a_silent_device_is_an_error(self) -> None:
        fake = FakeHid()
        fake.read = lambda length, timeout_ms=-1: []
        cc = _Opened(fake)
        with self.assertRaises(CommanderCoreError) as caught:
            cc.wake()
        self.assertIn("No reply", str(caught.exception))

    def test_wrong_data_type_names_both_sides(self) -> None:
        fake = FakeHid()
        original = fake.write

        def bad_type(packet):
            result = original(packet)
            if packet[2] == 0x08 and packet[4] == 0x01:
                reply = bytearray(fake._next[-1])
                reply[3:5] = b"\x99\x00"
                fake._next[-1] = bytes(reply)
            return result

        fake.write = bad_type
        cc = _Opened(fake)
        with self.assertRaises(CommanderCoreError) as caught:
            cc._read_led_counts()
        message = str(caught.exception)
        self.assertIn("expected", message)
        self.assertIn("15", message, "the expected type should be named")

    def test_operations_on_a_closed_device_raise(self) -> None:
        cc = CommanderCore(path=b"/dev/fake")
        with self.assertRaises(CommanderCoreError):
            cc.wake()

    def test_close_writes_black_before_releasing(self) -> None:
        """Closing without blacking leaves the firmware pattern (green fans)."""

        fake = FakeHid()
        cc = _Opened(fake)
        cc.led_counts = [29, 8, 8, 8, None, None, None]
        cc.close()
        self.assertTrue(fake.closed)
        colour_writes = [p for p in fake.written if p[2] == 0x06]
        self.assertTrue(colour_writes, "a colour packet should have been sent")
        body = colour_writes[-1][4:]
        self.assertEqual(body[6:], bytes(len(body) - 6), "black payload")

    def test_close_still_releases_when_blacking_fails(self) -> None:
        fake = FakeHid()
        cc = _Opened(fake)
        cc.led_counts = [29, 8, 8, 8, None, None, None]
        fake.write = lambda packet: (_ for _ in ()).throw(OSError("gone"))
        cc.close()
        self.assertTrue(fake.closed, "the handle must be released regardless")

    def test_missing_device_explains_the_usual_cause(self) -> None:
        cc = CommanderCore(path=None)
        import linux_rgb.commander_core as module

        original = module.find_device_path
        module.find_device_path = lambda: None
        try:
            with self.assertRaises(CommanderCoreError) as caught:
                cc.open()
            self.assertIn("OpenRGB", str(caught.exception), "name the usual culprit")
        finally:
            module.find_device_path = original


class ColourSelectionTest(unittest.TestCase):
    def test_only_real_leds_are_lit(self) -> None:
        """Ports 4-6 have nothing attached and must stay black."""

        fake = FakeHid()
        cc = _Opened(fake)
        cc.led_counts = [29, 8, 8, 8, None, None, None]
        cc.set_colour((255, 0, 0), (0, 0, 255))

        payload = reassemble_colour_payload(fake.written)
        self.assertEqual(len(payload), 699)
        self.assertEqual(payload[0:3], bytes((255, 0, 0)), "pump red")
        self.assertEqual(payload[84:87], bytes((255, 0, 0)), "pump LED 29 red")
        self.assertEqual(payload[87:90], bytes((0, 0, 255)), "port 1 blue")
        # Port 1 has 8 LEDs in a 34-slot region: slot 9 onwards is black.
        self.assertEqual(payload[87 + 24 : 87 + PORT_SLOTS * 3], bytes(PORT_SLOTS * 3 - 24))
        # Ports 4-6 have nothing attached at all.
        self.assertEqual(payload[87 + 3 * PORT_SLOTS * 3 :], bytes(3 * PORT_SLOTS * 3))


class SettlingRetryTest(unittest.TestCase):
    """A controller just released by another process needs a moment.

    Found by the shutdown unit, which opens the device seconds after the daemon
    closed it and got "returned data type (0, 0)" -- which reads like a protocol
    mismatch but is timing. Without the retry the AIO silently stayed lit
    through shutdown and drifted back to firmware green.
    """

    def test_it_recovers_after_transient_zeroed_reads(self) -> None:
        fake = FakeHid()
        original = fake.write
        state = {"failures": 2}

        def flaky(packet):
            result = original(packet)
            if packet[2] == 0x08 and packet[4] == 0x01 and state["failures"] > 0:
                state["failures"] -= 1
                reply = bytearray(fake._next[-1])
                reply[3:5] = b"\x00\x00"          # the zeroed data type
                fake._next[-1] = bytes(reply)
            return result

        fake.write = flaky
        cc = _Opened(fake)
        counts = cc._wake_and_read_counts(attempts=4, delay=0.0)
        self.assertEqual(counts, [29, 8, 8, 8, None, None, None])
        self.assertEqual(state["failures"], 0, "both transient failures were consumed")

    def test_it_gives_up_with_a_clear_message(self) -> None:
        fake = FakeHid()
        original = fake.write

        def always_bad(packet):
            result = original(packet)
            if packet[2] == 0x08 and packet[4] == 0x01:
                reply = bytearray(fake._next[-1])
                reply[3:5] = b"\x00\x00"
                fake._next[-1] = bytes(reply)
            return result

        fake.write = always_bad
        cc = _Opened(fake)
        with self.assertRaises(CommanderCoreError) as caught:
            cc._wake_and_read_counts(attempts=2, delay=0.0)
        self.assertIn("did not become ready", str(caught.exception))



class DrainTest(unittest.TestCase):
    """The pre-write drain must terminate even against a chatty device.

    An unbounded drain hung the daemon outright the first time it met a
    controller that kept producing reports, which is exactly what happens for a
    moment after another process releases the device.
    """

    def test_it_terminates_against_an_endless_device(self) -> None:
        fake = FakeHid()
        fake.read = lambda length, timeout_ms=-1: [0] * length  # never empty
        cc = _Opened(fake)
        cc._drain()  # must return rather than spin

    def test_it_never_asks_for_a_zero_timeout(self) -> None:
        """A 0 ms timeout BLOCKS in cython-hidapi; it does not poll.

        Written the obvious way this hung the daemon outright, so the value is
        asserted rather than trusted to survive a tidy-up.
        """

        fake = FakeHid()
        seen: list[int] = []

        def recording(length, timeout_ms=-1):
            seen.append(timeout_ms)
            return []

        fake.read = recording
        _Opened(fake)._drain()
        self.assertTrue(seen, "the drain should have read at least once")
        for timeout in seen:
            self.assertGreater(timeout, 0, "0 would block forever")

    def test_it_stops_as_soon_as_the_queue_is_empty(self) -> None:
        fake = FakeHid()
        calls = {"n": 0}

        def counting(length, timeout_ms=-1):
            calls["n"] += 1
            return [] if calls["n"] > 2 else [0] * length

        fake.read = counting
        cc = _Opened(fake)
        cc._drain()
        self.assertEqual(calls["n"], 3, "two queued reports, then empty")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
