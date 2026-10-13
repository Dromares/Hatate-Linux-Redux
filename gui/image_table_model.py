"""The model behind the main image list.

QTableWidget wants a QTableWidgetItem object per cell, created up front:
ten allocations per row for every entry in the list, whether or not that
row is ever scrolled into view. At a few hundred images that is merely
wasteful; at the tens of thousands a Hydrus library reaches, building the
table becomes the slowest thing the window does, and every re-sort pays
the whole cost again.

A model stores nothing per cell. Qt asks data() only for the rows it is
about to paint - roughly twenty - and reads straight from the ImageEntry
objects the window already holds. Adding, removing or re-sorting rows
then costs a signal rather than a rebuild.

The per-column rendering helpers live here rather than in main_window
because they define what a row LOOKS like, which is exactly this class's
job. main_window re-exports them for its sort keys and for the tests.

Thumbnails stay the window's business: it owns the icon cache and the
background worker that fills it. The model just asks, through the
`thumbnail_for` callback, and is told to re-paint a row when one lands.
"""
from __future__ import annotations

from typing import Callable, List, Optional

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PyQt6.QtGui import QFont, QIcon

from core.entry_filter import EntryFilter, upscale_verdict_label
from core.image_compare import compact_size_delta, size_delta_sort_key
from core.models import ImageEntry, MatchStatus
from core.similarity_display import (
    ESTIMATE_CHIP, similarity_label, similarity_tooltip,
)
from gui import theme

COLUMNS = ["Thumb", "File", "Status", "Engine", "Similarity", "Size diff.",
           "Booru", "Tags", "Cache", "Sent", "Reviewed", "Upscale"]

# Which chip a cell should be drawn as, for ChipDelegate. A role rather
# than the delegate reading the entry itself, so the model stays the one
# place that knows what an entry means and the delegate stays a painter.
CHIP_ROLE = Qt.ItemDataRole.UserRole + 10

COL_THUMB, COL_FILE, COL_STATUS, COL_ENGINE, COL_SIMILARITY = 0, 1, 2, 3, 4
COL_SIZE_DELTA, COL_BOORU, COL_TAGS, COL_CACHE, COL_SENT = 5, 6, 7, 8, 9
COL_REVIEWED, COL_UPSCALE = 10, 11

HEADER_TRACKING = 110  # percent: tokens.css's 0.1em on the table headers

# First-run column widths as shares of the width left once the thumbnail
# column has its own. The last column (Upscale) is the stretch section and
# takes whatever remains, so it has no entry. Proportions rather than
# pixel widths: at 1360 and at 1440 the default layout has to fill the
# viewport without a horizontal scrollbar (Q-04), which a fixed total
# cannot do at both.
DEFAULT_COLUMN_SHARES = {
    COL_FILE: 0.18, COL_STATUS: 0.10, COL_ENGINE: 0.08, COL_SIMILARITY: 0.09,
    COL_SIZE_DELTA: 0.09, COL_BOORU: 0.09, COL_TAGS: 0.05, COL_CACHE: 0.06,
    COL_SENT: 0.07, COL_REVIEWED: 0.08,
}
# Floors for the shares: below these the (uppercase, tracked) header title
# or the longest chip elides.
DEFAULT_COLUMN_MINIMUMS = {
    COL_FILE: 140, COL_STATUS: 120, COL_ENGINE: 80, COL_SIMILARITY: 100,
    COL_SIZE_DELTA: 100, COL_BOORU: 80, COL_TAGS: 60, COL_CACHE: 70,
    COL_SENT: 80, COL_REVIEWED: 100,
}


def default_column_widths(viewport_width: int, thumb_width: int) -> dict:
    """{column: width} for a first run (no saved layout) in a viewport
    `viewport_width` wide. The last column is left out: it stretches."""
    room = max(0, viewport_width - thumb_width)
    return {
        col: max(DEFAULT_COLUMN_MINIMUMS[col], int(room * share))
        for col, share in DEFAULT_COLUMN_SHARES.items()
    }


