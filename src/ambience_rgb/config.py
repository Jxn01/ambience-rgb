"""Configuration for ambience-rgb.

Every fact about this particular rig -- which controller belongs to which
group, which zones split, what is excluded, how fast each bus may be driven --
lives in TOML rather than in the source. The program is machine-specific by
purpose, but it should not be machine-specific by construction: a different
mix of hardware should mean a different config file, not a patch.

Read from, in order:

1. ``$AMBIENCE_RGB_CONFIG``
2. the user's config directory -- ``$XDG_CONFIG_HOME/ambience-rgb/config.toml``
   (usually ``~/.config/...``) on Linux, ``%APPDATA%\\ambience-rgb\\config.toml``
   on Windows
3. ``config.example.toml`` next to the repo, as a last-resort default

so a checkout runs without setup while a real install can override everything.

One file serves both operating systems. Where they genuinely differ -- how
matterlights reaches the daemon, how to restart the OpenRGB server -- the
config says so per platform, and ``platform`` is a parameter throughout so the
Windows branches run in the test suite on Linux, and the Linux ones on Windows.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ambience_rgb import paths
from ambience_rgb.transport import DEFAULT_UDP_PORT, LOOPBACK, Endpoint, UdpEndpoint, UnixEndpoint

GROUP_NEAR = "near"
GROUP_FAR = "far"
GROUP_EXCLUDED = "excluded"
VALID_GROUPS = frozenset({GROUP_NEAR, GROUP_FAR, GROUP_EXCLUDED})

BUS_USB = "usb"
BUS_SMBUS = "smbus"
VALID_BUSES = frozenset({BUS_USB, BUS_SMBUS})

TRANSPORT_AUTO = "auto"
TRANSPORT_UNIX = "unix"
TRANSPORT_UDP = "udp"
VALID_TRANSPORTS = (TRANSPORT_AUTO, TRANSPORT_UNIX, TRANSPORT_UDP)

# What a person writes in a config file for each platform -- "windows", not
# sys.platform's "win32".
_PLATFORM_NAMES = {"linux": "linux", "win32": "windows"}
VALID_PLATFORM_NAMES = frozenset(_PLATFORM_NAMES.values())

EXAMPLE_CONFIG = Path(__file__).resolve().parents[2] / "config.example.toml"


def platform_name(platform: str | None = None) -> str:
    """``linux`` or ``windows`` -- the name a config file uses for ``platform``."""

    platform = platform or sys.platform
    return _PLATFORM_NAMES.get(platform, platform)


REMOVED_KATANA_USB = (
    "[katana] enabled = true: the USB serial driver for the Sound Blaster Katana was "
    "removed on 2026-10-07. The soundbar is driven through Home Assistant now: set "
    "[katana] enabled = false (or delete the section) and enable [katana_ha] -- see "
    "config.example.toml and the README's Katana section."
)


class ConfigError(ValueError):
    """The configuration file is unusable, with the reason named."""


@dataclass(frozen=True, slots=True)
class DeviceRule:
    """How one OpenRGB controller should be driven.

    ``match`` is a case-insensitive substring of the controller name rather than
    an exact string, because OpenRGB renames devices between versions -- the
    Commander Core's ports went from "RGB Port 1" to "Corsair Fan Header 1"
    between 1.0rc3 and the git build. A substring survives that; an exact name
    silently stops matching and the device just stops lighting.
    """

    match: str
    group: str = GROUP_FAR
    bus: str = BUS_USB
    zones: dict[str, str] = field(default_factory=dict)
    channel_order: str = "RGB"
    """The order to send this device's channels in, e.g. ``"GRB"``: which of our
    channels drives each of its outputs, after anything OpenRGB does to them."""
    zone_channel_order: dict[str, str] = field(default_factory=dict)
    """Per-zone overrides, because one controller can treat zones differently.

    On the ASRock X870E Taichi, OpenRGB's red/green swap turns red into green
    on every zone that carries LEDs here, so the config sends those zones
    ``"GRB"``, swapping them back. That holds only while OpenRGB's swap is on --
    which the board's table decides differently between boots -- so
    ``openrgb_rg_swap`` pins it on for the same zones.
    """
    openrgb_rg_swap: tuple[str, ...] = ()
    """Zones whose red/green swap OpenRGB is made to do, through ``OpenRGB.json``.

    The installers write these into OpenRGB's ``ASRock Polychrome USB`` /
    ``RGSwap`` settings -- the one per-zone swap OpenRGB exposes -- with
    ``ambience_rgb.openrgb_settings``, and the names must be OpenRGB's zone
    names exactly. 🚨 A list, not a table of booleans: OpenRGB swaps a zone
    when the board's table OR the setting says so, so a setting can force the
    swap on and never off. Empty leaves OpenRGB's settings alone.
    """
    static: tuple[int, int, int] | None = None
    """A fixed colour to set once per connection, instead of syncing.

    For hardware that should simply sit at one colour. It is written on connect
    and on every reconnect, and never on the tick loop -- which is the point:
    OpenRGB's Direct mode does not persist, so restarting the server leaves such
    a device dark, and an ``excluded`` rule means nothing puts it back. That kept
    costing this rig its keyboard.
    """

    def matches(self, controller_name: str) -> bool:
        return self.match.lower() in controller_name.lower()

    def order_for_zone(self, zone_name: str) -> str:
        for name, order in self.zone_channel_order.items():
            if name.lower() == zone_name.lower():
                return order
        return self.channel_order

    def group_for_zone(self, zone_name: str) -> str:
        for name, group in self.zones.items():
            if name.lower() == zone_name.lower():
                return group
        return self.group


@dataclass(frozen=True, slots=True)
class CommanderConfig:
    enabled: bool = True
    pump_group: str = GROUP_FAR
    fan_group: str = GROUP_FAR


@dataclass(frozen=True, slots=True)
class KatanaHAConfig:
    """The Sound Blaster Katana V2X, through Home Assistant's ``sb_katana`` integration.

    HA owns the soundbar over Bluetooth; this only posts colours to it. Until
    2026-10-07 the soundbar was driven over its USB serial port instead, by
    ``[katana]``; that driver is gone, and a config still enabling it is refused
    (see ``REMOVED_KATANA_USB``).
    """

    enabled: bool = False
    group: str = GROUP_FAR
    url: str | None = None
    """HA's base URL, e.g. ``http://192.168.10.2:8123``. Required when enabled."""
    interval_seconds: float = 0.1
    """The least time between two posts: ``rate_hz`` in the file, 10 by default.
    HA applies its own ceiling too (the integration's screen-sync rate option)."""
    keepalive_seconds: float = 5.0
    """An unchanged colour is re-posted this often, so HA's hand-over (10 s
    without a frame -> lights off) does not fire on a static screen."""
    token_file: str | None = None
    """Where the long-lived access token is read from, when
    ``$AMBIENCE_RGB_HA_TOKEN`` is unset. Unset: ``.ha-token`` at the root of this
    checkout -- gitignored, and on the drive both operating systems share, so one
    file serves both halves of the rig (see ha_stream.read_token)."""
    timeout_seconds: float = 2.0


