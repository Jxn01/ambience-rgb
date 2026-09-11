"""Transport tests for the Commander Core, against a fake HID device.

The live device proves the happy path; these cover what live testing cannot
reach on demand -- a controller that answers with the wrong acknowledgement,
one that goes silent mid-write, an absent device, and the guarantee that
closing always writes black.
"""

from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

from ambience_rgb.commander_core import CommanderCore, CommanderCoreError, find_device_path
from ambience_rgb.commander_protocol import DATA_TYPE_LED_COUNT, PORT_SLOTS, REPORT_LENGTH


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
    """Answers like a Commander Core: echoes the command byte at index 1.

    Reads behave as cython-hidapi's do, because the driver's drain depends on
    exactly this: ``read(n, timeout_ms)`` with a timeout waits that long when
    nothing is queued -- tallied in ``waited_ms`` -- while ``read(n)`` (timeout
    0) returns at once in non-blocking mode and never returns at all in
    blocking mode, which here raises instead of hanging the suite.
    """

    def __init__(self, led_counts: bytes | None = None) -> None:
        self.written: list[bytes] = []
        self.closed = False
        self.opened_path: bytes | None = None
        self.nonblocking = False
        self.waited_ms = 0
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

    def open_path(self, path: bytes) -> None:
        self.opened_path = path

    def set_nonblocking(self, value: int) -> int:
        self.nonblocking = bool(value)
        return 0

    def read(self, length: int, timeout_ms: int = 0) -> list[int]:
        # cython-hidapi's signature and semantics: read(max_length, timeout_ms=0).
        if self._next:
            return list(self._next.pop(0))
        if timeout_ms > 0:
            self.waited_ms += timeout_ms
            return []
        if timeout_ms == 0 and self.nonblocking:
            return []
        raise AssertionError(
            f"read({length}, {timeout_ms}) on an empty queue never returns on a blocking handle"
        )

    def close(self) -> None:
        self.closed = True


class _Opened(CommanderCore):
    """CommanderCore pre-attached to a fake, skipping hid enumeration.

    Attached the way open() attaches a real handle, so the fake is in the mode
    every exchange assumes -- see NonBlockingDrainTest.
    """

    def __init__(self, fake: FakeHid) -> None:
        super().__init__(path=b"/dev/fake")
        self._adopt(fake)


class NonBlockingDrainTest(unittest.TestCase):
    """🚨 The drain must not wait -- and a timed read on an empty queue does.

    Measured on the Windows half, 2026-09-11: hidapi's ``read(64, 1)`` on an
    empty queue took 15.5 ms there, not 1 ms, and the driver drained before each
    of the ~14 exchanges in a colour frame. A frame cost 218 ms, 97% of it
    waiting, which held the daemon to 4.4 ticks a second -- under matterlights'
    5 -- and the frames it could not keep up with queued in its socket, minutes
    deep. In non-blocking mode ``read(64)`` returned in 0.00 ms, while a read
    that names a timeout still waited for its reply. (hidraw on Linux returns
    the same 1 ms read in 1 ms, which is why this never showed there.)
    """

    def _open(self) -> tuple[CommanderCore, FakeHid]:
        """Open through the real open(), with a fake in place of the hid module."""

        fake = FakeHid()
        with mock.patch.dict(sys.modules, {"hid": types.SimpleNamespace(device=lambda: fake)}):
            core = CommanderCore(path=b"/dev/fake").open()
        self.assertEqual(fake.opened_path, b"/dev/fake")
        return core, fake

    def test_open_leaves_the_handle_non_blocking(self) -> None:
        _core, fake = self._open()
        self.assertTrue(fake.nonblocking, "on a blocking handle the drain must wait -- or hang")

    def test_an_empty_queue_is_drained_without_waiting(self) -> None:
        core, fake = self._open()
        fake.waited_ms = 0
        core._drain()
        self.assertEqual(fake.waited_ms, 0, "the drain waited on an empty queue")

    def test_a_colour_frame_waits_for_nothing_but_replies(self) -> None:
        core, fake = self._open()
        fake.waited_ms = 0
        core.set_colour((10, 20, 30), (40, 50, 60))
        self.assertEqual(fake.waited_ms, 0, "the drains in one colour frame waited")

    def test_the_reply_read_still_names_its_timeout(self) -> None:
        """Non-blocking mode waits only on a read that names a timeout, so the reply must."""

        from ambience_rgb.commander_core import _READ_TIMEOUT_MS

        core, fake = self._open()
        timeouts: list[int] = []
        original = fake.read

        def recording(length, timeout_ms=0):
            timeouts.append(timeout_ms)
            return original(length, timeout_ms)

        fake.read = recording
        core.wake()
        self.assertIn(_READ_TIMEOUT_MS, timeouts, "the reply read must wait for the device")
        self.assertEqual(
            set(timeouts), {0, _READ_TIMEOUT_MS}, "a drain poll is timeout 0, a reply read the budget"
        )


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
        import ambience_rgb.commander_core as module

        original = module.find_device_path
        module.find_device_path = lambda: None
        try:
            with self.assertRaises(CommanderCoreError) as caught:
                cc.open()
            self.assertIn("OpenRGB", str(caught.exception), "name the usual culprit")
        finally:
            module.find_device_path = original


