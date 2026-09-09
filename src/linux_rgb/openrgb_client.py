"""A small OpenRGB SDK client, speaking the wire protocol directly.

No third-party client library. The protocol is a length-prefixed binary format
that takes about a hundred lines to speak, and the alternative is an AUR package
whose version has to track the server's. Everything here was verified against
the running server on this rig.

The server owns the hardware; this is purely a client. That split is forced,
not chosen: **exactly one process may hold a given HID device**, and when two
try, the symptoms are silent and baffling -- zones that read as size 0, colours
that never apply, and no error anywhere.

Protocol notes worth keeping:

* Every message is ``ORGB`` + ``device`` + ``command`` + ``length``, all
  little-endian u32, then the payload.
* ``REQUEST_CONTROLLER_DATA`` takes the protocol version the client wants; this
  asks for 3 and parses 3. Asking for a version and parsing another silently
  misaligns every field after the mode block.
* ⚠️ **RESIZEZONE is 1000, not 1102.** 1102 is SAVEMODE. Sending 1102 expecting
  a resize does nothing useful, and sending a genuine resize to a controller
  with no zones crashes OpenRGB 1.0rc3 outright with ``std::out_of_range``.
"""

from __future__ import annotations

import logging
import socket
import struct

LOGGER = logging.getLogger("linux_rgb.openrgb")

MAGIC = b"ORGB"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6742
PROTOCOL_VERSION = 3

CMD_REQUEST_CONTROLLER_COUNT = 0
CMD_REQUEST_CONTROLLER_DATA = 1
CMD_SET_CLIENT_NAME = 50
CMD_UPDATE_LEDS = 1050
CMD_UPDATE_ZONE_LEDS = 1051
CMD_SET_CUSTOM_MODE = 1100

RGB = tuple[int, int, int]


class OpenRGBError(RuntimeError):
    """The server is unreachable or answered something unparseable."""


class Zone:
    __slots__ = ("index", "name", "led_count")

    def __init__(self, index: int, name: str, led_count: int) -> None:
        self.index = index
        self.name = name
        self.led_count = led_count

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Zone({self.index}, {self.name!r}, {self.led_count})"


class Controller:
    __slots__ = ("index", "name", "led_count", "zones")

    def __init__(self, index: int, name: str, led_count: int, zones: list[Zone]) -> None:
        self.index = index
        self.name = name
        self.led_count = led_count
        self.zones = zones

    def zone_by_name(self, name: str) -> Zone | None:
        for zone in self.zones:
            if zone.name == name:
                return zone
        return None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Controller({self.index}, {self.name!r}, {self.led_count})"


def parse_controller(index: int, data: bytes) -> Controller:
    """Decode one protocol-3 controller record.

    Pure, so the parse can be tested against a captured record without a
    server. Field order matters absolutely: a mis-sized mode block shifts every
    zone and LED count after it, and the result looks like a device with no
    LEDs rather than like a parse error.
    """

    offset = [4]  # skip the record's own length

    def take(fmt: str):
        size = struct.calcsize(fmt)
        value = struct.unpack_from(fmt, data, offset[0])
        offset[0] += size
        return value

    def take_string() -> str:
        (length,) = take("<H")
        text = data[offset[0] : offset[0] + length - 1].decode("utf-8", "replace")
        offset[0] += length
        return text

    take("<i")  # device type
    name = take_string()
    for _ in range(5):  # vendor, description, version, serial, location
        take_string()

    (mode_count, _active_mode) = take("<Hi")
    for _ in range(mode_count):
        take_string()
        take("<i")            # value
        take("<IIIIIIIIIII")  # 11 u32 fields in protocol 3
        (colour_count,) = take("<H")
        offset[0] += 4 * colour_count

    (zone_count,) = take("<H")
    zones: list[Zone] = []
    for zone_index in range(zone_count):
        zone_name = take_string()
        take("<i")                     # zone type
        (_min, _max, led_count) = take("<III")
        (matrix_len,) = take("<H")
        offset[0] += matrix_len
        zones.append(Zone(zone_index, zone_name, led_count))

    (led_count,) = take("<H")
    return Controller(index, name, led_count, zones)


