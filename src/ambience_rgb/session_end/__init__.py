"""Stop the daemon cleanly when the Windows session ends.

On Linux this is SIGTERM's job: ``systemctl --user stop`` and a logout that
stops the unit both send one and wait for it. Windows sends a process no signal
at all. What it does send, to top-level windows, is ``WM_QUERYENDSESSION`` and
then ``WM_ENDSESSION`` -- so on Windows the daemon keeps one hidden window whose
only job is to hear those and ask the daemon to stop.

**This covers LOGOFF, not shutdown.** At an ordinary shutdown or restart the
callback does not fire although the window is alive; at a logoff it does. The
interactive session is torn down without the usual conversation. The layer that covers shutdown is the SYSTEM scheduled task on
System event 1074, which runs ``ambience-rgb off --stop-daemon`` from session 0
-- see ``scripts/windows/install.ps1``.

Two things follow from this daemon having hardware to release rather than a
network call to make:

* the callback is only a flag, :meth:`Daemon.request_stop`, so it runs inline;
* **``WM_ENDSESSION`` waits for the daemon to finish.** Windows may end the
  process the moment that message returns. Returning at once -- which a
  fire-and-forget callback can afford -- would let the session end while the
  black was still being written. So it blocks on ``finished``, bounded, because
  a hung app earns the "this app is preventing sign-out" screen.

It also takes a ``WM_CLOSE`` from outside as a stop request: that is what an
orderly "please exit" looks like to a windowed process, and Task Scheduler's
End may send it before it resorts to ``TerminateProcess``.
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Callable

LOGGER = logging.getLogger("ambience_rgb.session_end")

ENDSESSION_WAIT_SECONDS = 4.0
"""How long the session is held open for the black. Windows' own patience with
an unresponsive app, ``HungAppTimeout``, is five seconds."""


class _NullHook:
    """What every platform but Windows gets -- and Windows, when the window fails."""

    def stop(self) -> None:
        return None


def start_session_end_hook(
    on_session_end: Callable[[str], None],
    finished: threading.Event,
    logger: logging.Logger | None = None,
    *,
    platform: str | None = None,
):
    """Call ``on_session_end(reason)`` when the Windows session ends.

    ``finished`` is the daemon's event, set once the hardware is black and
    released; the window holds the session open on it. Elsewhere, and on any
    failure, this returns a no-op: losing the logoff black is much better than a
    daemon that will not start.
    """

    log = logger or LOGGER
    if (platform or sys.platform) != "win32":
        return _NullHook()
    try:
        from ambience_rgb.session_end.windows import Win32SessionEndHook

        hook = Win32SessionEndHook(on_session_end, finished, log)
        started = hook.start()
    except Exception:  # noqa: BLE001 - never let this stop the daemon starting
        log.warning(
            "Session-end detection is unavailable; logging off will not black the hardware",
            exc_info=True,
        )
        return _NullHook()
    if not started:
        log.warning("Session-end detection is unavailable; logging off will not black the hardware")
        return _NullHook()
    return hook
