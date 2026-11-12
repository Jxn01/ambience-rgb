"""The SDK client's parsing and its mode handling.

Two behaviours are pinned here: the parser must keep the mode table, and
``set_custom_mode`` must not be mistaken for putting hardware into direct mode,
since it sends a packet that never reaches the device.
"""

from __future__ import annotations

import socket
import struct
import unittest

from ambience_rgb.openrgb_client import (
    CMD_DEVICE_LIST_UPDATED,
    CMD_REQUEST_CONTROLLER_COUNT,
    CMD_REQUEST_PROTOCOL_VERSION,
    CMD_SET_CUSTOM_MODE,
    CMD_UPDATE_MODE,
    Controller,
    Mode,
    PROTOCOL_VERSION,
    OpenRGBClient,
    OpenRGBError,
    parse_controller,
)


def _string(text: str) -> bytes:
    raw = text.encode() + b"\x00"
    return struct.pack("<H", len(raw)) + raw


def _mode(name: str, colours: list[int] | None = None) -> bytes:
    colours = colours or []
    return (
        _string(name)
        + struct.pack("<i", 0)
        + struct.pack("<IIIIIIIIIII", *([0] * 11))
        + struct.pack("<H", len(colours))
        + b"".join(struct.pack("<I", c) for c in colours)
    )


def _zone(name: str, led_count: int) -> bytes:
    return (
        _string(name)
        + struct.pack("<i", 0)
        + struct.pack("<III", 0, led_count, led_count)
        + struct.pack("<H", 0)
    )


def build_record(
    name: str = "Test Device",
    modes: list[str] | None = None,
    active_mode: int = 0,
    zones: list[tuple[str, int]] | None = None,
    led_count: int = 4,
) -> bytes:
    """A protocol-3 controller record, laid out exactly as the server sends one."""

    modes = modes if modes is not None else ["Rainbow", "Direct"]
    zones = zones if zones is not None else [("Zone", led_count)]
    body = struct.pack("<i", 0) + _string(name)
    for _ in range(5):  # vendor, description, version, serial, location
        body += _string("")
    body += struct.pack("<Hi", len(modes), active_mode)
    for mode_name in modes:
        body += _mode(mode_name)
    body += struct.pack("<H", len(zones))
    for zone_name, zone_leds in zones:
        body += _zone(zone_name, zone_leds)
    body += struct.pack("<H", led_count)
    return struct.pack("<I", len(body) + 4) + body


class ParseTest(unittest.TestCase):
    def test_zones_and_led_count_survive_the_mode_block(self) -> None:
        """A mis-sized mode block shifts everything after it, silently."""

        controller = parse_controller(
            0, build_record(zones=[("IO Cover", 15), ("PCB", 9)], led_count=24)
        )
        self.assertEqual(controller.led_count, 24)
        self.assertEqual([(z.name, z.led_count) for z in controller.zones], [("IO Cover", 15), ("PCB", 9)])

    def test_modes_are_kept_with_their_raw_bytes(self) -> None:
        controller = parse_controller(0, build_record(modes=["Off", "Static", "Direct"]))
        self.assertEqual([m.name for m in controller.modes], ["Off", "Static", "Direct"])
        for mode in controller.modes:
            self.assertTrue(mode.blob, "a mode with no bytes cannot be sent back")

    def test_a_mode_blob_round_trips_byte_for_byte(self) -> None:
        """The blob must be the mode's exact slice; we echo it, we don't rebuild it."""

        controller = parse_controller(0, build_record(modes=["Direct"]))
        self.assertEqual(controller.modes[0].blob, _mode("Direct"))

    def test_active_mode_is_recorded(self) -> None:
        controller = parse_controller(0, build_record(modes=["A", "B", "C"], active_mode=2))
        self.assertEqual(controller.active_mode, 2)

    def test_direct_mode_is_found_by_name(self) -> None:
        controller = parse_controller(0, build_record(modes=["Rainbow", "Direct"]))
        self.assertEqual(controller.direct_mode().name, "Direct")

    def test_custom_is_accepted_when_there_is_no_direct(self) -> None:
        controller = parse_controller(0, build_record(modes=["Rainbow", "Custom"]))
        self.assertEqual(controller.direct_mode().name, "Custom")

    def test_a_device_with_neither_reports_none(self) -> None:
        controller = parse_controller(0, build_record(modes=["Rainbow", "Breathing"]))
        self.assertIsNone(controller.direct_mode())


