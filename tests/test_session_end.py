"""The Windows session-end window: its decisions, on either platform.

The Windows code cannot be run where it is written (Linux). What CAN run anywhere is its decision logic: the
window procedure and the console handler are plain Python methods that receive
message numbers. On Linux a thin fake stands in for the Win32 calls they make -- every
entry point answers "1, fine", except ``GetMessageW``, which answers "quit" so
the message pump ends at once instead of spinning. On Windows the very same
tests run against the real ``user32`` and ``kernel32``, and the factory test
creates a real hidden window.

**What this cannot tell you:** whether Windows really delivers these
messages to this window at a session end. It does at logoff and does not at
shutdown; that is why the shutdown task exists, and it takes a real Windows
restart to prove.
"""

from __future__ import annotations

import contextlib
import ctypes
import importlib
import logging
import sys
import threading
import time
import unittest

_PREFIX = "ambience_rgb.session_end"


class _FakeFunction:
    argtypes: object = None
    restype: object = None

    def __init__(self, result: int = 1) -> None:
        self.result = result
        self.calls: list[tuple] = []

    def __call__(self, *args, **_kwargs):
        self.calls.append(args)
        return self.result


class _FakeDll:
    OVERRIDES = {"GetMessageW": 0}

    def __init__(self, *_args, **_kwargs) -> None:
        self._functions: dict[str, _FakeFunction] = {}

    def __getattr__(self, name: str) -> _FakeFunction:
        if name.startswith("_"):
            raise AttributeError(name)
        functions = self.__dict__["_functions"]
        if name not in functions:
            functions[name] = _FakeFunction(self.OVERRIDES.get(name, 1))
        return functions[name]


@contextlib.contextmanager
def win32_environment():
    """Make the Windows module importable on Linux, then put everything back.

    On Windows it does nothing: the module imports for real there.
    """

    if sys.platform == "win32":
        yield
        return
    saved_platform = sys.platform
    saved_modules = {name: module for name, module in sys.modules.items() if name.startswith(_PREFIX)}
    missing = [
        name
        for name in ("WinDLL", "WINFUNCTYPE", "get_last_error", "WinError")
        if not hasattr(ctypes, name)
    ]
    for name in saved_modules:
        del sys.modules[name]
    fakes = {
        "WinDLL": _FakeDll,
        "WINFUNCTYPE": ctypes.CFUNCTYPE,
        "get_last_error": lambda: 0,
        "WinError": lambda code=0: OSError(code, "fake"),
    }
    for name in missing:
        setattr(ctypes, name, fakes[name])
    sys.platform = "win32"
    try:
        yield
    finally:
        sys.platform = saved_platform
        for name in missing:
            delattr(ctypes, name)
        for name in [n for n in sys.modules if n.startswith(_PREFIX)]:
            del sys.modules[name]
        sys.modules.update(saved_modules)


_WM_CLOSE = 0x0010
_WM_QUERYENDSESSION = 0x0011
_WM_ENDSESSION = 0x0016
_CTRL_C_EVENT = 0
_CTRL_SHUTDOWN_EVENT = 6


class NonWindowsTest(unittest.TestCase):
    def test_elsewhere_it_is_a_no_op_that_can_be_stopped(self) -> None:
        from ambience_rgb.session_end import start_session_end_hook

        # No warning either: the factory's fallback would also hand back a
        # no-op if the platform check were gone and the Windows import failed.
        with self.assertNoLogs("ambience_rgb.session_end", level="WARNING"):
            hook = start_session_end_hook(lambda _reason: None, threading.Event(), platform="linux")
        hook.stop()
        self.assertEqual(type(hook).__name__, "_NullHook")


