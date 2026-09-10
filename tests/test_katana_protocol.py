"""The Katana V2X wire protocol, every byte checked against the reference.

The reference is the MIT-licensed ``v2x`` crate, whose values come from USB
captures of Creative's own Windows app on firmware ``1.3.230619.1820`` -- the
version this rig's soundbar runs. Every expected value below is copied from that
crate's source or from the published write-up, never derived from our code.

The one that matters most is ``test_the_captured_pair_decrypts``: a real
challenge and the response Creative's app sent to it. If the key is wrong by a
single byte, that test fails before the device is ever touched -- which is the
only safe place for an authentication bug to surface, because a device that
rejects the unlock gives no hint why.
"""

from __future__ import annotations

import unittest

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ambience_rgb import katana_protocol as kp

# From auth.rs `key_construction`: header 1E 04, PID 83 32 (0x3283 little-endian).
V2X_KEY = bytes.fromhex(
    "1e04d31a21279be346f0999d6ec4c3fe" "be98901869c118fbb1256e0ce07b8332"
)
HEADER = bytes([0x1E, 0x04])
PID = bytes([0x83, 0x32])


class FrameBuilderTest(unittest.TestCase):
    """Each outgoing frame, against the reference's literal byte arrays."""

    def test_lighting_on_and_off(self) -> None:
        self.assertEqual(kp.lighting_power(True), bytes([0x5A, 0x3A, 0x02, 0x25, 0x01]))
        self.assertEqual(kp.lighting_power(False), bytes([0x5A, 0x3A, 0x02, 0x25, 0x00]))

    def test_colour_count(self) -> None:
        self.assertEqual(kp.colour_count(1), bytes([0x5A, 0x3A, 0x03, 0x37, 0x00, 0x01]))

    def test_solo_mode_is_mood_with_one_colour(self) -> None:
        # lighting.rs: "Solo is Mood (0x03) with color_count=1".
        self.assertEqual(kp.solo_mode(), bytes([0x5A, 0x3A, 0x03, 0x29, 0x00, 0x03]))

    def test_solo_colour_goes_on_the_wire_as_abgr(self) -> None:
        # color.rs `to_abgr`: [0xFF, b, g, r]. RGB order here would light the
        # wrong colour with no error -- exactly the GRB lesson from the board.
        self.assertEqual(
            kp.solo_colour((0x11, 0x22, 0x33)),
            bytes([0x5A, 0x3A, 0x08, 0x2B, 0x00, 0x01, 0x01, 0xFF, 0x33, 0x22, 0x11]),
        )

    def test_ping(self) -> None:
        self.assertEqual(kp.ping(), bytes([0x5A, 0x03, 0x00]))

    def test_firmware_version_query(self) -> None:
        # The write-up's own example: "Host -> Device: 5a 09 01 02".
        self.assertEqual(kp.firmware_version_query(), bytes([0x5A, 0x09, 0x01, 0x02]))

    def test_colour_query(self) -> None:
        self.assertEqual(
            kp.colour_query(), bytes([0x5A, 0x3A, 0x05, 0x2C, 0x00, 0x01, 0x01, 0x07])
        )

    def test_every_frame_declares_its_own_length(self) -> None:
        for frame in (
            kp.lighting_power(True), kp.colour_count(1), kp.solo_mode(),
            kp.solo_colour((1, 2, 3)), kp.ping(), kp.firmware_version_query(),
            kp.colour_query(),
        ):
            with self.subTest(frame=frame.hex()):
                self.assertEqual(frame[0], 0x5A)
                self.assertEqual(len(frame), 3 + frame[2])

    def test_out_of_range_channels_are_refused_not_wrapped(self) -> None:
        with self.assertRaises(ValueError):
            kp.solo_colour((256, 0, 0))


class ScopeTest(unittest.TestCase):
    """🚨 The reference can flash firmware and poke memory. This port cannot.

    The crate also implements firmware upload (the ``5B`` marker, opcode
    ``0x53``) and memory read/write/exec (``0x54``, patched firmware). None of
    that has any business in a lighting daemon, so it does not exist here --
    checked, rather than merely left out, so a future edit cannot quietly add it.
    """

    def test_no_firmware_or_memory_builders_exist(self) -> None:
        public = {name for name in dir(kp) if not name.startswith("_")}
        for forbidden in ("fw_setup", "fw_data", "fw_apply", "fw_validate",
                          "mem_read", "mem_write", "mem_exec", "upload_firmware"):
            self.assertNotIn(forbidden, public)

    def test_no_builder_emits_a_dangerous_opcode(self) -> None:
        for frame in (
            kp.lighting_power(True), kp.colour_count(1), kp.solo_mode(),
            kp.solo_colour((1, 2, 3)), kp.ping(), kp.firmware_version_query(),
            kp.colour_query(),
        ):
            self.assertNotEqual(frame[0], 0x5B)
            self.assertNotIn(frame[1], (0x53, 0x54))