_MISSING_TOOLTIP = (
    "This file is no longer on disk - most likely deleted from Hydrus.\n"
    "Its tags and matched URL are still intact, and \"Send URL to Hydrus's "
    "URL Importer\" still works for it."
)
_REVIEWED_TOOLTIP = (
    "You looked at this one and decided against sending it - the local copy is "
    "better, or the match is wrong.\n\n"
    "Separate from Sent because both are decisions, and a row with neither mark "
    "is one nobody has got to yet. That is what makes a long review pass "
    "resumable: right-click > Select by Review State > Needs review, or press N "
    "to jump to the next one."
)
_SIZE_DELTA_TOOLTIP = (
    "How the matched image's dimensions compare with your local file, on the long "
    "edge. \"+ 4x\" means the match is four times the size; \"- 4x\" means it's a "
    "quarter of it; \"=\" means they're the same. Blank when the match's size isn't "
    "known."
)
_UPSCALE_NOT_CHECKED_TOOLTIP = (
    "Right-click > Check for Upscaling has not been run on this image yet."
)


def _upscale_glyph(entry: ImageEntry) -> str:
    """'▲' (flagged - the same shape the Size diff. column's
    upgrade case uses, both reading "a bigger/better version exists").
    '●' for clear - the same confirmed-positive shape the Status and
    Sent columns use for 'good'/'sent'. Nothing for not yet checked: the
    cell's own text is blank there too."""
    if entry.upscale_verdict == "flagged":
        return "▲"
    if entry.upscale_verdict == "clear":
        return "●"
    return ""


def _upscale_weight(entry: ImageEntry) -> str:
    """ink_100 for flagged (a heuristic worth a look, not proof - the
    same caution the results dialog draws with a warning rather than an
    error icon), ink_65 for clear, ink_45 for not yet checked."""
    if entry.upscale_verdict == "flagged":
        return "ink_100"
    if entry.upscale_verdict == "clear":
        return "ink_65"
    return "ink_45"


def _entry_size_delta_label(entry: ImageEntry) -> str:
    """The Size diff. cell: how the match compares with the local
    file, always expressed relative to the local one."""
    candidate = entry.selected_candidate
    if candidate is None:
        return ""
    return compact_size_delta(
        entry.local_width or 0, entry.local_height or 0,
        candidate.width or 0, candidate.height or 0,
    )


def _size_delta_glyph(entry: ImageEntry) -> str:
    """'▲' for an upgrade - the same "a bigger/better version exists"
    shape the Upscale column's flagged case uses. '=' and a downgrade
    draw no glyph: compact_size_delta's own "+"/"-" prefix already puts
    the direction in the label text, so there is no missing shape
    channel there for a glyph to replace - only the weight tier below
    changes."""
    if _entry_size_delta_label(entry).startswith("+"):
        return "▲"
    return ""


def _size_delta_weight(entry: ImageEntry) -> str:
    """ink_100 for an upgrade (worth a look), ink_65 for a downgrade
    (routine, not alarming), ink_45 for "=" or unmeasured (nothing to
    act on) - the same reading the old hue tiers used, on the ink ramp
    instead of a hue."""
    label = _entry_size_delta_label(entry)
    if label.startswith("+"):
        return "ink_100"
    if label.startswith("-"):
        return "ink_65"
    return "ink_45"


def _entry_engine_label(entry: ImageEntry) -> str:
    """Which engine (IQDB or SauceNAO) found the entry's currently
    selected match, for the Engine table column. Blank if nothing's been
    found/selected yet."""
    candidate = entry.selected_candidate
    return candidate.engine if candidate else ""


# What the Engine cell says for the row that was being searched when the
# previous run died (S-02): not an engine, because none had answered.
INTERRUPTED_MARKER = "\u2014 interrupted mid-search"


def _is_interrupted(entry: ImageEntry) -> bool:
    """Whether the row still carries the restore's "was in flight" note.
    Only while it is unsearched: a row with a result has moved on, and
    the note would then contradict the cell beside it."""
    return entry.interrupted_mid_search and entry.status == MatchStatus.NOT_SEARCHED


