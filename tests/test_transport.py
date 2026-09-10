"""The link from matterlights to this daemon, on both platforms.

Two transports, one contract. Linux keeps the unix datagram socket, for its
``0600`` permissions. Windows has no unix *datagram* sockets at all -- its
``AF_UNIX`` is ``SOCK_STREAM`` only -- so it gets UDP on the loopback. Each has
to carry the same three guarantees, and each is asserted here against a real
socket rather than a fake:

* **fire-and-forget:** a send never waits for a reader, and a missing reader is
  not an error;
* **one daemon per endpoint:** a second daemon refuses to start, rather than
  quietly taking the socket over and fighting the first one for the hardware;
* **liveness you can ask about:** ``devices`` and the hardware check keep off
  hardware the daemon is driving, and ``off --stop-daemon`` can tell when the
  daemon has let go.

The unix tests skip where unix datagram sockets do not exist. The UDP tests run
everywhere, which on this rig means the Windows transport is exercised from
Linux on every run, long before Windows ever sees it.
"""

from __future__ import annotations

import errno
import socket
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ambience_rgb import transport
from ambience_rgb.transport import (
    DEFAULT_UDP_PORT,
    LOOPBACK,
    SO_EXCLUSIVEADDRUSE,
    AlreadyRunning,
    TransportError,
    UdpEndpoint,
    UnixEndpoint,
    bind,
    is_listening,
    release,
    send,
)

HAS_UNIX_DATAGRAMS = hasattr(socket, "AF_UNIX") and sys.platform != "win32"


def _free_udp_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind((LOOPBACK, 0))
        return probe.getsockname()[1]
    finally:
        probe.close()


class EndpointTest(unittest.TestCase):
    """``str(endpoint)`` is exactly what matterlights' ``RGB_PUBLISH_SOCKET`` takes.

    The installers print it verbatim for the user to paste, so a formatting
    change here is a broken install instruction, not a cosmetic one.
    """

    def test_a_unix_endpoint_prints_as_its_path(self) -> None:
        # str(path), not a literal: on a Windows host the same Path prints with
        # backslashes -- and a unix endpoint is refused there before anything
        # could print it. The literal made this fail on Windows, and only there.
        path = Path("/run/user/1000/ambience-rgb.sock")
        self.assertEqual(str(UnixEndpoint(path)), str(path))

    def test_a_udp_endpoint_prints_as_a_url(self) -> None:
        self.assertEqual(str(UdpEndpoint(LOOPBACK, 45731)), "udp://127.0.0.1:45731")

    def test_the_default_port_stays_out_of_the_dynamic_range(self) -> None:
        # 49152 and up is where Windows hands out ephemeral ports and where
        # Hyper-V and WSL reserve blocks at boot. A fixed port in there can be
        # taken before the daemon ever starts.
        self.assertLess(DEFAULT_UDP_PORT, 49152)
        self.assertGreater(DEFAULT_UDP_PORT, 1024)


class UdpTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.endpoint = UdpEndpoint(LOOPBACK, _free_udp_port())

    def _bound(self) -> socket.socket:
        sock = bind(self.endpoint, timeout=2.0)
        self.addCleanup(release, self.endpoint, sock)
        return sock

    def test_a_free_port_reads_as_not_running(self) -> None:
        self.assertFalse(is_listening(self.endpoint))

    def test_a_bound_listener_reads_as_running(self) -> None:
        self._bound()
        self.assertTrue(is_listening(self.endpoint))

    def test_a_datagram_arrives(self) -> None:
        sock = self._bound()
        send(self.endpoint, b'{"v": 2}')
        self.assertEqual(sock.recv(65535), b'{"v": 2}')

    def test_it_listens_on_the_loopback_only(self) -> None:
        # A colour is not a secret, but a stop command is an instruction:
        # anything that can reach this socket can turn the case off.
        self.assertEqual(self._bound().getsockname()[0], LOOPBACK)

    def test_a_non_loopback_host_is_refused(self) -> None:
        with self.assertRaises(TransportError):
            bind(UdpEndpoint("0.0.0.0", self.endpoint.port))

    def test_a_second_daemon_is_refused(self) -> None:
        self._bound()
        with self.assertRaises(AlreadyRunning) as caught:
            bind(self.endpoint)
        message = str(caught.exception)
        self.assertIn(str(self.endpoint.port), message)
        self.assertIn("udp_port", message, "say how to move it")

    def test_the_first_daemon_keeps_receiving_after_a_refused_second(self) -> None:
        sock = self._bound()
        with self.assertRaises(AlreadyRunning):
            bind(self.endpoint)
        send(self.endpoint, b"still mine")
        self.assertEqual(sock.recv(65535), b"still mine")

    def test_releasing_frees_the_port(self) -> None:
        sock = bind(self.endpoint)
        release(self.endpoint, sock)
        self.assertFalse(is_listening(self.endpoint))

    def test_sending_to_nobody_is_not_an_error(self) -> None:
        # UDP has no connection to refuse; the first send to a closed loopback
        # port succeeds on both operating systems.
        send(self.endpoint, b"anyone?")

    def test_the_timeout_is_applied(self) -> None:
        sock = bind(self.endpoint, timeout=0.05)
        self.addCleanup(release, self.endpoint, sock)
        with self.assertRaises(socket.timeout):
            sock.recv(1)


class _RecordingSocket:
    """Just enough of a socket to see which options ``bind`` sets."""

    def __init__(self) -> None:
        self.options: list[tuple] = []
        self.bound_to = None

    def setsockopt(self, level, name, value) -> None:
        self.options.append((level, name, value))

    def bind(self, address) -> None:
        self.bound_to = address

    def settimeout(self, _value) -> None:
        pass

    def close(self) -> None:
        pass


