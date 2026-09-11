"""The daemon's life: start, refuse a twin, obey a stop, always end black.

These run the real daemon loop against a real socket -- UDP on the loopback, so
they run on Windows as well -- with fake sinks standing in for the hardware.
They pin down the parts the Windows port leans on hardest:

* 🚨 **A second daemon refuses to start without touching the hardware.** Its
  exit path blacks every sink, so a refusal that got as far as building sinks
  would blank the case underneath the first daemon's colours.
* **A stop command is a clean exit, code 0,** with black written and the
  endpoint released before ``run()`` returns. Windows' shutdown task sends it
  and then waits for the endpoint to free, because only then has the daemon let
  go of the soundbar's COM port -- which Windows opens exclusively.
* **A stop request from another thread works,** because that is how the
  session-end window on Windows asks.
"""

from __future__ import annotations

import json
import logging
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from ambience_rgb import transport
from ambience_rgb.config import parse_config
from ambience_rgb.daemon import BLACK, STOP_COMMAND, Daemon, lights_off, stop_running_daemon
from ambience_rgb.transport import LOOPBACK, is_listening, send

FRAME = {
    "v": 2,
    "near": [200, 10, 10],
    "far": [10, 10, 200],
    "palette": [],
    "brightness": 255,
    "active_ratio": 0.5,
    "screen_dark": False,
    "display_on": True,
    "lights_on": True,
    "rgb_on": True,
}
WAIT = 5.0
HAS_UNIX_DATAGRAMS = hasattr(socket, "AF_UNIX") and sys.platform != "win32"

# Long enough that a daemon releasing its endpoint BEFORE closing its devices is
# caught every time, not only when the scheduler happens to be slow. Real
# hardware takes this long and more; an instant fake would hide the ordering.
SLOW_CLOSE_SECONDS = 0.3

# A tick slower than the publisher's frame interval, and a burst queued behind
# it. Replaying the burst in order costs BACKLOG * SLOW_WRITE_SECONDS (4 s);
# skipping to the newest frame costs about one write.
SLOW_WRITE_SECONDS = 0.1
BACKLOG = 40


def _frame(index: int) -> dict:
    """A frame whose colour names it: near and far are both ``(50 + index, 0, 0)``."""

    colour = [50 + index, 0, 0]
    return {**FRAME, "near": colour, "far": colour}


def _free_udp_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind((LOOPBACK, 0))
        return probe.getsockname()[1]
    finally:
        probe.close()


def _udp_config():
    return parse_config({"socket": {"transport": "udp", "udp_port": _free_udp_port()}}, platform="linux")


class _FakeSink:
    def __init__(
        self,
        journal: list | None = None,
        label: str = "sink",
        *,
        close_delay: float = 0.0,
        write_delay: float = 0.0,
    ) -> None:
        self.name = label
        self.journal = journal if journal is not None else []
        self.close_delay = close_delay
        self.write_delay = write_delay
        self.writes: list[tuple] = []
        self.blacks = 0
        self.closed = 0
        self.lit = threading.Event()

    def write(self, near, far, palette=None) -> None:
        time.sleep(self.write_delay)
        self.writes.append((tuple(near), tuple(far)))
        if near != BLACK or far != BLACK:
            self.lit.set()

    def black(self) -> None:
        self.blacks += 1
        self.journal.append(f"{self.name}: black")

    def close(self) -> None:
        time.sleep(self.close_delay)
        self.closed += 1
        self.journal.append(f"{self.name}: closed")


class _Running:
    """A daemon on its own thread, keeping what ``run()`` returned."""

    def __init__(self, config, sinks: list, **kwargs) -> None:
        self.daemon = Daemon(config, sinks_factory=lambda *_: sinks, install_signals=False, **kwargs)
        self.code: int | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        self.code = self.daemon.run()

    def start(self) -> "_Running":
        self.thread.start()
        # Waited on from the daemon's side: polling is_listening() here would
        # race the daemon's own bind, since a UDP probe briefly holds the port.
        if not self.daemon.ready.wait(WAIT):
            raise AssertionError("the daemon never started listening")
        return self

    def join(self) -> bool:
        self.thread.join(WAIT)
        return not self.thread.is_alive()

    def stop(self) -> None:
        if self.thread.is_alive():
            self.daemon.request_stop("test cleanup")
            self.join()


