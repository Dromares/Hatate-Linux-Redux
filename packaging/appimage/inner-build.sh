#!/usr/bin/env bash
# Runs INSIDE the Ubuntu 22.04 rootfs, as fake root (see build.sh). The
# repository is at /src (read-only), the pinned downloads at /dl, and the
# result goes to /out.
set -euo pipefail

HERE=/src/packaging/appimage
# shellcheck source=pins.sh
. "$HERE/pins.sh"

APPDIR=/work/AppDir
PY="$APPDIR/usr/python/bin/python3.13"
SHARE="$APPDIR/usr/share/hatate"
MANIFEST="$SHARE/build-manifest.txt"
export PLAYWRIGHT_BROWSERS_PATH="$APPDIR/usr/lib/ms-playwright"

step() { echo; echo "==> $*"; }

# ---------------------------------------------------------------- packages
step "system packages (apt)"
# universe is only needed for the `brotli` tool that reads Chromium's credits.
echo 'deb http://archive.ubuntu.com/ubuntu/ jammy universe' > /etc/apt/sources.list.d/universe.list
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    ca-certificates curl squashfs-tools desktop-file-utils binutils file xz-utils unzip brotli \
    libasound2 libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 libcairo2 libcups2 libdbus-1-3 \
    libdrm2 libgbm1 libglib2.0-0 libnspr4 libnss3 libpango-1.0-0 libx11-6 libxcb1 \
    libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2 \
    libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 \
    libxcb-render-util0 libxcb-shape0 libxcb-sync1 libxcb-xfixes0 libxcb-xinerama0 \
    libxcb-xkb1 libxkbcommon-x11-0 libwayland-client0 libwayland-cursor0 libwayland-egl1 libfontconfig1 libgl1 libegl1 libopengl0 \
    fonts-liberation fonts-dejavu-core fonts-freefont-ttf
dpkg -l | awk '/^ii/ {print $2" "$3}' > /out/rootfs-packages.txt

# ------------------------------------------------------------------ python
step "CPython (python-build-standalone, pinned)"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr" "$SHARE"
tar -xzf /dl/python-standalone.tar.gz -C "$APPDIR/usr"       # -> usr/python
"$PY" --version

step "Python packages (exact pins, hash-checked, wheels only)"
"$PY" -m pip install --quiet --no-cache-dir --no-compile --root-user-action=ignore \
    --require-hashes --only-binary=:all: -r "$HERE/requirements-appimage.txt"
# Console scripts carry the build path in their shebang and are not used.
find "$APPDIR/usr/python/bin" -type f ! -name 'python3*' -delete
find "$APPDIR/usr/python/bin" -type l ! -name 'python3*' -delete
SITE="$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
rm -rf "$SITE"/pip "$SITE"/pip-*.dist-info "$SITE"/setuptools* "$SITE"/_distutils_hack "$SITE"/pkg_resources 2>/dev/null || true

step "pruning the Qt wheels to what Hatate imports"
"$PY" "$HERE/prune-qt.py" "$SITE"

# ---------------------------------------------------------------- chromium
step "Playwright's Chromium"
REV="$("$PY" - <<'PYEOF'
import json, sysconfig, pathlib
p = pathlib.Path(sysconfig.get_paths()["purelib"]) / "playwright/driver/package/browsers.json"
b = {x["name"]: x for x in json.loads(p.read_text())["browsers"]}["chromium"]
print(b["revision"], b["browserVersion"])
PYEOF
)"
CHROMIUM_REV="${REV% *}"; CHROMIUM_VERSION="${REV#* }"
echo "chromium revision $CHROMIUM_REV ($CHROMIUM_VERSION)"
# --no-shell: the headless shell has no window, and Lens needs a headed browser.
"$PY" -m playwright install --no-shell chromium
rm -rf "$PLAYWRIGHT_BROWSERS_PATH"/ffmpeg-* "$PLAYWRIGHT_BROWSERS_PATH"/.links   # video recording only
CHROME_DIR="$PLAYWRIGHT_BROWSERS_PATH/chromium-$CHROMIUM_REV/chrome-linux64"
test -x "$CHROME_DIR/chrome"

