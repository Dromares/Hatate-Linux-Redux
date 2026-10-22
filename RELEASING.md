# Releasing the Hatate AppImage

A release is one file, `Hatate-x86_64.AppImage`, plus `SHA256SUMS`. It is
built from a **tagged `main` commit** in the Ubuntu 22.04 root filesystem
(that is what fixes the glibc floor at 2.35), uploaded as a **draft**,
checked by someone other than the person who built it, and only then
published. Never publish first and check afterwards.

Roles follow the repository's usual split: the **author** builds and
uploads the draft, a **non-author** checks it, and the **publish
executor** publishes. Nothing is published, tagged or made public without
the board's say-so on the release ticket.

## 1. Before building

1. `main` is green (lint, mypy, `test (3.10)`, `test (3.13)`).
2. The version is changed in one place, `core/version.py`
   (`__version__`), and nowhere else. `python3 main.py --version` must
   print `Hatate <version>`.
3. Re-run the status-claim sweep over the **whole tree** and fix every
   stale hit before tagging:

   ```bash
   git grep -n -i -E 'appimage|playwright install|not installed by default|<old version>'
   ```

4. Pins in `packaging/appimage/pins.sh` are current and every one still
   verifies (the build refuses a file whose SHA-256 differs). To change
   Playwright, edit `PLAYWRIGHT_VERSION`, `CHROMIUM_ZIP_SHA256` (the
   build log prints the archive's hash and the pinned one) and
   regenerate the wheel pins with
   `python3 packaging/appimage/make-lock.py > packaging/appimage/requirements-appimage.txt`.

## 2. Build

From a clean clone of the commit that will be tagged:

```bash
git clone https://github.com/Dromares/Hatate-Linux-Redux && cd Hatate-Linux-Redux
git checkout <the tagged main commit>
BUILD_DIR=/path/with/3GB/free packaging/appimage/build.sh 2>&1 | tee build.log
```

Needs `bwrap` (bubblewrap) with user namespaces, `curl` and `sha256sum`;
no docker, root or sudo. It writes `Hatate-x86_64.AppImage`,
`SHA256SUMS` and `build-manifest.txt` to `$BUILD_DIR/out/`. Keep
`build.log`: it records every pinned download's hash, the Chromium
revision and archive SHA-256, and the highest glibc symbol version in the
image (it must be 2.35 or lower; the build fails otherwise).

Smoke-test the result **on your own machine** before uploading:

```bash
./Hatate-x86_64.AppImage --version      # Hatate <version>, exit 0, no display needed
```

then launch it with throw-away `HOME`, `XDG_CONFIG_HOME` and
`XDG_CACHE_HOME` and no Hydrus configured, and run at most one Lens
search (Google rate-challenges repeated searches).

## 3. Upload a draft

```bash
git tag v<version> <commit> && git push origin v<version>     # only with board approval
gh release create v<version> --draft --title "Hatate <version>" \
    --notes-file notes.md Hatate-x86_64.AppImage SHA256SUMS
```

`--draft` is not optional. The notes carry the AppImage's size, the
glibc floor, the SHA-256 and the licence line ("the AppImage as a whole
is GPLv3 because it bundles PyQt6; Hatate's own source is MIT").

## 4. Non-author draft check

Done by someone who did not build it, on the assets **as uploaded to the
draft**, not on a local copy:

1. Download both assets with an authenticated `gh release download`, then
   `sha256sum -c --ignore-missing SHA256SUMS`.
2. `./Hatate-x86_64.AppImage --version` prints `Hatate <version>`, exit 0.
3. Run the shipped binary's network path, not just `--version`: launch the
   GUI with temporary `HOME`/`XDG_*` directories, and run one **positive
   control** Lens search (an image Google is known to match exactly, with
   its source named) and one **negative control** (a generated noise PNG).
   Paste the result count and top result URLs, and a screenshot. A robot
   check by itself is not a packaging failure, but one completed positive
   search with results is required. Confirm from `ps` that the running
   Chromium lives under the AppImage's mount, not on the host.
4. A **clean-system run**: a minimal Debian 12 root filesystem (`bwrap`)
   with no `python3`, `libnss3`, `libatk*` or `libxcb-cursor0`, binding
   only the display socket. `--version` plus the positive-control search
   must work there, and `ldd` over the bundled `chrome` and the Qt
   platform plugins must print no `not found`.
5. Nothing is written under the mount; list what landed in the temporary
   XDG directories.
6. Every bundled component has a licence file under `usr/share/licenses/`
   (`INDEX.txt` lists them) and `NOTICE` names the GPLv3 position.

## 5. The DAN-1702 probes: is the draft really private?

While the release is a draft, the tag page on github.com can still return
200 (the git tag is public even when the release is not), so the HTML tag
page proves nothing. Use these instead, anonymously (no token):

| Probe | Expect while draft | Expect after publish |
|-------|--------------------|----------------------|
| `curl -s -o /dev/null -w '%{http_code}' https://api.github.com/repos/Dromares/Hatate-Linux-Redux/releases/tags/v<version>` | `404` | `200` |
| each asset URL `https://github.com/Dromares/Hatate-Linux-Redux/releases/download/v<version>/<asset>` (`-L`, final status) | `404` | `200` |
| control: `.../releases/tags/v0.0.0-nope` | `404` | `404` |
| control: `.../releases/tags/<an already published tag>` | `200` | `200` |

If the first two return `200` before you published, the release is not a
draft: stop and tell the release owner. Re-run the same probes after
publishing; both asset URLs must then answer `200` and the downloaded
file must match `SHA256SUMS`. (Until the first release exists there is no
published tag for the last control; say so in the check and skip only that
row.)

## 6. Publish

Only after the non-author check passes at the exact assets in the draft:

```bash
gh release edit v<version> --draft=false --latest
```

Then re-run the probes in step 5 and the README's own install commands
against the public assets.

## 7. Clean up

Delete the build directory (the rootfs, the squashfs and the downloaded
Chromium are several GB). Keep `build.log`, `build-manifest.txt` and the
check evidence with the release ticket.
