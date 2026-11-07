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
cp "$SCRIPT_DIR/lestash-kobo-sync.service" "$DEST/"

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

# The Kobo trigger needs two root steps, once per machine; this script doesn't run them
KOBO_FSTAB='LABEL=KOBOeReader /media/kobo vfat ro,noauto,user,nosuid,nodev,noexec,uid=1000,gid=1000,umask=077,utf8 0 0'
if ! grep -qF "$KOBO_FSTAB" /etc/fstab || [ ! -f /etc/udev/rules.d/99-lestash-kobo.rules ]; then
    echo ""
    echo "Kobo plug-in sync needs, once, as root:"
    grep -qF "$KOBO_FSTAB" /etc/fstab || {
        echo "  sudo mkdir -p /media/kobo"
        echo "  echo '$KOBO_FSTAB' | sudo tee -a /etc/fstab && sudo systemctl daemon-reload"
    }
    [ -f /etc/udev/rules.d/99-lestash-kobo.rules ] ||
        echo "  sudo cp $SCRIPT_DIR/99-lestash-kobo.rules /etc/udev/rules.d/ && sudo udevadm control --reload"
fi
