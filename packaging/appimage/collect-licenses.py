#!/usr/bin/env python3
"""Puts the license text of every bundled component into AppDir/usr/share/licenses.

    collect-licenses.py APPDIR REPO_ROOT BUNDLED_SOURCES

Run with the AppDir's own Python. Fails (non-zero) when a component has no
license file, so an unlicensed wheel or library cannot slip into a build.

Layout under usr/share/licenses/:
  hatate/               Hatate's own MIT LICENSE
  cpython/              CPython (PSF)
  python/<dist>-<ver>/  every installed wheel (PyQt6 GPLv3, Playwright
                        Apache-2.0, numpy, lxml, Pillow, ...)
  qt/                   Qt's LGPLv3 + GPLv3 texts and the source offer
  playwright-driver/    the Node driver bundled in the Playwright wheel
  chromium/             Chromium's LICENSE and its third-party credits
  debian/<package>/     copyright files of every system library bundled
"""
import importlib.metadata as md
import re
import shutil
import subprocess
import sys
from pathlib import Path

LICENSE_NAME = re.compile(r"(^|/)(LICEN[CS]E|COPYING|NOTICE)[^/]*$", re.I)
SKIP_DISTS = {"pip", "setuptools", "wheel"}


def copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def main() -> None:
    appdir, repo, sources = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    out = appdir / "usr" / "share" / "licenses"
    py = appdir / "usr" / "python"
    site = next((py / "lib").glob("python3.*/site-packages"))
    problems = []

    copy(repo / "LICENSE", out / "hatate" / "LICENSE")
    copy(next((py / "lib").glob("python3.*/LICENSE.txt")), out / "cpython" / "LICENSE.txt")

    for dist in sorted(md.distributions(path=[str(site)]), key=lambda d: d.metadata["Name"].lower()):
        name = dist.metadata["Name"]
        if name.lower() in SKIP_DISTS:
            continue
        found = [f for f in (dist.files or []) if LICENSE_NAME.search(str(f))]
        for f in found:
            rel = re.sub(r"^.*?\.dist-info/", "", str(f))
            copy(Path(dist.locate_file(f)), out / "python" / f"{name}-{dist.version}" / rel)
        if not found:
            problems.append(f"wheel {name} {dist.version} ships no license file")

    qt = out / "qt"
    for name in ("LGPL-3", "GPL-3"):
        copy(Path("/usr/share/common-licenses") / name, qt / f"{name}.txt")
    copy(repo / "packaging" / "appimage" / "licenses" / "qt-source-offer.txt", qt / "SOURCE-OFFER.txt")

    drv = site / "playwright" / "driver"
    copy(drv / "LICENSE", out / "playwright-driver" / "LICENSE")
    copy(drv / "package" / "ThirdPartyNotices.txt", out / "playwright-driver" / "ThirdPartyNotices.txt")

    chromium = out / "chromium"
    copy(repo / "packaging" / "appimage" / "licenses" / "chromium-LICENSE.txt", chromium / "LICENSE")
    credits = appdir / "chromium-credits.html"
    if credits.is_file():
        shutil.move(str(credits), chromium / "credits.html")
    else:
        problems.append("Chromium third-party credits were not extracted")
    about = next((appdir / "usr" / "lib" / "ms-playwright").glob("chromium-*/chrome-linux64/ABOUT"), None)
    if about:
        copy(about, chromium / "ABOUT")

    packages = set()
    for line in sources.read_text().split():
        # Debian packages that predate the /usr merge list /lib/..., not /usr/lib/...
        res = subprocess.run(["dpkg", "-S", line], capture_output=True, text=True)
        if res.returncode != 0 and line.startswith("/usr/lib/"):
            res = subprocess.run(["dpkg", "-S", line[4:]], capture_output=True, text=True)
        if res.returncode != 0:
            problems.append(f"no Debian package owns bundled library {line}")
            continue
        packages.add(res.stdout.split(":")[0].strip())
    packages |= set(sys.argv[4:])           # extra packages (fonts) named on the command line
    for pkg in sorted(packages):
        doc = Path("/usr/share/doc") / pkg / "copyright"
        if doc.is_file():
            copy(doc, out / "debian" / pkg / "copyright")
        else:
            problems.append(f"Debian package {pkg} has no /usr/share/doc/{pkg}/copyright")

    (appdir / "usr" / "share" / "hatate" / "bundled-packages.txt").write_text(
        "\n".join(sorted(packages)) + "\n")

    index = sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file())
    (out / "INDEX.txt").write_text("\n".join(index) + "\n")
    print(f"collect-licenses: {len(index)} license files, {len(packages)} Debian packages")
    if problems:
        print("collect-licenses: " + "\n".join(problems), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
