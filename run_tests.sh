#!/usr/bin/env bash
# Runs the test suite. Prefers an already-activated virtualenv
# ($VIRTUAL_ENV), then the project's own venv/ (created by install.sh),
# otherwise falls back to the system python3.
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# DAN-890: resolving these from bare PATH alone breaks whenever something
# between the caller's venv activation and this script's own exec resets
# PATH without touching VIRTUAL_ENV (e.g. BASH_ENV in a non-interactive
# shell - see scripts/ci_local_matrix.sh, which sources an activate script
# and then shells out to `./run_tests.sh`, a separate bash process). Check
# VIRTUAL_ENV explicitly so the activated interpreter always wins.
PY="python3"
if [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python3" ]; then
    PY="$VIRTUAL_ENV/bin/python3"
elif [ -x "$DIR/venv/bin/python3" ]; then
    PY="$DIR/venv/bin/python3"
fi

# Qt needs a platform plugin; "offscreen" lets the GUI smoke tests run
# on a headless machine (CI, SSH session) with no display attached.
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"

# Lint first, with the same gate CI uses: a ruff failure is the one thing
# CI rejects that the tests themselves can't catch. Skipped with a note,
# not an error, when the dev tooling isn't installed - see
# requirements-dev.txt. SKIP_LINT=1 skips it deliberately.
RUFF=""
if [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/ruff" ]; then
    RUFF="$VIRTUAL_ENV/bin/ruff"
elif [ -x "$DIR/venv/bin/ruff" ]; then
    RUFF="$DIR/venv/bin/ruff"
fi
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
