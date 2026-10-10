"""The house widgets: the handful of shapes every view is built from.

Adapted from MUR's `gui_common.py`. MUR is PySide6 and this is PyQt6, so
the port is an import swap and nothing more - the two APIs agree on every
call used here.

The point of the file is that a card looks like a card everywhere without
anyone having to remember 18/16/18/16. Anything that decides a measurement
belongs here rather than in a view.
"""
from PyQt6.QtCore import QEvent, QPoint, QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QFontMetrics, QPainter, QPixmap, QRegion
from PyQt6.QtWidgets import (
    QButtonGroup, QComboBox, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QStyle, QStyleOptionComboBox, QStylePainter,
    QVBoxLayout, QWidget,
)

from gui import theme

TRACKING_LABEL = 0.16  # transcribed from tokens.css's --tracking-label

# The house measurements. MUR's, kept to the number so the two
# applications measure the same.
CARD_MARGINS = (18, 16, 18, 16)
CARD_SPACING = 10
PAGE_MARGINS = (22, 18, 22, 18)
# The main window's own page gutter (U3 / DAN-1277): tokens.css's `.app`
# pads `var(--space-6)` left and right, and the menubar and status bar are
# inset by the same 32. Dialogs keep PAGE_MARGINS; they are not screens.
PAGE_GUTTER = 32
SHELL_MARGINS = (PAGE_GUTTER, 18, PAGE_GUTTER, 18)
MENUBAR_HEIGHT = 28       # `.menubar`: 28px with its hairline, DAN-1158 B-1
STATUSBAR_ITEM_INSET = 2  # what QStatusBar itself puts left of its first item
STATUSBAR_HEIGHT = 24     # `.statusbar`: 24px with its top rule, B-2
PAGE_SPACING = 14
SPLITTER_HANDLE = 12
INPUT_MIN_HEIGHT = 38     # a single-line edit crushed below this stops being readable
BUTTON_MIN_HEIGHT = 40    # tokens.css's page-level button, plus the 36px boxed icon below
ICON_BOX = 36
RUN_BANNER_PROSE_WIDTH = 840   # the mockup's `.run-banner__detail` max-width
RUN_BANNER_MARGINS = (24, 24, 24, 24)   # its `padding: var(--space-5)`
SURVIVED_GLYPH_BOX = 42   # the mockup's boxed ✕: a 32px glyph in a hairline square


def card(title=None, framed=True):
    """A region of the page with an optional heading, plus its content layout.

    Returns the frame and the layout separately because the caller wants
    the frame (to put somewhere) and the layout (to fill) - handing back
    only the frame would mean every caller digging the layout back out.

    The frame is left named 'Card', which is what the stylesheet's
    `QFrame#Card` rule selects on. That rule is a transparent 1px
    hairline, not a fill (DAN-1160). `framed=False` is for a card that
    only groups other regions - a column holding a table that has its
    own hairline - where a second line round the outside would be a box
    in a box; it is carried as a property so the object name, which
    callers and tests walk up the parent chain looking for, stays 'Card'.
    """
    frame = QFrame()
    frame.setObjectName('Card')
    frame.setProperty('framed', framed)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(*CARD_MARGINS)
    layout.setSpacing(CARD_SPACING)
    if title:
        heading = QLabel(title)
        heading.setObjectName('Heading')
        layout.addWidget(heading)
    return frame, layout


def section_label(text):
    """A numbered section label - "01 // Filter" - in the mono, tracked, dim
    voice the mockup scans by.

    `text` is passed in sentence case on purpose. The uppercase comes from
    `QFont.Capitalization.AllUppercase`, because QSS has no
    `text-transform`; upper-casing the string instead would put shouting
    in the accessible name and in anything that reads `.text()`.
    """
    label = QLabel(text)
    label.setObjectName('SectionLabel')
    font = label.font()
    font.setCapitalization(QFont.Capitalization.AllUppercase)
    label.setFont(font)
    apply_tracking(label)
    return label


