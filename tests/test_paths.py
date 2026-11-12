"""Where this program keeps its config and its log, on each platform.

**Nothing in ``paths`` may raise.** The log path is decided before logging
exists, and the shutdown task on Windows runs as SYSTEM with an environment
nothing like a login shell's. ``Path.home()`` cannot fail on Linux -- it falls
back to the password database -- but on Windows it is purely environmental and
raises ``RuntimeError`` when ``USERPROFILE`` is missing. That asymmetry is a latent
startup crash, so every branch here is exercised,
including the degenerate one.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ambience_rgb import paths


class HomeTest(unittest.TestCase):
    def test_a_home_lookup_that_raises_reads_as_unknown(self) -> None:
        with mock.patch.object(Path, "home", side_effect=RuntimeError("Could not determine home directory.")):
            self.assertIsNone(paths._home_or_none())

    def test_expanding_a_tilde_without_a_home_leaves_it_alone(self) -> None:
        # A `~` in AMBIENCE_RGB_CONFIG, read by a SYSTEM task with no USERPROFILE.
        with mock.patch.object(Path, "expanduser", side_effect=RuntimeError("Could not determine home directory.")):
            self.assertEqual(paths.expand_user(Path("~/x.toml")), Path("~/x.toml"))


class StateDirTest(unittest.TestCase):
    def test_windows_uses_localappdata(self) -> None:
        state = paths.state_dir(platform="win32", environ={"LOCALAPPDATA": "/L"})
        self.assertEqual(state, Path("/L") / "ambience-rgb")

    def test_windows_without_localappdata_derives_it_from_home(self) -> None:
        with mock.patch.object(paths, "_home_or_none", return_value=Path("/h")):
            state = paths.state_dir(platform="win32", environ={})
        self.assertEqual(state, Path("/h/AppData/Local/ambience-rgb"))

    def test_linux_uses_xdg_state_home(self) -> None:
        state = paths.state_dir(platform="linux", environ={"XDG_STATE_HOME": "/S"})
        self.assertEqual(state, Path("/S") / "ambience-rgb")

    def test_linux_defaults_under_home(self) -> None:
        with mock.patch.object(paths, "_home_or_none", return_value=Path("/h")):
            state = paths.state_dir(platform="linux", environ={})
        self.assertEqual(state, Path("/h/.local/state/ambience-rgb"))

    def test_no_home_at_all_falls_back_to_temp(self) -> None:
        with mock.patch.object(paths, "_home_or_none", return_value=None):
            for platform in ("win32", "linux"):
                with self.subTest(platform=platform):
                    state = paths.state_dir(platform=platform, environ={})
                    self.assertEqual(state, Path(tempfile.gettempdir()) / "ambience-rgb")

    def test_the_log_lives_in_it(self) -> None:
        log = paths.default_log_path(platform="linux", environ={"XDG_STATE_HOME": "/S"})
        self.assertEqual(log, Path("/S/ambience-rgb/ambience-rgb.log"))


class ConfigDirTest(unittest.TestCase):
    def test_windows_uses_appdata(self) -> None:
        directory = paths.config_dir(platform="win32", environ={"APPDATA": "/R"})
        self.assertEqual(directory, Path("/R") / "ambience-rgb")

    def test_linux_uses_xdg_config_home(self) -> None:
        directory = paths.config_dir(platform="linux", environ={"XDG_CONFIG_HOME": "/C"})
        self.assertEqual(directory, Path("/C") / "ambience-rgb")

    def test_linux_defaults_under_home(self) -> None:
        with mock.patch.object(paths, "_home_or_none", return_value=Path("/h")):
            directory = paths.config_dir(platform="linux", environ={})
        self.assertEqual(directory, Path("/h/.config/ambience-rgb"))

    def test_windows_without_appdata_does_not_guess_a_linux_path(self) -> None:
        with mock.patch.object(paths, "_home_or_none", return_value=Path("/h")):
            directory = paths.config_dir(platform="win32", environ={})
        self.assertEqual(directory, Path("/h/AppData/Roaming/ambience-rgb"))

    def test_no_home_at_all_is_none_not_an_exception(self) -> None:
        with mock.patch.object(paths, "_home_or_none", return_value=None):
            for platform in ("win32", "linux"):
                with self.subTest(platform=platform):
                    self.assertIsNone(paths.config_dir(platform=platform, environ={}))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
