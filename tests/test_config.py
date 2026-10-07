"""Configuration parsing and the device mapping it produces.

The mapping is the whole personality of this program -- which controller is
near, which is far, what is left alone. A silent misparse here does not crash,
it just lights the wrong things, so every rule and every rejection is asserted.

Every test that depends on the platform says which one it means. The suite runs
on Windows as well, and a test that silently assumed Linux would either fail
there for no reason or -- worse -- pass there while checking nothing.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ambience_rgb.config import (
    GROUP_EXCLUDED,
    GROUP_FAR,
    GROUP_NEAR,
    ConfigError,
    config_search_path,
    load_config,
    parse_config,
    resolved_config_path,
)
from ambience_rgb.transport import DEFAULT_UDP_PORT, LOOPBACK, UdpEndpoint, UnixEndpoint

MINIMAL = {"socket": {"path": "/tmp/x.sock"}}
SHIPPED = Path(__file__).resolve().parents[1] / "config.example.toml"


def _without(*names: str) -> dict:
    return {key: value for key, value in os.environ.items() if key not in names}


class ParsingTest(unittest.TestCase):
    def test_socket_path_is_required_for_the_unix_socket(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            parse_config({}, platform="linux")
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

    def test_every_rejection_is_a_config_error(self) -> None:
        # The CLI turns a ConfigError into a clean one-line message and exit 2.
        # A bare ValueError from a helper escapes that and prints a traceback,
        # which is how a typo in saturation used to present.
        for section in (
            {"colour": {"saturation": 0}},
            {"colour": {"group_cohesion": 2}},
            {"spread": {"bias": -1}},
            {"devices": [{"match": "x", "channel_order": "RGX"}]},
            {"devices": [{"match": "x", "static": "#12345"}]},
            {"devices": [{"match": "x", "static": [1, 2, 300]}]},
            {"socket": {"transport": "udp", "udp_port": "high"}},
            {"openrgb": {"restart_command": "systemctl restart x"}},
        ):
            with self.subTest(section=section):
                with self.assertRaises(ConfigError):
                    parse_config({**MINIMAL, **section}, platform="linux")

    def test_broken_toml_is_a_config_error_too(self) -> None:
        # tomllib raises its own ValueError subclass, which escaped the CLI's
        # one-line handling as a traceback.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text("[socket\npath = 1\n", encoding="utf-8")
            with self.assertRaises(ConfigError) as caught:
                load_config(path)
        self.assertIn(str(path), str(caught.exception))

    def test_xdg_runtime_dir_falls_back_when_unset(self) -> None:
        """The shutdown unit runs as root with no XDG_RUNTIME_DIR.

        ``os.getuid`` is pinned rather than taken from the host. The fallback is
        Linux's, and a Windows host -- this suite runs there too -- has no
        ``getuid`` at all; ``test_no_getuid_is_a_config_error_not_a_crash``
        covers that side. Unpinned, this test errored on Windows.
        """

        with mock.patch.dict(os.environ, _without("XDG_RUNTIME_DIR"), clear=True):
            with mock.patch.object(os, "getuid", return_value=1000, create=True):
                config = parse_config({"socket": {"path": "$XDG_RUNTIME_DIR/x.sock"}}, platform="linux")
        self.assertEqual(config.endpoint, UnixEndpoint(Path("/run/user/1000/x.sock")))


class TransportSelectionTest(unittest.TestCase):
    """Which transport a config gets on each platform -- and that choosing never crashes."""

    def test_auto_is_the_unix_socket_on_linux(self) -> None:
        config = parse_config(MINIMAL, platform="linux")
        self.assertEqual(config.endpoint, UnixEndpoint(Path("/tmp/x.sock")))

    def test_auto_is_udp_on_windows(self) -> None:
        config = parse_config(MINIMAL, platform="win32")
        self.assertEqual(config.endpoint, UdpEndpoint(LOOPBACK, DEFAULT_UDP_PORT))

    def test_windows_needs_no_socket_path(self) -> None:
        self.assertIsInstance(parse_config({}, platform="win32").endpoint, UdpEndpoint)

    def test_udp_can_be_chosen_on_linux(self) -> None:
        config = parse_config({"socket": {"transport": "udp", "udp_port": 50000}}, platform="linux")
        self.assertEqual(config.endpoint, UdpEndpoint(LOOPBACK, 50000))

    def test_the_unix_socket_is_refused_on_windows_with_the_alternative_named(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            parse_config({"socket": {"transport": "unix", "path": "x.sock"}}, platform="win32")
        self.assertIn("udp", str(caught.exception))

    def test_an_unknown_transport_names_the_valid_ones(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            parse_config({"socket": {"transport": "pigeon"}}, platform="linux")
        for name in ("auto", "unix", "udp"):
            self.assertIn(name, str(caught.exception))

    def test_the_udp_port_must_be_a_port(self) -> None:
        for port in (0, 65536, -1, "high", 1.5, True):
            with self.subTest(port=port):
                with self.assertRaises(ConfigError):
                    parse_config({"socket": {"transport": "udp", "udp_port": port}}, platform="linux")

    def test_a_relative_path_is_resolved_from_the_config_file(self) -> None:
        config = parse_config({"socket": {"path": "rel.sock"}}, base_dir=Path("/etc/x"), platform="linux")
        self.assertEqual(config.endpoint.path, Path("/etc/x/rel.sock").resolve())

    def test_an_unset_variable_is_an_error_not_a_directory_named_dollar(self) -> None:
        """expandvars leaves an unknown variable in place, verbatim."""

        with mock.patch.dict(os.environ, _without("AMBIENCE_TEST_UNSET"), clear=True):
            with self.assertRaises(ConfigError) as caught:
                parse_config({"socket": {"path": "$AMBIENCE_TEST_UNSET/x.sock"}}, platform="linux")
        self.assertIn("AMBIENCE_TEST_UNSET", str(caught.exception))

    def test_no_getuid_is_a_config_error_not_a_crash(self) -> None:
        # The runtime-dir fallback needs os.getuid, which Windows does not have.
        with mock.patch.dict(os.environ, _without("XDG_RUNTIME_DIR"), clear=True):
            with mock.patch.object(os, "getuid", None, create=True):
                with self.assertRaises(ConfigError):
                    parse_config({"socket": {"path": "$XDG_RUNTIME_DIR/x.sock"}}, platform="linux")


class RestartCommandTest(unittest.TestCase):
    """The OpenRGB restart command differs per platform; the config file does not."""

    def _command(self, value, platform: str) -> tuple:
        return parse_config({**MINIMAL, "openrgb": {"restart_command": value}}, platform=platform).openrgb_restart_command

    def test_a_list_applies_on_every_platform(self) -> None:
        for platform in ("linux", "win32"):
            with self.subTest(platform=platform):
                self.assertEqual(self._command(["a", "b"], platform), ("a", "b"))

    def test_a_table_picks_this_platform(self) -> None:
        table = {"linux": ["systemctl", "x"], "windows": ["powershell", "y"]}
        self.assertEqual(self._command(table, "linux"), ("systemctl", "x"))
        self.assertEqual(self._command(table, "win32"), ("powershell", "y"))

    def test_a_table_without_this_platform_disables_it(self) -> None:
        self.assertEqual(self._command({"linux": ["systemctl", "x"]}, "win32"), ())

    def test_a_misspelled_platform_is_refused(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            self._command({"window": ["powershell"]}, "win32")
        self.assertIn("windows", str(caught.exception))
        self.assertIn("linux", str(caught.exception))

    def test_a_string_is_refused_rather_than_run_letter_by_letter(self) -> None:
        """🚨 ``tuple("systemctl restart x")`` is a tuple of CHARACTERS.

        It used to be accepted, and the restart then ran a program called ``s``
        -- which does not exist, so the one recovery for a wedged server failed
        with nothing in the log but a FileNotFoundError.
        """

        with self.assertRaises(ConfigError):
            self._command("systemctl --user restart ambience-rgb-openrgb", "linux")

    def test_every_argument_must_be_a_string(self) -> None:
        with self.assertRaises(ConfigError):
            self._command(["systemctl", 1], "linux")


class SingleColourDeviceTest(unittest.TestCase):
    """The Commander Core and the Katana: devices configured by group, not by rule."""

    def test_the_katana_is_off_unless_asked_for(self) -> None:
        # A config written before the Katana existed must not start posting to
        # a Home Assistant it never named.
        katana = parse_config(MINIMAL).katana_ha
        self.assertFalse(katana.enabled)
        self.assertEqual(katana.group, GROUP_FAR)

    def test_the_removed_usb_driver_is_refused_and_says_what_replaced_it(self) -> None:
        # Silently ignoring it would leave the light bar dark with no reason given.
        for payload in (
            {"katana": {"enabled": True}},
            {"katana": {"enabled": True, "port": "COM3"}, "katana_ha": {"enabled": True, "url": "http://h:8123"}},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ConfigError) as caught:
                    parse_config({**MINIMAL, **payload})
                self.assertIn("[katana_ha]", str(caught.exception))
                self.assertIn("removed", str(caught.exception))

    def test_a_disabled_usb_section_left_in_an_old_config_is_harmless(self) -> None:
        config = parse_config({**MINIMAL, "katana": {"enabled": False, "group": "far", "port": "/dev/ttyACM0"}})
        self.assertFalse(config.katana_ha.enabled)

    def test_excluded_is_refused_where_it_cannot_mean_anything(self) -> None:
        # 🚨 These devices are written whole, so a group of "excluded" cannot
        # leave part of one alone. It used to be accepted -- and then quietly
        # driven with the FAR colour, the opposite of what it says.
        for section in (
            {"katana_ha": {"group": "excluded"}},
            {"commander": {"pump_group": "excluded"}},
            {"commander": {"fan_group": "excluded"}},
        ):
            with self.subTest(section=section):
                with self.assertRaises(ConfigError) as caught:
                    parse_config({**MINIMAL, **section})
                self.assertIn("enabled = false", str(caught.exception))


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


class SearchPathTest(unittest.TestCase):
    def test_the_override_comes_first(self) -> None:
        found = config_search_path(
            platform="linux", environ={"AMBIENCE_RGB_CONFIG": "/x/c.toml", "XDG_CONFIG_HOME": "/cfg"}
        )
        self.assertEqual(found[0], Path("/x/c.toml"))

    def test_linux_looks_in_xdg_config_home(self) -> None:
        found = config_search_path(platform="linux", environ={"XDG_CONFIG_HOME": "/cfg"})
        self.assertIn(Path("/cfg/ambience-rgb/config.toml"), found)

    def test_windows_looks_in_appdata(self) -> None:
        found = config_search_path(platform="win32", environ={"APPDATA": "/roaming"})
        self.assertIn(Path("/roaming/ambience-rgb/config.toml"), found)

    def test_the_repo_default_is_always_last(self) -> None:
        for platform in ("linux", "win32"):
            with self.subTest(platform=platform):
                self.assertEqual(config_search_path(platform=platform, environ={})[-1], SHIPPED)

    def test_a_stripped_environment_does_not_crash(self) -> None:
        """A scheduled task or a service can start with no APPDATA and no home."""

        with mock.patch("ambience_rgb.paths._home_or_none", return_value=None):
            found = config_search_path(platform="win32", environ={})
        self.assertEqual(found, [SHIPPED])

    def test_the_resolved_path_is_the_first_that_exists(self) -> None:
        """What the installers bake into the shutdown task and unit.

        That runs as SYSTEM or root, with a different home and a different
        environment, so it cannot search for the user's config itself -- it has
        to be handed the path the daemon would have used.
        """

        with tempfile.TemporaryDirectory() as directory:
            environ = {"XDG_CONFIG_HOME": directory}
            self.assertEqual(resolved_config_path(platform="linux", environ=environ), SHIPPED)
            mine = Path(directory) / "ambience-rgb" / "config.toml"
            mine.parent.mkdir()
            mine.write_text("[socket]\npath = '/tmp/x.sock'\n", encoding="utf-8")
            self.assertEqual(resolved_config_path(platform="linux", environ=environ), mine)


class ShippedConfigTest(unittest.TestCase):
    """The example config is the fallback, so it must actually be valid -- everywhere."""

    def test_it_parses_and_describes_this_rig(self) -> None:
        config = load_config(SHIPPED)

        self.assertEqual(config.openrgb_host, "127.0.0.1", "never bind the LAN")
        self.assertTrue(config.commander.enabled)
        # Since 2026-10-07 the soundbar goes through Home Assistant, not USB.
        self.assertTrue(config.katana_ha.enabled)
        self.assertEqual(config.katana_ha.group, GROUP_FAR, "it lights the desk, not the screen")

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

        config = load_config(SHIPPED)
        self.assertGreater(config.smbus_interval_seconds, config.usb_interval_seconds)
        for name in ("ASUS TUF GeForce RTX 5090 Gaming OC", "Corsair Dominator Titanium RGB DDR5"):
            with self.subTest(device=name):
                self.assertEqual(config.rule_for(name).bus, "smbus")

    def test_it_loads_on_windows_without_a_posix_only_call(self) -> None:
        """🚨 The shipped config used to crash Windows before a single light lit.

        Its socket path is ``$XDG_RUNTIME_DIR/...``. With that variable unset --
        as it always is on Windows -- the fallback called ``os.getuid()``, which
        does not exist there: an ``AttributeError`` at config load, on every
        start, for every entry point including the shutdown task. Windows uses
        UDP, so the path must not even be looked at.
        """

        posix_only = AssertionError("a POSIX-only call was made while loading for Windows")
        with mock.patch.dict(os.environ, _without("XDG_RUNTIME_DIR"), clear=True):
            with mock.patch.object(os, "getuid", side_effect=posix_only, create=True):
                config = load_config(SHIPPED, platform="win32")
        self.assertEqual(config.endpoint, UdpEndpoint(LOOPBACK, DEFAULT_UDP_PORT))

    def test_it_restarts_the_right_openrgb_on_each_platform(self) -> None:
        # The Linux half of this runs on Windows too, which has neither
        # XDG_RUNTIME_DIR nor os.getuid to stand in for it -- this errored
        # there. The runtime dir given is absolute on whatever host runs this:
        # a POSIX /run/user path is relative on Windows, so it was joined to the
        # config's directory and resolved -- and under Wine, whose Z: is the
        # live Linux tree, that resolve landed on the real daemon's socket.
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": tempfile.gettempdir()}):
            linux = load_config(SHIPPED, platform="linux").openrgb_restart_command
        self.assertIn("systemctl", linux)
        self.assertIn("ambience-rgb-openrgb.service", linux)
        windows = " ".join(load_config(SHIPPED, platform="win32").openrgb_restart_command)
        self.assertIn("Start-ScheduledTask", windows)
        self.assertIn("ambience-rgb-openrgb", windows)
        self.assertNotIn("systemctl", windows)


class OpenRgbRgSwapTest(unittest.TestCase):
    """``openrgb_rg_swap``: the zones whose red/green swap OpenRGB is made to do.

    🚨 Two layers swap red and green on an ASRock Polychrome board, and they
    must move together. OpenRGB swaps a zone when the board's own table OR its
    settings file says so -- ``rgswap = table_bit || setting`` in
    ``PolychromeUSBController::SetDeviceInfo`` -- so the file can force a swap
    on and never off. ``zone_channel_order`` is the other layer. The installers
    write this list into ``OpenRGB.json``; see ``ambience_rgb.openrgb_settings``.
    """

    def test_it_is_a_list_of_zone_names(self) -> None:
        rule = parse_config(
            {**MINIMAL, "devices": [{"match": "x", "openrgb_rg_swap": ["IO Cover", "PCB"]}]}
        ).devices[0]
        self.assertEqual(rule.openrgb_rg_swap, ("IO Cover", "PCB"))

    def test_no_list_means_no_pin(self) -> None:
        # No rule, no pin: another board's OpenRGB settings are not ours to change.
        rule = parse_config({**MINIMAL, "devices": [{"match": "x"}]}).devices[0]
        self.assertEqual(rule.openrgb_rg_swap, ())

    def test_a_table_of_booleans_is_refused(self) -> None:
        """There is no "off" to pin, so a ``false`` would do nothing, silently."""

        with self.assertRaises(ConfigError) as caught:
            parse_config({**MINIMAL, "devices": [{"match": "x", "openrgb_rg_swap": {"PCB": False}}]})
        self.assertIn("list", str(caught.exception))

    def test_anything_but_zone_names_is_refused(self) -> None:
        # A plain string would iterate as its characters.
        for value in ("PCB", ["PCB", ""], [1], ["PCB", None]):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    parse_config({**MINIMAL, "devices": [{"match": "x", "openrgb_rg_swap": value}]})

    def test_every_zone_the_shipped_config_swaps_back_is_one_openrgb_swaps(self) -> None:
        """🚨 The pairing guard: a zone swapped back must be one OpenRGB swaps.

        The config's ``GRB`` undoes OpenRGB's red/green swap -- but only while
        that swap is on, and unpinned it is the board's table's call, which
        differs between boots: OpenRGB itself writes zeros into it on entering
        Direct mode, and on the first Windows boot it read set for the headers
        where Linux had read it clear. Pinned in ``OpenRGB.json``, the swap
        holds on every OpenRGB and every boot. A ``GRB`` without the pin comes
        out swapped once wherever the table reads clear: green for red.
        """

        config = load_config(SHIPPED)
        swapped_back = [
            (rule, zone)
            for rule in config.devices
            for zone, order in rule.zone_channel_order.items()
            if order == "GRB"
        ]
        self.assertTrue(swapped_back, "the shipped config swaps no zone back; this guard checks nothing")
        for rule, zone in swapped_back:
            with self.subTest(device=rule.match, zone=zone):
                self.assertIn(zone, rule.openrgb_rg_swap)

    def test_on_this_board_every_pinned_zone_is_swapped_back(self) -> None:
        """🚨 The five zones -- and on THIS board, the pin and the GRB are one set.

        With OpenRGB's swap on, all five ASRock zones show red as green, and the
        swap can be forced on but never off. So all five are pinned on and all
        five are sent GRB: the one state that does not depend on the board's
        table, which differs between boots. On the first Windows boot the
        headers showed green for red although OpenRGB.json said false -- the
        board's bits for them read set -- and pinning them on changed nothing,
        by eye with pure red and pure blue. Linux had read those bits clear.

        Not a universal rule -- LEDs that take their bytes as RGB would want the
        pin without the GRB -- but a fact of this rig. PCH and the two 12 V RGB
        headers are left to OpenRGB: none of them was ever checked by eye.
        """

        rule = load_config(SHIPPED).rule_for("ASRock X870E Taichi")
        pinned = set(rule.openrgb_rg_swap)
        self.assertEqual(
            pinned,
            {"IO Cover", "PCB", "Addressable Header 1", "Addressable Header 2", "Addressable Header 3/Audio"},
        )
        swapped_back = {zone for zone, order in rule.zone_channel_order.items() if order == "GRB"}
        self.assertEqual(swapped_back, pinned)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
