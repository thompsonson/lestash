#!/usr/bin/env bash
set -euo pipefail

REPO="thompsonson/lestash"
RELEASE="dev-desktop"

# Allow override: ./install-desktop.sh v1.12.0
if [ "${1:-}" != "" ]; then
  RELEASE="$1"
fi

TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

echo "LeStash Desktop Installer"
echo "========================="
echo "Release: $RELEASE"

# Check gh CLI
if ! command -v gh >/dev/null 2>&1; then
  echo "Error: gh CLI required. Install from https://cli.github.com/"
  exit 1
fi

OS=$(uname -s)
case "$OS" in
  Darwin)
    echo "Detected: macOS"
    echo "Downloading latest .dmg..."
    gh release download "$RELEASE" --repo "$REPO" --pattern "*.dmg" --dir "$TMPDIR" --clobber

    DMG=$(ls "$TMPDIR"/*.dmg 2>/dev/null | head -1)
    if [ -z "$DMG" ]; then
      echo "Error: No .dmg found in release"
      exit 1
    fi

    echo "Mounting $(basename "$DMG")..."
    hdiutil attach "$DMG" -nobrowse -quiet

    VOLUME=$(ls -d /Volumes/lestash* 2>/dev/null | head -1)
    if [ -z "$VOLUME" ]; then
      echo "Error: Could not find mounted volume"
      exit 1
    fi

    echo "Installing to /Applications..."
    rm -rf /Applications/lestash.app
    cp -R "$VOLUME/lestash.app" /Applications/
    hdiutil detach "$VOLUME" -quiet

    echo ""
    echo "Installed: /Applications/lestash.app"
    echo "Run:       open /Applications/lestash.app"
    ;;

  Linux)
    echo "Detected: Linux"
    if command -v dpkg >/dev/null 2>&1; then
      echo "Downloading latest .deb..."
      gh release download "$RELEASE" --repo "$REPO" --pattern "*.deb" --dir "$TMPDIR" --clobber

      DEB=$(ls "$TMPDIR"/*.deb 2>/dev/null | head -1)
      if [ -z "$DEB" ]; then
        echo "Error: No .deb found in release"
        exit 1
      fi

      echo "Installing $(basename "$DEB")..."
      sudo dpkg -i "$DEB"
      echo ""
      echo "Installed. Run: lestash-app"
    else
      echo "Downloading latest .AppImage..."
      gh release download "$RELEASE" --repo "$REPO" --pattern "*.AppImage" --dir "$TMPDIR" --clobber

      APPIMAGE=$(ls "$TMPDIR"/*.AppImage 2>/dev/null | head -1)
      if [ -z "$APPIMAGE" ]; then
        echo "Error: No .AppImage found in release"
        exit 1
      fi

      DEST="$HOME/.local/bin/lestash.AppImage"
      mkdir -p "$(dirname "$DEST")"
      cp "$APPIMAGE" "$DEST"
      chmod +x "$DEST"
      echo ""
      echo "Installed: $DEST"
      echo "Run:       $DEST"
    fi
    ;;

  *)
    echo "Unsupported OS: $OS"
    exit 1
    ;;
esac

echo "Done!"