class AuthTest(unittest.TestCase):
    def test_the_key_matches_the_reference(self) -> None:
        self.assertEqual(kp.build_key(HEADER, PID), V2X_KEY)

    def test_the_captured_pair_decrypts(self) -> None:
        # auth.rs `verify_known_pair`: a nonce the device sent and the response
        # Creative's app answered with, from a real capture.
        nonce = bytes.fromhex("490e1ebfc0d2d397040ea4d5b4d1d3d351895194a4b4b55f51a98624b49786a6")
        response = bytes.fromhex(
            "c9c930342e1cf00f935360bfbb6d92e51c0e185b3559828f9c393ae8c99d34b0"
            "ab40ff92ab656f5e816374c41f92ff230a585c813880eb3bd971cbd035d9b9c2"
        )
        key = kp.build_key(HEADER, PID)
        self.assertEqual(AESGCM(key).decrypt(response[:12], response[16:64], None), nonce)

    def test_our_response_uses_the_same_scheme(self) -> None:
        nonce = bytes(range(32))
        iv = bytes(range(100, 116))
        challenge = kp.Challenge(header=HEADER, pid=PID, nonce=nonce)
        response = kp.unlock_response(challenge, iv=iv)
        self.assertEqual(len(response), 64)
        self.assertEqual(response[:16], iv)
        self.assertEqual(AESGCM(V2X_KEY).decrypt(iv[:12], response[16:], None), nonce)

    def test_the_unlock_line_is_framed(self) -> None:
        challenge = kp.Challenge(header=HEADER, pid=PID, nonce=bytes(32))
        line = kp.unlock_line(challenge, iv=bytes(16))
        self.assertTrue(line.startswith(b"unlock"))
        self.assertTrue(line.endswith(b"\r\n"))
        self.assertEqual(len(line), len(b"unlock") + 64 + 2)

    def test_a_fresh_iv_every_time(self) -> None:
        challenge = kp.Challenge(header=HEADER, pid=PID, nonce=bytes(32))
        self.assertNotEqual(kp.unlock_response(challenge)[:16], kp.unlock_response(challenge)[:16])

    def test_a_generated_response_never_contains_a_line_break(self) -> None:
        # The unlock travels on a text-mode line. Whether the firmware reads it by
        # length or by line is unknown, and a random 64-byte response holds a CR
        # or LF about 40% of the time. Any IV is valid, so choosing one without
        # them costs nothing -- and the auth path runs once per soundbar power
        # cycle, so an intermittent failure there would be very hard to catch.
        challenge = kp.Challenge(header=HEADER, pid=PID, nonce=bytes(range(32)))
        for _ in range(200):
            response = kp.unlock_response(challenge)
            self.assertNotIn(b"\r", response)
            self.assertNotIn(b"\n", response)


class ChallengeTest(unittest.TestCase):
    NONCE = bytes(range(200, 232))

    def _line(self, nonce: bytes) -> bytes:
        return b"whoareyou" + HEADER + PID + nonce + b"\r\n"

    def test_it_parses(self) -> None:
        challenge = kp.parse_challenge(self._line(self.NONCE))
        self.assertEqual((challenge.header, challenge.pid, challenge.nonce), (HEADER, PID, self.NONCE))

    def test_a_nonce_containing_crlf_still_parses(self) -> None:
        # 🚨 The reference reads the challenge as a LINE, but the nonce is 32
        # random binary bytes: when they contain \\r\\n the line is cut short and
        # the unlock fails. Parsing by length does not care.
        nonce = bytes(10) + b"\r\n" + bytes(20)
        self.assertEqual(kp.parse_challenge(self._line(nonce)).nonce, nonce)

    def test_the_challenge_length_is_fixed(self) -> None:
        self.assertEqual(kp.CHALLENGE_LENGTH, len(self._line(self.NONCE)))

    def test_a_short_challenge_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            kp.parse_challenge(b"whoareyou" + HEADER + PID + bytes(8) + b"\r\n")

    def test_the_challenge_names_the_model(self) -> None:
        self.assertEqual(kp.model_for_pid(0x3283), "Sound Blaster Katana V2X")
        self.assertEqual(kp.model_for_pid(0x3260), "Sound Blaster Katana V2")
        self.assertIsNone(kp.model_for_pid(0x1234))