class _RecordingClient(OpenRGBClient):
    """A client that records packets instead of sending them."""

    def __init__(self, controllers: list[Controller]) -> None:
        super().__init__("127.0.0.1", 6742)
        self.controllers = controllers
        self.sent: list[tuple[int, int, bytes]] = []

    def _send(self, device: int, command: int, payload: bytes = b"") -> None:  # type: ignore[override]
        self.sent.append((device, command, payload))


class DirectModeTest(unittest.TestCase):
    """A device only leaves its firmware effect when it is sent UPDATE_MODE.

    ``SETCUSTOMMODE`` is handled by the OpenRGB server by assigning
    ``active_mode`` on its own in-memory controller. It never calls
    ``UpdateMode()``, so the device is never told and keeps running its firmware
    effect -- while every readback of the server reports "Direct". A GPU can keep
    cycling its rainbow behind exactly that.
    """

    def _client(self, modes: list[str]) -> _RecordingClient:
        return _RecordingClient([parse_controller(0, build_record(modes=modes))])

    def test_it_sends_UPDATE_MODE_not_SETCUSTOMMODE(self) -> None:
        client = self._client(["Rainbow", "Direct"])
        self.assertTrue(client.set_direct_mode(0))
        self.assertEqual([command for _d, command, _p in client.sent], [CMD_UPDATE_MODE])
        self.assertNotIn(CMD_SET_CUSTOM_MODE, [c for _d, c, _p in client.sent])

    def test_the_packet_carries_the_mode_index_and_its_blob(self) -> None:
        client = self._client(["Rainbow", "Direct"])
        client.set_direct_mode(0)
        _device, _command, payload = client.sent[0]
        (declared_size,) = struct.unpack_from("<I", payload, 0)
        (mode_index,) = struct.unpack_from("<i", payload, 4)
        self.assertEqual(declared_size, len(payload), "data_size must describe the whole payload")
        self.assertEqual(mode_index, 1)
        self.assertEqual(payload[8:], _mode("Direct"))

    def test_a_device_with_no_direct_mode_falls_back(self) -> None:
        client = self._client(["Rainbow", "Breathing"])
        self.assertFalse(client.set_direct_mode(0))
        self.assertEqual([command for _d, command, _p in client.sent], [CMD_SET_CUSTOM_MODE])

    def test_update_mode_accepts_a_mode_built_by_hand(self) -> None:
        client = _RecordingClient([])
        client.update_mode(3, Mode(7, "Direct", b"\x01\x02\x03"))
        device, command, payload = client.sent[0]
        self.assertEqual((device, command), (3, CMD_UPDATE_MODE))
        self.assertEqual(struct.unpack_from("<i", payload, 4)[0], 7)
        self.assertEqual(payload[8:], b"\x01\x02\x03")


