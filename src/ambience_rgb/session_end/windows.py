"""The hidden window that hears the Windows session end.

See :mod:`ambience_rgb.session_end` for what this covers and -- more
importantly -- what it does not. Adapted from matterlights'
``shutdown_hook/windows.py``: the window and the message loop are the same, the
reactions differ.

It is a normal top-level window that is never shown, not a message-only window:
message-only windows are not enumerated as top-level windows and so never
receive the session-end messages. ``WS_EX_TOOLWINDOW`` keeps it out of the
taskbar and Alt+Tab.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Callable

from ambience_rgb.session_end import ENDSESSION_WAIT_SECONDS

_WM_DESTROY = 0x0002
_WM_CLOSE = 0x0010
_WM_QUERYENDSESSION = 0x0011
_WM_ENDSESSION = 0x0016
_WS_EX_TOOLWINDOW = 0x00000080

_CTRL_CLOSE_EVENT = 2
_CTRL_LOGOFF_EVENT = 5
_CTRL_SHUTDOWN_EVENT = 6
_SESSION_END_CTRL_EVENTS = frozenset({_CTRL_CLOSE_EVENT, _CTRL_LOGOFF_EVENT, _CTRL_SHUTDOWN_EVENT})

_LRESULT = ctypes.c_ssize_t
_LPARAM = ctypes.c_ssize_t
_WPARAM = ctypes.c_size_t
_WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wintypes.HWND, wintypes.UINT, _WPARAM, _LPARAM)
_HANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)


class _WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class Win32SessionEndHook:
    def __init__(
        self,
        on_session_end: Callable[[str], None],
        finished: threading.Event,
        logger: logging.Logger,
        *,
        wait_seconds: float = ENDSESSION_WAIT_SECONDS,
    ) -> None:
        self._on_session_end = on_session_end
        self._finished = finished
        self._logger = logger
        self._wait_seconds = wait_seconds
        self._lock = threading.Lock()
        self._fired = False
        self._closing = False
        self._ready = threading.Event()
        self._failed = False
        self._hwnd: int | None = None
        self._class_atom: int | None = None
        self._hinstance: int | None = None
        self._class_name = f"AmbienceRgbSessionEnd_{id(self)}"
        # Strong references, so the ctypes trampolines are not collected while
        # Windows still holds pointers to them.
        self._wndproc = _WNDPROC(self._window_proc)
        self._ctrl_handler = _HANDLER_ROUTINE(self._console_ctrl_handler)
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._thread = threading.Thread(target=self._run, name="session-end", daemon=True)
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        user32 = self._user32
        kernel32 = self._kernel32
        user32.DefWindowProcW.restype = _LRESULT
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, _WPARAM, _LPARAM]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASS)]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        user32.GetMessageW.restype = ctypes.c_int
        user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        ]
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.restype = _LRESULT
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.UnregisterClassW.restype = wintypes.BOOL
        user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, _WPARAM, _LPARAM]
        user32.PostQuitMessage.argtypes = [ctypes.c_int]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL
        kernel32.SetConsoleCtrlHandler.argtypes = [_HANDLER_ROUTINE, wintypes.BOOL]

    # -- life cycle ----------------------------------------------------------

    def start(self) -> bool:
        self._thread.start()
        self._ready.wait(timeout=2.0)
        if self._failed:
            return False
        # A second, independent path. Harmless under pythonw, which has no console.
        try:
            self._kernel32.SetConsoleCtrlHandler(self._ctrl_handler, True)
        except OSError:
            self._logger.debug("Console control handler unavailable", exc_info=True)
        return True

    def stop(self) -> None:
        """Close the window without it reading as a request to stop."""

        self._closing = True
        hwnd = self._hwnd
        if hwnd:
            try:
                self._user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
            except OSError:
                pass
        try:
            self._kernel32.SetConsoleCtrlHandler(self._ctrl_handler, False)
        except OSError:
            pass

    # -- reacting ------------------------------------------------------------

    def fire(self, reason: str) -> None:
        """Ask the daemon to stop, at most once, whichever path noticed first."""

        with self._lock:
            if self._fired:
                return
            self._fired = True
        self._logger.info("Session ending (%s); turning RGB off", reason)
        try:
            self._on_session_end(reason)
        except Exception:  # noqa: BLE001 - the window must keep answering
            self._logger.exception("Could not ask the daemon to stop")

    def _wait_for_black(self) -> None:
        if not self._finished.wait(self._wait_seconds):
            self._logger.warning(
                "The session is ending before the hardware finished going dark (waited %.1f s)",
                self._wait_seconds,
            )

    def _window_proc(self, hwnd, message, wparam, lparam):
        if message == _WM_QUERYENDSESSION:
            # Act now: at a shutdown WM_ENDSESSION never arrives at all. And
            # always answer 1 -- 0 would veto the session end.
            self.fire("WM_QUERYENDSESSION")
            return 1
        if message == _WM_ENDSESSION:
            if wparam:
                self.fire("WM_ENDSESSION")
                self._wait_for_black()
            elif self._fired:
                # The logoff was cancelled, after the daemon had been asked to
                # stop. Nothing here can undo that; say how to get it back.
                from ambience_rgb import hints

                self._logger.warning(
                    "The session end was cancelled after the daemon began stopping; "
                    "start it again with: %s",
                    hints.start_daemon("win32"),
                )
            return 0
        if message == _WM_CLOSE:
            if not self._closing:
                self.fire("WM_CLOSE")
            self._user32.DestroyWindow(hwnd)
            return 0
        if message == _WM_DESTROY:
            self._hwnd = None
            self._user32.PostQuitMessage(0)
            return 0
        return self._user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _console_ctrl_handler(self, event: int) -> bool:
        if event in _SESSION_END_CTRL_EVENTS:
            self.fire(f"console event {event}")
            # Returning lets Windows end the process, so not before the black.
            self._wait_for_black()
            return True
        # Ctrl+C and Ctrl+Break go on to Python's own handler, which raises the
        # signal in the main thread -- the daemon's ordinary signal path.
        return False

    # -- the window ----------------------------------------------------------

    def _run(self) -> None:
        try:
            self._create_window()
        except OSError:
            self._failed = True
            self._logger.exception("Failed to start session-end detection")
            self._teardown()
            self._ready.set()
            return
        self._ready.set()
        self._pump_messages()

    def _create_window(self) -> None:
        self._hinstance = self._kernel32.GetModuleHandleW(None)
        window_class = _WNDCLASS()
        window_class.lpfnWndProc = self._wndproc
        window_class.hInstance = self._hinstance
        window_class.lpszClassName = self._class_name
        atom = self._user32.RegisterClassW(ctypes.byref(window_class))
        if not atom:
            raise ctypes.WinError(ctypes.get_last_error())
        self._class_atom = atom
        # Top-level (no parent) so it receives the session-end messages, never
        # shown, and a tool window so it stays out of the taskbar.
        hwnd = self._user32.CreateWindowExW(
            _WS_EX_TOOLWINDOW,
            self._class_name,
            "ambience-rgb session end",
            0,
            0,
            0,
            0,
            0,
            None,
            None,
            self._hinstance,
            None,
        )
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self._hwnd = hwnd

    def _pump_messages(self) -> None:
        msg = wintypes.MSG()
        try:
            while True:
                result = self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result in (0, -1):
                    break
                self._user32.TranslateMessage(ctypes.byref(msg))
                self._user32.DispatchMessageW(ctypes.byref(msg))
        except OSError:
            self._logger.exception("Session-end message loop stopped")
        finally:
            self._teardown()

    def _teardown(self) -> None:
        if self._hwnd:
            try:
                self._user32.DestroyWindow(self._hwnd)
            except OSError:
                pass
            self._hwnd = None
        if self._class_atom:
            try:
                self._user32.UnregisterClassW(self._class_name, self._hinstance)
            except OSError:
                pass
            self._class_atom = None
