"""Subscribe to matterlights' ambience frames and drive the RGB hardware.

The loop is deliberately dull: receive a frame, decide a colour, write it to
every sink, repeat. All the judgement lives upstream in matterlights, which has
already decided whether the screen is dark and whether the lights are meant to
be on. Mirroring the bulbs means obeying those verdicts, not re-deriving them.

⚠️ **Black is a thing you keep writing, not a thing you write once.** None of
this hardware has an off state. The Commander Core reverts to its firmware
pattern about ten seconds after the last commit, and the ASRock headers fall
back to the board's own green. So the loop writes on every tick whether the
colour changed or not, and "off" is a black frame repeated -- which is also why
stopping the daemon has to write black explicitly on the way out.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import time
from pathlib import Path

from linux_rgb.config import Config
from linux_rgb.sinks import ResilientSink, build_sinks

LOGGER = logging.getLogger("linux_rgb.daemon")

BLACK = (0, 0, 0)

# How long to wait on the socket before ticking anyway. Short enough that a tick
# still happens several times a second when matterlights is quiet, so black keeps
# being refreshed and the Commander Core never falls back to green.
RECEIVE_TIMEOUT_SECONDS = 0.5

# With no frame for this long, matterlights is stopped or wedged. Go dark rather
# than hold the last colour forever -- a frozen frame looks like working sync.
STALE_AFTER_SECONDS = 3.0

# The publisher's payload version this daemon understands.
SUPPORTED_PAYLOAD_VERSION = 1


def _colour(value) -> tuple[int, int, int] | None:
    """A payload field as an RGB triple, or None if it is not one.

    Checking the LENGTH alone is not enough, and the earlier version did exactly
    that: ``tuple("red")[:3]`` is ``('r', 'e', 'd')``, which is three items and
    passes. Those characters then reach the sinks, where packing them raises and
    degrades the sink for the retry window on every tick -- a malformed frame
    quietly taking the lights down instead of being ignored.
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
    def __init__(self, config: Config, logger: logging.Logger | None = None) -> None:
        self._config = config
        self._logger = logger or LOGGER
        self._socket: socket.socket | None = None
        self._sinks: list[ResilientSink] = []
        self._running = False
        self._last_frame_at = 0.0
        self._last_colours: tuple[tuple, tuple] | None = None
        self._warned_version = False

    # -- socket ------------------------------------------------------------

    def _bind(self) -> socket.socket:
        path = self._config.socket_path
        path.parent.mkdir(parents=True, exist_ok=True)
        # A datagram socket left behind by a previous run would make bind fail
        # with EADDRINUSE even though nothing is listening.
        if path.exists() or path.is_socket():
            try:
                path.unlink()
            except OSError:
                pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.bind(str(path))
        sock.settimeout(RECEIVE_TIMEOUT_SECONDS)
        os.chmod(path, 0o600)
        self._logger.info("Listening on %s", path)
        return sock

    def _receive(self) -> dict | None:
        assert self._socket is not None
        try:
            data = self._socket.recv(65535)
        except socket.timeout:
            return None
        except OSError as error:
            self._logger.warning("Receive failed: %s", error)
            return None
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._logger.debug("Ignoring an unparseable datagram")
            return None
        if not isinstance(payload, dict):
            return None
        version = payload.get("v")
        if version != SUPPORTED_PAYLOAD_VERSION and not self._warned_version:
            self._warned_version = True
            self._logger.warning(
                "Frame version %r, expected %d -- matterlights and linux-rgb are out of step",
                version,
                SUPPORTED_PAYLOAD_VERSION,
            )
        return payload

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

    # -- lifecycle ---------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        def handle(signum, _frame):
            self._logger.info("Signal %s; turning RGB off", signal.Signals(signum).name)
            self._running = False

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, handle)

    def run(self) -> int:
        self._sinks = build_sinks(self._config, self._logger)
        self._socket = self._bind()
        self._install_signal_handlers()
        self._running = True
        self._last_frame_at = time.monotonic()

        try:
            self._loop()
        finally:
            # Every exit path blacks the hardware. Without this the devices sit
            # on whatever they were last told, then drift back to firmware
            # colours a few seconds later -- which looks exactly like a crash.
            self._shutdown()
        return 0

    def _loop(self) -> None:
        latest: dict | None = None
        while self._running:
            payload = self._receive()
            if payload is not None:
                latest = payload
                self._last_frame_at = time.monotonic()

            stale = (time.monotonic() - self._last_frame_at) > STALE_AFTER_SECONDS
            near, far = self.colours_for(latest, stale=stale)

            if self._last_colours != (near, far):
                self._logger.debug("near=%s far=%s stale=%s", near, far, stale)
                self._last_colours = (near, far)

            for sink in self._sinks:
                sink.write(near, far)

    def _shutdown(self) -> None:
        for sink in self._sinks:
            sink.black()
        for sink in self._sinks:
            sink.close()
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        try:
            self._config.socket_path.unlink()
        except OSError:
            pass
        self._logger.info("RGB off; stopped")


def lights_off(config: Config, logger: logging.Logger | None = None) -> int:
    """Write black once and exit. Used by the shutdown unit.

    Deliberately does the least possible: open each sink, black it, close. No
    socket, no loop, no waiting on matterlights -- this runs while the machine
    is already going down.
    """

    log = logger or LOGGER
    for sink in build_sinks(config, log):
        try:
            sink.black()
        finally:
            sink.close()
    log.info("RGB off")
    return 0