class ProtocolNegotiationTest(unittest.TestCase):
    """The server keeps a protocol version PER CLIENT, defaulting to 0.

    Descriptions still come back at whatever version the *request* asks for, so
    skipping the handshake looks harmless -- right up until a mode is written
    back, which the server parses at the version it believes we speak. Read at
    version 0, a version-3 mode block is 8 bytes longer than expected and the
    remainder becomes the next packet's header. The connection never recovers
    and it presents as "the OpenRGB server is wedged", while other clients are
    served normally.
    """

    class _Client(OpenRGBClient):
        def __init__(self, reply: bytes | None) -> None:
            super().__init__("127.0.0.1", 6742)
            self.reply = reply
            self.sent: list[tuple[int, int, bytes]] = []

        def _send(self, device, command, payload=b"") -> None:  # type: ignore[override]
            self.sent.append((device, command, payload))

        def _receive(self, expected: int | None = None) -> bytes:  # type: ignore[override]
            if self.reply is None:
                raise OpenRGBError("timed out")
            return self.reply

    def _client(self, reply):
        client = self._Client(reply)
        # Never connected; only gettimeout/settimeout are exercised.
        client._socket = socket.socket()
        self.addCleanup(client._socket.close)
        return client

    def test_it_offers_our_version(self) -> None:
        client = self._client(struct.pack("<I", 3))
        self.assertEqual(client._negotiate_protocol(), 3)
        _device, command, payload = client.sent[0]
        self.assertEqual(command, CMD_REQUEST_PROTOCOL_VERSION)
        self.assertEqual(struct.unpack("<I", payload)[0], PROTOCOL_VERSION)

    def test_it_takes_the_lower_of_the_two(self) -> None:
        self.assertEqual(self._client(struct.pack("<I", 99))._negotiate_protocol(), PROTOCOL_VERSION)
        self.assertEqual(self._client(struct.pack("<I", 2))._negotiate_protocol(), 2)

    def test_a_server_too_old_to_answer_means_version_zero(self) -> None:
        """Not an error: such a server genuinely speaks 0, and matching it is
        what keeps our writes parseable by it."""

        self.assertEqual(self._client(None)._negotiate_protocol(), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class UnsolicitedPacketTest(unittest.TestCase):
    """The server talks when it was not asked to, and the client must cope.

    **This is a desync bug, not a noise problem.** ``DEVICE_LIST_UPDATED``
    arrives unbidden -- most often the moment detection finishes, which is
    precisely when this client is issuing requests. A reader that assumes the
    next packet is its own reply hands that notification back as the answer and
    every subsequent reply is off by one. Nothing raises. The controller count
    is read out of a zone record and simply looks like a smaller device list.
    """

    class _Stream:
        """A socket that replays a fixed byte stream."""

        def __init__(self, data: bytes) -> None:
            self.data = data
            self.position = 0

        def recv(self, size: int) -> bytes:
            chunk = self.data[self.position : self.position + size]
            self.position += len(chunk)
            return chunk

        def gettimeout(self):
            return None

        def settimeout(self, _value) -> None:
            pass

    @staticmethod
    def _packet(command: int, payload: bytes = b"") -> bytes:
        return b"ORGB" + struct.pack("<III", 0, command, len(payload)) + payload

    def _client(self, *packets: bytes) -> OpenRGBClient:
        client = OpenRGBClient("127.0.0.1", 6742)
        client._socket = self._Stream(b"".join(packets))  # type: ignore[assignment]
        return client

    def test_a_notification_before_the_reply_is_absorbed(self) -> None:
        client = self._client(
            self._packet(CMD_DEVICE_LIST_UPDATED),
            self._packet(CMD_REQUEST_CONTROLLER_COUNT, struct.pack("<I", 7)),
        )
        payload = client._receive(CMD_REQUEST_CONTROLLER_COUNT)
        self.assertEqual(struct.unpack("<I", payload), (7,))

    def test_a_notification_marks_the_device_list_stale(self) -> None:
        client = self._client(
            self._packet(CMD_DEVICE_LIST_UPDATED),
            self._packet(CMD_REQUEST_CONTROLLER_COUNT, struct.pack("<I", 7)),
        )
        self.assertFalse(client.device_list_dirty)
        client._receive(CMD_REQUEST_CONTROLLER_COUNT)
        self.assertTrue(client.device_list_dirty)

    def test_several_notifications_in_a_row_are_all_absorbed(self) -> None:
        client = self._client(
            self._packet(CMD_DEVICE_LIST_UPDATED),
            self._packet(CMD_DEVICE_LIST_UPDATED),
            self._packet(CMD_DEVICE_LIST_UPDATED),
            self._packet(CMD_REQUEST_CONTROLLER_COUNT, struct.pack("<I", 2)),
        )
        self.assertEqual(
            struct.unpack("<I", client._receive(CMD_REQUEST_CONTROLLER_COUNT)), (2,)
        )

    def test_a_mismatched_reply_raises_instead_of_being_returned(self) -> None:
        # Without the pairing this returned the protocol-version payload as if
        # it were a controller count -- a plausible small number, no error.
        client = self._client(
            self._packet(CMD_REQUEST_PROTOCOL_VERSION, struct.pack("<I", 3))
        )
        with self.assertRaises(OpenRGBError):
            client._receive(CMD_REQUEST_CONTROLLER_COUNT)

    def test_an_unpaired_read_still_accepts_anything(self) -> None:
        # Callers that genuinely do not care keep working.
        client = self._client(
            self._packet(CMD_REQUEST_PROTOCOL_VERSION, struct.pack("<I", 3))
        )
        self.assertEqual(struct.unpack("<I", client._receive()), (3,))
