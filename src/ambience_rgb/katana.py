"""Driver for the Sound Blaster Katana V2X (and V2) lighting, over its serial port.

The soundbar's control interface is a USB CDC ACM serial port. This module does
the I/O; the wire format, and the licence it is used under, are in
:mod:`ambience_rgb.katana_protocol`.

Connecting
----------
``open()`` first asks for the firmware version, a binary query whose reply is
known from a USB capture. An answer means the soundbar is already in binary
mode, which is the usual case: it has its own power supply, so it stays in
binary mode across PC reboots. Silence means text mode -- on 2026-09-10 the real
device met a binary frame in text mode with no reply at all -- and then:

1. ``whoareyou`` -> a challenge, ``NotYet`` (wait and ask again), or
   ``Unknown command`` (already unlocked, just not switched).
2. ``unlock`` + the sealed nonce -> ``unlock_OK``.
3. ``SW_MODE1`` -> binary mode, confirmed by asking for the firmware again.

Step 3 is not a formality. Text mode ignores binary frames in silence, so a
driver that skips the confirmation can "succeed" at every write forever while
the soundbar hears none of them.

Exactly one process may drive it
--------------------------------
Two writers on one serial port interleave and corrupt each other's frames. The
port is opened with pyserial's ``exclusive`` -- an advisory ``flock``, which only
stops programs that also ask for it -- *and* the kernel's ``TIOCEXCL``, which
refuses every further ``open()`` of the tty to anyone but root.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from ambience_rgb import katana_protocol as kp

LOGGER = logging.getLogger("ambience_rgb.katana")

RGB = tuple[int, int, int]

# CDC ACM ignores the baud rate; this is what the reference and Creative use.
BAUD = 115200

# One blocking read waits at most this long, so nothing here stalls the
# daemon's loop for longer than it has to.
READ_SLICE_SECONDS = 0.05

# ⚠️ pyserial's write() blocks FOREVER by default when the tty buffer is full.
# A stalled soundbar must fail a write, not freeze every other sink with it.
WRITE_TIMEOUT_SECONDS = 0.25

# How long to wait for an answer: to the probe (which in text mode never comes),
# to a query, and to each step of the unlock.
PROBE_SECONDS = 1.0
REPLY_SECONDS = 1.0
AUTH_SECONDS = 2.0

# "NotYet" means ask again shortly; the reference allows five retries a second apart.
NOT_YET_RETRIES = 5
NOT_YET_DELAY_SECONDS = 1.0

# Pause after SW_MODE1 before speaking binary, as the reference does.
MODE_SWITCH_SECONDS = 0.1

# A drain stops at the first quiet read slice, or after this long regardless.
DRAIN_LIMIT_SECONDS = 1.0

_UNLOCKED = "unlocked"
_NOT_YET = "NotYet"


class KatanaError(RuntimeError):
    """The soundbar could not be reached or would not cooperate; says why."""


def find_port(comports: Callable[[], list] | None = None) -> tuple[str, int]:
    """The serial port of the first Katana on USB, and its USB product ID.

    Matched strictly by USB IDs. The reference falls back to *any* ``ttyACM``
    port when none match and sends it ``whoareyou``; that could be a modem, a
    microcontroller or a 3D printer, so this refuses instead.
    """

    if comports is None:
        from serial.tools.list_ports import comports
    for port in comports():
        if port.vid == kp.VENDOR_ID and port.pid in kp.MODELS:
            return port.device, port.pid
    wanted = ", ".join(f"{kp.VENDOR_ID:04x}:{pid:04x}" for pid in kp.MODELS)
    raise KatanaError(
        f"No Sound Blaster Katana on USB (looked for {wanted}). "
        "Is it plugged in and switched on?"
    )


def open_serial(path: str):
    """Open the port for one owner only. See the module docstring."""

    import serial

    port = serial.Serial(
        path, BAUD,
        timeout=READ_SLICE_SECONDS,
        write_timeout=WRITE_TIMEOUT_SECONDS,
        exclusive=True,  # POSIX flock; accepted and ignored on Windows
    )
    try:
        _claim_exclusively(port)
    except OSError:
        port.close()
        raise
    return port


def _claim_exclusively(port) -> None:
    """TIOCEXCL: the kernel refuses any later open() of this tty, bar root's.

    Released automatically when the port is closed. Windows needs nothing
    here -- a COM port is already exclusive to whoever opened it.
    """

    try:
        import fcntl
        import termios
    except ImportError:
        return
    request = getattr(termios, "TIOCEXCL", None)
    if request is not None:
        fcntl.ioctl(port.fileno(), request)


class Katana:
    """One connection to the soundbar's lighting."""

    def __init__(
        self,
        path: str | None = None,
        logger: logging.Logger | None = None,
        *,
        read_only: bool = False,
        not_yet_retries: int = NOT_YET_RETRIES,
        port_factory: Callable[[str], object] = open_serial,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._path = path
        self._logger = logger or LOGGER
        self._read_only = read_only
        # How many times to wait out "NotYet", a second apart. Right for a
        # one-shot tool; the daemon passes 0, because its sleep would freeze
        # every other sink -- see KatanaSink._open_device.
        self._not_yet_retries = not_yet_retries
        self._port_factory = port_factory
        self._clock = clock
        self._sleep = sleep
        self._port = None
        self._reader = kp.FrameReader()
        # The keepalive question still waiting for its answer, if any.
        self._awaiting: bytes | None = None
        self.path = path
        self.model = "Sound Blaster Katana"
        self.firmware: str | None = None
        # Frames nobody asked for. Logged at debug; counted so a diagnostic can
        # say whether the soundbar answers its setters.
        self.unsolicited = 0

    def __enter__(self) -> "Katana":
        return self.open()

    def __exit__(self, *_exc) -> None:
        self.close()

    def open(self) -> "Katana":
        if self._path is None:
            self.path, pid = find_port()
            self.model = kp.model_for_pid(pid) or self.model
        self._port = self._port_factory(self.path)
        try:
            self._connect()
        except BaseException:
            self.release()
            raise
        return self

    # --- lighting -----------------------------------------------------------

    def prepare(self) -> None:
        """Lights on, in the static single-colour effect. Once per connection.

        Brightness is deliberately left alone: it is the owner's setting on the
        device, and nothing here has a reason to overrule it.
        """

        for frame in (kp.lighting_power(True), kp.colour_count(1), kp.solo_mode()):
            self._command(frame)

    def set_colour(self, colour: RGB) -> None:
        self._command(kp.solo_colour(colour))

    def black(self) -> None:
        self.set_colour((0, 0, 0))

    def read_colour(self) -> RGB:
        """The solo colour, as the soundbar reports it."""
        return kp.parse_colours(self._query(kp.colour_query()))[0]

    def keepalive(self) -> None:
        """Ask a question now; its answer is collected by later calls.

        Raises if the previous question was never answered. Never blocks: the
        answer is picked up on the next write or keepalive, so a live soundbar
        costs one frame per call and a dead one costs nothing extra.

        🚨 This is the only way to see a soundbar that was power-cycled while
        its USB link stayed up. It comes back in text mode, which ignores
        binary frames without a word, so every colour write would go on
        "succeeding" into a void.
        """

        self._absorb()
        if self._awaiting is not None:
            raise KatanaError(
                "The soundbar stopped answering: power-cycled back into text mode, or gone"
            )
        self._awaiting = kp.firmware_version_query()
        self._write(self._awaiting)

    # --- letting go ---------------------------------------------------------

    def release(self) -> None:
        """Let go of the port WITHOUT writing anything.

        Not the same as close(): see CommanderCore.release. A recovery path
        that blacks the device it is about to reopen darkens exactly the
        hardware it is trying to rescue.
        """

        port, self._port = self._port, None
        self._awaiting = None
        self._reader.clear()
        if port is not None:
            try:
                port.close()
            except Exception:  # noqa: BLE001 - letting go must not raise
                self._logger.debug("katana: close failed", exc_info=True)

    def close(self) -> None:
        """Black the soundbar and let go -- unless this is a read-only open."""

        if self._port is None:
            return
        try:
            if not self._read_only:
                self.black()
        finally:
            self.release()

    # --- connecting ---------------------------------------------------------

    def _connect(self) -> None:
        self._drain()
        # The probe carries a CRLF, as the reference's does. Binary mode ignores
        # it; in text mode it ends the garbled line, so the whoareyou after it
        # starts on a clean one.
        reply = self._await_reply(
            kp.firmware_version_query(), suffix=b"\r\n", timeout=PROBE_SECONDS
        )
        if reply is None:
            self._logger.info("katana: soundbar is in text mode; unlocking it")
            self._authenticate()
            self._enter_binary_mode()
        else:
            self.firmware = kp.parse_firmware_version(reply)

    def _authenticate(self) -> None:
        for attempt in range(self._not_yet_retries + 1):
            self._drain()
            self._write(b"whoareyou\r\n")
            received, verdict = self._await_text(_auth_verdict, AUTH_SECONDS)
            if verdict is None:
                raise KatanaError(
                    f"No answer to whoareyou within {AUTH_SECONDS:.0f}s (received {received!r})"
                )
            if verdict == _UNLOCKED:
                self._logger.info("katana: already unlocked")
                return
            if verdict == _NOT_YET:
                if attempt == self._not_yet_retries:
                    break
                self._logger.info(
                    "katana: soundbar says NotYet; waiting (%d/%d)",
                    attempt + 1, self._not_yet_retries,
                )
                self._sleep(NOT_YET_DELAY_SECONDS)
                continue
            self._unlock(verdict)
            return
        raise KatanaError(
            f"The soundbar answered NotYet {self._not_yet_retries + 1} time(s) in a row; "
            "it may still be starting up"
        )

    def _unlock(self, challenge: kp.Challenge) -> None:
        self._write(kp.unlock_line(challenge))
        received, verdict = self._await_text(_unlock_verdict, AUTH_SECONDS)
        if not verdict:
            raise KatanaError(f"The soundbar refused the unlock (received {received!r})")
        self._logger.info("katana: unlocked")

    def _enter_binary_mode(self) -> None:
        self._write(b"SW_MODE1\r\n")
        self._sleep(MODE_SWITCH_SECONDS)
        self._drain()
        reply = self._await_reply(kp.firmware_version_query(), timeout=REPLY_SECONDS)
        if reply is None:
            raise KatanaError(
                "Sent SW_MODE1, but the soundbar did not answer in binary mode"
            )
        self.firmware = kp.parse_firmware_version(reply)
        self._logger.info("katana: in binary mode")

    # --- the wire -----------------------------------------------------------

    def _write(self, data: bytes) -> None:
        if self._port is None:
            raise KatanaError("The soundbar is not open")
        self._logger.debug("katana -> %s", data.hex(" "))
        self._port.write(data)

    def _command(self, frame: bytes) -> None:
        """A write nothing waits on -- then take in whatever has arrived.

        Whether the soundbar answers its setters is not documented. If it does
        and nothing reads the answers, they pile up on the host, and a device
        blocked on a full IN pipe can stall. Reading what is waiting costs
        nothing when nothing is.
        """

        self._write(frame)
        self._absorb()

    def _query(self, request: bytes) -> bytes:
        reply = self._await_reply(request, timeout=REPLY_SECONDS)
        if reply is None:
            raise KatanaError(f"No reply to {request.hex(' ')} within {REPLY_SECONDS:.0f}s")
        return reply

    def _await_reply(self, request: bytes, *, timeout: float, suffix: bytes = b"") -> bytes | None:
        """Send ``request`` and return its answer, or None at the deadline.

        Anything else that arrives meanwhile is settled, not mistaken for the
        answer: the next frame on the wire is not necessarily the reply to the
        last question, which is the same lesson the OpenRGB and Commander Core
        drivers each learned the hard way.
        """

        self._absorb()
        self._write(request + suffix)
        deadline = self._clock() + timeout
        while True:
            reply = None
            for frame in self._reader.frames():
                if reply is None and kp.is_reply_to(frame, request):
                    reply = frame
                else:
                    self._settle(frame)
            if reply is not None:
                self._logger.debug("katana <- %s", reply.hex(" "))
                return reply
            if self._clock() >= deadline:
                return None
            self._reader.feed(self._port.read(max(1, self._port.in_waiting)))

    def _await_text(self, judge: Callable[[bytes], object], timeout: float) -> tuple[bytes, object]:
        """Collect raw text-mode bytes until ``judge`` returns a verdict, or time runs out."""

        received = bytearray()
        deadline = self._clock() + timeout
        while True:
            verdict = judge(bytes(received))
            if verdict is not None or self._clock() >= deadline:
                self._logger.debug("katana <- %r", bytes(received))
                return bytes(received), verdict
            received += self._port.read(max(1, self._port.in_waiting))

    def _absorb(self) -> None:
        """Take in whatever is waiting, without blocking."""

        waiting = self._port.in_waiting if self._port is not None else 0
        if waiting:
            self._reader.feed(self._port.read(waiting))
            for frame in self._reader.frames():
                self._settle(frame)

    def _settle(self, frame: bytes) -> None:
        if self._awaiting is not None and kp.is_reply_to(frame, self._awaiting):
            self._awaiting = None
            return
        self.unsolicited += 1
        self._logger.debug("katana <- unsolicited %s", frame.hex(" "))

    def _drain(self) -> None:
        """Throw away whatever is in flight: stale text, leftover frames."""

        self._port.reset_input_buffer()
        self._reader.clear()
        drained = bytearray()
        deadline = self._clock() + DRAIN_LIMIT_SECONDS
        while self._clock() < deadline:
            chunk = self._port.read(max(1, self._port.in_waiting))
            if not chunk:
                break
            drained += chunk
        if drained:
            self._logger.debug("katana: drained %r", bytes(drained))


def _auth_verdict(received: bytes):
    """What an answer to ``whoareyou`` says, once enough of it has arrived.

    Searched for rather than read as a line: status frames can arrive ahead of
    it, and the challenge's nonce is binary and may itself contain CR LF.
    """

    start = received.find(kp.CHALLENGE_PREFIX)
    if start >= 0:
        # The trailing CR LF is not waited for; the fields end before it.
        if len(received) - start >= kp.CHALLENGE_LENGTH - 2:
            return kp.parse_challenge(received[start:start + kp.CHALLENGE_LENGTH])
        return None
    if b"Unknown command" in received:
        return _UNLOCKED
    if b"NotYet" in received:
        return _NOT_YET
    return None


def _unlock_verdict(received: bytes):
    """True for ``unlock_OK``, False for any other complete ``unlock...`` line."""

    start = received.find(b"unlock")
    if start < 0 or b"\r\n" not in received[start:]:
        return None
    return received[start:].startswith(b"unlock_OK")
