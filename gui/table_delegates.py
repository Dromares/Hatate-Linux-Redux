"""Painting for the two columns that carry a verdict rather than a value.

Status and Sent are the columns read by the dozen down a long list, and
the two whose meaning this app has always put in colour. They used to be
painted by filling the whole cell with a saturated, hue-tinted pill -
which works for most eyes, and fails outright for the ones that can't
tell the tint apart. A status now draws as a glyph silhouette (shape) at
an ink-ramp opacity (weight) instead: two channels that still read apart
with colour vision taken out of the picture entirely, plus the word
chip_label() already supplies as a third, named channel.

Deliberately draws no background shape at all - no pill, no rect. Ryoku's
square-cornered, flat-printed chrome (gui/theme.py Tier 1) extends to
this delegate too: the glyph and the label sit directly on the row, the
same surface the next cell's plain text sits on.

The delegate knows nothing about entries: it asks the model for a chip
key through CHIP_ROLE and the theme for that key's glyph/weight/font.
Adding a status means adding to STATUS_GLYPHS/STATUS_WEIGHTS, not
touching this file.

Painting still goes through CE_ItemViewItem with the text blanked first,
same as before the glyph rewrite - that's what keeps a selected or
alternating row looking like every other row in the table, and it's the
thing tests/test_theme.py's TestTableItemsAreNeverStyled exists to
protect: that guard is about the QSS sheet never adding a
`QTableView::item` rule, which would make Qt paint its own item
background and silently stop asking this delegate - and the model - for
BackgroundRole/ForegroundRole at all. Nothing here needs touching it, but
the glyph this class draws depends on that rule staying absent.
"""
import re

from PyQt6.QtCore import QRectF, QSize, Qt
from PyQt6.QtGui import QBrush, QFont, QFontMetrics
from PyQt6.QtWidgets import QApplication, QStyle, QStyledItemDelegate, QStyleOptionViewItem

from gui import theme
from gui.image_table_model import CHIP_ROLE

MARGIN = 6         # from the cell's own edge
GLYPH_GAP = 6      # between the glyph and the label that follows it

TAG_ROW_HEIGHT = 32   # the mockup's measured .tag-row, rule included
TAG_ROW_PAD = 16      # its --space-4 side padding
TAG_FONT_PX = 13
_TAG_SOURCE = re.compile(r"^(\[[^\]]+\])\s(.*)$", re.DOTALL)


def paint_row_rule(painter, cell, mode):
    """The 1px hairline under a Queue row (Q-06). Drawn per cell, edge to
    edge, rather than as a `QTableView::item` border: an item rule would
    switch Qt to styled-item painting (see the module docstring)."""
    painter.save()
    painter.setPen(theme.ink_color(mode, "ink_18"))
    painter.drawLine(cell.left(), cell.bottom(), cell.right(), cell.bottom())
    painter.restore()


class RuledRowDelegate(QStyledItemDelegate):
    """The Queue table's default delegate: an ordinary cell plus the row rule."""

    def __init__(self, mode_getter, parent=None):
        super().__init__(parent)
        # A callable, not a value: the theme can change while the window
        # is open, and a delegate holding a copy would keep painting in
        # the old palette until something rebuilt it.
        self._mode_getter = mode_getter

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        paint_row_rule(painter, option.rect, self._mode_getter())


