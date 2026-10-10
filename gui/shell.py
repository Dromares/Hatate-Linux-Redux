"""The window's chrome: what surrounds whichever half of the app you are in.

Three pieces, top to bottom.

**The top bar** carries the identity, the mode switcher and the one button
that starts work. `Start Search` used to live in the status bar - the
lowest-attention strip of the window, between a quota reading and a
progress bar - which is an odd place for the control the whole
application exists to run.

**The run strip** carries everything about a run in progress: how far, how
much longer, what it is waiting on, what the quota has left, how many have
gone to Hydrus. Those were five labels crammed into the status bar beside
the button. They are here, in one place, at a readable size, and visible
from every mode - because a batch takes hours, and the point of being able
to review while it runs is being able to see it running.

**The mode stack** is the rest of the window. The modes come from the two
halves the README already names: a search you start and walk away from,
and a review you sit and do a few thousand times.

The status bar keeps the one thing a status bar is for: the last thing
that happened.

Every widget here keeps the attribute name it had when it lived somewhere
else. `progress_bar` is still `progress_bar`. That is deliberate: the
workers, the shortcut handlers and a 4,800-line smoke test all reach for
these by name, and a redesign that renames them would be a rewrite
wearing a redesign's clothes.
"""
import re

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QProgressBar, QSizePolicy, QStackedWidget,
    QStatusBar, QVBoxLayout, QWidget,
)

from core.applog import get_logger
from gui import widgets

log = get_logger("gui.shell")

# The mockup's run strip is 220px of gauge and a hairline of line
# (tokens.css `.run-strip__track` is 3px; the gap register's R-02 asks 2).
GAUGE_WIDTH = 220
GAUGE_HEIGHT = 2

# What the strip says when no run has anything to report (R-05).
IDLE_PROGRESS = "nothing queued"
IDLE_ETA = "ETA —"
# What the strip says after a run that left nothing behind (R-06).
RUN_FAILED_NOTE = "last run failed — see crash log"

# A "figure" in a readout: the part the mockup sets in bold (`<b>7/8</b>
# searched`, `waiting <b>38s</b>`, `ETA <b>~2 min</b>`). A number with the
# punctuation and unit that travel with it - "1,234/24,000", "~16d", "38s",
# "14:20".
_FIGURE = re.compile(r"~?\d[\d,.:/]*[A-Za-z]*")


def split_figures(text):
    """`text` as [(fragment, is_figure), ...], in order, losing nothing."""
    out = []
    at = 0
    for hit in _FIGURE.finditer(text):
        if hit.start() > at:
            out.append((text[at:hit.start()], False))
        out.append((hit.group(), True))
        at = hit.end()
    if at < len(text):
        out.append((text[at:], False))
    return out


class Readout(QWidget):
    """One `label  FIGURE  label` reading in the run strip.

    The mockup sets the figures bold and bright against dim mono labels.
    A QLabel can only do that with inline rich-text colours, which would
    pin the colour to one theme; so each run of text is its own QLabel
    and the stylesheet (`RunReadout` / `RunFigure`) colours them.

    It answers to the QLabel calls the rest of the window already makes:
    `setText`, `text`, `setToolTip`. `text()` is the plain string, not
    markup - the figures are a presentation of it, never part of it.
    """

    def __init__(self, text=''):
        super().__init__()
        self.setObjectName('RunReadoutGroup')
        self._plain = None
        self._parts = []
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self.setText(text)

    def text(self):
        return self._plain

    def setText(self, text):
        text = text or ''
        if text == self._plain:
            return
        self._plain = text
        for part in self._parts:
            # Detached now, not just scheduled for deletion: until the event
            # loop runs deleteLater the old text would still paint, under
            # the new.
            self._layout.removeWidget(part)
            part.hide()
            part.setParent(None)
            part.deleteLater()
        self._parts = []
        for fragment, is_figure in split_figures(text):
            label = QLabel(fragment)
            label.setObjectName('RunFigure' if is_figure else 'RunReadout')
            widgets.apply_tracking(label, 0.08)
            self._layout.addWidget(label)
            self._parts.append(label)
        self.setAccessibleName(text)
        # An empty reading takes no room, so the strip's spacing does not
        # leave a gap where it was.
        self.setVisible(bool(text))


