#!/bin/bash
# Font smoke test for a built AppImage (DAN-1937 F1): under AppRun's own
# environment, on a machine with no system fonts at all, the image's
# fontconfig must load the fonts bundled in the image.
#
#   smoke-fonts.sh Hatate-x86_64.AppImage
#
# It extracts the image into a fresh directory, runs AppRun under bwrap with
# /usr/share/fonts hidden and a throwaway HOME, with fonts-probe.py bound over
# main.py, and prints fonts-probe.py's report. Exit 1 if no font was loaded
# from the image.
set -euo pipefail
IMAGE="$(readlink -f "${1:?usage: smoke-fonts.sh <AppImage>}")"
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

(cd "$WORK" && "$IMAGE" --appimage-extract >/dev/null)
APPDIR="$WORK/squashfs-root"
mkdir -p "$WORK/home" "$WORK/run"
chmod 700 "$WORK/run"

OUT="$(bwrap --unshare-user --ro-bind / / --dev /dev --proc /proc \
    --tmpfs /usr/share/fonts \
    --ro-bind "$HERE/fonts-probe.py" "$APPDIR/usr/share/hatate/main.py" \
    --bind "$WORK/home" "$WORK/home" --bind "$WORK/run" "$WORK/run" \
    --setenv HOME "$WORK/home" --setenv XDG_CACHE_HOME "$WORK/home/k" \
    --setenv XDG_RUNTIME_DIR "$WORK/run" --unsetenv XDG_DATA_HOME --unsetenv FONTCONFIG_FILE \
    "$APPDIR/AppRun")"
echo "$OUT"
n="$(sed -n 's/^from-image=//p' <<<"$OUT")"
[ "${n:-0}" -gt 0 ]
