"""Heuristics for detecting whether an image appears to have been
artificially upscaled from a smaller original, rather than being genuine
native resolution.

Two independent signals, each with real limitations - neither is proof,
both are presented to the user as heuristics:

1. compare_to_source() - if we've found what appears to be the true
   source image via search and it's meaningfully smaller than the local
   copy, the local copy was probably upscaled from it (or a similar
   original) at some point. Reliable IF the matched source really is the
   original - but a search match is never a certainty.

2. detect_naive_upscale() - downscales the image and scales it back up
   with common interpolation filters, then compares the result to the
   original - specifically in regions with genuine fine detail (edges,
   linework, texture), not a flat whole-image average. Simple ("naive")
   upscaling via bicubic/bilinear/nearest-neighbor is close to reversible
   this way, so a very low difference after the round-trip, in the areas
   that actually have detail to lose, suggests the image may have gone
   through exactly this kind of resize. Flat/smooth regions (solid fills,
   simple gradients, cel shading - common in anime/manga-style art) are
   deliberately excluded from the comparison: they reconstruct almost
   perfectly under simple interpolation regardless of whether the image
   was ever upscaled, so including them would dilute the signal and
   cause false positives on exactly the kind of art this app is built
   around. This still does NOT reliably catch AI upscalers (ESRGAN,
   waifu2x, Real-ESRGAN, etc.), which synthesize new detail rather than
   simply interpolating - those can pass this check even though the
   image genuinely was upscaled.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
from PIL import Image

from .applog import get_logger

log = get_logger("upscale_detect")

# Common upscale factors and resize filters to test in the self-consistency check.
CANDIDATE_FACTORS = (1.5, 2.0, 3.0, 4.0)
CANDIDATE_FILTERS = {
    "Bicubic": Image.BICUBIC,  # type: ignore[attr-defined]  # Pillow stub gap: constant/return type missing or narrower than the object PIL actually returns
    "Bilinear": Image.BILINEAR,  # type: ignore[attr-defined]  # Pillow stub gap: constant/return type missing or narrower than the object PIL actually returns
    "Nearest-neighbor": Image.NEAREST,  # type: ignore[attr-defined]  # Pillow stub gap: constant/return type missing or narrower than the object PIL actually returns
    "Lanczos": Image.LANCZOS,  # type: ignore[attr-defined]  # Pillow stub gap: constant/return type missing or narrower than the object PIL actually returns
}

# Average per-pixel difference thresholds (0-255 scale) for the
# self-consistency round-trip test, measured only over detail/edge pixels.
STRONG_MATCH_THRESHOLD = 4.0
POSSIBLE_MATCH_THRESHOLD = 10.0

# Only the pixels at or above this percentile of local edge/gradient
# magnitude are counted as "detail" and included in the comparison - the
# rest (flat/smooth areas) are excluded since they're uninformative.
DETAIL_PERCENTILE = 70.0

# Minimum gradient magnitude (0-255 grayscale intensity scale) for a
# pixel to even be considered as a detail candidate at all, before the
# percentile cut - filters out flat regions before they can skew the
# percentile threshold itself.
MIN_GRADIENT = 2.0

# If less than this fraction of the image qualifies as "detail" (i.e. the
# image is almost entirely flat/simple), there isn't enough signal to
# test reliably.
MIN_DETAIL_FRACTION = 0.02

# Cap for the self-consistency test's working resolution - see the
# comment in detect_naive_upscale() for why this matters. 2000px is
# plenty of resolution for the interpolation-pattern signal to still be
# clearly detectable while keeping per-operation memory bounded.
MAX_ANALYSIS_DIMENSION = 2000

# Absolute ceiling on the ORIGINAL image's pixel count before we'll even
# attempt to open/decode it for this check - a universal safety net for
# formats (like PNG) where the decoder can't downscale during decoding,
# so MAX_ANALYSIS_DIMENSION alone can't help avoid a huge initial decode.
# ~40 megapixels comfortably covers any realistic booru/art image while
# keeping the worst-case decode small enough not to risk exhausting
# memory in a constrained environment.
HARD_PIXEL_LIMIT = 40_000_000

# Local file must be at least this much bigger than the matched source to
# be flagged - small differences are normal (re-encoding, minor crops).
SOURCE_COMPARISON_THRESHOLD_PERCENT = 15.0

# Known AI upscaling tool/model names that sometimes appear in an image's
# EXIF Software tag or PNG text chunks - kept intentionally conservative
# (distinctive full names, not short/generic fragments) to avoid false
# matches against unrelated text. When this survives in the file, it's a
# definitive answer, not a heuristic - but most tools don't write it, and
# it's trivially lost by re-saving/re-compressing, so absence proves
# nothing either way.
KNOWN_AI_UPSCALER_SIGNATURES = (
    "waifu2x", "real-esrgan", "realesrgan", "esrgan", "srmd", "realsr",
    "anime4k", "topaz gigapixel", "gigapixel ai", "letsenhance", "upscayl",
    "cupscale", "chainner", "swinir", "stable diffusion upscale", "ultimate sd upscale",
)


@dataclass
class SourceComparisonResult:
    flagged: bool
    local_size: Tuple[int, int]
    source_size: Tuple[int, int]
    message: str


@dataclass
class SelfConsistencyResult:
    best_factor: Optional[float] = None
    best_filter: Optional[str] = None
    best_diff: Optional[float] = None
    detail_fraction: Optional[float] = None
    confidence: str = "none"  # "none" | "possible" | "likely"
    message: str = ""


@dataclass
class UpscaleCheckResult:
    source_comparison: Optional[SourceComparisonResult] = None
    self_consistency: Optional[SelfConsistencyResult] = None
    metadata_signature: Optional[str] = None  # a known AI upscaler tool name found in EXIF/metadata, if any


def check_upscaler_metadata(path: str) -> Optional[str]:
    """Checks the image's own metadata (EXIF Software/Artist/ImageDescription
    tags, PNG text chunks) for a known AI upscaling tool or model name.
    This is a definitive answer when it's there - a literal declared tool
    name, not a guess - but most tools don't write this, and it's
    trivially lost the moment the image is re-saved or re-compressed, so
    finding nothing here proves nothing either way."""
    try:
        with Image.open(path) as im:
            texts = []

            # PNG text chunks (tEXt/iTXt/zTXt) land in im.info.
            for value in (im.info or {}).values():
                if isinstance(value, str):
                    texts.append(value)
                elif isinstance(value, bytes):
                    texts.append(value.decode("utf-8", errors="ignore"))

            # EXIF Software / Artist / ImageDescription tags (JPEG/TIFF).
            try:
                exif = im.getexif()
                for tag_id in (270, 305, 315):  # ImageDescription, Software, Artist
                    value = exif.get(tag_id)
                    if value:
                        texts.append(str(value))
            except Exception:
                pass

            combined = " ".join(texts).lower()
            for signature in KNOWN_AI_UPSCALER_SIGNATURES:
                if signature in combined:
                    return (
                        f"Image metadata mentions \"{signature}\" - a known AI upscaling "
                        "tool/model name."
                    )
    except (OSError, ValueError) as exc:
        log.warning("Could not read metadata from %s: %s", path, exc)
    return None


def compare_to_source(
    local_width: int, local_height: int,
    source_width: int, source_height: int,
    threshold_percent: float = SOURCE_COMPARISON_THRESHOLD_PERCENT,
) -> SourceComparisonResult:
    """Flags when the local image is meaningfully larger than what appears
    to be its true source - a strong, simple signal it was probably
    upscaled from it (or a similarly-sized original)."""
    if not source_width or not source_height:
        return SourceComparisonResult(
            flagged=False, local_size=(local_width, local_height), source_size=(0, 0),
            message="No source dimensions available to compare against.",
        )

    width_ratio = local_width / source_width
    height_ratio = local_height / source_height
    gain_percent = (max(width_ratio, height_ratio) - 1.0) * 100
    flagged = (
        gain_percent >= threshold_percent
        and local_width >= source_width
        and local_height >= source_height
    )

    if flagged:
        message = (
            f"Local file ({local_width}\u00d7{local_height}) is {gain_percent:.0f}% larger than "
            f"the matched source ({source_width}\u00d7{source_height}) - likely upscaled from it."
        )
    else:
        message = (
            f"Local file ({local_width}\u00d7{local_height}) is not meaningfully larger than "
            f"the matched source ({source_width}\u00d7{source_height})."
        )

    return SourceComparisonResult(
        flagged=flagged, local_size=(local_width, local_height),
        source_size=(source_width, source_height), message=message,
    )


def _detail_mask(im: Image.Image, percentile: float = DETAIL_PERCENTILE) -> np.ndarray:
    """Boolean mask of pixels with genuine local detail (edges/texture),
    from grayscale intensity gradients. Flat/smooth regions - very common
    in anime/manga-style art as solid fills, simple gradients, and cel
    shading - are excluded, since they reconstruct near-perfectly under
    simple interpolation whether or not the image was ever upscaled.

    The percentile threshold is computed only among pixels that have
    *some* real gradient (> MIN_GRADIENT), not the whole image - flat
    regions contribute a large mass of exact zeros that would otherwise
    drag a whole-image percentile down to ~0, making "magnitude >=
    threshold" match nearly every pixel, including the flat ones."""
    gray = np.asarray(im.convert("L"), dtype=np.float32)
    gy, gx = np.gradient(gray)
    magnitude = np.sqrt(gx * gx + gy * gy)

    nonzero = magnitude[magnitude > MIN_GRADIENT]
    if nonzero.size == 0:
        return np.zeros_like(magnitude, dtype=bool)  # perfectly flat image, no detail at all

    threshold = max(float(np.percentile(nonzero, percentile)), MIN_GRADIENT)
    return magnitude >= threshold


