"""HID transport for the Corsair Commander Core.

Everything about the wire format lives in :mod:`linux_rgb.commander_protocol`;
this module only moves those bytes over ``/dev/hidraw*`` and holds the device
state that a real connection needs.

**Why this exists at all.** OpenRGB can drive this controller from its CLI but
not through its SDK server: in server mode 1.0rc3 registers the device with every
zone sized 0, and ``RESIZEZONE`` on it kills the server with
``std::out_of_range``. The newer git build fixes the pump and leaves the fan
headers at 0. liquidctl reads the topology correctly but its ``set_color`` is
``raise NotSupportedByDriver``. Neither can drive the fans, so we do.

⚠️ **Exactly one process may hold this device.** OpenRGB's detector for it must
be disabled, or the two will fight over ``/dev/hidraw20`` and the symptoms are
baffling -- zones that read as size 0, colours that do not apply, no errors
anywhere. ``scripts/install.sh`` does this; see the design doc.
"""

from __future__ import annotations

import logging
from typing import Sequence

from linux_rgb.commander_protocol import (
    CMD_CLOSE_ENDPOINT,
    CMD_OPEN_ENDPOINT,
    CMD_READ_FINAL,
    CMD_READ_INITIAL,
    CMD_READ_MORE,
    CMD_SLEEP,
    CMD_SLEEP_DATA,
    CMD_WAKE,
    CMD_WAKE_DATA,
    DATA_TYPE_DIRECT_COLOUR,
    DATA_TYPE_FAN_MODE,
    DATA_TYPE_LED_COUNT,
    ENDPOINT_DIRECT_COLOUR,
    ENDPOINT_FAN_MODE,
    ENDPOINT_LED_COUNT,
    PORT_COUNT,
    PUMP_LEDS,
    REPORT_LENGTH,
    RGB,
    colour_buffer,
    command_packet,
    endpoint_write_packets,
    fan_mode_payload,
    parse_led_counts,
)

LOGGER = logging.getLogger("linux_rgb.commander_core")

VENDOR_ID = 0x1B1C
PRODUCT_ID = 0x0C32
INTERFACE_NUMBER = 0

# Long enough for a busy controller, short enough that a wedged one is noticed
# rather than hanging the daemon forever.
_READ_TIMEOUT_MS = 1000

# Ceiling on the pre-write drain; see CommanderCore._drain.
_MAX_DRAIN_READS = 32

# Must be > 0: cython-hidapi treats a 0 ms timeout as BLOCKING.
_DRAIN_TIMEOUT_MS = 1


class CommanderCoreError(RuntimeError):
    """The controller could not be opened or did not answer as expected."""


def find_device_path() -> bytes | None:
    """The hidraw path for interface 0, or None when the AIO is absent.

    Interface 1 exists too and answers nothing useful; picking it produces a
    device that opens cleanly and then times out on the first read.
    """

    import hid

    for entry in hid.enumerate(VENDOR_ID, PRODUCT_ID):
        if entry.get("interface_number") == INTERFACE_NUMBER:
            return entry["path"]
    return None