def _entry_cache_label(entry: ImageEntry) -> str:
    """Whether the entry's current result came from the local search
    cache or a fresh IQDB/SauceNAO search, for the Cache table column.
    Blank if it hasn't been searched at all yet."""
    if getattr(entry, "searched_without_saucenao", False):
        # Distinct from "Fresh" on purpose: this one is not finished. It
        # was searched without SauceNAO because the daily allowance was
        # spent, so it is not cached and is worth running again.
        return "Provisional"
    if entry.result_source == "cached":
        return "Cached"
    if entry.result_source == "fresh":
        return "Fresh"
    return ""


def _entry_sent_label(entry: ImageEntry) -> str:
    """Whether this image has been handed to Hydrus, for the Sent table
    column. "Sent" means Hydrus acknowledged holding the file; "Queued"
    means it was handed to Hydrus's own downloader, which works
    asynchronously, and nothing has confirmed it landed yet."""
    if not entry.sent_to_hydrus:
        return ""
    return "Sent" if entry.hydrus_import_confirmed else "Queued"


class ImageTableModel(QAbstractTableModel):
    """Presents the window's list of ImageEntry objects as table rows.

    Deliberately does NOT own the list: the window sorts and extends it
    in place, and handing the model its own copy would mean two lists to
    keep in step.

    But the window also REBINDS it in places - removing imported rows
    builds a filtered list, opening a session assigns a loaded one - and
    a stored reference then points at a list nobody is using any more.
    So refresh_all() takes the window's current list and re-reads it,
    rather than trusting the one captured at construction.
    """

    def __init__(self, entries: List[ImageEntry],
                 thumbnail_for: Callable[[ImageEntry], QIcon],
                 parent=None, mode_getter: Optional[Callable[[], str]] = None):
        super().__init__(parent)
        self._entries = entries
        self._thumbnail_for = thumbnail_for
        # A callable, not a value, for the same reason ChipDelegate takes
        # one: a theme switch re-polishes the table and asks data() for
        # ForegroundRole again, and a mode captured once here would keep
        # answering in the old palette. Defaults to dark so a caller that
        # doesn't care about live theme switching (most tests) doesn't
        # have to supply one.
        self._mode_getter = mode_getter or (lambda: "dark")
        self._filter = EntryFilter()
        # What the table actually shows. With no filter this is just a
        # copy of the list; with one it is the subset that passes.
        #
        # Every row-number-to-entry lookup in the window goes through
        # entry_at() and therefore through this list. That is the whole
        # safety property of filtering: a row number means a position on
        # SCREEN, and if anything resolved one against the unfiltered
        # list instead it would act on a row the user cannot see - which
        # for "Remove" or "Delete from Hydrus" would be the worst kind of
        # bug this app could have.
        self._visible: List[ImageEntry] = list(entries)

    # -- Qt interface --------------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:
        # A valid parent means "how many children does this cell have" -
        # always zero for a flat table. Returning the row count there
        # instead makes Qt treat every cell as a subtree.
        if parent.isValid():
            return 0
        return len(self._visible)

    def columnCount(self, parent=QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(COLUMNS)

    def headerData(self, section: int, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.FontRole and orientation == Qt.Orientation.Horizontal:
            # The column titles' type voice (Q-03). QSS has no text-transform
            # or letter-spacing, and a font set on the header widget is
            # replaced when the stylesheet's `::section` rule paints, so the
            # model is the one place the caps survive. The text stays
            # "Size diff.": the accessible name and any copy are not shouting.
            font = theme.mono_font()
            font.setPixelSize(10)
            font.setBold(True)
            font.setCapitalization(QFont.Capitalization.AllUppercase)
            font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, HEADER_TRACKING)
            return font
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return COLUMNS[section] if 0 <= section < len(COLUMNS) else None
        # Row numbers down the left edge. QTableWidget's built-in model
        # supplied these for free, so leaving them out here read as the
        # numbers having simply vanished from the list. 1-based: it is a
        # position in a list the user is counting through, not an index.
        if 0 <= section < len(self._visible):
            return str(section + 1)
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        if not (0 <= row < len(self._visible)):
            return None
        entry = self._visible[row]

        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(entry, col)
        if role == Qt.ItemDataRole.DecorationRole and col == COL_THUMB:
            return self._thumbnail_for(entry)
        if role == CHIP_ROLE:
            return self._chip(entry, col)
        if role == Qt.ItemDataRole.ForegroundRole:
            return self._foreground(entry, col)
        if (role == Qt.ItemDataRole.FontRole and col == COL_ENGINE
                and not _entry_engine_label(entry) and _is_interrupted(entry)):
            # Italic is all this sets, so the view's own face and size carry
            # through (Qt resolves a font from an item against the view's).
            italic = QFont()
            italic.setItalic(True)
            return italic
        if role == Qt.ItemDataRole.FontRole and col in (COL_SIZE_DELTA, COL_UPSCALE):
            # Neither column goes through ChipDelegate, so there is no
            # per-character font switch available to it the way the
            # Status/Sent/Similarity glyphs get one. Both columns' own
            # glyph ('▲'/'●') is missing from the table's ambient sans
            # font (confirmed by cmap inspection, the same check that
            # ruled out JetBrains Mono for '◐'/'○') - putting the WHOLE
            # cell in the mono stack instead covers the glyph and the
            # Latin label in one consistent face, with no split-paint
            # delegate needed for just these two.
            return theme.mono_font()
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(entry, col)
        return None

    # -- per-role rendering --------------------------------------------

    @staticmethod
    def _display(entry: ImageEntry, col: int):
        if col == COL_FILE:
            # Marked in the File column rather than by changing Status,
            # which would overwrite the search result the row still holds
            # - and that result stays useful: the URL importer can pull
            # the file back into Hydrus without the local copy.
            return f"{entry.filename}  (missing)" if entry.file_missing else entry.filename
        if col == COL_STATUS:
            return entry.status.label
        if col == COL_ENGINE:
            return _entry_engine_label(entry) or (
                INTERRUPTED_MARKER if _is_interrupted(entry) else "")
        if col == COL_SIMILARITY:
            return similarity_label(entry.similarity, entry.similarity_measured)
        if col == COL_SIZE_DELTA:
            label = _entry_size_delta_label(entry)
            glyph = _size_delta_glyph(entry)
            return f"{glyph} {label}" if glyph else label
        if col == COL_BOORU:
            return entry.booru_name or ""
        if col == COL_TAGS:
            return str(len(entry.tags))
        if col == COL_CACHE:
            return _entry_cache_label(entry)
        if col == COL_SENT:
            return _entry_sent_label(entry)
        if col == COL_REVIEWED:
            # A tick rather than the word: the column is read by the
            # dozen down a long list, where a mark either is or isn't
            # there reads faster than a word to be checked against a
            # header.
            return "\u2713" if entry.reviewed else ""
        if col == COL_UPSCALE:
            if not entry.upscale_verdict:
                return ""
            label = upscale_verdict_label(entry)
            glyph = _upscale_glyph(entry)
            return f"{glyph} {label}" if glyph else label
        return None  # COL_THUMB carries an icon, not text

    @staticmethod
    def _chip(entry: ImageEntry, col: int):
        """The chip key for a cell, or None to draw it as ordinary text.

        Status and Sent are the two columns read by the dozen down a long
        list, and the two whose meaning was already carried by colour.
        """
        if col == COL_STATUS:
            return entry.status.value
        if col == COL_SENT and entry.sent_to_hydrus:
            return "sent" if entry.hydrus_import_confirmed else "queued"
        if col == COL_SIMILARITY and entry.similarity is not None \
                and not entry.similarity_measured:
            # Only the unmeasured ones are chipped, the way only a SENT
            # row is: a real score is the normal case and reads as an
            # ordinary number. The key is deliberately absent from
            # theme.CHIP_LABELS so the chip keeps the cell's own text -
            # the number IS the content here, unlike Status and Sent
            # where the chip replaces a longer word.
            return ESTIMATE_CHIP
        return None

    def _foreground(self, entry: ImageEntry, col: int):
        mode = self._mode_getter()
        if col == COL_FILE and entry.file_missing:
            # ink_100 would be invisible here - it is the same colour an
            # ordinary (unstyled) filename already renders at, since
            # QTableView's own base text colour IS ink_100 (gui/theme.py),
            # so matching it would silently drop the distinction. A
            # missing file is closer to a disabled control than an alarm
            # - there is nothing left to act on, it cannot be previewed,
            # compared or sent - so it fades the same way QPushButton/
            # QComboBox's own :disabled state does, at ink_45.
            return theme.ink_color(mode, "ink_45")
        if col == COL_ENGINE and not _entry_engine_label(entry) and _is_interrupted(entry):
            # A note, not a result: the body-safe dim tier, which still
            # clears the text-contrast floor (ink_45 does not).
            return theme.ink_color(mode, "ink_65")
        if col == COL_REVIEWED and entry.reviewed:
            # The same routine-not-alarming tier a confirmed send uses:
            # both mean the row is finished with, and they should read
            # as one state.
            return theme.ink_color(mode, "ink_65")
        if col == COL_SIZE_DELTA:
            return theme.ink_color(mode, _size_delta_weight(entry))
        if col == COL_UPSCALE:
            return theme.ink_color(mode, _upscale_weight(entry))
        return None

    @staticmethod
    def _tooltip(entry: ImageEntry, col: int):
        if col == COL_FILE and entry.file_missing:
            return _MISSING_TOOLTIP
        if col == COL_SIZE_DELTA:
            return _SIZE_DELTA_TOOLTIP
        if col == COL_REVIEWED:
            return _REVIEWED_TOOLTIP
        if col == COL_SIMILARITY:
            # The cell has room for "~92%" and no more, so the whole
            # explanation of what the tilde means lives here.
            return similarity_tooltip(entry.similarity, entry.similarity_measured) or None
        if col == COL_STATUS:
            # The chip is deliberately terse; this is where the full
            # wording went, so nothing was actually lost by shortening it.
            return entry.status.label
        if col == COL_UPSCALE:
            return entry.upscale_check_detail or _UPSCALE_NOT_CHECKED_TOOLTIP
        return None

    # -- change notification -------------------------------------------

    def refresh_all(self, entries: Optional[List[ImageEntry]] = None):
        """The row SET changed - rows added, removed or reordered.

        Pass the window's current list. Several paths replace it with a
        new list object rather than mutating the old one (removing
        imported rows, loading a session, clearing), and a model still
        holding the previous list carries on rendering entries that are
        no longer in the window at all - stale rows, each showing the
        thumbnail of whatever used to be there. Re-reading it here means
        any such rebind is picked up, including future ones, since every
        structural change routes through this.

        beginResetModel rather than layoutChanged: a reset is the one
        signal that lets Qt discard everything it believed about the old
        rows, which is the honest description of a re-sort or a reload.
        The caller restores the selection afterwards, since a reset drops
        it.
        """
        self.beginResetModel()
        if entries is not None:
            self._entries = entries
        self._visible = self._filter.apply(self._entries)
        self.endResetModel()

    def refresh_entry(self, entry: ImageEntry):
        """One entry's data changed but the list didn't. Repaints just
        that row; a no-op if the entry is not on screen - it may have
        been auto-removed after import while work was in flight, or be
        hidden by the current filter."""
        try:
            row = self._visible.index(entry)
        except ValueError:
            return
        self.refresh_row(row)

    def entry_at(self, row: int) -> Optional[ImageEntry]:
        """The entry shown at this row.

        The ONLY place a row number becomes an entry. Everything acting
        on a selection goes through here, so a filtered-out entry can
        never be reached by a row number.
        """
        if 0 <= row < len(self._visible):
            return self._visible[row]
        return None

    def entries_at(self, rows) -> List[ImageEntry]:
        """The entries shown at these rows, skipping any that are out of
        range - a selection can outlive the rows it referred to."""
        found = []
        for row in rows:
            entry = self.entry_at(row)
            if entry is not None:
                found.append(entry)
        return found

    def visible_entries(self) -> List[ImageEntry]:
        """The rows currently on screen, in screen order."""
        return self._visible

    def row_of(self, entry: ImageEntry) -> Optional[int]:
        """Where this entry is on screen, or None if the filter hides it."""
        try:
            return self._visible.index(entry)
        except ValueError:
            return None

    def set_filter(self, entry_filter: EntryFilter):
        """Changes what is shown. Resets the model, so the caller restores
        the selection afterwards."""
        self.beginResetModel()
        self._filter = entry_filter
        self._visible = self._filter.apply(self._entries)
        self.endResetModel()

    @property
    def filter(self) -> EntryFilter:
        return self._filter

    def visible_count(self) -> int:
        return len(self._visible)

    def total_count(self) -> int:
        return len(self._entries)

    def refresh_row(self, row: int):
        if 0 <= row < len(self._visible):
            self.dataChanged.emit(
                self.index(row, 0), self.index(row, len(COLUMNS) - 1),
            )



def header_layout_is_usable(saved_state: str, saved_columns: int,
                            current_columns: Optional[int] = None) -> bool:
    """Whether a saved column layout describes THIS build's columns.

    Qt applies a mismatched layout partially rather than refusing it,
    which leaves header labels sitting over the wrong data and columns
    collapsed to zero width - it looks like the titles have vanished.

    The count must match exactly. Zero means the layout predates the count
    being recorded, which is precisely the untrustworthy case rather than
    an exemption: those were saved by a build with a DIFFERENT set of
    columns. Treating 0 as "close enough" is what let a stale nine-column
    layout load into a ten-column table.
    """
    if not saved_state:
        return False
    if current_columns is None:
        current_columns = len(COLUMNS)
    return saved_columns == current_columns


def sort_key_for_column(column: int) -> Callable:
    """How each column orders rows.

    Here rather than in the window because it is column knowledge, and
    COLUMNS lives here: the two are one edit apart. It previously indexed
    columns by bare number in another file, so reordering COLUMNS would
    have left every column sorting by its neighbour's value with nothing
    to say so - the named constants below already existed and simply
    weren't being used.

    An unknown column sorts everything equal rather than raising: a header
    click is not worth an exception, and a stable no-op is what the Thumb
    column wants anyway.
    """
    keys: dict = {
        # No meaningful order to sort a picture by.
        COL_THUMB: lambda e: 0,
        COL_FILE: lambda e: e.filename.lower(),
        COL_STATUS: lambda e: e.status.sort_rank,
        COL_ENGINE: lambda e: _entry_engine_label(e).lower(),
        COL_SIMILARITY: lambda e: e.similarity if e.similarity is not None else -1.0,
        # By the real ratio, not the label text.
        COL_SIZE_DELTA: lambda e: size_delta_sort_key(
            e.local_width or 0, e.local_height or 0,
            (e.selected_candidate.width or 0) if e.selected_candidate else 0,
            (e.selected_candidate.height or 0) if e.selected_candidate else 0,
        ),
        COL_BOORU: lambda e: (e.booru_name or "").lower(),
        COL_TAGS: lambda e: len(e.tags),
        COL_CACHE: lambda e: (e.result_source or "").lower(),
        # Unsent first, then queued, then confirmed.
        COL_SENT: lambda e: (
            0 if not e.sent_to_hydrus else (1 if not e.hydrus_import_confirmed else 2)
        ),
        # Rows still wanting a decision sort first, which is the order
        # someone clicking this header is asking for.
        COL_REVIEWED: lambda e: 1 if e.reviewed else 0,
        # Flagged first - the rows most worth a look - then clear, then
        # never checked.
        COL_UPSCALE: lambda e: (
            0 if e.upscale_verdict == "flagged" else (1 if e.upscale_verdict == "clear" else 2)
        ),
    }
    return keys.get(column, lambda e: 0)
