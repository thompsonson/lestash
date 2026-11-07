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

# Deploy TLS cert renewal timer
cp "$SCRIPT_DIR/lestash-cert.service" "$DEST/"
cp "$SCRIPT_DIR/lestash-cert.timer" "$DEST/"

# Deploy Kobo plug-in sync (started by udev, so nothing to enable)
cp "$SCRIPT_DIR/lestash-kobo.service" "$DEST/"
cp "$SCRIPT_DIR/lestash-kobo-sync@.service" "$DEST/"

systemctl --user daemon-reload

# Enable and start server
systemctl --user enable --now lestash-server.service

# Enable and start sync timer
systemctl --user enable --now lestash-sync.timer

# Enable cert renewal timer
systemctl --user enable --now lestash-cert.timer

echo "Deployed services:"
systemctl --user status lestash-server.service --no-pager || true
echo ""
systemctl --user list-timers lestash-sync.timer lestash-cert.timer --no-pager

# The Kobo plug-in sync also needs an fstab line and a udev rule, which need root.
# install-root.sh does both; say when it is due (missing, or the rule changed).
if ! grep -qE '^LABEL=KOBOeReader[[:space:]]+/media/kobo[[:space:]]' /etc/fstab ||
    ! cmp -s "$SCRIPT_DIR/99-lestash-kobo.rules" /etc/udev/rules.d/99-lestash-kobo.rules; then
    echo ""
    echo "Kobo plug-in sync: run once, with sudo:"
    echo "  sudo $SCRIPT_DIR/install-root.sh"
fi
