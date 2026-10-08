"""Generates a synthetic image library for the DAN-27 profiling harness.

Not a test - deliberately named so ``unittest discover`` (pattern
``test*.py``) never picks it up. It is slow by design (thousands of real
image files) and has nothing to assert; it exists to give
profile_search_path.py something realistic to chew on.

Produces PIL-drawn images, never any of the board's real files, with a
shape meant to resemble an actual Hydrus library: mostly ordinary photos,
a handful of near-duplicates (crops/resizes/rotations of each other, the
case the real similarity/ranking code has to work hardest on), a few very
large scans, a few tiny thumbnails, and a mix of the formats
core/formats.py declares. Deterministic for a given seed - two calls with
the same (root, size, seed) produce byte-identical libraries, so a profile
run is reproducible.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import numpy as np
from PIL import Image, ImageDraw

# Formats this app declares it accepts (core/formats.py) that PIL on this
# machine can actually encode. JXL has no writer here (PIL reports no jxl
# feature); WEBM/MP4 are video, not stills, and never reach the
# image_compare/image_prep code this harness measures - see the report's
# "module coverage" section for why those are out of scope rather than
# silently skipped.
FORMATS = ["JPEG", "PNG", "GIF", "BMP", "WEBP", "TIFF", "AVIF"]
# Roughly what a real library skews towards: JPEG dominant, a long tail of
# the rest. Weights are illustrative, not measured against a real library -
# this harness only needs "mixed", not "exact".
FORMAT_WEIGHTS = [55, 20, 5, 5, 8, 4, 3]

EXT = {
    "JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "BMP": ".bmp",
    "WEBP": ".webp", "TIFF": ".tif", "AVIF": ".avif",
}

# Size distribution, as (weight, (min_side, max_side)) - biased towards
# ordinary photo/screenshot sizes, with a thin slice of "very large scan"
# and "tiny thumbnail" outliers, per the DAN-27 brief.
SIZE_BANDS = [
    (78, (256, 900)),      # ordinary
    (15, (900, 2200)),     # larger photos
    (4, (2800, 5000)),     # a few very large images
    (3, (24, 96)),         # a few tiny ones
]

# Fraction of the library generated as near-duplicate PAIRS of an already-
# generated image (a crop, resize or rotation of it) rather than a fresh
# random image - this is the case image_compare/similarity_check exist to
# handle, and a library with none of it would flatter the profile.
NEAR_DUP_FRACTION = 0.12


@dataclass
class LibraryEntry:
    path: str
    fmt: str
    width: int
    height: int
    near_duplicate_of: Optional[str] = None


def _weighted_choice(rng: random.Random, weights: List[int]):
    return rng.choices(range(len(weights)), weights=weights, k=1)[0]


def _random_image(rng: random.Random, np_rng: np.random.Generator, width: int, height: int) -> Image.Image:
    """A cheap-but-not-degenerate image: low-res colour noise upscaled
    (so dhash has real gradients to read, not a flat field) plus a couple
    of drawn shapes (so it isn't pure noise either - closer to a photo's
    mix of texture and flat regions)."""
    small_w, small_h = max(2, width // 24), max(2, height // 24)
    noise = np_rng.integers(0, 256, size=(small_h, small_w, 3), dtype=np.uint8)
    image = Image.fromarray(noise, mode="RGB").resize((width, height), Image.BILINEAR)
    draw = ImageDraw.Draw(image)
    for _ in range(rng.randint(1, 4)):
        x0, y0 = rng.randint(0, width), rng.randint(0, height)
        x1, y1 = rng.randint(0, width), rng.randint(0, height)
        colour = tuple(int(v) for v in np_rng.integers(0, 256, size=3))
        xmin, xmax = sorted([x0, x1])
        ymin, ymax = sorted([y0, y1])
        box = [xmin, ymin, xmax, ymax]
        if rng.random() < 0.5:
            draw.rectangle(box, fill=colour)
        else:
            draw.ellipse(box, fill=colour)
    return image


def _near_duplicate(base: Image.Image, rng: random.Random) -> Image.Image:
    """A crop, resize or rotation of `base` - visually the same picture,
    never byte-identical, which is the realistic case for a re-post or a
    re-encode found on another site."""
    variant = rng.choice(["crop", "resize", "rotate"])
    if variant == "crop":
        w, h = base.size
        dx, dy = int(w * rng.uniform(0.02, 0.08)), int(h * rng.uniform(0.02, 0.08))
        return base.crop((dx, dy, w - dx, h - dy))
    if variant == "resize":
        w, h = base.size
        scale = rng.uniform(0.5, 1.6)
        return base.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    return base.rotate(rng.uniform(-3, 3), expand=True, fillcolor=(128, 128, 128))


def _save(image: Image.Image, path: Path, fmt: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    save_image = image
    if fmt in ("JPEG",) and image.mode not in ("RGB", "L"):
        save_image = image.convert("RGB")
    if fmt == "GIF":
        save_image = image.convert("P", palette=Image.ADAPTIVE)
    kwargs = {}
    if fmt == "JPEG":
        kwargs["quality"] = 85
    save_image.save(path, format=fmt, **kwargs)


def generate_library(root: Path, size: int, seed: int = 20260928) -> List[LibraryEntry]:
    """Builds `size` synthetic images under `root`, returns their metadata.

    Deterministic for a given (root, size, seed). Skips regeneration if
    `root` already holds exactly `size` files from a prior run with the
    same manifest, so re-running the harness after a crash does not
    re-pay the generation cost.
    """
    manifest_path = root / "manifest.tsv"
    if manifest_path.exists():
        existing = manifest_path.read_text().splitlines()
        if len(existing) == size + 1:  # + header
            return _load_manifest(manifest_path)

    rng = random.Random(seed)
    np_rng = np.random.default_rng(seed)
    root.mkdir(parents=True, exist_ok=True)

    entries: List[LibraryEntry] = []
    recent_originals: List[LibraryEntry] = []  # small ring buffer to pick near-dup sources from
    for i in range(size):
        # Shard into subdirectories of 1000 so no single directory holds
        # tens of thousands of files - realistic, and avoids pathological
        # directory-listing costs that would confound the profile with
        # filesystem behaviour instead of this app's own code.
        shard = root / f"{i // 1000:04d}"
        fmt_idx = _weighted_choice(rng, FORMAT_WEIGHTS)
        fmt = FORMATS[fmt_idx]

        make_dup = recent_originals and rng.random() < NEAR_DUP_FRACTION
        if make_dup:
            source = rng.choice(recent_originals)
            base_path = Path(source.path)
            with Image.open(base_path) as im:
                image = _near_duplicate(im.convert("RGB"), rng)
            width, height = image.size
            dup_of = source.path
        else:
            band_idx = _weighted_choice(rng, [w for w, _ in SIZE_BANDS])
            lo, hi = SIZE_BANDS[band_idx][1]
            width = rng.randint(lo, hi)
            height = int(width * rng.uniform(0.65, 1.4))
            image = _random_image(rng, np_rng, width, height)
            dup_of = None

        filename = f"img_{i:06d}{EXT[fmt]}"
        path = shard / filename
        _save(image, path, fmt)

        entry = LibraryEntry(path=str(path), fmt=fmt, width=width, height=height,
                              near_duplicate_of=dup_of)
        entries.append(entry)
        if dup_of is None:
            recent_originals.append(entry)
            if len(recent_originals) > 200:
                recent_originals.pop(0)

    with manifest_path.open("w") as fh:
        fh.write("path\tformat\twidth\theight\tnear_duplicate_of\n")
        for e in entries:
            fh.write(f"{e.path}\t{e.fmt}\t{e.width}\t{e.height}\t{e.near_duplicate_of or ''}\n")

    return entries


def _load_manifest(manifest_path: Path) -> List[LibraryEntry]:
    entries = []
    lines = manifest_path.read_text().splitlines()[1:]
    for line in lines:
        path, fmt, width, height, dup_of = line.split("\t")
        entries.append(LibraryEntry(path=path, fmt=fmt, width=int(width), height=int(height),
                                     near_duplicate_of=dup_of or None))
    return entries