def section_header(text, *trailing):
    """A `section_label` with a hairline running from it to the right edge,
    then whatever `trailing` widgets (a toolbar) sit at the end of the line.
    Returns the row as a layout, ready for `addLayout`."""
    row = QHBoxLayout()
    row.setSpacing(12)
    row.addWidget(section_label(text))
    rule = QFrame()
    rule.setObjectName('SectionRule')
    rule.setFixedHeight(1)
    row.addWidget(rule, 1, Qt.AlignmentFlag.AlignVCenter)
    for widget in trailing:
        row.addWidget(widget)
    return row


class WrappingLabel(QLabel):
    """A word-wrapped label that asks for the height its text needs at the
    width it was actually given.

    A plain wrapped QLabel inside a frame reports the height of its text
    UNWRAPPED, so a paragraph that wraps draws over (or under) whatever
    sits below it - the frame's own layout never learns the width. Here
    the minimum height follows the width, and the layout is re-asked.
    """

    def __init__(self, text=''):
        super().__init__(text)
        self.setWordWrap(True)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        needed = self.heightForWidth(self.width())
        if needed > 0 and needed != self.minimumHeight():
            self.setMinimumHeight(needed)

    def setText(self, text):
        super().setText(text)
        needed = self.heightForWidth(self.width())
        if needed > 0:
            self.setMinimumHeight(needed)


def run_banner():
    """The interrupted-run notice (DAN-660, S-01): a bracketed frame with a
    status glyph, a kicker, reassuring prose, a meta line, and three actions
    in a row beneath.

    Returns the frame plus its content widgets, since the banner holds no
    state of its own - the caller (MainWindow, which knows the restored
    entries) fills in the prose and the meta line and wires the actions to
    its own existing behaviour. The meta line is added into `meta_slot`
    when it is known, because its figures come from what is on disk then.
    """
    frame = QFrame()
    frame.setObjectName('RunBanner')
    layout = QHBoxLayout(frame)
    layout.setContentsMargins(*RUN_BANNER_MARGINS)
    layout.setSpacing(CARD_SPACING)
    layout.setAlignment(Qt.AlignmentFlag.AlignTop)

    glyph = QLabel(theme.status_glyph('poor'))
    glyph.setObjectName('RunBannerGlyph')
    layout.addWidget(glyph, 0, Qt.AlignmentFlag.AlignTop)

    text_col = QVBoxLayout()
    text_col.setSpacing(8)
    # The kicker is sentence case with the capitals in the font, as
    # `section_label` does: the string stays readable to anything that
    # reads `.text()`.
    head = QLabel("Run interrupted \u2014 partial results survived")
    head.setObjectName('RunBannerHead')
    uppercase_voice(head)
    text_col.addWidget(head)
    body = WrappingLabel()
    body.setObjectName('RunBannerBody')
    body.setMaximumWidth(RUN_BANNER_PROSE_WIDTH)
    text_col.addWidget(body)
    meta_slot = QVBoxLayout()
    meta_slot.setContentsMargins(0, 0, 0, 0)
    text_col.addLayout(meta_slot)

    # The action hierarchy is unchanged from DAN-660 and is not to be
    # restyled for the frame: Resume is the one primary, and "Discard" is
    # deliberately the quietest element on the banner, not a stamped
    # button - error-cost asymmetry: a wrong delete costs more than a
    # wrong tag, so it never gets primary-button weight.
    actions = QHBoxLayout()
    actions.setSpacing(12)
    resume_button = pill_button("Resume Queue", primary=True)
    actions.addWidget(resume_button)
    crash_log_button = pill_button("View Crash Log")
    actions.addWidget(crash_log_button)
    discard_label = LinkLabel("Discard &amp; start fresh")
    discard_label.setObjectName('RunBannerDiscard')
    actions.addWidget(discard_label)
    actions.addStretch(1)
    text_col.addLayout(actions)
    layout.addLayout(text_col, 1)

    Brackets(frame)
    return frame, {
        'glyph': glyph,
        'head': head,
        'body': body,
        'meta_slot': meta_slot,
        'resume_button': resume_button,
        'crash_log_button': crash_log_button,
        'discard_label': discard_label,
    }


