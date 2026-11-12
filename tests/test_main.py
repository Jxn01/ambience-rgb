"""The command line: the flags the Windows tasks depend on, and logging without a console.

**pythonw has no stderr.** The Windows tasks run the daemon under
``pythonw.exe`` so that no console window flashes up at logon, and there
``sys.stderr`` is ``None``: a ``StreamHandler`` pointed at it drops every line,
silently. So without a console the log goes to a file, and these tests hold
that in place.
"""

from __future__ import annotations

import io
import logging
import os
import logging.handlers
import re
import socket
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ambience_rgb import __main__ as cli
from ambience_rgb import hints
from ambience_rgb.config import load_config
from ambience_rgb.daemon import Daemon
from ambience_rgb.transport import LOOPBACK, UdpEndpoint, bind, release


def _free_port(kind: int) -> int:
    probe = socket.socket(socket.AF_INET, kind)
    try:
        probe.bind((LOOPBACK, 0))
        return probe.getsockname()[1]
    finally:
        probe.close()


def _write_config(directory: str, port: int, *, devices: bool = False) -> Path:
    """UDP on a free port, OpenRGB on a closed one, own drivers and HA only if asked."""

    path = Path(directory) / "config.toml"
    enabled = "true" if devices else "false"
    path.write_text(
        "[socket]\n"
        'transport = "udp"\n'
        f"udp_port = {port}\n"
        "[openrgb]\n"
        f"port = {_free_port(socket.SOCK_STREAM)}\n"
        f"[commander]\nenabled = {enabled}\n"
        f"[katana_ha]\nenabled = {enabled}\n"
        f'url = "http://{LOOPBACK}:{_free_port(socket.SOCK_STREAM)}"\n',
        encoding="utf-8",
    )
    return path


class _LoggingIsolation(unittest.TestCase):
    """Put the root logger back afterwards, and close any file it opened.

    Closing matters on Windows: an open log file would stop the temporary
    directory from being removed.
    """

    def setUp(self) -> None:
        root = logging.getLogger()
        saved_handlers, saved_level = root.handlers[:], root.level

        def restore() -> None:
            for handler in root.handlers[:]:
                if handler not in saved_handlers:
                    handler.close()
            root.handlers[:] = saved_handlers
            root.setLevel(saved_level)

        self.addCleanup(restore)

    @staticmethod
    def _read(path: Path) -> str:
        for handler in logging.getLogger().handlers:
            handler.close()
        return path.read_text(encoding="utf-8")


class ArgumentTest(unittest.TestCase):
    def test_stop_daemon_only_goes_with_off(self) -> None:
        with self.assertRaises(SystemExit) as caught, redirect_stderr(io.StringIO()):
            cli.main(["run", "--stop-daemon"])
        self.assertEqual(caught.exception.code, 2)