class BacklogTest(unittest.TestCase):
    """🚨 Only the newest frame matters: the daemon must never replay a queue.

    Measured on the Windows half, 2026-09-11: the case ran minutes behind the
    screen while the bulbs, fed by the same publisher, kept up. Each tick read
    ONE datagram -- the oldest waiting -- and then spent longer writing the
    hardware than matterlights takes between frames, so frames piled up in the
    socket's receive buffer (64 KB on Windows, some 180 frames) and were played
    back in order. A stop command sent into it at 02:05:13 had still not been
    honoured eight minutes later -- queued behind the frames or dropped from a
    full buffer. A unix datagram socket caps its queue at about ten and the
    sender drops the rest, which is why Linux never showed it.
    """

    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.config = _udp_config()
        self.endpoint = self.config.endpoint
        self.sink = _FakeSink(write_delay=SLOW_WRITE_SECONDS)
        self.running = _Running(self.config, [self.sink]).start()
        self.addCleanup(self.running.stop)

    def _queue_a_backlog(self) -> None:
        # One frame first, so the daemon is busy with a slow write when the
        # burst lands and has to find it waiting in the socket afterwards.
        send(self.endpoint, json.dumps(_frame(1)).encode("utf-8"))
        self.assertTrue(self.sink.lit.wait(WAIT), "the first frame never reached a sink")
        for index in range(2, BACKLOG + 1):
            send(self.endpoint, json.dumps(_frame(index)).encode("utf-8"))

    def test_a_backlog_is_skipped_to_the_newest_frame(self) -> None:
        self._queue_a_backlog()
        newest = tuple(_frame(BACKLOG)["near"])
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline and newest not in [near for near, _far in self.sink.writes]:
            time.sleep(0.02)
        written = [near for near, _far in self.sink.writes]
        self.assertIn(newest, written, "the newest frame never reached the sink")
        replayed = {near for near in written[: written.index(newest)]} - {BLACK, tuple(_frame(1)["near"])}
        self.assertLessEqual(
            len(replayed), 3, f"{len(replayed)} stale frames were written before the newest one"
        )

    def test_a_stop_queued_behind_a_backlog_is_honoured_without_replaying_it(self) -> None:
        """The shutdown task's stop joins the same queue as the frames.

        Replayed in order it waited behind minutes of stale colour -- far past the
        task's one-minute limit, with the soundbar's COM port still held.
        """

        self._queue_a_backlog()
        started = time.monotonic()
        send(self.endpoint, STOP_COMMAND)
        self.assertTrue(self.running.join(), "still running: the stop is waiting behind the backlog")
        self.assertLess(
            time.monotonic() - started,
            BACKLOG * SLOW_WRITE_SECONDS / 2,
            "the stop was honoured only after the backlog had been replayed",
        )


class LifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.config = _udp_config()
        self.endpoint = self.config.endpoint
        self.journal: list[str] = []
        self.sink = _FakeSink(self.journal, "daemon")

    def _start(self, **kwargs) -> _Running:
        running = _Running(self.config, [self.sink], **kwargs).start()
        self.addCleanup(running.stop)
        return running

    def test_a_frame_reaches_the_sinks(self) -> None:
        self._start()
        send(self.endpoint, json.dumps(FRAME).encode("utf-8"))
        self.assertTrue(self.sink.lit.wait(WAIT), "the frame never reached a sink")

    def test_a_stop_command_is_a_clean_exit(self) -> None:
        running = self._start()
        send(self.endpoint, STOP_COMMAND)
        self.assertTrue(running.join(), "still running after the stop command")
        self.assertEqual(running.code, 0, "a requested stop is success, not a crash")
        self.assertGreaterEqual(self.sink.blacks, 1)
        self.assertEqual(self.sink.closed, 1)
        self.assertFalse(is_listening(self.endpoint), "the endpoint is free once it is gone")

    def test_a_stop_command_is_not_read_as_a_frame(self) -> None:
        # It carries no payload version, and must not trip the warning that
        # says matterlights and ambience-rgb are out of step.
        logging.disable(logging.NOTSET)
        running = self._start()
        with self.assertNoLogs("ambience_rgb.daemon", level="WARNING"):
            send(self.endpoint, STOP_COMMAND)
            self.assertTrue(running.join())

    def test_a_stop_request_from_another_thread_ends_it(self) -> None:
        """How the Windows session-end window asks: from its own thread."""

        running = self._start()
        threading.Thread(target=running.daemon.request_stop, args=("session ending",)).start()
        self.assertTrue(running.join())
        self.assertEqual(running.code, 0)
        self.assertGreaterEqual(self.sink.blacks, 1)

    def test_finished_is_set_only_once_the_hardware_is_black_and_released(self) -> None:
        """The session-end hook holds Windows back on this event, so it must not be early."""

        running = self._start()
        seen: list[list[str]] = []

        def watch() -> None:
            running.daemon.finished.wait(WAIT)
            seen.append(list(self.journal))

        watcher = threading.Thread(target=watch)
        watcher.start()
        running.daemon.request_stop("test")
        self.assertTrue(running.join())
        watcher.join(WAIT)
        self.assertEqual(seen, [["daemon: black", "daemon: closed"]])

    def test_the_session_hook_is_given_the_stop_and_stopped_at_the_end(self) -> None:
        hooks = []

        class Hook:
            def __init__(hook, on_session_end, finished, logger) -> None:
                hook.on_session_end = on_session_end
                hook.finished = finished
                hook.stopped = False
                hooks.append(hook)

            def stop(hook) -> None:
                hook.stopped = True

        running = self._start(session_hook_factory=Hook)
        self.assertEqual(len(hooks), 1)
        hooks[0].on_session_end("WM_QUERYENDSESSION")
        self.assertTrue(running.join())
        self.assertEqual(running.code, 0)
        self.assertTrue(hooks[0].stopped)
        self.assertIs(hooks[0].finished, running.daemon.finished)

    def test_a_second_daemon_refuses_without_touching_the_hardware(self) -> None:
        """🚨 Its exit path blacks every sink -- underneath the first daemon's colours."""

        holder = transport.bind(self.endpoint)
        self.addCleanup(transport.release, self.endpoint, holder)
        built = []
        daemon = Daemon(
            self.config,
            sinks_factory=lambda *_: built.append(True) or [self.sink],
            install_signals=False,
        )
        self.assertNotEqual(daemon.run(), 0)
        self.assertEqual(built, [], "no sink may even be built")
        self.assertEqual(self.sink.blacks, 0)


