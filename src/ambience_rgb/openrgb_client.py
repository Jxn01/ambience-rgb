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
import time

LOGGER = logging.getLogger("ambience_rgb.openrgb")

MAGIC = b"ORGB"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6742
PROTOCOL_VERSION = 3

CMD_REQUEST_CONTROLLER_COUNT = 0
CMD_REQUEST_CONTROLLER_DATA = 1
CMD_REQUEST_PROTOCOL_VERSION = 40
CMD_SET_CLIENT_NAME = 50
CMD_UPDATE_LEDS = 1050
CMD_UPDATE_ZONE_LEDS = 1051
CMD_SET_CUSTOM_MODE = 1100
CMD_UPDATE_MODE = 1101

RGB = tuple[int, int, int]


class OpenRGBError(RuntimeError):
    """The server is unreachable or answered something unparseable."""


class Mode:
    """One of a controller's modes, kept with the exact bytes it arrived in.

    ``blob`` is the mode's slice of the controller record, verbatim. Sending a
    mode back means echoing that slice, so we never have to re-serialise a
    struct whose field list differs between protocol versions -- a mis-sized
    mode block is the one parse error that produces a plausible-looking device
    with the wrong LED counts rather than an exception.
    """

    __slots__ = ("index", "name", "blob")

    def __init__(self, index: int, name: str, blob: bytes) -> None:
        self.index = index
        self.name = name
        self.blob = blob

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Mode({self.index}, {self.name!r})"


class Zone:
    __slots__ = ("index", "name", "led_count")

    def __init__(self, index: int, name: str, led_count: int) -> None:
        self.index = index
        self.name = name
        self.led_count = led_count

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Zone({self.index}, {self.name!r}, {self.led_count})"


class Controller:
    __slots__ = ("index", "name", "led_count", "zones", "modes", "active_mode")

    def __init__(
        self,
        index: int,
        name: str,
        led_count: int,
        zones: list[Zone],
        modes: list[Mode] | None = None,
        active_mode: int = 0,
    ) -> None:
        self.index = index
        self.name = name
        self.led_count = led_count
        self.zones = zones
        self.modes = modes or []
        self.active_mode = active_mode

    def direct_mode(self) -> Mode | None:
        """The mode that makes per-LED writes take effect, if the device has one.

        Named "Direct" on every controller on this rig, but the fallback matters:
        a device with no such mode (some keyboards call it "Custom") should still
        get its closest equivalent rather than nothing.
        """

        for wanted in ("direct", "custom"):
            for mode in self.modes:
                if mode.name.strip().lower() == wanted:
                    return mode
        return None

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

    (mode_count, active_mode) = take("<Hi")
    modes: list[Mode] = []
    for mode_index in range(mode_count):
        start = offset[0]
        mode_name = take_string()
        take("<i")            # value
        take("<IIIIIIIIIII")  # 11 u32 fields in protocol 3
        (colour_count,) = take("<H")
        offset[0] += 4 * colour_count
        modes.append(Mode(mode_index, mode_name, data[start : offset[0]]))

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
    return Controller(index, name, led_count, zones, modes, active_mode)