class FrameReaderTest(unittest.TestCase):
    def test_one_frame(self) -> None:
        reader = kp.FrameReader()
        reader.feed(bytes([0x5A, 0x03, 0x00]))
        self.assertEqual(reader.frames(), [bytes([0x5A, 0x03, 0x00])])

    def test_a_frame_split_across_reads_waits_for_the_rest(self) -> None:
        reader = kp.FrameReader()
        reader.feed(bytes([0x5A, 0x3A, 0x03, 0x38]))
        self.assertEqual(reader.frames(), [])
        reader.feed(bytes([0x00, 0x01]))
        self.assertEqual(reader.frames(), [bytes([0x5A, 0x3A, 0x03, 0x38, 0x00, 0x01])])

    def test_two_frames_in_one_read(self) -> None:
        reader = kp.FrameReader()
        reader.feed(bytes([0x5A, 0x03, 0x00, 0x5A, 0x03, 0x00]))
        self.assertEqual(len(reader.frames()), 2)

    def test_stray_bytes_between_frames_are_skipped(self) -> None:
        reader = kp.FrameReader()
        reader.feed(b"\r\n" + bytes([0x5A, 0x03, 0x00]))
        self.assertEqual(reader.frames(), [bytes([0x5A, 0x03, 0x00])])

    def test_a_5b_frame_is_consumed_whole_by_its_u16_length(self) -> None:
        # Never sent by us, but the device may emit one; it must not be mistaken
        # for the start of a 5A frame partway through its body.
        reader = kp.FrameReader()
        reader.feed(bytes([0x5B, 0x98, 0x03, 0x00, 0x5A, 0x5A, 0x5A]) + bytes([0x5A, 0x03, 0x00]))
        self.assertEqual(
            reader.frames(),
            [bytes([0x5B, 0x98, 0x03, 0x00, 0x5A, 0x5A, 0x5A]), bytes([0x5A, 0x03, 0x00])],
        )


class ReplyTest(unittest.TestCase):
    def test_the_firmware_version_reply_from_the_write_up(self) -> None:
        # "Device -> Host: 5a 09 12 02 10 "1.3.230619.1820\\0""
        frame = bytes([0x5A, 0x09, 0x12, 0x02, 0x10]) + b"1.3.230619.1820\x00"
        self.assertEqual(kp.parse_firmware_version(frame), "1.3.230619.1820")

    def test_colours_come_back_abgr_in_slot_order(self) -> None:
        frame = (
            bytes([0x5A, 0x3A, 0x20, 0x2C, 0x00, 0x01, 0x01])
            + bytes([0xFF, 0x33, 0x22, 0x11]) + bytes([0xFF] * 24)
        )
        colours = kp.parse_colours(frame)
        self.assertEqual(len(colours), 7)
        self.assertEqual(colours[0], (0x11, 0x22, 0x33))

    def test_white_is_a_colour_not_an_empty_slot(self) -> None:
        # 🚨 The reference skips FFFFFFFF as "unused" -- but solo white is
        # FF FF FF FF in ABGR, so that rule reads a white soundbar back as having
        # no colour at all. Slots come back as they are; the caller knows the count.
        frame = bytes([0x5A, 0x3A, 0x20, 0x2C, 0x00, 0x01, 0x01]) + bytes([0xFF] * 28)
        self.assertEqual(kp.parse_colours(frame)[0], (255, 255, 255))

    def test_a_frame_that_is_not_a_colour_reply_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            kp.parse_colours(bytes([0x5A, 0x09, 0x12, 0x02, 0x10]) + bytes(16))

    def test_a_reply_is_paired_to_its_request_not_to_whatever_came_next(self) -> None:
        # Same class as the OpenRGB and Commander Core desyncs: the next frame on
        # the wire is not necessarily the answer to the last question.
        colours = bytes([0x5A, 0x3A, 0x20, 0x2C, 0x00, 0x01, 0x01]) + bytes(28)
        firmware = bytes([0x5A, 0x09, 0x12, 0x02, 0x10]) + bytes(16)
        self.assertTrue(kp.is_reply_to(colours, kp.colour_query()))
        self.assertFalse(kp.is_reply_to(firmware, kp.colour_query()))
        self.assertTrue(kp.is_reply_to(firmware, kp.firmware_version_query()))

    def test_a_different_parameter_query_is_not_a_colour_reply(self) -> None:
        # Colours and speed are both sub-command 0x2C; only the parameter byte
        # tells them apart.
        speed = bytes([0x5A, 0x3A, 0x05, 0x2C, 0x00, 0x03, 0x10, 0x27])
        self.assertFalse(kp.is_reply_to(speed, kp.colour_query()))


if __name__ == "__main__":
    unittest.main()