class DiscoveryTest(unittest.TestCase):
    """Which HID entry is the Commander Core's control interface, on each OS.

    The device has two vendor interfaces. Interface 0 answers; interface 1 opens
    cleanly and then times out on the first read. Linux (hidraw) lists one entry
    per USB interface and always knows its number. Windows hidapi lists one entry
    per HID top-level COLLECTION and does not always know the interface number.
    Both report the usage from the report descriptor, and there the two differ:
    ``0xFF42``/``0x0001`` is control -- read off this rig's own descriptors,
    ``06 42 ff 09 01`` on hidraw20 and ``06 42 ff 09 02`` on hidraw21.
    """

    def _entry(self, interface: int, usage: int, path: bytes) -> dict:
        return {"interface_number": interface, "usage_page": 0xFF42, "usage": usage, "path": path}

    def test_linux_picks_interface_zero(self) -> None:
        entries = [self._entry(1, 0x0002, b"/dev/hidraw21"), self._entry(0, 0x0001, b"/dev/hidraw20")]
        self.assertEqual(find_device_path(entries), b"/dev/hidraw20")

    def test_windows_picks_the_control_collection_without_interface_numbers(self) -> None:
        entries = [
            self._entry(-1, 0x0002, b"\\\\?\\hid#vid_1b1c&pid_0c32&mi_01#7&b"),
            self._entry(-1, 0x0001, b"\\\\?\\hid#vid_1b1c&pid_0c32&mi_00#7&a"),
        ]
        self.assertEqual(find_device_path(entries), b"\\\\?\\hid#vid_1b1c&pid_0c32&mi_00#7&a")

    def test_the_usage_outranks_the_interface_number(self) -> None:
        # Ask the device: the descriptor says what an interface IS.
        entries = [self._entry(0, 0x0002, b"wrong"), self._entry(1, 0x0001, b"right")]
        self.assertEqual(find_device_path(entries), b"right")

    def test_without_usages_it_falls_back_to_interface_zero(self) -> None:
        # Some hidapi builds report usage 0 where they cannot parse it.
        entries = [
            {"interface_number": 1, "usage_page": 0, "usage": 0, "path": b"one"},
            {"interface_number": 0, "usage_page": 0, "usage": 0, "path": b"zero"},
        ]
        self.assertEqual(find_device_path(entries), b"zero")

    def test_the_dead_interface_alone_is_not_the_device(self) -> None:
        self.assertIsNone(find_device_path([self._entry(1, 0x0002, b"x")]))
        self.assertIsNone(find_device_path([]))

    def test_interface_zero_naming_itself_as_something_else_is_not_the_device(self) -> None:
        # The fallback to interface 0 is for a usage nobody could read -- not
        # for an interface whose descriptor says it is not the control one.
        self.assertIsNone(find_device_path([self._entry(0, 0x0002, b"x")]))


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

    def test_a_timeout_free_read_only_ever_meets_a_non_blocking_handle(self) -> None:
        """``read(n)`` BLOCKS FOREVER on a blocking handle in cython-hidapi; it does not poll.

        Written that way on a blocking handle, the drain hung the daemon outright
        -- which is why it once read with a 1 ms timeout instead. That timeout
        cost 15.5 ms a read on Windows (see NonBlockingDrainTest), so the drain
        now reads with no timeout, on a handle open() has made non-blocking. This
        pins the pairing, asserted rather than trusted to survive a tidy-up:
        every timeout-free read must find the handle non-blocking.
        """

        fake = FakeHid()
        seen: list[tuple[int, bool]] = []
        original = fake.read

        def recording(length, timeout_ms=0):
            seen.append((timeout_ms, fake.nonblocking))
            return original(length, timeout_ms)

        fake.read = recording
        _Opened(fake)._drain()
        self.assertTrue(seen, "the drain should have read at least once")
        for timeout_ms, nonblocking in seen:
            if timeout_ms == 0:
                self.assertTrue(nonblocking, "read(n) on a blocking handle never returns")

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