def meta_line(items, centered=True):
    """The mockup's `.run-banner__meta`: a mono-caps row of `label  FIGURE`
    pairs, the figure bright and bold against its dim label.

    A QLabel can only do that with inline colours, which would pin it to
    one theme, so each run is its own label (the same shape as the run
    strip's `Readout`). `items` is `[(label, figure), ...]`; returns the
    row widget and its flat list of labels, in order. Centred under a
    centred block (S-05); `centered=False` sets it flush left under the
    run banner's prose (S-01).
    """
    row = QWidget()
    row.setObjectName('MetaLine')
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(24)
    if centered:
        layout.addStretch(1)
    labels = []
    for label_text, figure_text in items:
        pair = QHBoxLayout()
        pair.setSpacing(6)
        for text, name in ((label_text, 'MetaLabel'), (figure_text, 'MetaFigure')):
            label = QLabel(text)
            label.setObjectName(name)
            uppercase_voice(label, 0.08)
            pair.addWidget(label)
            labels.append(label)
        layout.addLayout(pair)
    layout.addStretch(1)
    return row, labels


def nothing_survived(meta_items, below_weight):
    """The Queue's "Nothing survived." variant of the empty state (S-05).

    An unclean shutdown whose saved session came back empty: the same
    bracketed frame as the drop zone, but the block says what happened -
    a boxed ✕, a headline, why there is nothing to recover, a meta line,
    and three actions. Returns the frame and its parts; the caller wires
    the buttons, since it knows what they do.

    `below_weight` is the drop zone's top-weighting (E-03), so the two
    variants put their block at the same height and the page does not
    jump when one replaces the other.
    """
    panel = QFrame()
    panel.setObjectName('DropZone')
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(32, 24, 32, 24)
    section = section_label("01 // Queue")
    layout.addWidget(section, 0, Qt.AlignmentFlag.AlignLeft)
    layout.addStretch(1)

    glyph = QLabel("\u2715")
    glyph.setObjectName('SurvivedGlyph')
    glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
    glyph.setFixedSize(SURVIVED_GLYPH_BOX, SURVIVED_GLYPH_BOX)
    layout.addWidget(glyph, 0, Qt.AlignmentFlag.AlignHCenter)
    layout.addSpacing(16)

    title = display_title("Nothing survived.")
    title.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(title)
    layout.addSpacing(6)

    body = hint(
        "Hatate stopped without a clean exit, and the session it had saved was "
        "empty or could not be read, so there is no queue to restore. Anything "
        "already sent to Hydrus is unaffected."
    )
    body.setAlignment(Qt.AlignmentFlag.AlignCenter)
    # A fixed width inside a stretch row - see MainWindow._build_drop_zone
    # for why a word-wrapped label is not handed an alignment instead.
    body.setFixedWidth(560)
    body_row = QHBoxLayout()
    body_row.addStretch(1)
    body_row.addWidget(body)
    body_row.addStretch(1)
    layout.addLayout(body_row)
    layout.addSpacing(12)

    meta, meta_labels = meta_line(meta_items)
    layout.addWidget(meta)
    layout.addSpacing(18)

    new_search = pill_button("Start new search", primary=True)
    crash_log = pill_button("View crash log")
    add_files = pill_button("Add files\u2026")
    buttons = QHBoxLayout()
    buttons.addStretch(1)
    for button in (new_search, crash_log, add_files):
        buttons.addWidget(button)
    buttons.addStretch(1)
    layout.addLayout(buttons)

    layout.addStretch(below_weight)
    Brackets(panel)
    return panel, {
        'section': section,
        'glyph': glyph,
        'title': title,
        'body': body,
        'meta': meta,
        'meta_labels': meta_labels,
        'new_search_button': new_search,
        'crash_log_button': crash_log,
        'add_files_button': add_files,
    }


