"""Subscribe to matterlights' ambience frames and drive the RGB hardware.

The loop is deliberately dull: receive a frame, decide a colour, write it to
every sink, repeat. All the judgement lives upstream in matterlights, which has
already decided whether the screen is dark and whether the lights are meant to
be on. Mirroring the bulbs means obeying those verdicts, not re-deriving them.

**Black is a thing you keep writing, not a thing you write once.** None of
this hardware has an off state. The Commander Core reverts to its firmware
pattern about ten seconds after the last commit, and the ASRock headers fall
back to the board's own green. So the loop writes on every tick whether the
colour changed or not, and "off" is a black frame repeated -- which is also why
stopping the daemon has to write black explicitly on the way out.

**Stopping** comes from four directions and ends the same way -- black written,
every device released, the endpoint freed, then :attr:`Daemon.finished` set:

* a signal (``SIGTERM`` from systemd, ``SIGINT``/``SIGBREAK`` from a console);
* the stop command, :data:`STOP_COMMAND`, sent by ``off --stop-daemon`` --
  Windows' shutdown task, since Windows sends a process no signal;
* the Windows session-end window (:mod:`ambience_rgb.session_end`), from its
  own thread;
* :meth:`Daemon.request_stop`, which the other three all go through.
"""

from __future__ import annotations

import json
import logging
import signal
import threading
import time

from ambience_rgb import transport
from ambience_rgb.config import Config
from ambience_rgb.render import cohere
from ambience_rgb.sinks import ResilientSink, build_sinks

LOGGER = logging.getLogger("ambience_rgb.daemon")

BLACK = (0, 0, 0)

# How long to wait on the socket before ticking anyway. Short enough that a tick
# still happens several times a second when matterlights is quiet, so black keeps
# being refreshed and the Commander Core never falls back to green. It is also
# the longest a stop request waits to be noticed.
RECEIVE_TIMEOUT_SECONDS = 0.5

# With no frame for this long, matterlights is stopped or wedged. Go dark rather
# than hold the last colour forever -- a frozen frame looks like working sync.
#
# **This must exceed the publisher's worst BLOCKING call, not just its tick.**
# matterlights' Home Assistant timeout is 3.0 s: a single bulb that stops
# answering blocks its loop for that long, so a staleness window of 3.0 would
# blank the whole case every time a flaky bulb timed out.
#
# A publisher stalled on a slow network call is not a dead publisher. 10 s clears
# three sequential Home Assistant timeouts and still notices a genuinely stopped
# publisher well before anyone wonders why the colour is frozen.
STALE_AFTER_SECONDS = 10.0

# The most datagrams one tick will take off the socket. A tick keeps only the
# newest frame (see Daemon._receive), so this only bounds a flood: a publisher
# sending faster than this cannot hold a tick forever. 256 frames is more than
# Windows' whole 64 KB UDP receive buffer, so one tick can always empty it.
MAX_DATAGRAMS_PER_TICK = 256

# "No datagram was waiting" -- distinct from None, which is a datagram that was
# not a frame (a command, or garbage).
_NOTHING = object()

# The publisher's payload version this daemon understands.
SUPPORTED_PAYLOAD_VERSION = 2

# v1 had no palette. It is still accepted: an older publisher simply means flat
# colours, which is exactly what this did before the palette existed.
MINIMUM_PAYLOAD_VERSION = 1

STOP_COMMAND = json.dumps({"command": "stop"}).encode("utf-8")
"""The one datagram that is not a frame: black everything, release it, exit 0.

A payload with a ``command`` key is a command, never a frame, so it cannot be
misread as colours or trip the version warning. Anything that can reach the
endpoint can send it -- the unix socket is ``0600``, the UDP one listens on the
loopback only -- which is no more than it could already do by publishing black.
"""

STOP_TIMEOUT_SECONDS = 5.0
"""How long ``stop_running_daemon`` waits for the endpoint to free.

Blacking and closing every device takes well under a second here. On Windows the
shutdown task has roughly fifteen seconds between event 1074 and the end of the
session, and still has its own black to write after this."""