class AnotherMasterTest(unittest.TestCase):
    """🚨 A reply to a wake/sleep this driver never sent means another process is on it.

    Observed 2026-09-10: CoolerControl picked up a newly installed liquidctl at boot
    and began polling the Commander Core. liquidctl brackets every operation with
    WAKE and SLEEP (``01 03 00 02`` / ``01 03 00 01``); SLEEP is hardware mode, so
    the AIO flickered between our colours and its firmware rainbow every second or
    two, and hidraw -- which has no exclusive open -- handed liquidctl's replies to
    both readers. The daemon logged ``Reply 0x01 does not acknowledge command 0x0d``
    fifty-seven times: true, and useless, because it names neither the cause nor
    the check.
    """

    def _answering_with(self, reply_command: int) -> _Opened:
        fake = FakeHid()
        original = fake.write

        def write(packet):
            result = original(packet)
            fake._next[-1] = bytes([0x00, reply_command]) + bytes(REPORT_LENGTH - 2)
            return result

        fake.write = write
        return _Opened(fake)

    def test_a_stray_wake_or_sleep_reply_names_another_process(self) -> None:
        cc = self._answering_with(0x01)
        with self.assertRaises(CommanderCoreError) as caught:
            cc._send((0x0D, 0x00), (0x22,))
        message = str(caught.exception)
        self.assertIn("does not acknowledge", message)  # existing log greps keep matching
        self.assertIn("another process", message)
        self.assertIn("CoolerControl", message)
        self.assertIn("fuser", message)

    def test_an_ordinary_mismatch_does_not_blame_another_process(self) -> None:
        # Narrow on purpose: blaming a stranger for every bad byte would teach
        # whoever reads the journal to ignore the one time it is true.
        cc = self._answering_with(0xFF)
        with self.assertRaises(CommanderCoreError) as caught:
            cc._send((0x0D, 0x00), (0x22,))
        self.assertNotIn("another process", str(caught.exception))

    def test_our_own_wake_is_not_mistaken_for_a_stranger(self) -> None:
        _Opened(FakeHid()).wake()  # 0x01 acknowledged by 0x01: no error


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class ReadOnlyInspectionTest(unittest.TestCase):
    """⚠️ A diagnostic must not mutate the thing it inspects.

    Exactly one process may hold this device. ``ambience-rgb devices`` opening it
    normally forces fan mode on the way in and writes black on the way out --
    which, with the daemon driving, fought it over the single handle and
    surfaced as ``Reply 0x01 does not acknowledge command 0x07``. Observed for
    real while debugging, from the debugging itself.
    """

    def test_a_read_only_close_does_not_black_the_device(self) -> None:
        fake = FakeHid()
        cc = _Opened(fake)
        cc._read_only = True
        cc.led_counts = [29, 8, 8, 8, None, None, None]
        cc.close()
        self.assertTrue(fake.closed, "the handle must still be released")
        self.assertEqual(
            [p for p in fake.written if p[2] == 0x06], [], "a read-only close must write nothing"
        )

    def test_a_normal_close_still_blacks(self) -> None:
        """The guard must not have disabled the behaviour it guards."""

        fake = FakeHid()
        cc = _Opened(fake)
        cc.led_counts = [29, 8, 8, 8, None, None, None]
        cc.close()
        self.assertTrue([p for p in fake.written if p[2] == 0x06])


# Whether a daemon holds the endpoint -- a stale socket file, a live one, UDP --
# is tested in test_transport.py, alongside the transport that answers it.