def _masked_mean_abs_diff(im_a: Image.Image, im_b: Image.Image, mask: np.ndarray) -> float:
    """Mean absolute per-pixel difference (averaged across RGB channels),
    restricted to the True positions in `mask`."""
    arr_a = np.asarray(im_a, dtype=np.float32)
    arr_b = np.asarray(im_b, dtype=np.float32)
    per_pixel_diff = np.abs(arr_a - arr_b).mean(axis=2)  # average across channels
    masked = per_pixel_diff[mask]
    if masked.size == 0:
        return float(per_pixel_diff.mean())  # no detail pixels at all - fall back to overall mean
    return float(masked.mean())


def detect_naive_upscale(path: str) -> SelfConsistencyResult:
    """Tests whether downscaling then re-upscaling the image (with common
    interpolation filters) closely reconstructs the original, measured
    only in regions with genuine detail - a signal the image may have
    gone through exactly this kind of simple resize. Doesn't reliably
    catch AI upscalers; caller should treat the result as a heuristic,
    not a verdict."""
    try:
        with Image.open(path) as im:
            # Check dimensions from the header BEFORE decoding any pixel
            # data - im.size is cheap (no decode), but im.convert("RGB")
            # forces a full decode at the image's TRUE resolution. Doing
            # that first, then downscaling afterward, meant a genuinely
            # huge image could already exhaust memory and crash natively
            # (a SIGSEGV, which Python can't catch or recover from) during
            # the initial decode - before any of our capping logic ever
            # got a chance to run.
            true_w, true_h = im.size

            if true_w < 64 or true_h < 64:
                return SelfConsistencyResult(message="Image too small to meaningfully test.")

            if true_w * true_h > HARD_PIXEL_LIMIT:
                log.warning(
                    "%s is %dx%d (%.0fMP), over the %.0fMP safety limit - "
                    "skipping the upscale self-consistency check entirely",
                    path, true_w, true_h, true_w * true_h / 1e6, HARD_PIXEL_LIMIT / 1e6,
                )
                return SelfConsistencyResult(
                    message=(
                        f"Image is too large ({true_w}\u00d7{true_h}) to safely analyze - "
                        "skipped to avoid a potential crash."
                    ),
                )

            if max(true_w, true_h) > MAX_ANALYSIS_DIMENSION:
                # Ask the decoder to produce an already-downscaled image
                # during decoding itself where supported (JPEG) - this
                # avoids ever materializing a huge full-resolution pixel
                # buffer just to immediately shrink it back down. Not all
                # formats support this (notably PNG doesn't); harmless
                # no-op if so, and the hard pixel limit above plus the
                # resize below still apply either way.
                try:
                    im.draft("RGB", (MAX_ANALYSIS_DIMENSION, MAX_ANALYSIS_DIMENSION))
                except Exception:
                    pass

            im = im.convert("RGB")  # type: ignore[assignment]  # Pillow stub gap: constant/return type missing or narrower than the object PIL actually returns

            if max(im.size) > MAX_ANALYSIS_DIMENSION:
                # draft() only gives an approximate size (rounds to
                # whatever block size the decoder can cheaply produce) or
                # wasn't supported for this format at all - finish getting
                # down to the target size with a normal resize, hopefully
                # starting from something already much smaller than the
                # true original.
                scale = MAX_ANALYSIS_DIMENSION / max(im.size)
                im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)  # type: ignore[assignment, attr-defined]  # Pillow stub gap: constant/return type missing or narrower than the object PIL actually returns

            if im.size != (true_w, true_h):
                log.debug(
                    "Downscaled %s from %dx%d to %dx%d for upscale analysis (memory safety)",
                    path, true_w, true_h, im.width, im.height,
                )

            orig_w, orig_h = im.size

            detail_mask = _detail_mask(im)
            detail_fraction = float(detail_mask.mean())
            log.debug("%s: %.1f%% of pixels qualify as 'detail' for the upscale check", path, detail_fraction * 100)

            if detail_fraction < MIN_DETAIL_FRACTION:
                return SelfConsistencyResult(
                    detail_fraction=detail_fraction,
                    message=(
                        "This image is almost entirely flat/simple content (little fine "
                        "detail to test against) - not enough signal to reliably check for upscaling."
                    ),
                )

            best = SelfConsistencyResult(detail_fraction=detail_fraction)
            for factor in CANDIDATE_FACTORS:
                small_w = max(1, round(orig_w / factor))
                small_h = max(1, round(orig_h / factor))
                if small_w < 32 or small_h < 32:
                    continue
                downscaled = im.resize((small_w, small_h), Image.LANCZOS)  # type: ignore[attr-defined]  # Pillow stub gap: constant/return type missing or narrower than the object PIL actually returns

                for filter_name, filter_val in CANDIDATE_FILTERS.items():
                    reconstructed = downscaled.resize((orig_w, orig_h), filter_val)
                    diff = _masked_mean_abs_diff(im, reconstructed, detail_mask)
                    if best.best_diff is None or diff < best.best_diff:
                        best.best_diff = diff
                        best.best_factor = factor
                        best.best_filter = filter_name

            if best.best_diff is None:
                return SelfConsistencyResult(message="Image too small to meaningfully test.")

            if best.best_diff <= STRONG_MATCH_THRESHOLD:
                best.confidence = "likely"
                best.message = (
                    f"Likely upscaled ~{best.best_factor:.1f}\u00d7 using {best.best_filter} "
                    f"interpolation (in detail regions, round-trip reconstruction very close, "
                    f"avg diff {best.best_diff:.1f}/255)."
                )
            elif best.best_diff <= POSSIBLE_MATCH_THRESHOLD:
                best.confidence = "possible"
                best.message = (
                    f"Possibly upscaled ~{best.best_factor:.1f}\u00d7 using {best.best_filter} "
                    f"interpolation (in detail regions, avg diff {best.best_diff:.1f}/255) - "
                    "not a strong signal, could be coincidental."
                )
            else:
                best.confidence = "none"
                best.message = (
                    f"No strong signal of simple upscaling (best round-trip diff in detail "
                    f"regions: {best.best_diff:.1f}/255). Note this test can't detect AI-based "
                    "upscaling (ESRGAN, waifu2x, etc.), which doesn't leave the same signature."
                )
            return best
    except (OSError, ValueError) as exc:
        log.warning("Could not analyze %s for upscaling: %s", path, exc)
        return SelfConsistencyResult(message=f"Could not analyze image: {exc}")