STOP_POLL_SECONDS = 0.05


def _colour(value) -> tuple[int, int, int] | None:
    """A payload field as an RGB triple, or None if it is not one.

    Checking the LENGTH alone is not enough: ``tuple("red")[:3]`` is
    ``('r', 'e', 'd')``, which is three items and passes. Those characters would
    reach the sinks, where packing them raises and degrades the sink for the
    retry window on every tick -- a malformed frame taking the lights down
    instead of being ignored.
    """

    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    channels = []
    for channel in value:
        if isinstance(channel, bool) or not isinstance(channel, int):
            return None
        channels.append(max(0, min(255, channel)))
    return (channels[0], channels[1], channels[2])


class Daemon:
    def __init__(
        self,
        config: Config,
        logger: logging.Logger | None = None,
        *,
        sinks_factory=None,
        session_hook_factory=None,
        install_signals: bool = True,
    ) -> None:
        self._config = config
        self._logger = logger or LOGGER
        self._sinks_factory = sinks_factory or build_sinks
        self._session_hook_factory = session_hook_factory
        self._install_signals = install_signals
        self._socket = None
        self._sinks: list[ResilientSink] = []
        self._hook = None
        self._stop = threading.Event()
        self._stop_reason: str | None = None
        self._last_frame_at = 0.0
        self._last_colours: tuple[tuple, tuple] | None = None
        self._warned_version = False
        self.ready = threading.Event()
        """Set once the endpoint is bound and the sinks are built."""
        self.finished = threading.Event()
        """Set once the hardware is black and released and the endpoint freed --
        or at once, when the start was refused. The Windows session-end window
        holds the session open on this, so it must never be set early."""

    # -- stopping ----------------------------------------------------------

    def request_stop(self, reason: str) -> None:
        """Finish the current tick, black everything and exit. Safe from any thread."""

        if not self._stop.is_set():
            self._stop_reason = reason
            self._logger.info("Stopping (%s); turning RGB off", reason)
        self._stop.set()

    def _install_signal_handlers(self) -> None:
        def handle(signum, _frame):
            self.request_stop(f"signal {signal.Signals(signum).name}")

        # SIGBREAK is Windows' Ctrl+Break; SIGTERM exists there but nothing
        # sends it. Each is installed only where the platform has it.
        for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is not None:
                signal.signal(sig, handle)

    def _start_session_hook(self):
        factory = self._session_hook_factory
        if factory is None:
            from ambience_rgb.session_end import start_session_end_hook as factory
        return factory(self.request_stop, self.finished, self._logger)

    # -- socket ------------------------------------------------------------

    def _receive(self) -> dict | None:
        """The NEWEST frame waiting, with every command on the way acted on.

        **Never one datagram per tick.** A frame is absolute state, worthless
        the moment a newer one exists. Taking the OLDEST waiting one per tick
        and writing the hardware before taking the next lets frames pile up in
        the socket whenever a tick outlasts the publisher's frame interval, to be
        played back in order. On Windows, whose UDP receive buffer holds 64 KB --
        some 180 frames -- the case would run minutes behind the screen, and a
        stop command would sit behind the frames or be dropped from a full
        buffer. A unix datagram socket caps its queue at about ten and the
        sender drops the rest, so Linux does not show it.

        So: wait up to the receive timeout for the first datagram, then take
        everything already queued without waiting, keeping only the newest
        frame. A stop found on the way ends the drain at once, and the drain is
        bounded so that a flood cannot hold a tick forever.
        """

        assert self._socket is not None
        latest = self._receive_one()
        if latest is _NOTHING:
            return None
        previous_timeout = self._socket.gettimeout()
        self._socket.settimeout(0.0)
        try:
            for _ in range(MAX_DATAGRAMS_PER_TICK):
                if self._stop.is_set():
                    break
                datagram = self._receive_one()
                if datagram is _NOTHING:
                    break
                if datagram is not None:
                    latest = datagram
        finally:
            # A zero timeout left behind would turn every later wait into a
            # busy loop.
            self._socket.settimeout(previous_timeout)
        return latest

    def _receive_one(self):
        """One datagram: its frame, None when it was not a frame, _NOTHING when none waited.

        A command is acted on here, wherever in a drain it turns up.
        """

        assert self._socket is not None
        try:
            data = self._socket.recv(65535)
        except (TimeoutError, BlockingIOError):
            # TimeoutError: the blocking wait ran out. BlockingIOError: the
            # non-blocking drain found the socket empty. Both mean "nothing".
            return _NOTHING
        except OSError as error:
            self._logger.warning("Receive failed: %s", error)
            return _NOTHING
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._logger.debug("Ignoring an unparseable datagram")
            return None
        if not isinstance(payload, dict):
            return None
        if "command" in payload:
            self._handle_command(payload["command"])
            return None
        version = payload.get("v")
        if not isinstance(version, int) or not (
            MINIMUM_PAYLOAD_VERSION <= version <= SUPPORTED_PAYLOAD_VERSION
        ):
            if not self._warned_version:
                self._warned_version = True
                self._logger.warning(
                    "Frame version %r, expected %d..%d -- matterlights and ambience-rgb "
                    "are out of step",
                    version,
                    MINIMUM_PAYLOAD_VERSION,
                    SUPPORTED_PAYLOAD_VERSION,
                )
        return payload

    def _handle_command(self, command) -> None:
        if command == "stop":
            self.request_stop("stop command")
        else:
            self._logger.debug("Ignoring unknown command %r", command)

    # -- decision ----------------------------------------------------------

    @staticmethod
    def colours_for(payload: dict | None, *, stale: bool) -> tuple[tuple, tuple]:
        """The colours to write for this tick.

        Pure and static so every branch is testable without a socket or a light.
        Any one of: no frame, a stale frame, the display asleep, the RGB switch
        off, or a dark screen, means black. Otherwise mirror the frame's near
        and far colours.
        """

        if payload is None or stale:
            return BLACK, BLACK
        if not payload.get("display_on", True):
            return BLACK, BLACK
        if not payload.get("rgb_on", True):
            return BLACK, BLACK
        # ``lights_on`` is deliberately NOT honoured. The RGB extension has its
        # own switch: turning the lamps off at night should not also kill the
        # case lighting. The field is carried in the payload for diagnostics,
        # but the only master switch for this hardware is rgb_on.
        if payload.get("screen_dark", False):
            return BLACK, BLACK
        near = _colour(payload.get("near"))
        far = _colour(payload.get("far"))
        if near is None or far is None:
            return BLACK, BLACK
        return near, far

    @staticmethod
    def palette_for(payload: dict | None, *, stale: bool) -> list[tuple[tuple, float]]:
        """The frame's palette, or empty when there is nothing to show.

        Empty is always safe: :func:`ambience_rgb.render.spread` falls back to the
        group's single colour, which is exactly the behaviour that existed before
        payload v2. So a v1 publisher, a black frame and a malformed palette all
        degrade to the same well-tested path rather than to a special case.
        """

        if payload is None or stale:
            return []
        entries = payload.get("palette")
        if not isinstance(entries, list):
            return []
        out = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            colour = _colour(entry.get("rgb"))
            weight = entry.get("weight")
            if colour is None or not isinstance(weight, (int, float)) or weight <= 0:
                continue
            out.append((colour, float(weight)))
        return out

    # -- lifecycle ---------------------------------------------------------

    def run(self) -> int:
        endpoint = self._config.endpoint
        try:
            self._socket = transport.bind(endpoint, timeout=RECEIVE_TIMEOUT_SECONDS)
        except transport.TransportError as error:
            # Refused BEFORE any sink exists, deliberately. The exit path
            # below blacks every sink, so a second daemon that got as far as
            # building them would blank the case under the first one's colours.
            self._logger.error("%s", error)
            self.finished.set()
            return 1
        self._logger.info("Listening on %s", endpoint)

        try:
            self._sinks = self._sinks_factory(self._config, self._logger)
            self._hook = self._start_session_hook()
            if self._install_signals:
                self._install_signal_handlers()
            self._last_frame_at = time.monotonic()
            self.ready.set()
            self._loop()
        finally:
            # Every exit path blacks the hardware. Without this the devices sit
            # on whatever they were last told, then drift back to firmware
            # colours a few seconds later -- which looks exactly like a crash.
            self._shutdown()
        # A requested stop is a success. Task Scheduler records the exit code,
        # and a stop that read as a failure would look like a crash there.
        return 0

    def _loop(self) -> None:
        latest: dict | None = None
        while not self._stop.is_set():
            payload = self._receive()
            if self._stop.is_set():
                break
            if payload is not None:
                latest = payload
                self._last_frame_at = time.monotonic()

            stale = (time.monotonic() - self._last_frame_at) > STALE_AFTER_SECONDS
            near, far = self.colours_for(latest, stale=stale)
            if self._config.group_cohesion > 0.0 and (near != BLACK or far != BLACK):
                near, far = cohere(near, far, self._config.group_cohesion)
            # Black means black: never let a gradient repaint a frame that the
            # policy above decided should be dark.
            palette = [] if near == BLACK and far == BLACK else self.palette_for(latest, stale=stale)

            if self._last_colours != (near, far):
                self._logger.debug("near=%s far=%s stale=%s", near, far, stale)
                self._last_colours = (near, far)

            for sink in self._sinks:
                sink.write(near, far, palette)

    def _shutdown(self) -> None:
        for sink in self._sinks:
            sink.black()
        for sink in self._sinks:
            sink.close()
        # The endpoint is released only after every device is: whoever is
        # waiting for it to free -- off --stop-daemon -- may open them next.
        if self._socket is not None:
            transport.release(self._config.endpoint, self._socket)
            self._socket = None
        if self._hook is not None:
            self._hook.stop()
            self._hook = None
        self._logger.info("RGB off; stopped")
        self.finished.set()


