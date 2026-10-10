"""The row of controls above the list for narrowing what it shows.

A filter only changes what is DISPLAYED. It never removes or alters an
entry, and a search still runs over the whole list - the count on the
right says so out loud, because a list that silently shows a third of its
rows is how someone concludes the app lost their work.

Split out of MainWindow because none of this needs a window: it is a
widget that owns a selection and reports when that selection moves. The
owner decides what to do about it, which keeps the part that coordinates
the table, the selection and the thumbnail scheduling in one place and the
part that is purely about the filter in another.
"""
from __future__ import annotations

from typing import Iterable, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu, QToolButton,
)

from core.entry_filter import (
    EntryFilter, NO_SITE, sites_present, statuses_present, upscale_verdicts_present,
)
from gui import widgets


FILTER_BAND_MARGINS = (16, 10, 16, 10)
FILTER_BAND_SPACING = 12


def set_all_checked(actions, checked: bool) -> None:
    """Ticks or unticks every action without firing their handlers - each
    one would otherwise re-apply the filter, once per action."""
    for action in actions.values():
        action.blockSignals(True)
        action.setChecked(checked)
        action.blockSignals(False)


def summarise_selection(prefix: str, chosen: Optional[Iterable], present: Iterable) -> str:
    """The text for a filter button: "Status: all", "Site: Danbooru",
    "Status: 3 selected".

    `chosen` of None means "not filtering", and so does a chosen set that
    covers everything present - the two are the same thing to a reader,
    and saying "4 selected" when all four are ticked would suggest a
    filter is doing something when it is not.
    """
    present = set(present)
    if chosen is None or set(chosen) >= present:
        return f"{prefix}: all"
    chosen = set(chosen)
    if len(chosen) == 1:
        return f"{prefix}: {next(iter(chosen))}"
    return f"{prefix}: {len(chosen)} selected"


def describe_hidden(shown: int, total: int) -> str:
    """Says plainly that the rest are hidden, not gone."""
    return f"showing {shown:,} of {total:,} (the rest are hidden, not removed)"


def hidden_count_text(shown: int, total: int) -> str:
    """The right-hand reading in the bar: "3 hidden" (U4). The number is
    rows the filter is holding back, so it is `total - shown` and nothing
    else; `describe_hidden`'s longer sentence is its tooltip."""
    return f"{max(total - shown, 0):,} hidden"


