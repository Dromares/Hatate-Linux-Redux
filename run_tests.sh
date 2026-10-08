#!/usr/bin/env bash
# Runs the test suite. Uses the project's venv if one exists (created by
# install.sh), otherwise falls back to the system python3.
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

PY="python3"
[ -x "$DIR/venv/bin/python3" ] && PY="$DIR/venv/bin/python3"

# Qt needs a platform plugin; "offscreen" lets the GUI smoke tests run
# on a headless machine (CI, SSH session) with no display attached.
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"

# Lint first, with the same gate CI uses: a ruff failure is the one thing
# CI rejects that the tests themselves can't catch. Skipped with a note,
# not an error, when the dev tooling isn't installed - see
# requirements-dev.txt. SKIP_LINT=1 skips it deliberately.
RUFF=""
[ -x "$DIR/venv/bin/ruff" ] && RUFF="$DIR/venv/bin/ruff"
[ -z "$RUFF" ] && command -v ruff >/dev/null 2>&1 && RUFF="ruff"
if [ -n "${SKIP_LINT:-}" ]; then
    :
elif [ -n "$RUFF" ]; then
    echo "Linting with $RUFF"
    "$RUFF" check .
    echo
else
    echo "ruff not installed - skipping lint (pip install -r requirements-dev.txt)"
    echo
fi

echo "Running tests with $PY (QT_QPA_PLATFORM=$QT_QPA_PLATFORM)"
echo
exec "$PY" -m unittest discover -s tests -t . -v "$@"
