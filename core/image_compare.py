"""Comparing a local image against a matched one.

Two separate questions, deliberately answered separately:

1. "Is the match BETTER than what I have?" - a size and dimension
   comparison. The raw numbers were already on screen, but as two
   independent readouts, leaving the actual arithmetic to the reader.
   What matters is the relationship: bigger, smaller, or a different
   shape entirely.

2. "Is it the SAME image?" - answered with a perceptual hash rather than
   a pixel difference. A pixel diff is the obvious approach and a bad
   one here: the two images are nearly always different resolutions (the
   whole reason for comparing), so one has to be resampled onto the
   other, and resampling alone produces differences along every edge.
   Boorus also re-encode, and JPEG round-tripping perturbs essentially
   every pixel. A diff would therefore report "different" for perfect
   matches, which is worse than reporting nothing. A perceptual hash is
   resolution-independent and survives re-encoding, so its answer means
   something.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import List, Optional, Tuple, Union

from PIL import Image, ImageOps

from .applog import get_logger

log = get_logger("image_compare")

# Long-edge ratios within this of 1.0 count as "the same size" - a few
# pixels either way is re-encoding noise, not a better version.
SAME_SIZE_TOLERANCE = 0.02

# Aspect ratios differing by more than this suggest a crop or a different
# edit rather than the same image at another resolution. Worth saying out
# loud: a "bigger" match that's also a different shape may be missing
# part of the picture.
ASPECT_TOLERANCE = 0.04

# dHash is 64 bits. Thresholds are the usual working values for
# "same image" vs "related" vs "different".
DHASH_SAME_MAX = 5
DHASH_SIMILAR_MAX = 12


@dataclass
class SizeComparison:
    """How a matched image's dimensions relate to the local file's."""
    local_size: Optional[tuple] = None
    remote_size: Optional[tuple] = None
    scale: Optional[float] = None          # long-edge ratio, remote / local
    megapixel_ratio: Optional[float] = None
    aspect_differs: bool = False
    verdict: str = ""                      # short human phrase, e.g. "2.0x larger"

    @property
    def remote_is_bigger(self) -> bool:
        return self.scale is not None and self.scale > 1 + SAME_SIZE_TOLERANCE


def compare_sizes(local_w, local_h, remote_w, remote_h) -> SizeComparison:
    """Relates a match's dimensions to the local file's.

    Reported on the LONG EDGE rather than by pixel count, because that's
    how people describe resolution - "twice as big" means twice the
    width, not four times the pixels. The megapixel ratio is kept
    alongside for anyone who wants it.
    """
    if not all(isinstance(v, int) and v > 0 for v in (local_w, local_h, remote_w, remote_h)):
        return SizeComparison(verdict="")

    local_long = max(local_w, local_h)
    remote_long = max(remote_w, remote_h)
    scale = remote_long / local_long
    megapixels = (remote_w * remote_h) / (local_w * local_h)

    local_aspect = local_w / local_h
    remote_aspect = remote_w / remote_h
    aspect_differs = abs(local_aspect - remote_aspect) / local_aspect > ASPECT_TOLERANCE

    if abs(scale - 1.0) <= SAME_SIZE_TOLERANCE:
        verdict = "same size"
    elif scale > 1:
        verdict = f"{scale:.2g}\u00d7 larger"
    else:
        # Expressed as how much smaller rather than as a fraction: "half
        # the size" is immediately readable, "0.5x larger" is not.
        verdict = f"{1 / scale:.2g}\u00d7 smaller"

    if aspect_differs:
        verdict += ", different shape"

    return SizeComparison(
        local_size=(local_w, local_h), remote_size=(remote_w, remote_h),
        scale=scale, megapixel_ratio=megapixels,
        aspect_differs=aspect_differs, verdict=verdict,
    )


def compact_size_delta(local_w, local_h, remote_w, remote_h) -> str:
    """A short "how does the match compare" label for the table column:
    "+ 4x" when the match is four times the size, "- 4x" when it's a
    quarter, "=" when they're the same, "" when it can't be determined.

    Always expressed relative to the LOCAL file, so the sign reads the
    same way every time: + means the match is an upgrade, - means it's
    smaller than what you already have. Uses the same long-edge scale as
    compare_sizes(), so this column and the preview panel's comparison
    line can never disagree about the same pair of images.
    """
    comparison = compare_sizes(local_w, local_h, remote_w, remote_h)
    if comparison.scale is None:
        return ""
    if abs(comparison.scale - 1.0) <= SAME_SIZE_TOLERANCE:
        return "="
    if comparison.scale > 1:
        return f"+ {_ratio(comparison.scale, 3)}x"
    # Expressed as how many times SMALLER rather than as a fraction:
    # "- 4x" is immediately readable, "- 0.25x" needs mental arithmetic.
    return f"- {_ratio(1 / comparison.scale, 3)}x"


def _ratio(value: float, digits: int) -> str:
    """A ratio to `digits` significant figures - but never in exponent
    form. Python's "g" format switches to it as soon as the integer part
    has more digits than that, which put "1.5e+03× bigger file" on the
    comparison banner for a match 1,500 times the size."""
    if value >= 10 ** (digits - 1):
        return f"{round(value):,}"
    return f"{value:.{digits}g}"


def size_delta_sort_key(local_w, local_h, remote_w, remote_h) -> float:
    """Orders the column by the real ratio rather than its text.

    Sorting the labels as strings would put "- 4x" next to "- 1.5x" and
    "+ 10x" before "+ 2x", which is not what anyone clicking that header
    is asking for. Unknown values sort to one end rather than colliding
    with genuine parity at 1.0.
    """
    comparison = compare_sizes(local_w, local_h, remote_w, remote_h)
    return comparison.scale if comparison.scale is not None else -1.0


def compare_file_sizes(local_bytes: Optional[int], remote_bytes: Optional[int]) -> str:
    """Short phrase relating two file sizes, or "" when either is unknown."""
    if not local_bytes or not remote_bytes or local_bytes <= 0 or remote_bytes <= 0:
        return ""
    ratio = remote_bytes / local_bytes
    if abs(ratio - 1.0) <= 0.05:
        return "about the same size on disk"
    if ratio > 1:
        return f"{_ratio(ratio, 2)}\u00d7 bigger file"
    return f"{_ratio(1 / ratio, 2)}\u00d7 smaller file"


def dhash(source: Union[str, bytes, Image.Image], hash_size: int = 8) -> Optional[int]:
    """Difference hash: compares each pixel to its right-hand neighbour on
    a tiny grayscale version, giving one bit per comparison.

    Resolution-independent by construction (everything is scaled to the
    same grid first) and insensitive to re-encoding, brightness shifts
    and mild colour changes - exactly the differences that make a raw
    pixel diff useless here.
    """
    try:
        if isinstance(source, Image.Image):
            image = source
        elif isinstance(source, bytes):
            image = Image.open(io.BytesIO(source))
        else:
            image = Image.open(source)

        with image:
            # hash_size+1 wide so there are hash_size horizontal
            # comparisons per row.
            small = image.convert("L").resize(
                (hash_size + 1, hash_size), Image.LANCZOS,  # type: ignore[attr-defined]  # Pillow stub gap: constant/return type missing or narrower than the object PIL actually returns
            )
            pixels = list(small.getdata())

        bits = 0
        for row in range(hash_size):
            offset = row * (hash_size + 1)
            for col in range(hash_size):
                bits <<= 1
                if pixels[offset + col] > pixels[offset + col + 1]:
                    bits |= 1
        return bits
    except (OSError, ValueError, IndexError) as exc:
        log.debug("Could not compute a perceptual hash: %s", exc)
        return None


def image_dimensions(source: Union[str, bytes, Image.Image]) -> Optional[tuple]:
    """(width, height), or None if the image can't be read.

    Cheap for a path: PIL reads only the header, not the pixel data, so
    this costs far less than hashing the file.
    """
    try:
        if isinstance(source, Image.Image):
            return source.size
        if isinstance(source, bytes):
            with Image.open(io.BytesIO(source)) as image:
                return image.size
        with Image.open(source) as image:
            return image.size
    except (OSError, ValueError) as exc:
        log.debug("Could not read the dimensions of an image: %s", exc)
        return None


def hamming_distance(a: Optional[int], b: Optional[int]) -> Optional[int]:
    """How many bits differ between two hashes, or None if either is
    missing."""
    if a is None or b is None:
        return None
    return bin(a ^ b).count("1")


# Anchors for turning a hash distance into a percentage, taken from the
# bands above rather than invented: the boundary of "looks like the same
# image" becomes 90%, the boundary of "similar but not identical"
# becomes 70%, and a distance of 32 - what two unrelated pictures
# average on a 64-bit hash - becomes 0. Between the anchors it is
# linear.
#
# The scale is chosen to read like IQDB's and SauceNAO's, because it is
# shown in the same column: a 90+ means the same picture there, and it
# has to mean the same picture here.
SIMILARITY_ANCHORS = ((0, 100.0), (DHASH_SAME_MAX, 90.0), (DHASH_SIMILAR_MAX, 70.0), (32, 0.0))


def perceptual_similarity(distance: Optional[int]) -> Optional[float]:
    """A percentage from a hash distance, or None if there is no distance.

    This is a MEASUREMENT, unlike the ordinal scores ascii2d and the two
    Google engines produce - those number their results by position
    because the service reports no similarity at all.
    """
    if distance is None:
        return None
    if distance <= 0:
        return 100.0
    for (low_d, low_pct), (high_d, high_pct) in zip(SIMILARITY_ANCHORS, SIMILARITY_ANCHORS[1:], strict=False):
        if distance <= high_d:
            span = high_d - low_d
            if span <= 0:
                return high_pct
            fraction = (distance - low_d) / span
            return round(low_pct + (high_pct - low_pct) * fraction, 1)
    return 0.0


def describe_perceptual_match(distance: Optional[int]) -> str:
    """Turns a hash distance into something meaningful to read.

    Phrased as confidence rather than certainty: a perceptual hash can
    be fooled, and claiming two images are definitively identical on 64
    bits of evidence would be overstating it.
    """
    if distance is None:
        return "Couldn't compare the images"
    if distance <= DHASH_SAME_MAX:
        return f"Looks like the same image (difference {distance}/64)"
    if distance <= DHASH_SIMILAR_MAX:
        return f"Similar, but not identical - possibly a different edit or crop ({distance}/64)"
    return f"These look like different images (difference {distance}/64)"


# -- comparing a thumbnail with the local file, tolerating how thumbnails differ
#
# A thumbnail from a search engine is rarely just the picture made smaller.
# MEASURED on 418 images from a real library, each thumbnailed the ways
# engines do it and compared with a plain 64-bit dHash: a 4% crop scored
# a median 81% (18% of them reached 90%), 10% off one side 76% (6%),
# letterboxing 84% (25%), and a mirrored copy 21% (0.5%) - while plain
# thumbnails, tiny ones and colour shifts were already near 100%.
#
# So the local image is compared in a few shapes - as it is, cropped
# slightly all round or off one side (from both the picture and the
# picture with any plain border trimmed), and mirrored - against the
# thumbnail as it is and trimmed, and the closest pair counts; a 90+ must
# then be confirmed at 256 bits (see FINE_CONFIRM_MAX). On the same
# images: 99.5-100% of crops and letterboxed copies reach 90%, plain
# thumbnails lose nothing, and of 57,824 pairs of DIFFERENT pictures none
# reached 90% (4 reached 75%, the highest 78.6%).

# Hashing a crop of a 5,000px original means resizing all of it again;
# every shape is cut from one copy this size instead. dHash reads a 9x8
# grid, so nothing it uses is lost.
LOCAL_WORKING_SIZE = 512
ALIGN_EDGE_CROPS = (0.04, 0.08)
ALIGN_SIDE_CROP = 0.10
# A plain bar: nearly every pixel close to the corner's shade of grey.
# "Nearly": JPEG smears the edge where a bar meets the picture, and
# demanding every pixel stopped the trim a few rows short - leaving 3px
# of bar per side on a thumbnail, which is nothing at 64 bits and enough
# to fail the 256-bit confirmation.
BORDER_TOLERANCE = 24
BORDER_FLAT_SHARE = 0.97
BORDER_MAX_FRACTION = 0.25
# A mirrored copy is the same artwork but not the same file. Recognised,
# and scored as a strong match to review - never high enough to be sent
# to Hydrus automatically, since that would import the flipped file.
MIRRORED_MAX_SIMILARITY = 85.0

# A 90+ has to be confirmed at 256 bits before it stands. A 64-bit hash
# cannot tell a picture from an EDITED VARIANT of it (censored, retexted,
# a colour or pose alternate). MEASURED against IQDB on a real library:
# of 12 matches IQDB put at 75-89% and this scored 90+, six were the same
# picture (within 15 bits of 256) and six were variants or different
# (23-61) - IQDB was right about those. IQDB's own 90+ matches were all
# within 16. Same-picture thumbnails measure within 20; a crop cannot
# always be lined up that closely (65-80% of them are), so an unconfirmed
# 90+ is held just below auto-import, for review, rather than dropped.
FINE_HASH_SIZE = 16
FINE_CONFIRM_MAX = 20
UNCONFIRMED_MAX_SIMILARITY = 85.0


@dataclass
class LocalPrints:
    """The local image's hashes in every shape compare_to_local tries -
    64-bit for scoring, 256-bit (fine_*) for confirming a 90+."""
    straight: List[int]
    mirrored: Optional[int]
    fine_straight: List[int]
    fine_mirrored: Optional[int]


def trim_border(image: Image.Image) -> Image.Image:
    """The image without plain bars along its edges (letterboxing, any
    colour). Leaves it alone rather than cut into more than a quarter."""
    grey = image.convert("L")
    width, height = grey.size
    pixels = grey.load()

    def flat(values, ref):
        values = list(values)
        close = sum(1 for v in values if abs(v - ref) <= BORDER_TOLERANCE)
        return bool(values) and close >= BORDER_FLAT_SHARE * len(values)

    def flat_row(y, ref):
        return flat((pixels[x, y] for x in range(0, width, 2)), ref)

    def flat_col(x, top, bottom, ref):
        return flat((pixels[x, y] for y in range(top, bottom, 2)), ref)

    top_ref, bottom_ref = pixels[0, 0], pixels[width - 1, height - 1]  # type: ignore[index]  # Pillow stub gap: constant/return type missing or narrower than the object PIL actually returns
    top = 0
    while top < height * BORDER_MAX_FRACTION and flat_row(top, top_ref):
        top += 1
    bottom = height
    while height - bottom < height * BORDER_MAX_FRACTION and flat_row(bottom - 1, bottom_ref):
        bottom -= 1
    left = 0
    while left < width * BORDER_MAX_FRACTION and flat_col(left, top, bottom, top_ref):
        left += 1
    right = width
    while width - right < width * BORDER_MAX_FRACTION and flat_col(right - 1, top, bottom,
                                                                   bottom_ref):
        right -= 1
    if bottom - top < height / 2 or right - left < width / 2:
        return image
    if (left, top, right, bottom) == (0, 0, width, height):
        return image
    return image.crop((left, top, right, bottom))


def _shapes(image: Image.Image) -> List[Image.Image]:
    width, height = image.size
    shapes = [image]
    for f in ALIGN_EDGE_CROPS:
        dx, dy = int(width * f), int(height * f)
        shapes.append(image.crop((dx, dy, width - dx, height - dy)))
    dx, dy = int(width * ALIGN_SIDE_CROP), int(height * ALIGN_SIDE_CROP)
    shapes += [image.crop((dx, 0, width, height)), image.crop((0, dy, width, height)),
               image.crop((0, 0, width - dx, height)), image.crop((0, 0, width, height - dy))]
    return shapes


def local_prints(source: Union[str, bytes, Image.Image]) -> Optional[LocalPrints]:
    """Hash the local image once, in every shape a thumbnail is compared
    against. None if it can't be read."""
    try:
        if isinstance(source, Image.Image):
            image = source.copy()
        else:
            with Image.open(io.BytesIO(source) if isinstance(source, bytes) else source) as im:
                im.draft("RGB", (LOCAL_WORKING_SIZE, LOCAL_WORKING_SIZE))   # fast JPEG decode
                image = im.convert("RGB")
        image.thumbnail((LOCAL_WORKING_SIZE, LOCAL_WORKING_SIZE), Image.LANCZOS)  # type: ignore[attr-defined]  # Pillow stub gap: constant/return type missing or narrower than the object PIL actually returns
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        # The bomb check only fires where the app hasn't lifted Pillow's
        # size limit (main.py does, for large local files) - but a
        # fingerprint must fail quietly, never take a search down.
        log.debug("Could not read the local image to fingerprint it: %s", exc)
        return None
    trimmed = trim_border(image)
    # Cropped both ways: a trim can take a plain background for a border,
    # so crops of the untrimmed picture are kept alongside.
    shapes = _shapes(image) + (_shapes(trimmed) if trimmed is not image else [])
    mirror = ImageOps.mirror(trimmed)
    straight = [h for h in (dhash(s) for s in shapes) if h is not None]
    if not straight:
        return None
    fine = [h for h in (dhash(s, FINE_HASH_SIZE) for s in shapes) if h is not None]
    return LocalPrints(straight=straight, mirrored=dhash(mirror),
                       fine_straight=fine, fine_mirrored=dhash(mirror, FINE_HASH_SIZE))


