"""Byte-exact tests for the Commander Core wire format.

These matter more than the usual protocol test. The lighting endpoint is
write-only: a wrong offset produces no error, no log line and no failed call --
just the wrong LEDs lit, or none. The device cannot tell us we are wrong, so
these assertions are the only check that exists.

Expected values are taken from the two reference implementations (liquidctl's
Python framing and OpenRGB's CorsairCommanderCoreController) and from the real
device's own reported topology: pump 29, ports 1-3 with 8 each,
ports 4-6 empty.
"""

from __future__ import annotations

import unittest

from ambience_rgb.commander_protocol import (
    CMD_CLOSE_ENDPOINT,
    CMD_OPEN_ENDPOINT,
    CMD_WRITE,
    CMD_WRITE_MORE,
    COLOUR_BUFFER_LENGTH,
    DATA_TYPE_DIRECT_COLOUR,
    ENDPOINT_DIRECT_COLOUR,
    PACKET_LENGTH,
    REPORT_LENGTH,
    PORT_SLOTS,
    PUMP_LEDS,
    colour_buffer,
    command_packet,
    endpoint_write_packets,
    fan_mode_payload,
    parse_led_counts,
)

RED = (255, 0, 0)
GREEN = (0, 255, 0)
BLUE = (0, 0, 255)
BLACK = (0, 0, 0)


class CommandPacketTest(unittest.TestCase):
    def test_envelope_layout(self) -> None:
        packet = command_packet((0x06, 0x00), b"\xaa\xbb")
        self.assertEqual(len(packet), PACKET_LENGTH)
        self.assertEqual(packet[0], 0x00, "byte 0 is the hidapi report id")
        self.assertEqual(packet[1], 0x08, "byte 1 is always 0x08 outbound")
        self.assertEqual(packet[2:4], b"\x06\x00", "command at offset 2")
        self.assertEqual(packet[4:6], b"\xaa\xbb", "payload at offset 4")
        self.assertEqual(packet[6:], bytes(PACKET_LENGTH - 6), "rest zero-filled")

    def test_three_byte_commands_shift_the_payload(self) -> None:
        """CMD_READ_* are three bytes, so their payload starts at 5, not 4."""

        packet = command_packet((0x08, 0x00, 0x01), b"\xff")
        self.assertEqual(packet[2:5], b"\x08\x00\x01")
        self.assertEqual(packet[5], 0xFF)


