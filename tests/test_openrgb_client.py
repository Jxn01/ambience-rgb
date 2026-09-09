"""The SDK client's parsing and its mode handling.

These exist because the two bugs that made the rig look dead for a whole day
both lived in this file and neither was covered: the parser threw away the mode
table, and ``set_custom_mode`` claimed to put hardware into direct mode while
sending a packet that never reaches the device.
"""

from __future__ import annotations

import struct
import unittest

from linux_rgb.openrgb_client import (
    CMD_SET_CUSTOM_MODE,
    CMD_UPDATE_MODE,
    Controller,
    Mode,
    OpenRGBClient,
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
    """🚨 The regression guard for the bug that made this look dead.

    ``SETCUSTOMMODE`` is handled by the OpenRGB server by assigning
    ``active_mode`` on its own in-memory controller. It never calls
    ``UpdateMode()``, so the device is never told and keeps running its firmware
    effect -- while every readback of the server reports "Direct". The ASUS GPU
    cycled its rainbow for a full day behind exactly that.
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
