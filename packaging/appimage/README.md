# Hatate AppImage

`build.sh` produces **`Hatate-x86_64.AppImage`**: Hatate with its own
CPython 3.13, PyQt6, Playwright and Playwright's headed Chromium, so a
Google Lens search works with nothing else installed. For the release
procedure see [`../../RELEASING.md`](../../RELEASING.md).

## Building

```bash
BUILD_DIR=/somewhere/with/3GB packaging/appimage/build.sh
```

Needs `bwrap` (user namespaces), `curl`, `sha256sum` and `tar`. No docker,
no root, no sudo. Output goes to `$BUILD_DIR/out/` (default `build/appimage`,
git-ignored): the AppImage, `SHA256SUMS`, `build-manifest.txt`,
`bundle-libs.log`. A first build takes a few minutes (about 1 GB of downloads
in total); the rootfs is reused afterwards.

The build runs in an **Ubuntu 22.04 root filesystem** entered with `bwrap`
as a fake uid 0, so the result needs **glibc 2.35 or newer** and nothing newer.
`inner-build.sh` measures this: it reads the highest `GLIBC_x.y` symbol
version of every ELF file in the image and fails above 2.35.

| File | What it does |
|------|--------------|
| `build.sh` | host side: downloads and hash-checks the pinned inputs, makes the rootfs, enters it |
| `pins.sh` | every pinned URL and SHA-256 (rootfs, python-build-standalone, appimagetool, type-2 runtime, the AppImage excludelist, Chromium's archive) and the Playwright version |
| `requirements-appimage.txt` | exact versions **and hashes** of every wheel installed (`make-lock.py` regenerates it); pip runs with `--require-hashes --only-binary=:all:` |
| `inner-build.sh` | in the rootfs: apt libraries, CPython, wheels, Chromium, the app, fonts, libraries, licences, the glibc check, a smoke test, `appimagetool` |
| `prune-qt.py` | removes the ~170 MB of Qt modules, QML and plugins Hatate never loads |
| `bundle-libs.py` | copies the shared libraries chrome, Qt and Python need (the AppImage excludelist, less a short forced-bundle list, stays on the host) |
| `extract-credits.py` | pulls Chromium's `chrome://credits` page out of `resources.pak` |
| `collect-licenses.py` | puts every component's licence text under `usr/share/licenses/`, and fails if one has none |
| `AppRun`, `hatate.desktop`, `NOTICE`, `licenses/` | files shipped in the image |

Nothing is downloaded unpinned: the rootfs tarball, python-build-standalone,
appimagetool (release `1.9.1`), the type-2 runtime (`20251108`) and the
excludelist (a fixed commit) each have a URL and a SHA-256 in `pins.sh`, which
`build.sh` checks before use; the Chromium archive Playwright fetches is
fetched again and compared with its pinned hash, and the installed `chrome`
binary is compared with the one inside that archive. The apt packages the
libraries come from are Ubuntu 22.04's current ones; their exact versions are
recorded in `build-manifest.txt`.

## What is in the image

```
AppRun                       sets HATATE_APPIMAGE=1, PLAYWRIGHT_BROWSERS_PATH, LD_LIBRARY_PATH
usr/python/                  python-build-standalone CPython 3.13 + site-packages (no pip)
usr/share/hatate/            main.py core/ gui/ workers/ resources/ + build-manifest.txt
usr/lib/ms-playwright/       chromium-<rev>/chrome-linux64 (Chrome for Testing, headed)
usr/lib/                     bundled shared libraries (nss, atk, at-spi, cups, gbm, xkbcommon, xcb-cursor, ...)
usr/share/licenses/          licence texts of everything above (INDEX.txt)
NOTICE                       the GPLv3-as-a-whole statement and the component table
```

`mcp` is deliberately not bundled. The image is read-only: nothing writes
inside `$APPDIR`, all data lives in the XDG directories `core/paths.py` picks.

## Testing

`tests/test_appimage_lens.py` covers the AppImage-specific behaviour of
`core/lens_browser.py` (available with the bundled browsers, never offers
`pip install playwright` / `playwright install` there, source install
unchanged) and `--version`. `tests/test_appimage_packaging.py` checks the pins
and the files that ship. To exercise a built image, run it with throw-away
`HOME`, `XDG_CONFIG_HOME` and `XDG_CACHE_HOME` and no Hydrus configured.
