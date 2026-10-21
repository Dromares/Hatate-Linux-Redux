#!/usr/bin/env python3
"""Strips the PyQt6 wheels down to what Hatate imports.

    prune-qt.py SITE_PACKAGES

The PyQt6 and PyQt6-Qt6 wheels ship ~40 Python modules, QML, 3D, WebEngine,
multimedia, SQL and text-to-speech plugins - about 250 MB Hatate never
loads (it imports QtCore, QtGui and QtWidgets, plus the SVG icon engine
for resources/icon.svg). Left in, they would also drag in dozens of system
libraries (pulseaudio, speech-dispatcher, MySQL, ...) that bundle-libs
would then have to chase. Anything not listed here is deleted, and then
every library nothing refers to any more.
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

KEEP_MODULES = {"QtCore", "QtGui", "QtWidgets", "QtDBus", "QtSvg"}
KEEP_PLUGINS = {
    # X11 and Wayland sessions; offscreen is what the GUI test-suite and
    # headless smoke runs use. No eglfs/linuxfb/vnc (kiosk targets).
    "platforms": {"libqxcb.so", "libqwayland.so", "libqoffscreen.so", "libqminimal.so"},
    # xdg-desktop-portal theme only: the GTK3 one needs libgtk-3.
    "platformthemes": {"libqxdgdesktopportal.so"},
    "platforminputcontexts": None,       # None = whole directory
    "imageformats": {"libqgif.so", "libqico.so", "libqicns.so", "libqjpeg.so", "libqsvg.so",
                     "libqtga.so", "libqtiff.so", "libqwbmp.so", "libqwebp.so"},
    "iconengines": None,
    "xcbglintegrations": None,
    "wayland-decoration-client": None,
    "wayland-graphics-integration-client": {"libqt-plugin-wayland-egl.so", "libshm-emulation-server.so"},
    "wayland-shell-integration": {"libxdg-shell.so"},
}


def needed(path: Path):
    out = subprocess.run(["readelf", "-d", str(path)], capture_output=True, text=True).stdout
    return re.findall(r"Shared library: \[([^\]]+)\]", out)


def main() -> None:
    site = Path(sys.argv[1])
    pyqt = site / "PyQt6"
    qt = pyqt / "Qt6"
    before = subprocess.run(["du", "-sk", str(pyqt)], capture_output=True, text=True).stdout.split()[0]

    for child in list(pyqt.iterdir()):
        name = child.name
        if name in ("__init__.py", "sip.cpython-313-x86_64-linux-gnu.so", "Qt6"):
            continue
        m = re.match(r"(Qt[A-Za-z0-9]+)\.(abi3\.so|pyi)$", name)
        if m and m.group(1) in KEEP_MODULES and name.endswith(".abi3.so"):
            continue
        if m and m.group(1) in KEEP_MODULES:
            continue                         # its .pyi stub is tiny and harmless
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()

    for sub in ("qml", "translations", "libexec", "metatypes", "modules", "resources", "qsci"):
        shutil.rmtree(qt / sub, ignore_errors=True)

    plugins = qt / "plugins"
    for category in list(plugins.iterdir()):
        keep = KEEP_PLUGINS.get(category.name, set())
        if keep is None:
            continue
        if not keep:
            shutil.rmtree(category)
            continue
        for plugin in category.iterdir():
            if plugin.name not in keep:
                plugin.unlink()

    # Libraries in Qt6/lib that nothing refers to any more (Qt's FFmpeg, ...): repeat until none are left.
    lib = qt / "lib"
    while True:
        roots = [p for p in site.rglob("*") if p.is_file() and not p.is_symlink()
                 and (p.suffix == ".so" or ".so." in p.name) and lib not in p.parents]
        wanted = set()
        for p in roots:
            wanted.update(needed(p))
        # libraries in lib/ that other libraries in lib/ need
        changed = True
        while changed:
            changed = False
            for p in lib.glob("*.so*"):
                if p.name in wanted:
                    for dep in needed(p):
                        if dep not in wanted:
                            wanted.add(dep)
                            changed = True
        doomed = [p for p in lib.glob("*.so*") if p.name not in wanted]
        if not doomed:
            break
        for p in doomed:
            p.unlink()

    after = subprocess.run(["du", "-sk", str(pyqt)], capture_output=True, text=True).stdout.split()[0]
    kept = sorted(p.name for p in lib.glob("libQt6*.so*"))
    print(f"prune-qt: PyQt6 {int(before) // 1024} MB -> {int(after) // 1024} MB; Qt libraries kept: {len(kept)}")
    print("  " + " ".join(kept))


if __name__ == "__main__":
    main()
