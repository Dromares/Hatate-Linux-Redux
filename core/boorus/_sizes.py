"""Reading image dimensions off a booru post page.

Gelbooru, Safebooru, Yande.re and Konachan all descend from Danbooru
1.x, so they share a Statistics sidebar of the form:

    Id: 3484690
    Posted: 2016-12-23 13:27:09
    Size: 1232x918
    Rating: Explicit

This lives in one place because the interesting part - which character
sits between the two numbers - is a property of that shared lineage, not
of any one site. It renders as ASCII "x" in some views and as &times;
(U+00D7) in others, and a pattern matching only "x" silently returns no
dimensions at all: no error, no warning, just a size comparison that
quietly shows nothing. Fixing that per-site would mean fixing it
repeatedly and forgetting one.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

# Accepts every separator these sites have been seen to use, plus the
# spacing variants that appear when the numbers sit in separate elements.
SIZE_RE = re.compile(r"\bSize:\s*(\d[\d,]*)\s*[x\u00d7\u2715\u2716*]\s*(\d[\d,]*)", re.IGNORECASE)

# Anything of the form "1500x2000" - a looser fallback for pages that
# state the dimensions without the "Size:" label, e.g. inside a
# download link's text.
BARE_DIMENSIONS_RE = re.compile(r"\b(\d{2,6})\s*[x\u00d7\u2715\u2716]\s*(\d{2,6})\b")

# Used only to explain a miss in the log.
SIZE_CONTEXT_RE = re.compile(r".{0,40}\bSize:.{0,40}", re.IGNORECASE | re.DOTALL)


def _to_int(raw: str) -> Optional[int]:
    try:
        return int(raw.replace(",", ""))
    except ValueError:
        return None


def parse_size_label(text: str) -> Tuple[Optional[int], Optional[int]]:
    """Dimensions from a "Size: WxH" label, or (None, None)."""
    match = SIZE_RE.search(text or "")
    if not match:
        return None, None
    return _to_int(match.group(1)), _to_int(match.group(2))


def parse_bare_dimensions(text: str) -> Tuple[Optional[int], Optional[int]]:
    """Dimensions from an unlabelled "1500x2000", for pages that state
    them without the Statistics label.

    Deliberately a SECOND choice: without the label there's nothing
    tying the numbers to the image, so on the wrong slice of text this
    could pick up something unrelated. Callers should hand it a narrow
    region - a single link's text, not a whole page.
    """
    match = BARE_DIMENSIONS_RE.search(text or "")
    if not match:
        return None, None
    return _to_int(match.group(1)), _to_int(match.group(2))


def describe_size_context(text: str) -> str:
    """What the page actually says around "Size:", for logging a miss.
    Guessing at this markup from outside has already produced one wrong
    fix, so a failure should report what it saw."""
    match = SIZE_CONTEXT_RE.search(text or "")
    if not match:
        return "<no 'Size:' on the page>"
    return " ".join(match.group(0).split())
