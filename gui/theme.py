"""The application's look: two palettes and one stylesheet.

Ryoku: a single ink colour (bone `#cdc4ba` on charcoal in dark, charcoal ink
on paper `#e8e1d2` in light) drawn at a handful of opacities instead of four
hues, printed flat - no gradients, no drop shadows, no rounded corners
anywhere. The old accent family (`primary`/`primary_lo`/`primary_hi`, a
lifted steel blue) is gone; the one thing on screen that needs to look
urgent now gets a full foreground/background swap instead of a colour -
`stamp_bg`/`stamp_fg` - reused for the primary button, the active segment,
and a selected row alike.

Match-quality status no longer owns a hue budget at all: `STATUS_GLYPHS`/
`STATUS_WEIGHTS` below replace the old `STATUS_HUES` table, encoding a
status as a glyph silhouette (shape) plus an ink-ramp opacity (weight) -
two channels that survive colour-blindness by construction, instead of
one hue a status chip used to own outright.

DAN-149: the `success`/`warning`/`danger` keys that used to back the
Activity engine row's on/off chips (`gui/widgets.py`'s `chip()` factory)
are gone outright, not just unwired from that one call site. They had
exactly one live consumer - QLabel#ChipGood via that chip - and no other
`.py` file ever set a `Danger`/`Warning`/`ChipPoor`/`ChipBad` object name,
so keeping the palette keys around would have left a hue a future change
could silently re-wire. The engine row now draws EngineChipOn/Off: an
outlined hairline tag with no background fill, carried by ink tier plus
border weight exactly like tokens.css's `.engine-tag` - not the inverted
`stamp_bg`/`stamp_fg` treatment, which stays reserved for primary actions
and the active tab/segment.

No Qt is imported at module level: the palettes and the sheet are just
data and a format string, which keeps this importable (and testable)
without a QApplication. `resolve_mode` is the one function that needs Qt,
and it imports it when called.
"""

DARK = {
    'page': '#0d0d0d',
    'card': '#141414',
    'card_alt': '#090909',
    # The one accent: a full fg/bg swap instead of a hue, used for the
    # primary button, the active segment, and a selected row alike.
    'stamp_bg': '#cdc4ba',
    'stamp_fg': '#0d0d0d',
    # The one ink colour, at four opacities. Alpha is literal CSS-style
    # 0.0-1.0 here, not Qt's C++-API 0-255 - confirmed empirically that
    # QSS's rgba() parses the fraction correctly, so these are a direct
    # transcription of tokens.css, not a re-derived value.
    'ink_100': 'rgba(205,196,186,1)',
    'ink_65': 'rgba(205,196,186,0.65)',
    # DAN-224: a dedicated border tier (ink_46) plus a louder one for its
    # hover/focus state (ink_58) - NOT ink_45 reused. ink_45 falls short of
    # WCAG 1.4.11's 3:1 non-text floor against card_alt (2.94:1 light,
    # measured) and reusing it would couple border visibility to
    # disabled-text dimming, the same incidental coupling that let
    # ink_18 drift under the floor unnoticed. Re-measured against this
    # file's live DARK/LIGHT dicts, not taken on faith from the audit:
    # the audit's own "dark 0.44" came out at 2.97:1 against card_alt
    # here, under the floor, so the resting tier landed at 0.46 instead.
    # See the DAN-212 audit document: /DAN/issues/DAN-212#document-border-audit
    'ink_58': 'rgba(205,196,186,0.58)',
    'ink_46': 'rgba(205,196,186,0.46)',
    'ink_45': 'rgba(205,196,186,0.45)',
    'ink_28': 'rgba(205,196,186,0.28)',
    # ink_18's own value is unchanged (DAN-224). 12 of its 15 border
    # consumers measured under 3:1 here and were moved to ink_46/ink_58
    # above instead of raising this tier. The remaining 3 stay deliberately
    # quiet - most notably QLabel#EngineChipOff, whose ink_18 vs
    # EngineChipOn's ink_46 IS the on/off encoding for the Activity engine
    # chips (see the stylesheet comment at EngineChipOn/Off). Do not raise
    # ink_18 itself to "fix" a future contrast complaint without re-reading
    # the DAN-212 document first - it names exactly which of the 15 sites
    # were left alone and why.
    'ink_18': 'rgba(205,196,186,0.18)',
    # Deliberately the same literal in both DARK and LIGHT: WipeView's
    # split line (gui/compare_dialog.py's `_wipe_line_colour`) is drawn
    # over a photograph of unknown brightness, not over app chrome, so
    # it must not flip with the theme the way every other token here
    # does. `stylesheet()` never interpolates `{on_accent}` - the one
    # consumer reaches it via a direct `palette(mode)["on_accent"]`
    # dict subscript in Python, not the format string - so a sweep for
    # tokens dead to the stylesheet will find this one and be wrong.
    # Removed once on exactly that reasoning (DAN-185) and broke
    # `main`'s CI the same day with a `KeyError` (DAN-162); do not
    # remove it again without checking every `palette(...)[...]`
    # subscript in the repo, not just `stylesheet()`.
    'on_accent': '#ffffff',
}