class EndpointRetryTest(unittest.TestCase):
    """🚨 A dropped acknowledgement must not take the AIO out of service.

    Under sustained streaming this controller occasionally fails to answer a
    continuation packet inside the read timeout -- measured at ~0.4% of frames,
    one every 45 s at 5 Hz during a twelve-minute soak. Propagated, each miss
    took the sink out for its retry window, which is long enough for the fans to
    revert to firmware green and flash.
    """

    class _FlakyHid(FakeHid):
        """Drops the Nth REPLY once, then behaves.

        Only real reply reads are counted. The drain also calls read(), with no
        timeout on the non-blocking handle, and an empty return there is its
        normal successful result -- counting those made the fake fail somewhere
        unpredictable.
        """

        def __init__(self, fail_on: int) -> None:
            super().__init__()
            self.fail_on = fail_on
            self.replies = 0

        def read(self, length, timeout_ms=0):
            if timeout_ms == 0:  # a drain poll, not a reply read
                return super().read(length, timeout_ms)
            self.replies += 1
            if self.replies == self.fail_on:
                return []  # no reply: exactly what was seen on the wire
            return super().read(length, timeout_ms)

    def test_a_single_dropped_reply_is_retried_not_raised(self) -> None:
        fake = self._FlakyHid(fail_on=2)
        cc = _Opened(fake)
        cc.led_counts = [29, 8, 8, 8, None, None, None]
        cc.set_colour((10, 20, 30), (40, 50, 60))  # must not raise

    def test_it_gives_up_eventually_rather_than_looping(self) -> None:
        class Silent(FakeHid):
            def read(self, length, timeout_ms=0):
                return []

        cc = _Opened(Silent())
        cc.led_counts = [29, 8, 8, 8, None, None, None]
        with self.assertRaises(CommanderCoreError) as caught:
            cc.set_colour((10, 20, 30), (40, 50, 60))
        self.assertIn("attempts", str(caught.exception))

    def test_the_retry_restarts_the_whole_sequence(self) -> None:
        """Resending one packet into a half-finished transaction desyncs it."""

        fake = self._FlakyHid(fail_on=3)
        cc = _Opened(fake)
        cc.led_counts = [29, 8, 8, 8, None, None, None]
        cc.set_colour((1, 2, 3), (4, 5, 6))
        opens = [p for p in fake.written if p[2] == 0x0D]
        self.assertGreaterEqual(len(opens), 2, "a retry must re-open the endpoint")


class ReadTimeoutBudgetTest(unittest.TestCase):
    """🚨 The reply timeout is a latency budget for the WHOLE daemon.

    The loop is single-threaded, so a failed exchange spends the entire timeout
    with the OpenRGB sink waiting behind it. At 1000 ms one bad frame stalled the
    loop for three seconds across the retries and made the motherboard miss its
    colour as well -- a fault in one device surfacing as a fault in another.

    Measured on this rig over 2020 exchanges: p50 3.0 ms, p99 3.0 ms, max 49 ms.
    """

    def test_the_timeout_is_generous_but_not_a_stall(self) -> None:
        from ambience_rgb.commander_core import _READ_TIMEOUT_MS

        self.assertGreaterEqual(_READ_TIMEOUT_MS, 100, "must clear the 49 ms worst case")
        self.assertLessEqual(
            _READ_TIMEOUT_MS, 300, "a longer wait starves the other sink on every miss"
        )

    def test_a_whole_frame_of_failures_stays_under_a_second(self) -> None:
        """Three attempts x ten packets must not add up to a visible stall."""

        from ambience_rgb.commander_core import _READ_TIMEOUT_MS

        worst_case_seconds = 3 * _READ_TIMEOUT_MS / 1000
        self.assertLess(worst_case_seconds, 1.0)


class ReleaseDoesNotBlackTest(unittest.TestCase):
    """🚨 Letting go of a broken handle must not turn the hardware off.

    close() blacks the device first, which is right for a clean shutdown. The
    recovery path called it too -- so every transient failure explicitly
    darkened the AIO it was trying to rescue, then reopened it. With this
    controller's 0xF0 bursts arriving every ~25 s, the device spent its life
    being blacked, and the owner reported simply that "the commander doesn't
    light up".
    """

    def test_release_writes_nothing(self) -> None:
        fake = FakeHid()
        cc = _Opened(fake)
        cc.led_counts = [29, 8, 8, 8, None, None, None]
        fake.written.clear()
        cc.release()
        self.assertTrue(fake.closed, "the handle must still be dropped")
        self.assertEqual(fake.written, [], "a release must put nothing on the wire")

    def test_release_is_idempotent(self) -> None:
        fake = FakeHid()
        cc = _Opened(fake)
        cc.release()
        cc.release()  # must not raise

    def test_close_still_blacks(self) -> None:
        """The guard must not have disabled the behaviour it guards."""

        fake = FakeHid()
        cc = _Opened(fake)
        cc.led_counts = [29, 8, 8, 8, None, None, None]
        fake.written.clear()
        cc.close()
        self.assertTrue([p for p in fake.written if p[2] == 0x06], "close() must write black")
