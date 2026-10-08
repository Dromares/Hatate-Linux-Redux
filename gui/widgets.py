"""The house widgets: the handful of shapes every view is built from.

Adapted from MUR's `gui_common.py`. MUR is PySide6 and this is PyQt6, so
the port is an import swap and nothing more - the two APIs agree on every
call used here.

The point of the file is that a card looks like a card everywhere without
anyone having to remember 18/16/18/16. Anything that decides a measurement
belongs here rather than in a view.
"""
from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QFont, QPixmap
from PyQt6.QtWidgets import (
    QButtonGroup, QComboBox, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from gui import theme

TRACKING_LABEL = 0.16  # transcribed from tokens.css's --tracking-label

# The house measurements. MUR's, kept to the number so the two
# applications measure the same.
CARD_MARGINS = (18, 16, 18, 16)
CARD_SPACING = 10
PAGE_MARGINS = (22, 18, 22, 18)
PAGE_SPACING = 14
SPLITTER_HANDLE = 12
INPUT_MIN_HEIGHT = 38     # a single-line edit crushed below this stops being readable


def card(title=None):
    """A square-cornered panel with an optional heading, plus its content layout.

    Returns the frame and the layout separately because the caller wants
    the frame (to put somewhere) and the layout (to fill) - handing back
    only the frame would mean every caller digging the layout back out.

    The frame is left named 'Card', which is what the stylesheet's
    `QFrame#Card` rule selects on.
    """
    frame = QFrame()
    frame.setObjectName('Card')
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(*CARD_MARGINS)
    layout.setSpacing(CARD_SPACING)
    if title:
        heading = QLabel(title)
        heading.setObjectName('Heading')
        layout.addWidget(heading)
    return frame, layout


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


def muted(text=''):
    label = QLabel(text)
    label.setObjectName('Muted')
    return label


def hint(text=''):
    label = QLabel(text)
    label.setObjectName('Hint')
    label.setWordWrap(True)
    return label


def pill_button(text, on_click=None, primary=False):
    """A button. `primary` gives it the flat ink-stamp fill and the weight."""
    button = QPushButton(text)
    if primary:
        button.setObjectName('Primary')
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if on_click is not None:
        button.clicked.connect(on_click)
    return button


def icon_button(glyph, tooltip='', on_click=None, checkable=False):
    """A borderless glyph button - the small ones that sit beside a heading."""
    button = QPushButton(glyph)
    button.setObjectName('IconButton')
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setCheckable(checkable)
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


def segmented(choices, on_change=None, current=None, role='Segment'):
    """Several readings of one thing, drawn as a single control.

    `choices` is [(key, label, tooltip)]. Returns the row widget and a
    {key: button} map, so a caller can re-check one later without keeping
    its own list.

    Exclusivity is a real QButtonGroup rather than hand-written
    uncheck-the-others code; the one-pill look is entirely the `segment`
    property plus the stylesheet, which is why the buttons can stay
    ordinary QPushButtons.
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


def apply_theme(root, mode):
    """Puts the sheet on the application."""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is not None:
        app.setStyleSheet(theme.stylesheet(mode))


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
