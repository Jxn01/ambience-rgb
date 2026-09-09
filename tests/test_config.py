"""Configuration parsing and the device mapping it produces.

The mapping is the whole personality of this program -- which controller is
near, which is far, what is left alone. A silent misparse here does not crash,
it just lights the wrong things, so every rule and every rejection is asserted.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from linux_rgb.config import (
    GROUP_EXCLUDED,
    GROUP_FAR,
    GROUP_NEAR,
    ConfigError,
    load_config,
    parse_config,
)

MINIMAL = {"socket": {"path": "/tmp/x.sock"}}


class ParsingTest(unittest.TestCase):
    def test_socket_path_is_required(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            parse_config({})
        self.assertIn("socket.path", str(caught.exception))
        self.assertIn("RGB_PUBLISH_SOCKET", str(caught.exception), "name the other end")

    def test_rates_become_intervals(self) -> None:
        config = parse_config({**MINIMAL, "rates": {"usb_hz": 5, "smbus_hz": 1}})
        self.assertAlmostEqual(config.usb_interval_seconds, 0.2)
        self.assertAlmostEqual(config.smbus_interval_seconds, 1.0)

    def test_a_zero_rate_is_rejected_rather_than_dividing_by_zero(self) -> None:
        with self.assertRaises(ConfigError):
            parse_config({**MINIMAL, "rates": {"usb_hz": 0}})

    def test_a_nonsense_rate_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            parse_config({**MINIMAL, "rates": {"usb_hz": "fast"}})

    def test_an_unknown_group_names_the_valid_ones(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            parse_config({**MINIMAL, "devices": [{"match": "x", "group": "middle"}]})
        message = str(caught.exception)
        self.assertIn("near", message)
        self.assertIn("far", message)

    def test_an_unknown_bus_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            parse_config({**MINIMAL, "devices": [{"match": "x", "bus": "pcie"}]})

    def test_a_device_without_match_is_rejected(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            parse_config({**MINIMAL, "devices": [{"group": "near"}]})
        self.assertIn("match", str(caught.exception))

    def test_an_unknown_group_inside_zones_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            parse_config(
                {**MINIMAL, "devices": [{"match": "x", "zones": {"A": "sideways"}}]}
            )

    def test_xdg_runtime_dir_falls_back_when_unset(self) -> None:
        """The shutdown unit runs as root with no XDG_RUNTIME_DIR."""

        saved = os.environ.pop("XDG_RUNTIME_DIR", None)
        try:
            config = parse_config({"socket": {"path": "$XDG_RUNTIME_DIR/x.sock"}})
        finally:
            if saved is not None:
                os.environ["XDG_RUNTIME_DIR"] = saved
        self.assertNotIn("$", str(config.socket_path), "no literal variable survives")
        self.assertTrue(config.socket_path.is_absolute())


class MatchingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = parse_config(
            {
                **MINIMAL,
                "devices": [
                    {"match": "AW3225QF", "group": "near", "bus": "usb"},
                    {"match": "Dominator Titanium", "group": "near", "bus": "smbus"},
                    {
                        "match": "ASRock X870E",
                        "group": "far",
                        "bus": "usb",
                        "zones": {"IO Cover": "near", "PCB": "near"},
                    },
                    {"match": "Keychron", "group": "excluded"},
                ],
            }
        )

    def test_matching_is_a_case_insensitive_substring(self) -> None:
        """OpenRGB renames devices between versions; exact names silently rot."""

        rule = self.config.rule_for("Corsair Dominator Titanium RGB DDR5")
        self.assertIsNotNone(rule)
        self.assertEqual(rule.group, GROUP_NEAR)
        self.assertEqual(self.config.rule_for("alienware aw3225qf").group, GROUP_NEAR)

    def test_an_unclaimed_controller_has_no_rule(self) -> None:
        self.assertIsNone(self.config.rule_for("Some Future Fan Hub"))

    def test_exclusion_is_explicit(self) -> None:
        self.assertEqual(self.config.rule_for("Keychron K0 Max").group, GROUP_EXCLUDED)

    def test_zones_override_the_device_group(self) -> None:
        rule = self.config.rule_for("ASRock X870E Taichi")
        self.assertEqual(rule.group_for_zone("IO Cover"), GROUP_NEAR)
        self.assertEqual(rule.group_for_zone("PCB"), GROUP_NEAR)
        self.assertEqual(rule.group_for_zone("Addressable Header 1"), GROUP_FAR)
        self.assertEqual(rule.group_for_zone("anything else"), GROUP_FAR)

    def test_zone_names_match_case_insensitively(self) -> None:
        rule = self.config.rule_for("ASRock X870E Taichi")
        self.assertEqual(rule.group_for_zone("io cover"), GROUP_NEAR)

    def test_the_first_matching_rule_wins(self) -> None:
        config = parse_config(
            {
                **MINIMAL,
                "devices": [
                    {"match": "Corsair", "group": "far"},
                    {"match": "Corsair Dominator", "group": "near"},
                ],
            }
        )
        self.assertEqual(config.rule_for("Corsair Dominator Titanium").group, GROUP_FAR)


class ShippedConfigTest(unittest.TestCase):
    """The example config is the fallback, so it must actually be valid."""

    def test_it_parses_and_describes_this_rig(self) -> None:
        path = Path(__file__).resolve().parents[1] / "config.example.toml"
        config = load_config(path)

        self.assertEqual(config.openrgb_host, "127.0.0.1", "never bind the LAN")
        self.assertTrue(config.commander.enabled)

        expected = {
            "Alienware AW3225QF": GROUP_NEAR,
            "ASUS TUF GeForce RTX 5090 Gaming OC": GROUP_NEAR,
            "Corsair Dominator Titanium RGB DDR5": GROUP_NEAR,
            "ASRock X870E Taichi": GROUP_FAR,
            "Razer Naga Pro V2 (Wireless)": GROUP_FAR,
            "Keychron K0 Max": GROUP_EXCLUDED,
        }
        for name, group in expected.items():
            with self.subTest(device=name):
                rule = config.rule_for(name)
                self.assertIsNotNone(rule, f"{name} has no rule")
                self.assertEqual(rule.group, group)

    def test_smbus_is_driven_slower_than_usb(self) -> None:
        """SMBus is shared with the sensors; hammering it is the real risk."""

        path = Path(__file__).resolve().parents[1] / "config.example.toml"
        config = load_config(path)
        self.assertGreater(config.smbus_interval_seconds, config.usb_interval_seconds)
        for name in ("ASUS TUF GeForce RTX 5090 Gaming OC", "Corsair Dominator Titanium RGB DDR5"):
            with self.subTest(device=name):
                self.assertEqual(config.rule_for(name).bus, "smbus")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