def restyle(widget, name):
    """Moves a widget to a different styled role, and makes it take.

    Qt re-reads `#name` rules when a widget is polished, not when its
    object name changes, so a chip that goes from good to bad keeps the
    old colour until something forces the re-read. This is that
    something. It is a no-op when the role is already right, because
    unpolishing a widget on every table refresh is not free.
    """
    if widget.objectName() == name:
        return
    widget.setObjectName(name)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


def heading(text):
    label = QLabel(text)
    label.setObjectName('Heading')
    return label


def screen_title(text):
    """A screen's own title - "Queue.", "Review." - the serif display
    head at the top of a top-level page, bigger than a card's `heading()`
    and always the first thing on it."""
    label = QLabel(text)
    label.setObjectName('ScreenTitle')
    return label


def display_title(text):
    """A headline in the screen-title face and size that is not a screen's
    own title - the empty Queue's "Drop images here." (E-01). Kept apart
    from `screen_title` so "the first thing on a page" stays countable."""
    label = QLabel(text)
    label.setObjectName('DisplayTitle')
    return label


def muted(text=''):
    label = QLabel(text)
    label.setObjectName('Muted')
    return label


def hint(text=''):
    label = QLabel(text)
    label.setObjectName('Hint')
    label.setWordWrap(True)
    return label


def uppercase_voice(widget, tracking=TRACKING_LABEL):
    """Gives a control the mockup's type voice: mono, UPPERCASE, tracked.

    The uppercase is `QFont.Capitalization.AllUppercase`, not a string
    transform - QSS has no `text-transform`, and upper-casing the string
    would put shouting in `text()`, the accessible name and anything that
    copies the label. Tooltips are separate strings and are untouched.
    """
    font = widget.font()
    font.setCapitalization(QFont.Capitalization.AllUppercase)
    widget.setFont(font)
    apply_tracking(widget, tracking)


def pill_button(text, on_click=None, primary=False, uppercase=True):
    """A button. `primary` gives it the flat ink-stamp fill and the weight.

    Page-level buttons speak the mockup's UPPERCASE mono voice. Dialogs do
    not (ruling C-4 on DAN-1155: they are not in the mockup and keep
    sentence case), so a dialog passes `uppercase=False`.
    """
    button = QPushButton(text)
    if primary:
        button.setObjectName('Primary')
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if uppercase:
        button.setProperty('voice', 'caps')
        button.setMinimumHeight(BUTTON_MIN_HEIGHT)
        uppercase_voice(button)
    if on_click is not None:
        button.clicked.connect(on_click)
    return button


def link_button(text, on_click=None):
    """An action drawn as an underlined mono-caps label, not a button: the
    mockup's filter-bar `Clear`. No border, no fill - the underline is the
    affordance, and the stylesheet's disabled tier is what says "nothing to
    clear". The underline is on the QFont, so the accessible name is still
    plain `text`.
    """
    button = QPushButton(text)
    button.setObjectName('LinkButton')
    button.setFlat(True)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    uppercase_voice(button)
    font = button.font()
    font.setUnderline(True)
    button.setFont(font)
    if on_click is not None:
        button.clicked.connect(on_click)
    return button


def icon_button(glyph, tooltip='', on_click=None, checkable=False, boxed=False):
    """A borderless glyph button - the small ones that sit beside a heading.

    `boxed` draws it as the top bar's square hairline box, ICON_BOX px a
    side (the mockup's gear, G-04), for an icon that is a control in its
    own right rather than a mark beside a heading.
    """
    button = QPushButton(glyph)
    button.setObjectName('IconButton')
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setCheckable(checkable)
    if boxed:
        button.setProperty('boxed', True)
        button.setFixedSize(ICON_BOX, ICON_BOX)
    if tooltip:
        button.setToolTip(tooltip)
    if on_click is not None:
        button.clicked.connect(on_click)
    return button


