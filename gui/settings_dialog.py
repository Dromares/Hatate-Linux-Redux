from __future__ import annotations

import json
from datetime import datetime, timezone

from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton,
    QKeySequenceEdit, QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem,
    QMessageBox, QTabWidget, QVBoxLayout, QWidget,
)
from PyQt6.QtGui import QDesktopServices, QKeySequence
from PyQt6.QtCore import Qt, QTimer, QUrl

from core.applog import get_logger
from core.config import Settings
from core.engines import ALL_ENGINES, label_for as engine_label, pipeline_summary
from core import lens_browser, mcp_audit, shortcuts as shortcut_registry, sidecar
from core.hydrus_client import HydrusClient, HydrusError
from core.mcp_server import HOST as MCP_HOST
from core.provenance_note import DEFAULT_NOTE_NAME
from gui import message, settings_search, theme, widgets

log = get_logger("gui.settings")

# mcp-settings-spec.md §7: tier id -> the column label shown in the
# Recent-tool-calls table and in a refused call's Outcome cell. Not the
# same strings as core/mcp_tools.py's own §8 refusal_message() template -
# that one picks "is"/"are" per tier for a grammatical sentence; this one
# is a fixed table column, spec'd as "Refused — {tier label} is off"
# unconditionally.
_MCP_TIER_LABELS = {
    "read": "Read",
    "local_write": "Reversible",
    "research": "Re-search",
    "hydrus_write": "Hydrus writes",
    "destructive": "Destructive",
}

# How many of the MCP audit log's most recent entries the table shows -
# mcp-settings-spec.md §7's "cap at 200 displayed rows".
_MCP_AUDIT_DISPLAY_LIMIT = 200


def _nonblank_lines(edit: QPlainTextEdit) -> list[str]:
    """The box's lines, trimmed, with blanks dropped."""
    return [line.strip() for line in edit.toPlainText().splitlines() if line.strip()]


def _read_mcp_audit_entries(path) -> list[dict]:
    """Every parseable line of the MCP audit log, oldest first.

    Never raises: a missing file means no calls yet, and a line that
    fails to parse - most likely the log's own last line, still
    mid-write by the server's background thread when this reads it - is
    silently dropped rather than aborting the whole read (mcp-settings-
    spec.md's "must not fall over on a partially-written final line").
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _mcp_time_cell(timestamp: str) -> str:
    """mcp-settings-spec.md §7: HH:MM:SS if today, else "MMM D, HH:MM:SS".
    The audit log's own timestamps (core/mcp_audit.py's _iso_now) are UTC;
    shown as recorded rather than converted, so this always matches what
    is actually on disk."""
    try:
        when = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return timestamp
    if when.date() == datetime.now(timezone.utc).date():
        return when.strftime("%H:%M:%S")
    return when.strftime("%b %-d, %H:%M:%S")


def _mcp_target_cell(entry: dict) -> str:
    row = entry.get("row")
    candidate_index = entry.get("candidate_index")
    if row is None:
        return "—"
    if candidate_index is None:
        return f"#{row}"
    return f"#{row} → candidate {candidate_index}"


def _mcp_outcome_weight(entry: dict) -> str:
    """The ink-ramp tier for one audit entry's Outcome cell - split out
    from _mcp_outcome_cell() below, with only literal `return`s, so the
    gui/theme.py call site that colors the cell is a direct,
    statically-resolvable function call. tests/test_theme.py's
    discover_ink_color_tiers() walks every ink_color() call site at scan
    time and refuses to guess at a dynamic tier - see its docstring."""
    if not entry.get("allowed") or entry.get("dry_run"):
        return "ink_100"
    tool = entry.get("tool", "")
    outcome = entry.get("outcome") or {}
    if tool in ("send_upload", "send_url", "download_send"):
        if outcome.get("success"):
            return "ink_65"
        return "ink_100" if (outcome.get("error") or outcome.get("skipped_reason")) else "ink_65"
    if tool == "research":
        if outcome.get("research_started") and outcome.get("completed"):
            return "ink_65"
        return "ink_100"
    if "error" in outcome:
        return "ink_100"
    return "ink_65"


def _mcp_outcome_cell(entry: dict) -> tuple[str, str]:
    """(cell text, ink-ramp tier) for one audit entry's Outcome column,
    per mcp-settings-spec.md §7's table."""
    tool = entry.get("tool", "")
    outcome = entry.get("outcome") or {}
    weight = _mcp_outcome_weight(entry)

    if not entry.get("allowed"):
        tier = entry.get("tier", "")
        tier_label = _MCP_TIER_LABELS.get(tier, tier)
        return f"Refused — {tier_label} is off", weight
    if entry.get("dry_run"):
        return "Recorded, not run — dry run is on", weight

    if tool in ("send_upload", "send_url", "download_send"):
        if outcome.get("success"):
            return f"Allowed — sent  {theme.STATUS_GLYPHS['sent']}", weight
        error = outcome.get("error") or outcome.get("skipped_reason")
        return (f"Error — {error}", weight) if error else ("Allowed", weight)
    if tool == "select_candidate":
        return f"Allowed — picked candidate {outcome.get('selected_candidate_index')}", weight
    if tool == "toggle_reviewed":
        word = "reviewed" if outcome.get("reviewed") else "unreviewed"
        return f"Allowed — marked {word}", weight
    if tool == "research":
        if outcome.get("research_started") and outcome.get("completed"):
            # The audit entry only records the final state, not a
            # before/after diff, so "new candidates" is approximated as
            # the total candidate count across the re-searched rows.
            count = sum(r.get("candidate_count", 0) for r in outcome.get("results") or [])
            return f"Allowed — re-searched, {count} new candidates", weight
        return f"Error — {outcome.get('reason', 'research did not complete')}", weight
    if tool == "remove_row":
        return "Allowed — row removed", weight
    if tool == "reset_result":
        return "Allowed — result reset", weight
    if "error" in outcome:
        return f"Error — {outcome['error']}", weight
    return "Allowed", weight


