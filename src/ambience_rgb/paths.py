"""Where ambience-rgb keeps its config and its log, on each platform.

Modelled on matterlights' ``paths`` module, and bound by the same rule.

**NOTHING HERE MAY RAISE.** The log path is decided before logging exists,
and on Windows the shutdown task runs as SYSTEM, in an environment nothing like
a login shell's. ``Path.home()`` is the trap: on POSIX it falls back to the
password database and effectively cannot fail, while on Windows it is purely
environmental -- ``USERPROFILE``, or ``HOMEDRIVE`` plus ``HOMEPATH`` -- and
raises ``RuntimeError`` when none of them is set. A lookup that is provably safe
from Linux is a live exception path over there. So every function degrades to a worse
answer instead: a log in the temp directory beats a daemon that will not start.

``platform`` and ``environ`` are parameters so that every branch -- Windows ones
included -- runs in the test suite on either system.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

APP_DIR_NAME = "ambience-rgb"
LOG_FILE_NAME = "ambience-rgb.log"


def _home_or_none() -> Path | None:
    """``Path.home()`` that reports failure instead of raising."""

    try:
        return Path.home()
    except (RuntimeError, OSError, KeyError):
        return None


def expand_user(path: Path) -> Path:
    """``Path.expanduser()`` that leaves the path alone when home is unknown.

    A literal ``~`` directory is visible and recoverable; a process that will
    not start is neither.
    """

    try:
        return path.expanduser()
    except (RuntimeError, OSError, KeyError):
        return path


def config_dir(*, platform: str | None = None, environ: Mapping[str, str] | None = None) -> Path | None:
    r"""The user's config directory for this program, or None when unknowable.

    Windows: ``%APPDATA%\ambience-rgb`` -- the roaming profile, where per-user
    settings belong. Elsewhere: ``$XDG_CONFIG_HOME/ambience-rgb``, defaulting to
    ``~/.config/ambience-rgb``.
    """

    platform = platform or sys.platform
    environ = os.environ if environ is None else environ
    home = None
    if platform == "win32":
        if environ.get("APPDATA"):
            return Path(environ["APPDATA"]) / APP_DIR_NAME
        home = _home_or_none()
        return None if home is None else home / "AppData" / "Roaming" / APP_DIR_NAME
    if environ.get("XDG_CONFIG_HOME"):
        return Path(environ["XDG_CONFIG_HOME"]) / APP_DIR_NAME
    home = _home_or_none()
    return None if home is None else home / ".config" / APP_DIR_NAME


def state_dir(*, platform: str | None = None, environ: Mapping[str, str] | None = None) -> Path:
    r"""The directory for state this program writes itself: the log.

    Windows: ``%LOCALAPPDATA%\ambience-rgb`` -- local, not roaming: a log has no
    business following the user between machines. Elsewhere:
    ``$XDG_STATE_HOME/ambience-rgb``, defaulting to
    ``~/.local/state/ambience-rgb``. The temp directory only when there is no
    home at all.
    """

    platform = platform or sys.platform
    environ = os.environ if environ is None else environ
    if platform == "win32":
        if environ.get("LOCALAPPDATA"):
            return Path(environ["LOCALAPPDATA"]) / APP_DIR_NAME
        home = _home_or_none()
        if home is not None:
            return home / "AppData" / "Local" / APP_DIR_NAME
    else:
        if environ.get("XDG_STATE_HOME"):
            return Path(environ["XDG_STATE_HOME"]) / APP_DIR_NAME
        home = _home_or_none()
        if home is not None:
            return home / ".local" / "state" / APP_DIR_NAME
    try:
        return Path(tempfile.gettempdir()) / APP_DIR_NAME
    except OSError:
        # No usable temp directory either. Relative to wherever we are is the
        # last answer that cannot fail.
        return Path(APP_DIR_NAME)


def default_log_path(*, platform: str | None = None, environ: Mapping[str, str] | None = None) -> Path:
    return state_dir(platform=platform, environ=environ) / LOG_FILE_NAME
