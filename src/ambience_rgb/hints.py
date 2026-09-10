"""Commands to print for a person, in the words of the platform they are on.

Every message that tells someone to run something comes from here. Before the
Windows port three files each carried their own ``systemctl`` line, and on
Windows each would have been advice to run a program that does not exist. A
guard in ``tests/test_hints.py`` keeps platform commands in this one module.

Windows has Task Scheduler where Linux has systemd. The task names match the
unit names on purpose -- ``ambience-rgb``, ``ambience-rgb-openrgb``,
``ambience-rgb-lights-off`` -- so one vocabulary serves both halves of the same
machine, and the installers' tests hold them to these constants.
"""

from __future__ import annotations

import sys

DAEMON_TASK = "ambience-rgb"
OPENRGB_TASK = "ambience-rgb-openrgb"
SHUTDOWN_TASK = "ambience-rgb-lights-off"


def _windows(platform: str | None) -> bool:
    return (platform or sys.platform) == "win32"


def stop_daemon(platform: str | None = None) -> str:
    """How to stop the daemon so that it blacks the hardware on its way out."""

    if _windows(platform):
        # The stop command rather than Stop-ScheduledTask: with it the daemon
        # blacks and releases everything itself. Whether ending the task is as
        # orderly is unverified -- it may simply terminate the process.
        return r".venv\Scripts\python -m ambience_rgb off --stop-daemon   (in the ambience-rgb folder)"
    return "systemctl --user stop ambience-rgb"


def start_daemon(platform: str | None = None) -> str:
    if _windows(platform):
        return f"Start-ScheduledTask -TaskName {DAEMON_TASK}"
    return "systemctl --user start ambience-rgb"


def restart_openrgb(platform: str | None = None) -> str:
    if _windows(platform):
        # Not in one breath: the task's policy is IgnoreNew, so a start issued
        # while the stop is still in flight is dropped and the server stays down.
        return (
            f"Stop-ScheduledTask -TaskName {OPENRGB_TASK}, wait until it shows Ready, "
            f"then Start-ScheduledTask -TaskName {OPENRGB_TASK}"
        )
    return "systemctl --user restart ambience-rgb-openrgb"
