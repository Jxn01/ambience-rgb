"""Turn off OpenRGB's Commander Core detectors -- for both installers.

Exactly one process may drive a HID device, and neither Linux's hidraw nor
Windows' HID stack enforces it: a second driver opens the device just as
successfully, and the two interleave. OpenRGB registers the Commander Core even
though it cannot size its zones, so with its detector on nothing errors and the
colours never apply.

This was an inline Python snippet in ``install.sh``. It is a module so that
``install.ps1`` runs the very same edit, rather than a PowerShell copy that
would drift -- and that would meet ``ConvertTo-Json``'s default depth of 2,
which silently flattens anything nested deeper. ``OpenRGB.json`` is deeper.

    python -m ambience_rgb.openrgb_settings PATH-TO/OpenRGB.json
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

COMMANDER_DETECTORS = ("Corsair Commander Core", "Corsair Commander Core XT")


def disable_commander_detectors(path: Path, *, backup_to: Path | None = None) -> list[str]:
    """Set both detectors off in ``path``; return the names that changed.

    Nothing is written when both are already off, so re-running an installer
    leaves the file byte-for-byte alone. When a change IS needed and
    ``backup_to`` is given, the original goes there first.

    🚨 **No file yet is not a reason to skip.** On the Windows half OpenRGB is
    not even installed yet, and waiting would leave a trap: install it after
    ambience-rgb, run it once, and it detects the Commander Core with its
    detector on -- beside the daemon. So the file is created holding just these
    two entries, and OpenRGB starts from them whenever it first runs.

    Raises ``ValueError`` for a file that is not a JSON object, rather than
    replacing someone's settings with ours.
    """

    path = Path(path)
    original = path.read_bytes() if path.exists() else None
    settings = json.loads(original.decode("utf-8-sig")) if original is not None else {}
    if not isinstance(settings, dict):
        raise ValueError(f"{path} is not a JSON object; leaving it alone")
    section = settings.setdefault("Detectors", {})
    detectors = section.setdefault("detectors", {}) if isinstance(section, dict) else None
    if not isinstance(detectors, dict):
        raise ValueError(f"{path} has a Detectors section this does not understand; leaving it alone")

    changed = [name for name in COMMANDER_DETECTORS if detectors.get(name) is not False]
    if not changed:
        return []
    for name in changed:
        detectors[name] = False
    if original is not None and backup_to is not None:
        Path(backup_to).write_bytes(original)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=4), encoding="utf-8")
    return changed


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m ambience_rgb.openrgb_settings PATH-TO/OpenRGB.json", file=sys.stderr)
        return 2
    path = Path(args[0])
    backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    existed = path.exists()
    try:
        changed = disable_commander_detectors(path, backup_to=backup)
    except (OSError, ValueError) as error:
        print(f"  Could not update {path}: {error}")
        return 1
    if not changed:
        print(f"  OpenRGB's Commander Core detectors: already disabled ({path})")
        return 0
    print(f"  OpenRGB's Commander Core detectors disabled: {', '.join(changed)}")
    if existed:
        print(f"  backup: {backup}")
    else:
        print(f"  created {path}; OpenRGB will start from it whenever it first runs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