@unittest.skipUnless(HAS_UNIX_DATAGRAMS, "unix datagram sockets")
class UnixLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_the_socket_file_is_gone_after_a_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = parse_config({"socket": {"path": f"{directory}/rgb.sock"}}, platform="linux")
            running = _Running(config, [_FakeSink()]).start()
            send(config.endpoint, STOP_COMMAND)
            self.assertTrue(running.join())
            self.assertEqual(running.code, 0)
            self.assertFalse(Path(directory, "rgb.sock").exists())


class StopRunningDaemonTest(unittest.TestCase):
    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.config = _udp_config()

    def test_nothing_running_is_already_stopped(self) -> None:
        self.assertTrue(stop_running_daemon(self.config, timeout=0.5))

    def test_it_returns_only_once_the_hardware_is_released(self) -> None:
        sink = _FakeSink(close_delay=SLOW_CLOSE_SECONDS)
        running = _Running(self.config, [sink]).start()
        self.addCleanup(running.stop)
        self.assertTrue(stop_running_daemon(self.config, timeout=WAIT))
        self.assertEqual(sink.closed, 1, "returned before the daemon let go of its devices")
        self.assertTrue(running.join())
        self.assertEqual(running.code, 0)

    def test_a_holder_that_ignores_it_is_reported_after_the_timeout(self) -> None:
        holder = transport.bind(self.config.endpoint)
        self.addCleanup(transport.release, self.config.endpoint, holder)
        started = time.monotonic()
        self.assertFalse(stop_running_daemon(self.config, timeout=0.3))
        self.assertLess(time.monotonic() - started, 2.0)


class LightsOffTest(unittest.TestCase):
    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.config = _udp_config()

    def test_stop_daemon_releases_the_hardware_before_blacking_it_again(self) -> None:
        """🚨 Windows opens the soundbar's COM port EXCLUSIVELY.

        The shutdown task used to be able to run only beside a live daemon: its
        own open of COM3 would fail, and even where it succeeded the daemon's
        next tick repainted over its black. So it stops the daemon first and
        opens nothing until the daemon has closed everything.
        """

        journal: list[str] = []
        daemon_sink = _FakeSink(journal, "daemon", close_delay=SLOW_CLOSE_SECONDS)
        running = _Running(self.config, [daemon_sink]).start()
        self.addCleanup(running.stop)
        off_sink = _FakeSink(journal, "off")

        def build(*_args):
            journal.append("off: built")
            return [off_sink]

        self.assertEqual(lights_off(self.config, stop_daemon=True, sinks_factory=build), 0)
        self.assertIn("daemon: closed", journal, "the daemon never let go")
        self.assertLess(journal.index("daemon: closed"), journal.index("off: built"))
        self.assertEqual((off_sink.blacks, off_sink.closed), (1, 1))
        self.assertTrue(running.join())

    def test_without_the_flag_a_running_daemon_is_left_alone(self) -> None:
        running = _Running(self.config, [_FakeSink()]).start()
        self.addCleanup(running.stop)
        lights_off(self.config, sinks_factory=lambda *_: [_FakeSink()])
        self.assertTrue(running.thread.is_alive())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
