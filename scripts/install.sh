#!/usr/bin/env bash
#
# Install ambience-rgb on this machine.
#
# Idempotent: safe to re-run after pulling. Everything it changes outside the
# repo is backed up first and listed at the end.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Outside the checkout -- why is in linux-venv.sh.
# shellcheck source=linux-venv.sh
source "$REPO_ROOT/scripts/linux-venv.sh"
VENV="$AMBIENCE_RGB_VENV"
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

# Present is not the same as KEPT. python-hidapi can arrive as a
# dependency of liquidctl, so pacman considered it liquidctl's: removing
# liquidctl -- or any orphan sweep -- would have uninstalled the binding this
# daemon imports, and the Commander Core would have died on its next open. So it
# is pinned. (Pins left by the removed USB Katana driver, for pyserial and
# cryptography, are not undone; nothing needs them now.)
# `pacman -Qdq` is used rather than parsing `-Qi`, whose labels are localised.
if command -v pacman >/dev/null; then
    for package in python-hidapi; do
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
    mkdir -p "$(dirname "$VENV")"
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
# The edit lives in ambience_rgb.openrgb_settings, shared with the Windows
# installer: the Commander Core detectors off, and the red/green swap on for
# every zone the config lists in openrgb_rg_swap. It backs the file up before
# changing it, leaves it byte-for-byte alone when nothing needs changing, and
# creates it when OpenRGB has never run -- so OpenRGB's first run starts from
# these settings, whenever it happens.
#
# OpenRGB reads the file once, as it detects devices, and writes the whole of it
# back from memory whenever it saves a setting: an edit made while it runs does
# not apply until it restarts, and can be overwritten before then. So our unit
# is stopped for the edit, and `enable --now` below starts it again. An OpenRGB
# running any other way is reported, not touched.
if systemctl --user is-active --quiet ambience-rgb-openrgb.service; then
    systemctl --user stop ambience-rgb-openrgb.service
    echo "  stopped ambience-rgb-openrgb.service for the edit; it starts again below"
fi
if pgrep -x openrgb >/dev/null; then
    warn "  OpenRGB is running outside ambience-rgb-openrgb.service (pid $(pgrep -x openrgb | tr '\n' ' ')). It reads these settings only when it starts, and can write them back from memory: close it now, and start it again after this."
fi
"$VENV/bin/python" -m ambience_rgb.openrgb_settings "$OPENRGB_CONFIG" \
    || warn "  $OPENRGB_CONFIG was not fully updated; see above. Without it OpenRGB may take the Commander Core, and zones the config swaps back may show red as green."

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
# 3b. Keep ModemManager off the Katana's serial port
#
# The soundbar is driven through Home Assistant now, but plugged in by USB it
# still exposes a CDC ACM control port -- by USB class, a modem -- and an active
# ModemManager probes every such port with AT commands. A soundbar is not a
# modem; the rule tells ModemManager to leave it alone. Nothing in this project
# opens the port any more, so the rule no longer grants anyone access to it.
# --------------------------------------------------------------------------
UDEV_RULE=60-ambience-rgb-katana.rules
if [[ -d /etc/udev/rules.d ]]; then
    sudo install -m 0644 "$REPO_ROOT/udev/$UDEV_RULE" "/etc/udev/rules.d/$UDEV_RULE"
    sudo udevadm control --reload
    # Re-apply to a soundbar that is already plugged in; a new one gets it on arrival.
    sudo udevadm trigger --action=change --subsystem-match=tty --sysname-match='ttyACM*'
    sudo udevadm settle
    echo "  installed /etc/udev/rules.d/$UDEV_RULE"
fi

# --------------------------------------------------------------------------
# 4. Units
# --------------------------------------------------------------------------
# The shutdown unit runs as root, which has neither this user's home nor their
# environment to search -- so it is handed the config file the daemon reads.
# Re-run this script after creating or moving a config.
CONFIG_PATH=$("$VENV/bin/python" -c "from ambience_rgb.config import resolved_config_path; print(resolved_config_path())")
CONFIG_DIR=$(dirname "$CONFIG_PATH")
echo "  config: $CONFIG_PATH"

install_unit() {   # $1 = unit, $2 = dest dir, $3 = "sudo" or empty
    local src="$REPO_ROOT/systemd/$1" dst="$2/$1"
    ${3:+sudo} mkdir -p "$2"
    # Every @PLACEHOLDER@ a unit uses must be filled here; test_unit_files.py
    # fails the build when one is not.
    sed -e "s|@INSTALL_DIR@|$REPO_ROOT|g" \
        -e "s|@VENV_DIR@|$VENV|g" \
        -e "s|@CONFIG_PATH@|$CONFIG_PATH|g" \
        -e "s|@CONFIG_DIR@|$CONFIG_DIR|g" \
        "$src" | ${3:+sudo} tee "$dst" >/dev/null
    echo "  installed $dst"
}

install_unit ambience-rgb-openrgb.service "$USER_UNIT_DIR"
install_unit ambience-rgb.service         "$USER_UNIT_DIR"
systemctl --user daemon-reload
systemctl --user enable --now ambience-rgb-openrgb.service
# Not `enable --now` for the daemon: that starts a stopped unit and leaves a
# running one alone, so a re-run after a pull left the daemon on its old code
# and its old config -- it reads the config once, at start -- against the
# OpenRGB settings just edited above. openrgb_rg_swap and zone_channel_order
# are two halves of one setting, so a daemon left on the old half shows red as
# green on every zone that changed. `restart` starts a stopped unit and
# restarts a running one, as install.ps1 restarts its task; the stop path
# blacks the case and lets go of the hardware before the new daemon opens it.
systemctl --user enable ambience-rgb.service
systemctl --user restart ambience-rgb.service

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
          "$VENV/bin/python" -c "from ambience_rgb.config import load_config; print(load_config().endpoint)"
      )

  then: systemctl --user restart matterlights-sync

  What is driven:  $VENV/bin/python -m ambience_rgb devices
  Logs:            journalctl --user -u ambience-rgb -f
  Off right now:   systemctl --user stop ambience-rgb
EOF
