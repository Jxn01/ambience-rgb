"""How matterlights' frames reach this daemon: a unix datagram socket, or UDP.

Linux keeps the unix socket. It is a file with ``0600`` permissions, so only
this user can publish colours to it or send it the stop command. Windows cannot
have one -- its ``AF_UNIX`` is ``SOCK_STREAM`` only -- and a stream socket would
bring the one thing this link must never have: a connection a publisher can
block on. So Windows gets UDP on ``127.0.0.1``, which keeps every property that
is load-bearing: fire-and-forget, never blocking, a dropped frame harmless
because the next one is a tick away.

The surface is deliberately small. The daemon binds, receives and releases;
``devices``, the hardware check and ``off --stop-daemon`` ask whether a daemon
is listening, and the last of them sends it one datagram.

⚠️ ``socket.AF_UNIX`` is only ever looked up at call time, through
:func:`_unix_family`. On Windows the attribute does not exist, and a reference
evaluated at import would be a crash on the first import -- the same shape as
the ``AttributeError`` that matterlights' publisher once carried.
"""

from __future__ import annotations

import errno
import ipaddress
import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

LOOPBACK = "127.0.0.1"

DEFAULT_UDP_PORT = 45731
"""Arbitrary, and deliberately below 49152: that is Windows' dynamic range,
where ephemeral ports are handed out and Hyper-V and WSL reserve blocks at
boot. A fixed port in there can be taken before the daemon ever starts."""

SO_EXCLUSIVEADDRUSE = getattr(socket, "SO_EXCLUSIVEADDRUSE", ~4)
"""Windows only (``~SO_REUSEADDR``); named here so it exists on every platform."""

# Winsock's numbers for "that address is taken". Python on Windows reports
# socket failures with these rather than the C runtime's errno values. WSAEACCES
# is what a second bind gets when the first holder used SO_EXCLUSIVEADDRUSE --
# and also when Windows has reserved the port for itself, which from here is
# indistinguishable and has the same remedy: pick another port.
_WSAEACCES = 10013
_WSAEADDRINUSE = 10048
_IN_USE = frozenset({errno.EADDRINUSE, _WSAEADDRINUSE, _WSAEACCES})

SEND_TIMEOUT_SECONDS = 1.0


class TransportError(RuntimeError):
    """The endpoint cannot be used, with the reason named."""


class AlreadyRunning(TransportError):
    """A live daemon already holds the endpoint."""


@dataclass(frozen=True, slots=True)
class UnixEndpoint:
    """A unix datagram socket at ``path``."""

    path: Path

    def __str__(self) -> str:
        # Exactly matterlights' RGB_PUBLISH_SOCKET for this endpoint.
        return str(self.path)


@dataclass(frozen=True, slots=True)
class UdpEndpoint:
    """UDP on ``host``:``port``. The host is always the loopback."""

    host: str
    port: int

    def __str__(self) -> str:
        # Exactly matterlights' RGB_PUBLISH_SOCKET for this endpoint.
        return f"udp://{self.host}:{self.port}"


Endpoint = UnixEndpoint | UdpEndpoint


def _unix_family() -> int | None:
    """``socket.AF_UNIX``, or None where there is no such thing (Windows)."""

    return getattr(socket, "AF_UNIX", None)


def _address_in_use(error: OSError) -> bool:
    return error.errno in _IN_USE or getattr(error, "winerror", None) in _IN_USE


def _require_loopback(host: str) -> None:
    try:
        loopback = ipaddress.IPv4Address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise TransportError(
            f"Refusing to listen on {host}: the frames and the stop command are for "
            f"this machine only. Use {LOOPBACK}."
        )


def bind(
    endpoint: Endpoint,
    *,
    timeout: float | None = None,
    platform: str = sys.platform,
    socket_factory=socket.socket,
) -> socket.socket:
    """Listen on ``endpoint``, or raise :class:`AlreadyRunning` if a daemon has it.

    ``platform`` and ``socket_factory`` exist for the tests, which check the
    Windows-only socket option from Linux.
    """

    if isinstance(endpoint, UdpEndpoint):
        sock = _bind_udp(endpoint, platform, socket_factory)
    else:
        sock = _bind_unix(endpoint, socket_factory)
    sock.settimeout(timeout)
    return sock


