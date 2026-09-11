#!/usr/bin/env bash
# Remove ambience-rgb's units and turn the hardware off. Leaves the repo and
# OpenRGB's settings alone: re-enable the Commander Core detector in OpenRGB's
# own settings, and to drop the red/green swaps the config pinned, delete the
# "ASRock Polychrome USB" section from OpenRGB.json with OpenRGB closed -- it
# writes its own defaults at its next start.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

systemctl --user disable --now ambience-rgb.service ambience-rgb-openrgb.service 2>/dev/null || true
rm -f "$USER_UNIT_DIR/ambience-rgb.service" "$USER_UNIT_DIR/ambience-rgb-openrgb.service"
systemctl --user daemon-reload

sudo systemctl disable --now ambience-rgb-lights-off.service 2>/dev/null || true
sudo rm -f /etc/systemd/system/ambience-rgb-lights-off.service
sudo systemctl daemon-reload

"$REPO_ROOT/.venv/bin/python" -m ambience_rgb off 2>/dev/null || true

# After the final `off`, so that it could still reach the soundbar.
sudo rm -f /etc/udev/rules.d/60-ambience-rgb-katana.rules
sudo udevadm control --reload

echo "Removed. OpenRGB's Commander Core detector is still disabled; turn it back on in OpenRGB if you want OpenRGB to see the AIO again. The red/green swaps the config pinned are still on: with OpenRGB closed, delete the 'ASRock Polychrome USB' section from OpenRGB.json to go back to its defaults."