LIGHT = {
    'page': '#e8e1d2',
    'card': '#f2ede1',
    'card_alt': '#ddd2bd',
    'stamp_bg': '#211d17',
    'stamp_fg': '#f6f1e6',
    # Not a mechanical inversion of dark's alphas - ink on paper subtracts
    # light rather than adding it, so every tier here is independently
    # re-measured against the paper colour (see PLAN.md's contrast table).
    'ink_100': 'rgba(33,29,23,1)',
    'ink_65': 'rgba(33,29,23,0.70)',
    # ink_58/ink_46: DAN-224's dedicated border tiers - see DARK above for
    # why they exist. Independently re-measured against paper, same as
    # every other tier here: 0.51/0.62 clear 3:1 on every background the
    # must-raise selectors use (worst case card_alt, 3.02:1/4.05:1).
    'ink_58': 'rgba(33,29,23,0.62)',
    'ink_46': 'rgba(33,29,23,0.51)',
    'ink_45': 'rgba(33,29,23,0.50)',
    'ink_28': 'rgba(33,29,23,0.45)',
    # ink_18: unchanged, still deliberately quiet in the same 3 places - see
    # DARK above.
    'ink_18': 'rgba(33,29,23,0.32)',
    # Deliberately identical to DARK's on_accent - see there.
    'on_accent': '#ffffff',
}

MODES = ('system', 'dark', 'light')

# The three registered voices (gui/fonts.py), named first in each stack
# purely as a safety net if a file fails to register - Qt just falls
# through to the next name, the same reasoning tokens.css's own stacks
# use. Mode-independent: unlike the palette, type doesn't change with
# dark/light, so these are plain constants, not DARK/LIGHT keys.
FONT_SERIF = "'Source Serif 4', Georgia, serif"
FONT_SANS = "'Space Grotesk', 'Helvetica Neue', Arial, sans-serif"
FONT_MONO = "'JetBrains Mono', 'DejaVu Sans Mono', 'Courier New', monospace"

# One glyph per status, used as a scannable silhouette down a column
# instead of a hue - a shape reads the same to every eye, which a tint
# never did. The keys are MatchStatus values, plus the Sent column's two
# and the Similarity column's 'estimated' (see similarity_display.py).
#
# Only six silhouettes are both distinct and guaranteed to render:
# STATUS_GLYPH_FONTS below routes '◐'/'○' through the bundled
# Ryoku Status face because JetBrains Mono's own cmap is missing them
# (confirmed by inspection - see resources/fonts/ryoku-status/SOURCE.txt).
# Nine status keys therefore share six shapes on purpose, the same way
# STATUS_HUES used to give 'good'/'sent' the identical hue and
# 'not_searched'/'estimated' the identical grey - the chip's adjacent
# word (chip_label) still tells them apart; the glyph is the fast-scan
# channel, not the only one.
STATUS_GLYPHS = {
    'not_searched': '·',   # ·  absence of information
    'good': '●',           # ●  confirmed positive
    'poor': '◐',           # ◐  needs a decision
    'not_found': '○',      # ○  needs a decision
    'error': '×',          # ×  needs a decision
    'searching': '◐',      # ◐  in motion - same family as 'poor',
                                 #    separated by weight below
    'sent': '●',           # ●  confirmed positive, same as 'good'
    'queued': '◐',         # ◐  pending, same family as 'poor'
    'estimated': '·',      # ·  absence of information, same as
                                 #    'not_searched'
}