# Record the archive Playwright just unpacked: the same URL, fetched again,
# and compared with the pinned hash.
CHROMIUM_URL="https://cdn.playwright.dev/builds/cft/$CHROMIUM_VERSION/linux64/chrome-linux64.zip"
curl -fsSL --retry 3 -o /tmp/chrome-linux64.zip "$CHROMIUM_URL"
CHROMIUM_ZIP_SHA256_GOT="$(sha256sum /tmp/chrome-linux64.zip | cut -d' ' -f1)"
echo "chromium archive $CHROMIUM_URL"
echo "chromium archive sha256 $CHROMIUM_ZIP_SHA256_GOT (pinned: ${CHROMIUM_ZIP_SHA256})"
if [ "$CHROMIUM_ZIP_SHA256_GOT" != "$CHROMIUM_ZIP_SHA256" ]; then
    echo "inner-build: Chromium archive does not match the pinned SHA-256" >&2
    exit 1
fi
# ... and the binary Playwright installed must be the one in that archive.
INSTALLED_SHA="$(sha256sum "$CHROME_DIR/chrome" | cut -d' ' -f1)"
ARCHIVE_SHA="$(unzip -p /tmp/chrome-linux64.zip chrome-linux64/chrome | sha256sum | cut -d' ' -f1)"
echo "chrome binary sha256 $INSTALLED_SHA (archive: $ARCHIVE_SHA)"
[ "$INSTALLED_SHA" = "$ARCHIVE_SHA" ] || { echo "inner-build: installed chrome differs from the pinned archive" >&2; exit 1; }
rm -f /tmp/chrome-linux64.zip
"$PY" "$HERE/extract-credits.py" "$CHROME_DIR" "$APPDIR/chromium-credits.html"

# ---------------------------------------------------------------- the app
step "Hatate sources"
mkdir -p "$SHARE"
cp -r /src/main.py /src/core /src/gui /src/workers /src/resources "$SHARE/"
find "$SHARE" -name __pycache__ -prune -exec rm -rf {} +
HATATE_VERSION="$("$PY" -c "import sys; sys.path.insert(0, '$SHARE'); from core.version import __version__; print(__version__)")"
echo "version $HATATE_VERSION"
# Byte-compile now: the image is read-only, so nothing could cache it later.
"$PY" -m compileall -q -j 0 "$APPDIR/usr/python/lib" "$SHARE"

# ------------------------------------------------------------------- fonts
step "fonts for a machine that has none"
mkdir -p "$APPDIR/usr/share/fonts/hatate"
cp /usr/share/fonts/truetype/dejavu/DejaVu*.ttf /usr/share/fonts/truetype/liberation/Liberation*.ttf \
    "$APPDIR/usr/share/fonts/hatate/"
# FreeSerif is here for U+29C9 (the compare/copy button glyph), which neither
# DejaVu nor Liberation has.
cp /usr/share/fonts/truetype/freefont/FreeSerif.ttf "$APPDIR/usr/share/fonts/hatate/"
# fonts.conf is written by AppRun at launch: it needs the absolute mount path
# (the image's fontconfig 2.13 does not resolve a relative <dir>).

# --------------------------------------------------------------- libraries
step "shared libraries"
"$PY" "$HERE/bundle-libs.py" "$APPDIR" /dl/excludelist /tmp/bundled-sources.txt | tee /out/bundle-libs.log

# --------------------------------------------------------------- AppDir files
step "AppDir files"
install -m 0755 "$HERE/AppRun" "$APPDIR/AppRun"
install -m 0644 "$HERE/hatate.desktop" "$APPDIR/hatate.desktop"
echo "X-AppImage-Version=$HATATE_VERSION" >> "$APPDIR/hatate.desktop"
desktop-file-validate "$APPDIR/hatate.desktop"
cp /src/resources/icon.svg "$APPDIR/hatate.svg"
mkdir -p "$APPDIR/usr/share/icons/hicolor/scalable/apps" "$APPDIR/usr/share/applications" "$APPDIR/usr/bin"
cp /src/resources/icon.svg "$APPDIR/usr/share/icons/hicolor/scalable/apps/hatate.svg"
cp "$APPDIR/hatate.desktop" "$APPDIR/usr/share/applications/hatate.desktop"
printf '#!/bin/sh\nexec "$(dirname "$(readlink -f "$0")")/../../AppRun" "$@"\n' > "$APPDIR/usr/bin/hatate"
chmod +x "$APPDIR/usr/bin/hatate"
ln -sf hatate.svg "$APPDIR/.DirIcon"