def apply_tracking(widget, tracking=TRACKING_LABEL):
    """Widens a mono-caps label's letter-spacing - QSS has no
    letter-spacing property at all, so this is the one piece of the
    mono-caps look (gui/theme.py's `QPushButton#Segment`/`#Mode` already
    carry the font-family) that has to be set in Python rather than the
    stylesheet. `tracking` is a fraction of the glyph width, matching
    tokens.css's own `--tracking-label: 0.16em`.
    """
    font = widget.font()
    font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 100 + tracking * 100)
    widget.setFont(font)


def segmented(choices, on_change=None, current=None, role='Segment', uppercase=True):
    """Several readings of one thing, drawn as a single control.

    `choices` is [(key, label, tooltip)]. Returns the row widget and a
    {key: button} map, so a caller can re-check one later without keeping
    its own list.

    Exclusivity is a real QButtonGroup rather than hand-written
    uncheck-the-others code; the one-pill look is entirely the `segment`
    property plus the stylesheet, which is why the buttons can stay
    ordinary QPushButtons. Labels are drawn UPPERCASE (see
    `uppercase_voice`); a dialog passes `uppercase=False` to keep sentence
    case (ruling C-4).
    """
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)

    group = QButtonGroup(row)
    group.setExclusive(True)
    buttons = {}
    last = len(choices) - 1
    for index, choice in enumerate(choices):
        key, label = choice[0], choice[1]
        tooltip = choice[2] if len(choice) > 2 else ''
        button = QPushButton(label)
        button.setObjectName(role)
        button.setCheckable(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        if uppercase:
            uppercase_voice(button)
        else:
            apply_tracking(button)
        if tooltip:
            button.setToolTip(tooltip)
        button.setProperty(
            'segment',
            'first' if index == 0 else 'last' if index == last else 'middle',
        )
        group.addButton(button, index)
        buttons[key] = button
        layout.addWidget(button)

    if current is not None and current in buttons:
        buttons[current].setChecked(True)
    elif choices:
        buttons[choices[0][0]].setChecked(True)

    if on_change is not None:
        keys = [choice[0] for choice in choices]

        def _changed(index, checked):
            if checked and 0 <= index < len(keys):
                on_change(keys[index])

        group.idToggled.connect(_changed)

    # The group would otherwise be collected with the local scope and the
    # buttons would stop being exclusive.
    row._group = group  # type: ignore[attr-defined]
    row.buttons = buttons  # type: ignore[attr-defined]
    return row, buttons


def disclosure(title, on_toggle=None, open_at_first=False):
    """A fold for a group that is set once and then left alone.

    No animation: it shows or it doesn't. The triangle is the whole
    affordance, and it is in the button's own text so there is nothing
    else to keep in step.
    """
    button = QPushButton()
    button.setObjectName('Disclosure')
    button.setCheckable(True)
    button.setCursor(Qt.CursorShape.PointingHandCursor)

    def _retext(shown):
        button.setText('{}  {}'.format('▾' if shown else '▸', title))

    _retext(open_at_first)
    button.setChecked(open_at_first)
    if on_toggle is not None:
        button.toggled.connect(on_toggle)
    button.toggled.connect(_retext)
    return button


def fold(title, box, open_at_first=False):
    """`disclosure`, already wired to show and hide one widget."""
    button = disclosure(title, box.setVisible, open_at_first)
    box.setVisible(open_at_first)
    return button


def chip(text='', role='EngineChipOff'):
    """An on/off tag - today, the Activity page's engine row. `role` is
    'EngineChipOn' or 'EngineChipOff' (gui/theme.py); both draw as an
    outlined hairline with no fill, told apart by ink tier and border
    weight rather than a hue."""
    label = QLabel(text)
    label.setObjectName(role)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    apply_tracking(label)
    return label


def roomy(widget):
    """Stops a single-line edit being crushed to a sliver by a tight row."""
    if isinstance(widget, QLineEdit):
        widget.setMinimumHeight(INPUT_MIN_HEIGHT)
    return widget


# The ghost kanji (G-07): tokens.css `.kanji-ghost` is 420px Ryoku Kanji,
# 40px in from the right edge and 40px above the top, so the glyph's top
# bleeds off the window.
GHOST_SIZE = 420
GHOST_TOP = -40
GHOST_RIGHT = 40
# Corner brackets (V-03, E-02): tokens.css `.frame__corner` is a 12px L, 2px
# thick, sitting on the frame's own 1px hairline.
BRACKET_ARM = 12
BRACKET_WEIGHT = 2


class GhostKanji(QWidget):
    """A page that paints one very faint kanji behind whatever is on it.

    It is the window's central widget: the mockup hangs the glyph off the
    whole content area, behind the top bar and the run strip as well as the
    mode's own page, so it belongs to the one surface they all sit on rather
    than to each page. `set_glyph` swaps the character as the mode changes
    (力 Queue, 鏡 Review, 動 Activity, 空 Empty).

    It is a paint, not a child widget: nothing sits in the way of the mouse,
    so there is no hit-test to interfere with. Colour comes from the sheet
    (`QWidget#GhostKanji { color: ink_05 }`), which is how it follows a
    dark/light switch with no mode plumbing of its own.

    **It only shows through surfaces that paint nothing.** A child with a
    fill (the table, an input, a plain `QWidget` under the global
    `QWidget { background }` rule) hides it, which is why DAN-1160 made the
    cards transparent first and why the page chain carries
    `background: transparent` in the sheet.

    The glyph is rasterised once per (character, colour, pixel ratio) into
    a pixmap and blitted. Qt repaints a translucent parent under every
    child that scrolls, so a paintEvent that re-shaped 420px of text each
    time would run on every scroll step.
    """

    def __init__(self, glyph="", parent=None):
        super().__init__(parent)
        self.setObjectName('GhostKanji')
        self._glyph = glyph
        self._cache_key = None
        self._cache = None
        self.renders = 0  # how often the glyph was rasterised; tests read it

    def glyph(self):
        return self._glyph

    def set_glyph(self, glyph):
        if glyph != self._glyph:
            self._glyph = glyph
            self.update()

    def _font(self):
        font = QFont("Ryoku Kanji")
        font.setStyleHint(QFont.StyleHint.Serif)
        font.setPixelSize(GHOST_SIZE)
        return font

    def ghost_rect(self):
        """Where the glyph's box sits, in this widget's coordinates.

        The box is as wide as the glyph advances and 420px tall, as the
        mockup's `line-height: 1` makes it, anchored 40px in from the right.
        """
        width = QFontMetrics(self._font()).horizontalAdvance(self._glyph)
        return QRect(self.width() - GHOST_RIGHT - width, GHOST_TOP,
                     width, GHOST_SIZE)

    def _pixmap(self):
        colour = self.palette().color(self.foregroundRole())
        ratio = self.devicePixelRatioF()
        key = (self._glyph, colour.rgba(), ratio)
        if key != self._cache_key:
            font = self._font()
            metrics = QFontMetrics(font)
            width = metrics.horizontalAdvance(self._glyph)
            pixmap = QPixmap(round(width * ratio), round(GHOST_SIZE * ratio))
            pixmap.setDevicePixelRatio(ratio)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setFont(font)
            painter.setPen(colour)
            # CSS `line-height: 1` centres the font's ascent+descent in the
            # 420px line box; the baseline falls where that puts it.
            baseline = (GHOST_SIZE - (metrics.ascent() + metrics.descent())) // 2 \
                + metrics.ascent()
            painter.drawText(0, baseline, self._glyph)
            painter.end()
            self._cache_key, self._cache = key, pixmap
            self.renders += 1
        return self._cache

    def paintEvent(self, event):
        box = self.ghost_rect()
        if not self._glyph or not box.intersects(event.rect()):
            return
        painter = QPainter(self)
        painter.drawPixmap(box.topLeft(), self._pixmap())


def see_through(root):
    """Lets the ghost kanji show through every plain container under `root`.

    The sheet's global `QWidget { background: page }` fills any bare
    `QWidget`, and a page is a stack of them (the page, its panels, a row
    holding a button strip). Each would paint the page colour over the
    glyph. A plain container carries no fill of its own to lose, so it is
    marked `seeThrough` and the sheet makes it transparent.

    Only exact `QWidget`s are marked: a subclass is a control that owns
    its look (an input, a table viewport, a button) and keeps its fill,
    as does a scroll area's viewport. A container built later than this
    call is not marked; `tests/test_ghost_kanji.py` samples every page for
    exactly that.
    """
    from PyQt6.QtWidgets import QAbstractScrollArea

    for child in root.findChildren(QWidget):
        if type(child) is not QWidget:
            continue
        if isinstance(child.parentWidget(), QAbstractScrollArea):
            continue
        child.setProperty('seeThrough', True)


class Brackets(QWidget):
    """Four corner crop-marks over a framed region: the dossier frame.

    An overlay: `Brackets(frame)` lays itself over `frame`, follows its
    size, and stays on top of whatever the frame later gains. The mockup's
    `.frame__corner` marks sit on the frame's hairline, so the arms are
    drawn from the frame's outer corner inward.

    The overlay is masked down to the four L shapes, which does two jobs.
    Nothing is under the mouse but the marks themselves (and those are
    `WA_TransparentForMouseEvents`, so the frame's contents still take every
    click), and a table scrolling underneath never asks the overlay to
    repaint, because the overlay does not cover the part that moves.

    Colour comes from the sheet (`QWidget#Brackets { color: ink_65 }`).
    """

    def __init__(self, frame, arm=BRACKET_ARM, weight=BRACKET_WEIGHT):
        super().__init__(frame)
        self.setObjectName('Brackets')
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._arm = arm
        self._weight = weight
        frame.installEventFilter(self)
        self._fit()
        self.show()  # a child made after its parent is shown starts hidden

    def corner_rects(self):
        """The eight bars that make up the four L shapes, none overlapping."""
        w, h, a, t = self.width(), self.height(), self._arm, self._weight
        # The vertical bars start below the horizontal ones so the corner
        # square is covered once: translucent ink painted twice is darker.
        return [
            QRect(0, 0, a, t), QRect(0, t, t, a - t),
            QRect(w - a, 0, a, t), QRect(w - t, t, t, a - t),
            QRect(0, h - t, a, t), QRect(0, h - a, t, a - t),
            QRect(w - a, h - t, a, t), QRect(w - t, h - a, t, a - t),
        ]

    def _fit(self):
        frame = self.parentWidget()
        self.setGeometry(QRect(QPoint(0, 0), frame.size()))
        region = QRegion()
        for rect in self.corner_rects():
            region = region.united(QRegion(rect))
        self.setMask(region)
        self.raise_()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.ChildAdded,
                            QEvent.Type.Show):
            self._fit()
        return False

    def paintEvent(self, event):
        painter = QPainter(self)
        colour = self.palette().color(self.foregroundRole())
        for rect in self.corner_rects():
            painter.fillRect(rect, colour)