class LoggingTest(_LoggingIsolation):
    def test_without_a_console_it_logs_to_the_state_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state" / "ambience-rgb.log"
            with mock.patch.object(sys, "stderr", None), mock.patch.object(
                cli.paths, "default_log_path", return_value=target
            ):
                cli._configure_logging(False)
                logging.getLogger("ambience_rgb").info("hello from pythonw")
            self.assertIn("hello from pythonw", self._read(target))

    def test_a_log_file_can_be_named(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "logs" / "off.log"
            cli._configure_logging(False, target)
            logging.getLogger("ambience_rgb").warning("named")
            self.assertIn("named", self._read(target))

    def test_file_lines_carry_a_timestamp(self) -> None:
        # Under systemd the journal stamps every line. A file has no journal.
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "x.log"
            cli._configure_logging(False, target)
            logging.getLogger("ambience_rgb").warning("stamped")
            self.assertRegex(self._read(target), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")

    def test_the_file_is_rotated_not_grown_forever(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cli._configure_logging(False, Path(directory) / "x.log")
            handlers = [
                h for h in logging.getLogger().handlers if isinstance(h, logging.handlers.RotatingFileHandler)
            ]
            self.assertEqual(len(handlers), 1)
            self.assertGreater(handlers[0].maxBytes, 0)
            self.assertGreater(handlers[0].backupCount, 0)
            for handler in handlers:
                handler.close()

    def test_an_unwritable_log_does_not_stop_the_program(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            blocker = Path(directory) / "a-file"
            blocker.write_text("not a directory", encoding="utf-8")
            with mock.patch.object(sys, "stderr", None):
                cli._configure_logging(False, blocker / "sub" / "x.log")
                logging.getLogger("ambience_rgb").error("still standing")

    def test_with_a_console_it_logs_there(self) -> None:
        stream = io.StringIO()
        with mock.patch.object(sys, "stderr", stream):
            cli._configure_logging(False)
            logging.getLogger("ambience_rgb").info("to the console")
        self.assertIn("to the console", stream.getvalue())


class OffCommandTest(_LoggingIsolation):
    def test_off_stop_daemon_stops_a_running_daemon(self) -> None:
        """What the Windows shutdown task runs."""

        with tempfile.TemporaryDirectory() as directory:
            config_path = _write_config(directory, _free_port(socket.SOCK_DGRAM))
            daemon = Daemon(load_config(config_path), sinks_factory=lambda *_: [], install_signals=False)
            thread = threading.Thread(target=daemon.run, daemon=True)
            thread.start()
            self.assertTrue(daemon.ready.wait(5))
            with mock.patch("ambience_rgb.daemon.build_sinks", return_value=[]):
                code = cli.main(
                    ["--config", str(config_path), "--log-file", str(Path(directory) / "off.log"),
                     "off", "--stop-daemon"]
                )
            self.assertEqual(code, 0)
            thread.join(5)
            self.assertFalse(thread.is_alive(), "the daemon is still running")
            self._read(Path(directory) / "off.log")


class _FakeHA:
    """Stands in for HAStreamClient: answers ``status()`` from ``answer``, and remembers the token."""

    answer: dict = {}
    tokens: list[str] = []

    def __init__(self, _url: str, token: str, _timeout: float = 2.0) -> None:
        _FakeHA.tokens.append(token)

    def status(self) -> dict:
        return dict(_FakeHA.answer)

    def close(self) -> None:
        pass


def _fake_ha(**answer):
    """Patch HA in: the client, and a token from the environment rather than a real ``.ha-token``."""

    _FakeHA.answer = {
        "screen_sync": True,
        "receiving": False,
        "available": True,
        "frames_received": 4,
        "frames_dropped": 1,
        "writes_sent": 2,
        "write_errors": 0,
        **answer,
    }
    _FakeHA.tokens = []
    return (
        mock.patch("ambience_rgb.ha_stream.HAStreamClient", _FakeHA),
        mock.patch.dict(os.environ, {"AMBIENCE_RGB_HA_TOKEN": "test-token"}),
    )


class DevicesTest(_LoggingIsolation):
    def test_it_prints_the_value_matterlights_needs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            port = _free_port(socket.SOCK_DGRAM)
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = cli.main(["--config", str(_write_config(directory, port)), "devices"])
        self.assertEqual(code, 0)
        self.assertIn(f"RGB_PUBLISH_SOCKET=udp://127.0.0.1:{port}", out.getvalue())

    def test_it_keeps_off_hardware_the_daemon_holds_and_says_how_to_stop_it(self) -> None:
        opened = []

        class Refuse:
            def __init__(self, *_args, **_kwargs) -> None:
                opened.append(type(self).__name__)
                raise AssertionError("a held device was opened")

        with tempfile.TemporaryDirectory() as directory:
            port = _free_port(socket.SOCK_DGRAM)
            config_path = _write_config(directory, port, devices=True)
            endpoint = UdpEndpoint(LOOPBACK, port)
            holder = bind(endpoint)
            out = io.StringIO()
            try:
                client, token = _fake_ha()
                with mock.patch("ambience_rgb.commander_core.CommanderCore", Refuse), client, token, redirect_stdout(
                    out
                ), redirect_stderr(io.StringIO()):
                    cli.main(["--config", str(config_path), "devices"])
            finally:
                release(endpoint, holder)
        self.assertEqual(opened, [])
        self.assertIn("held by the running daemon", out.getvalue())
        self.assertIn(hints.stop_daemon(), out.getvalue())
        # HA is asked even then: it is another machine, and asking changes nothing.
        self.assertEqual(_FakeHA.tokens, ["test-token"])
        self.assertIn("Screen sync on, receiving no, soundbar connected yes", out.getvalue())

    def test_it_says_when_home_assistant_is_dropping_the_colours(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            port = _free_port(socket.SOCK_DGRAM)
            config_path = _write_config(directory, port, devices=True)
            endpoint = UdpEndpoint(LOOPBACK, port)
            holder = bind(endpoint)  # held, so no real device is opened
            out = io.StringIO()
            try:
                client, token = _fake_ha(screen_sync=False)
                with client, token, redirect_stdout(out), redirect_stderr(io.StringIO()):
                    cli.main(["--config", str(config_path), "devices"])
            finally:
                release(endpoint, holder)
        self.assertIn("Screen sync OFF", out.getvalue())
        self.assertIn("drops every colour", out.getvalue())

    def test_an_unreachable_home_assistant_is_reported_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            port = _free_port(socket.SOCK_DGRAM)
            config_path = _write_config(directory, port, devices=True)  # HA's url: a closed loopback port
            endpoint = UdpEndpoint(LOOPBACK, port)
            holder = bind(endpoint)
            out = io.StringIO()
            try:
                with mock.patch.dict(os.environ, {"AMBIENCE_RGB_HA_TOKEN": "test-token"}), redirect_stdout(
                    out
                ), redirect_stderr(io.StringIO()):
                    code = cli.main(["--config", str(config_path), "devices"])
            finally:
                release(endpoint, holder)
        self.assertEqual(code, 0)
        self.assertIn("unavailable: Home Assistant unreachable", out.getvalue())


class DevicesContentionTest(_LoggingIsolation):
    def test_it_warns_when_openrgb_has_the_commander_core_too(self) -> None:
        from types import SimpleNamespace

        controllers = [SimpleNamespace(name="Corsair Commander Core", led_count=0, zones=[])]

        class Server:
            def __init__(self, *_args, **_kwargs) -> None:
                self.controllers = controllers

            def __enter__(self):
                return self

            def __exit__(self, *_exc) -> bool:
                return False

        class Refuse:
            def __init__(self, *_args, **_kwargs) -> None:
                raise AssertionError("a held device was opened")

        with tempfile.TemporaryDirectory() as directory:
            port = _free_port(socket.SOCK_DGRAM)
            config_path = _write_config(directory, port, devices=True)
            endpoint = UdpEndpoint(LOOPBACK, port)
            holder = bind(endpoint)  # held, so no real device is opened
            out = io.StringIO()
            try:
                client, token = _fake_ha()
                with mock.patch("ambience_rgb.openrgb_client.OpenRGBClient", Server), mock.patch(
                    "ambience_rgb.commander_core.CommanderCore", Refuse
                ), client, token, redirect_stdout(out), redirect_stderr(io.StringIO()):
                    cli.main(["--config", str(config_path), "devices"])
            finally:
                release(endpoint, holder)
        self.assertIn("detector", out.getvalue(), "say that OpenRGB must let go of it")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