# ---------------------------------------------------------------- licenses
step "licenses"
"$PY" "$HERE/collect-licenses.py" "$APPDIR" /src /tmp/bundled-sources.txt fonts-liberation fonts-dejavu-core fonts-freefont-ttf
cp "$HERE/NOTICE" "$APPDIR/usr/share/licenses/NOTICE"
cp "$HERE/NOTICE" "$APPDIR/NOTICE"

# ----------------------------------------------------------- glibc floor
step "glibc floor"
FLOOR="$(find "$APPDIR" -type f \( -perm -u+x -o -name '*.so*' \) -print0 \
    | xargs -0 -r file -L 2>/dev/null | grep -F 'ELF 64-bit' | cut -d: -f1 \
    | xargs -r objdump -T 2>/dev/null | grep -o 'GLIBC_[0-9][0-9.]*' | sort -V -u | tail -1)"
echo "highest GLIBC symbol version required by anything in the image: $FLOOR"
if [ "$(printf '%s\n' "$FLOOR" "GLIBC_2.35" | sort -V | tail -1)" != "GLIBC_2.35" ]; then
    echo "inner-build: image needs $FLOOR, above the documented 2.35 floor" >&2
    exit 1
fi

# --------------------------------------------------- smoke test in the rootfs
step "smoke test inside the rootfs"
(
    unset LD_LIBRARY_PATH
    export HOME=/tmp/smoke-home XDG_CONFIG_HOME=/tmp/smoke-home/c XDG_CACHE_HOME=/tmp/smoke-home/k
    mkdir -p "$HOME"
    APPDIR="$APPDIR" "$APPDIR/AppRun" --version
    HATATE_APPIMAGE=1 LD_LIBRARY_PATH="$APPDIR/usr/lib" "$PY" -I -c "
import PyQt6.QtCore, playwright, lxml.etree, numpy, PIL.Image, requests, bs4
print('imports ok')"
)

# ---------------------------------------------------------------- manifest
step "manifest"
{
    echo "Hatate $HATATE_VERSION AppImage build manifest"
    echo "built (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "rootfs: $(. /etc/os-release; echo "$PRETTY_NAME") glibc $(ldd --version | head -1 | awk '{print $NF}'), image needs at most $FLOOR"
    echo "python: $("$PY" --version) ($PBS_URL sha256 $PBS_SHA256)"
    echo "playwright: $PLAYWRIGHT_VERSION"
    echo "chromium: revision $CHROMIUM_REV, Chrome for Testing $CHROMIUM_VERSION"
    echo "chromium archive: $CHROMIUM_URL sha256 $CHROMIUM_ZIP_SHA256_GOT"
    echo "appimagetool: $APPIMAGETOOL_URL sha256 $APPIMAGETOOL_SHA256"
    echo "type2 runtime: $RUNTIME_URL sha256 $RUNTIME_SHA256"
    echo "excludelist: $EXCLUDELIST_URL sha256 $EXCLUDELIST_SHA256"
    echo
    echo "installed Python distributions:"
    "$PY" -m pip --version >/dev/null 2>&1 || true
    "$PY" -c "
import importlib.metadata as m
for d in sorted(m.distributions(), key=lambda d: d.metadata['Name'].lower()):
    print('  %s==%s' % (d.metadata['Name'], d.version))"
    echo
    echo "bundled system libraries (Debian packages):"
    xargs -a "$SHARE/bundled-packages.txt" -r dpkg-query -W -f '  ${Package} ${Version}\n'
} > "$MANIFEST"

# ---------------------------------------------------------------- squashfs
step "AppImage"
rm -f /out/Hatate-x86_64.AppImage
ARCH=x86_64 /dl/appimagetool-x86_64.AppImage --appimage-extract-and-run \
    --comp zstd --mksquashfs-opt -Xcompression-level --mksquashfs-opt 19 \
    --runtime-file /dl/runtime-x86_64 --no-appstream \
    "$APPDIR" /out/Hatate-x86_64.AppImage
chmod +x /out/Hatate-x86_64.AppImage
(cd /out && sha256sum Hatate-x86_64.AppImage > SHA256SUMS)
cp "$MANIFEST" /out/build-manifest.txt
ls -l /out/Hatate-x86_64.AppImage
cat /out/SHA256SUMS
