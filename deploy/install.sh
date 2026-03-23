#!/usr/bin/env bash
set -euo pipefail

SERVICE=lestash-server.service
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.config/systemd/user"

mkdir -p "$DEST"
cp "$SCRIPT_DIR/$SERVICE" "$DEST/$SERVICE"

systemctl --user daemon-reload
systemctl --user enable --now "$SERVICE"

echo "Deployed $SERVICE"
systemctl --user status "$SERVICE" --no-pager
