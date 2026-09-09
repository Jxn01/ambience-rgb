#!/usr/bin/env bash
#
# Install linux-rgb on this machine.
#
# Idempotent: safe to re-run after pulling. Everything it changes outside the
# repo is backed up first and listed at the end.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO_ROOT/.venv"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SYSTEM_UNIT_DIR="/etc/systemd/system"
OPENRGB_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/OpenRGB/OpenRGB.json"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------
# 1. Interpreter
#
# `hid` (python-hidapi) is a DISTRIBUTION package, like PyGObject is for
# matterlights. The venv therefore needs --system-site-packages AND must be
# built from the system interpreter, because a venv inherits the site-packages
# of the python it was created from. `python3` on PATH is frequently miniconda,
# whose site-packages has no hid, so the venv comes up looking fine and dies on
# the first Commander Core write.
# --------------------------------------------------------------------------
SYSTEM_PYTHON="${LINUX_RGB_PYTHON:-/usr/bin/python3}"
[[ -x "$SYSTEM_PYTHON" ]] || die "No system Python at $SYSTEM_PYTHON"

if [[ "$("$SYSTEM_PYTHON" -c 'import sys; print(sys.prefix)')" == *conda* ]]; then
    die "$SYSTEM_PYTHON looks like a conda interpreter. Use /usr/bin/python3."
fi

"$SYSTEM_PYTHON" -c 'import hid' 2>/dev/null || die \
"$SYSTEM_PYTHON cannot import 'hid'. Install it with:
    sudo pacman -S --needed python-hidapi
(Debian/Ubuntu: python3-hid)"

command -v openrgb >/dev/null || warn "openrgb is not installed; only the Commander Core will be driven."

bold "Using $SYSTEM_PYTHON ($("$SYSTEM_PYTHON" -V))"

# --------------------------------------------------------------------------
# 2. Virtualenv
# --------------------------------------------------------------------------
if [[ ! -d "$VENV" ]]; then
    bold "Creating $VENV (--system-site-packages, so it can see hid)"
    "$SYSTEM_PYTHON" -m venv --system-site-packages "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -e "$REPO_ROOT"
bold "Installed linux-rgb into the venv"

# --------------------------------------------------------------------------
# 3. Hand the Commander Core to us, not to OpenRGB
#
# Exactly one process may hold a HID device. OpenRGB registers the Commander
# Core even though it cannot size its zones, and while it holds /dev/hidraw20
# our driver cannot open it -- with no useful error from either side.
# --------------------------------------------------------------------------
if [[ -f "$OPENRGB_CONFIG" ]]; then
    backup="$OPENRGB_CONFIG.bak-$(date +%Y%m%d-%H%M%S)"
    cp "$OPENRGB_CONFIG" "$backup"
    "$SYSTEM_PYTHON" - "$OPENRGB_CONFIG" <<'PY'
import json, sys
from pathlib import Path

path = Path(sys.argv[1])
config = json.loads(path.read_text())
detectors = config.setdefault("Detectors", {}).setdefault("detectors", {})
changed = [
    name
    for name in ("Corsair Commander Core", "Corsair Commander Core XT")
    if detectors.get(name) is not False
]
for name in changed:
    detectors[name] = False
path.write_text(json.dumps(config, indent=4))
print(f"  OpenRGB detectors disabled: {changed or '(already disabled)'}")
PY
    echo "  backup: $backup"
else
    warn "  No OpenRGB config at $OPENRGB_CONFIG; run OpenRGB once, then re-run this."
fi

# --------------------------------------------------------------------------
# 4. Units
# --------------------------------------------------------------------------
install_unit() {   # $1 = unit, $2 = dest dir, $3 = "sudo" or empty
    local src="$REPO_ROOT/systemd/$1" dst="$2/$1"
    ${3:+sudo} mkdir -p "$2"
    sed "s|@INSTALL_DIR@|$REPO_ROOT|g" "$src" | ${3:+sudo} tee "$dst" >/dev/null
    echo "  installed $dst"
}

install_unit linux-rgb-openrgb.service "$USER_UNIT_DIR"
install_unit linux-rgb.service         "$USER_UNIT_DIR"
systemctl --user daemon-reload
systemctl --user enable --now linux-rgb-openrgb.service linux-rgb.service

install_unit linux-rgb-lights-off.service "$SYSTEM_UNIT_DIR" sudo
sudo systemctl daemon-reload
sudo systemctl enable --now linux-rgb-lights-off.service
echo "  (it does nothing at boot; the work is in ExecStop)"

# --------------------------------------------------------------------------
# 5. Tell the user what matterlights still needs
# --------------------------------------------------------------------------
bold "Done."
cat <<EOF
  MatterLights must be told to publish. In its .env:

      RGB_EXTENSION_ENABLED=true
      RGB_PUBLISH_SOCKET=$(
          "$VENV/bin/python" -c "from linux_rgb.config import load_config; print(load_config().socket_path)"
      )

  then: systemctl --user restart matterlights-sync

  What is driven:  $VENV/bin/python -m linux_rgb devices
  Logs:            journalctl --user -u linux-rgb -f
  Off right now:   systemctl --user stop linux-rgb
EOF
