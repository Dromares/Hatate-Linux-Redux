"""Canonical ids and display names for reverse-image-search engines.

Kept in one place so Settings, the search orchestrator, and the
right-click "search with a specific engine" menu cannot drift apart on
what the valid names are.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Dict, Optional, Tuple

if TYPE_CHECKING:
    from core.config import Settings

# Ids stored in Settings and passed as override_engine. Stable; do not
# rename without a config migration.
IQDB = "iqdb"
SAUCENAO = "saucenao"
ASCII2D = "ascii2d"
TRACEMOE = "tracemoe"
IQDB3D = "iqdb3d"
GOOGLE_IMAGES = "googleimages"
GOOGLE_LENS = "googlelens"
YANDEX = "yandex"
PAWCHIVE = "pawchive"
PAWCHIVE_INDEX = "pawchiveindex"

# The two engines that participate in the primary / secondary order.
# Extra engines (ascii2d, trace.moe, IQDB 3D, Google Images, Google
# Lens, Yandex) are independent toggles and always run in parallel with that
# pair when enabled.
PRIMARY_ENGINES: Tuple[str, ...] = (IQDB, SAUCENAO)

ALL_ENGINES: Tuple[str, ...] = (
    IQDB, SAUCENAO, ASCII2D, TRACEMOE, IQDB3D, GOOGLE_IMAGES, GOOGLE_LENS, YANDEX, PAWCHIVE,
    PAWCHIVE_INDEX,
)

# What MatchCandidate.engine and the Engine column show. Distinct from
# the id so a row that came from IQDB 3D does not look like it came from
# regular IQDB.
ENGINE_LABELS: Dict[str, str] = {
    IQDB: "IQDB",
    SAUCENAO: "SauceNAO",
    ASCII2D: "ascii2d",
    TRACEMOE: "trace.moe",
    IQDB3D: "IQDB 3D",
    GOOGLE_IMAGES: "Google Images",
    GOOGLE_LENS: "Google Lens",
    YANDEX: "Yandex",
    PAWCHIVE: "Pawchive",
    PAWCHIVE_INDEX: "Pawchive index",
}


# Engines whose similarity is a MEASUREMENT of how alike two pictures
# are. The rest score ordinally - a position in a list dressed up as a
# percentage - because the service returns no similarity at all:
# ascii2d, Google Images, Google Lens and Yandex all say so in their modules.
#
# The distinction matters wherever a number is compared against a
# threshold. Google Lens's first result is always 80%, so on a "fall
# back below 75%" setting it satisfied the threshold on every image it
# ran - suppressing the fallback even when the engines that DO measure
# had found nothing at all.
# Pawchive is the strongest of them: its lookup is by the file's SHA-256,
# so a result is the same bytes - a certain 100%, not an estimate.
MEASURES_SIMILARITY: Tuple[str, ...] = (
    IQDB, SAUCENAO, IQDB3D, TRACEMOE, PAWCHIVE, PAWCHIVE_INDEX,
)

# Engines that answer from this machine and make no request at all. They
# owe no host a pause, so the search pacing never waits on their account.
LOCAL_ENGINES: Tuple[str, ...] = (PAWCHIVE_INDEX,)

_MEASURED_LABELS = frozenset(ENGINE_LABELS[e].lower() for e in MEASURES_SIMILARITY)


def reports_real_similarity(engine_label: Optional[str]) -> bool:
    """Whether a candidate's similarity means what it says.

    Takes the LABEL, because that is what MatchCandidate.engine holds.
    An unknown label counts as ordinal: a number that cannot be placed
    should not be allowed to satisfy a threshold.
    """
    return (engine_label or "").strip().lower() in _MEASURED_LABELS


def label_for(engine_id: str) -> str:
    return ENGINE_LABELS.get(engine_id, engine_id)


def effective_primary(configured: str) -> str:
    """The configured primary engine, or IQDB if it is not one of the two
    that take part in the primary/secondary order.

    A config carrying something unexpected here - hand-edited, or written
    by an older build - must not make the opposite-engine action refuse to
    do anything.
    """
    return configured if configured in PRIMARY_ENGINES else IQDB


def opposite_engine(current: Optional[str], configured_primary: str) -> str:
    """Whichever of IQDB/SauceNAO did NOT produce `current`.

    `current` is a MatchCandidate.engine, which is a LABEL ("SauceNAO")
    rather than an id, and may carry stray whitespace - so it is compared
    case-insensitively after stripping.

    A row with no match has no current engine to flip. Rather than refuse
    the whole request over that, it gets the opposite of the configured
    primary, which is the same thing the user is asking for: try the
    engine that has not been tried. Refusing those outright was a real
    bug - running IQDB over some unsearched images is an ordinary thing to
    want once SauceNAO's daily allowance is spent.
    """
    fallback = SAUCENAO if effective_primary(configured_primary) == IQDB else IQDB
    if not current or not current.strip():
        return fallback
    return SAUCENAO if current.strip().lower() == IQDB else IQDB


def pipeline_summary(settings: "Settings") -> str:
    """Returns a human-readable description of the engine pipeline.

    The pipeline is built from the same logic as `_engine_waves` in
    engine_runner.py, but described as a sentence rather than executed.
    """
    primary = effective_primary(settings.primary_engine)
    secondary = SAUCENAO if primary == IQDB else IQDB
    mode = settings.secondary_engine_mode if settings.secondary_engine_mode in (
        "always", "fallback", "disabled",
    ) else "always"

    # Build extras list in the same order as _engine_waves
    extras = []
    if settings.enable_ascii2d:
        extras.append(ASCII2D)
    if settings.enable_tracemoe:
        extras.append(TRACEMOE)
    if settings.enable_iqdb3d:
        extras.append(IQDB3D)
    if settings.enable_google_images:
        extras.append(GOOGLE_IMAGES)
    if settings.enable_google_lens:
        extras.append(GOOGLE_LENS)
    if getattr(settings, "enable_yandex", False):
        extras.append(YANDEX)

    deferred = extras if settings.extras_only_as_fallback else []
    upfront = [] if settings.extras_only_as_fallback else extras

    # Pawchive always runs up front
    if getattr(settings, "enable_pawchive_index", False):
        upfront = [PAWCHIVE_INDEX, *upfront]
    if getattr(settings, "enable_pawchive", False):
        upfront = [PAWCHIVE, *upfront]

    def label(eid: str) -> str:
        return ENGINE_LABELS.get(eid, eid)

    # Build wave descriptions
    wave_parts = []
    if mode == "always":
        wave1 = [primary, secondary, *upfront]
        wave2 = deferred
        wave_parts.append(" → ".join(label(e) for e in wave1))
        if wave2:
            threshold = settings.fallback_below_similarity
            if threshold > 0:
                wave_parts.append(f"(if best match < {threshold:.0f}%) " + ", ".join(label(e) for e in wave2))
            else:
                wave_parts.append("(fallback) " + ", ".join(label(e) for e in wave2))
    elif mode == "fallback":
        wave1 = [primary, *upfront]
        wave2 = [secondary, *deferred]
        wave_parts.append(" → ".join(label(e) for e in wave1))
        if wave2:
            threshold = settings.fallback_below_similarity
            if threshold > 0:
                wave_parts.append(f"(if best match < {threshold:.0f}%) " + ", ".join(label(e) for e in wave2))
            else:
                wave_parts.append("(fallback) " + ", ".join(label(e) for e in wave2))
    else:  # disabled
        wave1 = [primary, *upfront]
        wave2 = deferred
        wave_parts.append(" → ".join(label(e) for e in wave1))
        if wave2:
            threshold = settings.fallback_below_similarity
            if threshold > 0:
                wave_parts.append(f"(if best match < {threshold:.0f}%) " + ", ".join(label(e) for e in wave2))
            else:
                wave_parts.append("(fallback) " + ", ".join(label(e) for e in wave2))

    return " → ".join(wave_parts)