class CommanderCore:
    """An open Commander Core.

    Use as a context manager. Leaving the block writes black and releases the
    handle: the controller has no off state, and simply closing the handle
    returns it to its firmware pattern (green fans on this rig).
    """

    def __init__(self, path: bytes | None = None, logger: logging.Logger | None = None) -> None:
        self._logger = logger or LOGGER
        self._path = path
        self._device = None
        self.led_counts: list[int | None] = []

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> "CommanderCore":
        import hid

        path = self._path or find_device_path()
        if path is None:
            raise CommanderCoreError(
                f"No Corsair Commander Core ({VENDOR_ID:04x}:{PRODUCT_ID:04x}) on interface "
                f"{INTERFACE_NUMBER}. Is it plugged in, and is OpenRGB's detector for it disabled?"
            )
        device = hid.device()
        try:
            device.open_path(path)
        except OSError as error:
            raise CommanderCoreError(
                f"Cannot open {path!r}: {error}. Another process (usually OpenRGB) may hold it."
            ) from error
        self._device = device
        self._path = path

        # The controller answers reads with a zeroed data type until it is
        # woken -- and for a short while after ANOTHER process has released it,
        # a wake is not immediately enough either. That second case is not
        # hypothetical: it is exactly what the shutdown unit hits, opening the
        # device moments after the daemon has closed it, and it presented as
        # "returned data type (0, 0)" which reads like a protocol mismatch
        # rather than a timing one. Retry rather than fail the whole shutdown.
        #
        # Read the topology BEFORE forcing fan mode: afterwards the controller
        # can report the 34 slots it exposes rather than the LEDs that exist.
        self.led_counts = self._wake_and_read_counts()
        self._force_fan_mode()
        self._logger.info(
            "Commander Core open on %s; LED counts %s", path.decode(errors="replace"), self.led_counts
        )
        return self

    def close(self) -> None:
        if self._device is None:
            return
        try:
            self.black()
        except Exception:  # noqa: BLE001 - closing must not raise
            self._logger.debug("Could not black the Commander Core on close", exc_info=True)
        try:
            self._device.close()
        finally:
            self._device = None

    def __enter__(self) -> "CommanderCore":
        return self.open()

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- transport ---------------------------------------------------------

    def _drain(self) -> None:
        """Discard any reply still queued from an earlier exchange.

        ⚠️ **Load-bearing.** Without this the controller's replies drift one
        behind under sustained writing, and the acknowledgement check starts
        rejecting perfectly good packets -- observed as
        ``Reply 0x18 does not acknowledge 0x07 during endpoint write`` after a
        few seconds of colour streaming. liquidctl calls its own
        ``clear_enqueued_reports`` before every single command for exactly this
        reason; a colour frame is ten packets, so the queue builds fast.
        """

        assert self._device is not None
        # ⚠️ The timeout must be 1 ms, NOT 0. In cython-hidapi a timeout of 0
        # BLOCKS -- it does not mean "non-blocking" -- so a drain written the
        # obvious way hangs the daemon forever on the first empty queue.
        # Measured: read(N, 1) returns empty in 0.001 s; read(N, 0) never
        # returns. Bounded as well, so a device that keeps talking cannot
        # trap the loop either.
        for _ in range(_MAX_DRAIN_READS):
            if not self._device.read(REPORT_LENGTH, _DRAIN_TIMEOUT_MS):
                return

    def _exchange(self, packet: bytes, expect: int) -> bytes:
        """Write one report, read its reply, and check the acknowledgement.

        The reply echoes the command byte at index 1. That is a genuine
        device-level acknowledgement and the only confirmation this protocol
        offers, so it is checked on every packet rather than assumed.
        """

        if self._device is None:
            raise CommanderCoreError("Device is not open")
        self._drain()
        self._device.write(packet)
        reply = self._device.read(REPORT_LENGTH, _READ_TIMEOUT_MS)
        while reply and reply[0] != 0x00:
            reply = self._device.read(REPORT_LENGTH, _READ_TIMEOUT_MS)
        reply = bytes(reply)
        if not reply:
            raise CommanderCoreError(f"No reply to command {expect:#04x}")
        if reply[1] != expect:
            raise CommanderCoreError(
                f"Reply {reply[1]:#04x} does not acknowledge command {expect:#04x}"
            )
        return reply

    def _send(self, command: Sequence[int], data: bytes | Sequence[int] = ()) -> bytes:
        return self._exchange(command_packet(tuple(command), data), command[0])

    def _read_endpoint(self, endpoint, data_type) -> bytes:
        self._send(CMD_OPEN_ENDPOINT, endpoint)
        initial = self._send(CMD_READ_INITIAL)
        more = self._send(CMD_READ_MORE)
        final = self._send(CMD_READ_FINAL)
        self._send(CMD_CLOSE_ENDPOINT)
        if tuple(initial[3:5]) != tuple(data_type):
            raise CommanderCoreError(
                f"Endpoint {endpoint!r} returned data type {tuple(initial[3:5])!r}, "
                f"expected {tuple(data_type)!r}"
            )
        return initial[5:] + more[3:] + final[3:]

    def _write_endpoint(self, endpoint, data_type, data: bytes) -> None:
        for packet in endpoint_write_packets(tuple(endpoint), tuple(data_type), data):
            self._exchange(packet, packet[2])

    # -- operations --------------------------------------------------------

    def _wake_and_read_counts(self, attempts: int = 4, delay: float = 0.25) -> list[int | None]:
        """Wake the controller and read its topology, retrying a settling device."""

        import time

        last: Exception | None = None
        for attempt in range(attempts):
            try:
                self._send(CMD_WAKE, CMD_WAKE_DATA)
                return self._read_led_counts()
            except CommanderCoreError as error:
                last = error
                if attempt + 1 < attempts:
                    self._logger.debug(
                        "Commander Core not ready (%s); retrying in %.2fs", error, delay
                    )
                    time.sleep(delay)
        raise CommanderCoreError(
            f"Commander Core did not become ready after {attempts} attempts: {last}"
        )

    def _read_led_counts(self) -> list[int | None]:
        return parse_led_counts(self._read_endpoint(ENDPOINT_LED_COUNT, DATA_TYPE_LED_COUNT))

    def _force_fan_mode(self) -> None:
        self._write_endpoint(ENDPOINT_FAN_MODE, DATA_TYPE_FAN_MODE, fan_mode_payload())
        self._send(CMD_WAKE, CMD_WAKE_DATA)

    def wake(self) -> None:
        self._send(CMD_WAKE, CMD_WAKE_DATA)

    def sleep(self) -> None:
        self._send(CMD_SLEEP, CMD_SLEEP_DATA)

    @property
    def pump_leds(self) -> int:
        return self.led_counts[0] if self.led_counts and self.led_counts[0] else PUMP_LEDS

    @property
    def port_leds(self) -> list[int]:
        """Real LED counts for the six RGB ports; 0 where nothing is attached."""

        counts = self.led_counts[1 : 1 + PORT_COUNT]
        counts += [None] * (PORT_COUNT - len(counts))
        return [count or 0 for count in counts]

    def set_colour(self, pump: RGB, ports: RGB) -> None:
        """Light the pump one colour and every attached fan LED another."""

        self.set_direct([pump] * self.pump_leds, [[ports] * n for n in self.port_leds])

    def set_direct(self, pump: list[RGB], ports: list[list[RGB]]) -> None:
        self._write_endpoint(
            ENDPOINT_DIRECT_COLOUR, DATA_TYPE_DIRECT_COLOUR, colour_buffer(pump, ports)
        )

    def black(self) -> None:
        """Write black everywhere.

        Not the same as stopping: the controller reverts to its firmware
        pattern about ten seconds after the last commit, so going dark has to
        be an explicit write.
        """

        self.set_colour((0, 0, 0), (0, 0, 0))