@dataclass(frozen=True, slots=True)
class Config:
    endpoint: Endpoint
    """Where matterlights' frames arrive. Its ``str()`` is the exact value for
    matterlights' ``RGB_PUBLISH_SOCKET``."""
    openrgb_host: str = "127.0.0.1"
    openrgb_port: int = 6742
    usb_interval_seconds: float = 0.2
    smbus_interval_seconds: float = 1.0
    openrgb_restart_command: tuple[str, ...] = ()
    """How to restart the OpenRGB server when it wedges beyond reconnecting.

    🚨 **This server can stop driving hardware while still answering.** Observed
    on 2026-09-10: round trips at 0.1 ms, new connections served normally, seven
    controllers reported -- and three consecutive colour writes with no effect on
    any device. The liveness probe cannot see that, because the server IS live.
    Only restarting it recovered.

    Already resolved for this platform. Empty disables it, which is the default:
    restarting another program is a real side effect and has to be opted into.
    """
    saturation: float = 1.0
    """Push colours away from grey before writing them. 1.0 disables it.

    An LED is a point source at full output; a bulb is a diffuse area source in a
    room. Identical chromaticity, very different perception -- pale shades that
    read as tinted on a lamp read as plain white on a strip. This is the knob for
    that, and it deliberately makes the two DIFFER in the numbers so they match
    to the eye.
    """
    gamma: float = 1.0
    """Spread the channels apart. 1.0 disables it. See render.treat -- note it
    does not dim a colour already at peak."""
    group_cohesion: float = 0.0
    """Pull the near and far group colours toward each other. 0.0 = untouched.

    The engine gives the groups different palette colours on purpose. On hardware
    that can read as a fault -- the board showing cyan while the fans show pink
    looks broken rather than deliberate. This trades some depth for coherence.
    """
    spread_bias: float = 1.0
    """How hard each LED is pulled back to its group's single colour.

    1.0 is the original flat behaviour and the safe default; 0.0 is the raw
    palette gradient. Anything below 1.0 needs a publisher sending a palette
    (matterlights payload v2 or later).
    """
    devices: tuple[DeviceRule, ...] = ()
    commander: CommanderConfig = field(default_factory=CommanderConfig)
    katana_ha: KatanaHAConfig = field(default_factory=KatanaHAConfig)

    def rule_for(self, controller_name: str) -> DeviceRule | None:
        for rule in self.devices:
            if rule.matches(controller_name):
                return rule
        return None


