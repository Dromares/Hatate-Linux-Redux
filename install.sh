#!/usr/bin/env bash
# One-time setup: after this, Hatate-linux shows up in your applications
# menu like any other installed program - no more opening a terminal to
# launch it. Run this once with: bash install.sh
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "Setting up Hatate-linux from $DIR"
echo

if [ ! -d "venv" ]; then
    echo "Creating virtual environment and installing dependencies (this may take a minute)..."
    python3 -m venv venv
    "$DIR/venv/bin/pip" install --quiet --upgrade pip
    "$DIR/venv/bin/pip" install --quiet -r requirements.txt
    echo "Dependencies installed."
else
    echo "Virtual environment already exists, skipping setup."
fi

chmod +x "$DIR/run.sh"

DESKTOP_DIR="$HOME/.local/share/applications"
DESKTOP_FILE="$DESKTOP_DIR/hatate-linux.desktop"
mkdir -p "$DESKTOP_DIR"

cat > "$DESKTOP_FILE" << EOF
[Desktop Entry]
Type=Application
Name=Hatate-linux
Comment=IQDB/SauceNAO reverse image search and tagger for Hydrus
Exec="$DIR/run.sh"
Icon=$DIR/resources/icon.svg
Terminal=false
Categories=Graphics;Utility;
StartupWMClass=hatate-linux
EOF

echo "Desktop entry written to $DESKTOP_FILE"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
fi

echo
echo "Done! Hatate-linux should now appear in your applications menu"
echo "(you may need to search for \"Hatate\" or log out/in once for it to show up)."
echo "You can also launch it any time by double-clicking run.sh, or running:"
echo "  $DIR/run.sh"