def _mcp_outcome_tooltip(entry: dict) -> str:
    """The full literal string the tool actually returned, per §7: short
    form in the cell, the whole thing on hover."""
    if not entry.get("allowed"):
        return entry.get("reason") or ""
    outcome = entry.get("outcome") or {}
    if entry.get("dry_run") and outcome.get("note"):
        return outcome["note"]
    return json.dumps(outcome, default=str)


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(480)
        self.settings = settings
        self._mode = theme.resolve_mode(settings.theme)
        # The MCP tab's Connection card needs a handle to the actually
        # running server (owned by MainWindow, not by this dialog) to
        # show live state and offer a real "Restart server" button - see
        # mcp-settings-spec.md §10 risk 1. `parent` is None in most tests
        # and in the harness construction below, which this tab treats as
        # "no live server to report on" rather than raising.
        self._mcp_main_window = parent

        # Wide enough for the widest row this dialog has - General's
        # "min [ ] max (seconds) [ ]" delay row - without it squeezing
        # the fields to a sliver.
        self.resize(940, 660)

        self.tabs = QTabWidget()
        self._pages = []
        for builder, name in (
            (self._build_general_tab, "General"),
            (self._build_engine_tab, "Engine"),
            (self._build_import_tab, "Import"),
            (self._build_saucenao_tab, "SauceNAO"),
            (self._build_hydrus_tab, "Hydrus"),
            (self._build_tag_namespaces_tab, "Tag Namespaces"),
            (self._build_site_logins_tab, "Site Logins"),
            (self._build_shortcuts_tab, "Shortcuts"),
            (self._build_mcp_tab, "MCP"),
        ):
            page = builder()
            self._pages.append((name, page))
            self.tabs.addTab(self._scrollable(page), name)

        # The audit table only tails while the MCP tab is the one actually
        # on screen - mcp-settings-spec.md §10 risk 3, the same lifecycle
        # discipline as ActivityViewMixin's own tab/page-visibility timer.
        self._mcp_tab_index = next(
            i for i, (name, _page) in enumerate(self._pages) if name == "MCP")
        self.tabs.currentChanged.connect(self._on_tabs_current_changed)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(*widgets.PAGE_MARGINS)
        layout.setSpacing(widgets.PAGE_SPACING)
        layout.addWidget(self._build_search_row())
        layout.addWidget(self.search_results)
        layout.addWidget(self.tabs)
        layout.addWidget(buttons)

    # -- finding a setting --------------------------------------------
    def _scrollable(self, page):
        """Every tab scrolls.

        Several of these are taller than a laptop screen, and a tab that
        cannot be scrolled simply does not show its last few settings.
        """
        area = QScrollArea()
        area.setWidget(page)
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return area

    def _build_search_row(self):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Find:"))

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText(
            "a setting's name - \"quota\", \"cookies\", \"thumbnail\"…")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(self._on_search)
        layout.addWidget(self.search_box, 1)

        self.search_results = QListWidget()
        self.search_results.setMaximumHeight(150)
        self.search_results.hide()
        # Both signals are wired on purpose, not left over: itemClicked
        # jumps on a single click for a fast type-then-click workflow,
        # itemActivated jumps on Enter for a keyboard-only one. A
        # double-click fires both (and itemClicked twice), which used to
        # corrupt the target's restored style - see settings_search.
        # highlight()'s docstring - but highlight() is now safe to call
        # re-entrantly, so the duplicate delivery is harmless.
        self.search_results.itemActivated.connect(self._jump_to_setting)
        self.search_results.itemClicked.connect(self._jump_to_setting)

        # Indexed once the tabs exist, and then left alone: these pages
        # are built once and their labels do not change.
        self._search_index = settings_search.index_tabs(self._pages)
        return row

    def _on_search(self, text):
        self.search_results.clear()
        found = settings_search.matches(self._search_index, text)
        if not found:
            self.search_results.setVisible(bool(text.strip()))
            if text.strip():
                item = QListWidgetItem("Nothing matches that.")
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                self.search_results.addItem(item)
            return

        for tab_index, tab_name, name, widget in found[:40]:
            item = QListWidgetItem(f"{tab_name}  ›  {name}")
            item.setData(Qt.ItemDataRole.UserRole, (tab_index, widget))
            self.search_results.addItem(item)
        self.search_results.show()

    def _jump_to_setting(self, item):
        payload = item.data(Qt.ItemDataRole.UserRole)
        if not payload:
            return
        tab_index, widget = payload
        self.tabs.setCurrentIndex(tab_index)
        area = self.tabs.widget(tab_index)
        if isinstance(area, QScrollArea):
            area.ensureWidgetVisible(widget, 0, 40)
        settings_search.highlight(widget)

    # -- General ------------------------------------------------------
    def _build_general_tab(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)
        outer.setSpacing(widgets.CARD_SPACING)

        performance = QGroupBox("Performance")
        form = QFormLayout(performance)

        self.delay_min = QDoubleSpinBox()
        self.delay_min.setRange(5, 600)
        self.delay_min.setValue(self.settings.delay_min_seconds)
        self.delay_max = QDoubleSpinBox()
        self.delay_max.setRange(5, 900)
        self.delay_max.setValue(self.settings.delay_max_seconds)
        delay_row = QHBoxLayout()
        delay_row.addWidget(QLabel("min"))
        delay_row.addWidget(self.delay_min)
        delay_row.addWidget(QLabel("max (seconds)"))
        delay_row.addWidget(self.delay_max)
        form.addRow("Delay between searches:", delay_row)

        # Per-engine overrides. Each engine is a separate host with its own
        # tolerance, and one global gap has to be set for the strictest of
        # them - so a run that queries only IQDB (what the README suggests
        # once SauceNAO's daily quota is spent) waits at SauceNAO's pace for
        # a service it never calls.
        #
        # Every engine starts at "same as above" rather than at a guessed
        # number: setting one too low is what gets an IP banned, and that is
        # not a judgement to make on the user's behalf.
        self.engine_delays = {}
        for engine in ALL_ENGINES:
            low, high = (self.settings.engine_delays or {}).get(engine, [0.0, 0.0])[:2] or (0.0, 0.0)
            spin_min = QDoubleSpinBox()
            spin_min.setRange(0, 600)
            spin_min.setSpecialValueText("same as above")  # shown at 0
            spin_min.setValue(float(low or 0.0))
            spin_max = QDoubleSpinBox()
            spin_max.setRange(0, 900)
            spin_max.setSpecialValueText("same as above")
            spin_max.setValue(float(high or 0.0))
            row = QHBoxLayout()
            row.addWidget(QLabel("min"))
            row.addWidget(spin_min)
            row.addWidget(QLabel("max (seconds)"))
            row.addWidget(spin_max)
            tip = (
                f"How long to leave between two requests to {engine_label(engine)} "
                "specifically.\n\n"
                "Left at \"same as above\", this engine uses the general delay. Set it "
                "only for a service whose limits you actually know - the general delay "
                "is the safe default, and a value that is too low risks getting your IP "
                "blocked by that site.\n\n"
                "The longer of the two waits always wins, so this can slow a search down "
                "but never speeds it past the general delay unless you set it lower here."
            )
            spin_min.setToolTip(tip)
            spin_max.setToolTip(tip)
            form.addRow(f"    \u21b3 {engine_label(engine)} delay:", row)
            self.engine_delays[engine] = (spin_min, spin_max)


        self.hash_source = QComboBox()
        self.hash_source.addItem("Hydrus hashes - read from the filename where possible", userData="hydrus")
        self.hash_source.addItem("Local hashes - always read every file", userData="local")
        source_index = self.hash_source.findData(self.settings.hash_source)
        self.hash_source.setCurrentIndex(source_index if source_index >= 0 else 0)
        self.hash_source.setToolTip(
            "Where a file's SHA256 comes from when adding a batch.\n\n"
            "Hydrus hashes: Hydrus names every file in its store after that file's own SHA256 - "
            "the very hash this app would otherwise spend time computing - so it's taken straight "
            "from the filename and the file is never opened. Files that aren't named that way are "
            "still read normally. On a large batch over a network share this is the difference "
            "between minutes and seconds. A few files per batch are read and checked against their "
            "names first, and if any disagree the whole batch is read properly instead.\n\n"
            "Local hashes: always read and hash every file's bytes. Slower, but makes no "
            "assumption about what filenames mean - use this if you have files named like hashes "
            "whose contents don't match.\n\n"
            "Both produce identical hashes for a genuine Hydrus store."
        )
        form.addRow("Hash source:", self.hash_source)

        self.thumbnail_source = QComboBox()
        self.thumbnail_source.addItem("Hydrus thumbnails where available (fastest)", userData="hydrus")
        self.thumbnail_source.addItem("Decode from the files themselves", userData="local")
        self.thumbnail_source.addItem("No row thumbnails", userData="off")
        thumb_index = self.thumbnail_source.findData(self.settings.thumbnail_source)
        self.thumbnail_source.setCurrentIndex(thumb_index if thumb_index >= 0 else 0)
        self.thumbnail_source.setToolTip(
            "Where the small preview in each table row comes from.\n\n"
            "Hydrus thumbnails: for files Hydrus already has, it serves its own stored "
            "thumbnail - a few KB, versus reading the whole multi-megabyte file just to shrink "
            "it. Anything Hydrus doesn't have is still decoded from the file as normal. Needs a "
            "Hydrus access key.\n\n"
            "Decode from the files: always read the file itself. Correct everywhere, but on a "
            "large batch over a network share it means pulling every byte across the wire.\n\n"
            "No row thumbnails: reads nothing at all. The fastest way to add a very large batch."
        )
        form.addRow("Row thumbnails:", self.thumbnail_source)

        self.lazy_thumbnails = QCheckBox("Only load thumbnails for rows on screen")
        self.lazy_thumbnails.setToolTip(
            "Generates thumbnails for the rows actually visible (plus a small buffer above and "
            "below), refreshing as you scroll, instead of generating one for every file the "
            "moment it's added.\n\n"
            "A table shows around twenty rows at a time, so for a batch of tens of thousands "
            "this is the difference between reading a handful of files and reading the whole "
            "library.\n\n"
            "Turn off to go back to generating every thumbnail up front."
        )
        self.lazy_thumbnails.setChecked(self.settings.lazy_thumbnails)
        form.addRow(self.lazy_thumbnails)

        self.hash_workers = QSpinBox()
        self.hash_workers.setRange(1, 16)
        self.hash_workers.setValue(self.settings.hash_workers)
        self.hash_workers.setToolTip(
            "How many files to hash at once when adding a batch. 1 = sequential (the safe "
            "default). Higher values often help a lot on a network share (each read has "
            "latency a single stream spends idle), but usually do nothing - or slightly hurt - "
            "on a local disk. Whichever you pick, the app logs its own MB/s for each batch "
            "(Help > View Logs), so you can compare values and see what your storage actually prefers."
        )
        form.addRow("Parallel file hashing:", self.hash_workers)
        outer.addWidget(performance)

        session_group = QGroupBox("Session")
        form = QFormLayout(session_group)

        self.restore_session = QCheckBox("Restore the image list on startup")
        self.restore_session.setToolTip(
            "Saves the working list when you close the app and reloads it next launch, so a "
            "restart doesn't re-hash files it has already seen - which for a large batch on a "
            "network share is minutes of work. Thumbnails regenerate in the background."
        )
        self.restore_session.setChecked(self.settings.restore_session_on_start)
        form.addRow(self.restore_session)

        self.autosave_session = QCheckBox("Autosave the image list while running")
        self.autosave_session.setToolTip(
            "Saves the working list periodically, not just when you close the app.\n\n"
            "Searching a large batch runs for hours or days; without this, a crash or power cut "
            "loses everything since launch - most importantly which files had already been sent "
            "to Hydrus, so you'd have no way to tell what still needs sending.\n\n"
            "This is the app's own automatic session. Sessions you save yourself from the File "
            "menu are never touched by it."
        )
        self.autosave_session.setChecked(self.settings.autosave_session)
        form.addRow(self.autosave_session)

        self.autosave_interval = QSpinBox()
        self.autosave_interval.setRange(10, 3600)
        self.autosave_interval.setSuffix(" seconds")
        self.autosave_interval.setValue(self.settings.autosave_interval_seconds)
        self.autosave_interval.setToolTip(
            "How often to autosave. The save is cheap - it writes a small JSON file and skips "
            "thumbnails entirely - so a short interval costs little, but there's no point going "
            "below the time it takes to search a single image."
        )
        form.addRow("Autosave every:", self.autosave_interval)
        outer.addWidget(session_group)

        logging_group = QGroupBox("Logging")
        form = QFormLayout(logging_group)

        self.log_matched = QCheckBox("Log matched URLs to a text file")
        self.log_matched.setChecked(self.settings.log_matched_urls)
        form.addRow(self.log_matched)

        self.log_path = QLineEdit(self.settings.log_file_path)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_log_path)
        log_row = QHBoxLayout()
        log_row.addWidget(self.log_path)
        log_row.addWidget(browse)
        form.addRow("Log file:", log_row)
        outer.addWidget(logging_group)

        tag_sources_group = QGroupBox("Tag sources shown in the tags list")
        form = QFormLayout(tag_sources_group)
        self.tag_sources_list = QListWidget()
        for name in ["User", "Hydrus", "Search engine", "Booru", "Hatate-linux"]:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if name in self.settings.enabled_tag_sources
                else Qt.CheckState.Unchecked
            )
            self.tag_sources_list.addItem(item)
        form.addRow(self.tag_sources_list)

        # Two separate sentences on purpose. Before DAN-72 the list above
        # was labelled "shown in the tags list" and did exactly that,
        # while the README claimed it also decided what got sent - so the
        # honest fix is to say plainly that the list is about looking,
        # and make sending a switch you have to ask for.
        self.filter_sent_tags = QCheckBox(
            "Also apply these sources to tags sent to Hydrus and written to exports"
        )
        self.filter_sent_tags.setChecked(self.settings.filter_sent_tags_by_source)
        self.filter_sent_tags.setToolTip(
            "Off by default, and left off for existing settings: until now every send "
            "carried every tag regardless of what was ticked above, so turning this on "
            "for you would have changed what your next send put in your Hydrus library.\n\n"
            "On, a send and an export carry only the sources ticked above - including "
            "none at all, if you untick everything."
        )
        form.addRow(self.filter_sent_tags)
        outer.addWidget(tag_sources_group)

        # Files > Write Tag Files (DAN-76). Two controls rather than one,
        # because they answer unrelated questions: what the file is called
        # is about which other tool is going to read it, and what happens
        # to an existing file is about the user's own data.
        sidecar_group = QGroupBox("Tag files written beside your images (Files > Write Tag Files)")
        form = QFormLayout(sidecar_group)
        self.sidecar_style = QComboBox()
        self.sidecar_style.addItem("image.jpg.txt - keeps the image's extension",
                                   userData=sidecar.STYLE_APPEND)
        self.sidecar_style.addItem("image.txt - replaces the image's extension",
                                   userData=sidecar.STYLE_REPLACE)
        style_index = self.sidecar_style.findData(sidecar.style_of(self.settings))
        self.sidecar_style.setCurrentIndex(style_index if style_index >= 0 else 0)
        self.sidecar_style.setToolTip(
            "Both forms are in real use and neither is readable by everything.\n\n"
            "image.jpg.txt is what Hydrus's own sidecar importer looks for, and it "
            "can't be confused with a text file of yours or collide with the sidecar "
            "for a same-named .png.\n\n"
            "image.txt is what the stable-diffusion training tooling expects. Pick it "
            "if that's what will read these, bearing in mind it's the form that can "
            "land on an existing file that has nothing to do with this app."
        )
        form.addRow("Name tag files:", self.sidecar_style)

        self.sidecar_overwrite = QComboBox()
        self.sidecar_overwrite.addItem("Ask me", userData=sidecar.OVERWRITE_ASK)
        self.sidecar_overwrite.addItem("Keep the existing file",
                                       userData=sidecar.OVERWRITE_SKIP)
        self.sidecar_overwrite.addItem("Replace it",
                                       userData=sidecar.OVERWRITE_OVERWRITE)
        overwrite_index = self.sidecar_overwrite.findData(
            sidecar.overwrite_policy_of(self.settings))
        self.sidecar_overwrite.setCurrentIndex(
            overwrite_index if overwrite_index >= 0 else 0)
        self.sidecar_overwrite.setToolTip(
            "What to do when a text file is already sitting where a tag file would "
            "go. This app cannot tell one it wrote on an earlier run from a note you "
            "wrote yourself, which is why the default asks.\n\n"
            "Either of the other two still shows the count and says which it is about "
            "to do before writing anything - it just doesn't make you choose each time."
        )
        form.addRow("Existing tag files:", self.sidecar_overwrite)
        outer.addWidget(sidecar_group)

        return w

    def _browse_log_path(self):
        path, _ = QFileDialog.getSaveFileName(self, "Choose log file", self.log_path.text())
        if path:
            self.log_path.setText(path)

    # -- Engine -------------------------------------------------------
    def _build_engine_tab(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)
        outer.setSpacing(widgets.CARD_SPACING)

        primary_group = QGroupBox("Primary search")
        form = QFormLayout(primary_group)

        self.search_timeout = QDoubleSpinBox()
        self.search_timeout.setRange(5, 300)
        self.search_timeout.setValue(self.settings.search_timeout)
        self.search_timeout.setSuffix(" seconds")
        self.search_timeout.setToolTip(
            "How long to wait for IQDB/SauceNAO and booru page requests before giving up. "
            "Separate from the Hydrus tab's timeout, which only applies to Hydrus API calls - "
            "a slow/unresponsive search request will now fail cleanly after this long instead "
            "of hanging indefinitely (or as long as your Hydrus timeout happens to allow)."
        )
        form.addRow("Search/booru request timeout:", self.search_timeout)

        self.primary_engine = QComboBox()
        self.primary_engine.addItem("IQDB", userData="iqdb")
        self.primary_engine.addItem("SauceNAO", userData="saucenao")
        primary_index = self.primary_engine.findData(self.settings.primary_engine)
        self.primary_engine.setCurrentIndex(primary_index if primary_index >= 0 else 0)
        form.addRow("Search first with:", self.primary_engine)

        self.secondary_engine_mode = QComboBox()
        self.secondary_engine_mode.addItem("Always search both, merge every site", userData="always")
        self.secondary_engine_mode.addItem("Only as a fallback, if the first finds nothing", userData="fallback")
        self.secondary_engine_mode.addItem("Never use the second engine", userData="disabled")
        mode_index = self.secondary_engine_mode.findData(self.settings.secondary_engine_mode)
        self.secondary_engine_mode.setCurrentIndex(mode_index if mode_index >= 0 else 0)
        self.secondary_engine_mode.setToolTip(
            "When set to \"Always\", IQDB and SauceNAO run at the same time - they are "
            "different hosts, so waiting on one then the other only lengthens the per-image wait."
        )
        form.addRow("Second engine:", self.secondary_engine_mode)

        self.fallback_below_similarity = QDoubleSpinBox()
        self.fallback_below_similarity.setRange(0, 100)
        self.fallback_below_similarity.setSuffix("%")
        self.fallback_below_similarity.setSpecialValueText("off")   # shown at 0
        self.fallback_below_similarity.setValue(self.settings.fallback_below_similarity)
        self.fallback_below_similarity.setToolTip(
            "Treat a weak result as no result, and let the fallback engines run anyway.\n\n"
            "Normally the fallback only runs when the first engine finds NOTHING, so a "
            "42% \"maybe\" stops the search just as firmly as a 96% certainty. Set a "
            "number here and anything below it counts as not good enough, so the "
            "remaining engines get a turn.\n\n"
            "Only engines that MEASURE similarity count towards this: IQDB, SauceNAO, "
            "IQDB 3D, trace.moe and Pawchive (an exact copy, so 100%). ascii2d, Yandex and the "
            "two Google engines score their "
            "results by position rather than by likeness, so their numbers cannot "
            "satisfy the threshold - Google Lens's first result is always 80%, which "
            "would otherwise clear a 75% setting on every image.\n\n"
            "Left at \"off\", nothing changes: any result at all still ends the search."
        )
        form.addRow("Fall back when the best match is under:", self.fallback_below_similarity)

        # Pipeline summary - updates live as checkboxes are toggled. The
        # initial refresh happens after every engine widget below is built
        # (see the signal-wiring loop later in this method), since
        # _update_engine_pipeline() reads all of them.
        self.engine_pipeline = QLabel()
        self.engine_pipeline.setWordWrap(True)
        self.engine_pipeline.setObjectName("Hint")
        form.addRow("Current pipeline:", self.engine_pipeline)
        outer.addWidget(primary_group)

        extras_group = QGroupBox("Extra engines")
        form = QFormLayout(extras_group)

        self.extras_only_as_fallback = QCheckBox(
            "Only run the extra engines when falling back")
        self.extras_only_as_fallback.setChecked(self.settings.extras_only_as_fallback)
        self.extras_only_as_fallback.setToolTip(
            "Holds ascii2d, trace.moe, IQDB 3D, Google Images, Google Lens and Yandex back, so "
            "they run only on the images IQDB/SauceNAO could not place well - rather "
            "than on every image.\n\n"
            "Worth turning on once the slow or metered engines are in use: Google Lens "
            "spends at least 90 seconds per image and Cloud Vision is billed after its "
            "free monthly allowance, and most images never need either.\n\n"
            "Pairs with the setting above. With that left \"off\", the extras run "
            "whenever the first engine finds nothing at all."
        )
        form.addRow(self.extras_only_as_fallback)

        self.enable_ascii2d = QCheckBox("Also search ascii2d (colour / feature, often finds Pixiv)")
        self.enable_ascii2d.setChecked(self.settings.enable_ascii2d)
        self.enable_ascii2d.setToolTip(
            "ascii2d.net colour and feature search, run in parallel with IQDB/SauceNAO.\n\n"
            "No API key. Strong at exact Pixiv and Twitter reposts IQDB often misses. "
            "Does not report a real similarity percentage - results are ordered, not scored, "
            "so they will not overturn a high-confidence IQDB match."
        )
        form.addRow(self.enable_ascii2d)

        self.enable_tracemoe = QCheckBox("Also search trace.moe (anime screenshots)")
        self.enable_tracemoe.setChecked(self.settings.enable_tracemoe)
        self.enable_tracemoe.setToolTip(
            "Identifies which anime a screenshot is from, which episode, and the timestamp.\n\n"
            "Off by default: anonymous use has a monthly quota, and a booru-tagging batch "
            "would burn it on images that are not screenshots. Use the right-click "
            "\"trace.moe only\" action for a handful of frames, or turn this on for a "
            "screenshot-only list.\n\n"
            "A match points at AniList (the series), not a booru post - there is no "
            "full-resolution still to download. The value is the series tag."
        )
        form.addRow(self.enable_tracemoe)

        self.tracemoe_min_sim = QDoubleSpinBox()
        self.tracemoe_min_sim.setRange(50, 100)
        self.tracemoe_min_sim.setSuffix("%")
        self.tracemoe_min_sim.setValue(self.settings.tracemoe_min_similarity)
        self.tracemoe_min_sim.setToolTip(
            "trace.moe false-positives a lot below ~85%. Results under this are dropped."
        )
        form.addRow("trace.moe minimum similarity:", self.tracemoe_min_sim)

        self.enable_iqdb3d = QCheckBox("Also search IQDB 3D (3d.iqdb.org, for 3D/CG)")
        self.enable_iqdb3d.setChecked(self.settings.enable_iqdb3d)
        self.enable_iqdb3d.setToolTip(
            "Same protocol as IQDB, against the 3D/CG index. Off by default so a 2D "
            "library does not spend a request per image on an index that will not match it."
        )
        form.addRow(self.enable_iqdb3d)

        self.enable_google_images = QCheckBox("Also search Google Images (the whole web, no tags)")
        self.enable_google_images.setChecked(self.settings.enable_google_images)
        self.enable_google_images.setToolTip(
            "Google's reverse image search, run in parallel with IQDB/SauceNAO.\n\n"
            "Searches the whole web instead of a booru index, so it is the one that can "
            "still find a source - a personal site, an article, an artist's own "
            "portfolio - when every booru engine comes back empty.\n\n"
            "Needs the Cloud Vision API key below. Google's public reverse-image page "
            "still accepts the upload, but it no longer puts any results in the page it "
            "sends back: they are fetched and drawn by Google's own JavaScript, which "
            "this app does not run. Without a key the search says so once and skips "
            "Google for the rest of the batch.\n\n"
            "Vision's index is not the same one the reverse image search in your browser "
            "uses, and it is weaker: it will sometimes find nothing for a picture Lens "
            "places straight away. It is the best route available without running "
            "Google's JavaScript, not an equal substitute.\n\n"
            "A hit carries no tags - only the page URL, plus whatever a booru parser can "
            "read if the page happens to be one it knows.\n\n"
            "Does not report a real similarity percentage - results are ordered by page "
            "relevance, not by how much the picture matches, so they are scored below "
            "ascii2d and will not overturn an IQDB or SauceNAO match."
        )
        form.addRow(self.enable_google_images)

        self.google_images_key = QLineEdit(self.settings.google_images_api_key)
        self.google_images_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.google_images_key.setToolTip(
            "A Google Cloud API key with the Cloud Vision API enabled, from "
            "console.cloud.google.com. Its web-detection feature is what actually answers "
            "\"which pages is this image on\".\n\n"
            "Free for the first 1,000 images a month at the time of writing, billed after "
            "that - so on a large library, set the engine delay or leave this engine off "
            "for the bulk run and use the right-click \"Google Images only\" action on "
            "images nothing else could place.\n\n"
            "Stored in the config file alongside your Hydrus and SauceNAO keys, which is "
            "kept readable only by you."
        )
        form.addRow("    \u21b3 Cloud Vision API key:", self.google_images_key)

        self.enable_google_lens = QCheckBox("Also search Google Lens (drives a real browser)")
        self.enable_google_lens.setChecked(self.settings.enable_google_lens)
        self.enable_google_lens.setToolTip(
            "The reverse image search you get by dropping a picture into "
            "images.google.com - a different index from the Cloud Vision engine above. "
            "It returns the pages a picture actually appears on: DeviantArt, Reddit, "
            "Tumblr, Instagram, personal sites.\n\n"
            "Needs Playwright and a Chromium build, which are optional and not installed "
            "by default (~150MB, and nothing else here uses them). In this app's "
            "virtualenv:\n"
            "    venv/bin/pip install playwright\n"
            "    venv/bin/playwright install chromium\n\n"
            "A Chromium window opens while a search runs. That is deliberate: headless "
            "is challenged on sight, and a visible window is what lets you answer "
            "Google's robot check when it appears. The answer is remembered in a profile "
            "under ~/.config/hatate-linux/lens_profile, so it should be rare.\n\n"
            "Lens's \"Exact matches\" tab renders its results with no link in the page "
            "at all, so those can only be recovered where the title carries the post id "
            "(Paheal does). \"Visual matches\" embeds its URLs and comes through "
            "directly. Exact matches sort above visual ones.\n\n"
            "Google hides the matches for an explicit image behind a \"these results may "
            "be explicit\" confirmation. The engine clicks through that for you, so a "
            "library full of them does not need a click per image.\n\n"
            "The slowest engine here - each image is a real page load, so it gets at "
            "least 90 seconds whatever the timeout above says. No tags, only the source "
            "page, and no real similarity percentage: results are scored below ascii2d "
            "and will not overturn an IQDB or SauceNAO match.\n\n"
            "Best used through right-click \"Google Lens only\" on the few images "
            "nothing else could place, rather than across a whole library."
        )
        # Said here as well as at search time, because here is where it can
        # still be acted on. Turning the engine on and finding out only
        # from the first image of a long batch - whose other engines ran
        # normally, so the run looks fine - is how this went unnoticed.
        self.enable_google_lens.toggled.connect(self._warn_if_lens_cannot_run)
        form.addRow(self.enable_google_lens)

        self.lens_search_whole_image = QCheckBox(
            "    \u21b3 Search the whole picture, not the part Lens picks out")
        self.lens_search_whole_image.setChecked(self.settings.lens_search_whole_image)
        self.lens_search_whole_image.setToolTip(
            "Google Lens chooses a \"search area\" out of the uploaded picture and "
            "searches only that. Usually it takes the whole image, but it can settle on "
            "one small detail - a face in a group shot, a single panel of a page - and "
            "then the results are about that detail rather than the picture you "
            "searched for.\n\n"
            "With this on, the crop is dragged back out to the whole image - the same "
            "thing you would do by hand - and only on the images Lens actually "
            "cropped.\n\n"
            "Turn it off to take Lens's own choice, which is occasionally the smarter "
            "one - it can pick the character out of a busy collage."
        )
        form.addRow(self.lens_search_whole_image)

        self.enable_yandex = QCheckBox(
            "Also search Yandex (finds rule34.us / Danbooru posts the others miss)")
        self.enable_yandex.setChecked(self.settings.enable_yandex)
        self.enable_yandex.setToolTip(
            "yandex.com's reverse image search, over plain HTTP - no browser, so it is "
            "far quicker than Google Lens.\n\n"
            "Tried on 72 images IQDB, SauceNAO and Lens had found nothing or only a poor "
            "match for, it placed 7 on a post the app can tag from - mostly rule34.us, "
            "which IQDB does not index, plus Danbooru and Xbooru.\n\n"
            "Yandex finds a great deal more than that - Pinterest, Telegraph, reposting "
            "blogs - but none of it carries tags, so only posts on sites the app can "
            "read are kept. Like the Google engines it gives no similarity of its own: "
            "each result is measured against your picture from its thumbnail.\n\n"
            "If Yandex asks for a captcha, it rests for half an hour and the other "
            "engines carry on.\n\n"
            "Off by default because each picture is uploaded to Yandex's servers."
        )
        form.addRow(self.enable_yandex)

        self.enable_pawchive = QCheckBox(
            "Also look files up on Pawchive (exact copies of Patreon / Fanbox posts)")
        self.enable_pawchive.setChecked(self.settings.enable_pawchive)
        self.enable_pawchive.setToolTip(
            "Asks pawchive.pw whether it holds this exact file, by the file's SHA-256 "
            "hash. SauceNAO and IQDB don't index pawchive, so without this its posts "
            "never come up.\n\n"
            "A hit is the very same file, so it's a certain 100% - and the post brings "
            "its artist and tags with it. Resized or re-saved copies have a different "
            "hash and are not found.\n\n"
            "One small request per image and nothing uploaded, so it always runs "
            "alongside the first engine, even with \"Only run the extra engines when "
            "falling back\" on.\n\n"
            "Off by default because it sends each file's hash (not the picture) to "
            "pawchive.pw."
        )
        form.addRow(self.enable_pawchive)

        self.enable_pawchive_index = QCheckBox(
            "Search my Pawchive index (resized or re-saved copies)")
        self.enable_pawchive_index.setChecked(self.settings.enable_pawchive_index)
        self.enable_pawchive_index.setToolTip(
            "Compares each picture against the pawchive artists indexed under "
            "Files > Pawchive Index, so copies whose file has changed are found too.\n\n"
            "Entirely local: nothing is sent, and until an artist is indexed it does "
            "nothing at all."
        )
        form.addRow(self.enable_pawchive_index)
        outer.addWidget(extras_group)

        matching_group = QGroupBox("Match handling")
        form = QFormLayout(matching_group)

        self.use_search_cache = QCheckBox("Reuse previous search results for images searched before")
        self.use_search_cache.setToolTip(
            "Skips a fresh IQDB/SauceNAO request if this exact image (by file content, not "
            "filename) has already been searched before, using the saved result instead. "
            "\"Re-search\" and \"Search with Opposite Engine\" always bypass this and get a fresh result."
        )
        self.use_search_cache.setChecked(self.settings.use_search_cache)
        form.addRow(self.use_search_cache)

        self.search_cache_ttl = QDoubleSpinBox()
        self.search_cache_ttl.setRange(0, 3650)
        self.search_cache_ttl.setSuffix(" days")
        self.search_cache_ttl.setValue(self.settings.search_cache_ttl_days)
        self.search_cache_ttl.setToolTip(
            "How long a cached \"not found\" stays trusted before the image is searched again. "
            "Sites index new work constantly, so an old negative result says little about today - "
            "and once cached it is never rechecked. Set 0 to never expire.\n\n"
            "Successful matches are never aged out: they don't go stale the same way, and "
            "re-running them would spend rate-limited quota to reach the same answer."
        )
        form.addRow("Re-search \"not found\" after:", self.search_cache_ttl)

        self.retrieve_booru_tags = QCheckBox("Retrieve tags from matched booru page")
        self.retrieve_booru_tags.setChecked(self.settings.retrieve_tags_from_booru)
        form.addRow(self.retrieve_booru_tags)

        self.rank_by_quality = QCheckBox("Recommend the most useful match, not just the most similar")
        self.rank_by_quality.setToolTip(
            "Orders the match dropdown so its first entry - the one selected by default - is "
            "the best match you can actually use, rather than whichever scored highest on "
            "visual similarity alone.\n\n"
            "Similarity still decides: the quality adjustment is capped, so a clearly better "
            "match always stays on top. It only reorders matches that are already about "
            "equally likely to be the same image, preferring ones on sites this app can read "
            "tags from, and ones at a higher resolution than your local file.\n\n"
            "Turn off for strict similarity order."
        )
        self.rank_by_quality.setChecked(self.settings.rank_matches_by_quality)
        form.addRow(self.rank_by_quality)

        self.drop_dead_matches = QCheckBox("Discard matches whose source page is gone (404)")
        self.drop_dead_matches.setToolTip(
            "While searching, if a match's source page returns a definite 404/410 - the post "
            "was deleted or removed - drop it and use the next best match instead, rather than "
            "presenting a dead link as a result. Only applies to definite 404/410 responses; a "
            "timeout, rate-limit or block leaves the match alone, since those don't mean the "
            "content is gone. Requires \"Retrieve tags from matched booru page\" above."
        )
        self.drop_dead_matches.setChecked(self.settings.drop_dead_matches)
        form.addRow(self.drop_dead_matches)

        self.drop_restricted_matches = QCheckBox("Drop matches the site won't show without an account")
        self.drop_restricted_matches.setToolTip(
            "Some sites list a post but hold it back unless you're signed in:\n\n"
            "  - Danbooru keeps banned posts (taken down at an artist's request) and posts "
            "with censored tags viewable only with a Gold account. They come back looking "
            "like normal matches, but the image can't be fetched, so there's nothing to "
            "preview, compare, download or send to Hydrus. The tags do still come through.\n"
            "  - Anime-Pictures answers its API with 403 for account-only posts, which "
            "carries nothing at all - no tags, no dimensions, not even a preview.\n"
            "  - DeviantArt serves adult deviations BLURRED when logged out. The artist "
            "still comes through, but the picture can't be compared against your file, "
            "which is the whole point of having it.\n\n"
            "With this on they're dropped like any other unusable match, and the reason is "
            "written to the log. Turn it off to keep them listed.\n\n"
            "Signing in is the real fix: cookies under Site Logins make these load normally."
        )
        self.drop_restricted_matches.setChecked(self.settings.drop_restricted_matches)
        form.addRow(self.drop_restricted_matches)
        outer.addWidget(matching_group)

        # Connect engine widgets to live-update the pipeline summary
        for widget in (
            self.primary_engine, self.secondary_engine_mode,
            self.fallback_below_similarity, self.extras_only_as_fallback,
            self.enable_ascii2d, self.enable_tracemoe, self.enable_iqdb3d,
            self.enable_google_images, self.enable_google_lens, self.enable_yandex,
            self.enable_pawchive, self.enable_pawchive_index,
        ):
            if hasattr(widget, "currentIndexChanged"):
                widget.currentIndexChanged.connect(self._update_engine_pipeline)
            elif hasattr(widget, "valueChanged"):
                widget.valueChanged.connect(self._update_engine_pipeline)
            elif hasattr(widget, "toggled"):
                widget.toggled.connect(self._update_engine_pipeline)
            elif hasattr(widget, "stateChanged"):
                widget.stateChanged.connect(self._update_engine_pipeline)

        self._update_engine_pipeline()

        return w

    def _warn_if_lens_cannot_run(self, enabled: bool) -> None:
        """An early warning when Google Lens is switched on without its
        optional dependencies.

        Not a veto: the checkbox stays ticked. The user may be about to
        install Playwright, or be running a second copy of the app from a
        venv that has it, and a settings dialog that refuses the choice
        would be wrong in both cases. It only means the "no results" that
        would otherwise follow is explained before the search rather than
        after it.

        Costs an import and a directory listing - no browser is started -
        so it is cheap enough to sit on a toggle.
        """
        if not enabled:
            return
        status = lens_browser.check_dependencies()
        if status.ok:
            return
        log.info("Google Lens was enabled but %s is not installed", status.missing)
        box = message.build(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Google Lens is not installed yet")
        box.setText(
            "Google Lens is on, but it cannot run yet: "
            + ("the Playwright package it needs is not installed."
               if status.missing == lens_browser.MISSING_PLAYWRIGHT
               else "Playwright is installed, but the Chromium browser it drives is not.")
            + "\n\nSearches will carry on with the other engines and Lens will be "
              "skipped until this is fixed."
        )
        box.setDetailedText(status.message)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()
        box.deleteLater()

    # -- Import -----------------------------------------------------------
    def _build_import_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)

        self.borrow_tags = QCheckBox("Take tags from another match when the best one has none")
        self.borrow_tags.setToolTip(
            "Some sites cannot supply tags at all. MangaDex has no booru-style tags, and "
            "Sankaku can only be found by file hash - so a re-encoded or resaved local file "
            "can never be traced back to its post. Between them they were 94% of the "
            "untagged matches on a real library.\n\n"
            "Often another site's copy of the SAME picture is already among the matches, "
            "with a full tag list, and only the best match's page is normally read. This "
            "reads one more and uses its tags.\n\n"
            "The chosen match does not change - it is still the best picture, and the tag "
            "list says which match the tags actually came from."
        )
        self.borrow_tags.setChecked(self.settings.borrow_tags_from_other_matches)
        form.addRow(self.borrow_tags)

        self.borrow_tags_slack = QDoubleSpinBox()
        self.borrow_tags_slack.setRange(0.0, 25.0)
        self.borrow_tags_slack.setSingleStep(1.0)
        self.borrow_tags_slack.setSuffix("  percentage points")
        self.borrow_tags_slack.setValue(self.settings.borrow_tags_similarity_slack)
        self.borrow_tags_slack.setToolTip(
            "How far BELOW the chosen match's similarity another match may be and still "
            "lend its tags.\n\n"
            "This is the guard against tagging your image with a different picture's tags. "
            "A candidate within a few points is the same picture on another site, which is "
            "exactly what makes its tags worth having; a much weaker one may be something "
            "else entirely.\n\n"
            "0 allows only an equal or better match to lend - the strictest setting, and on "
            "a measured library it covered 14% of the untagged matches, because ranking puts "
            "the strongest first and the rest sit a point or two behind. 5 covered 76%."
        )
        form.addRow("    …and it may be weaker by up to:", self.borrow_tags_slack)

        self.retry_failed_searches = QCheckBox("Retry images that failed on a network error, once, at the end of a run")
        self.retry_failed_searches.setToolTip(
            "An image marked Error failed on a timeout, a 502, or a rate limit - a network "
            "fault rather than a verdict about the image. Those results are never cached, "
            "precisely so they can be tried again, but until now nothing did: the run ended "
            "and the rows sat there until you noticed and re-searched them by hand.\n\n"
            "With this on, each one gets exactly one more attempt once the first pass "
            "finishes. One, so an image that keeps failing cannot loop - and none are "
            "retried if you have stopped the run or SauceNAO's daily allowance has gone, "
            "since those would just fail the same way."
        )
        self.retry_failed_searches.setChecked(self.settings.retry_failed_searches)
        form.addRow(self.retry_failed_searches)

        self.remove_after_import = QCheckBox("Remove image(s) from the list after importing to Hydrus")
        self.remove_after_import.setToolTip(
            "Applies to any of the Hydrus send actions in the right-click menu. "
            "Only removes images that were actually sent successfully - failed ones stay for a retry."
        )
        self.remove_after_import.setChecked(self.settings.remove_after_import)
        form.addRow(self.remove_after_import)

        self.auto_import_enabled = QCheckBox("Automatically send confident matches to Hydrus during search")
        self.auto_import_enabled.setToolTip(
            "As each image in a batch is searched, if the match meets the similarity threshold "
            "below, it's sent to Hydrus immediately using the chosen method - before the next "
            "image in the batch is searched. Requires a Hydrus access key (Hydrus tab)."
        )
        self.auto_import_enabled.setChecked(self.settings.auto_import_enabled)
        form.addRow(self.auto_import_enabled)

        self.auto_import_min_similarity = QDoubleSpinBox()
        self.auto_import_min_similarity.setRange(0, 100)
        self.auto_import_min_similarity.setSuffix("%")
        self.auto_import_min_similarity.setValue(self.settings.auto_import_min_similarity)
        form.addRow("Minimum similarity to auto-import:", self.auto_import_min_similarity)

        self.auto_import_method = QComboBox()
        self.auto_import_method.addItem("Send File + URL + Tags (upload)", userData="upload")
        self.auto_import_method.addItem("Send URL to Hydrus's URL Importer", userData="url_importer")
        self.auto_import_method.addItem("Download Matched Image + Send with Tags", userData="download_send")
        idx = self.auto_import_method.findData(self.settings.auto_import_method)
        self.auto_import_method.setCurrentIndex(idx if idx >= 0 else 0)
        self.auto_import_method.setToolTip(
            "Same three methods as the right-click menu. If \"Remove image(s) from the list "
            "after importing\" above is also on, the URL Importer method will wait (up to the "
            "timeout below) for Hydrus to actually confirm the download finished before "
            "removing it - so this batch may pause longer on images sent this way."
        )
        form.addRow("Auto-import method:", self.auto_import_method)

        self.url_import_confirm_timeout = QDoubleSpinBox()
        self.url_import_confirm_timeout.setRange(5, 600)
        self.url_import_confirm_timeout.setSuffix(" seconds")
        self.url_import_confirm_timeout.setValue(self.settings.url_import_confirm_timeout)
        self.url_import_confirm_timeout.setToolTip(
            "How long to wait for Hydrus to confirm a URL-importer download actually finished "
            "(used by both the right-click action and auto-import) before giving up and "
            "leaving the image in the list rather than risk removing it too early. A live "
            "countdown shows next to the status bar while this is happening."
        )
        form.addRow("Hydrus import confirm timeout:", self.url_import_confirm_timeout)

        self.url_import_confirm_interval = QDoubleSpinBox()
        self.url_import_confirm_interval.setRange(0.5, 30)
        self.url_import_confirm_interval.setSuffix(" seconds")
        self.url_import_confirm_interval.setValue(self.settings.url_import_confirm_interval)
        self.url_import_confirm_interval.setToolTip("How often to check Hydrus while waiting.")
        form.addRow("Check interval:", self.url_import_confirm_interval)

        return w

    # -- Site Logins ------------------------------------------------------
    def _build_site_logins_tab(self) -> QWidget:
        """Credentials for individual sites. Kept apart from the rest of
        settings because these are secrets rather than preferences - they
        are stored in plain text in the config file, and it is worth that
        being obvious rather than buried among unrelated options."""
        w = QWidget()
        form = QFormLayout(w)

        form.addRow(QLabel(
            "Credentials for sites that hide content from logged-out visitors.\n"
            "Stored in plain text in your config file - treat them like passwords."
        ))

        self.pixiv_session_cookie = QLineEdit(self.settings.pixiv_session_cookie)
        self.pixiv_session_cookie.setEchoMode(QLineEdit.EchoMode.Password)
        self.pixiv_session_cookie.setPlaceholderText("PHPSESSID value from a logged-in browser session")
        self.pixiv_session_cookie.setToolTip(
            "Pixiv no longer supports username/password sign-in for third-party tools, so this "
            "uses a session cookie instead. In your browser, log in to Pixiv, open developer "
            "tools (F12) > Application/Storage > Cookies > pixiv.net, and copy the value of "
            "PHPSESSID.\n\n"
            "This lets the app see works that require login and R-18 content, which Pixiv hides "
            "from logged-out requests. Treat it like a password: it grants access to your "
            "account and is stored in plain text in the config file. It also expires "
            "periodically - re-copy it when Pixiv results start coming back empty."
        )
        form.addRow("Pixiv session cookie:", self.pixiv_session_cookie)

        pixiv_test_btn = QPushButton("Test Pixiv…")
        pixiv_test_btn.setToolTip(
            "Runs the real parser against a Pixiv artwork URL you provide and reports exactly "
            "what came back - HTTP status, whether the page data was found, and how many tags "
            "and image URLs were extracted. Uses the cookie currently typed above, so you can "
            "check a new one before saving."
        )
        pixiv_test_btn.clicked.connect(self._test_pixiv)
        self.pixiv_test_result = QLabel("")
        self.pixiv_test_result.setWordWrap(True)
        form.addRow(pixiv_test_btn, self.pixiv_test_result)

        self.e621_username = QLineEdit(self.settings.e621_username)
        self.e621_username.setPlaceholderText("your e621 username")
        self.e621_username.setToolTip(
            "e621 keeps some posts under a global blacklist. The API still returns them, "
            "but withholds the file - so the match arrives with no picture to preview, "
            "download or compare, and often no tags either. Signing in shows them.\n\n"
            "Unlike the cookie-based sites here, e621 provides a proper API key for "
            "third-party tools, so no browser cookie-hunting is needed."
        )
        form.addRow("e621 username:", self.e621_username)

        self.e621_api_key = QLineEdit(self.settings.e621_api_key)
        self.e621_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.e621_api_key.setPlaceholderText("from Account > Manage API Access on e621")
        self.e621_api_key.setToolTip(
            "On e621: Account > Manage API Access. Treat it like a password - it grants "
            "access to the account it came from.\n\n"
            "Both fields are needed; a username on its own authenticates nothing and is "
            "ignored.\n\n"
            "A WRONG key is worse than none: e621 answers 401 to every request carrying "
            "one, including posts that would have worked anonymously. If that happens the "
            "app says so on the match rather than reporting a generic failure - clear both "
            "fields to go back to browsing anonymously."
        )
        form.addRow("e621 API key:", self.e621_api_key)

        e621_test_btn = QPushButton("Test e621 login…")
        e621_test_btn.setToolTip(
            "Sends one authenticated request to e621 with the username and key typed "
            "above, and reports whether they were accepted.\n\n"
            "Worth doing before a run: a WRONG key is worse than none, because e621 "
            "answers 401 to every request carrying it - including ones that would have "
            "worked anonymously."
        )
        e621_test_btn.clicked.connect(self._test_e621)
        self.e621_test_result = QLabel("")
        self.e621_test_result.setWordWrap(True)
        form.addRow(e621_test_btn, self.e621_test_result)

        self.sankaku_cookies = QLineEdit(self.settings.sankaku_cookies)
        self.sankaku_cookies.setEchoMode(QLineEdit.EchoMode.Password)
        self.sankaku_cookies.setPlaceholderText("_sankakuchannel_session=...; locale=en")
        self.sankaku_cookies.setToolTip(
            "Sankaku hides adult and account-only posts from logged-out requests - it shows "
            "a \"No Content\" page rather than returning an error, so without a login they're "
            "indistinguishable from deleted posts and get dropped as dead links.\n\n"
            "To get them: log in to Sankaku, press F12 > Storage (Firefox) or Application "
            "(Chrome) > Cookies > chan.sankakucomplex.com. Paste them here as name=value "
            "pairs separated by semicolons, e.g.\n"
            "    _sankakuchannel_session=u2uE0z...; locale=en\n\n"
            "_sankakuchannel_session is the one that carries your login - the rest are just "
            "display preferences. You don't have to work out which is which, though: paste "
            "them all and whatever Sankaku currently uses gets sent.\n\n"
            "Two things that catch people out:\n"
            "  - That cookie is HttpOnly, so typing document.cookie in the console will NOT "
            "show it. It has to come from the Storage/Application panel.\n"
            "  - Its value is long (600+ characters) and the panel truncates it on screen. "
            "Right-click the row and choose Copy Value to get the whole thing, or you'll "
            "paste a cut-off session that silently fails to authenticate.\n\n"
            "Treat it like a password: it grants access to your account and is stored in "
            "plain text in the config file. It also expires, so if Sankaku matches start "
            "disappearing again, re-copy it."
        )
        form.addRow("Sankaku cookies:", self.sankaku_cookies)

        sankaku_test_btn = QPushButton("Test Sankaku…")
        sankaku_test_btn.setToolTip(
            "Fetches a Sankaku post URL you provide using the cookies typed above and reports "
            "whether it came back as a real post or as the blank \"no such post\" page."
        )
        sankaku_test_btn.clicked.connect(self._test_sankaku)
        self.sankaku_test_result = QLabel("")
        self.sankaku_test_result.setWordWrap(True)
        form.addRow(sankaku_test_btn, self.sankaku_test_result)

        self.animepictures_cookies = QLineEdit(self.settings.animepictures_cookies)
        self.animepictures_cookies.setEchoMode(QLineEdit.EchoMode.Password)
        self.animepictures_cookies.setPlaceholderText("anime_pictures_jwt=eyJ0eXAiOiJKV1Qi…")
        self.animepictures_cookies.setToolTip(
            "Anime-Pictures keeps some posts account-only. Its API answers those with "
            "HTTP 403 Forbidden, which carries none of the post - no tags, no dimensions, "
            "not even a preview - so without a login they simply come back blank.\n\n"
            "The cookie that carries your login is anime_pictures_jwt. To get it: log in to "
            "anime-pictures.net, press F12 > Storage (Firefox) or Application (Chrome) > "
            "Cookies > anime-pictures.net, right-click that row and choose Copy Value.\n\n"
            "Paste it here as anime_pictures_jwt=<value>. Pasting just the value on its own "
            "also works - the name is filled in for you. Other cookies can be included as "
            "name=value pairs separated by semicolons, but none of them are needed.\n\n"
            "Two things that catch people out:\n"
            "  - Use Copy Value, not a text selection. The panel truncates long values on "
            "screen, and a cut-off token is accepted as simply invalid: nothing errors, "
            "account-only posts just keep coming back blank.\n"
            "  - It is HttpOnly, so typing document.cookie in the console will NOT show it. "
            "It has to come from the Storage/Application panel.\n\n"
            "Don't bother with cf_clearance - it is tied to your browser's IP and "
            "User-Agent, so it won't validate for this app and can cause a challenge "
            "rather than avoid one.\n\n"
            "Treat it like a password: it grants access to your account and is stored in "
            "plain text in the config file. It expires, so if account-only posts start "
            "coming back blank again, re-copy it."
        )
        form.addRow("Anime-Pictures cookies:", self.animepictures_cookies)

        ap_test_btn = QPushButton("Test Anime-Pictures…")
        ap_test_btn.setToolTip(
            "Fetches a post through the real API path using the cookies typed above and "
            "reports exactly what came back - whether it was readable, how many tags and "
            "what dimensions were found, or that it is still account-only."
        )
        ap_test_btn.clicked.connect(self._test_animepictures)
        self.animepictures_test_result = QLabel("")
        self.animepictures_test_result.setWordWrap(True)
        form.addRow(ap_test_btn, self.animepictures_test_result)

        # Snapshotted so apply_to_settings can tell "the user actually
        # edited this box" from "a background search rotated the saved
        # cookie while the dialog happened to be open" - see there for why
        # that distinction is what keeps a same-session rotation from
        # being silently clobbered by an unrelated Save (DAN-123).
        self._deviantart_cookies_snapshot = self.settings.deviantart_cookies
        self.deviantart_cookies = QLineEdit(self.settings.deviantart_cookies)
        self.deviantart_cookies.setEchoMode(QLineEdit.EchoMode.Password)
        self.deviantart_cookies.setPlaceholderText("auth=...; auth_secure=...; userinfo=...")
        self.deviantart_cookies.setToolTip(
            "DeviantArt shows adult deviations BLURRED to logged-out visitors. The blur is "
            "baked into the signed image token itself, so nothing this app can do to the URL "
            "undoes it - a logged-in session is the only way to see the real picture.\n\n"
            "To get them: log in to deviantart.com, press F12 > Storage (Firefox) or "
            "Application (Chrome) > Cookies > deviantart.com. Paste them here as name=value "
            "pairs separated by semicolons.\n\n"
            "Paste all of them rather than picking one - DeviantArt splits a session across "
            "several cookies, and which ones matter has changed before.\n\n"
            "Use Copy Value rather than selecting text: the panel truncates long values on "
            "screen, and a cut-off session is accepted as simply invalid - nothing errors, "
            "adult deviations just stay blurred.\n\n"
            "DeviantArt renews its session cookies on activity (confirmed: it reissues one "
            "with a fresh expiry on every page it serves), which is why a browser tab stays "
            "logged in for months (DAN-122). This app now captures that renewal too "
            "(DAN-123): whenever a search or availability check actually fetches a "
            "DeviantArt page with these cookies attached, whatever comes back is folded in "
            "here and saved automatically, so a session kept in regular use should stay "
            "valid about as long as it would in a browser.\n\n"
            "That only happens on an actual request, though - there's no way to renew a "
            "cookie without one. A pasted session that sits unused (the app closed, or no "
            "DeviantArt matches turning up) still ages out on its OWN original expiry and "
            "will need a fresh paste; that's expected, not a sign it was entered wrong.\n\n"
            "Note this only affects the PICTURE. DeviantArt publishes no booru-style tags, "
            "so a match here gives you the artist and a preview either way.\n\n"
            "Treat it like a password: it grants access to your account and is stored in "
            "plain text in the config file."
        )
        form.addRow("DeviantArt cookies:", self.deviantart_cookies)

        da_test_btn = QPushButton("Test DeviantArt…")
        da_test_btn.setToolTip(
            "Fetches a deviation through the real parser path using the cookies typed above "
            "and reports what came back - the artist, and whether the picture arrived "
            "properly or is still the blurred logged-out copy."
        )
        da_test_btn.clicked.connect(self._test_deviantart)
        self.deviantart_test_result = QLabel("")
        self.deviantart_test_result.setWordWrap(True)
        form.addRow(da_test_btn, self.deviantart_test_result)

        return w

    def _test_deviantart(self):
        """Checks a real deviation with whatever cookies are currently
        typed. An adult deviation is the useful case: logged out it comes
        back blurred, so "still blurred" and "cookies wrong" are the same
        symptom and only this can tell them apart."""
        from PyQt6.QtWidgets import QInputDialog
        from core.boorus import BooruContentGoneError, BooruError, fetch_page_info
        from core.search_engine import cookie_paste_warnings, parse_cookie_string

        url, ok = QInputDialog.getText(
            self, "Test DeviantArt",
            "Paste a DeviantArt deviation URL (ideally an adult one,\n"
            "since that is what a login actually changes):",
            text="https://www.deviantart.com/",
        )
        if not ok or not url.strip():
            return
        url = url.strip()
        if "deviantart.com" not in url and "fav.me" not in url:
            self.deviantart_test_result.setText("That does not look like a DeviantArt URL.")
            return

        raw = self.deviantart_cookies.text()
        cookies = parse_cookie_string(raw)
        if not cookies and raw.strip():
            self.deviantart_test_result.setText(
                "That doesn't look like cookies. They need to be name=value pairs separated "
                "by semicolons, e.g. auth=…; auth_secure=…"
            )
            return
        if not cookies:
            self.deviantart_test_result.setText(
                "No cookies entered - testing anonymously, which is what the app would do."
            )
        else:
            warnings = cookie_paste_warnings(raw, session_name="auth")
            msg = f"Testing with {len(cookies)} cookie(s): {', '.join(sorted(cookies))}…"
            if warnings:
                msg += "\n\n⚠ " + "\n⚠ ".join(warnings)
            self.deviantart_test_result.setText(msg)
        QApplication.processEvents()

        try:
            info = fetch_page_info(
                url, timeout=self.settings.search_timeout, cookies=cookies or None,
            )
        except BooruContentGoneError:
            self.deviantart_test_result.setText(
                "That deviation is gone (HTTP 404) - deleted, or the URL is wrong. "
                "Try another; this says nothing about whether the cookies work."
            )
            return
        except BooruError as exc:
            self.deviantart_test_result.setText(f"Could not fetch it: {exc}")
            return
        except Exception as exc:
            log.exception("DeviantArt test failed unexpectedly")
            self.deviantart_test_result.setText(f"Unexpected error: {exc}")
            return

        artist = next((t.name for t in info.tags if t.namespace == "artist"), None)
        if info.restricted:
            self.deviantart_test_result.setText(
                ("Read the deviation" + (f" (artist '{artist}')" if artist else "")
                 + ", but the picture is still the BLURRED logged-out copy. ")
                + ("Re-copy the cookies from a logged-in browser - they may have expired "
                   "or been cut off." if cookies else
                   "Add cookies from a logged-in account above and retry.")
            )
            log.info("DeviantArt test on %s: still blurred", url)
            return

        if not artist and not info.preview_url:
            self.deviantart_test_result.setText(
                "Nothing could be read from that URL. DeviantArt's data endpoint only accepts "
                "the full /artist/art/title-12345 form - a /view/ or fav.me link is resolved "
                "automatically, but only if the site answers. See Help > View Logs."
            )
            return

        parts = ["Working."]
        if artist:
            parts.append(f"Artist '{artist}'.")
        parts.append("Picture available (unblurred)." if info.preview_url else "No picture came back.")
        summary = " ".join(parts)
        if not cookies:
            summary += " (No cookies set - adult deviations will still come back blurred.)"
        self.deviantart_test_result.setText(summary)
        log.info("DeviantArt test on %s succeeded: %s", url, summary)

    def _test_animepictures(self):
        """Checks a real post with whatever cookies are currently typed, so
        a login can be verified before saving it - and so "still blank"
        is distinguishable from "cookies wrong"."""
        from PyQt6.QtWidgets import QInputDialog
        from core.boorus import BooruContentGoneError, BooruError, fetch_page_info
        from core.search_engine import (
            ANIMEPICTURES_SESSION_COOKIE, animepictures_cookies, cookie_paste_warnings,
        )

        url, ok = QInputDialog.getText(
            self, "Test Anime-Pictures",
            "Paste an Anime-Pictures post URL (ideally an account-only one):",
            text="https://anime-pictures.net/pictures/view_post/",
        )
        if not ok or not url.strip():
            return
        url = url.strip()
        if "anime-pictures.net" not in url:
            self.animepictures_test_result.setText("That does not look like an Anime-Pictures URL.")
            return

        # Use the cookies currently TYPED, not the saved ones, so a new
        # value can be checked before committing to it.
        raw = self.animepictures_cookies.text()
        cookies = animepictures_cookies(raw)
        if not cookies and raw.strip():
            # Something WAS typed but nothing could be made of it. Saying
            # "no cookies entered" here (as this used to) is just wrong,
            # and leaves the user with no idea what to change.
            self.animepictures_test_result.setText(
                "That doesn't look like a cookie. Paste it as "
                f"{ANIMEPICTURES_SESSION_COOKIE}=<value>, or paste just the value on its own "
                "- but it has to be one unbroken run of text, so check nothing was pasted "
                "with a line break in the middle."
            )
            return
        if not cookies:
            self.animepictures_test_result.setText(
                "No cookies entered - testing anonymously, which is what the app would do."
            )
        else:
            warnings = cookie_paste_warnings(raw, session_name=ANIMEPICTURES_SESSION_COOKIE)
            msg = f"Testing with {len(cookies)} cookie(s): {', '.join(sorted(cookies))}…"
            if warnings:
                msg += "\n\n⚠ " + "\n⚠ ".join(warnings)
            self.animepictures_test_result.setText(msg)
        QApplication.processEvents()

        try:
            info = fetch_page_info(
                url, timeout=self.settings.search_timeout, cookies=cookies or None,
            )
        except BooruContentGoneError:
            self.animepictures_test_result.setText(
                "That post is gone (HTTP 404) - it was deleted or the ID does not exist. "
                "Try a different one; this says nothing about whether the cookies work."
            )
            return
        except BooruError as exc:
            self.animepictures_test_result.setText(f"Could not fetch it: {exc}")
            return
        except Exception as exc:
            log.exception("Anime-Pictures test failed unexpectedly")
            self.animepictures_test_result.setText(f"Unexpected error: {exc}")
            return

        if info.restricted:
            self.animepictures_test_result.setText(
                "Still account-only with these cookies. "
                + ("Re-copy them from a logged-in browser - they may have expired."
                   if cookies else "Add cookies from a logged-in account above and retry.")
            )
            log.info("Anime-Pictures test on %s: still restricted", url)
            return

        if not info.tags and not info.width:
            self.animepictures_test_result.setText(
                "The post was fetched but nothing could be read from it. "
                "See Help > View Logs for the specific reason."
            )
            return

        parts = [f"Working. {len(info.tags)} tag(s)"]
        if info.width and info.height:
            parts.append(f"{info.width}×{info.height}")
        if info.file_format:
            parts.append(info.file_format)
        summary = ", ".join(parts) + "."
        if not cookies:
            summary += " (No cookies set - account-only posts will still come back blank.)"
        self.animepictures_test_result.setText(summary)
        log.info("Anime-Pictures test on %s succeeded: %s", url, summary)

    def _test_sankaku(self):
        """Checks a real Sankaku URL with whatever cookies are currently
        typed, so a login can be verified before saving it - and so
        "still blank" is distinguishable from "cookies wrong"."""
        from PyQt6.QtWidgets import QInputDialog
        from core.search_engine import (
            check_url_available, cookie_paste_warnings, parse_cookie_string,
        )

        url, ok = QInputDialog.getText(
            self, "Test Sankaku",
            "Paste a Sankaku post URL (ideally one you can only see when logged in):",
        )
        if not ok or not url.strip():
            return

        cookies = parse_cookie_string(self.sankaku_cookies.text())
        if not cookies:
            self.sankaku_test_result.setText(
                "No cookies entered - testing anonymously, which is what the app would do."
            )
        else:
            # Naming what was parsed is the quickest way to spot a paste
            # that didn't come out as intended - a stray line break, or a
            # value cut off by the browser panel's truncated display.
            warnings = cookie_paste_warnings(self.sankaku_cookies.text())
            message = f"Testing with {len(cookies)} cookie(s): {', '.join(sorted(cookies))}…"
            if warnings:
                message += "\n\n⚠ " + "\n⚠ ".join(warnings)
            self.sankaku_test_result.setText(message)
        QApplication.processEvents()

        available = check_url_available(url.strip(), timeout=20.0, cookies=cookies or None)
        if available is True:
            self.sankaku_test_result.setText(
                "The post loaded normally - it would be kept as a live match."
            )
        elif available is False:
            self.sankaku_test_result.setText(
                "Came back as the blank \"no such post\" page, so it would be dropped. "
                "If you can see this post in your browser while logged in, the cookies "
                "above aren't working - re-copy them."
            )
        else:
            self.sankaku_test_result.setText(
                "Couldn't tell (network error, timeout, or a block). The match would be "
                "kept rather than dropped."
            )

    # -- SauceNAO -------------------------------------------------------
    def _build_saucenao_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)

        self.saucenao_key = QLineEdit(self.settings.saucenao.api_key)
        self.saucenao_key.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("API key:", self.saucenao_key)

        self.saucenao_json = QCheckBox("Use JSON API (needs a key, retrieves more tags)")
        self.saucenao_json.setChecked(self.settings.saucenao.use_json_api)
        form.addRow(self.saucenao_json)

        self.saucenao_pause_on_quota = QCheckBox("Pause searching when the daily quota runs out")
        self.saucenao_pause_on_quota.setToolTip(
            "Stops the current batch as soon as SauceNAO reports its DAILY allowance is "
            "spent, leaving the remaining images unsearched so you can resume once it "
            "resets - rather than searching on and recording weaker results for the rest "
            "of the list. The ~30-second burst limit does NOT trigger this; that one "
            "clears by itself and the app already waits it out. Only applies when "
            "SauceNAO is part of your engine order."
        )
        self.saucenao_pause_on_quota.setChecked(self.settings.saucenao.pause_search_on_quota_exhausted)

        self.continue_without_saucenao = QCheckBox(
            "...or carry on without SauceNAO instead of stopping")
        self.continue_without_saucenao.setToolTip(
            "IQDB has no daily cap, so the alternative to stopping is an idle machine: a "
            "24,000-image library against a 5,000-a-day allowance is five days, most of "
            "them spent waiting for midnight.\n\n"
            "Results found this way are weaker than a full search would have given - which "
            "is exactly why stopping is the default - so they are marked Provisional in "
            "the Cache column and are NOT saved to the result cache. Once the allowance "
            "resets, right-click > Select by Cache State > Provisional and re-search them "
            "to get the real answer.\n\n"
            "Takes precedence over the checkbox above."
        )
        self.continue_without_saucenao.setChecked(
            self.settings.continue_without_saucenao_on_quota)
        form.addRow(self.continue_without_saucenao)
        form.addRow(self.saucenao_pause_on_quota)

        self.saucenao_min_sim = QDoubleSpinBox()
        self.saucenao_min_sim.setRange(0, 100)
        self.saucenao_min_sim.setValue(self.settings.saucenao.min_similarity)
        form.addRow("Minimum similarity %:", self.saucenao_min_sim)

        self.saucenao_namespace = QLineEdit(self.settings.saucenao.namespace)
        self.saucenao_namespace.setPlaceholderText("optional, e.g. \"sn\"")
        form.addRow("Tag namespace:", self.saucenao_namespace)

        form.addRow(QLabel("Tag types to retrieve:"))
        self.saucenao_tag_types = QListWidget()
        for name in ["character", "material", "creator"]:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if name in self.settings.saucenao.tag_types
                else Qt.CheckState.Unchecked
            )
            self.saucenao_tag_types.addItem(item)
        form.addRow(self.saucenao_tag_types)

        return w

    # -- Hydrus -----------------------------------------------------------
    def _build_hydrus_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)

        self.hydrus_url = QLineEdit(self.settings.hydrus.api_url)
        form.addRow("API URL:", self.hydrus_url)

        self.hydrus_key = QLineEdit(self.settings.hydrus.access_key)
        self.hydrus_key.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Access key:", self.hydrus_key)

        self.hydrus_service_key = QComboBox()
        self.hydrus_service_key.setEditable(True)
        self.hydrus_service_key.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.hydrus_service_key.lineEdit().setText(self.settings.hydrus.tag_service_key)  # type: ignore[union-attr]  # PyQt6 stub: return type is Optional but guaranteed set at this call site
        self.hydrus_service_key.lineEdit().setPlaceholderText('empty = "my tags" local service')  # type: ignore[union-attr]  # PyQt6 stub: return type is Optional but guaranteed set at this call site
        fetch_services_btn = QPushButton("Fetch from Hydrus…")
        fetch_services_btn.clicked.connect(self._fetch_tag_services)
        self.hydrus_service_key.activated.connect(self._on_service_selected)
        service_row = QHBoxLayout()
        service_row.addWidget(self.hydrus_service_key)
        service_row.addWidget(fetch_services_btn)
        form.addRow("Tag service:", service_row)

        self.hydrus_verify_ssl = QCheckBox("Verify SSL certificate")
        self.hydrus_verify_ssl.setChecked(self.settings.hydrus.verify_ssl)
        form.addRow(self.hydrus_verify_ssl)

        self.set_hydrus_duplicate_relationships = QCheckBox(
            "Mark a downloaded better copy as a duplicate of your existing file")
        self.set_hydrus_duplicate_relationships.setToolTip(
            "When \"Download match + send to Hydrus\" brings in a better copy of a file Hydrus "
            "already holds, Hydrus ends up with two files it believes are unrelated - and "
            "nothing points from one to the other.\n\n"
            "With this on, the app tells Hydrus how they relate. If the two are the same "
            "picture, the new copy becomes the king of the duplicate group - the one Hydrus "
            "shows. If it's a different edit or crop, the pair is marked as alternates "
            "instead, so neither supersedes the other.\n\n"
            "Only ever on a confident comparison of the two actual files: anything the "
            "measurement can't settle - including a mirrored copy - is left alone rather than "
            "filed as a guess. Nothing is ever deleted.\n\n"
            "Off by default, because it writes a relationship into your library and an "
            "incorrect pairing is tedious to undo.\n\n"
            "Needs the \"Edit File Relationships\" permission on your Hydrus access key "
            "(services → review services → local → client api). Without it the "
            "import still succeeds and the app says which permission is missing."
        )
        self.set_hydrus_duplicate_relationships.setChecked(
            self.settings.set_hydrus_duplicate_relationships)
        form.addRow(self.set_hydrus_duplicate_relationships)

        self.write_hydrus_provenance_note = QCheckBox(
            "Write a note on the file recording where the match came from")
        self.write_hydrus_provenance_note.setToolTip(
            "Everything this app knows about a match - which engine found it, which site it "
            "was on, how similar it is, and the link - is on screen and in the exports, and "
            "none of it survives in Hydrus. The file and its tags arrive; nothing says an "
            "automated tool put them there or what it matched against.\n\n"
            "With this on, each send also writes a short note on the file in Hydrus with the "
            "engine, the site, the similarity, the matched URL and the date.\n\n"
            "The similarity is written with the same marker the Similarity column uses: a "
            "\"~\" and a plain-English note when the number is the engine's own ranking of "
            "its results rather than a measurement, which is what ascii2d and the Google "
            "engines give. A bare percentage would claim more than this app knows.\n\n"
            "The app replaces its own note on every send, so re-sending a file does not "
            "stack up copies. Notes under any other name - including your own - are never "
            "touched, and this one is deleted from Hydrus's own notes panel in two clicks.\n\n"
            "Needs the \"Edit File Notes\" permission on your Hydrus access key "
            "(services → review services → local → client api). Without it the import "
            "still succeeds and the app says which permission is missing."
        )
        self.write_hydrus_provenance_note.setChecked(
            self.settings.write_hydrus_provenance_note)
        form.addRow(self.write_hydrus_provenance_note)

        self.hydrus_provenance_note_name = QLineEdit(
            self.settings.hydrus_provenance_note_name)
        self.hydrus_provenance_note_name.setPlaceholderText(
            f'empty = "{DEFAULT_NOTE_NAME}"')
        self.hydrus_provenance_note_name.setToolTip(
            "Which note the line above goes in. The app overwrites this note every time it "
            "sends the file, so give it a name you are not using for notes of your own."
        )
        form.addRow("Source note name:", self.hydrus_provenance_note_name)

        test_btn = QPushButton("Test connection")
        test_btn.clicked.connect(self._test_hydrus)
        self.hydrus_test_result = QLabel("")
        form.addRow(test_btn, self.hydrus_test_result)

        return w

    def _test_e621(self):
        """Checks the typed e621 credentials against the real API.

        Uses the app's own header builder rather than assembling the
        auth itself, so a pass here means searches will authenticate -
        not merely that some request with some header succeeded.

        posts.json is the endpoint because e621 validates credentials on
        it even though it is public: MEASURED, a bad key gets 401
        ("SessionLoader::AuthenticationFailure") while no key at all
        gets a normal 200. That makes the answer unambiguous, which an
        endpoint like favorites.json would not - it answers 404 when
        anonymous, and a real account with no favourites is
        indistinguishable from that.
        """
        import requests
        from types import SimpleNamespace
        from core.search_engine import headers_for_url

        user = self.e621_username.text().strip()
        key = self.e621_api_key.text().strip()
        if not user or not key:
            # Matching e621_auth_header: half-filled credentials send no
            # auth at all, so this would otherwise "pass" anonymously.
            missing = "username" if not user else "API key"
            self.e621_test_result.setText(
                f"No {missing} entered. Both halves are needed - with one missing the app "
                "sends no credentials at all and e621 answers as if you were logged out."
            )
            return

        # The values TYPED, not the saved ones, so a new key can be
        # checked before committing to it.
        typed = SimpleNamespace(e621_username=user, e621_api_key=key)
        url = "https://e621.net/posts.json?limit=1"
        headers = headers_for_url(url, typed)

        self.e621_test_result.setText(f"Testing as {user}…")
        QApplication.processEvents()

        try:
            response = requests.get(url, headers=headers, timeout=20)
        except requests.RequestException as exc:
            self.e621_test_result.setText(
                f"Couldn't reach e621 ({exc}). That is a network problem, not a verdict "
                "on the credentials."
            )
            return

        if response.status_code == 200:
            self.e621_test_result.setText(
                f"e621 accepted the credentials for {user}. Posts under its global "
                "blacklist will be visible to searches."
            )
        elif response.status_code == 401:
            self.e621_test_result.setText(
                "e621 rejected the username or API key (401). Check both on e621 under "
                "Account > Manage API Access - the key is not your password, and it is "
                "case-sensitive.\n\n"
                "Leave them blank rather than wrong: a bad key makes e621 refuse every "
                "request, including ones that would have worked anonymously."
            )
        elif response.status_code == 403:
            self.e621_test_result.setText(
                "e621 answered 403. The credentials may be right but the account is "
                "blocked from the API, or the request was refused for another reason."
            )
        elif response.status_code == 503:
            self.e621_test_result.setText(
                "e621 answered 503 - it is rate-limiting or under load. Try again in a "
                "moment; this says nothing about the credentials."
            )
        else:
            self.e621_test_result.setText(
                f"e621 answered HTTP {response.status_code}, which is neither an accept "
                "nor a rejection. Try again shortly."
            )

    def _test_pixiv(self):
        """Fetches one Pixiv artwork through the real parser path and
        reports what actually happened.

        Deliberately exercises the same code a search uses - not a
        simplified version - so a pass here means searches will work,
        rather than only proving that Pixiv is reachable."""
        from PyQt6.QtWidgets import QInputDialog
        from core.boorus import BooruContentGoneError, BooruError, fetch_page_info
        from core.search_engine import headers_for_url

        url, ok = QInputDialog.getText(
            self, "Test Pixiv",
            "Paste any Pixiv artwork URL (either the modern /artworks/ form\n"
            "or a legacy member_illust.php one):",
            text="https://www.pixiv.net/artworks/",
        )
        if not ok or not url.strip():
            return
        url = url.strip()

        if "pixiv.net" not in url:
            self.pixiv_test_result.setText("That does not look like a Pixiv URL.")
            return

        # Use the cookie currently TYPED, not the saved one, so a new value
        # can be checked before committing to it.
        cookie = self.pixiv_session_cookie.text().strip()
        cookies = {"PHPSESSID": cookie} if cookie else None

        self.pixiv_test_result.setText("Testing…")
        self.pixiv_test_result.repaint()

        try:
            info = fetch_page_info(
                url, timeout=self.settings.search_timeout,
                cookies=cookies, headers=headers_for_url(url),
            )
        except BooruContentGoneError:
            self.pixiv_test_result.setText(
                "That artwork is gone (HTTP 404) - it was deleted or the ID does not exist. "
                "Try a different URL; this does not tell us whether the cookie works."
            )
            return
        except BooruError as exc:
            self.pixiv_test_result.setText(f"Could not fetch it: {exc}")
            return
        except Exception as exc:
            log.exception("Pixiv test failed unexpectedly")
            self.pixiv_test_result.setText(f"Unexpected error: {exc}")
            return

        tag_count = len(info.tags)
        has_file = bool(info.file_url)
        creator = next((t.name for t in info.tags if t.namespace == "creator"), None)

        if tag_count or has_file:
            parts = [f"Working. Extracted {tag_count} tag(s)"]
            if creator:
                parts.append(f"artist '{creator}'")
            parts.append("full-resolution URL found" if has_file else "but NO full-res URL")
            if info.width and info.height:
                parts.append(f"{info.width}x{info.height}")
            summary = ", ".join(parts) + "."
            if not cookie:
                summary += " (No cookie set - restricted works will still fail.)"
            self.pixiv_test_result.setText(summary)
            log.info("Pixiv test on %s succeeded: %s", url, summary)
            return

        # Fetched fine but produced nothing - the interesting failure.
        if cookie:
            msg = ("Page fetched, but no data could be read from it. The cookie may have expired "
                   "(they do, periodically) - try copying a fresh PHPSESSID from your browser. "
                   "See Help > View Logs for the specific reason.")
        else:
            msg = ("Page fetched, but no data could be read from it. If this artwork requires "
                   "login or is R-18, set a session cookie above and retry. "
                   "See Help > View Logs for the specific reason.")
        self.pixiv_test_result.setText(msg)
        log.warning("Pixiv test on %s returned no usable data", url)

    def _on_service_selected(self, index: int):
        """When picking an item from the fetched-services list, write the
        full hex service key into the editable field - not the shortened
        'name (abc123…)' display text used in the dropdown list."""
        key = self.hydrus_service_key.itemData(index)
        if key:
            self.hydrus_service_key.lineEdit().setText(key)  # type: ignore[union-attr]  # PyQt6 stub: return type is Optional but guaranteed set at this call site

    def _fetch_tag_services(self):
        from dataclasses import replace
        temp = replace(
            self.settings.hydrus,
            api_url=self.hydrus_url.text(),
            access_key=self.hydrus_key.text(),
            verify_ssl=self.hydrus_verify_ssl.isChecked(),
        )
        log.debug("Fetching tag services from %s", temp.api_url)
        try:
            services = HydrusClient(temp).list_tag_services()
        except HydrusError as exc:
            log.error("Fetch tag services failed: %s", exc)
            message.warning(self, "Fetch tag services", f"Could not fetch services: {exc}")
            return

        if not services:
            log.info("Hydrus reported no tag services at %s", temp.api_url)
            message.information(self, "Fetch tag services", "Hydrus reported no tag services.")
            return

        log.info("Fetched %d tag service(s) from Hydrus", len(services))
        current_text = self.hydrus_service_key.currentText()
        self.hydrus_service_key.clear()
        for key, name in services:
            self.hydrus_service_key.addItem(f"{name}  ({key[:12]}…)", userData=key)
        # Keep whatever the user had typed rather than force-picking one.
        self.hydrus_service_key.lineEdit().setText(current_text)

    def _test_hydrus(self):
        from dataclasses import replace
        temp = replace(
            self.settings.hydrus,
            api_url=self.hydrus_url.text(),
            access_key=self.hydrus_key.text(),
            verify_ssl=self.hydrus_verify_ssl.isChecked(),
        )
        log.debug("Testing Hydrus connection to %s", temp.api_url)
        try:
            ok = HydrusClient(temp).test_connection()
            log.info("Hydrus connection test to %s: %s", temp.api_url, "OK" if ok else "no response")
            self.hydrus_test_result.setText("Connected ✓" if ok else "No response")
        except HydrusError as exc:
            log.warning("Hydrus connection test to %s failed: %s", temp.api_url, exc)
            self.hydrus_test_result.setText(f"Failed: {exc}")

    # -- Tag Namespaces -------------------------------------------------
    def _build_tag_namespaces_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        self.enable_namespace_remap = QCheckBox("Rename tag namespaces automatically")
        self.enable_namespace_remap.setToolTip(
            "E.g. rewrite booru sites' \"artist:\" to \"creator:\" to match Hydrus's PTR tagging "
            "convention, or strip a namespace entirely (leave the new side blank). Applies to "
            "tags pulled from search engines/booru pages, existing Hydrus tags when imported, "
            "and manual inline edits - not to new tags typed via \"Add tags\"."
        )
        self.enable_namespace_remap.setChecked(self.settings.enable_tag_namespace_remap)
        layout.addWidget(self.enable_namespace_remap)

        layout.addWidget(QLabel(
            "One rule per line, as old:new - e.g. artist:creator rewrites every\n"
            "\"artist:\" tag to \"creator:\" as it's pulled in. Leave the new side\n"
            "empty (e.g. general:) to strip that namespace entirely - "
            "\"general:bikini_top\" becomes plain \"bikini_top\"."
        ))

        self.namespace_remap_edit = QPlainTextEdit()
        self.namespace_remap_edit.setPlaceholderText("artist:creator")
        self.namespace_remap_edit.setPlainText(
            "\n".join(f"{old}:{new}" for old, new in self.settings.tag_namespace_remap.items())
        )
        layout.addWidget(self.namespace_remap_edit)

        self.enable_tag_blacklist = QCheckBox("Discard blacklisted tags as they're pulled in")
        self.enable_tag_blacklist.setToolTip(
            "Drops booru housekeeping tags (highres, tagme, bad_id, commentary_request, ...) "
            "so they never reach the tag list or get sent to Hydrus. Applies to tags from "
            "search engines and booru pages only - NOT to tags Hydrus already has on a file "
            "(those stay visible, since this can't remove them from Hydrus anyway), and NOT "
            "to tags you type yourself."
        )
        self.enable_tag_blacklist.setChecked(self.settings.enable_tag_blacklist)
        layout.addWidget(self.enable_tag_blacklist)

        layout.addWidget(QLabel(
            "One pattern per line. A pattern without a colon matches the tag name in any\n"
            "namespace (highres blocks both \"highres\" and \"meta:highres\"); with a colon it\n"
            "matches the full namespaced form (meta:* blocks that whole namespace). * and ?\n"
            "wildcards work in both. Case-insensitive, and spaces/underscores are equivalent.\n"
            "Note: namespace renaming above is applied first, so write patterns against the\n"
            "renamed namespace (creator:*, not artist:*, if you remap artist to creator)."
        ))

        self.tag_blacklist_edit = QPlainTextEdit()
        self.tag_blacklist_edit.setPlaceholderText("highres\nbad_*\nmeta:*")
        self.tag_blacklist_edit.setPlainText("\n".join(self.settings.tag_blacklist))
        layout.addWidget(self.tag_blacklist_edit)

        self.add_rating_tag = QCheckBox("Add the matched post's rating as a tag")
        self.add_rating_tag.setToolTip(
            "Turns the booru's own age rating for the matched post into a tag - "
            "\"rating:explicit\", \"rating:safe\", and so on. Only sites that actually state "
            "one contribute it (Danbooru, e621, Gelbooru/Safebooru/rule34/Xbooru); it is "
            "never guessed from anything else. The tag goes through the renaming and "
            "blacklist rules above like any other booru tag, so \"rating:*\" in the "
            "blacklist drops it again. Off by default, since this adds a tag to your "
            "library that wasn't there before - including in Hydrus."
        )
        self.add_rating_tag.setChecked(self.settings.add_rating_tag)
        layout.addWidget(self.add_rating_tag)

        rating_row = QHBoxLayout()
        rating_row.addWidget(QLabel("Rating namespace:"))
        self.rating_tag_namespace = QLineEdit(self.settings.rating_tag_namespace)
        self.rating_tag_namespace.setPlaceholderText("rating")
        self.rating_tag_namespace.setToolTip(
            "The namespace that tag is filed under. Leave it blank to use \"rating\"."
        )
        rating_row.addWidget(self.rating_tag_namespace)
        layout.addLayout(rating_row)

        self.enable_tag_colors = QCheckBox("Color tags to match Hydrus's namespace colors")
        self.enable_tag_colors.setToolTip(
            "Hydrus has no API to report your actual configured colors (and lets you "
            "recolor namespaces per-install), so this uses Hydrus's commonly-cited "
            "defaults - creator/artist red, character green, series/copyright magenta, "
            "studio maroon, meta gray. Override any of them below if yours differ."
        )
        self.enable_tag_colors.setChecked(self.settings.enable_hydrus_tag_colors)
        layout.addWidget(self.enable_tag_colors)

        layout.addWidget(QLabel(
            "Optional overrides, one per line, as namespace:#RRGGBB - e.g. character:#RRGGBB\n"
            "(use an empty namespace, i.e. just \":#RRGGBB\", to override the unnamespaced color)"
        ))

        self.namespace_colors_edit = QPlainTextEdit()
        self.namespace_colors_edit.setPlaceholderText("character:#RRGGBB")
        self.namespace_colors_edit.setPlainText(
            "\n".join(f"{ns}:{color}" for ns, color in self.settings.tag_namespace_colors.items())
        )
        layout.addWidget(self.namespace_colors_edit)

        layout.addWidget(self._build_outcome_tags_group())

        return w

    # -- Tags applied by rule (DAN-77) --------------------------------
    def _build_outcome_tags_group(self) -> QGroupBox:
        """The three by-rule tag lists.

        Grouped rather than dropped in among the other tag settings on
        purpose: everything above this box changes tags that came from
        somewhere else, while these three put tags the app invented onto
        the image. That is a different kind of thing to be doing to
        somebody's library and the box says so.
        """
        group = QGroupBox("Tags applied by rule")
        box = QVBoxLayout(group)

        box.addWidget(QLabel(
            "Tags this program adds itself, by how the search turned out - one per line,\n"
            "optionally namespace:tag. All three are empty by default and nothing is added\n"
            "until you fill one in. A search that errored gets nothing either way: it is\n"
            "retried, so it has no outcome yet."
        ))

        # Each row is label + box rather than a form layout: the
        # placeholder is doing real explanatory work here (it is where
        # "hatate:not found" is actually suggested) and a QFormLayout
        # squeezes these to a width that hides it.
        self.tags_for_found_edit = QPlainTextEdit()
        self.tags_for_found_edit.setPlaceholderText("hatate:found")
        self.tags_for_found_edit.setPlainText("\n".join(self.settings.tags_for_found))
        self.tags_for_found_edit.setToolTip(
            "Added to every image a match was found for - both green (good) and yellow "
            "(worth reviewing) rows, since both are matches."
        )
        box.addWidget(QLabel("When a match is found:"))
        box.addWidget(self.tags_for_found_edit)

        self.tags_for_not_found_edit = QPlainTextEdit()
        self.tags_for_not_found_edit.setPlaceholderText("hatate:not found")
        self.tags_for_not_found_edit.setPlainText("\n".join(self.settings.tags_for_not_found))
        self.tags_for_not_found_edit.setToolTip(
            "Added to every image no match was found for. This is the one worth setting: "
            "it turns \"everything this program could not place\" into a tag you can "
            "search your library for later, instead of a red row in a window you have to "
            "keep open.\n\nRemoved again automatically if a later search does find a match."
        )
        box.addWidget(QLabel("When no match is found:"))
        box.addWidget(self.tags_for_not_found_edit)

        self.tags_for_low_tag_count_edit = QPlainTextEdit()
        self.tags_for_low_tag_count_edit.setPlaceholderText("hatate:few tags")
        self.tags_for_low_tag_count_edit.setPlainText(
            "\n".join(self.settings.tags_for_low_tag_count)
        )
        self.tags_for_low_tag_count_edit.setToolTip(
            "Added on top of the found tags when the match came back with fewer tags than "
            "the \"minimum tags for a good match\" setting in Match Conditions - the same "
            "number that already makes the row yellow.\n\nNot added to images with no "
            "match at all: those have no tags by definition, so it would say nothing the "
            "not-found tag above did not."
        )
        box.addWidget(QLabel("When a match has few tags:"))
        box.addWidget(self.tags_for_low_tag_count_edit)

        # The one thing a user cannot work out from the boxes above, and
        # the thing that decides whether this feature is safe for them.
        box.addWidget(QLabel(
            "These are filed under the \"Hatate-linux\" tag source. To keep them in this\n"
            "program and out of Hydrus, untick that source under General and turn on\n"
            "\"also apply these sources to tags sent to Hydrus\"."
        ))

        return group

    # -- Apply --------------------------------------------------------
    # -- Shortcuts ----------------------------------------------------
    def _build_shortcuts_tab(self) -> QWidget:
        """Keyboard bindings for the review pass.

        These are bound to the image table rather than to the window,
        which is what lets the defaults be bare letters - the note at the
        top says so, because a user picking a key needs to know it will
        not fire while they are typing in the filter box.
        """
        widget = QWidget()
        layout = QVBoxLayout(widget)

        intro = QLabel(
            "Keys for reviewing results without the mouse. They work while the "
            "image list has focus, so the filter box and the tag editor keep "
            "every character you type into them.\n\n"
            "Click a shortcut to record a new key. Each does exactly what the "
            "right-click menu entry of the same name does, confirmation prompts "
            "included."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        bindings = shortcut_registry.resolve(
            getattr(self.settings, "review_shortcuts", None)
        )
        actions = shortcut_registry.REVIEW_ACTIONS

        self.shortcut_edits = {}
        table = QTableWidget(len(actions), 4)
        table.setHorizontalHeaderLabels(["Action", "Shortcut", "Default", ""])
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # Qt types both header accessors as optional. They are never None on
        # a QTableWidget, but the layout is cosmetic either way - a missing
        # header is not worth a crash in the settings window.
        row_header = table.verticalHeader()
        if row_header is not None:
            row_header.setVisible(False)
        header = table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            for column in (1, 2, 3):
                header.setSectionResizeMode(
                    column, QHeaderView.ResizeMode.ResizeToContents
                )

        for row, action in enumerate(actions):
            name = QTableWidgetItem(action.label)
            if action.description:
                name.setToolTip(action.description)
            table.setItem(row, 0, name)

            editor = QKeySequenceEdit(QKeySequence(bindings.get(action.id, "")))
            # One key per action. Qt otherwise records a multi-key chord
            # (the Emacs-style "C, then D"), which is not what anyone is
            # trying to set in a list of single-keystroke review actions,
            # and is easy to enter by accident just by typing quickly.
            editor.setMaximumSequenceLength(1)
            editor.keySequenceChanged.connect(self._refresh_shortcut_conflicts)
            table.setCellWidget(row, 1, editor)
            self.shortcut_edits[action.id] = editor

            default = QTableWidgetItem(action.default or "—")
            default.setFlags(Qt.ItemFlag.ItemIsEnabled)
            table.setItem(row, 2, default)

            # Clearing has to be a button: there is no keystroke that means
            # "no keystroke", so a QKeySequenceEdit cannot be emptied from
            # the keyboard. An unbound action stays unbound - it is not the
            # same as never having been set, and the default does not creep
            # back on the next launch.
            clear = QPushButton("Clear")
            clear.setToolTip(f"Leave \"{action.label}\" with no key at all.")
            clear.clicked.connect(lambda _checked=False, e=editor: e.clear())
            table.setCellWidget(row, 3, clear)

        table.setMinimumHeight(320)
        layout.addWidget(table)

        self.shortcut_conflict_label = QLabel("")
        self.shortcut_conflict_label.setWordWrap(True)
        # "Needs a decision" tier - the same weight Status's error/
        # not_found chips draw at (gui/theme.py STATUS_WEIGHTS) - rather
        # than a hue, since a shortcut collision is exactly that: the
        # user has to resolve it, it is not a passive warning.
        self.shortcut_conflict_label.setStyleSheet(
            f"color: {theme.ink_color(self._mode, 'ink_100').name()};"
        )
        layout.addWidget(self.shortcut_conflict_label)

        reset = QPushButton("Reset all to defaults")
        reset.clicked.connect(self._reset_shortcuts_to_defaults)
        row_layout = QHBoxLayout()
        row_layout.addStretch(1)
        row_layout.addWidget(reset)
        layout.addLayout(row_layout)

        self._refresh_shortcut_conflicts()
        return widget

    def _current_shortcut_bindings(self) -> dict:
        return {
            action_id: editor.keySequence().toString()
            for action_id, editor in self.shortcut_edits.items()
        }

    def _reset_shortcuts_to_defaults(self):
        for action_id, key in shortcut_registry.default_bindings().items():
            editor = self.shortcut_edits.get(action_id)
            if editor is not None:
                editor.setKeySequence(QKeySequence(key))
        self._refresh_shortcut_conflicts()

    def _refresh_shortcut_conflicts(self) -> dict:
        """Shows any key bound twice, and reports them to the caller.

        Worth naming rather than resolving silently: Qt's answer to an
        ambiguous shortcut is to fire neither action, so a duplicate does
        not produce the wrong behaviour - it produces a key that does
        nothing at all.
        """
        clashes = shortcut_registry.conflicts(self._current_shortcut_bindings())
        if not clashes:
            self.shortcut_conflict_label.setText("")
            return clashes
        lines = []
        for ids in clashes.values():
            labels = [shortcut_registry.ACTIONS_BY_ID[i].label for i in ids]
            shown = self.shortcut_edits[ids[0]].keySequence().toString()
            lines.append(f"{shown} is bound to {' and '.join(labels)}")
        self.shortcut_conflict_label.setText(
            "Same key on more than one action - Qt fires neither, so these "
            "would both stop working:\n" + "\n".join(lines)
        )
        return clashes

    # -- MCP -------------------------------------------------------------
    def _build_mcp_tab(self) -> QWidget:
        """The embedded MCP server - mcp-settings-spec.md (DAN-706) is the
        accepted authority for every string and grouping below; follow it
        exactly rather than inventing wording."""
        mcp = self.settings.mcp
        widget = QWidget()
        layout = QVBoxLayout(widget)

        intro = widgets.muted(
            "Lets a connected AI read the queue, see the pictures, and record "
            "decisions while you're away. Off by default."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.mcp_enabled = QCheckBox("Enable the MCP server")
        self.mcp_enabled.setChecked(mcp.enabled)
        # Connected AFTER setChecked, so restoring a saved enabled=True
        # state from the loaded Settings never fires this - only a real
        # user click does. See §5a / §10 risk 5: the one-time summary
        # must never fire on construction, only on an actual flip.
        self.mcp_enabled.toggled.connect(self._on_mcp_enabled_toggled)
        layout.addWidget(self.mcp_enabled)

        layout.addWidget(self._build_mcp_connection_card())

        form = QFormLayout()
        self.mcp_port = QSpinBox()
        self.mcp_port.setRange(1024, 65535)
        self.mcp_port.setValue(mcp.port)
        self.mcp_port.valueChanged.connect(self._refresh_mcp_connection)
        form.addRow("Port:", self.mcp_port)
        form.addRow("", widgets.hint(
            "Loopback only (127.0.0.1) — never reachable from another machine on "
            "your network."))

        self.mcp_token = QLineEdit(mcp.token)
        self.mcp_token.setEchoMode(QLineEdit.EchoMode.Password)
        token_row = QHBoxLayout()
        token_row.addWidget(self.mcp_token)
        token_row.addWidget(widgets.icon_button(
            "⧉", "Copy token to clipboard", self._copy_mcp_token))
        token_row.addWidget(widgets.pill_button(
            "Generate new token", self._generate_mcp_token, uppercase=False))
        form.addRow("Token:", token_row)
        self.mcp_token_hint = widgets.hint("")
        self.mcp_token.textEdited.connect(lambda _text: self.mcp_token_hint.setText(""))
        form.addRow("", self.mcp_token_hint)
        layout.addLayout(form)

        self.mcp_dry_run = QCheckBox("Dry run — practice mode")
        self.mcp_dry_run.setChecked(mcp.dry_run)
        self.mcp_dry_run.toggled.connect(self._refresh_mcp_dry_run_hint)
        layout.addWidget(self.mcp_dry_run)
        self.mcp_dry_run_hint = widgets.hint("")
        layout.addWidget(self.mcp_dry_run_hint)
        self._refresh_mcp_dry_run_hint()

        layout.addWidget(self._build_mcp_always_available_group())
        self.mcp_allow_hydrus_writes = self._build_mcp_tier_checkbox_group(
            layout,
            box_title="On by default — sends to your live Hydrus",
            label="Let it send files and tags to Hydrus",
            checked=mcp.allow_hydrus_writes,
            hint_text=(
                "This is on, because you asked for it. Every send lands as a row "
                "below in Recent tool calls — check there for exactly what went "
                "out and when. Turn this off here to stop it."),
            dangerous=False,
            live_marker=True,
        )
        self.mcp_allow_research = self._build_mcp_tier_checkbox_group(
            layout,
            box_title="Off by default — spends a limited allowance",
            label="Let it re-search a stuck entry",
            checked=mcp.allow_research,
            hint_text=(
                "Spends the same daily allowance a manual re-search would — "
                "SauceNAO's is capped per day. Leave this off to keep that "
                "allowance for your own searching."),
            dangerous=False,
        )
        self.mcp_allow_destructive = self._build_mcp_tier_checkbox_group(
            layout,
            box_title="Off by default — hard to undo by hand",
            label="Let it remove rows and reset results",
            checked=mcp.allow_destructive,
            hint_text=(
                "Throws away work this app already did. There's no confirmation "
                "prompt from the AI side — only this switch."),
            dangerous=True,
        )

        layout.addWidget(self._build_mcp_audit_card())
        layout.addStretch(1)

        self._refresh_mcp_connection()
        return widget

    def _build_mcp_connection_card(self) -> QWidget:
        card, layout = widgets.card("Connection")

        self.mcp_connection_primary = QLabel("")
        layout.addWidget(self.mcp_connection_primary)
        self.mcp_connection_remedy = widgets.hint("")
        layout.addWidget(self.mcp_connection_remedy)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.mcp_open_log_btn = widgets.pill_button(
            "Open the log", lambda: self._mcp_main_window.action_view_logs(),
            uppercase=False)
        buttons.addWidget(self.mcp_open_log_btn)
        self.mcp_restart_btn = widgets.pill_button(
            "Restart server", self._mcp_restart_server, uppercase=False)
        buttons.addWidget(self.mcp_restart_btn)
        layout.addLayout(buttons)
        return card

    def _build_mcp_always_available_group(self) -> QGroupBox:
        box = QGroupBox("Always available when MCP is on")
        layout = QVBoxLayout(box)
        for text in (
            "Read everything — the queue, each entry, candidates, scores, "
            "upscale verdicts, tags, the images themselves, and the diff "
            "between two candidates.",
            "Make reversible local decisions — pick a candidate, mark an entry "
            "reviewed. Undo by opening the entry again and choosing differently.",
        ):
            label = QLabel(text)
            label.setWordWrap(True)
            layout.addWidget(label)
        return box

    def _build_mcp_tier_checkbox_group(
        self, parent_layout, *, box_title, label, checked, hint_text, dangerous,
        live_marker=False,
    ) -> QCheckBox:
        """One of §5's tier QGroupBoxes: a checkbox, optionally the `●`
        live marker next to it, and a one-line consequence hint at the
        given ink weight. Returns the checkbox so apply_to_settings() can
        read it back."""
        box = QGroupBox(box_title)
        box_layout = QVBoxLayout(box)

        row = QHBoxLayout()
        checkbox = QCheckBox(label)
        checkbox.setChecked(checked)
        row.addWidget(checkbox)
        if live_marker:
            # A one-character QLabel on purpose - see mcp-settings-spec.md
            # §9: it is deliberately NOT a useful search term, so its text
            # is exactly the glyph and nothing else. STATUS_GLYPHS['sent']
            # is the same "confirmed positive, this happened for real"
            # glyph the audit table's own Outcome column reuses below.
            marker = QLabel(theme.STATUS_GLYPHS['sent'])
            marker.setToolTip("Live - this permission is already on.")
            marker.setStyleSheet(
                f"color: {theme.ink_color(self._mode, 'ink_65').name()};")
            row.addWidget(marker)
        row.addStretch(1)
        box_layout.addLayout(row)

        hint = widgets.hint(hint_text)
        # The two branches are both literals, resolved inline rather than
        # through a passed-in tier string - see _mcp_outcome_weight()'s
        # docstring on why tests/test_theme.py needs this shape.
        hint.setStyleSheet(
            f"color: {theme.ink_color(self._mode, 'ink_100' if dangerous else 'ink_65').name()};")
        box_layout.addWidget(hint)

        parent_layout.addWidget(box)
        return checkbox

    def _copy_mcp_token(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.mcp_token.text())

    def _generate_mcp_token(self):
        import secrets
        value = secrets.token_urlsafe(32)
        self.mcp_token.setText(value)
        QApplication.clipboard().setText(value)
        self.mcp_token_hint.setText("New token generated and copied to clipboard.")

    def _refresh_mcp_dry_run_hint(self):
        on = self.mcp_dry_run.isChecked()
        self.mcp_dry_run_hint.setStyleSheet(
            f"color: {theme.ink_color(self._mode, 'ink_65' if on else 'ink_100').name()};")
        self.mcp_dry_run_hint.setText(
            "Dry run is on. The AI can look at everything and tell you what it "
            "would pick, but every action — picking a candidate, marking "
            "reviewed, re-searching, sending to Hydrus, removing a row — is "
            "logged as recorded-not-run and never actually happens."
            if on else
            "Dry run is off. Allowed actions happen for real, including "
            "anything enabled above."
        )

    def _on_mcp_enabled_toggled(self, checked: bool):
        self._refresh_mcp_connection()
        if checked:
            self._show_mcp_enable_summary()

    def _show_mcp_enable_summary(self):
        """mcp-settings-spec.md §5a: fires once per unchecked→checked
        edge of the server checkbox, listing exactly the tiers currently
        ticked in this dialog - not the saved settings, since the user
        may have just changed one and not applied yet."""
        lines = [
            "Starting now, a connected AI with the token below can:",
            "",
            " •  Read everything — the queue, entries, candidates, scores, tags,",
            "    the images themselves, and diffs.",
            " •  Pick a candidate and mark entries reviewed. Undo by opening the",
            "    entry again.",
        ]
        if self.mcp_allow_hydrus_writes.isChecked():
            lines.append(" •  Send files and tags to your live Hydrus library — on by default.")
        if self.mcp_allow_research.isChecked():
            lines.append(" •  Re-search a stuck entry, spending your SauceNAO allowance.")
        if self.mcp_allow_destructive.isChecked():
            lines.append(" •  Remove rows and reset results, with no confirmation on its side.")
        lines.append("")
        lines.append("Recent tool calls below records exactly what it does. Turn any of")
        lines.append("this off in the groups above, any time.")

        box = message.build(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("Enabling the MCP server")
        box.setText("\n".join(lines))
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        ok_button = box.button(QMessageBox.StandardButton.Ok)
        if ok_button is not None:
            # "OK" reads as dismissing an error; this is acknowledging a fact.
            ok_button.setText("Got it")
        try:
            box.exec()
        finally:
            box.deleteLater()

    def _mcp_server_handle(self):
        return getattr(self._mcp_main_window, "_mcp_server", None)

    def _mcp_restart_server(self):
        """Re-binds the live server with whatever port/token/enabled is
        currently TYPED, not the saved settings - mirrors the Hydrus tab's
        "Test connection" button, just with a real side effect instead of
        a read-only probe (mcp-settings-spec.md §3's note on this button)."""
        from dataclasses import replace

        server = self._mcp_server_handle()
        if server is None:
            return
        temp = replace(
            self.settings.mcp,
            enabled=True, port=self.mcp_port.value(), token=self.mcp_token.text(),
        )
        server.restart(temp)
        self._refresh_mcp_connection()

    def _refresh_mcp_connection(self):
        """Redraws the Connection card from the actually-running server,
        per mcp-settings-spec.md §3's six states."""
        server = self._mcp_server_handle()
        port = self.mcp_port.value()
        enabled = self.mcp_enabled.isChecked()

        if not enabled:
            state, err = 1, None
        elif server is not None and server.running:
            state, err = 2, None
        else:
            err = (server.last_error if server is not None else None) or ""
            lowered = err.lower()
            if "not installed" in lowered:
                state = 3
            elif "blank bearer token" in lowered:
                state = 4
            elif "in use" in lowered:
                state = 5
            elif err:
                state = 6
            else:
                # Enabled, but nothing has actually attempted a bind from
                # here yet (e.g. no main-window handle at all, as in a
                # headless construction) - nothing to report beyond "not
                # running".
                state = 1

        primary, remedy = {
            1: ("Stopped.",
                'Turn on "Enable the MCP server" above to let a connected AI read '
                'the queue — and, once you choose, act on it.'),
            2: (f"Listening on {MCP_HOST}:{port}.",
                "A client with the token below can connect now."),
            3: ("Can't start — the mcp package isn't installed.",
                'Install it: venv/bin/pip install "mcp>=1.2,<2", then restart Hatate. '
                "(It's optional, so this app never installs it for you.)"),
            4: ("Can't start — no token is set.",
                'Generate one below, then click "Restart server."'),
            5: (f"Can't start — port {port} is already in use.",
                "Something else on this machine is using that port — maybe "
                'another copy of Hatate. Pick a different port below and click '
                '"Restart server."'),
            6: (err or "Couldn't start.", ""),
        }[state]
        # States 1-2 are healthy/inactive-by-choice (ink_65); 3-6 need the
        # user to act (ink_100) - same ink-ramp convention STATUS_WEIGHTS
        # already uses. Inlined as a literal IfExp (rather than a variable
        # threaded from the dict above) so tests/test_theme.py's
        # discover_ink_color_tiers() can resolve it statically.
        healthy = state in (1, 2)

        self.mcp_connection_primary.setText(primary)
        self.mcp_connection_primary.setStyleSheet(
            f"color: {theme.ink_color(self._mode, 'ink_65' if healthy else 'ink_100').name()};")
        self.mcp_connection_remedy.setText(remedy)
        self.mcp_connection_remedy.setVisible(bool(remedy))
        self.mcp_restart_btn.setVisible(enabled)
        self.mcp_open_log_btn.setVisible(state == 6)

    # -- MCP: recent tool calls ------------------------------------------
    def _build_mcp_audit_card(self) -> QWidget:
        card, layout = widgets.card()

        header = QHBoxLayout()
        header.addWidget(widgets.heading("Recent tool calls"))
        header.addStretch(1)
        self.mcp_audit_filter_row, self.mcp_audit_filter_buttons = widgets.segmented(
            [("actions", "Actions"), ("all", "All calls")],
            on_change=self._on_mcp_audit_filter_changed, current="actions",
            uppercase=False,
        )
        header.addWidget(self.mcp_audit_filter_row)
        layout.addLayout(header)

        controls = QHBoxLayout()
        self.mcp_audit_follow_btn = widgets.icon_button(
            "⟳", "Keep this up to date while the tab is open.",
            self._on_mcp_audit_follow_toggled, checkable=True)
        self.mcp_audit_follow_btn.setChecked(True)
        controls.addWidget(self.mcp_audit_follow_btn)
        controls.addWidget(widgets.icon_button(
            "↗", "Open the folder the audit log is in.", self._open_mcp_audit_folder))
        controls.addStretch(1)
        controls.addWidget(QLabel("Keep up to"))
        self.mcp_audit_max_mb = QSpinBox()
        self.mcp_audit_max_mb.setRange(1, 500)
        self.mcp_audit_max_mb.setValue(
            max(1, self.settings.mcp.audit_log_max_bytes // 1_000_000))
        controls.addWidget(self.mcp_audit_max_mb)
        controls.addWidget(QLabel("MB before trimming the oldest entries."))
        layout.addLayout(controls)

        self.mcp_audit_table = QTableWidget(0, 5)
        self.mcp_audit_table.setHorizontalHeaderLabels(
            ["Time", "Tool", "Tier", "Target", "Outcome"])
        self.mcp_audit_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.mcp_audit_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        row_header = self.mcp_audit_table.verticalHeader()
        if row_header is not None:
            row_header.setVisible(False)
        header_view = self.mcp_audit_table.horizontalHeader()
        if header_view is not None:
            for column in (0, 1, 2, 3):
                header_view.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
            header_view.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.mcp_audit_table.setMinimumHeight(220)
        layout.addWidget(self.mcp_audit_table)

        self.mcp_audit_shown_caption = widgets.muted("")
        layout.addWidget(self.mcp_audit_shown_caption)
        layout.addWidget(widgets.muted(f"Writing to {mcp_audit.AUDIT_LOG_FILE}"))

        self._mcp_audit_timer = QTimer(self)
        self._mcp_audit_timer.setInterval(2000)
        self._mcp_audit_timer.timeout.connect(self._refresh_mcp_audit_table)
        self._refresh_mcp_audit_table()
        return card

    def _on_mcp_audit_filter_changed(self, _key):
        self._refresh_mcp_audit_table()

    def _on_mcp_audit_follow_toggled(self):
        if self.mcp_audit_follow_btn.isChecked():
            self._refresh_mcp_audit_table()
            if self.tabs.currentIndex() == self._mcp_tab_index:
                self._mcp_audit_timer.start()
        else:
            self._mcp_audit_timer.stop()

    def _open_mcp_audit_folder(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(mcp_audit.AUDIT_LOG_FILE.parent)))

    def _on_tabs_current_changed(self, index: int):
        timer = getattr(self, "_mcp_audit_timer", None)
        if timer is None:
            return
        if index == self._mcp_tab_index:
            self._refresh_mcp_connection()
            self._refresh_mcp_audit_table()
            if self.mcp_audit_follow_btn.isChecked():
                timer.start()
        else:
            timer.stop()

    def _refresh_mcp_audit_table(self):
        entries = _read_mcp_audit_entries(mcp_audit.AUDIT_LOG_FILE)
        if self.mcp_audit_filter_buttons["actions"].isChecked():
            entries = [e for e in entries if e.get("tier") != "read"]
        total = len(entries)
        shown = list(reversed(entries[-_MCP_AUDIT_DISPLAY_LIMIT:]))

        table = self.mcp_audit_table
        table.setRowCount(len(shown))
        for row, entry in enumerate(shown):
            table.setItem(row, 0, QTableWidgetItem(_mcp_time_cell(entry.get("timestamp", ""))))
            table.item(row, 0).setFont(theme.mono_font())
            table.setItem(row, 1, QTableWidgetItem(str(entry.get("tool", ""))))
            table.item(row, 1).setFont(theme.mono_font())
            tier = entry.get("tier", "")
            tier_label = _MCP_TIER_LABELS.get(tier, tier)
            table.setItem(row, 2, QTableWidgetItem(tier_label))
            table.setItem(row, 3, QTableWidgetItem(_mcp_target_cell(entry)))

            text, _ = _mcp_outcome_cell(entry)
            outcome_item = QTableWidgetItem(text)
            outcome_item.setToolTip(_mcp_outcome_tooltip(entry))
            outcome_item.setForeground(theme.ink_color(self._mode, _mcp_outcome_weight(entry)))
            table.setItem(row, 4, outcome_item)

        caption = (
            f"Showing the most recent {len(shown)} of {total} calls."
            if total > len(shown) else "")
        self.mcp_audit_shown_caption.setText(caption)

    def accept(self):
        """Refuses to close on a shortcut conflict.

        A warning that can be clicked past would leave two dead keys
        behind, and nothing later would explain why they stopped working.
        """
        built = getattr(self, "shortcut_conflict_label", None)
        if built is not None and self._refresh_shortcut_conflicts():
            message.warning(
                self, "Shortcut conflict",
                self.shortcut_conflict_label.text()
                + "\n\nGive one of them a different key, or clear it.",
            )
            return
        super().accept()

    def apply_to_settings(self):
        s = self.settings
        s.review_shortcuts = shortcut_registry.prune(self._current_shortcut_bindings())
        s.delay_min_seconds = self.delay_min.value()
        s.delay_max_seconds = max(self.delay_max.value(), self.delay_min.value())

        # 0 in either box means "same as above" - no override for that
        # engine, so the key is dropped rather than stored as a real zero,
        # which would read as "no delay at all".
        overrides = {}
        for engine, (spin_min, spin_max) in self.engine_delays.items():
            low, high = spin_min.value(), spin_max.value()
            if low <= 0 and high <= 0:
                continue
            low = low if low > 0 else high
            high = high if high > 0 else low
            overrides[engine] = [min(low, high), max(low, high)]
        s.engine_delays = overrides
        s.search_timeout = self.search_timeout.value()
        s.hash_source = self.hash_source.currentData()
        s.thumbnail_source = self.thumbnail_source.currentData()
        s.lazy_thumbnails = self.lazy_thumbnails.isChecked()
        s.hash_workers = self.hash_workers.value()
        s.retrieve_tags_from_booru = self.retrieve_booru_tags.isChecked()
        s.rank_matches_by_quality = self.rank_by_quality.isChecked()
        s.drop_dead_matches = self.drop_dead_matches.isChecked()
        s.drop_restricted_matches = self.drop_restricted_matches.isChecked()
        s.borrow_tags_from_other_matches = self.borrow_tags.isChecked()
        s.borrow_tags_similarity_slack = self.borrow_tags_slack.value()
        s.retry_failed_searches = self.retry_failed_searches.isChecked()
        s.remove_after_import = self.remove_after_import.isChecked()
        s.restore_session_on_start = self.restore_session.isChecked()
        s.autosave_session = self.autosave_session.isChecked()
        s.autosave_interval_seconds = self.autosave_interval.value()
        s.search_cache_ttl_days = self.search_cache_ttl.value()
        s.use_search_cache = self.use_search_cache.isChecked()
        s.auto_import_enabled = self.auto_import_enabled.isChecked()
        s.auto_import_min_similarity = self.auto_import_min_similarity.value()
        s.auto_import_method = self.auto_import_method.currentData()
        s.url_import_confirm_timeout = self.url_import_confirm_timeout.value()
        s.url_import_confirm_interval = self.url_import_confirm_interval.value()
        s.pixiv_session_cookie = self.pixiv_session_cookie.text().strip()
        s.e621_username = self.e621_username.text().strip()
        s.e621_api_key = self.e621_api_key.text().strip()
        s.sankaku_cookies = self.sankaku_cookies.text().strip()
        s.animepictures_cookies = self.animepictures_cookies.text().strip()
        # Only written when the user actually changed the box. A search
        # running on a background QThread while this dialog was open may
        # have already rotated s.deviantart_cookies to a newer, still-live
        # session (see core.site_access.capture_rotated_deviantart_cookies)
        # - if the box was never touched, that rotation is the more
        # current value and Save must not overwrite it with the stale
        # string the dialog was opened with. An actual edit is explicit
        # user intent and always wins, including clearing the field
        # entirely to log out (DAN-123).
        da_typed = self.deviantart_cookies.text().strip()
        if da_typed != self._deviantart_cookies_snapshot.strip():
            s.deviantart_cookies = da_typed
        s.enable_tag_namespace_remap = self.enable_namespace_remap.isChecked()

        remap = {}
        for line in self.namespace_remap_edit.toPlainText().splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            old, new = line.split(":", 1)
            old, new = old.strip(), new.strip()
            if old:
                remap[old] = new  # an empty "new" means "strip this namespace entirely"
        s.tag_namespace_remap = remap

        s.enable_tag_blacklist = self.enable_tag_blacklist.isChecked()
        # Keep the user's lines verbatim apart from trimming and dropping
        # blanks - normalization happens at match time, so the box always
        # shows back exactly what was typed.
        s.tag_blacklist = [
            line.strip()
            for line in self.tag_blacklist_edit.toPlainText().splitlines()
            if line.strip()
        ]

        s.add_rating_tag = self.add_rating_tag.isChecked()
        s.rating_tag_namespace = self.rating_tag_namespace.text().strip()

        # Tags applied by rule (DAN-77). Stored as typed, minus blank
        # lines; the namespace split happens when the tag is built, so
        # the box shows back exactly what was entered.
        s.tags_for_found = _nonblank_lines(self.tags_for_found_edit)
        s.tags_for_not_found = _nonblank_lines(self.tags_for_not_found_edit)
        s.tags_for_low_tag_count = _nonblank_lines(self.tags_for_low_tag_count_edit)

        s.enable_hydrus_tag_colors = self.enable_tag_colors.isChecked()
        colors = {}
        for line in self.namespace_colors_edit.toPlainText().splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            ns, color = line.split(":", 1)
            ns, color = ns.strip(), color.strip()
            if color:  # namespace may be empty (means "unnamespaced"), but a color is required
                colors[ns] = color
        s.tag_namespace_colors = colors

        s.primary_engine = self.primary_engine.currentData()
        s.secondary_engine_mode = self.secondary_engine_mode.currentData()
        s.fallback_below_similarity = self.fallback_below_similarity.value()
        s.extras_only_as_fallback = self.extras_only_as_fallback.isChecked()
        s.enable_ascii2d = self.enable_ascii2d.isChecked()
        s.enable_tracemoe = self.enable_tracemoe.isChecked()
        s.enable_iqdb3d = self.enable_iqdb3d.isChecked()
        s.enable_google_images = self.enable_google_images.isChecked()
        s.google_images_api_key = self.google_images_key.text()
        s.enable_google_lens = self.enable_google_lens.isChecked()
        s.lens_search_whole_image = self.lens_search_whole_image.isChecked()
        s.enable_yandex = self.enable_yandex.isChecked()
        s.enable_pawchive = self.enable_pawchive.isChecked()
        s.enable_pawchive_index = self.enable_pawchive_index.isChecked()
        s.tracemoe_min_similarity = self.tracemoe_min_sim.value()
        s.log_matched_urls = self.log_matched.isChecked()
        s.log_file_path = self.log_path.text()
        s.enabled_tag_sources = [
            self.tag_sources_list.item(i).text()
            for i in range(self.tag_sources_list.count())
            if self.tag_sources_list.item(i).checkState() == Qt.CheckState.Checked
        ]
        s.filter_sent_tags_by_source = self.filter_sent_tags.isChecked()
        s.sidecar_filename_style = self.sidecar_style.currentData()
        s.sidecar_overwrite = self.sidecar_overwrite.currentData()

        s.saucenao.api_key = self.saucenao_key.text()
        s.saucenao.use_json_api = self.saucenao_json.isChecked()
        s.saucenao.pause_search_on_quota_exhausted = self.saucenao_pause_on_quota.isChecked()
        s.continue_without_saucenao_on_quota = self.continue_without_saucenao.isChecked()
        s.saucenao.min_similarity = self.saucenao_min_sim.value()
        s.saucenao.namespace = self.saucenao_namespace.text()
        s.saucenao.tag_types = [
            self.saucenao_tag_types.item(i).text()
            for i in range(self.saucenao_tag_types.count())
            if self.saucenao_tag_types.item(i).checkState() == Qt.CheckState.Checked
        ]

        s.hydrus.api_url = self.hydrus_url.text().rstrip("/")
        s.hydrus.access_key = self.hydrus_key.text()
        s.hydrus.tag_service_key = self.hydrus_service_key.currentText().strip()
        s.hydrus.verify_ssl = self.hydrus_verify_ssl.isChecked()
        s.set_hydrus_duplicate_relationships = self.set_hydrus_duplicate_relationships.isChecked()
        s.write_hydrus_provenance_note = self.write_hydrus_provenance_note.isChecked()
        s.hydrus_provenance_note_name = self.hydrus_provenance_note_name.text().strip()

        s.mcp.enabled = self.mcp_enabled.isChecked()
        s.mcp.port = self.mcp_port.value()
        s.mcp.token = self.mcp_token.text()
        s.mcp.dry_run = self.mcp_dry_run.isChecked()
        s.mcp.allow_research = self.mcp_allow_research.isChecked()
        s.mcp.allow_hydrus_writes = self.mcp_allow_hydrus_writes.isChecked()
        s.mcp.allow_destructive = self.mcp_allow_destructive.isChecked()
        s.mcp.audit_log_max_bytes = self.mcp_audit_max_mb.value() * 1_000_000

        log.info("Settings updated from Preferences dialog")
        s.save()

    def _update_engine_pipeline(self):
        """Updates the pipeline summary label from current UI state."""
        # Create a temporary Settings object with current UI values
        from core.config import Settings
        temp = Settings()
        temp.primary_engine = self.primary_engine.currentData()
        temp.secondary_engine_mode = self.secondary_engine_mode.currentData()
        temp.fallback_below_similarity = self.fallback_below_similarity.value()
        temp.extras_only_as_fallback = self.extras_only_as_fallback.isChecked()
        temp.enable_ascii2d = self.enable_ascii2d.isChecked()
        temp.enable_tracemoe = self.enable_tracemoe.isChecked()
        temp.enable_iqdb3d = self.enable_iqdb3d.isChecked()
        temp.enable_google_images = self.enable_google_images.isChecked()
        temp.enable_google_lens = self.enable_google_lens.isChecked()
        temp.enable_yandex = self.enable_yandex.isChecked()
        temp.enable_pawchive = self.enable_pawchive.isChecked()
        temp.enable_pawchive_index = self.enable_pawchive_index.isChecked()
        text = pipeline_summary(temp)
        self.engine_pipeline.setText(text)
