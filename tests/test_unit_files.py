"""The systemd units must say what the design requires.

Checked as raw text rather than parsed: configparser keeps only the last of a
repeated key while systemd accumulates them, so a parsed view can hide a
duplicate or a missing directive that systemd would honour.
"""

from __future__ import annotations

import unittest
from pathlib import Path

UNITS = Path(__file__).resolve().parents[1] / "systemd"


def read(name: str) -> str:
    return (UNITS / name).read_text(encoding="utf-8")


class OpenRgbServerUnitTest(unittest.TestCase):
    def test_it_binds_only_the_loopback(self) -> None:
        """OpenRGB's own default is 0.0.0.0 -- lighting control on the LAN."""

        text = read("ambience-rgb-openrgb.service")
        self.assertIn("--server-host 127.0.0.1", text)
        # Only directives matter; the comment above it names 0.0.0.0 on purpose.
        directives = [
            line for line in text.splitlines() if line and not line.lstrip().startswith("#")
        ]
        self.assertNotIn("0.0.0.0", "\n".join(directives))

    def test_it_dies_with_the_desktop_session(self) -> None:
        text = read("ambience-rgb-openrgb.service")
        self.assertIn("PartOf=graphical-session.target", text)


class DaemonUnitTest(unittest.TestCase):
    def test_it_only_wants_the_openrgb_server(self) -> None:
        """Requires= would take the AIO down with the server, for no reason."""

        text = read("ambience-rgb.service")
        self.assertIn("Wants=ambience-rgb-openrgb.service", text)
        self.assertNotIn("Requires=ambience-rgb-openrgb.service", text)

    def test_it_is_given_time_to_write_black_on_the_way_out(self) -> None:
        text = read("ambience-rgb.service")
        self.assertIn("KillSignal=SIGTERM", text)
        self.assertIn("TimeoutStopSec=", text)

    def test_it_stops_with_the_session_so_logout_blacks_the_hardware(self) -> None:
        self.assertIn("PartOf=graphical-session.target", read("ambience-rgb.service"))


class ShutdownUnitTest(unittest.TestCase):
    def test_the_work_is_in_execstop(self) -> None:
        """It must do nothing at boot; shutdown is the whole point."""

        text = read("ambience-rgb-lights-off.service")
        self.assertIn("ExecStart=/bin/true", text)
        self.assertIn("ExecStop=", text)
        self.assertIn("-m ambience_rgb off", text)
        self.assertIn("RemainAfterExit=yes", text)
        self.assertIn("Type=oneshot", text)

    def test_it_is_ordered_before_shutdown(self) -> None:
        text = read("ambience-rgb-lights-off.service")
        self.assertIn("Before=shutdown.target", text)
        self.assertIn("Conflicts=shutdown.target", text)
        self.assertIn("DefaultDependencies=no", text)

    def test_it_carries_a_config_that_works_without_a_session(self) -> None:
        """Root at shutdown has no XDG_CONFIG_HOME to find a config in."""

        self.assertIn("AMBIENCE_RGB_CONFIG=", read("ambience-rgb-lights-off.service"))

    def test_it_waits_for_the_install_directory_to_be_mounted(self) -> None:
        self.assertIn("RequiresMountsFor=", read("ambience-rgb-lights-off.service"))

    def test_it_reads_the_config_the_daemon_reads(self) -> None:
        """🚨 It used to read the repo's example config, whatever the daemon used.

        Root at shutdown has neither this user's home nor their environment, so
        it cannot search for their config. With a config of their own, the
        shutdown pass would black the example's set of devices instead of theirs.
        The installer resolves the daemon's file and hands it over -- the same
        fix the Windows shutdown task needed, for the same reason.
        """

        text = read("ambience-rgb-lights-off.service")
        self.assertIn("AMBIENCE_RGB_CONFIG=@CONFIG_PATH@", text)
        directives = [
            line for line in text.splitlines() if line and not line.lstrip().startswith("#")
        ]
        self.assertNotIn("config.example.toml", "\n".join(directives))

    def test_it_waits_for_the_config_to_be_mounted_too(self) -> None:
        self.assertIn("@CONFIG_DIR@", read("ambience-rgb-lights-off.service"))


class InstallerTest(unittest.TestCase):
    def test_it_fills_in_every_placeholder_the_units_use(self) -> None:
        """A placeholder the installer does not substitute reaches systemd verbatim."""

        import re

        installer = (UNITS.parent / "scripts" / "install.sh").read_text(encoding="utf-8")
        used: set[str] = set()
        for path in UNITS.glob("*.service"):
            used |= set(re.findall(r"@[A-Z_]+@", path.read_text(encoding="utf-8")))
        for placeholder in sorted(used):
            with self.subTest(placeholder=placeholder):
                self.assertIn(f"s|{placeholder}|", installer)


class PlaceholderTest(unittest.TestCase):
    def test_every_unit_uses_the_install_dir_placeholder(self) -> None:
        """The installer substitutes it; a hard-coded path would not travel."""

        for name in ("ambience-rgb.service", "ambience-rgb-lights-off.service"):
            with self.subTest(unit=name):
                self.assertIn("@INSTALL_DIR@", read(name))

    def test_no_unit_hard_codes_a_home_directory(self) -> None:
        for path in UNITS.glob("*.service"):
            with self.subTest(unit=path.name):
                self.assertNotIn("/home/", path.read_text(encoding="utf-8"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