@dataclass
class Comparison:
    distance: Optional[int]          # 64-bit, the closest shape
    mirrored: bool = False           # that closest shape was the mirrored one
    fine_distance: Optional[int] = None   # 256-bit, the same way round


def compare_to_local(prints: Optional[LocalPrints],
                     thumbnail: Union[bytes, Image.Image]) -> Comparison:
    """How close a thumbnail is to the local image, in its closest shape."""
    if prints is None:
        return Comparison(None)
    try:
        with (Image.open(io.BytesIO(thumbnail)) if isinstance(thumbnail, bytes)
              else thumbnail.copy()) as im:
            image = im.convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        log.debug("Could not read a thumbnail to compare: %s", exc)
        return Comparison(None)
    views = (image, trim_border(image))
    theirs = [h for h in (dhash(v) for v in views) if h is not None]
    if not theirs:
        return Comparison(None)
    fine_theirs = [h for h in (dhash(v, FINE_HASH_SIZE) for v in views) if h is not None]

    def closest(mine, others):
        return min((bin(m ^ t).count("1") for m in mine for t in others), default=None)

    straight = closest(prints.straight, theirs)
    mirrored = closest([prints.mirrored], theirs) if prints.mirrored is not None else None
    if mirrored is not None and mirrored < straight:
        fine = (closest([prints.fine_mirrored], fine_theirs)
                if prints.fine_mirrored is not None else None)
        return Comparison(mirrored, True, fine)
    return Comparison(straight, False, closest(prints.fine_straight, fine_theirs))


def aligned_similarity(prints: Optional[LocalPrints],
                       thumbnail: Union[bytes, Image.Image]) -> Tuple[Optional[float], bool]:
    """(percentage, mirrored) - compare_to_local on the usual scale, with
    a mirrored copy, and a 90+ the fine hash doesn't confirm, held below
    auto-import."""
    comparison = compare_to_local(prints, thumbnail)
    score = perceptual_similarity(comparison.distance)
    if score is None:
        return None, False
    if comparison.mirrored:
        score = min(score, MIRRORED_MAX_SIMILARITY)
    if score > UNCONFIRMED_MAX_SIMILARITY and (
            comparison.fine_distance is None or comparison.fine_distance > FINE_CONFIRM_MAX):
        score = UNCONFIRMED_MAX_SIMILARITY
    return score, comparison.mirrored
