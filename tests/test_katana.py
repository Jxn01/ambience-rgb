"""The Katana transport, against a simulated soundbar.

``FakeSoundbar`` plays the device's side of the conversation. It is written from
the wire format directly -- deliberately *not* from ``katana_protocol`` -- so a
wrong byte cannot hide by being wrong in both places, and it checks the unlock
with real AES-GCM under the reference's key: it says ``unlock_OK`` only to a
response that genuinely decrypts to its nonce.

The behaviours it models were taken from the reference and from the device
itself: on 2026-09-10 the real soundbar, in text mode, answered a binary probe
with complete silence, and that is what the fake does too.
"""

from __future__ import annotations

import logging
import unittest

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ambience_rgb.katana import Katana, KatanaError, find_port

V2X_KEY = bytes.fromhex(
    "1e04d31a21279be346f0999d6ec4c3fe" "be98901869c118fbb1256e0ce07b8332"
)
FIRMWARE = b"1.3.230619.1820"
SLICE = 0.05


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class FakeSoundbar:
    """The pyserial surface the driver uses, with a soundbar behind it."""

    def __init__(
        self,
        clock: Clock,
        *,
        mode: str = "text",
        locked: bool = True,
        not_yet: int = 0,
        nonce: bytes = bytes(range(100, 132)),
        before_challenge: bytes = b"",
        accepts_unlock: bool = True,
        switches_mode: bool = True,
        ack_setters: bool = False,
        deaf: bool = False,
    ) -> None:
        self.clock = clock
        self.mode = mode
        self.locked = locked
        self.not_yet = not_yet
        self.nonce = nonce
        self.before_challenge = before_challenge
        self.accepts_unlock = accepts_unlock
        self.switches_mode = switches_mode
        self.ack_setters = ack_setters
        self.deaf = deaf
        self.pending = bytearray()
        self.written: list[bytes] = []
        self.colour = (1, 2, 3)
        self.power: bool | None = None
        self.effect: int | None = None
        self.count: int | None = None
        self.closed = False
        # Sent ahead of the next query reply: a frame arriving after the
        # question and before its answer, which is the race that matters.
        self.interject = b""

    # --- pyserial ---------------------------------------------------------
    @property
    def in_waiting(self) -> int:
        return len(self.pending)

    def read(self, size: int = 1) -> bytes:
        if not self.pending:
            self.clock.now += SLICE  # a real read blocks for the port timeout
            return b""
        chunk = bytes(self.pending[:size])
        del self.pending[:size]
        return chunk

    def write(self, data: bytes) -> int:
        data = bytes(data)
        self.written.append(data)
        if not self.deaf:
            if self.mode == "binary":
                self._hear_frame(data)
            else:
                self._hear_text(data)
        return len(data)

    def reset_input_buffer(self) -> None:
        self.pending.clear()

    def close(self) -> None:
        self.closed = True

    # --- the device -------------------------------------------------------
    def announce(self, frame: bytes) -> None:
        """An unsolicited frame, as the device sends status changes."""
        self.pending += frame

    def _hear_text(self, data: bytes) -> None:
        if data == b"whoareyou\r\n":
            if not self.locked:
                self.pending += b"Unknown command\r\n"
            elif self.not_yet:
                self.not_yet -= 1
                self.pending += b"NotYet\r\n"
            else:
                self.pending += (
                    self.before_challenge + b"whoareyou" + bytes([0x1E, 0x04, 0x83, 0x32])
                    + self.nonce + b"\r\n"
                )
        elif data.startswith(b"unlock") and len(data) == 6 + 64 + 2 and data.endswith(b"\r\n"):
            response = data[6:70]
            try:
                opened = AESGCM(V2X_KEY).decrypt(response[:12], response[16:], None)
            except InvalidTag:
                opened = None
            if opened == self.nonce and self.accepts_unlock:
                self.locked = False
                self.pending += b"unlock_OK\r\n"
            else:
                self.pending += b"unlock_NG\r\n"
        elif data == b"SW_MODE1\r\n" and not self.locked and self.switches_mode:
            self.mode = "binary"
        # Everything else -- binary frames included -- meets silence.

    def _hear_frame(self, data: bytes) -> None:
        frame = data.removesuffix(b"\r\n")  # the probe's CRLF is ignored here
        if frame == bytes([0x5A, 0x09, 0x01, 0x02]):
            self.pending += self.interject
            self.interject = b""
            self.pending += bytes([0x5A, 0x09, 0x12, 0x02, 0x10]) + FIRMWARE + b"\x00"
            return
        if frame == bytes([0x5A, 0x3A, 0x05, 0x2C, 0x00, 0x01, 0x01, 0x07]):
            self.pending += self.interject
            self.interject = b""
            red, green, blue = self.colour
            self.pending += (
                bytes([0x5A, 0x3A, 0x20, 0x2C, 0x00, 0x01, 0x01, 0xFF, blue, green, red])
                + b"\xff" * 24
            )
            return
        head = frame[:4]
        if head == bytes([0x5A, 0x3A, 0x08, 0x2B]):
            self.colour = (frame[10], frame[9], frame[8])
        elif head == bytes([0x5A, 0x3A, 0x02, 0x25]):
            self.power = frame[4] == 0x01
        elif head == bytes([0x5A, 0x3A, 0x03, 0x29]):
            self.effect = frame[5]
        elif head == bytes([0x5A, 0x3A, 0x03, 0x37]):
            self.count = frame[5]
        else:
            return
        if self.ack_setters:
            # The shape the real soundbar answered a solo colour with, first
            # contact 2026-09-10: 5A 02 0A, the opcode, status 00, the selector.
            selector = frame[3:7].ljust(4, b"\x00")
            self.pending += bytes([0x5A, 0x02, 0x0A, frame[1], 0x00]) + selector + bytes([1, 0, 0, 0])


