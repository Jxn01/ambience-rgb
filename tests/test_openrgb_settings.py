"""Turning off OpenRGB's Commander Core detectors, for both installers.

Exactly one process may drive a HID device, and neither Linux's hidraw nor
Windows' HID stack enforces it: a second driver opens the device just as
successfully, and the two interleave. OpenRGB registers the Commander Core even
though it cannot size its zones, so with its detector on nothing errors and the
colours never apply.

This used to be an inline Python snippet in ``install.sh``. It is a module now
so that ``install.ps1`` runs the very same edit -- rather than a PowerShell copy
that would drift, and would meet ``ConvertTo-Json``'s default depth of 2, which
silently flattens anything nested deeper. ``OpenRGB.json`` is deeper.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from ambience_rgb.openrgb_settings import COMMANDER_DETECTORS, disable_commander_detectors, main


def _settings(directory: str, content) -> Path:
    path = Path(directory) / "OpenRGB.json"
    path.write_text(json.dumps(content, indent=4), encoding="utf-8")
    return path


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


class CommandLineTest(unittest.TestCase):
    def test_a_change_is_backed_up_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original = {"Detectors": {"detectors": {"Corsair Commander Core": True}}}
            path = _settings(directory, original)
            before = path.read_bytes()
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main([str(path)]), 0)
            backups = list(Path(directory).glob("OpenRGB.json.bak-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), before)
            self.assertIn(str(backups[0]), out.getvalue())

    def test_no_change_means_no_backup(self) -> None:
        # Re-running the installer must not leave a pile of identical copies.
        with tempfile.TemporaryDirectory() as directory:
            path = _settings(directory, {"Detectors": {"detectors": {name: False for name in COMMANDER_DETECTORS}}})
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main([str(path)]), 0)
            self.assertEqual(list(Path(directory).glob("*.bak-*")), [])
            self.assertIn("already", out.getvalue())

    def test_an_unreadable_file_is_a_clear_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "OpenRGB.json"
            path.write_text("{not json", encoding="utf-8")
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main([str(path)]), 1)
            self.assertIn(str(path), out.getvalue())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