def stop_running_daemon(
    config: Config,
    logger: logging.Logger | None = None,
    *,
    timeout: float = STOP_TIMEOUT_SECONDS,
) -> bool:
    """Ask the daemon on ``config.endpoint`` to stop, and wait until it has.

    True once the endpoint is free -- which the daemon releases only after
    blacking and closing every device -- or at once when nothing held it. False
    when it is still held after ``timeout``: a wedged daemon, or some other
    program on the port.
    """

    log = logger or LOGGER
    endpoint = config.endpoint
    if not transport.is_listening(endpoint):
        log.info("No daemon is listening on %s", endpoint)
        return True
    try:
        transport.send(endpoint, STOP_COMMAND)
    except (OSError, transport.TransportError) as error:
        log.warning("Could not send the stop command to %s: %s", endpoint, error)
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not transport.is_listening(endpoint):
            log.info("The daemon on %s has stopped", endpoint)
            return True
        time.sleep(STOP_POLL_SECONDS)
    log.warning("The daemon on %s did not stop within %.0f s", endpoint, timeout)
    return False


def lights_off(
    config: Config,
    logger: logging.Logger | None = None,
    *,
    stop_daemon: bool = False,
    sinks_factory=None,
) -> int:
    """Write black once and exit. What the shutdown unit and task run.

    Deliberately does the least possible: open each sink, black it, close. No
    loop, no waiting on matterlights -- this runs while the machine is already
    going down.

    ``stop_daemon`` first stops a running daemon and waits for it to let go.
    Windows needs that: it opens the soundbar's COM port exclusively, so this
    could not reach it while the daemon held it -- and the daemon's next tick
    would repaint over this black anyway.
    """

    log = logger or LOGGER
    if stop_daemon and not stop_running_daemon(config, log):
        log.warning("Blacking anyway; a device the daemon still holds may refuse to open")
    for sink in (sinks_factory or build_sinks)(config, log):
        try:
            sink.black()
        finally:
            sink.close()
    log.info("RGB off")
    return 0