def _katana(fake: FakeSoundbar, **options) -> Katana:
    return Katana(
        path="/dev/fake",
        logger=logging.getLogger("test"),
        port_factory=lambda path: fake,
        clock=fake.clock,
        sleep=fake.clock.sleep,
        **options,
    )


def _whoareyous(fake: FakeSoundbar) -> int:
    return sum(1 for data in fake.written if data == b"whoareyou\r\n")


class HandshakeTest(unittest.TestCase):
    def test_a_locked_soundbar_is_unlocked_and_switched_to_binary_mode(self) -> None:
        fake = FakeSoundbar(Clock())
        katana = _katana(fake).open()
        self.assertFalse(fake.locked)
        self.assertEqual(fake.mode, "binary")
        self.assertEqual(katana.firmware, "1.3.230619.1820")

    def test_a_soundbar_already_in_binary_mode_is_not_asked_who_it_is(self) -> None:
        # The normal case after the first handshake: the soundbar has its own
        # power supply, so it stays in binary mode across PC reboots.
        fake = FakeSoundbar(Clock(), mode="binary", locked=False)
        katana = _katana(fake).open()
        self.assertEqual(_whoareyous(fake), 0)
        self.assertEqual(katana.firmware, "1.3.230619.1820")

    def test_an_unlocked_soundbar_in_text_mode_goes_straight_to_binary(self) -> None:
        fake = FakeSoundbar(Clock(), locked=False)
        _katana(fake).open()
        self.assertEqual(fake.mode, "binary")
        self.assertFalse(any(data.startswith(b"unlock") for data in fake.written))

    def test_notyet_is_waited_out(self) -> None:
        fake = FakeSoundbar(Clock(), not_yet=2)
        _katana(fake).open()
        self.assertEqual(_whoareyous(fake), 3)
        self.assertEqual(fake.mode, "binary")

    def test_notyet_forever_gives_up_and_says_why(self) -> None:
        fake = FakeSoundbar(Clock(), not_yet=99)
        with self.assertRaisesRegex(KatanaError, "NotYet"):
            _katana(fake).open()
        self.assertTrue(fake.closed)

    def test_with_no_retries_notyet_fails_at_once_without_sleeping(self) -> None:
        # The daemon's setting. Its tick must never sleep: ResilientSink's own
        # retry is the wait, and every other sink keeps running meanwhile.
        clock = Clock()
        fake = FakeSoundbar(clock, not_yet=1)
        with self.assertRaisesRegex(KatanaError, "NotYet"):
            _katana(fake, not_yet_retries=0).open()
        self.assertEqual(clock.slept, [], "the daemon's tick slept")
        self.assertTrue(fake.closed)

    def test_a_nonce_containing_crlf_still_unlocks(self) -> None:
        # 🚨 The reference reads the challenge as a line, so this nonce cuts it
        # short. Its NotYet retry happens to paper over that; ours never needs to.
        fake = FakeSoundbar(Clock(), nonce=bytes(10) + b"\r\n" + bytes(20))
        _katana(fake).open()
        self.assertFalse(fake.locked)
        self.assertEqual(_whoareyous(fake), 1)

    def test_a_status_frame_ahead_of_the_challenge_is_skipped(self) -> None:
        fake = FakeSoundbar(Clock(), before_challenge=bytes([0x5A, 0x30, 0x02, 0x01, 0x00]) + b"\r\n")
        _katana(fake).open()
        self.assertFalse(fake.locked)

    def test_a_refused_unlock_says_so_and_lets_go(self) -> None:
        fake = FakeSoundbar(Clock(), accepts_unlock=False)
        with self.assertRaisesRegex(KatanaError, "unlock_NG"):
            _katana(fake).open()
        self.assertTrue(fake.closed)

    def test_silence_to_whoareyou_is_an_error_not_a_hang(self) -> None:
        fake = FakeSoundbar(Clock(), deaf=True)
        with self.assertRaisesRegex(KatanaError, "whoareyou"):
            _katana(fake).open()
        self.assertTrue(fake.closed)

    def test_a_soundbar_that_ignores_sw_mode1_is_reported(self) -> None:
        fake = FakeSoundbar(Clock(), switches_mode=False)
        with self.assertRaisesRegex(KatanaError, "SW_MODE1"):
            _katana(fake).open()
        self.assertTrue(fake.closed)


class BinaryModeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeSoundbar(Clock(), mode="binary", locked=False)
        self.katana = _katana(self.fake).open()

    def test_prepare_turns_the_lights_on_in_solo_mode(self) -> None:
        self.katana.prepare()
        self.assertEqual((self.fake.power, self.fake.effect, self.fake.count), (True, 0x03, 1))

    def test_a_colour_reaches_the_soundbar(self) -> None:
        self.katana.set_colour((10, 20, 30))
        self.assertEqual(self.fake.colour, (10, 20, 30))

    def test_the_colour_reads_back(self) -> None:
        self.fake.colour = (200, 100, 50)
        self.assertEqual(self.katana.read_colour(), (200, 100, 50))

    def test_an_unrelated_frame_is_not_taken_for_the_reply(self) -> None:
        # Arriving between the question and its answer: a firmware reply and a
        # speed reply, which shares the colour query's sub-command.
        self.fake.colour = (7, 8, 9)
        self.fake.interject = (
            bytes([0x5A, 0x09, 0x12, 0x02, 0x10]) + bytes(16)
            + bytes([0x5A, 0x3A, 0x05, 0x2C, 0x00, 0x03, 0x10, 0x27])
        )
        self.assertEqual(self.katana.read_colour(), (7, 8, 9))

    def test_a_frame_left_over_from_before_is_not_taken_for_the_reply(self) -> None:
        self.fake.colour = (7, 8, 9)
        self.fake.announce(bytes([0x5A, 0x3A, 0x20, 0x2C, 0x00, 0x01, 0x01]) + bytes(28))
        self.assertEqual(self.katana.read_colour(), (7, 8, 9))

    def test_an_unanswered_query_raises(self) -> None:
        self.fake.deaf = True
        with self.assertRaises(KatanaError):
            self.katana.read_colour()

    def test_replies_to_writes_are_drained_as_they_come(self) -> None:
        # The real soundbar acknowledges every setter with a 13-byte frame, and
        # nothing waits for those. Left unread they pile up in the host's
        # buffer -- at 2 Hz a 4 KiB tty buffer is full within minutes -- and a
        # device blocked on a full IN pipe can stall. So every write takes in
        # whatever is waiting.
        self.fake.ack_setters = True
        for step in range(500):
            self.katana.set_colour((step % 256, 0, 0))
        self.assertLessEqual(self.fake.in_waiting, 13)

    def test_keepalive_is_satisfied_by_an_answer(self) -> None:
        self.katana.keepalive()
        self.katana.keepalive()
        self.katana.keepalive()

    def test_keepalive_notices_a_soundbar_that_stopped_listening(self) -> None:
        # 🚨 Power-cycled with USB still up, the soundbar is back in text mode --
        # and text mode ignores binary frames in silence. Every write would
        # "succeed" into a void, forever. Only an unanswered question shows it.
        self.katana.keepalive()
        self.fake.mode = "text"
        self.katana.keepalive()
        with self.assertRaisesRegex(KatanaError, "stopped answering"):
            self.katana.keepalive()

    def test_close_blacks_then_lets_go(self) -> None:
        self.katana.close()
        self.assertEqual(self.fake.colour, (0, 0, 0))
        self.assertTrue(self.fake.closed)

    def test_release_lets_go_without_writing(self) -> None:
        # See CommanderCore.release: a recovery path must not darken the
        # hardware it is trying to rescue.
        self.fake.colour = (9, 9, 9)
        writes = len(self.fake.written)
        self.katana.release()
        self.assertEqual(len(self.fake.written), writes)
        self.assertTrue(self.fake.closed)

    def test_a_read_only_open_never_blacks(self) -> None:
        fake = FakeSoundbar(Clock(), mode="binary", locked=False)
        fake.colour = (9, 9, 9)
        _katana(fake, read_only=True).open().close()
        self.assertEqual(fake.colour, (9, 9, 9))
        self.assertTrue(fake.closed)


class _Port:
    def __init__(self, device: str, vid: int | None, pid: int | None) -> None:
        self.device, self.vid, self.pid = device, vid, pid


class FindPortTest(unittest.TestCase):
    def test_the_katana_is_found_by_its_usb_ids(self) -> None:
        ports = [_Port("/dev/ttyACM0", 0x2341, 0x0043), _Port("/dev/ttyACM1", 0x041E, 0x3283)]
        self.assertEqual(find_port(lambda: ports), ("/dev/ttyACM1", 0x3283))

    def test_the_v2_is_found_too(self) -> None:
        self.assertEqual(find_port(lambda: [_Port("/dev/ttyACM0", 0x041E, 0x3260)]), ("/dev/ttyACM0", 0x3260))

    def test_another_acm_device_is_never_tried(self) -> None:
        # The reference falls back to ANY ttyACM and sends it "whoareyou". That
        # could be an Arduino, a modem or a 3D printer. Refuse instead.
        with self.assertRaisesRegex(KatanaError, "041e:3283"):
            find_port(lambda: [_Port("/dev/ttyACM0", 0x2341, 0x0043)])

    def test_nothing_at_all(self) -> None:
        with self.assertRaises(KatanaError):
            find_port(lambda: [])


if __name__ == "__main__":
    unittest.main()
