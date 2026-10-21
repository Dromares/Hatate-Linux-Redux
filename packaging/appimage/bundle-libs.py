#!/usr/bin/env python3
"""Copies the shared libraries the bundled programs need into AppDir/usr/lib.

    bundle-libs.py APPDIR EXCLUDELIST SOURCES_OUT

Every ELF file under APPDIR is run through ldd; each library that resolves
outside APPDIR is copied (under the name the program asked for) unless it
is excluded. Copies are scanned in turn, so the closure is complete.

What is NOT bundled is the AppImage project's own excludelist (pinned in
pins.sh): glibc and friends, which must match the running kernel and
loader, plus libstdc++, which Mesa drivers loaded from the host need to be
at least as new as the host's. FORCE_BUNDLE then takes back the entries a
machine without a desktop stack would otherwise be missing: Chromium and
Qt refuse to start without them, and the AppImage is meant to run with
nothing else installed.

A library that ldd cannot find is a build failure, not a warning.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Excludelist entries that are bundled anyway. Each is a DT_NEEDED of
# chrome or of Qt's Gui/xcb plugin and has no business being missing.
FORCE_BUNDLE = {
    "libgbm.so.1", "libdrm.so.2",                    # chrome
    "libasound.so.2",                                # chrome
    "libGL.so.1", "libEGL.so.1", "libGLX.so.0",      # Qt Gui; glvnd front ends, vendor
    "libGLdispatch.so.0", "libOpenGL.so.0",          # drivers still come from the host
    "libxcb.so.1", "libX11.so.6", "libX11-xcb.so.1", # Qt xcb, chrome
    "libfontconfig.so.1", "libfreetype.so.6",        # Qt Gui, chrome (via pango/cairo)
    "libharfbuzz.so.0", "libfribidi.so.0",           # pango
    "libexpat.so.1",                                 # chrome, fontconfig
}

# Libraries loaded with dlopen(), which ldd cannot see. Package name ->
# files to take from it.
DLOPENED = {
    # libnss3 loads its softokn/freebl/root-cert modules by path at start-up.
    "libnss3": ["libsoftokn3.so", "libfreeblpriv3.so", "libnssckbi.so",
                "libnssdbm3.so", "libnsssysinit.so"],
}

ELF = b"\x7fELF"


def is_elf(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == ELF
    except OSError:
        return False


def ldd(path: Path):
    proc = subprocess.run(["ldd", str(path)], capture_output=True, text=True)
    for line in proc.stdout.splitlines():
        line = line.strip()
        m = re.match(r"(\S+) => (\S+) \(0x", line)
        if m:
            yield m.group(1), m.group(2)
            continue
        m = re.match(r"(\S+) => not found", line)
        if m:
            yield m.group(1), None


def main() -> None:
    appdir = Path(sys.argv[1]).resolve()
    excludes = {
        ln.split("#")[0].strip()
        for ln in Path(sys.argv[2]).read_text().splitlines()
    } - {""} - FORCE_BUNDLE
    dest = appdir / "usr" / "lib"
    dest.mkdir(parents=True, exist_ok=True)

    # dlopen'd libraries first, so their own dependencies are scanned.
    dlopened = set()
    for pkg, names in DLOPENED.items():
        listing = subprocess.run(["dpkg", "-L", pkg], capture_output=True, text=True).stdout.split()
        for name in names:
            found = [p for p in listing if os.path.basename(p) == name and os.path.isfile(p)]
            if not found:
                sys.exit(f"bundle-libs: {name} not found in package {pkg}")
            shutil.copy2(os.path.realpath(found[0]), dest / name)
            dlopened.add(os.path.realpath(found[0]))

    copied = {}
    skipped = {}
    missing = []
    queue = [p for p in appdir.rglob("*") if p.is_file() and not p.is_symlink() and is_elf(p)]
    seen = set(queue)
    while queue:
        path = queue.pop()
        for soname, target in ldd(path):
            if target is None:
                missing.append((str(path.relative_to(appdir)), soname))
                continue
            real = Path(os.path.realpath(target))
            if appdir in real.parents:
                continue                       # shipped inside the AppDir already
            if soname in excludes or real.name in excludes:
                skipped.setdefault(soname, str(path.relative_to(appdir)))
                continue
            out = dest / soname
            if soname not in copied:
                shutil.copy2(real, out)
                copied[soname] = str(real)
                if out not in seen:
                    seen.add(out)
                    queue.append(out)

    if missing:
        for where, soname in sorted(set(missing)):
            print(f"bundle-libs: NOT FOUND {soname} (needed by {where})", file=sys.stderr)
        sys.exit(1)

    print(f"bundle-libs: copied {len(copied)} libraries into {dest}")
    for soname in sorted(copied):
        print(f"  bundled  {soname:40s} <- {copied[soname]}")
    for soname in sorted(skipped):
        print(f"  host     {soname:40s} (needed by {skipped[soname]})")
    # Real source paths, for the licence step (dpkg -S).
    Path(sys.argv[3]).write_text("\n".join(sorted(set(copied.values()) | dlopened)) + "\n")


if __name__ == "__main__":
    main()
