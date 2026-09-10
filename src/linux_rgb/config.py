"""Configuration for linux-rgb.

Every fact about this particular rig -- which controller belongs to which
group, which zones split, what is excluded, how fast each bus may be driven --
lives in TOML rather than in the source. The program is machine-specific by
purpose, but it should not be machine-specific by construction: a different
mix of hardware should mean a different config file, not a patch.

Read from, in order:

1. ``$LINUX_RGB_CONFIG``
2. ``$XDG_CONFIG_HOME/linux-rgb/config.toml`` (usually ``~/.config/...``)
3. ``config.example.toml`` next to the repo, as a last-resort default

so a checkout runs without setup while a real install can override everything.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

GROUP_NEAR = "near"
GROUP_FAR = "far"
GROUP_EXCLUDED = "excluded"
VALID_GROUPS = frozenset({GROUP_NEAR, GROUP_FAR, GROUP_EXCLUDED})

BUS_USB = "usb"
BUS_SMBUS = "smbus"
VALID_BUSES = frozenset({BUS_USB, BUS_SMBUS})


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
    """The order this device's outputs are wired, e.g. ``"GRB"``."""
    zone_channel_order: dict[str, str] = field(default_factory=dict)
    """Per-zone overrides, because one controller can wire zones differently.

    The ASRock X870E Taichi drives its addressable headers as RGB and its own
    onboard LEDs as GRB. Same device, same write, two orders.
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
class Config:
    socket_path: Path
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

    Empty disables it, which is the default: restarting another unit is a real
    side effect and has to be opted into.
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

    def rule_for(self, controller_name: str) -> DeviceRule | None:
        for rule in self.devices:
            if rule.matches(controller_name):
                return rule
        return None


def _validate_order(value, where: str) -> str:
    order = str(value).upper()
    if sorted(order) != ["B", "G", "R"]:
        raise ValueError(
            f"{where}.channel_order must be a permutation of RGB, got {value!r}"
        )
    return order


def _positive(value, where: str) -> float:
    number = float(value)
    if number <= 0:
        raise ValueError(f"{where} must be greater than 0, got {value!r}")
    return number


def _fraction(value, where: str) -> float:
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"{where} must be between 0 and 1, got {value!r}")
    return number


def _validate_static(value, where: str) -> tuple[int, int, int] | None:
    """Parse a fixed colour: ``[r, g, b]`` or ``"#rrggbb"``."""

    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if len(text) != 6:
            raise ValueError(f"{where}.static must be #rrggbb, got {value!r}")
        try:
            return tuple(int(text[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
        except ValueError as error:
            raise ValueError(f"{where}.static is not a hex colour: {value!r}") from error
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{where}.static must be three numbers or #rrggbb, got {value!r}")
    channels = []
    for channel in value:
        number = int(channel)
        if not 0 <= number <= 255:
            raise ValueError(f"{where}.static channel out of range: {channel!r}")
        channels.append(number)
    return (channels[0], channels[1], channels[2])


def _validate_group(value: str, where: str) -> str:
    group = str(value).strip().lower()
    if group not in VALID_GROUPS:
        raise ConfigError(
            f"{where}: group must be one of {', '.join(sorted(VALID_GROUPS))}, got {value!r}"
        )
    return group


def _validate_bus(value: str, where: str) -> str:
    bus = str(value).strip().lower()
    if bus not in VALID_BUSES:
        raise ConfigError(
            f"{where}: bus must be one of {', '.join(sorted(VALID_BUSES))}, got {value!r}"
        )
    return bus


def _expand_runtime_vars(raw: str) -> str:
    """Expand environment variables, with a working default for XDG_RUNTIME_DIR.

    The shutdown unit runs as root with a stripped environment, where
    ``$XDG_RUNTIME_DIR`` is simply unset -- and ``os.path.expandvars`` leaves an
    unset variable in place verbatim, producing a path with a literal ``$`` in
    it. That is not an error anyone can read. Substituting the value systemd
    would have used keeps the same config file valid in both contexts.
    """

    if "$XDG_RUNTIME_DIR" in raw and not os.getenv("XDG_RUNTIME_DIR"):
        raw = raw.replace("$XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return os.path.expandvars(raw)


def parse_config(payload: dict, *, base_dir: Path | None = None) -> Config:
    """Turn a parsed TOML mapping into a Config, or say exactly what is wrong.

    Pure, so every validation branch is testable without touching the disk.
    """

    if not isinstance(payload, dict):
        raise ConfigError("Configuration must be a table")

    socket_section = payload.get("socket") or {}
    raw_path = str(socket_section.get("path", "")).strip()
    if not raw_path:
        raise ConfigError("socket.path is required -- it must match RGB_PUBLISH_SOCKET")
    socket_path = Path(os.path.expanduser(_expand_runtime_vars(raw_path)))
    if not socket_path.is_absolute() and base_dir is not None:
        socket_path = (base_dir / socket_path).resolve()

    openrgb = payload.get("openrgb") or {}
    rates = payload.get("rates") or {}

    def interval(key: str, default: float) -> float:
        hz = rates.get(key, default)
        try:
            hz = float(hz)
        except (TypeError, ValueError) as error:
            raise ConfigError(f"rates.{key} must be a number, got {hz!r}") from error
        if hz <= 0:
            raise ConfigError(f"rates.{key} must be greater than zero, got {hz}")
        return 1.0 / hz

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
            )
        )

    colour = payload.get("colour") or {}
    spread_section = payload.get("spread") or {}
    commander_section = payload.get("commander") or {}
    commander = CommanderConfig(
        enabled=bool(commander_section.get("enabled", True)),
        pump_group=_validate_group(commander_section.get("pump_group", GROUP_FAR), "commander"),
        fan_group=_validate_group(commander_section.get("fan_group", GROUP_FAR), "commander"),
    )

    return Config(
        socket_path=socket_path,
        openrgb_host=str(openrgb.get("host", "127.0.0.1")),
        openrgb_port=int(openrgb.get("port", 6742)),
        usb_interval_seconds=interval("usb_hz", 5.0),
        smbus_interval_seconds=interval("smbus_hz", 1.0),
        openrgb_restart_command=tuple(openrgb.get("restart_command", ()) or ()),
        saturation=_positive(colour.get("saturation", 1.0), "colour.saturation"),
        gamma=_positive(colour.get("gamma", 1.0), "colour.gamma"),
        group_cohesion=_fraction(colour.get("group_cohesion", 0.0), "colour.group_cohesion"),
        spread_bias=_fraction(spread_section.get("bias", 1.0), "spread.bias"),
        devices=tuple(rules),
        commander=commander,
    )


def config_search_path() -> list[Path]:
    paths = []
    override = os.getenv("LINUX_RGB_CONFIG")
    if override:
        paths.append(Path(override).expanduser())
    xdg = os.getenv("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    paths.append(Path(xdg) / "linux-rgb" / "config.toml")
    paths.append(Path(__file__).resolve().parents[2] / "config.example.toml")
    return paths


def load_config(path: Path | None = None) -> Config:
    candidates = [path] if path is not None else config_search_path()
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            with candidate.open("rb") as handle:
                payload = tomllib.load(handle)
            return parse_config(payload, base_dir=candidate.parent)
    searched = ", ".join(str(c) for c in candidates if c is not None)
    raise ConfigError(f"No configuration file found. Looked in: {searched}")