class ChipDelegate(RuledRowDelegate):
    """Draws a cell's text as a glyph plus a weighted label."""

    def paint(self, painter, option, index):
        key = index.data(CHIP_ROLE)
        if not key:
            super().paint(painter, option, index)
            return

        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text = ""

        # Draw the row itself first - selection, alternating background,
        # focus - with no text, then put the glyph+label on top. Going
        # through the style rather than drawing by hand is what keeps the
        # selected row looking like every other selected row.
        style = opt.widget.style() if opt.widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        mode = self._mode_getter()
        paint_row_rule(painter, option.rect, mode)
        if not text:
            return

        if opt.state & QStyle.StateFlag.State_Selected:
            # The selection fill is stamp_bg, which is the very colour
            # ink_color() resolves every tier to (only the alpha differs),
            # so ink on a selected row is ink on ink: 1.00:1, a blank cell
            # (DAN-1151). The view's own HighlightedText is the colour its
            # stylesheet already pairs with that fill (`selection-color`),
            # and it is what the plain-text cells of the same row use. The
            # ink-ramp weight is dropped here, but the glyph and the word
            # still carry the verdict.
            color = opt.palette.highlightedText().color()
        else:
            color = theme.ink_color(mode, theme.status_weight(key))
        glyph = theme.status_glyph(key)
        glyph_font_family = theme.status_glyph_font(key)
        label = theme.chip_label(key, text)

        font = QFont(opt.font)
        if key == "searching":
            # Mirrors tokens.css's .status--searching: the one status
            # that is neither a confirmed result nor a decision pending
            # on the user, so it gets a third, purely typographic marker
            # on top of its glyph/weight.
            font.setItalic(True)

        cell = option.rect
        x = cell.left() + MARGIN
        right_edge = cell.right() - MARGIN

        painter.save()
        painter.setPen(color)

        if glyph:
            glyph_font = QFont(font)
            if glyph_font_family:
                glyph_font.setFamily(glyph_font_family)
            glyph_width = QFontMetrics(glyph_font).horizontalAdvance(glyph)
            glyph_rect = QRectF(x, cell.top(), glyph_width, cell.height())
            painter.setFont(glyph_font)
            painter.drawText(
                glyph_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, glyph,
            )
            x += glyph_width + GLYPH_GAP

        available = max(0, right_edge - x)
        metrics = QFontMetrics(font)
        shown = metrics.elidedText(label, Qt.TextElideMode.ElideRight, available)
        label_rect = QRectF(x, cell.top(), available, cell.height())
        painter.setFont(font)
        painter.drawText(
            label_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, shown,
        )
        painter.restore()

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        key = index.data(CHIP_ROLE)
        glyph = theme.status_glyph(key) if key else None
        if glyph:
            glyph_width = QFontMetrics(option.font).horizontalAdvance(glyph)
            size.setWidth(size.width() + glyph_width + GLYPH_GAP)
        return size


class TagRowDelegate(QStyledItemDelegate):
    """The Review tag list's rows: a fixed-height ruled row in mono, with the
    `[Source]` marker dimmed and the tag itself bright.

    A list can mix `[Booru]` / `[Hydrus]` / user tags, and the source is
    the part you skip over while scanning for the tag. The marker is read
    out of the item's text, so an item without one (the editable list,
    which shows plain `namespace:name`) draws as a single bright run.
    """

    def __init__(self, mode_getter, parent=None):
        super().__init__(parent)
        self._mode_getter = mode_getter    # a callable, as in ChipDelegate

    def sizeHint(self, option, index):
        return QSize(super().sizeHint(option, index).width(), TAG_ROW_HEIGHT)

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text = ""
        style = opt.widget.style() if opt.widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)

        mode = self._mode_getter()
        row = option.rect
        painter.save()
        painter.setPen(theme.ink_color(mode, "ink_18"))
        painter.drawLine(row.left(), row.bottom(), row.right(), row.bottom())

        if opt.state & QStyle.StateFlag.State_Selected:
            # The fill is stamp_bg, the colour every ink tier resolves to
            # (DAN-1151): ink on a selected row would be ink on ink.
            bright = dim = opt.palette.highlightedText().color()
        else:
            foreground = index.data(Qt.ItemDataRole.ForegroundRole)
            brush = foreground if isinstance(foreground, QBrush) else None
            bright = (brush.color() if brush is not None and brush.style() != Qt.BrushStyle.NoBrush
                      else theme.ink_color(mode, "ink_100"))
            dim = theme.ink_color(mode, "ink_65")

        match = _TAG_SOURCE.match(text)
        source, tag = (match.group(1) + " ", match.group(2)) if match else ("", text)

        font = theme.mono_font(opt.font)
        font.setPixelSize(TAG_FONT_PX)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        x = row.left() + TAG_ROW_PAD
        right = row.right() - TAG_ROW_PAD
        align = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
        if source:
            width = metrics.horizontalAdvance(source)
            painter.setPen(dim)
            painter.drawText(QRectF(x, row.top(), width, row.height() - 1), align, source)
            x += width
        shown = metrics.elidedText(tag, Qt.TextElideMode.ElideRight, max(0, right - x))
        painter.setPen(bright)
        painter.drawText(QRectF(x, row.top(), max(0, right - x), row.height() - 1), align, shown)
        painter.restore()
