#!/usr/bin/env bash
# Builds Hatate-x86_64.AppImage inside an Ubuntu 22.04 root filesystem, so
# the glibc floor is 2.35 whatever distribution this runs on.
#
#   packaging/appimage/build.sh
#
# Needs bwrap (bubblewrap) with unprivileged user namespaces, curl and
# sha256sum on the host. No docker, no sudo, no root: the rootfs is
# entered as a fake uid 0 in a user namespace.
#
# Environment:
#   BUILD_DIR   where the rootfs, downloads and output go (default: build/appimage
#               under the repository; git-ignored)
#
# Output, in $BUILD_DIR/out:
#   Hatate-x86_64.AppImage   SHA256SUMS   build-manifest.txt
#
# Every download is pinned to a URL and a SHA-256 in pins.sh and checked
# here before use. The rest of the build (apt, pip, Playwright's browser
# download) happens in inner-build.sh, inside the rootfs.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
BUILD_DIR="${BUILD_DIR:-$ROOT/build/appimage}"
# shellcheck source=pins.sh
. "$HERE/pins.sh"

for tool in bwrap curl sha256sum tar; do
    command -v "$tool" >/dev/null || { echo "build.sh: $tool is required on the host" >&2; exit 1; }
done

DL="$BUILD_DIR/dl"
ROOTFS="$BUILD_DIR/rootfs"
OUT="$BUILD_DIR/out"
mkdir -p "$DL" "$OUT"

# fetch URL SHA256 DEST: download if missing, then refuse anything whose
# hash is not the pinned one. A cached file is re-checked every time.
fetch() {
    local url="$1" sha="$2" dest="$3"
    if [ ! -f "$dest" ]; then
        echo "fetching $url"
        curl -fsSL --retry 3 -o "$dest.part" "$url"
        mv "$dest.part" "$dest"
    fi
    local got
    got="$(sha256sum "$dest" | cut -d' ' -f1)"
    if [ "$got" != "$sha" ]; then
        echo "build.sh: SHA-256 mismatch for $dest" >&2
        echo "  expected $sha" >&2
        echo "  got      $got" >&2
        rm -f "$dest"
        exit 1
    fi
    echo "verified $(basename "$dest") sha256=$sha"
}

fetch "$UBUNTU_BASE_URL"   "$UBUNTU_BASE_SHA256"   "$DL/ubuntu-base.tar.gz"
fetch "$PBS_URL"           "$PBS_SHA256"           "$DL/python-standalone.tar.gz"
fetch "$APPIMAGETOOL_URL"  "$APPIMAGETOOL_SHA256"  "$DL/appimagetool-x86_64.AppImage"
fetch "$RUNTIME_URL"       "$RUNTIME_SHA256"       "$DL/runtime-x86_64"
fetch "$EXCLUDELIST_URL"   "$EXCLUDELIST_SHA256"   "$DL/excludelist"
chmod +x "$DL/appimagetool-x86_64.AppImage"

# bw ARGS...: run a command in the rootfs as fake root. The repository is
# mounted read-only so the build cannot modify the source tree.
bw() {
    bwrap --unshare-user --uid 0 --gid 0 --bind "$ROOTFS" / --proc /proc --dev /dev \
        --ro-bind /etc/resolv.conf /etc/resolv.conf \
        --tmpfs /tmp --tmpfs /var/tmp \
        --ro-bind "$ROOT" /src --ro-bind "$DL" /dl --bind "$OUT" /out \
        --setenv HOME /root --setenv TMPDIR /tmp --setenv LANG C.UTF-8 \
        --setenv PATH /usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        --setenv DEBIAN_FRONTEND noninteractive \
        --setenv APPIMAGE_EXTRACT_AND_RUN 1 \
        --chdir / "$@"
}

if [ ! -f "$ROOTFS/.hatate-rootfs-ok" ]; then
    echo "creating the Ubuntu 22.04 rootfs in $ROOTFS"
    rm -rf "$ROOTFS"
    mkdir -p "$ROOTFS"
    # dev/ is populated by bwrap; extracting its device nodes needs root.
    tar -xzf "$DL/ubuntu-base.tar.gz" -C "$ROOTFS" --exclude='dev/*'
    # The tarball's dirs can be 0555; the fake root still needs to write.
    chmod -R u+rwX "$ROOTFS"
    mkdir -p "$ROOTFS/src" "$ROOTFS/dl" "$ROOTFS/out" "$ROOTFS/tmp"
    # Debian/Ubuntu apt drops privileges to _apt; there is no such uid in
    # a one-uid user namespace, so it is told to stay root.
    echo 'APT::Sandbox::User "root";' > "$ROOTFS/etc/apt/apt.conf.d/99-hatate-build"
    # fontconfig-config's postinst chowns this to root:staff when it creates it,
    # and gid 50 does not exist in a one-uid user namespace (chown: Invalid
    # argument, then half the library packages stay unconfigured). If the
    # directory is already there it skips the chown.
    mkdir -p "$ROOTFS/usr/local/share/fonts"
    chmod 2775 "$ROOTFS/usr/local/share/fonts"
    touch "$ROOTFS/.hatate-rootfs-ok"
fi

bw bash /src/packaging/appimage/inner-build.sh

echo
echo "built: $OUT/Hatate-x86_64.AppImage"
ls -l "$OUT"