def _bind_udp(endpoint: UdpEndpoint, platform: str, socket_factory) -> socket.socket:
    _require_loopback(endpoint.host)
    sock = socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if platform == "win32":
            # Without this, Windows lets a second socket share the port via
            # SO_REUSEADDR and hands each datagram to one of the two.
            sock.setsockopt(socket.SOL_SOCKET, SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((endpoint.host, endpoint.port))
    except OSError as error:
        sock.close()
        if _address_in_use(error):
            raise AlreadyRunning(
                f"{endpoint} is already in use: another ambience-rgb is running, another "
                f"program holds UDP port {endpoint.port}, or Windows has reserved it. To "
                f"move it, set socket.udp_port in this config and the same port in "
                f"matterlights' RGB_PUBLISH_SOCKET."
            ) from error
        raise TransportError(f"Cannot listen on {endpoint}: {error}") from error
    return sock


def _bind_unix(endpoint: UnixEndpoint, socket_factory) -> socket.socket:
    family = _unix_family()
    if family is None:
        raise TransportError(
            f"Unix datagram sockets do not exist on this platform, so {endpoint} "
            'cannot be used. Set socket.transport = "udp".'
        )
    path = endpoint.path
    path.parent.mkdir(parents=True, exist_ok=True)
    sock = socket_factory(family, socket.SOCK_DGRAM)
    try:
        sock.bind(str(path))
    except OSError as error:
        if not _address_in_use(error):
            sock.close()
            raise TransportError(f"Cannot listen on {path}: {error}") from error
        # 🚨 Something is already at the path: a live daemon's socket, or the
        # file a crashed one left behind. Only connecting tells them apart --
        # and unlinking a LIVE socket is how a second daemon used to take the
        # path over, leaving the first deaf and the two fighting for hardware.
        if is_listening(endpoint):
            sock.close()
            raise AlreadyRunning(
                f"Another ambience-rgb is already listening on {path}; not starting a second one."
            ) from error
        try:
            path.unlink()
            sock.bind(str(path))
        except OSError as retry_error:
            sock.close()
            raise TransportError(
                f"{path} exists and could not be replaced: {retry_error}"
            ) from retry_error
    os.chmod(path, 0o600)
    return sock


def release(endpoint: Endpoint, sock: socket.socket) -> None:
    """Close the listener and, for a unix socket, remove its file."""

    try:
        sock.close()
    finally:
        if isinstance(endpoint, UnixEndpoint):
            try:
                endpoint.path.unlink()
            except OSError:
                pass


def is_listening(endpoint: Endpoint) -> bool:
    """Whether a daemon currently holds ``endpoint``.

    A unix socket FILE exists whether or not anyone is bound to it, so its
    presence proves nothing; connecting to it does. A UDP port has no file and
    accepts no connection, so the test runs the other way: try to bind it, and a
    refusal means someone has it.

    ⚠️ The UDP probe holds the port for the instant between its bind and its
    close. A daemon starting in exactly that instant is refused and says so.
    """

    if isinstance(endpoint, UdpEndpoint):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.bind((endpoint.host, endpoint.port))
            return False
        except OSError as error:
            return _address_in_use(error)
        finally:
            probe.close()

    family = _unix_family()
    if family is None:
        return False
    probe = socket.socket(family, socket.SOCK_DGRAM)
    try:
        probe.connect(str(endpoint.path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def send(endpoint: Endpoint, data: bytes) -> None:
    """Send one datagram to ``endpoint``.

    Raises ``OSError`` when it cannot be sent -- for a unix socket, that
    includes nobody listening. UDP has no connection to refuse, so there a
    missing listener is silent; ask :func:`is_listening` first when it matters.
    Used for the stop command. matterlights' publisher is the other sender, with
    its own never-raise wrapper around the same calls.
    """

    if isinstance(endpoint, UdpEndpoint):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        address: object = (endpoint.host, endpoint.port)
    else:
        family = _unix_family()
        if family is None:
            raise TransportError(f"Unix datagram sockets do not exist here; cannot reach {endpoint}")
        sock = socket.socket(family, socket.SOCK_DGRAM)
        address = str(endpoint.path)
    try:
        sock.settimeout(SEND_TIMEOUT_SECONDS)
        sock.sendto(data, address)
    finally:
        sock.close()
