#!/usr/bin/env bash
# Remove linux-rgb's units and turn the hardware off. Leaves the repo and the
# OpenRGB detector change alone -- re-enable that in OpenRGB's own settings.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

systemctl --user disable --now linux-rgb.service linux-rgb-openrgb.service 2>/dev/null || true
rm -f "$USER_UNIT_DIR/linux-rgb.service" "$USER_UNIT_DIR/linux-rgb-openrgb.service"
systemctl --user daemon-reload

sudo systemctl disable --now linux-rgb-lights-off.service 2>/dev/null || true
sudo rm -f /etc/systemd/system/linux-rgb-lights-off.service
sudo systemctl daemon-reload

"$REPO_ROOT/.venv/bin/python" -m linux_rgb off 2>/dev/null || true
echo "Removed. OpenRGB's Commander Core detector is still disabled; turn it back on in OpenRGB if you want OpenRGB to see the AIO again."
