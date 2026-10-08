"""Colors tags in the tag list to match Hydrus's own namespace color
convention, so the app's tag panel looks/feels consistent with what
you're used to seeing inside Hydrus itself.

IMPORTANT CAVEAT: Hydrus's Client API has no endpoint to fetch a user's
actual configured namespace colors (as of writing, this is an open,
unimplemented feature request - hydrusnetwork/hydrus#915), and Hydrus
lets users recolor namespaces per-install under options > tag
presentation. So there's no single "correct" value to pull automatically.

What's below are Hydrus's commonly-cited DEFAULT namespace colors,
sourced from a Hydrus developer's own description (creator=red,
character=green) plus a third-party Hydrus-compatible client's
explicitly-documented default palette. If your own Hydrus has been
recolored, override these in Settings > Tag Namespaces to match.
"""
from __future__ import annotations

from typing import Optional

from .config import Settings
from .models import Tag

# Hydrus's own booru-style namespace, plus the common booru-side spelling
# that means the same thing (e.g. booru's "artist:" is Hydrus/PTR's
# "creator:") mapped to the same color, so this works out of the box even
# without the namespace remap feature turned on.
DEFAULT_NAMESPACE_COLORS = {
    "creator": "#bb1800",
    "artist": "#bb1800",
    "character": "#00b401",
    "person": "#008f00",
    "series": "#bb2cb9",
    "copyright": "#bb2cb9",
    "studio": "#941100",
    "meta": "#676767",
}

# Hydrus also colors unnamespaced tags distinctly from its default text
# color - this is the closest documented fallback value found.
UNNAMESPACED_COLOR = "#0088fb"


def get_tag_color(tag: Tag, settings: Settings) -> Optional[str]:
    """Returns a hex color string for this tag, or None to use the
    default text color (no special namespace color applies)."""
    if not settings.enable_hydrus_tag_colors:
        return None

    overrides = settings.tag_namespace_colors
    if tag.namespace:
        if tag.namespace in overrides:
            return overrides[tag.namespace]
        return DEFAULT_NAMESPACE_COLORS.get(tag.namespace)

    return overrides.get("", UNNAMESPACED_COLOR)