class WindowsExclusiveUseTest(unittest.TestCase):
    """🚨 Windows lets another socket share a UDP port unless told not to.

    With ``SO_REUSEADDR`` a second process can bind the same port and Windows
    then delivers each datagram to one of the two -- frames would silently go
    missing. ``SO_EXCLUSIVEADDRUSE`` makes that second bind fail instead, which
    is also what makes the liveness probe trustworthy there.
    """

    def test_windows_claims_the_port_exclusively(self) -> None:
        recorder = _RecordingSocket()
        bind(UdpEndpoint(LOOPBACK, 45731), platform="win32", socket_factory=lambda *_: recorder)
        self.assertIn((socket.SOL_SOCKET, SO_EXCLUSIVEADDRUSE, 1), recorder.options)
        self.assertEqual(recorder.bound_to, (LOOPBACK, 45731))

    def test_linux_sets_no_sharing_option_either_way(self) -> None:
        recorder = _RecordingSocket()
        bind(UdpEndpoint(LOOPBACK, 45731), platform="linux", socket_factory=lambda *_: recorder)
        self.assertEqual(recorder.options, [])


class AddressInUseTest(unittest.TestCase):
    """Which bind failures mean "someone already has it"."""

    def test_eaddrinuse_means_in_use(self) -> None:
        self.assertTrue(transport._address_in_use(OSError(errno.EADDRINUSE, "in use")))

    def test_the_winsock_codes_mean_in_use(self) -> None:
        # WSAEADDRINUSE for an ordinary holder. WSAEACCES when the holder took
        # the port with SO_EXCLUSIVEADDRUSE -- or when Windows has reserved it.
        for code in (10048, 10013):
            with self.subTest(code=code):
                self.assertTrue(transport._address_in_use(OSError(code, "in use")))

    def test_other_failures_are_not(self) -> None:
        self.assertFalse(transport._address_in_use(OSError(errno.EADDRNOTAVAIL, "no such address")))


@unittest.skipUnless(HAS_UNIX_DATAGRAMS, "unix datagram sockets")
class UnixTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "rgb.sock"
        self.endpoint = UnixEndpoint(self.path)

    def _bound(self) -> socket.socket:
        sock = bind(self.endpoint, timeout=2.0)
        self.addCleanup(release, self.endpoint, sock)
        return sock

    def test_an_unbound_path_reads_as_not_running(self) -> None:
        self.assertFalse(is_listening(self.endpoint))

    def test_a_bound_socket_reads_as_running(self) -> None:
        self._bound()
        self.assertTrue(is_listening(self.endpoint))

    def test_a_stale_socket_file_reads_as_not_running(self) -> None:
        """A crashed daemon leaves the file behind; that must not fool anyone."""

        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind(str(self.path))
        server.close()
        self.assertTrue(self.path.exists(), "the file survives the process")
        self.assertFalse(is_listening(self.endpoint))

    def test_a_datagram_arrives(self) -> None:
        sock = self._bound()
        send(self.endpoint, b'{"v": 2}')
        self.assertEqual(sock.recv(65535), b'{"v": 2}')

    def test_only_this_user_can_reach_it(self) -> None:
        self._bound()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_a_stale_file_is_replaced(self) -> None:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind(str(self.path))
        server.close()
        sock = self._bound()
        send(self.endpoint, b"fresh")
        self.assertEqual(sock.recv(65535), b"fresh")

    def test_a_live_daemon_is_not_displaced(self) -> None:
        """🚨 The old bind unlinked whatever was there -- including a LIVE socket.

        A second daemon started by hand then took the path over, the first went
        deaf and stale, and the two fought over the Commander Core: one writing
        colour, the other writing black. hidraw has no exclusive open, so
        nothing stopped them.
        """

        sock = self._bound()
        with self.assertRaises(AlreadyRunning):
            bind(self.endpoint)
        self.assertTrue(self.path.exists(), "the live socket's file must survive")
        send(self.endpoint, b"still mine")
        self.assertEqual(sock.recv(65535), b"still mine")

    def test_a_missing_directory_is_created(self) -> None:
        endpoint = UnixEndpoint(self.path.parent / "nested" / "rgb.sock")
        sock = bind(endpoint)
        self.addCleanup(release, endpoint, sock)
        self.assertTrue(is_listening(endpoint))

    def test_releasing_removes_the_file(self) -> None:
        sock = bind(self.endpoint)
        release(self.endpoint, sock)
        self.assertFalse(self.path.exists())
        self.assertFalse(is_listening(self.endpoint))

    def test_sending_to_nobody_raises_for_the_caller_to_judge(self) -> None:
        with self.assertRaises(OSError):
            send(self.endpoint, b"anyone?")


class NoUnixSocketsTest(unittest.TestCase):
    """Windows CPython has no unix datagram sockets. Nothing may crash on that."""

    def test_liveness_is_simply_false(self) -> None:
        with mock.patch.object(transport, "_unix_family", return_value=None):
            self.assertFalse(is_listening(UnixEndpoint(Path("/tmp/nowhere.sock"))))

    def test_binding_says_what_to_use_instead(self) -> None:
        with mock.patch.object(transport, "_unix_family", return_value=None):
            with self.assertRaises(TransportError) as caught:
                bind(UnixEndpoint(Path("/tmp/nowhere.sock")))
        self.assertIn("udp", str(caught.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
