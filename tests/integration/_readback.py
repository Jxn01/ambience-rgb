"""Read back more of an OpenRGB controller record than the client bothers with.

``linux_rgb.openrgb_client`` parses only what it needs to drive hardware: zones,
LED counts, and each mode's raw bytes. These checks need the rest -- the active
mode and the current colours -- so the walk lives here rather than bloating the
client with fields nothing in production reads.

🚨 **What this can and cannot tell you.** Everything here comes from the OpenRGB
*server's own model* of a device. It proves a packet arrived and was accepted. It
does **not** prove a single LED lit. That distinction cost this project a full
day: four separate readbacks reported perfect health while the Commander Core sat
dark, because a wrong HID report size truncated the colour payload while leaving
the command header -- and therefore every acknowledgement -- intact.

For anything that must be true of the *hardware*, use ``check_hardware.py``,
which asks a human.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from linux_rgb.openrgb_client import (  # noqa: E402
    CMD_REQUEST_CONTROLLER_DATA,
    OpenRGBClient,
)

RGB = tuple[int, int, int]


def walk(data: bytes):
    """Decode a protocol-3 controller record fully.

    Returns ``(name, active_mode, mode_names, zones, colours)`` where ``zones``
    is a list of ``(name, led_count)`` and ``colours`` one RGB triple per LED.
    """

    offset = [4]

    def take(fmt: str):
        value = struct.unpack_from(fmt, data, offset[0])
        offset[0] += struct.calcsize(fmt)
        return value

    def text() -> str:
        (length,) = take("<H")
        out = data[offset[0] : offset[0] + length - 1].decode("utf-8", "replace")
        offset[0] += length
        return out

    take("<i")
    name = text()
    for _ in range(5):  # vendor, description, version, serial, location
        text()

    mode_count, active_mode = take("<Hi")
    modes = []
    for _ in range(mode_count):
        modes.append(text())
        take("<i")
        take("<IIIIIIIIIII")
        (colour_count,) = take("<H")
        offset[0] += 4 * colour_count

    (zone_count,) = take("<H")
    zones = []
    for _ in range(zone_count):
        zone_name = text()
        take("<i")
        _min, _max, led_count = take("<III")
        (matrix_len,) = take("<H")
        offset[0] += matrix_len
        zones.append((zone_name, led_count))

    (led_count,) = take("<H")
    for _ in range(led_count):
        text()
        take("<I")

    (colour_count,) = take("<H")
    colours: list[RGB] = []
    for _ in range(colour_count):
        (raw,) = take("<I")
        colours.append((raw & 0xFF, (raw >> 8) & 0xFF, (raw >> 16) & 0xFF))

    return name, active_mode, modes, zones, colours


class Reader:
    """A connection used only for reading device state back."""

    def __init__(self, host: str = "127.0.0.1", port: int = 6742) -> None:
        self._client = OpenRGBClient(host, port).connect()

    @property
    def controllers(self):
        return self._client.controllers

    def device(self, index: int):
        self._client._send(
            index, CMD_REQUEST_CONTROLLER_DATA, struct.pack("<I", self._client.protocol_version)
        )
        return walk(self._client._receive())

    def colours(self, index: int) -> list[RGB]:
        return sorted(set(self.device(index)[4]))

    def zone_colours(self, index: int) -> dict[str, list[RGB]]:
        _name, _active, _modes, zones, colours = self.device(index)
        out: dict[str, list[RGB]] = {}
        cursor = 0
        for zone_name, count in zones:
            out[zone_name] = sorted(set(colours[cursor : cursor + count]))
            cursor += count
        return out

    def close(self) -> None:
        self._client.close()