def apply_theme(root, mode):
    """Puts the sheet on the application."""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is not None:
        app.setStyleSheet(theme.stylesheet(mode))


class LinkLabel(QLabel):
    """Plain underlined text that acts like a link - no border, no fill,
    none of a QPushButton's visual weight. For an action that should read
    as the quietest thing on screen (see run_banner's "Discard" use)."""

    clicked = pyqtSignal()

    def __init__(self, text=''):
        super().__init__(f"<u>{text}</u>")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event):
        self.clicked.emit()
        super().mousePressEvent(event)


class WideComboBox(QComboBox):
    """A QComboBox whose dropdown popup widens to fit its longest item's
    full text. Qt's default popup width matches the closed combo box's
    own width, which would otherwise truncate something like a long
    match URL even though there's plenty of screen space to show it."""

    def showPopup(self):
        needed_width = self.view().sizeHintForColumn(0) + 40  # padding for scrollbar/margins
        if needed_width > self.view().minimumWidth():
            self.view().setMinimumWidth(needed_width)
        super().showPopup()


class CandidatePicker(WideComboBox):
    """The Review rail's match picker (U6): one mono line that ends in an
    ellipsis when it does not fit, and a visible chevron.

    Qt clips a closed combo's text hard at the edge, and the stylesheet's
    drop-down box (which has no arrow image) paints nothing, so both are
    drawn here. The popup is untouched and still lists the full text.
    """

    HEIGHT = 32          # `.picker`: height 32px
    CHEVRON = "\u25be"   # the mockup's ▾

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("CandidatePicker")
        self.setFixedHeight(self.HEIGHT)

    def elided_text(self):
        """What the closed picker shows for the current item."""
        option = self._option()
        edit = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox, option,
            QStyle.SubControl.SC_ComboBoxEditField, self)
        return self.fontMetrics().elidedText(
            self.currentText(), Qt.TextElideMode.ElideRight, edit.width())

    def _option(self):
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        return option

    def paintEvent(self, event):
        painter = QStylePainter(self)
        option = self._option()
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)
        option.currentText = self.elided_text()
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, option)
        arrow = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox, option,
            QStyle.SubControl.SC_ComboBoxArrow, self)
        painter.setPen(self.palette().color(self.palette().ColorRole.ButtonText))
        font = painter.font()
        font.setPixelSize(15)   # the 11px label size reads as a speck
        painter.setFont(font)
        painter.drawText(arrow, Qt.AlignmentFlag.AlignCenter, self.CHEVRON)