class FilterBar(QFrame):
    """Filename text, plus multi-select status and site menus.

    A QFrame so the stylesheet can draw it as a hairline band (DAN-1160).

    Emits `changed` whenever the selection moves. It deliberately does not
    apply anything itself - the owner holds the table and decides.
    """

    changed = pyqtSignal()

    def __init__(self, entries_provider, parent=None):
        """`entries_provider` is called to get the CURRENT working list.

        A callable rather than the list itself: MainWindow rebinds
        self.entries wholesale (opening a session, clearing it), and a
        captured reference would go on describing the list that was there
        when the menus were last built.
        """
        super().__init__(parent)
        self._entries = entries_provider
        self.setObjectName("FilterBand")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(*FILTER_BAND_MARGINS)
        layout.setSpacing(FILTER_BAND_SPACING)

        self.filter_label = widgets.section_label("01 // Filter")
        layout.addWidget(self.filter_label)

        self.filter_text = QLineEdit()
        self.filter_text.setPlaceholderText("filename contains…")
        self.filter_text.setClearButtonEnabled(True)
        self.filter_text.textChanged.connect(lambda _t: self.changed.emit())
        # Flexes with the row (U4) - it shares the slack with the spacer
        # before the count, as the mockup's `.field--grow` + spacer do.
        layout.addWidget(self.filter_text, 1)

        # Status and site are multi-select, so they are menus on a button
        # rather than combo boxes - a combo would force one-at-a-time, and
        # "show me everything that failed OR errored" is the common ask.
        self.filter_status_button = QToolButton()
        self.filter_status_button.setText("Status: all")
        self.filter_status_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.filter_status_menu = QMenu(self.filter_status_button)
        self.filter_status_button.setMenu(self.filter_status_menu)
        self.filter_status_button.setToolTip(
            "Show only rows with the statuses you tick. Only the statuses "
            "actually present in the list are offered."
        )
        self._speak_caps(self.filter_status_button)
        layout.addWidget(self.filter_status_button)

        self.filter_site_button = QToolButton()
        self.filter_site_button.setText("Site: all")
        self.filter_site_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.filter_site_menu = QMenu(self.filter_site_button)
        self.filter_site_button.setMenu(self.filter_site_menu)
        self.filter_site_button.setToolTip(
            "Show only rows whose match came from the sites you tick, by the "
            "site shown in the Booru column.\n\n"
            f'"{NO_SITE}" collects rows with no match at all - not the same as '
            '"Other", which means a match from a site with no name of its own.'
        )
        self._speak_caps(self.filter_site_button)
        layout.addWidget(self.filter_site_button)

        self.filter_upscale_button = QToolButton()
        self.filter_upscale_button.setText("Upscale: all")
        self.filter_upscale_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.filter_upscale_menu = QMenu(self.filter_upscale_button)
        self.filter_upscale_button.setMenu(self.filter_upscale_menu)
        self.filter_upscale_button.setToolTip(
            "Show only rows with the Check for Upscaling verdict you tick."
        )
        self._speak_caps(self.filter_upscale_button)
        layout.addWidget(self.filter_upscale_button)

        self.filter_clear_button = widgets.link_button("Clear", self.clear)
        self.filter_clear_button.setToolTip("Show everything again.")
        layout.addWidget(self.filter_clear_button)

        layout.addStretch(1)

        self.filter_count_label = QLabel("")
        self.filter_count_label.setObjectName("FilterCount")
        self.filter_count_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.filter_count_label)

        # Chosen values live here rather than being read back off the
        # menus, so rebuilding the menus (which happens whenever the list
        # changes) cannot quietly drop a selection.
        self._statuses = None   # None = not filtering on status
        self._sites = None
        self._upscale_verdicts = None
        self._status_actions = {}
        self._site_actions = {}
        self._upscale_actions = {}

    @staticmethod
    def _speak_caps(button):
        """The page-level mono-caps voice (Q-01 casing, DAN-1161): the
        stylesheet's `voice="caps"` face plus an AllUppercase QFont, so
        `.text()` and the accessible name stay "Status: all"."""
        button.setProperty('voice', 'caps')
        widgets.uppercase_voice(button)

    # -- what the owner reads -------------------------------------------
    def current_filter(self) -> EntryFilter:
        return EntryFilter(
            statuses=self._statuses,
            sites=self._sites,
            upscale_verdicts=self._upscale_verdicts,
            text=self.filter_text.text(),
        )

    # -- what the owner drives ------------------------------------------
    def rebuild_menus(self) -> None:
        """Repopulates the status and site menus from what the list now
        holds. Offering only what is present keeps the menus short, and
        means a site can't be ticked into showing nothing.

        Each menu opens with All / None, the same shape the Sites menu on
        the menu bar already uses - with a dozen statuses or sites in the
        list, ticking them off one at a time to isolate one is tedious,
        and "None then tick the one I want" is the quicker way round.
        """
        self._status_actions = {}
        self.filter_status_menu.clear()
        entries = self._entries()
        present_statuses = statuses_present(entries)
        if present_statuses:
            self._add_all_none(self.filter_status_menu, self._set_all_statuses)
        for status in present_statuses:
            action = self.filter_status_menu.addAction(status.label)
            action.setCheckable(True)
            action.setChecked(self._statuses is None or status in self._statuses)
            action.toggled.connect(
                lambda checked, s=status: self._on_status_toggled(s, checked)
            )
            self._status_actions[status] = action

        self._site_actions = {}
        self.filter_site_menu.clear()
        present_sites = sites_present(entries)
        if present_sites:
            self._add_all_none(self.filter_site_menu, self._set_all_sites)
        for site in present_sites:
            action = self.filter_site_menu.addAction(site)
            action.setCheckable(True)
            action.setChecked(self._sites is None or site in self._sites)
            action.toggled.connect(
                lambda checked, s=site: self._on_site_toggled(s, checked)
            )
            self._site_actions[site] = action

        self._upscale_actions = {}
        self.filter_upscale_menu.clear()
        present_verdicts = upscale_verdicts_present(entries)
        if present_verdicts:
            self._add_all_none(self.filter_upscale_menu, self._set_all_upscale_verdicts)
        for verdict in present_verdicts:
            action = self.filter_upscale_menu.addAction(verdict)
            action.setCheckable(True)
            action.setChecked(self._upscale_verdicts is None or verdict in self._upscale_verdicts)
            action.toggled.connect(
                lambda checked, v=verdict: self._on_upscale_verdict_toggled(v, checked)
            )
            self._upscale_actions[verdict] = action

    def refresh(self, shown: int, total: int, active: bool) -> None:
        """Keeps the buttons and the count in step with the filter."""
        self.filter_status_button.setText(summarise_selection(
            "Status",
            self._statuses and {s.label for s in self._statuses},
            {s.label for s in statuses_present(self._entries())},
        ))
        self.filter_site_button.setText(summarise_selection(
            "Site", self._sites, sites_present(self._entries()),
        ))
        self.filter_upscale_button.setText(summarise_selection(
            "Upscale", self._upscale_verdicts, upscale_verdicts_present(self._entries()),
        ))
        self.filter_clear_button.setEnabled(active)
        self.filter_count_label.setText("" if not active else hidden_count_text(shown, total))
        self.filter_count_label.setToolTip("" if not active else describe_hidden(shown, total))

    def clear(self) -> None:
        """Back to showing everything, as one change rather than two.

        The text field is cleared with its signal blocked: letting it fire
        would emit `changed` for the text and again for the reset below,
        so the owner would rebuild the table twice for one click.
        """
        self._statuses = None
        self._sites = None
        self._upscale_verdicts = None
        blocked = self.filter_text.blockSignals(True)
        self.filter_text.clear()
        self.filter_text.blockSignals(blocked)
        self.changed.emit()

    # -- internals -------------------------------------------------------
    @staticmethod
    def _add_all_none(menu, setter):
        """Puts All / None at the top of a filter menu, above a separator."""
        all_action = menu.addAction("All")
        all_action.setToolTip("Tick everything - the same as not filtering on this at all.")
        all_action.triggered.connect(lambda: setter(True))
        none_action = menu.addAction("None")
        none_action.setToolTip(
            "Untick everything. The list goes empty until you tick something back on - "
            "which is the quick way to isolate one value out of many."
        )
        none_action.triggered.connect(lambda: setter(False))
        menu.addSeparator()

    def _set_all_statuses(self, checked: bool):
        set_all_checked(self._status_actions, checked)
        # Everything ticked is stored as None - "not filtering" - so the
        # filter reports itself inactive rather than listing every status
        # as an explicit choice. Nothing ticked is a real empty set, which
        # genuinely matches no rows.
        self._statuses = None if checked else set()
        self.changed.emit()

    def _set_all_sites(self, checked: bool):
        set_all_checked(self._site_actions, checked)
        self._sites = None if checked else set()
        self.changed.emit()

    def _set_all_upscale_verdicts(self, checked: bool):
        set_all_checked(self._upscale_actions, checked)
        self._upscale_verdicts = None if checked else set()
        self.changed.emit()

    def _on_status_toggled(self, status, checked: bool):
        present = set(statuses_present(self._entries()))
        current = set(present) if self._statuses is None else set(self._statuses)
        current.add(status) if checked else current.discard(status)
        # Everything ticked means "not filtering", which is stored as None
        # so the filter reports itself inactive rather than listing every
        # status as an explicit choice.
        self._statuses = None if current >= present else current
        self.changed.emit()

    def _on_site_toggled(self, site: str, checked: bool):
        present = set(sites_present(self._entries()))
        current = set(present) if self._sites is None else set(self._sites)
        current.add(site) if checked else current.discard(site)
        self._sites = None if current >= present else current
        self.changed.emit()

    def _on_upscale_verdict_toggled(self, verdict: str, checked: bool):
        present = set(upscale_verdicts_present(self._entries()))
        current = set(present) if self._upscale_verdicts is None else set(self._upscale_verdicts)
        current.add(verdict) if checked else current.discard(verdict)
        self._upscale_verdicts = None if current >= present else current
        self.changed.emit()