# ⚠️ Every helper below raises ConfigError, never a bare ValueError. The CLI turns
# a ConfigError into one clean line and exit 2; anything else escapes as a
# traceback, which is how a typo in `saturation` used to present.


def _validate_order(value, where: str) -> str:
    order = str(value).upper()
    if sorted(order) != ["B", "G", "R"]:
        raise ConfigError(
            f"{where}.channel_order must be a permutation of RGB, got {value!r}"
        )
    return order


def _validate_zone_list(value, where: str) -> tuple[str, ...]:
    # A list, never a table: OpenRGB's setting can force a swap on but not off,
    # so a table's `false` would do nothing, silently. And never a plain
    # string, which would iterate as its characters.
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f'{where} must be a list of zone names, e.g. ["IO Cover", "PCB"], got {value!r}')
    for name in value:
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"{where} must hold zone names, got {name!r}")
    return tuple(name.strip() for name in value)


def _number(value, where: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise ConfigError(f"{where} must be a number, got {value!r}") from error


def _positive(value, where: str) -> float:
    number = _number(value, where)
    if number <= 0:
        raise ConfigError(f"{where} must be greater than 0, got {value!r}")
    return number


def _fraction(value, where: str) -> float:
    number = _number(value, where)
    if not 0.0 <= number <= 1.0:
        raise ConfigError(f"{where} must be between 0 and 1, got {value!r}")
    return number


def _rate_interval(value, where: str) -> float:
    """A rate in Hz, as the interval between writes."""
    return 1.0 / _positive(value, where)


def _validate_static(value, where: str) -> tuple[int, int, int] | None:
    """Parse a fixed colour: ``[r, g, b]`` or ``"#rrggbb"``."""

    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if len(text) != 6:
            raise ConfigError(f"{where}.static must be #rrggbb, got {value!r}")
        try:
            return tuple(int(text[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
        except ValueError as error:
            raise ConfigError(f"{where}.static is not a hex colour: {value!r}") from error
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ConfigError(f"{where}.static must be three numbers or #rrggbb, got {value!r}")
    channels = []
    for channel in value:
        try:
            number = int(channel)
        except (TypeError, ValueError) as error:
            raise ConfigError(f"{where}.static channel is not a number: {channel!r}") from error
        if not 0 <= number <= 255:
            raise ConfigError(f"{where}.static channel out of range: {channel!r}")
        channels.append(number)
    return (channels[0], channels[1], channels[2])


def _validate_group(value: str, where: str) -> str:
    group = str(value).strip().lower()
    if group not in VALID_GROUPS:
        raise ConfigError(
            f"{where}: group must be one of {', '.join(sorted(VALID_GROUPS))}, got {value!r}"
        )
    return group


def _validate_sync_group(value, where: str) -> str:
    """A group for a device written whole in one go: near or far, never excluded.

    🚨 "excluded" cannot leave part of such a device alone. It used to be
    accepted anyway -- and then driven with the FAR colour, the opposite of
    what it says. Leaving the device alone is what ``enabled = false`` is for.
    """

    group = _validate_group(value, where)
    if group == GROUP_EXCLUDED:
        raise ConfigError(
            f"{where}: this device is written whole, so it cannot be excluded by group; "
            "set enabled = false to leave it alone"
        )
    return group


def _validate_bus(value: str, where: str) -> str:
    bus = str(value).strip().lower()
    if bus not in VALID_BUSES:
        raise ConfigError(
            f"{where}: bus must be one of {', '.join(sorted(VALID_BUSES))}, got {value!r}"
        )
    return bus


_UNEXPANDED = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")


def _expand_runtime_vars(raw: str) -> str:
    """Expand environment variables, with a working default for XDG_RUNTIME_DIR.

    The shutdown unit runs as root with a stripped environment, where
    ``$XDG_RUNTIME_DIR`` is simply unset -- and ``os.path.expandvars`` leaves an
    unset variable in place verbatim, producing a path with a literal ``$`` in
    it. That is not an error anyone can read. Substituting the value systemd
    would have used keeps the same config file valid in both contexts.

    ⚠️ That substitution needs ``os.getuid``, which does not exist on Windows.
    It used to be called unconditionally, so the shipped config -- whose path is
    exactly ``$XDG_RUNTIME_DIR/...`` -- crashed there at load. Windows no longer
    reaches this at all (it uses UDP), and the lookup is guarded regardless.
    Any variable still unexpanded afterwards is an error that names it.
    """

    if not os.getenv("XDG_RUNTIME_DIR"):
        getuid = getattr(os, "getuid", None)
        if getuid is not None:
            runtime_dir = f"/run/user/{getuid()}"
            raw = raw.replace("${XDG_RUNTIME_DIR}", runtime_dir).replace("$XDG_RUNTIME_DIR", runtime_dir)
    expanded = os.path.expandvars(raw)
    leftover = _UNEXPANDED.search(expanded)
    if leftover:
        raise ConfigError(
            f"socket.path uses ${leftover.group(1)}, which is not set here -- "
            "set it, or write the path out in full"
        )
    return expanded


def _udp_port(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ConfigError(f"socket.udp_port must be a whole number from 1 to 65535, got {value!r}")
    return value


def _parse_endpoint(section, *, base_dir: Path | None, platform: str) -> Endpoint:
    """The transport for this platform, reading only the keys that transport uses.

    That last part is load-bearing, not tidiness: the unix path is never even
    looked at when the transport is UDP, which is what keeps the shipped
    config's ``$XDG_RUNTIME_DIR`` away from Windows.
    """

    if not isinstance(section, dict):
        raise ConfigError("[socket] must be a table")
    transport = str(section.get("transport", TRANSPORT_AUTO)).strip().lower()
    if transport not in VALID_TRANSPORTS:
        raise ConfigError(
            f"socket.transport must be one of {', '.join(VALID_TRANSPORTS)}, "
            f"got {section.get('transport')!r}"
        )
    if transport == TRANSPORT_AUTO:
        transport = TRANSPORT_UDP if platform == "win32" else TRANSPORT_UNIX

    if transport == TRANSPORT_UDP:
        return UdpEndpoint(LOOPBACK, _udp_port(section.get("udp_port", DEFAULT_UDP_PORT)))

    if platform == "win32":
        raise ConfigError(
            'socket.transport = "unix" is not available on Windows, which has no unix '
            'datagram sockets; use "udp", or "auto"'
        )
    raw_path = str(section.get("path", "")).strip()
    if not raw_path:
        raise ConfigError(
            "socket.path is required for the unix socket -- it must match RGB_PUBLISH_SOCKET"
        )
    path = paths.expand_user(Path(_expand_runtime_vars(raw_path)))
    if not path.is_absolute() and base_dir is not None:
        path = (base_dir / path).resolve()
    return UnixEndpoint(path)


def _command_list(value, where: str) -> tuple[str, ...]:
    # 🚨 A bare string is refused rather than tuple()'d: that would be a tuple
    # of its characters, and the restart would run a program called "s".
    if not isinstance(value, (list, tuple)):
        raise ConfigError(
            f'{where} must be a list of arguments, e.g. ["program", "--flag", "value"], got {value!r}'
        )
    if not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{where}: every argument must be a string, got {value!r}")
    return tuple(value)


def _restart_command(value, platform: str) -> tuple[str, ...]:
    """A list for every platform, or a table of lists keyed ``linux``/``windows``.

    Every entry is validated, not only this platform's, so a broken Windows
    command is reported on Linux too -- before the machine that needs it boots.
    """

    if value is None:
        return ()
    if isinstance(value, dict):
        unknown = sorted(str(name) for name in value if name not in VALID_PLATFORM_NAMES)
        if unknown:
            raise ConfigError(
                f"openrgb.restart_command: unknown platform {', '.join(map(repr, unknown))}; "
                f"use {' or '.join(sorted(VALID_PLATFORM_NAMES))}"
            )
        commands = {
            name: _command_list(command, f"openrgb.restart_command.{name}")
            for name, command in value.items()
        }
        return commands.get(platform_name(platform), ())
    return _command_list(value, "openrgb.restart_command")


def parse_config(
    payload: dict, *, base_dir: Path | None = None, platform: str | None = None
) -> Config:
    """Turn a parsed TOML mapping into a Config, or say exactly what is wrong.

    Pure, so every validation branch is testable without touching the disk.
    """

    platform = platform or sys.platform
    if not isinstance(payload, dict):
        raise ConfigError("Configuration must be a table")

    endpoint = _parse_endpoint(payload.get("socket") or {}, base_dir=base_dir, platform=platform)

    openrgb = payload.get("openrgb") or {}
    rates = payload.get("rates") or {}

    def interval(key: str, default: float) -> float:
        return _rate_interval(rates.get(key, default), f"rates.{key}")

    rules: list[DeviceRule] = []
    for index, entry in enumerate(payload.get("devices") or []):
        where = f"devices[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where} must be a table")
        match = str(entry.get("match", "")).strip()
        if not match:
            raise ConfigError(f"{where}.match is required")
        zones = entry.get("zones") or {}
        if not isinstance(zones, dict):
            raise ConfigError(f"{where}.zones must be a table of zone name to group")
        rules.append(
            DeviceRule(
                match=match,
                group=_validate_group(entry.get("group", GROUP_FAR), where),
                bus=_validate_bus(entry.get("bus", BUS_USB), where),
                zones={
                    str(name): _validate_group(group, f"{where}.zones[{name!r}]")
                    for name, group in zones.items()
                },
                static=_validate_static(entry.get("static"), where),
                channel_order=_validate_order(entry.get("channel_order", "RGB"), where),
                zone_channel_order={
                    str(name): _validate_order(order, f"{where}.zone_channel_order[{name!r}]")
                    for name, order in (entry.get("zone_channel_order") or {}).items()
                },
                openrgb_rg_swap=_validate_zone_list(entry.get("openrgb_rg_swap"), f"{where}.openrgb_rg_swap"),
            )
        )

    colour = payload.get("colour") or {}
    spread_section = payload.get("spread") or {}
    commander_section = payload.get("commander") or {}
    commander = CommanderConfig(
        enabled=bool(commander_section.get("enabled", True)),
        pump_group=_validate_sync_group(
            commander_section.get("pump_group", GROUP_FAR), "commander.pump_group"
        ),
        fan_group=_validate_sync_group(
            commander_section.get("fan_group", GROUP_FAR), "commander.fan_group"
        ),
    )
    if (payload.get("katana") or {}).get("enabled"):
        raise ConfigError(REMOVED_KATANA_USB)

    katana_ha_section = payload.get("katana_ha") or {}
    katana_ha = KatanaHAConfig(
        enabled=bool(katana_ha_section.get("enabled", False)),
        group=_validate_sync_group(katana_ha_section.get("group", GROUP_FAR), "katana_ha.group"),
        url=str(katana_ha_section.get("url") or "").strip().rstrip("/") or None,
        interval_seconds=_rate_interval(katana_ha_section.get("rate_hz", 10.0), "katana_ha.rate_hz"),
        keepalive_seconds=_positive(katana_ha_section.get("keepalive_seconds", 5.0), "katana_ha.keepalive_seconds"),
        token_file=str(katana_ha_section.get("token_file") or "").strip() or None,
        timeout_seconds=_positive(katana_ha_section.get("timeout_seconds", 2.0), "katana_ha.timeout_seconds"),
    )
    if katana_ha.enabled:
        if not katana_ha.url or not katana_ha.url.startswith(("http://", "https://")):
            raise ConfigError("katana_ha.url must be Home Assistant's base URL, e.g. http://192.168.10.2:8123")
        if katana_ha.keepalive_seconds >= 10.0:
            raise ConfigError("katana_ha.keepalive_seconds must stay under HA's 10 s hand-over")

    return Config(
        endpoint=endpoint,
        openrgb_host=str(openrgb.get("host", "127.0.0.1")),
        openrgb_port=int(openrgb.get("port", 6742)),
        usb_interval_seconds=interval("usb_hz", 5.0),
        smbus_interval_seconds=interval("smbus_hz", 1.0),
        openrgb_restart_command=_restart_command(openrgb.get("restart_command"), platform),
        saturation=_positive(colour.get("saturation", 1.0), "colour.saturation"),
        gamma=_positive(colour.get("gamma", 1.0), "colour.gamma"),
        group_cohesion=_fraction(colour.get("group_cohesion", 0.0), "colour.group_cohesion"),
        spread_bias=_fraction(spread_section.get("bias", 1.0), "spread.bias"),
        devices=tuple(rules),
        commander=commander,
        katana_ha=katana_ha,
    )


def config_search_path(
    *, platform: str | None = None, environ: Mapping[str, str] | None = None
) -> list[Path]:
    """Every place a config is looked for, most specific first."""

    environ = os.environ if environ is None else environ
    found: list[Path] = []
    override = environ.get("AMBIENCE_RGB_CONFIG")
    if override:
        found.append(paths.expand_user(Path(override)))
    directory = paths.config_dir(platform=platform, environ=environ)
    if directory is not None:
        found.append(directory / "config.toml")
    found.append(EXAMPLE_CONFIG)
    return found


def resolved_config_path(
    *, platform: str | None = None, environ: Mapping[str, str] | None = None
) -> Path:
    """The file :func:`load_config` would read for this user, right now.

    The installers bake this into the shutdown unit and task. Those run as root
    or SYSTEM, with another home and another environment, so they cannot search
    for this user's config themselves -- they have to be handed the path the
    daemon uses, or they black a different set of devices from the one it drives.
    """

    candidates = config_search_path(platform=platform, environ=environ)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(c) for c in candidates)
    raise ConfigError(f"No configuration file found. Looked in: {searched}")


def load_config(path: Path | None = None, *, platform: str | None = None) -> Config:
    candidate = path if path is not None else resolved_config_path(platform=platform)
    if not candidate.is_file():
        raise ConfigError(f"No configuration file at {candidate}")
    try:
        with candidate.open("rb") as handle:
            payload = tomllib.load(handle)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{candidate} is not valid TOML: {error}") from error
    return parse_config(payload, base_dir=candidate.parent, platform=platform)