# The second channel: an ink-ramp tier rather than a hue. Mirrors
# tokens.css's own rule - ink-100 bold for "needs a decision", ink-65 for
# "routine/done", ink-45 for "inert" - so a status that shares a glyph
# with another (poor/searching/queued all draw '◐') still reads apart by
# weight, and chip_label's text is there as the third, named channel.
#
# DAN-211: 'not_searched'/'estimated' used to sit at 'ink_45', the
# "inert" tier - 3.10:1 (dark) / 3.14:1 (light) against the QTableView's
# `card` surface they actually paint on, both short of WCAG AA's 4.5:1
# body-text floor (ChipDelegate paints this text at 14px regular, so it
# does not qualify for the relaxed large-text threshold). Raised to
# 'ink_65' - 5.12:1 / 5.76:1 - rather than lifting the shared 'ink_45'
# tier itself, which half a dozen unrelated selectors (disabled widgets,
# hairline borders) also read; redefining the tier would have changed
# all of them to chase a contrast floor that only these two statuses
# needed. 'not_searched' and 'estimated' still read apart from
# 'good'/'searching'/'sent'/'queued' by glyph ('·' vs '●'/'◐') even
# though they now share a weight with them - see STATUS_GLYPHS above.
# TestStatusWeightContrast in tests/test_theme.py asserts the 4.5:1
# floor against the live DARK/LIGHT dicts so this can't silently regress
# back to a tier that reads as decoration instead of text.
STATUS_WEIGHTS = {
    'not_searched': 'ink_65',
    'good': 'ink_65',
    'poor': 'ink_100',
    'not_found': 'ink_100',
    'error': 'ink_100',
    'searching': 'ink_65',
    'sent': 'ink_65',
    'queued': 'ink_65',
    'estimated': 'ink_65',
}

# Only the two glyphs JetBrains Mono can't draw need routing to the
# bundled subset; the other four (● × · ▲) stay on whatever font the
# caller is already using.
STATUS_GLYPH_FONT = 'Ryoku Status'
_NEEDS_GLYPH_FONT = frozenset(('poor', 'not_found', 'searching', 'queued'))


# What a chip says. Shorter than the model's own label, because a chip is
# scanned down a column rather than read: at the width this column is
# usually left at, "Found (good)" elides to "Found (..." - which keeps
# the word both statuses share and drops the one that tells them apart.
# The full label stays on the cell's tooltip, and in the filter menus.
CHIP_LABELS = {
    'not_searched': 'Unsearched',
    'good': 'Good',
    'poor': 'Review',
    'not_found': 'Not found',
    'error': 'Error',
    'searching': 'Searching',
    'sent': 'Sent',
    'queued': 'Queued',
    # 'estimated' is deliberately NOT here. Its chip carries the
    # similarity number itself, and chip_label() falls back to the cell's
    # own text for a key it does not know - adding a word for it would
    # replace the number with that word.
}


def chip_label(key, fallback=''):
    return CHIP_LABELS.get(key, fallback)


def status_glyph(key):
    """The glyph for a chip, or None if the key is not one we draw.

    Unknown keys answer None rather than raising: a status added to the
    model and not yet to the table should draw as plain text, not take
    the window down on a repaint. Mode-independent - the shape a status
    draws does not change with the theme, only the ink tier's resolved
    colour does.
    """
    return STATUS_GLYPHS.get(key)


