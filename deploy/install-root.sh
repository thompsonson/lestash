#!/usr/bin/env bash
# The root half of the LeStash install: what the Kobo plug-in sync needs from the
# system. Run once per machine, and again after an update to the udev rule:
#
#   sudo deploy/install-root.sh
#
# Safe to re-run: each step checks first and changes nothing that is already right.
# deploy/install.sh (no sudo) installs the user services and says when this is due.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MOUNT_POINT=/media/kobo
RULE_SRC="$SCRIPT_DIR/99-lestash-kobo.rules"
RULE_DEST=/etc/udev/rules.d/99-lestash-kobo.rules
FSTAB_MATCH='^LABEL=KOBOeReader[[:space:]]+/media/kobo[[:space:]]'
RW_POINT=/media/kobo-rw
RW_MATCH='^LABEL=KOBOeReader[[:space:]]+/media/kobo-rw[[:space:]]'

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this with sudo: sudo $0" >&2
    exit 1
fi
# The Kobo is mounted for the user who runs LeStash, not for root
if [ -z "${SUDO_UID:-}" ] || [ "$SUDO_UID" -eq 0 ]; then
    echo "Run this with sudo from the account that runs LeStash, not as root directly." >&2
    exit 1
fi

# Read-only (ro), mountable by that user without root (user), and never at boot
# (noauto): the plug-in unit mounts it, copies it, and unmounts it
FSTAB_LINE="LABEL=KOBOeReader $MOUNT_POINT vfat ro,noauto,user,nosuid,nodev,noexec,uid=$SUDO_UID,gid=${SUDO_GID:?},umask=077,utf8 0 0"

# Read-write, for writing collections (#315) and kobo-sideload. Only one of the two is
# mounted at a time; the plug-in unit mounts this one only after its snapshot.
RW_LINE="LABEL=KOBOeReader $RW_POINT vfat rw,noauto,user,nosuid,nodev,noexec,uid=$SUDO_UID,gid=${SUDO_GID:?},umask=077,utf8 0 0"

mkdir -p "$MOUNT_POINT" "$RW_POINT"

if grep -qE "$FSTAB_MATCH" /etc/fstab; then
    echo "fstab: a Kobo line is already there; left as it is."
    if ! grep -E "$FSTAB_MATCH" /etc/fstab | awk '{print $4}' | tr ',' '\n' | grep -qx ro; then
        echo "WARNING: that line has no 'ro' option, so the Kobo would be mounted writable." >&2
    fi
else
    cp /etc/fstab /etc/fstab.lestash.bak
    printf '%s\n' "$FSTAB_LINE" >>/etc/fstab
    systemctl daemon-reload
    echo "fstab: added the Kobo line (previous file kept as /etc/fstab.lestash.bak)."
fi

if grep -qE "$RW_MATCH" /etc/fstab; then
    echo "fstab: the Kobo read-write line is already there; left as it is."
else
    [ -e /etc/fstab.lestash.bak ] || cp /etc/fstab /etc/fstab.lestash.bak
    printf '%s\n' "$RW_LINE" >>/etc/fstab
    systemctl daemon-reload
    echo "fstab: added the Kobo read-write line."
fi

if cmp -s "$RULE_SRC" "$RULE_DEST"; then
    echo "udev: the rule is up to date."
else
    install -m 0644 "$RULE_SRC" "$RULE_DEST"
    udevadm control --reload
    echo "udev: installed $RULE_DEST."
fi

echo "Done. Plug in the Kobo and tap Connect to sync it."