class OpenRGBClient:
    """A connection to an OpenRGB SDK server."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        name: str = "ambience-rgb",
        timeout: float = 5.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._name = name
        self._timeout = timeout
        self._logger = logger or LOGGER
        self.protocol_version = PROTOCOL_VERSION
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
        self.protocol_version = self._negotiate_protocol()
        if self.protocol_version < PROTOCOL_VERSION:
            # parse_controller decodes the version-3 record layout and nothing
            # else. Reading an older one would not fail -- it would shift every
            # zone and LED count after the mode block and look like a device
            # with the wrong number of LEDs.
            self.close()
            raise OpenRGBError(
                f"The OpenRGB server speaks protocol {self.protocol_version}; "
                f"this client needs {PROTOCOL_VERSION}. Upgrade OpenRGB."
            )
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

    def _negotiate_protocol(self) -> int:
        """Agree a protocol version with the server, and remember it.

        🚨 **Skipping this desyncs the connection the moment you write a mode.**
        The server keeps a protocol version *per client* and defaults it to 0
        until this handshake happens. Asking for a controller description at
        version 3 still returns a version-3 record -- that request carries its
        own version -- so everything looks fine. But an ``UPDATE_MODE`` written
        back is parsed at the client's *stored* version: at 0 the server reads
        8 bytes fewer than we sent (the brightness fields), and the leftovers
        become the header of the next packet.

        The connection then never recovers. The server sits waiting for a
        payload length it invented, our packets pile up unread -- 238 kB of them
        were measured -- and every reply we wait for never comes, while other
        clients are served normally at 0.1 ms. It presents as "the OpenRGB
        server is wedged", and it is not: only our stream is.

        Every description is requested at the negotiated version and every mode
        echoed back at the same one, so the two cannot drift apart.
        """

        assert self._socket is not None
        previous = self._socket.gettimeout()
        self._socket.settimeout(2.0)
        try:
            self._send(0, CMD_REQUEST_PROTOCOL_VERSION, struct.pack("<I", PROTOCOL_VERSION))
            (server_version,) = struct.unpack("<I", self._receive())
        except (OSError, OpenRGBError, struct.error):
            # A server too old to know the request answers nothing. Version 0 is
            # then the truth, and honouring it keeps us in step with it.
            self._logger.info("OpenRGB: no protocol handshake; assuming version 0")
            return 0
        finally:
            if self._socket is not None:
                self._socket.settimeout(previous)
        agreed = min(PROTOCOL_VERSION, server_version)
        self._logger.debug("OpenRGB: protocol %d (server offered %d)", agreed, server_version)
        return agreed

    def _read_controllers(self) -> list[Controller]:
        self._send(0, CMD_REQUEST_CONTROLLER_COUNT)
        (count,) = struct.unpack("<I", self._receive())
        controllers = []
        for index in range(count):
            self._send(index, CMD_REQUEST_CONTROLLER_DATA, struct.pack("<I", self.protocol_version))
            controllers.append(parse_controller(index, self._receive()))
        return controllers

    # -- writing -----------------------------------------------------------

    def set_custom_mode(self, device: int) -> None:
        """Ask the SERVER to select the controller's custom mode.

        🚨 **This does not reach the hardware, and the name invites you to think
        it does.** OpenRGB's server handles ``SETCUSTOMMODE`` by assigning
        ``active_mode`` on its own in-memory controller and nothing else -- it
        never calls ``UpdateMode()``. The device is never told, so it keeps
        running whatever firmware effect it was in, while every readback of the
        server cheerfully reports "Direct".

        Kept only as the fallback for a controller that exposes no direct mode
        to push. Use :meth:`set_direct_mode`.
        """

        self._send(device, CMD_SET_CUSTOM_MODE)

    def round_trip_seconds(self, timeout: float) -> float:
        """Time a request/reply, which measures how far behind the server is.

        🚨 **The only honest backpressure signal available.** Writes are
        fire-and-forget and, on loopback, our own send queue reads zero even
        while the server has hundreds of kilobytes of our packets unread -- the
        bytes simply move into its receive buffer. Locally everything looks
        perfect right up until the lights are minutes stale.

        A reply is queued behind every packet already sent, so the time it takes
        to come back IS the backlog, in seconds. Raises when the server does not
        answer inside ``timeout``.
        """

        if self._socket is None:
            raise OpenRGBError("Not connected")
        started = time.monotonic()
        previous = self._socket.gettimeout()
        self._socket.settimeout(timeout)
        try:
            self._send(0, CMD_REQUEST_CONTROLLER_COUNT)
            self._receive()
        except (OSError, OpenRGBError) as error:
            raise OpenRGBError(
                f"The OpenRGB server did not answer within {timeout:.1f}s ({error}); "
                "it is wedged or far behind"
            ) from error
        finally:
            if self._socket is not None:
                self._socket.settimeout(previous)
        return time.monotonic() - started

    def update_mode(self, device: int, mode: Mode) -> None:
        """Write a mode to the DEVICE. This is the call that changes hardware.

        The mode is echoed back exactly as it was received, so the wire format
        is whatever the server already sent us rather than a struct we rebuild
        and have to keep in step with the protocol version.
        """

        payload = struct.pack("<i", mode.index) + mode.blob
        self._send(device, CMD_UPDATE_MODE, struct.pack("<I", len(payload) + 4) + payload)

    def set_direct_mode(self, device: int) -> bool:
        """Put the controller into direct mode for real; True if one was pushed.

        Devices split into two camps, and the split is invisible until you look
        at the hardware. Corsair DRAM and the Razer mouse honour per-LED writes
        whatever mode they are in, so they work either way. The ASUS GPU, the
        ASRock board and the Alienware monitor must be *told* to enter direct
        mode, and until they are they keep cycling their firmware effect while
        accepting -- and ignoring -- every colour written to them.
        """

        controller = next((c for c in self.controllers if c.index == device), None)
        mode = controller.direct_mode() if controller is not None else None
        if mode is None:
            self._logger.info(
                "OpenRGB: device %d exposes no direct mode; falling back to custom", device
            )
            self.set_custom_mode(device)
            return False
        self.update_mode(device, mode)
        return True

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