def status_weight(key):
    """The ink-ramp tier ('ink_100'/'ink_65'/'ink_45') a chip's glyph and
    text draw at. 'ink_45' for an unknown key, the same "inert" tier a
    key that draws no glyph at all would read as."""
    return STATUS_WEIGHTS.get(key, 'ink_45')


def status_glyph_font(key):
    """The font family a chip's glyph must be painted in, or None to use
    whatever font the caller already has. Only '◐' (poor/searching/
    queued) and '○' (not_found) need this - see STATUS_GLYPH_FONT's
    docstring-adjacent comment above."""
    return STATUS_GLYPH_FONT if key in _NEEDS_GLYPH_FONT else None


def mono_font(base=None):
    """A QFont carrying the same mono stack FONT_MONO names, for the
    Python-constructed fonts (FontRole, a delegate's painting font) that
    never pass through the stylesheet and so can't rely on QSS's
    comma-separated font-family falling through on its own.
    `QFont.setFamilies` is Qt's own ordered-fallback API - the Python
    equivalent of what the CSS string already does for QSS.
    """
    from PyQt6.QtGui import QFont

    font = QFont(base) if base is not None else QFont()
    font.setFamilies(["JetBrains Mono", "DejaVu Sans Mono", "Courier New"])
    return font


def ink_color(mode, tier):
    """A real QColor for an ink-ramp tier, for the Python-side painting
    code (ChipDelegate, ImageTableModel's ForegroundRole) that can't use
    the stylesheet's `rgba(r,g,b,a)` string directly - QColor(str) does
    not parse CSS function syntax, only '#rrggbb' and SVG names, so a
    naive QColor(palette(mode)[tier]) is silently invalid (isValid() is
    False, the colour paints black). Imports Qt lazily, same reasoning as
    resolve_mode above: this file stays importable without a QApplication.
    """
    from PyQt6.QtGui import QColor

    value = palette(mode)[tier]
    r, g, b, a = value[len('rgba('):-1].split(',')
    return QColor(int(r), int(g), int(b), round(float(a) * 255))


def palette(mode):
    """The colour table for a resolved mode ('dark' or 'light')."""
    return DARK if mode == 'dark' else LIGHT


def resolve_mode(setting):
    """Turns the stored setting into the mode actually to be drawn.

    'dark' and 'light' pin it. 'system' follows the desktop, which Qt6
    reports through the style hints; on a Qt that predates that, or a
    desktop that expresses no preference, dark is the fallback - it is
    what this app defaults to and what it was designed against.
    """
    if setting in ('dark', 'light'):
        return setting
    try:
        from PyQt6.QtGui import QGuiApplication
        from PyQt6.QtCore import Qt
        scheme = QGuiApplication.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Light:
            return 'light'
        if scheme == Qt.ColorScheme.Dark:
            return 'dark'
    except Exception:
        # An older Qt has no colorScheme(), and a headless test run has no
        # style hints at all. Neither is worth failing a launch over.
        pass
    return 'dark'


