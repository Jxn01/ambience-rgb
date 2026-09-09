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

    def matches(self, controller_name: str) -> bool:
        return self.match.lower() in controller_name.lower()

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
    devices: tuple[DeviceRule, ...] = ()
    commander: CommanderConfig = field(default_factory=CommanderConfig)

    def rule_for(self, controller_name: str) -> DeviceRule | None:
        for rule in self.devices:
            if rule.matches(controller_name):
                return rule
        return None


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
            )
        )

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
