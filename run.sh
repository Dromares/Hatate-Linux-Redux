#!/usr/bin/env bash
# Launches Hatate-linux without needing to open a terminal by hand each
# time. Works no matter where this folder is placed - it locates itself,
# and installs a virtual environment automatically on first run if one
# doesn't exist yet. Used both for direct double-click launching and as
# the Exec target of the desktop menu entry created by install.sh.
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if [ ! -d "venv" ]; then
    echo "First run: setting up a Python virtual environment in $DIR/venv ..."
    python3 -m venv venv
    "$DIR/venv/bin/pip" install --quiet --upgrade pip
    "$DIR/venv/bin/pip" install --quiet -r requirements.txt
fi

exec "$DIR/venv/bin/python3" "$DIR/main.py" "$@"
