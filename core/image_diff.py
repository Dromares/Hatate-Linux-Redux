"""Where two copies of the same picture actually differ.

The compare dialog can already show the two side by side under a wipe,
which answers "are these the same shot". It does not answer the next
question - WHAT changed - and that is usually the one that decides which
copy to keep: a watermark, a signature, added text, a censoring bar, a
logo burned into a corner.

The obvious implementation is the wrong one, and WipeView's own docstring
says why: the two images are almost always different resolutions, so
"a pixel-aligned overlay would be the wrong tool". A straight
pixel-by-pixel difference between a 1200px local file and a 2000px match
lights up over the whole frame - JPEG ringing, resampling softness and a
one-pixel misregistration all read as "different" and bury the thing
worth seeing.

So this is deliberately STRUCTURAL rather than exact. Both images are
scaled to one working size, reduced to luminance, levelled against each
other, differenced, blurred to throw away the high-frequency noise that
re-encoding invents, and thresholded. What survives is the regions that
genuinely changed. The result is honest about being approximate: it says
"this area differs", never "these exact pixels differ" - and the
perceptual hash the dialog already reports remains the answer to "is it
the same image at all".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
from PIL import Image, ImageFilter

from .applog import get_logger

log = get_logger("image_diff")

# Both images are resampled to this long edge before comparing. Big
# enough that a signature or a small watermark survives, small enough
# that the whole comparison stays interactive - the dialog recomputes on
# demand, not in a worker.
WORK_LONG_EDGE = 900

# Radius of the blur applied to the difference. This is what separates
# "the encoder made different choices" from "something is actually
# there": compression noise is high-frequency and disappears, while a
# watermark or a caption is a solid region and survives.
NOISE_BLUR_RADIUS = 2.0

# Luminance difference, 0-255, below which a pixel counts as unchanged.
# Set from what re-encoding alone produces: two JPEG encodings of one
# picture routinely differ by a few levels everywhere.
CHANGE_THRESHOLD = 26

# Above this fraction the differences are no longer localised, and this
# view stops being able to say what changed. Measured: two clearly
# different scenes of the same shape came out at 54%, so the line sits
# below that - a uniformly lit rectangle is not a result, and calling one
# "an edit" would be worse than declining to guess.
#
# Deliberately NOT presented as a verdict on whether it is the same
# picture. The dialog already reports a perceptual hash, which is the
# right tool for that question; this one only says how much moved.
WHOLESALE_FRACTION = 0.45

# Aspect ratios further apart than this cannot be scaled into the same
# box without distorting one of them, which misaligns every edge and
# makes the whole comparison meaningless.
ASPECT_TOLERANCE = 0.06


@dataclass
class DifferenceMap:
    """The regions that differ, plus what that amounts to."""
    mask: Optional[Image.Image]   # 'L', working size: 0 unchanged, brighter = more different
    changed_fraction: float       # 0..1 of the frame that changed
    verdict: str                  # what to tell the user
    reliable: bool                # False when the two don't line up well enough to trust

    @property
    def changed_percent(self) -> float:
        return self.changed_fraction * 100.0


def _aspect(size: Tuple[int, int]) -> float:
    w, h = size
    return (w / h) if h else 0.0


def _prepare(image: Image.Image, size: Tuple[int, int]) -> np.ndarray:
    """One image as a float RGB array at the working size.

    Colour, not luminance. Comparing brightness alone was the first
    version's mistake and a real one: two colours of similar brightness -
    a recolour, a different hair or eye colour, a shifted palette -
    cancel out entirely in greyscale and the change simply is not there
    to find. Measured on a reported pair, the red channel differed by a
    99th-percentile of 146 levels while luminance showed 106.

    Image.Resampling rather than the older Image.LANCZOS alias: same
    filter, but the alias is missing from Pillow's type stubs.
    """
    rgb = image.convert("RGB").resize(size, Image.Resampling.LANCZOS)
    return np.asarray(rgb, dtype=np.float32)


def _working_size(local: Image.Image) -> Tuple[int, int]:
    w, h = local.size
    if not w or not h:
        return (0, 0)
    scale = WORK_LONG_EDGE / float(max(w, h))
    if scale >= 1.0:
        return (w, h)   # already small - don't upscale, it invents detail
    return (max(1, int(round(w * scale))), max(1, int(round(h * scale))))


def difference_map(local: Image.Image, match: Image.Image) -> DifferenceMap:
    """Where `match` differs from `local`, as a mask over `local`'s frame.

    Levels are matched before differencing. Two encodes of one picture
    often sit a few levels apart overall - a gamma or quantisation
    difference, not a content one - and without this that constant offset
    would light the entire frame while telling nobody anything.
    """
    if local is None or match is None:
        return DifferenceMap(None, 0.0, "Nothing to compare", False)

    size = _working_size(local)
    if size == (0, 0):
        return DifferenceMap(None, 0.0, "Nothing to compare", False)

    aspect_gap = abs(_aspect(local.size) - _aspect(match.size))
    aspects_match = aspect_gap <= ASPECT_TOLERANCE * max(1.0, _aspect(local.size))

    a = _prepare(local, size)
    b = _prepare(match, size)

    # Level-matched per channel. Encoders and colour profiles shift the
    # channels independently, so one shared offset would leave a genuine
    # cast in two of them looking like content.
    #
    # On the median rather than the mean: a large added element - a white
    # caption bar, a black censor block - drags a mean noticeably and
    # would be partly subtracted away as if it were a global shift.
    for channel in range(b.shape[2]):
        b[:, :, channel] += (
            float(np.median(a[:, :, channel])) - float(np.median(b[:, :, channel]))
        )

    # The STRONGEST channel disagreement, not their average. A change
    # confined to one channel - which is what a recolour is - gets
    # diluted by two-thirds if the channels are averaged, and that is
    # exactly the case the greyscale version was missing. Measured on a
    # reported pair: 6.8% found by averaging against 15.2% by this, with
    # every re-encoding control still at 0.00%.
    diff = np.abs(a - b).max(axis=2)
    blurred = Image.fromarray(np.clip(diff, 0, 255).astype(np.uint8), mode="L")
    blurred = blurred.filter(ImageFilter.GaussianBlur(NOISE_BLUR_RADIUS))
    values = np.asarray(blurred, dtype=np.float32)

    changed = values >= CHANGE_THRESHOLD
    fraction = float(changed.mean()) if changed.size else 0.0

    # Only what cleared the threshold is shown, scaled so the strongest
    # real difference reads as full intensity - otherwise a subtle but
    # genuine change renders as almost invisible.
    shown = np.where(changed, values, 0.0)
    peak = float(shown.max()) if shown.size else 0.0
    if peak > 0:
        shown = shown * (255.0 / peak)
    mask = Image.fromarray(np.clip(shown, 0, 255).astype(np.uint8), mode="L")

    reliable = aspects_match and fraction < WHOLESALE_FRACTION
    return DifferenceMap(mask, fraction, _verdict(fraction, aspects_match), reliable)


def _verdict(fraction: float, aspects_match: bool) -> str:
    if not aspects_match:
        return (
            "These are different shapes, so they cannot be lined up - the highlights "
            "below are the mismatch, not an edit. Compare them under the wipe instead."
        )
    percent = fraction * 100.0
    if fraction >= WHOLESALE_FRACTION:
        return (
            f"{percent:.0f}% of the frame differs - too much for this view to say WHAT "
            "changed. Either a heavily reworked version, a different crop, or a "
            "different picture; the perceptual match above is the better guide."
        )
    if fraction < 0.005:
        return "No visible differences - the two look like the same file's content."
    if fraction < 0.05:
        return (
            f"{percent:.1f}% of the frame differs, in small patches - typically a "
            "signature, a watermark or a small caption."
        )
    return (
        f"{percent:.0f}% of the frame differs - large enough to be a real edit: added "
        "text, a censoring bar, a logo, or a retouch."
    )