class _ModeStack(QStackedWidget):
    """A mode stack whose footprint follows only the page on screen.

    `QStackedLayout`'s default size constraint takes the minimum size
    hint as the max over *every* page it holds, shown or not - so the
    window's floor was always Activity's width and Review's height
    (whichever page needs the most of each), no matter which mode was
    actually visible. Delegating both hints to `currentWidget()` lets
    the floor mean the mode you are looking at, and overriding
    `setCurrentWidget` to invalidate this widget's cached hints means a
    mode switch is what grows the window back out, rather than relying
    on whatever `QStackedLayout` happens to do internally when a page
    is swapped.
    """

    def minimumSizeHint(self):
        current = self.currentWidget()
        if current is None:
            return super().minimumSizeHint()
        return current.minimumSizeHint()

    def sizeHint(self):
        current = self.currentWidget()
        if current is None:
            return super().sizeHint()
        return current.sizeHint()

    def setCurrentWidget(self, widget):
        super().setCurrentWidget(widget)
        self.updateGeometry()


MARK_GLYPH = "鏡"  # U+93E1, in the bundled Ryoku Kanji subset

# The ghost kanji behind each mode (G-07), all four in the bundled subset.
# The Empty state is not a mode of its own - it is a page of Queue - so its
# glyph is asked for through `ghost_glyph_for` while that page is showing.
GHOST_GLYPHS = {"queue": "力", "review": "鏡", "activity": "動"}
EMPTY_GHOST_GLYPH = "空"

# (key, label, tooltip). The order is the order of the work.
MODES = (
    ("queue", "Queue",
     "The list: add images, filter them, and run searches over them."),
    ("review", "Review",
     "One image at a time, your copy against the match, with the keys to decide."),
    ("activity", "Activity",
     "How the run is going, what each engine is doing, and the log."),
)


