"""The OpenRGB settings ambience-rgb depends on -- for both installers.

Two things in ``OpenRGB.json`` decide whether this program works, and OpenRGB
gets neither right on its own:

- **The Commander Core detectors must be off.** Exactly one process may drive
  a HID device, and neither Linux's hidraw nor Windows' HID stack enforces it: a
  second driver opens the device just as successfully, and the two interleave.
  OpenRGB registers the Commander Core even though it cannot size its zones, so
  with its detector on nothing errors and the colours never apply.
- **The red/green swap must be on for the zones the config names** -- each
  device rule's ``openrgb_rg_swap``. OpenRGB swaps an ASRock Polychrome zone
  when the board's own table OR this setting says so
  (``PolychromeUSBController::SetDeviceInfo``: ``rgswap = table_bit ||
  setting``), so the file can force a swap on and never off -- and the table is
  nothing to lean on, since OpenRGB writes zeros into it on entering Direct
  mode. What ``zone_channel_order`` sends comes out right only while the swap
  is where the config assumes: if the board's table has a zone's swap on
  and nothing swaps it back, that zone shows green for red.

OpenRGB reads this file once, as it detects devices, and writes the whole of it
back from memory whenever it saves a setting. An edit made while it runs does
not apply until it restarts, and can be overwritten before then -- so both
installers stop their OpenRGB first and start it again afterwards.

This is a module, not an inline snippet, so that ``install.sh`` and
``install.ps1`` run the very same edit, rather than a PowerShell copy that
would drift -- and that would meet ``ConvertTo-Json``'s default depth of 2,
which silently flattens anything nested deeper. ``OpenRGB.json`` is deeper.

    python -m ambience_rgb.openrgb_settings PATH-TO/OpenRGB.json [--config CONFIG.toml]

Without ``--config`` it reads the config the daemon reads.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ambience_rgb.config import ConfigError, load_config

COMMANDER_DETECTORS = ("Corsair Commander Core", "Corsair Commander Core XT")

# The one per-zone red/green swap OpenRGB exposes: its ASRock Polychrome USB
# controller's, keyed by OpenRGB's zone names ("IO Cover", "Addressable Header 1").
RG_SWAP_SECTION = "ASRock Polychrome USB"
RG_SWAP_KEY = "RGSwap"

USAGE = "usage: python -m ambience_rgb.openrgb_settings PATH-TO/OpenRGB.json [--config CONFIG.toml]"


@dataclass(frozen=True)
class Changes:
    """What an update changed. False when it changed nothing."""

    detectors: tuple[str, ...] = ()
    rg_swaps: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.detectors or self.rg_swaps)


def update_settings(path: Path, *, rg_swaps=(), backup_to: Path | None = None) -> Changes:
    """Turn both detectors off and force each zone in ``rg_swaps`` on, in ``path``.

    One read, one write and one backup of the original, however much changes.
    Nothing is written when nothing needs changing, so re-running an installer
    leaves the file byte-for-byte alone. When a change IS needed and
    ``backup_to`` is given, the original goes there first.

    **No file yet is not a reason to skip.** OpenRGB may not be installed yet,
    and waiting would leave a trap: install it after
    ambience-rgb, run it once, and it detects the Commander Core with its
    detector on -- beside the daemon. So the file is created holding just these
    entries, and OpenRGB starts from them whenever it first runs. A swap section
    holding only our zones is safe: OpenRGB reads a zone's swap only when the
    zone is present, and writes its own defaults only when the section is
    missing altogether.

    Zones not in ``rg_swaps`` are left exactly as they are, and with no zones no
    swap section is written at all: another board's settings are not ours.

    Raises ``ValueError`` for a file, or a section of one, that this does not
    understand, rather than replacing someone's settings with ours.
    """

    path = Path(path)
    original = path.read_bytes() if path.exists() else None
    settings = json.loads(original.decode("utf-8-sig")) if original is not None else {}
    if not isinstance(settings, dict):
        raise ValueError(f"{path} is not a JSON object; leaving it alone")
    changes = Changes(
        detectors=_disable_detectors(settings, path),
        rg_swaps=_force_rg_swaps(settings, tuple(rg_swaps), path),
    )
    if not changes:
        return changes
    if original is not None and backup_to is not None:
        Path(backup_to).write_bytes(original)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=4), encoding="utf-8")
    return changes


def disable_commander_detectors(path: Path, *, backup_to: Path | None = None) -> list[str]:
    """The detector half of :func:`update_settings`: the names that changed."""

    return list(update_settings(path, backup_to=backup_to).detectors)


def _disable_detectors(settings: dict, path: Path) -> tuple[str, ...]:
    section = settings.setdefault("Detectors", {})
    detectors = section.setdefault("detectors", {}) if isinstance(section, dict) else None
    if not isinstance(detectors, dict):
        raise ValueError(f"{path} has a Detectors section this does not understand; leaving it alone")
    changed = tuple(name for name in COMMANDER_DETECTORS if detectors.get(name) is not False)
    for name in changed:
        detectors[name] = False
    return changed


def _force_rg_swaps(settings: dict, zones: tuple[str, ...], path: Path) -> tuple[str, ...]:
    if not zones:
        return ()
    section = settings.setdefault(RG_SWAP_SECTION, {})
    swaps = section.setdefault(RG_SWAP_KEY, {}) if isinstance(section, dict) else None
    if not isinstance(swaps, dict):
        raise ValueError(f"{path} has an {RG_SWAP_SECTION!r} section this does not understand; leaving it alone")
    changed = tuple(zone for zone in dict.fromkeys(zones) if swaps.get(zone) is not True)
    for zone in changed:
        swaps[zone] = True
    return changed


def _rg_swaps_from_config(config_path: Path | None) -> tuple[str, ...]:
    """Every zone any device rule asks OpenRGB to swap, once each, in order."""

    config = load_config(config_path) if config_path is not None else load_config()
    return tuple(dict.fromkeys(zone for rule in config.devices for zone in rule.openrgb_rg_swap))


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    config_path = None
    if "--config" in args:
        at = args.index("--config")
        if at + 1 >= len(args):
            print(USAGE, file=sys.stderr)
            return 2
        config_path = Path(args[at + 1])
        del args[at : at + 2]
    if len(args) != 1:
        print(USAGE, file=sys.stderr)
        return 2
    path = Path(args[0])

    # The detectors are pinned whatever the config says, or fails to say: a
    # typo in an unrelated key must not hand the AIO to OpenRGB.
    status = 0
    try:
        zones = _rg_swaps_from_config(config_path)
    except (ConfigError, OSError) as error:
        print(f"  Could not read the config, so OpenRGB's red/green swaps are NOT pinned: {error}")
        zones = ()
        status = 1

    backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    existed = path.exists()
    try:
        changes = update_settings(path, rg_swaps=zones, backup_to=backup)
    except (OSError, ValueError) as error:
        print(f"  Could not update {path}: {error}")
        return 1

    if changes.detectors:
        print(f"  OpenRGB's Commander Core detectors disabled: {', '.join(changes.detectors)}")
    else:
        print(f"  OpenRGB's Commander Core detectors: already disabled ({path})")
    if changes.rg_swaps:
        print(f"  OpenRGB's red/green swap forced on: {', '.join(changes.rg_swaps)}")
    elif zones:
        print(f"  OpenRGB's red/green swap: already on for {', '.join(zones)}")
    if changes:
        if existed:
            print(f"  backup: {backup}")
        else:
            print(f"  created {path}; OpenRGB will start from it whenever it first runs")
        print("  OpenRGB reads this file only when it starts: restart it for the change to apply.")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
