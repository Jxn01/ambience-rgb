"""OpenRGB's settings that ambience-rgb depends on, set by both installers.

Two things in ``OpenRGB.json`` decide whether this program works, and OpenRGB
gets neither right on its own:

- **The Commander Core detectors must be off.** Exactly one process may drive a
  HID device, and neither Linux's hidraw nor Windows' HID stack enforces it: a
  second driver opens the device just as successfully, and the two interleave.
  OpenRGB registers the Commander Core even though it cannot size its zones, so
  with its detector on nothing errors and the colours never apply.
- **The red/green swap must be on for the zones the config names.** OpenRGB
  swaps an ASRock Polychrome zone when the board's own table OR its settings say
  so -- the file can force a swap on, never off -- and what the config's
  ``zone_channel_order`` sends comes out right only if that swap is where the
  config assumes it is.

This used to be an inline Python snippet in ``install.sh``. It is a module now
so that ``install.ps1`` runs the very same edit -- rather than a PowerShell copy
that would drift, and would meet ``ConvertTo-Json``'s default depth of 2, which
silently flattens anything nested deeper. ``OpenRGB.json`` is deeper.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ambience_rgb.openrgb_settings import (
    COMMANDER_DETECTORS,
    RG_SWAP_SECTION,
    disable_commander_detectors,
    main,
    update_settings,
)

# What OpenRGB 1.0rc3.1 wrote on its first run on the Windows half, 2026-09-11:
# the board's own table marks PCH, IO Cover and PCB as swapped, and OpenRGB
# defaults every other zone to off.
FIRST_RUN_RG_SWAP = {
    "Addressable Header 1": False,
    "Addressable Header 2": False,
    "Addressable Header 3/Audio": False,
    "IO Cover": True,
    "PCB": True,
    "PCH": True,
    "RGB LED 1 Header": False,
    "RGB LED 2 Header": False,
}
# The zones this rig's config pins.
PINNED = ("IO Cover", "PCB", "Addressable Header 1", "Addressable Header 2", "Addressable Header 3/Audio")
ASROCK_RULE = """
[[devices]]
match = "ASRock X870E Taichi"
openrgb_rg_swap = ["IO Cover", "PCB", "Addressable Header 1", "Addressable Header 2", "Addressable Header 3/Audio"]
"""


def _settings(directory: str, content) -> Path:
    path = Path(directory) / "OpenRGB.json"
    path.write_text(json.dumps(content, indent=4), encoding="utf-8")
    return path


def _config(directory: str, extra: str = "") -> Path:
    """A config for the command line, so that no test reads the host's own."""

    path = Path(directory) / "config.toml"
    path.write_text('[socket]\npath = "/tmp/x.sock"\n' + extra, encoding="utf-8")
    return path


def _swaps(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))[RG_SWAP_SECTION]["RGSwap"]


