#!/usr/bin/env bash
#
# Install ambience-rgb on this machine.
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
SYSTEM_PYTHON="${AMBIENCE_RGB_PYTHON:-/usr/bin/python3}"
[[ -x "$SYSTEM_PYTHON" ]] || die "No system Python at $SYSTEM_PYTHON"

if [[ "$("$SYSTEM_PYTHON" -c 'import sys; print(sys.prefix)')" == *conda* ]]; then
    die "$SYSTEM_PYTHON looks like a conda interpreter. Use /usr/bin/python3."
fi

"$SYSTEM_PYTHON" -c 'import hid' 2>/dev/null || die \
"$SYSTEM_PYTHON cannot import 'hid'. Install it with:
    sudo pacman -S --needed python-hidapi
(Debian/Ubuntu: python3-hid)"

# ⚠️ Present is not the same as KEPT. On the author's rig python-hidapi arrived as
# a dependency of liquidctl, so pacman considered it liquidctl's: removing
# liquidctl -- or any orphan sweep -- would have uninstalled the binding this
# daemon imports, and the Commander Core would have died on its next open. The
# Katana driver's two libraries were the same shape (pyserial arrived with
# inkscape, cryptography with python-pip), so all three are pinned.
# `pacman -Qdq` is used rather than parsing `-Qi`, whose labels are localised.
if command -v pacman >/dev/null; then
    for package in python-hidapi python-pyserial python-cryptography; do
        if pacman -Qdq "$package" >/dev/null 2>&1; then
            sudo pacman -D --asexplicit "$package" >/dev/null \
                && echo "  $package marked explicitly installed (it was only held as a dependency)"
        fi
    done
fi

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
bold "Installed ambience-rgb into the venv"

# --------------------------------------------------------------------------
# 3. Hand the Commander Core to us, not to OpenRGB or CoolerControl
#
# Exactly one process may DRIVE a HID device, and hidraw will not enforce it:
# there is no exclusive open, so a second driver opens it just as successfully
# and the two processes' commands and replies simply interleave. OpenRGB
# registers the Commander Core even though it cannot size its zones; with its
# detector on, nothing errors and the colours never apply.
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

# CoolerControl is the second contender, and it arrives silently. Its liquidctl
# integration defaults to ON but does nothing while liquidctl is absent -- so it
# can sit armed for months and go live at the first boot after anything installs
# liquidctl. It then polls the Commander Core, and liquidctl brackets every poll
# with WAKE and SLEEP; SLEEP is hardware mode, so the AIO flickers between our
# colours and its firmware rainbow every second or two.
# Warned about rather than edited: it is a root service that also runs GPU fan
# curves, and editing its config safely means stopping it.
CC_CONFIG=/etc/coolercontrol/config.toml
if [[ -f "$CC_CONFIG" ]] && grep -qE '^[[:space:]]*liquidctl_integration[[:space:]]*=[[:space:]]*true' "$CC_CONFIG"; then
    if "$SYSTEM_PYTHON" -c 'import liquidctl' 2>/dev/null; then
        state="ACTIVE: liquidctl is installed, so it drives the Commander Core (from CoolerControl's next restart at the latest)"
    else
        state="ARMED: harmless until something installs liquidctl, then live at CoolerControl's next restart"
    fi
    warn "  CoolerControl's liquidctl integration is on -- $state.
  It will fight this daemon for the Commander Core. Turn it off:
      sudo systemctl stop coolercontrold
      sudo sed -i 's/^liquidctl_integration = true/liquidctl_integration = false/' $CC_CONFIG
      sudo systemctl start coolercontrold
  GPU and hwmon fan control are unaffected; only liquidctl devices leave CoolerControl."
fi

# --------------------------------------------------------------------------
# 3b. Give this user the Katana's serial port, and keep ModemManager off it
#
# The soundbar's control port is a CDC ACM device -- by USB class, a modem -- so
# it is created root:uucp 0660 and ModemManager probes it with AT commands. The
# rule tags it `uaccess` (an ACL for whoever is logged in at the seat) and tells
# ModemManager to leave it alone. See the rule file for why its name starts 60.
# --------------------------------------------------------------------------
UDEV_RULE=60-ambience-rgb-katana.rules
if [[ -d /etc/udev/rules.d ]]; then
    sudo install -m 0644 "$REPO_ROOT/udev/$UDEV_RULE" "/etc/udev/rules.d/$UDEV_RULE"
    sudo udevadm control --reload
    # Re-apply to a soundbar that is already plugged in; a new one gets it on arrival.
    sudo udevadm trigger --action=change --subsystem-match=tty --sysname-match='ttyACM*'
    sudo udevadm settle
    echo "  installed /etc/udev/rules.d/$UDEV_RULE"
    # The ACL is the check, not the rule file's existence.
    katana_tty=$("$SYSTEM_PYTHON" -c '
from serial.tools.list_ports import comports
print(next((p.device for p in comports() if p.vid == 0x041E and p.pid in (0x3283, 0x3260)), ""))
' 2>/dev/null || true)
    if [[ -n "$katana_tty" ]]; then
        if [[ -r "$katana_tty" && -w "$katana_tty" ]]; then
            echo "  Katana at $katana_tty: readable and writable by $USER"
        else
            warn "  Katana at $katana_tty is still not accessible to $USER.
  uaccess grants the user logged in at the seat; over SSH, log in locally. Check: getfacl $katana_tty"
        fi
    fi
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

install_unit ambience-rgb-openrgb.service "$USER_UNIT_DIR"
install_unit ambience-rgb.service         "$USER_UNIT_DIR"
systemctl --user daemon-reload
systemctl --user enable --now ambience-rgb-openrgb.service ambience-rgb.service

install_unit ambience-rgb-lights-off.service "$SYSTEM_UNIT_DIR" sudo
sudo systemctl daemon-reload
sudo systemctl enable --now ambience-rgb-lights-off.service
echo "  (it does nothing at boot; the work is in ExecStop)"

# --------------------------------------------------------------------------
# 5. Tell the user what matterlights still needs
# --------------------------------------------------------------------------
bold "Done."
cat <<EOF
  MatterLights must be told to publish. In its .env:

      RGB_EXTENSION_ENABLED=true
      RGB_PUBLISH_SOCKET=$(
          "$VENV/bin/python" -c "from ambience_rgb.config import load_config; print(load_config().socket_path)"
      )

  then: systemctl --user restart matterlights-sync

  What is driven:  $VENV/bin/python -m ambience_rgb devices
  Logs:            journalctl --user -u ambience-rgb -f
  Off right now:   systemctl --user stop ambience-rgb
EOF