class OpenRGBClient:
    """A connection to an OpenRGB SDK server."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        name: str = "linux-rgb",
        timeout: float = 5.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._name = name
        self._timeout = timeout
        self._logger = logger or LOGGER
        self._socket: socket.socket | None = None
        self.controllers: list[Controller] = []

    # -- connection --------------------------------------------------------

    def connect(self) -> "OpenRGBClient":
        try:
            self._socket = socket.create_connection((self._host, self._port), self._timeout)
        except OSError as error:
            raise OpenRGBError(
                f"Cannot reach the OpenRGB server at {self._host}:{self._port}: {error}. "
                "Is matterlights-openrgb.service running?"
            ) from error
        self._send(0, CMD_SET_CLIENT_NAME, self._name.encode() + b"\x00")
        self.controllers = self._read_controllers()
        self._logger.info(
            "OpenRGB: %d controllers (%s)",
            len(self.controllers),
            ", ".join(c.name for c in self.controllers),
        )
        return self

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None
        self.controllers = []

    def __enter__(self) -> "OpenRGBClient":
        return self.connect()

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- transport ---------------------------------------------------------

    def _send(self, device: int, command: int, payload: bytes = b"") -> None:
        if self._socket is None:
            raise OpenRGBError("Not connected")
        header = MAGIC + struct.pack("<III", device, command, len(payload))
        try:
            self._socket.sendall(header + payload)
        except OSError as error:
            raise OpenRGBError(f"Send failed: {error}") from error

    def _receive(self) -> bytes:
        if self._socket is None:
            raise OpenRGBError("Not connected")
        header = self._recv_exactly(16)
        if header[:4] != MAGIC:
            raise OpenRGBError(f"Bad magic {header[:4]!r} in reply")
        _device, _command, size = struct.unpack("<III", header[4:16])
        return self._recv_exactly(size)

    def _recv_exactly(self, size: int) -> bytes:
        assert self._socket is not None
        chunks = b""
        while len(chunks) < size:
            try:
                chunk = self._socket.recv(size - len(chunks))
            except OSError as error:
                raise OpenRGBError(f"Receive failed: {error}") from error
            if not chunk:
                raise OpenRGBError("Server closed the connection")
            chunks += chunk
        return chunks

    def _read_controllers(self) -> list[Controller]:
        self._send(0, CMD_REQUEST_CONTROLLER_COUNT)
        (count,) = struct.unpack("<I", self._receive())
        controllers = []
        for index in range(count):
            self._send(index, CMD_REQUEST_CONTROLLER_DATA, struct.pack("<I", PROTOCOL_VERSION))
            controllers.append(parse_controller(index, self._receive()))
        return controllers

    # -- writing -----------------------------------------------------------

    def set_custom_mode(self, device: int) -> None:
        """Put a controller into its direct/custom mode so writes take effect.

        Devices sitting in a firmware effect (the GPU ships on Rainbow, the
        ASRock board on Off) ignore colour writes until this is sent.
        """

        self._send(device, CMD_SET_CUSTOM_MODE)

    def update_leds(self, device: int, colours: list[RGB]) -> None:
        body = struct.pack("<H", len(colours)) + b"".join(
            struct.pack("<BBBB", r, g, b, 0) for (r, g, b) in colours
        )
        self._send(device, CMD_UPDATE_LEDS, struct.pack("<I", len(body) + 4) + body)

    def update_zone_leds(self, device: int, zone: int, colours: list[RGB]) -> None:
        """Write one zone, leaving the controller's other zones untouched.

        This is what makes a per-zone split possible: the ASRock board's
        IO-cover and PCB zones take the near colour while its addressable
        headers take the far one, from a single controller.
        """

        body = (
            struct.pack("<I", zone)
            + struct.pack("<H", len(colours))
            + b"".join(struct.pack("<BBBB", r, g, b, 0) for (r, g, b) in colours)
        )
        self._send(device, CMD_UPDATE_ZONE_LEDS, struct.pack("<I", len(body) + 4) + body)