class ShellMixin:
    """Builds the chrome and owns the mode switch.

    A mixin rather than a widget, in MUR's manner: the window is one
    object and every part of it assigns to `self`, so there is no
    plumbing between a shell object and the views it contains.
    """

    # ------------------------------------------------------------------
    # Assembly
    # ------------------------------------------------------------------
    def _build_shell(self, pages):
        """Puts the chrome around `pages`, a {mode key: widget} mapping."""
        central = widgets.GhostKanji(GHOST_GLYPHS["queue"])
        self.ghost = central
        root = QVBoxLayout(central)
        root.setContentsMargins(*widgets.PAGE_MARGINS)
        root.setSpacing(widgets.PAGE_SPACING)

        root.addWidget(self._build_top_bar())
        root.addWidget(self._build_run_strip())

        self.mode_stack = _ModeStack()
        self._mode_pages = {}
        for key, _label, _tip in MODES:
            page = pages.get(key) or self._placeholder_page(key)
            self._mode_pages[key] = page
            self.mode_stack.addWidget(page)
        root.addWidget(self.mode_stack, 1)

        widgets.see_through(central)
        self.setCentralWidget(central)
        self.set_mode("queue")

    def _build_top_bar(self):
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(14)

        row.addWidget(self._build_app_mark())
        row.addSpacing(8)

        self.mode_switch, self.mode_buttons = widgets.segmented(
            MODES, on_change=self.set_mode, current="queue", role="Mode",
        )
        row.addWidget(self.mode_switch)
        row.addStretch(1)

        # The button that runs the application, at the size that says so.
        # Created here rather than in the status bar, which is where it
        # used to be.
        self.search_toggle_btn = widgets.pill_button(
            "▶ Start Search", self.action_toggle_search, primary=True,
        )
        row.addWidget(self.search_toggle_btn)

        row.addWidget(widgets.icon_button(
            "⚙", "Preferences…", self.action_open_settings, boxed=True,
        ))
        return bar

    def _build_app_mark(self):
        """The in-window wordmark: the kanji 鏡 (mirror) beside HATATE.

        `icon.svg` stays the window and taskbar icon; in the window itself
        the mockup's mark is type (G-01). The word is passed in sentence
        case and drawn uppercase by `QFont` capitalization, so the
        accessible name stays "Hatate".
        """
        mark = QWidget()
        mark.setAccessibleName("Hatate")
        row = QHBoxLayout(mark)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(9)

        glyph = QLabel(MARK_GLYPH)
        glyph.setObjectName('MarkGlyph')
        row.addWidget(glyph)

        name = QLabel("Hatate")
        name.setObjectName('MarkWord')
        widgets.uppercase_voice(name)
        row.addWidget(name)
        return mark

    def _build_run_strip(self):
        """Everything about a run, in one line, visible from every mode.

        Left to right, as the mockup has it: `00 // RUN`, the gauge, then
        `searched`, `sent`, `waiting`, the SauceNAO quota - and, alone on
        the right edge, the ETA. The ETA is the answer to "will it finish
        tonight?", so it takes the position that is read second.
        """
        card, layout = widgets.card()
        layout.setContentsMargins(18, 12, 18, 12)
        self.run_strip = card

        row = QHBoxLayout()
        row.setSpacing(16)

        self.run_tag = widgets.section_label("00 // Run")
        row.addWidget(self.run_tag)

        # A fixed width rather than the whole row: stretched across the
        # window an empty gauge reads as a run sitting at 0%, when what it
        # means is that nothing is running. 2px tall, with a track the eye
        # can find (`RunGauge` in the stylesheet) - the old 20px bar's
        # track was 1.08:1 against the strip, an empty black slab.
        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName('RunGauge')
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedSize(GAUGE_WIDTH, GAUGE_HEIGHT)
        self.progress_bar.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed,
        )
        row.addWidget(self.progress_bar, 0, Qt.AlignmentFlag.AlignVCenter)

        # Kept as separate readouts, with the names they have always had,
        # because each is refreshed by its own handler on its own signal.
        # The gauge means SEARCHED, so the count beside it is `n/m
        # searched`; what has gone to Hydrus is a different number
        # (`sent_count_label`) and is not what the gauge draws (R-04).
        self.run_progress_label = Readout()
        self.run_progress_label.setToolTip(
            "How far through the run: images searched out of the images in this run. "
            "This is what the gauge to the left draws. It is not the number sent to "
            "Hydrus, which is counted separately."
        )
        row.addWidget(self.run_progress_label)

        # A note about the run as a whole rather than a count - R-06's
        # `last run failed - see crash log`, and P12a's `stopped 2h 14m
        # ago`. Empty (and so out of the way) until something sets it.
        self.run_note_label = Readout()
        row.addWidget(self.run_note_label)

        self.sent_count_label = Readout()
        self.sent_count_label.setToolTip(
            "How many images in the list have been sent to Hydrus. Counts images "
            "Hydrus acknowledged holding; anything still queued with Hydrus's own "
            "downloader is shown separately until it's confirmed."
        )
        row.addWidget(self.sent_count_label)

        self.wait_countdown_label = Readout()
        self.wait_countdown_label.setToolTip(
            "Live countdown for whatever the app is currently waiting on - the rate-limit "
            "delay between searches, or confirming a Hydrus URL-importer download during "
            "auto-import."
        )
        row.addWidget(self.wait_countdown_label)

        self.saucenao_quota_label = Readout()
        row.addWidget(self.saucenao_quota_label)

        row.addStretch(1)

        self.run_eta_label = Readout()
        self.run_eta_label.setToolTip(
            "How much longer the run has to go at the pace measured so far. A search is "
            "paced at 45-75s an image by default, so this is the estimate that says "
            "whether a batch finishes tonight or next week.\n\n"
            "The pace is averaged over the whole run. Images served from the search "
            "cache cost almost nothing, so a batch with many of them finishes sooner "
            "than the delay alone would suggest, and the estimate follows as it learns."
        )
        row.addWidget(self.run_eta_label)

        # Never blank: an idle strip says so (R-05). The window overwrites
        # these as soon as there is anything to say.
        self.run_progress_label.setText(IDLE_PROGRESS)
        self.run_eta_label.setText(IDLE_ETA)

        layout.addLayout(row)
        return card

    def _placeholder_page(self, key):
        """A mode that has not been built yet still has to be something."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addStretch(1)
        label = widgets.muted("Not built yet.")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)
        layout.addStretch(1)
        return page

    def _build_status_bar(self):
        """What a status bar is actually for: the last thing that happened.

        The progress, the estimate, the countdown, the quota and the sent
        count have gone to the run strip, and the Start button to the top
        bar. What is left is one line of text, which is all this ever
        wanted to be.
        """
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_label = QLabel("Ready")
        self.status_bar.addWidget(self.status_label)

    # ------------------------------------------------------------------
    # Modes
    # ------------------------------------------------------------------
    def set_mode(self, key):
        """Shows one mode. Safe to call before the stack exists."""
        stack = getattr(self, "mode_stack", None)
        if stack is None:
            return
        page = self._mode_pages.get(key)
        if page is None:
            return
        if stack.currentWidget() is not page:
            stack.setCurrentWidget(page)
        self._current_mode = key
        self.set_ghost(self.ghost_glyph_for(key))

        button = getattr(self, "mode_buttons", {}).get(key)
        if button is not None and not button.isChecked():
            button.setChecked(True)

        self._on_mode_changed(key)

    def ghost_glyph_for(self, key):
        """The watermark a mode shows. A hook: the Queue swaps in the Empty
        glyph while it is showing its drop zone."""
        return GHOST_GLYPHS.get(key, "")

    def set_ghost(self, glyph):
        """Changes the watermark. Safe to call before the shell exists."""
        ghost = getattr(self, "ghost", None)
        if ghost is not None:
            ghost.set_glyph(glyph)

    def current_mode(self):
        return getattr(self, "_current_mode", "queue")

    def _on_mode_changed(self, key):
        """Hook for the views. Overridden where a mode has to catch up."""