class WindowProcedureTest(unittest.TestCase):
    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self._environment = win32_environment()
        self._environment.__enter__()
        self.addCleanup(self._environment.__exit__, None, None, None)
        windows = importlib.import_module("ambience_rgb.session_end.windows")
        self.reasons: list[str] = []
        self.finished = threading.Event()
        self.hook = windows.Win32SessionEndHook(
            self.reasons.append, self.finished, logging.getLogger("test"), wait_seconds=0.3
        )

    def _send(self, message: int, wparam: int = 0):
        # hwnd 1 is not a window. On Windows the calls the procedure makes with
        # it -- DestroyWindow, DefWindowProcW -- simply fail, harmlessly.
        return self.hook._window_proc(1, message, wparam, 0)

    def test_the_query_asks_the_daemon_to_stop_and_never_vetoes(self) -> None:
        self.assertEqual(self._send(_WM_QUERYENDSESSION), 1, "returning 0 would block the logoff")
        self.assertEqual(self.reasons, ["WM_QUERYENDSESSION"])

    def test_it_asks_only_once_per_session_end(self) -> None:
        self._send(_WM_QUERYENDSESSION)
        self.finished.set()
        self._send(_WM_ENDSESSION, wparam=1)
        self.assertEqual(self.reasons, ["WM_QUERYENDSESSION"])

    def test_the_end_holds_the_session_until_the_hardware_is_black(self) -> None:
        """Windows may end the process the moment WM_ENDSESSION returns."""

        threading.Timer(0.15, self.finished.set).start()
        started = time.monotonic()
        self._send(_WM_ENDSESSION, wparam=1)
        self.assertGreaterEqual(time.monotonic() - started, 0.14, "returned before the black was written")
        self.assertEqual(self.reasons, ["WM_ENDSESSION"])

    def test_the_hold_is_bounded(self) -> None:
        # A hung app earns the "this app is preventing sign-out" screen.
        started = time.monotonic()
        self._send(_WM_ENDSESSION, wparam=1)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_a_cancelled_end_does_not_stop_anything_by_itself(self) -> None:
        self._send(_WM_ENDSESSION, wparam=0)
        self.assertEqual(self.reasons, [])

    def test_a_close_from_outside_is_a_stop_request(self) -> None:
        # An orderly "please exit" to a windowed process -- what Task
        # Scheduler's End may send before it resorts to TerminateProcess.
        self._send(_WM_CLOSE)
        self.assertEqual(self.reasons, ["WM_CLOSE"])

    def test_our_own_close_is_not(self) -> None:
        self.hook.stop()
        self._send(_WM_CLOSE)
        self.assertEqual(self.reasons, [])

    def test_a_console_shutdown_asks_and_waits(self) -> None:
        threading.Timer(0.15, self.finished.set).start()
        started = time.monotonic()
        self.assertTrue(self.hook._console_ctrl_handler(_CTRL_SHUTDOWN_EVENT))
        self.assertGreaterEqual(time.monotonic() - started, 0.14)
        self.assertEqual(len(self.reasons), 1)

    def test_ctrl_c_is_left_to_python(self) -> None:
        # Returning False passes it on to Python's own handler, which raises
        # SIGINT in the main thread -- the daemon's signal path.
        self.assertFalse(self.hook._console_ctrl_handler(_CTRL_C_EVENT))
        self.assertEqual(self.reasons, [])

    def test_a_failing_callback_does_not_take_the_window_down(self) -> None:
        def explode(_reason):
            raise RuntimeError("boom")

        self.hook._on_session_end = explode
        self.assertEqual(self._send(_WM_QUERYENDSESSION), 1)


class FactoryTest(unittest.TestCase):
    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_windows_gets_the_window(self) -> None:
        # On Windows this creates a real hidden window and a real message loop.
        with win32_environment():
            session_end = importlib.import_module("ambience_rgb.session_end")
            hook = session_end.start_session_end_hook(lambda _r: None, threading.Event(), platform="win32")
            try:
                self.assertEqual(type(hook).__name__, "Win32SessionEndHook")
            finally:
                hook.stop()

    def test_a_hook_that_cannot_be_built_degrades_to_a_no_op(self) -> None:
        """Losing the logoff black is much better than a daemon that will not start."""

        with win32_environment():
            session_end = importlib.import_module("ambience_rgb.session_end")
            windows = importlib.import_module("ambience_rgb.session_end.windows")
            original = windows.Win32SessionEndHook

            def broken(*_args, **_kwargs):
                raise OSError("no window station")

            windows.Win32SessionEndHook = broken
            try:
                hook = session_end.start_session_end_hook(lambda _r: None, threading.Event(), platform="win32")
            finally:
                windows.Win32SessionEndHook = original
            self.assertEqual(type(hook).__name__, "_NullHook")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
