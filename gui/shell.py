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
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QProgressBar, QSizePolicy, QStackedWidget,
    QStatusBar, QVBoxLayout, QWidget,
)

from core.applog import get_logger
from core.paths import RESOURCES_DIR
from gui import widgets

log = get_logger("gui.shell")


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
        central = QWidget()
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
            "▶  Start Search", self.action_toggle_search, primary=True,
        )
        row.addWidget(self.search_toggle_btn)

        row.addWidget(widgets.icon_button(
            "⚙", "Preferences…", self.action_open_settings,
        ))
        return bar

    def _build_app_mark(self):
        mark = QWidget()
        row = QHBoxLayout(mark)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(9)

        icon = QIcon(str(RESOURCES_DIR / "icon.svg"))
        if not icon.isNull():
            glyph = QLabel()
            glyph.setPixmap(icon.pixmap(26, 26))
            row.addWidget(glyph)

        name = QLabel("Hatate")
        name.setObjectName("Heading")
        row.addWidget(name)
        return mark

    def _build_run_strip(self):
        """Everything about a run, in one line, visible from every mode."""
        card, layout = widgets.card()
        layout.setContentsMargins(18, 12, 18, 12)
        self.run_strip = card

        row = QHBoxLayout()
        row.setSpacing(16)

        # Given a fixed width rather than the whole row: stretched across
        # the window an empty bar reads as a run sitting at 0%, when what
        # it means is that nothing is running. At this width it reads as
        # the gauge it is, and the labels beside it carry the sentence.
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedWidth(240)
        self.progress_bar.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed,
        )
        row.addWidget(self.progress_bar)

        # Kept as separate labels, with the names they have always had,
        # because each is refreshed by its own handler on its own signal.
        self.run_progress_label = widgets.muted()
        self.run_progress_label.setToolTip(
            "How far through the run, and how much longer it has to go at the pace "
            "measured so far. A search is paced at 45-75s an image by default, so this "
            "is the estimate that says whether a batch finishes tonight or next week.\n\n"
            "The pace is averaged over the whole run. Images served from the search "
            "cache cost almost nothing, so a batch with many of them finishes sooner "
            "than the delay alone would suggest, and the estimate follows as it learns."
        )
        row.addWidget(self.run_progress_label)

        self.wait_countdown_label = widgets.muted()
        self.wait_countdown_label.setToolTip(
            "Live countdown for whatever the app is currently waiting on - the rate-limit "
            "delay between searches, or confirming a Hydrus URL-importer download during "
            "auto-import."
        )
        row.addWidget(self.wait_countdown_label)

        self.saucenao_quota_label = widgets.muted()
        row.addWidget(self.saucenao_quota_label)

        self.sent_count_label = widgets.muted()
        self.sent_count_label.setToolTip(
            "How many images in the list have been sent to Hydrus. Counts images "
            "Hydrus acknowledged holding; anything still queued with Hydrus's own "
            "downloader is shown separately until it's confirmed."
        )
        row.addWidget(self.sent_count_label)
        row.addStretch(1)

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

        button = getattr(self, "mode_buttons", {}).get(key)
        if button is not None and not button.isChecked():
            button.setChecked(True)

        self._on_mode_changed(key)

    def current_mode(self):
        return getattr(self, "_current_mode", "queue")

    def _on_mode_changed(self, key):
        """Hook for the views. Overridden where a mode has to catch up."""