class ScaledImageLabel(QLabel):
    """A picture that fills its box sharply, at any size and screen scale.

    A plain QLabel shows a pixmap at whatever size it was handed, so the
    caller had to pre-scale to the label's size at that moment - which
    went stale the moment the box changed size (switching to Review,
    moving the splitter, resizing the window), and which was measured in
    logical pixels, so on a 125% display the result was stretched again
    by the compositor. This keeps the source and redraws from it on
    every resize, at the device pixel ratio.

    setPixmap takes the SOURCE; pixmap() returns what is on screen.
    """

    def __init__(self, text=""):
        super().__init__(text)
        self._source = QPixmap()

    def setPixmap(self, pixmap):
        self._source = QPixmap(pixmap) if pixmap is not None else QPixmap()
        self._render()

    def source_pixmap(self):
        return self._source

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render()

    # Never grow to the pixmap's size: with a pixmap's size as its hint, a
    # large picture would hold the layout open and the box could only get
    # bigger, never smaller.
    def sizeHint(self):
        return self.minimumSize().expandedTo(QSize(100, 100))

    def minimumSizeHint(self):
        return self.minimumSize()

    def _render(self):
        if self._source.isNull():
            super().setPixmap(QPixmap())
            return
        ratio = self.devicePixelRatioF()
        box = self.contentsRect().size()
        width = max(int(box.width() * ratio), 1)
        height = max(int(box.height() * ratio), 1)
        shown = self._source.scaled(
            width, height, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        shown.setDevicePixelRatio(ratio)
        super().setPixmap(shown)