class EndpointWriteTest(unittest.TestCase):
    def test_sequence_is_open_write_close(self) -> None:
        packets = endpoint_write_packets((0x22,), (0x12, 0x00), b"\x01\x02\x03")
        self.assertEqual(packets[0][2:4], bytes(CMD_OPEN_ENDPOINT))
        self.assertEqual(packets[0][4], 0x22, "endpoint id is the open payload")
        self.assertEqual(packets[1][2:4], bytes(CMD_WRITE))
        self.assertEqual(packets[-1][2:4], bytes(CMD_CLOSE_ENDPOINT))

    def test_first_packet_header(self) -> None:
        """Declared length counts the data-type bytes, per both references."""

        data = b"\x01\x02\x03"
        packets = endpoint_write_packets((0x22,), (0x12, 0x00), data)
        body = packets[1][4:]
        self.assertEqual(
            int.from_bytes(body[0:2], "little"), len(data) + 2, "len includes data type"
        )
        self.assertEqual(body[2:4], b"\x00\x00")
        self.assertEqual(body[4:6], b"\x12\x00", "data type at offset 4 of the body")
        self.assertEqual(body[6:9], data)

    def test_full_colour_buffer_chunking(self) -> None:
        """699 bytes across 64-byte reports: 55 in the first write, then 61 each.

        The packet COUNT depends on REPORT_LENGTH, and getting that wrong is
        invisible from the transport: the command header survives truncation, so
        every packet is still acknowledged while the colour data is cut off and
        the LEDs stay dark. This device's reports are 64 bytes -- see the note on
        REPORT_LENGTH.
        """

        packets = endpoint_write_packets(
            ENDPOINT_DIRECT_COLOUR, DATA_TYPE_DIRECT_COLOUR, bytes(COLOUR_BUFFER_LENGTH)
        )
        expected_more = -(-(COLOUR_BUFFER_LENGTH - (REPORT_LENGTH - 9)) // (REPORT_LENGTH - 3))
        kinds = [tuple(p[2:4]) for p in packets]
        self.assertEqual(kinds[0], CMD_OPEN_ENDPOINT)
        self.assertEqual(kinds[1], CMD_WRITE)
        self.assertEqual(kinds[-1], CMD_CLOSE_ENDPOINT)
        self.assertTrue(
            all(len(packet) == PACKET_LENGTH for packet in packets),
            "every report must be exactly the size the descriptor declares",
        )
        self.assertEqual(
            kinds[2:-1],
            [CMD_WRITE_MORE] * expected_more,
            f"{COLOUR_BUFFER_LENGTH - (REPORT_LENGTH - 9)} bytes after the first packet"
        )
        self.assertEqual(len(packets), 2 + expected_more + 1, "open + write + continuations + close")

    def test_every_packet_is_report_sized(self) -> None:
        packets = endpoint_write_packets(
            ENDPOINT_DIRECT_COLOUR, DATA_TYPE_DIRECT_COLOUR, bytes(COLOUR_BUFFER_LENGTH)
        )
        for index, packet in enumerate(packets):
            with self.subTest(packet=index):
                self.assertEqual(len(packet), PACKET_LENGTH)

    def test_no_data_is_dropped_across_the_split(self) -> None:
        """Reassemble the payload from the packets and compare to the source."""

        data = bytes((i % 251) for i in range(COLOUR_BUFFER_LENGTH))
        packets = endpoint_write_packets(ENDPOINT_DIRECT_COLOUR, DATA_TYPE_DIRECT_COLOUR, data)
        rebuilt = packets[1][4 + 6 :]           # first packet, past its 6-byte header
        rebuilt = rebuilt[: 96 - 9]
        for packet in packets[2:-1]:
            rebuilt += packet[4:][: 96 - 3]
        self.assertEqual(rebuilt[: len(data)], data)


class ColourBufferTest(unittest.TestCase):
    def test_length_is_exactly_699(self) -> None:
        self.assertEqual(COLOUR_BUFFER_LENGTH, 699)
        self.assertEqual(len(colour_buffer([], [])), 699)

    def test_pump_occupies_the_first_87_bytes(self) -> None:
        buf = colour_buffer([RED] * PUMP_LEDS, [])
        self.assertEqual(buf[0:87], bytes([255, 0, 0] * PUMP_LEDS))
        self.assertEqual(buf[87:], bytes(699 - 87), "ports untouched")

    def test_each_port_starts_on_its_own_34_slot_boundary(self) -> None:
        ports = [[GREEN], [BLUE], [RED], [], [], []]
        buf = colour_buffer([], ports)
        base = PUMP_LEDS * 3
        self.assertEqual(buf[base : base + 3], bytes(GREEN), "port 1 at 87")
        self.assertEqual(buf[base + 102 : base + 105], bytes(BLUE), "port 2 at 189")
        self.assertEqual(buf[base + 204 : base + 207], bytes(RED), "port 3 at 291")

    def test_unused_slots_in_a_port_stay_black(self) -> None:
        """Fans have 8 LEDs in a 34-slot region; 9..34 must be black."""

        buf = colour_buffer([], [[RED] * 8, [], [], [], [], []])
        base = PUMP_LEDS * 3
        self.assertEqual(buf[base : base + 24], bytes([255, 0, 0] * 8))
        self.assertEqual(
            buf[base + 24 : base + PORT_SLOTS * 3], bytes(PORT_SLOTS * 3 - 24)
        )

    def test_a_long_port_is_truncated_not_overrun(self) -> None:
        """Overrun would silently colour the next port -- looks like miswiring."""

        buf = colour_buffer([], [[RED] * 50, [GREEN], [], [], [], []])
        base = PUMP_LEDS * 3
        port2 = base + PORT_SLOTS * 3
        self.assertEqual(buf[port2 : port2 + 3], bytes(GREEN), "port 2 intact")
        self.assertEqual(len(buf), COLOUR_BUFFER_LENGTH)

    def test_a_long_pump_list_is_truncated(self) -> None:
        buf = colour_buffer([RED] * 100, [[GREEN], [], [], [], [], []])
        base = PUMP_LEDS * 3
        self.assertEqual(buf[base : base + 3], bytes(GREEN), "port 1 not overwritten")

    def test_more_than_six_ports_is_ignored(self) -> None:
        buf = colour_buffer([], [[RED]] * 9)
        self.assertEqual(len(buf), COLOUR_BUFFER_LENGTH)


class FanModeTest(unittest.TestCase):
    def test_matches_openrgb_byte_for_byte(self) -> None:
        expected = bytes(
            [0x07, 0x01, 0x08, 0x01, 0x06, 0x01, 0x06, 0x01, 0x06, 0x01, 0x06, 0x01, 0x06, 0x01, 0x06]
        )
        self.assertEqual(fan_mode_payload(), expected)
        self.assertEqual(len(fan_mode_payload()), 15)


class LedCountTest(unittest.TestCase):
    def _channel(self, connected: int, leds: int) -> bytes:
        return connected.to_bytes(2, "little") + leds.to_bytes(2, "little")

    def test_this_rig_topology(self) -> None:
        """Pump 29, ports 1-3 with 8 LEDs, ports 4-6 empty.

        Cross-checked against liquidctl, which reports exactly this for the
        real device and is a wholly independent implementation.
        """

        data = bytes([7])
        data += self._channel(2, 29)
        data += self._channel(2, 8) * 3
        data += self._channel(1, 0) * 3
        self.assertEqual(parse_led_counts(data), [29, 8, 8, 8, None, None, None])

    def test_unconnected_is_none_not_zero(self) -> None:
        data = bytes([1]) + self._channel(1, 0)
        self.assertEqual(parse_led_counts(data), [None])

    def test_truncated_response_does_not_raise(self) -> None:
        self.assertEqual(parse_led_counts(b""), [])
        self.assertEqual(parse_led_counts(bytes([4]) + self._channel(2, 8)), [8])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
