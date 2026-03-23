#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.config/systemd/user"

mkdir -p "$DEST"

# Deploy server
cp "$SCRIPT_DIR/lestash-server.service" "$DEST/"

# Deploy sync timer
cp "$SCRIPT_DIR/lestash-sync.service" "$DEST/"
cp "$SCRIPT_DIR/lestash-sync.timer" "$DEST/"

systemctl --user daemon-reload

# Enable and start server
systemctl --user enable --now lestash-server.service

# Enable and start sync timer
systemctl --user enable --now lestash-sync.timer

echo "Deployed services:"
systemctl --user status lestash-server.service --no-pager || true
echo ""
systemctl --user list-timers lestash-sync.timer --no-pager