class DisableTest(unittest.TestCase):
    def test_both_detectors_are_turned_off(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _settings(directory, {"Detectors": {"detectors": {"Corsair Commander Core": True}}})
            changed = disable_commander_detectors(path)
            detectors = json.loads(path.read_text(encoding="utf-8"))["Detectors"]["detectors"]
        self.assertEqual(set(changed), set(COMMANDER_DETECTORS))
        for name in COMMANDER_DETECTORS:
            self.assertIs(detectors[name], False)

    def test_everything_else_survives_untouched(self) -> None:
        original = {
            "Detectors": {"detectors": {"ASRock Polychrome USB": True, "Corsair Commander Core": True}},
            "Server": {"host": "127.0.0.1", "nested": {"deeper": {"deepest": [1, 2, 3]}}},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = _settings(directory, original)
            disable_commander_detectors(path)
            after = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(after["Detectors"]["detectors"]["ASRock Polychrome USB"])
        self.assertEqual(after["Server"], original["Server"])

    def test_already_off_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "OpenRGB.json"
            # Formatted unlike this module's own output, so that a needless
            # rewrite would show in the bytes.
            content = {"Detectors": {"detectors": {name: False for name in COMMANDER_DETECTORS}}}
            path.write_text(json.dumps(content, indent=2), encoding="utf-8")
            before = path.read_bytes()
            self.assertEqual(disable_commander_detectors(path), [])
            self.assertEqual(path.read_bytes(), before, "an idempotent re-run must not rewrite the file")

    def test_a_file_without_a_detectors_section_gets_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _settings(directory, {"Server": {}})
            disable_commander_detectors(path)
            detectors = json.loads(path.read_text(encoding="utf-8"))["Detectors"]["detectors"]
        self.assertEqual(detectors, {name: False for name in COMMANDER_DETECTORS})

    def test_no_file_yet_is_created_with_the_detectors_already_off(self) -> None:
        """🚨 On the Windows half OpenRGB is not even installed yet.

        Waiting for its settings file would leave a trap: install OpenRGB after
        ambience-rgb, run it once, and it detects the Commander Core with its
        detector on -- beside our daemon. Writing the two entries first means
        OpenRGB starts from them, whenever it arrives.
        """

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "OpenRGB" / "OpenRGB.json"
            changed = disable_commander_detectors(path)
            detectors = json.loads(path.read_text(encoding="utf-8"))["Detectors"]["detectors"]
        self.assertEqual(set(changed), set(COMMANDER_DETECTORS))
        self.assertEqual(detectors, {name: False for name in COMMANDER_DETECTORS})

    def test_a_file_that_is_not_an_object_is_refused_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "OpenRGB.json"
            path.write_text("[1, 2, 3]", encoding="utf-8")
            with self.assertRaises(ValueError):
                disable_commander_detectors(path)
            self.assertEqual(path.read_text(encoding="utf-8"), "[1, 2, 3]")


class RgSwapTest(unittest.TestCase):
    """Forcing OpenRGB's red/green swap on, for the zones the config names.

    🚨 OpenRGB swaps a zone when ``board_table_bit || setting``, so the file can
    force a swap on and never off. On is therefore the only state worth
    pinning, and only for zones a device rule lists in ``openrgb_rg_swap``.
    """

    def test_the_listed_zones_are_forced_on_and_nothing_else_moves(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _settings(directory, {RG_SWAP_SECTION: {"RGSwap": dict(FIRST_RUN_RG_SWAP)}})
            changes = update_settings(path, rg_swaps=PINNED)
            swaps = _swaps(path)
        self.assertEqual(
            set(changes.rg_swaps), {"Addressable Header 1", "Addressable Header 2", "Addressable Header 3/Audio"}
        )
        for zone in PINNED:
            self.assertIs(swaps[zone], True, zone)
        for zone in ("PCH", "RGB LED 1 Header", "RGB LED 2 Header"):
            self.assertIs(swaps[zone], FIRST_RUN_RG_SWAP[zone], f"{zone} was not listed; it is OpenRGB's")

    def test_already_on_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "OpenRGB.json"
            content = {
                "Detectors": {"detectors": {name: False for name in COMMANDER_DETECTORS}},
                RG_SWAP_SECTION: {"RGSwap": {**FIRST_RUN_RG_SWAP, **{zone: True for zone in PINNED}}},
            }
            # Formatted unlike this module's own output, as above.
            path.write_text(json.dumps(content, indent=2), encoding="utf-8")
            before = path.read_bytes()
            self.assertFalse(update_settings(path, rg_swaps=PINNED))
            self.assertEqual(path.read_bytes(), before, "an idempotent re-run must not rewrite the file")

    def test_no_zones_listed_writes_no_section(self) -> None:
        """No rule, no pin: another ASRock board's settings are not ours to change."""

        with tempfile.TemporaryDirectory() as directory:
            path = _settings(directory, {"Detectors": {"detectors": {"Corsair Commander Core": True}}})
            update_settings(path)
            after = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn(RG_SWAP_SECTION, after)

    def test_a_missing_section_is_created_holding_only_the_listed_zones(self) -> None:
        """OpenRGB reads each zone's swap only when the zone is present.

        So a section holding just ours is safe, and the zones it does not name
        keep OpenRGB's own handling. With no section at all OpenRGB writes its
        defaults on the first run -- the addressable headers off.
        """

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "OpenRGB" / "OpenRGB.json"
            update_settings(path, rg_swaps=PINNED)
            swaps = _swaps(path)
        self.assertEqual(swaps, {zone: True for zone in PINNED})

    def test_a_section_it_does_not_understand_is_refused_not_overwritten(self) -> None:
        for section in ([], {"RGSwap": "on"}, {"RGSwap": [1]}):
            with self.subTest(section=section), tempfile.TemporaryDirectory() as directory:
                path = _settings(directory, {RG_SWAP_SECTION: section})
                before = path.read_bytes()
                with self.assertRaises(ValueError):
                    update_settings(path, rg_swaps=PINNED)
                self.assertEqual(path.read_bytes(), before)

    def test_both_changes_share_one_backup_of_the_original(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _settings(
                directory,
                {
                    "Detectors": {"detectors": {"Corsair Commander Core": True}},
                    RG_SWAP_SECTION: {"RGSwap": dict(FIRST_RUN_RG_SWAP)},
                },
            )
            before = path.read_bytes()
            backup = Path(directory) / "OpenRGB.json.bak"
            changes = update_settings(path, rg_swaps=PINNED, backup_to=backup)
            self.assertEqual(backup.read_bytes(), before)
        self.assertEqual(set(changes.detectors), set(COMMANDER_DETECTORS))
        self.assertEqual(len(changes.rg_swaps), 3)


class CommandLineTest(unittest.TestCase):
    def test_a_change_is_backed_up_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original = {"Detectors": {"detectors": {"Corsair Commander Core": True}}}
            path = _settings(directory, original)
            before = path.read_bytes()
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main([str(path), "--config", str(_config(directory))]), 0)
            backups = list(Path(directory).glob("OpenRGB.json.bak-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), before)
            self.assertIn(str(backups[0]), out.getvalue())

    def test_no_change_means_no_backup(self) -> None:
        # Re-running the installer must not leave a pile of identical copies.
        with tempfile.TemporaryDirectory() as directory:
            path = _settings(directory, {"Detectors": {"detectors": {name: False for name in COMMANDER_DETECTORS}}})
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main([str(path), "--config", str(_config(directory))]), 0)
            self.assertEqual(list(Path(directory).glob("*.bak-*")), [])
            self.assertIn("already", out.getvalue())

    def test_an_unreadable_file_is_a_clear_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "OpenRGB.json"
            path.write_text("{not json", encoding="utf-8")
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main([str(path), "--config", str(_config(directory))]), 1)
            self.assertIn(str(path), out.getvalue())

    def test_the_zones_the_config_names_are_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _settings(directory, {RG_SWAP_SECTION: {"RGSwap": dict(FIRST_RUN_RG_SWAP)}})
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main([str(path), "--config", str(_config(directory, ASROCK_RULE))]), 0)
            swaps = _swaps(path)
        for zone in PINNED:
            self.assertIs(swaps[zone], True, zone)
        self.assertIn("Addressable Header 1", out.getvalue())
        self.assertIn("restart", out.getvalue().lower(), "OpenRGB reads the file only when it starts")

    def test_without_config_it_reads_the_one_the_daemon_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config(directory, ASROCK_RULE)
            path = _settings(directory, {})
            with mock.patch.dict(os.environ, {"AMBIENCE_RGB_CONFIG": str(config)}):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main([str(path)]), 0)
            swaps = _swaps(path)
        self.assertEqual(swaps, {zone: True for zone in PINNED})

    def test_a_broken_config_still_disables_the_detectors(self) -> None:
        """🚨 The detector pin must not hang on the config parsing.

        Without it OpenRGB can take the AIO from the daemon, silently, and a
        typo in an unrelated key would be enough. So the detectors are pinned
        regardless; the swaps are reported as not pinned, and the exit status
        says something went wrong, which both installers turn into a warning.
        """

        with tempfile.TemporaryDirectory() as directory:
            path = _settings(directory, {"Detectors": {"detectors": {"Corsair Commander Core": True}}})
            broken = Path(directory) / "config.toml"
            broken.write_text("[socket\n", encoding="utf-8")
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main([str(path), "--config", str(broken)]), 1)
            detectors = json.loads(path.read_text(encoding="utf-8"))["Detectors"]["detectors"]
        self.assertEqual(
            {name: detectors[name] for name in COMMANDER_DETECTORS}, {name: False for name in COMMANDER_DETECTORS}
        )
        self.assertIn(str(broken), out.getvalue())

    def test_a_malformed_command_line_is_refused(self) -> None:
        for argv in ([], ["a.json", "b.json"], ["a.json", "--config"]):
            with self.subTest(argv=argv):
                with redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(main(argv), 2)
                self.assertIn("usage", err.getvalue())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