def stylesheet(mode):
    """The whole application sheet, for a resolved mode."""
    p = palette(mode)
    return """
    QWidget {{
            background: {page};
            color: {ink_100};
            font-size: 14px;
            font-family: {font_sans};
    }}
    QMainWindow, QDialog {{ background: {page}; }}

    /* ---- cards: flat panels, lifted by a hairline rather than a
       shadow or a radius ---- */
    QFrame#Card {{
            background: {card};
    }}

    /* ---- the crash-recovery notice (DAN-660) - same flat-panel shape
       as Card, named separately because it carries its own hairline
       border (an ordinary Card doesn't) so it still reads as "something
       happened" against an otherwise identical page. No new ink tier or
       glyph here - RunBannerGlyph reuses STATUS_GLYPHS['poor']/
       STATUS_WEIGHTS['poor'] exactly, which is why its colour is
       {ink_100} rather than a dedicated token. ---- */
    QFrame#RunBanner {{
            background: {card};
            border: 1px solid {ink_46};
    }}
    QLabel#RunBannerGlyph {{
            font-family: '{glyph_font}', {font_sans};
            font-size: 20px;
            color: {ink_100};
            background: transparent;
    }}
    /* No font-weight here: Space Grotesk (the default QWidget family)
       ships weight 400 only - a declared 600 would silently render as
       400 anyway (DAN-148's TestFontWeightsMatchTheRegisteredFace would
       catch it). ink_100 alone carries the emphasis, same as
       STATUS_WEIGHTS' "ink-100 bold for needs-a-decision" above - that
       pairing is already a colour tier standing in for weight, not a
       literal font-weight declaration. */
    QLabel#RunBannerHead {{
            color: {ink_100};
            background: transparent;
    }}
    QLabel#RunBannerBody {{
            color: {ink_65};
            background: transparent;
    }}
    QLabel#RunBannerDiscard {{
            color: {ink_65};
            background: transparent;
    }}
    QLabel#RunBannerDiscard:hover {{ color: {ink_100}; }}

    QLabel#Heading {{
            font-family: {font_serif};
            font-size: 17px;
            font-weight: 600;
            background: transparent;
    }}

    /* ---- a screen's own title ("Queue.", "Review.") - bigger than a
       card heading and always the first thing on the page. ---- */
    QLabel#ScreenTitle {{
            font-family: {font_serif};
            font-size: 28px;
            font-weight: 600;
            background: transparent;
    }}
    QLabel#Muted, QLabel#Hint {{
            color: {ink_65};
            background: transparent;
    }}
    QLabel#CommitCaption {{
            font-family: {font_mono};
            font-size: 10px;
            color: {ink_65};
            background: transparent;
    }}
    QLabel {{ background: transparent; }}

    /* ---- a setting the search has just jumped to. Bright enough to
       find with the eye after a tab switch, and temporary - see
       gui/settings_search.py. ---- */
    QLabel#Found, QCheckBox#Found, QRadioButton#Found {{
            background: {stamp_bg};
            color: {stamp_fg};
            padding: 1px 5px;
    }}

    /* ---- generic on/off indicators (the Activity engine row) - NOT the
       table's match-quality chips, which table_delegates.ChipDelegate
       paints itself from STATUS_GLYPHS/STATUS_WEIGHTS and never reaches
       QSS at all. DAN-149: an outlined hairline tag with no background
       fill, matching tokens.css's `.engine-tag` - on/off carried by ink
       tier plus border weight, not a hue (and not font-weight: 700,
       which this Regular-only Space Grotesk build can't render -
       DAN-148). ---- */
    QLabel#EngineChipOn {{
            background: transparent; color: {ink_100};
            border: 1px solid {ink_46};
            padding: 2px 10px; font-size: 12px;
    }}
    QLabel#EngineChipOff {{
            background: transparent; color: {ink_65};
            border: 1px solid {ink_18};
            padding: 2px 10px; font-size: 12px;
    }}

    /* ---- inputs ---- */
    QPlainTextEdit, QLineEdit, QTextEdit {{
            background: {card_alt};
            border: 1px solid {ink_46};
            padding: 10px;
            selection-background-color: {stamp_bg};
            selection-color: {stamp_fg};
    }}
    QPlainTextEdit:focus, QLineEdit:focus, QTextEdit:focus {{
            border: 1px solid {ink_58};
    }}
    QPlainTextEdit:disabled, QLineEdit:disabled {{ color: {ink_45}; }}
    QPlainTextEdit#Log {{
            font-family: {font_mono};
            font-size: 12px;
            color: {ink_65};
    }}

    QComboBox {{
            background: {card_alt};
            border: 1px solid {ink_46};
            padding: 8px 12px;
            min-width: 90px;
    }}
    QComboBox:hover {{ border: 1px solid {ink_58}; }}
    QComboBox:disabled {{ color: {ink_45}; }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox QAbstractItemView {{
            background: {card_alt};
            border: 1px solid {ink_46};
            selection-background-color: {stamp_bg};
            selection-color: {stamp_fg};
            outline: none;
    }}

    QSpinBox, QDoubleSpinBox {{
            background: {card_alt};
            border: 1px solid {ink_46};
            padding: 7px 10px;
            selection-background-color: {stamp_bg};
            selection-color: {stamp_fg};
    }}
    QSpinBox:focus, QDoubleSpinBox:focus {{ border: 1px solid {ink_58}; }}
    QSpinBox:disabled, QDoubleSpinBox:disabled {{ color: {ink_45}; }}
    /* Without these the steppers keep their native boxed frames, which
       sit inside a flat field looking like the field is clipped. */
    QSpinBox::up-button, QDoubleSpinBox::up-button,
    QSpinBox::down-button, QDoubleSpinBox::down-button {{
            background: transparent;
            border: none;
            width: 16px;
    }}
    QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
    QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
            background: {card};
    }}

    /* ---- buttons ---- */
    QPushButton#Primary {{
            background: {stamp_bg};
            color: {stamp_fg};
            border: none;
            padding: 11px 26px;
            font-size: 14px;
    }}
    QPushButton#Primary:pressed {{ background: {stamp_bg}; }}
    QPushButton#Primary:disabled {{
            background: {ink_18};
            color: {ink_45};
    }}

    QPushButton {{
            background: transparent;
            color: {ink_100};
            border: 1px solid {ink_46};
            padding: 9px 18px;
    }}
    QPushButton:hover {{ border-color: {ink_58}; }}
    QPushButton:pressed {{ background: {card_alt}; }}
    QPushButton:disabled {{ color: {ink_45}; border-color: {ink_46}; }}
    QPushButton:checked {{
            background: {card_alt};
            border-color: {ink_46};
    }}

    QPushButton#IconButton {{
            border: none;
            padding: 4px 8px;
            color: {ink_65};
            font-size: 15px;
    }}
    QPushButton#IconButton:hover {{ color: {ink_100}; background: {card_alt}; }}
    QPushButton#IconButton:checked {{ color: {ink_100}; background: {card_alt}; }}

    /* ---- the segmented control ----
       Several readings of one thing, drawn as one control rather than
       loose buttons: square edges and a shared border, so it reads as a
       single thing with one of its parts chosen. ---- */
    QPushButton#Segment {{
            background: transparent;
            color: {ink_65};
            border: 1px solid {ink_46};
            padding: 5px 14px;
            font-family: {font_mono};
            font-size: 12px;
            margin: 0;
    }}
    QPushButton#Segment[segment="middle"], QPushButton#Segment[segment="last"] {{
            border-left: none;
    }}
    QPushButton#Segment:hover {{ color: {ink_100}; }}
    QPushButton#Segment:checked {{
            background: {stamp_bg};
            color: {stamp_fg};
            border-color: {stamp_bg};
    }}
    QPushButton#Segment:disabled {{ color: {ink_45}; border-color: {ink_46}; }}

    /* ---- the mode switcher in the top bar: the same control, bigger,
       because it is the one that says which half of the app you are in ---- */
    QPushButton#Mode {{
            background: transparent;
            color: {ink_65};
            border: 1px solid {ink_46};
            padding: 8px 22px;
            font-family: {font_mono};
            font-size: 13px;
            font-weight: 700;
            margin: 0;
    }}
    QPushButton#Mode[segment="middle"], QPushButton#Mode[segment="last"] {{
            border-left: none;
    }}
    QPushButton#Mode:hover {{ color: {ink_100}; }}
    QPushButton#Mode:checked {{
            background: {stamp_bg};
            color: {stamp_fg};
            border-color: {stamp_bg};
    }}

    /* ---- a fold that opens a rarely-used group ---- */
    QPushButton#Disclosure {{
            background: transparent;
            border: none;
            padding: 4px 0;
            color: {ink_65};
            font-size: 12px;
            text-align: left;
    }}
    QPushButton#Disclosure:hover {{ color: {ink_100}; }}
    QPushButton#Disclosure:checked {{ color: {ink_100}; background: transparent; border: none; }}

    /* ---- buttons carrying a menu of what they will do ---- */
    QToolButton#Primary {{
            background: {stamp_bg};
            color: {stamp_fg};
            border: none;
            padding: 11px 26px;
            font-size: 14px;
    }}
    QToolButton#Primary:disabled {{ background: {ink_18}; color: {ink_45}; }}
    QToolButton {{
            background: transparent;
            color: {ink_100};
            border: 1px solid {ink_46};
            padding: 7px 14px;
    }}
    QToolButton:hover {{ border-color: {ink_58}; }}
    QToolButton:disabled {{ color: {ink_45}; border-color: {ink_46}; }}
    QToolButton::menu-button {{
            border: none;
            width: 18px;
            padding-right: 4px;
    }}
    QToolButton::menu-arrow {{ width: 8px; height: 8px; }}

    QMenu {{
            background: {card};
            color: {ink_100};
            border: 1px solid {ink_46};
            padding: 6px;
    }}
    QMenu::item {{ padding: 7px 22px 7px 26px; }}
    QMenu::item:selected {{ background: {card_alt}; }}
    QMenu::item:disabled {{ color: {ink_45}; }}
    QMenu::separator {{ height: 1px; background: {ink_18}; margin: 5px 8px; }}
    QMenu::indicator {{ width: 14px; height: 14px; left: 8px; }}

    QMenuBar {{ background: {page}; color: {ink_100}; border: none; }}
    QMenuBar::item {{ background: transparent; padding: 6px 12px; }}
    QMenuBar::item:selected {{ background: {card_alt}; }}

    /* ---- tabs ---- */
    QTabWidget::pane {{ border: none; background: {page}; }}
    QTabBar::tab {{
            background: transparent;
            color: {ink_65};
            padding: 9px 20px;
            margin-right: 6px;
    }}
    QTabBar::tab:selected {{ background: {card}; color: {ink_100}; }}
    QTabBar::tab:hover:!selected {{ color: {ink_100}; }}

    /* ---- the image list ----
       No gridlines and a generous row height: the rows carry thumbnails,
       and a grid drawn between pictures reads as part of the pictures. ---- */
    QTableView {{
            background: {card};
            color: {ink_100};
            border: none;
            gridline-color: transparent;
            selection-background-color: {stamp_bg};
            selection-color: {stamp_fg};
            outline: none;
    }}
    /* Deliberately no `alternate-background-color` and no call to
       setAlternatingRowColors: {card_alt} is this sheet's interaction
       tone everywhere else it appears (hover, pressed, selected menu
       item) - it means "you're touching this", not "every other row".
       Rendered both ways on the Queue table (both themes) to check:
       dark reads fine because the two tones sit close, but the same
       rule in light theme turns the row colour it uses for hover/press
       into a loud, almost tan zebra stripe down the whole list - noise,
       not structure, at real row counts. Left off in both so the table
       looks the same in either theme. */
    /* Deliberately NO `QTableView::item` rule. Styling an item switches Qt
       to styled-item painting, at which point the model's BackgroundRole is
       ignored - and this table coded match quality green/amber/red long
       before it had a theme. Row padding comes from the row height set in
       code; selection comes from the view's own palette above, which is
       honoured either way. Verified: with an ::item rule the green cell
       painted {card} (the card colour), without it, the model's own colour. */

    QHeaderView {{ background: transparent; border: none; }}
    QHeaderView::section {{
            background: {card_alt};
            color: {ink_65};
            border: none;
            border-right: 1px solid {ink_46};
            padding: 7px 8px;
            font-size: 12px;
    }}
    QHeaderView::section:hover {{ color: {ink_100}; }}
    QHeaderView::section:last {{ border-right: none; }}
    QTableCornerButton::section {{ background: {card_alt}; border: none; }}

    /* ---- the tag list, and every other plain list ---- */
    QListWidget, QListView, QTreeView {{
            background: {card_alt};
            border: 1px solid {ink_46};
            padding: 4px;
            outline: none;
            selection-background-color: {stamp_bg};
            selection-color: {stamp_fg};
    }}
    QListWidget::item, QListView::item, QTreeView::item {{
            padding: 5px 8px;
    }}
    QListWidget::item:selected, QListView::item:selected, QTreeView::item:selected {{
            background: {stamp_bg};
            color: {stamp_fg};
    }}
    QListWidget::item:hover:!selected {{ background: {card}; }}

    /* ---- progress ---- */
    QProgressBar {{
            background: {card_alt};
            border: none;
            height: 12px;
            text-align: center;
            color: transparent;
    }}
    QProgressBar::chunk {{
            background: {stamp_bg};
    }}

    /* ---- slider ---- */
    QSlider::groove:horizontal {{
            height: 6px;
            background: {card_alt};
    }}
    QSlider::sub-page:horizontal {{
            background: {stamp_bg};
    }}
    QSlider::handle:horizontal {{
            background: {stamp_bg};
            width: 16px;
            margin: -6px 0;
            border: 1px solid {card_alt};
    }}

    /* ---- checkbox / radio ---- */
    QCheckBox, QRadioButton {{ background: transparent; spacing: 10px; }}
    QCheckBox:disabled, QRadioButton:disabled {{ color: {ink_45}; }}
    QCheckBox::indicator {{
            width: 18px; height: 18px;
            border: 1px solid {ink_46};
            background: {card_alt};
    }}
    QCheckBox::indicator:checked {{
            background: {stamp_bg};
            border-color: {stamp_bg};
    }}
    QCheckBox::indicator:disabled {{ background: {ink_18}; }}
    QRadioButton::indicator {{
            width: 18px; height: 18px;
            border: 1px solid {ink_46};
            background: {card_alt};
    }}
    QRadioButton::indicator:checked {{
            background: {stamp_bg};
            border: 4px solid {card_alt};
    }}

    /* ---- grouping in the settings dialog ---- */
    QGroupBox {{
            background: transparent;
            border: 1px solid {ink_18};
            margin-top: 14px;
            padding: 12px 10px 10px 10px;
    }}
    QGroupBox::title {{
            subcontrol-origin: margin;
            left: 14px;
            padding: 0 6px;
            color: {ink_65};
    }}

    /* ---- the strip along the bottom ---- */
    /* Deliberately quiet (DAN-241): passive single-label strip, no interactive affordance; page bg matches the content above it with no discontinuity to bridge, and the status bar's fixed window position does the framing. Same reasoning as QGroupBox. */
    QStatusBar {{ background: {page}; color: {ink_65}; border-top: 1px solid {ink_18}; }}
    QStatusBar::item {{ border: none; }}
    QStatusBar QLabel {{ background: transparent; }}

    /* ---- splitter handles: visible, but calm ---- */
    QSplitter::handle {{ background: transparent; }}
    QSplitter::handle:horizontal {{ width: 12px; }}
    QSplitter::handle:vertical {{ height: 12px; }}
    QSplitter::handle:hover {{ background: {ink_28}; }}

    /* ---- scrollbars ---- */
    QScrollBar:vertical, QScrollBar:horizontal {{
            background: transparent;
            border: none;
    }}
    QScrollBar:vertical {{ width: 10px; }}
    QScrollBar:horizontal {{ height: 10px; }}
    QScrollBar::handle {{
            background: {ink_18};
            min-width: 30px;
            min-height: 30px;
    }}
    QScrollBar::handle:hover {{ background: {ink_28}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    QScrollArea {{ background: transparent; border: none; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}

    QToolTip {{
            background: {card};
            color: {ink_100};
            border: 1px solid {ink_18};
            padding: 6px 8px;
    }}
    """.format(**p, font_serif=FONT_SERIF, font_sans=FONT_SANS, font_mono=FONT_MONO,
               glyph_font=STATUS_GLYPH_FONT)
