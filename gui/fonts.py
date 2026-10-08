"""Registers the bundled type with Qt before the stylesheet needs it.

Five families, under `resources/fonts/`: Source Serif 4 (display heads),
Space Grotesk (body), JetBrains Mono (mono-caps labels and most of the
Ryoku status glyphs), and two narrow subsets built for this app - Ryoku
Kanji (the Empty-state ghost mark, Tier 3) and Ryoku Status (the two
status glyphs, review's `◐` and not-found's `○`, that JetBrains Mono's
own cmap is missing - confirmed by inspection, not assumed). Registering
Ryoku Kanji now even though nothing draws it yet keeps this the one place
the app ever has to learn where the font files live.

`QFontDatabase.addApplicationFont` returns -1 for a path that doesn't
resolve to a loadable font - missing file, corrupt data - rather than
raising. A font-less launch should still launch, the same reasoning
`main.py`'s icon-load check two lines above this call already uses, so a
failure here is logged and skipped rather than treated as fatal.
"""
from __future__ import annotations

from pathlib import Path
from typing import List

from core.applog import get_logger
from core.paths import RESOURCES_DIR

log = get_logger("fonts")

FONTS_DIR = RESOURCES_DIR / "fonts"


def register_fonts(fonts_dir: Path = FONTS_DIR) -> List[int]:
    """Registers every `.woff2` under `fonts_dir` and returns the ids Qt
    handed back, one per file, in the order found - a missing or corrupt
    file contributes -1 rather than stopping the rest from loading.

    Takes the directory as a parameter, rather than reading the module
    constant directly, so a test can point it at an empty or missing
    directory without touching the real bundle.
    """
    from PyQt6.QtGui import QFontDatabase

    ids = []
    for font_path in sorted(fonts_dir.glob("*/*.woff2")):
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        if font_id == -1:
            log.warning("Failed to register font: %s", font_path)
        ids.append(font_id)
    return ids
