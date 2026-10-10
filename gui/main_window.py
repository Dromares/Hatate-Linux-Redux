from __future__ import annotations

import os
import shutil
import tempfile
import time
import weakref
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

from PIL import Image
from PyQt6 import sip
from PyQt6.QtCore import (
    Qt, QSize, QByteArray, QEvent, QItemSelection, QItemSelectionModel, QTimer, pyqtSignal,
)
from PyQt6.QtGui import (
    QAction, QActionGroup, QColor, QDesktopServices, QIcon, QImageReader, QPalette,
    QPixmap, QShortcut,
)
from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QFrame, QStackedWidget,
    QHBoxLayout, QHeaderView, QListWidgetItem,
    QMainWindow, QMenu, QMessageBox, QTableView, QVBoxLayout, QWidget,
)

from core import crashlog
from core.applog import get_logger
from core.file_intake import filter_duplicate_paths, scan_folder
from core.formats import FILE_DIALOG_FILTER, IMAGE_EXTENSIONS
from core.engines import effective_primary, label_for as engine_label, opposite_engine
from core import export as results_export
from core.eta import RunEstimate
from core.config import Settings
from core import parser_health
from core.hydrus_client import HydrusClient, HydrusError
from core.hydrus_reconcile import entries_awaiting_confirmation
from core.lru_cache import LRUCache
from core.hydrus_import import (
    ImportResult, download_and_send, normalize_url_for_hydrus, send_file_upload,
    send_url_or_download,
)
from core.models import ImageEntry, MatchStatus, TagSource
from core.ascii2d import reset_blocked_flag as reset_ascii2d_blocked
from core.google_images import reset_blocked_flag as reset_google_blocked
from core import engine_alerts, google_lens, lens_browser, session_db, sidecar, yandex
from core.saucenao import (
    clear_quota_pause, describe_quota, describe_quota_pause, describe_short_window,
    get_last_quota, get_quota_pause_state, reset_daily_limit_flag,
)
from core.session import (
    clear_session, has_saved_session, load_session, save_session, session_entry_count,
)
from core.similarity_display import similarity_label
from core.sites import ALL_SITE_OPTIONS
from core.viewport import entries_needing_thumbnails, visible_range_with_buffer
from core.tag_colors import get_tag_color
from core.tag_rules import apply_inline_edit
from gui import theme, widgets
from gui.shell import EMPTY_GHOST_GLYPH, IDLE_ETA, IDLE_PROGRESS, ShellMixin
from gui.review_view import ReviewViewMixin
from gui.activity_view import ActivityViewMixin
from gui.table_delegates import RuledRowDelegate, ChipDelegate
from gui.add_tags_dialog import AddTagsDialog
# Re-exported deliberately: these define how a row renders, so they live
# with the model, but the sort keys here (and the GUI tests) still import
# them from this module.
from gui.image_table_model import (  # noqa: F401
    COLUMNS, COL_SENT, COL_SIMILARITY, COL_STATUS, COL_THUMB, ImageTableModel,
    default_column_widths, header_layout_is_usable,
    sort_key_for_column, _entry_cache_label, _entry_engine_label,
    _entry_sent_label, _entry_size_delta_label, _size_delta_weight,
)
from gui.compare_dialog import CompareDialog
from gui.parser_health_dialog import ParserHealthDialog
from gui import message
from gui.hydrus_query_dialog import HydrusQueryDialog
from gui.log_viewer_dialog import LogViewerDialog
from gui.match_conditions_dialog import MatchConditionsDialog
from gui.settings_dialog import SettingsDialog
from gui.filter_bar import FilterBar
from gui import review_shortcuts
from gui.preview_text import (
    ComparisonReadout, comparison_readout, human_size,
    local_image_info_text, matched_caption, matched_image_info_text,
    no_candidate_text,
)
from gui.session_autosave import SessionAutosaver
from gui import table_context_menu
from gui.mcp_bridge import McpToolHandlers
from gui.worker_lifecycle import WorkerRegistry
from core.mcp_server import McpServerController
from workers.availability_worker import AvailabilityWorker
from workers.candidate_worker import CandidateFetchWorker
from workers.file_hash_worker import FileHashWorker
from workers.hydrus_import_poll_worker import HydrusImportPollWorker
from workers.hydrus_reconcile_worker import HydrusReconcileWorker
from workers.hydrus_lookup_worker import HydrusTagLookupWorker
from workers.missing_file_worker import MissingFileWorker
from workers.search_worker import SearchWorker
from workers.thumbnail_worker import ThumbnailWorker
from workers.upscale_check_worker import UpscaleCheckWorker

log = get_logger("gui")
# Its own logger so the (deliberately chatty) lazy-thumbnail tracing can be
# read - or filtered out - independently of everything else the GUI logs.
lazylog = get_logger("gui.lazy_thumbs")

THUMB_COLUMN_SIZE = 40  # px, the row icon; the row itself is QUEUE_ROW_HEIGHT
QUEUE_ROW_HEIGHT = 49   # px, the mockup's row (Q-06): hairline included, so 48 + 1 rule

# Row thumbnails are decoded at this multiple of their display size, then
# smooth-scaled down. Asking the decoder for exactly 48px produces a
# visibly rough result; decoding larger and filtering down is far cleaner,
# and also leaves headroom for HiDPI displays where the icon is drawn at
# more than its logical pixel size.
THUMB_DECODE_QUALITY_FACTOR = 4

# Cache caps, sized by how expensive each entry is rather than one shared
# number. Row icons are tiny (~9 KB) and evicting one leaves a visibly
# blank row until it's regenerated, so they get a generous cap. Previews
# are ~1.9 MB each, so a much tighter cap keeps peak memory sane - you
# only ever look at one at a time, and re-decoding on revisit is quick.
THUMB_ICON_CACHE_ENTRIES = 4000      # ~36 MB worst case

# Lazy thumbnails: how many rows either side of the visible range to
# generate as well, so a short scroll lands on already-loaded rows
# instead of blank ones.
# A column narrower than this can't show its own title, which reads as
# "the headers are missing" rather than as a layout problem.
MIN_COLUMN_WIDTH = 24
DEFAULT_COLUMN_WIDTH = 90
MIN_VIEWPORT_FOR_DEFAULT_WIDTHS = 600  # narrower is a not-yet-laid-out table, not a real size

THUMB_VIEWPORT_BUFFER_ROWS = 15
# Scroll events fire continuously; coalesce a burst into one pass.
THUMB_VIEWPORT_DEBOUNCE_MS = 120
PREVIEW_PIXMAP_CACHE_ENTRIES = 6     # at 2048px, ~75-100 MB worst case - the
                                     # same budget 40 entries were at 700px, and
                                     # still enough for stepping back and forth


def _format_duration(seconds: float) -> str:
    """Compact human-readable duration for progress estimates - '45s',
    '2m 30s', '1h 5m', '6d 4h'. Deliberately coarse: an estimate implying
    second-level precision would overstate how accurate it is.

    Days matter because a search run reaches them easily - at the default
    45-75s an image, any sizeable library is a multi-day run, and '389h'
    is a number nobody can read as a fortnight.
    """
    seconds = max(int(round(seconds)), 0)
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    days, hrs = divmod(hours, 24)
    return f"{days}d {hrs}h" if hrs else f"{days}d"


def _format_finish_time(seconds_from_now: float, now: Optional[datetime] = None) -> str:
    """When a run will end, phrased the way the distance warrants.

    'ends 14:20' for something finishing today, 'ends tomorrow 09:05',
    and a bare date beyond that - by which point the clock time is
    false precision on an estimate this soft, and the day is the part
    anyone actually wants.
    """
    start = datetime.now() if now is None else now
    try:
        finish = start + timedelta(seconds=max(seconds_from_now, 0))
    except OverflowError:
        # A pace estimate taken during a stall can produce an absurd
        # horizon; no finish time is better than a crash or a year 9999.
        return ""
    days_ahead = (finish.date() - start.date()).days
    if days_ahead <= 0:
        return f"ends {finish:%H:%M}"
    if days_ahead == 1:
        return f"ends tomorrow {finish:%H:%M}"
    if days_ahead < 7:
        return f"ends {finish:%a %H:%M}"
    return f"ends {finish:%-d %b}"


def _host_from_url(url: str) -> Optional[str]:
    try:
        return urlparse(url).netloc or None
    except ValueError:
        return None


# The largest the local picture is decoded at for the Review page's side-
# by-side view. That view is a third of the window or more, and on a 125%
# display a 1,000px box is 1,250 real pixels - the old 700px decode (sized
# for a 280px box) was stretched to twice its size and looked soft next to
# the wipe, which uses the full file. Still far short of a multi-thousand-
# pixel scan's full decode.
PREVIEW_DECODE_SIZE = QSize(2048, 2048)
# The empty Queue's headline sits 1 : this of the free height above : below it.
DROP_ZONE_BELOW_WEIGHT = 4

# "Re-check Queued Imports" deliberately appears in both the Files and the
# Hydrus menu (DAN-36). One QAction is shared between them, so this text is
# here rather than inline only to keep it out of _build_hydrus_menu's reach -
# an earlier copy-pasted second copy had already started to drift.
RECHECK_QUEUED_IMPORTS_TOOLTIP = (
    "Asks Hydrus which of the files still showing \"Queued\" it now actually holds, "
    "and marks those as Sent.\n\n"
    "Sending a URL to Hydrus's own downloader is asynchronous, and the check that "
    "runs at the time gives up after a minute - so anything Hydrus took longer than "
    "that to fetch stays Queued indefinitely even though the import worked. This "
    "asks again, by file hash rather than by URL.\n\n"
    "Runs by itself when a session is restored. Nothing is ever un-marked: a file "
    "Hydrus still doesn't know about may simply not have finished downloading."
)


def _load_preview_pixmap(path: str) -> Optional[QPixmap]:
    """Loads an image for preview display, decoding it pre-scaled to
    PREVIEW_DECODE_SIZE rather than loading the full original resolution
    into memory first - matters for large local files (high-res scans,
    wallpapers) where a plain QPixmap(path) load can be slow and memory-
    heavy even after Qt's decode size limit is raised. Falls back to a
    plain QPixmap load if anything about the scaled path doesn't pan out,
    so this never behaves worse than the simple approach."""
    try:
        reader = QImageReader(path)
        reader.setAutoTransform(True)  # respect EXIF orientation
        original_size = reader.size()
        if original_size.isValid() and not original_size.isEmpty():
            scaled = original_size.scaled(PREVIEW_DECODE_SIZE, Qt.AspectRatioMode.KeepAspectRatio)
            reader.setScaledSize(scaled)
        image = reader.read()
        if not image.isNull():
            return QPixmap.fromImage(image)
    except Exception:
        pass  # fall through to the plain loader below

    pix = QPixmap(path)
    return pix if not pix.isNull() else None


def _muted_text_color(widget) -> str:
    """A theme-correct, guaranteed-readable secondary-text color: Qt's
    'disabled' color group is specifically designed to be a dimmed but
    still-legible variant of normal text against the current window
    background, computed by the active style/theme itself - so this is
    correct in both light and dark themes without guessing at a fixed
    color or relying on a specific QSS palette-role keyword spelling."""
    color = widget.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText)
    return color.name()


def _nothing_to_write_text(plan) -> str:
    """Why Write Tag Files has nothing to do.

    Three different reasons, and the difference matters: "none of these
    have been searched yet" is a step to take next, whereas "the files are
    gone" is not. A single "nothing to write" would send the user looking
    for a setting that does not exist."""
    if plan.without_tags and plan.missing_files:
        return (f"Nothing to write: {len(plan.without_tags)} image(s) have no tags "
                f"yet and {len(plan.missing_files)} are missing from disk.")
    if plan.missing_files:
        return (f"Nothing to write: all {len(plan.missing_files)} of those files are "
                "missing from disk, so there's nowhere to put a tag file.")
    return ("Nothing to write: none of those images have any tags yet.\n\n"
            "Search them, or add tags by hand, and try again.")


def _written_summary(plan, result) -> str:
    """The status-bar line after a write.

    Says what was skipped as well as what was written. A run that quietly
    reported "wrote 40 tag files" for 300 selected images would look like
    it had worked."""
    parts = [f"Wrote {len(result.written)} tag file(s)"]
    if result.skipped_existing:
        parts.append(f"{len(result.skipped_existing)} already existed and were kept")
    if plan.without_tags:
        parts.append(f"{len(plan.without_tags)} had no tags")
    if plan.missing_files:
        parts.append(f"{len(plan.missing_files)} missing from disk")
    if result.failures:
        parts.append(f"{len(result.failures)} could not be written")
    return ", ".join(parts)


def _engine_alert_forwarder(window) -> Callable[[object], None]:
    """An engine-alert listener that cannot outlive its window unsafely.

    The registry in core/engine_alerts.py keeps its listeners for the
    session, and it has no idea when a window goes away. A listener that
    held the window strongly would keep the whole main window - its
    caches, its entries - alive for as long as the process ran, and
    emitting a signal on a QMainWindow whose C++ half has already been
    deleted SEGFAULTS, with no traceback and nothing in the log. So the
    reference is weak and the C++ side is checked before the emit;
    closeEvent takes the listener off on the ordinary path, and this is
    what makes a window dropped without being closed harmless.
    """
    ref = weakref.ref(window)

    def forward(alert: object) -> None:
        live = ref()
        if live is None or sip.isdeleted(live):
            return
        live.engine_alert.emit(alert)

    return forward


class MainWindow(ShellMixin, ReviewViewMixin, ActivityViewMixin, QMainWindow):
    # An engine has told core/engine_alerts.py that it cannot run at all.
    # A signal rather than a direct call because the raising side is plain
    # core code on a search worker's thread, and a QMessageBox may only be
    # built on the GUI thread - Qt delivers a cross-thread emit queued, so
    # the search carries on while the box is up rather than stopping dead
    # behind it.
    engine_alert = pyqtSignal(object)

    def __init__(self, unclean_shutdown: bool = False):
        super().__init__()
        self.setWindowTitle("Hatate-linux — IQDB/SauceNAO tagger for Hydrus")
        self.resize(1100, 650)

        # Whether main.py found the previous run's marker still down when
        # this one started - i.e. the last exit was a crash, a kill, or a
        # power cut rather than a normal quit (DAN-485). Read by
        # _restore_saved_session to tell the user apart from the ordinary
        # "restored your last session" case.
        self._unclean_shutdown = unclean_shutdown

        self.settings = Settings.load()
        self.entries: List[ImageEntry] = []
        self._entry_sequence: Dict[int, int] = {}  # id(entry) -> insertion order, for restoring "original"
        # Capped rather than unbounded: a 1,144-image batch previously held
        # every decoded pixmap at once. The two caches get very different
        # caps because their entries differ ~200x in size - a 48px row icon
        # is ~9 KB, a 2048px preview is ~12-16 MB, so 1,144 previews alone
        # would be well over 10 GB while the same number of icons is ~10 MB.
        self._thumb_icon_cache = LRUCache(THUMB_ICON_CACHE_ENTRIES)
        self._local_pixmap_cache = LRUCache(PREVIEW_PIXMAP_CACHE_ENTRIES)
        # Entries whose cached icon was evicted and needs regenerating.
        # Batched behind a short timer so scrolling through a long list
        # triggers one background pass, not one per row.
        self._pending_thumb_regen: set = set()
        self._thumb_regen_timer = QTimer(self)
        self._thumb_regen_timer.setSingleShot(True)
        self._thumb_regen_timer.timeout.connect(self._run_thumbnail_regeneration)

        # Lazy (viewport-driven) thumbnails
        self._viewport_thumb_timer = QTimer(self)
        self._viewport_thumb_timer.setSingleShot(True)
        self._viewport_thumb_timer.timeout.connect(self._update_visible_thumbnails)
        self._thumbs_in_flight: set = set()   # id(entry) currently being generated
        self._last_viewport_range = None      # (first, last) actually generated for, to skip no-op passes
        self._viewport_pass_count = 0
        self._next_sequence = 0
        self._sort_column: Optional[int] = None  # None = "original" (insertion) order
        self._sort_ascending = True
        self._tags_editable = False  # whether the tag list is currently in inline-edit mode
        # Pace of the running search, for the "how much longer" estimate.
        # `_run_active` is separate because the count stays on screen after
        # a run ends - where you got to is worth reading - while a "time
        # left" for a run that is no longer moving would be a lie.
        self._run_estimate = RunEstimate()
        self._run_active = False
        # True once a run has put a count on the strip. Until then the strip
        # follows the list ("nothing queued" / "N not searched").
        self._run_counted = False
        self.worker: Optional[SearchWorker] = None
        self.candidate_worker: Optional[CandidateFetchWorker] = None
        self.hydrus_lookup_worker: Optional[HydrusTagLookupWorker] = None
        self.hydrus_import_poll_worker: Optional[HydrusImportPollWorker] = None
        self.hydrus_reconcile_worker: Optional[HydrusReconcileWorker] = None
        self._reconcile_was_quiet = True
        self.upscale_check_worker: Optional[UpscaleCheckWorker] = None
        self.missing_file_worker: Optional[MissingFileWorker] = None
        self.file_hash_worker: Optional[FileHashWorker] = None
        self.availability_worker: Optional[AvailabilityWorker] = None
        self.thumbnail_worker: Optional[ThumbnailWorker] = None
        # Workers that have been replaced but whose thread hasn't ended yet.
        # See gui/worker_lifecycle.py - deleting a running QThread aborts
        # the process.
        self._workers = WorkerRegistry()
        self._autosaver = SessionAutosaver(lambda: self.entries, self._workers, self)
        # Set at the top of closeEvent, before anything else runs. A worker
        # finishing signal is queued across threads, so one can still be
        # sitting in the event queue - already past retire()'s disconnect
        # race, or from a worker retire() hasn't gotten to yet - when
        # closeEvent starts. Completion slots that would otherwise pop a
        # modal check this first and bail, rather than show a "Missing
        # files" (or similar) dialog belonging to a window that's already on
        # its way out (DAN-128).
        self._closing = False
        self._pending_import_paths: List[str] = []
        self._pending_poll_confirmed: List[ImageEntry] = []
        self._pending_poll_unconfirmed: List[ImageEntry] = []
        self._pending_poll_warnings: List[str] = []
        # Set by _restore_saved_session when it has something to say, so
        # _show_persisted_quota_pause (which runs right after, still inside
        # __init__) can join onto it instead of silently overwriting it -
        # a crash-recovery restore and a persisted quota pause can both be
        # true at once, and losing either one is a real loss (DAN-485).
        self._session_restore_status: Optional[str] = None

        self._build_menu()
        self._build_central_widget()
        self._build_status_bar()
        # Built here, subscribed only while a search is running - see
        # _launch_search_worker. An alert is about an engine that cannot
        # run, so there is nothing to say when nothing is searching, and a
        # channel that is open for the window's whole life is one a
        # background task can put an unexpected modal dialog through.
        self.engine_alert.connect(self._on_engine_alert)
        self._engine_alert_listener = _engine_alert_forwarder(self)
        # After the central widget: these bind to the image table, which
        # does not exist until it is built.
        self._review_shortcuts: List[QShortcut] = []
        review_shortcuts.install(self)
        self.setAcceptDrops(True)

        if self.settings.restore_session_on_start:
            self._restore_saved_session()

        self._autosaver.apply_settings(self.settings)
        self._show_persisted_quota_pause()

        # The embedded MCP server (DAN-707) - lets a local AI drive a
        # review pass through the same actions the Shortcuts tab exposes.
        # Built unconditionally (cheap: it does nothing but hold a
        # reference to this window until asked to start) and started
        # only if Settings > MCP has it enabled, since that's where a
        # blank token or a missing optional `mcp` package is reported
        # rather than raised.
        self._mcp_handlers = McpToolHandlers(self)
        self._mcp_server = McpServerController(self._mcp_handlers)
        if not self._mcp_server.start(self.settings.mcp):
            if self.settings.mcp.enabled and self._mcp_server.last_error:
                log.warning("MCP server did not start: %s", self._mcp_server.last_error)

    def _show_persisted_quota_pause(self):
        """Surfaces a quota pause from a PREVIOUS visit, not just the
        moment it happened - persisting it only matters if something
        later reads it back (DAN-486)."""
        state = get_quota_pause_state()
        if state is None:
            return
        try:
            reset_at = datetime.fromisoformat(state.reset_at)
        except ValueError:
            clear_quota_pause()
            return
        if reset_at <= datetime.now(timezone.utc):
            # The allowance has already come back since this was written -
            # stale, not still true.
            clear_quota_pause()
            return
        quota_text = describe_quota_pause(state)
        if self._session_restore_status:
            self.status_label.setText(f"{self._session_restore_status} · {quota_text}")
        else:
            self.status_label.setText(quota_text)

    # ------------------------------------------------------------------
    # Menu
    # ------------------------------------------------------------------
    def _build_menu(self):
        menubar = self.menuBar()

        files_menu = menubar.addMenu("&Files")
        add_files_action = QAction("Add Files…", self)
        add_files_action.triggered.connect(self.action_add_files)
        files_menu.addAction(add_files_action)

        add_folder_action = QAction("Add Folder…", self)
        add_folder_action.triggered.connect(self.action_add_folder)
        files_menu.addAction(add_folder_action)

        query_hydrus_action = QAction("Query Hydrus…", self)
        query_hydrus_action.triggered.connect(self.action_query_hydrus)
        files_menu.addAction(query_hydrus_action)

        # Shared with the Hydrus menu below - one QAction added twice, so the
        # label, tooltip and handler cannot drift apart between the two homes.
        self.reconcile_action = QAction("Re-check Queued Imports", self)
        self.reconcile_action.setToolTip(RECHECK_QUEUED_IMPORTS_TOOLTIP)
        self.reconcile_action.triggered.connect(self.action_reconcile_with_hydrus)
        files_menu.addAction(self.reconcile_action)

        pawchive_index_action = QAction("Pawchive Index…", self)
        pawchive_index_action.setToolTip(
            "Choose pawchive artists to index, so resized or re-saved copies of their "
            "images are found too - the exact lookup only finds identical files."
        )
        pawchive_index_action.triggered.connect(self.action_pawchive_index)
        files_menu.addAction(pawchive_index_action)

        files_menu.addSeparator()

        save_session_action = QAction("Save Session As…", self)
        save_session_action.setShortcut("Ctrl+S")
        save_session_action.setToolTip(
            "Writes the current list - files, matches, tags and what's been sent to Hydrus - "
            "to a file you choose, so you can keep several named sessions and come back to any "
            "of them. Separate from the automatic session, which this never touches."
        )
        save_session_action.triggered.connect(self.action_save_session_as)
        files_menu.addAction(save_session_action)

        open_session_action = QAction("Open Session…", self)
        open_session_action.setShortcut("Ctrl+O")
        open_session_action.setToolTip(
            "Loads a previously saved session file, replacing the current list."
        )
        open_session_action.triggered.connect(self.action_open_session)
        files_menu.addAction(open_session_action)

        files_menu.addSeparator()

        export_action = QAction("Export Results…", self)
        export_action.setToolTip(
            "Writes every row in the list out as a CSV spreadsheet or as JSON - "
            "filename, status, matched URL, site, similarity, dimensions, tags, and "
            "what has been sent or reviewed.\n\n"
            "For counting results, scripting against them, or diffing one run against "
            "a later one. Exports the whole list; to export a selection, use the "
            "right-click menu.\n\n"
            "CSV joins each image's tags into one cell, which is readable in a "
            "spreadsheet but cannot be taken apart again if a tag contains a comma. "
            "JSON keeps them as a real list and is the lossless form."
        )
        export_action.triggered.connect(lambda: self.action_export_results())
        files_menu.addAction(export_action)

        write_tags_action = QAction("Write Tag Files…", self)
        write_tags_action.setToolTip(
            "Writes each image's tags to a text file next to the image itself, one "
            "tag per line - the form every other tagger, training script and "
            "Hydrus's own sidecar importer reads.\n\n"
            "Unlike Export Results, which describes the run in one file, this puts "
            "the tags where the pictures are, so a folder stays tagged after it "
            "leaves this app.\n\n"
            "The only thing here that writes into your own picture folders. It shows "
            "you what it is about to write and where, and never replaces a text file "
            "that is already there without asking.\n\n"
            "Covers the whole list; for a selection, use the right-click menu."
        )
        write_tags_action.triggered.connect(lambda: self.action_write_tag_files())
        files_menu.addAction(write_tags_action)

        files_menu.addSeparator()

        open_log_action = QAction("Open Matched URLs Log", self)
        open_log_action.triggered.connect(self.action_open_log)
        files_menu.addAction(open_log_action)

        files_menu.addSeparator()
        quit_action = QAction("Exit", self)
        quit_action.triggered.connect(self.close)
        files_menu.addAction(quit_action)

        self._build_hydrus_menu(menubar)
        self._build_sites_menu(menubar)

        settings_menu = menubar.addMenu("&Settings")
        general_action = QAction("Preferences…", self)
        general_action.triggered.connect(self.action_open_settings)
        settings_menu.addAction(general_action)
        conditions_action = QAction("Edit Match Conditions…", self)
        conditions_action.triggered.connect(self.action_edit_conditions)
        settings_menu.addAction(conditions_action)

        settings_menu.addSeparator()
        theme_menu = settings_menu.addMenu("Theme")
        self._theme_actions = {}
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)
        for mode, label in (("system", "Follow the desktop"),
                            ("dark", "Dark"),
                            ("light", "Light")):
            action = QAction(label, self)
            action.setCheckable(True)
            action.setChecked(self.settings.theme == mode)
            action.triggered.connect(lambda _checked, m=mode: self.action_set_theme(m))
            theme_group.addAction(action)
            theme_menu.addAction(action)
            self._theme_actions[mode] = action

        help_menu = menubar.addMenu("&Help")
        view_logs_action = QAction("View Logs…", self)
        view_logs_action.triggered.connect(self.action_view_logs)
        help_menu.addAction(view_logs_action)

        parser_health_action = QAction("Parser Health…", self)
        parser_health_action.setToolTip(
            "Whether each site's tag parsing still works. A parser that breaks does so "
            "silently - matches keep appearing, just without tags - so this is where to "
            "look when one site's results seem thin. Can also check every site live."
        )
        parser_health_action.triggered.connect(self.action_parser_health)
        help_menu.addAction(parser_health_action)

        self.clear_cache_action = QAction("Clear Search Cache…", self)
        self.clear_cache_action.setToolTip(
            "Deletes all saved search results. Future searches for images you've "
            "already searched before will hit IQDB/SauceNAO fresh again."
        )
        self.clear_cache_action.triggered.connect(self.action_clear_search_cache)
        help_menu.addAction(self.clear_cache_action)

        clear_session_action = QAction("Clear List and Saved Session…", self)
        clear_session_action.setToolTip(
            "Empties the current list and discards the session saved on exit. "
            "Only affects this app's list - your files and anything already sent "
            "to Hydrus are untouched."
        )
        clear_session_action.triggered.connect(self.action_clear_session)
        help_menu.addAction(clear_session_action)
        help_menu.aboutToShow.connect(self._update_clear_cache_action_label)

    def _build_hydrus_menu(self, menubar):
        """The two ways to get a result into Hydrus, plus the queued-import
        recheck, gathered under one menu - the recheck keeps its Files entry
        too, this just gives the same action a second, more discoverable
        home. Must run after _build_menu creates self.reconcile_action."""
        hydrus_menu = menubar.addMenu("&Hydrus")

        send_upload_action = QAction("Send File + URL + Tags to Hydrus (upload)", self)
        send_upload_action.setToolTip(
            "Uploads the local file, associates the matched URL with it, "
            "and adds the tag list."
        )
        send_upload_action.triggered.connect(self.action_send_to_hydrus)
        hydrus_menu.addAction(send_upload_action)

        send_url_action = QAction("Send URL to Hydrus's URL Importer…", self)
        send_url_action.setToolTip(
            "Hands the matched URL to Hydrus's own downloader, which fetches "
            "and imports the file itself - like pasting it into Hydrus's URL box."
        )
        send_url_action.triggered.connect(self.action_import_url_to_hydrus)
        hydrus_menu.addAction(send_url_action)

        hydrus_menu.addSeparator()

        # The same QAction the Files menu holds, not a copy - Qt is happy for
        # one action to live in two menus, and it keeps the five-line tooltip
        # in one place.
        hydrus_menu.addAction(self.reconcile_action)

    def _build_sites_menu(self, menubar):
        """Sites menu: which originating sites (Danbooru, Gelbooru, etc.)
        to keep as candidate matches - applies to both IQDB and SauceNAO
        results alike, filtered after searching by the matched URL's host."""
        sites_menu = menubar.addMenu("Si&tes")
        self.site_actions: Dict[str, QAction] = {}

        select_all_action = QAction("Select All", self)
        select_all_action.triggered.connect(lambda: self._set_all_sites(True))
        sites_menu.addAction(select_all_action)

        select_none_action = QAction("Select None", self)
        select_none_action.triggered.connect(lambda: self._set_all_sites(False))
        sites_menu.addAction(select_none_action)

        sites_menu.addSeparator()

        for site in ALL_SITE_OPTIONS:
            action = QAction(site, self)
            action.setCheckable(True)
            action.setChecked(site in self.settings.enabled_sites)
            action.toggled.connect(lambda checked, s=site: self._on_site_toggled(s, checked))
            sites_menu.addAction(action)
            self.site_actions[site] = action

    def _on_site_toggled(self, site: str, checked: bool):
        enabled = set(self.settings.enabled_sites)
        if checked:
            enabled.add(site)
        else:
            enabled.discard(site)
        self.settings.enabled_sites = [s for s in ALL_SITE_OPTIONS if s in enabled]
        log.debug("Site filter: %s -> %s (now enabled: %s)", site, checked, self.settings.enabled_sites)
        self.settings.save()

    def _set_all_sites(self, checked: bool):
        for action in self.site_actions.values():
            action.blockSignals(True)
            action.setChecked(checked)
            action.blockSignals(False)
        self.settings.enabled_sites = list(ALL_SITE_OPTIONS) if checked else []
        log.info("Site filter: %s all sites", "enabled" if checked else "disabled")
        self.settings.save()

    # ------------------------------------------------------------------
    # Central widget: image table + tag panel
    # ------------------------------------------------------------------
    def _build_central_widget(self):
        self._build_shell({
            "queue": self._build_queue_page(),
            "review": self._build_review_page(),
            "activity": self._build_activity_page(),
        })

    def _build_queue_page(self):
        """The list, and nothing else.

        The tags and the two previews used to share this page, which left
        the list about a third of the window - for the one view whose
        whole job is showing a lot of rows at once. They are in Review
        now, so the list finally gets the width.
        """
        # Unframed: the filter band and the table each draw their own
        # hairline, so a line round all of it would be a box in a box.
        card, card_layout = widgets.card(framed=False)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.addWidget(widgets.screen_title("Queue."))

        # Hidden until _restore_saved_session finds an unclean-shutdown
        # restore with something in it (DAN-660) - built here, not there,
        # because the Queue page itself doesn't exist yet at that point.
        self.run_banner, banner_parts = widgets.run_banner()
        self.run_banner.setVisible(False)
        self._run_banner_body = banner_parts['body']
        banner_parts['resume_button'].clicked.connect(self.action_start_search)
        banner_parts['crash_log_button'].clicked.connect(self._open_crash_log)
        banner_parts['discard_label'].clicked.connect(self.action_clear_session)
        card_layout.addWidget(self.run_banner)

        # The model is built before anything that reads it - the filter
        # bar sets its initial state from the model's counts, so it has to
        # exist first.
        self.table_model = ImageTableModel(
            self.entries, self._get_row_thumbnail, self,
            mode_getter=lambda: theme.resolve_mode(self.settings.theme),
        )

        table_panel = QWidget()
        table_layout = QVBoxLayout(table_panel)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.setSpacing(12)
        self.filter_bar = FilterBar(lambda: self.entries)
        self.filter_bar.changed.connect(self._apply_filter)
        self.filter_bar.rebuild_menus()
        self._refresh_filter_ui()
        table_layout.addWidget(self.filter_bar)

        self.table = QTableView()
        self.table.setModel(self.table_model)
        header = self.table.horizontalHeader()
        # All columns freely resizable and reorderable by dragging - no
        # column is locked to Fixed/Stretch, so the user has full control.
        for col in range(len(COLUMNS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
        header.setSectionsMovable(True)
        header.setMinimumSectionSize(24)  # a column can be shrunk small, but never dragged to nothing
        self.table.setColumnWidth(0, THUMB_COLUMN_SIZE + 20)
        # The last column takes up whatever the others leave, so the
        # default layout fills the width with no horizontal scrollbar and
        # no dead strip after "Upscale" (Q-04). The columns before it stay
        # Interactive: still user-resizable and movable (B-11).
        header.setStretchLastSection(True)
        # Q-03: UPPERCASE mono, left-aligned, like every other label in
        # the mockup's voice. The caps come from the model (headerData's
        # FontRole), so the section text itself stays "Size diff.".
        header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        # Lazy thumbnails follow the viewport, so anything that changes
        # WHICH rows are on screen has to trigger a re-check.
        self.table.verticalScrollBar().valueChanged.connect(
            lambda _v: self._schedule_viewport_thumbnails("scroll")
        )
        self.table.verticalScrollBar().rangeChanged.connect(
            lambda _a, _b: self._schedule_viewport_thumbnails("scrollbar range changed")
        )
                # Wide enough for the longest chip plus its padding. At Qt's
        # default section width "Unsearched" elides to "Unsearc…",
        # which is the first thing a new list is full of.
        self.table.setColumnWidth(COL_STATUS, 130)
        self.table.setIconSize(QSize(THUMB_COLUMN_SIZE, THUMB_COLUMN_SIZE))
        # No row numbers (Q-05): nothing here reads a click on the
        # vertical header, and the sort/selection state is in the rows.
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(QUEUE_ROW_HEIGHT)
        self.table.setShowGrid(False)  # no cell lines through a selected row (Q-07)
        self.table.setAlternatingRowColors(True)  # zebra; the tone is theme.py's
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # QTableView has no itemSelectionChanged (that is a widget-item
        # signal); the selection model carries the equivalent.
        self.table.selectionModel().selectionChanged.connect(
            lambda _sel, _desel: self._on_selection_changed()
        )
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_table_context_menu)
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(False)
        self.table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        # Status and Sent draw as chips rather than as a filled cell. The
        # mode is read through a callable so a theme switch repaints in
        # the new palette without anything rebuilding the delegate.
        self.table.setItemDelegate(RuledRowDelegate(
            lambda: theme.resolve_mode(self.settings.theme), self.table))
        self._chip_delegate = ChipDelegate(
            lambda: theme.resolve_mode(self.settings.theme), self.table)
        self.table.setItemDelegateForColumn(COL_STATUS, self._chip_delegate)
        self.table.setItemDelegateForColumn(COL_SENT, self._chip_delegate)
        # Similarity too, but only for the rows whose number is the
        # engine's ranking rather than a measurement - the model returns
        # no chip key for a measured score, so those stay plain text.
        self.table.setItemDelegateForColumn(COL_SIMILARITY, self._chip_delegate)

        self._restore_table_header_state()
        # A saved layout is the user's; without one the default widths are
        # worked out from the viewport once it has a real size.
        self._default_widths_pending = not self.settings.table_header_state
        self.table.viewport().installEventFilter(self)
        header.viewport().installEventFilter(self)
        table_layout.addWidget(self.table)

        # An empty list shows the drop zone instead of an empty grid.
        # Deliberately keyed on `entries` and not on how many rows are
        # VISIBLE: a filter that currently matches nothing still has a
        # list behind it, and replacing it with "drop images here" would
        # say the images are gone when they are only hidden. The filter
        # bar's own count is what explains that case.
        self.queue_stack = QStackedWidget()
        self.queue_stack.addWidget(table_panel)
        self.queue_stack.addWidget(self._build_drop_zone())
        card_layout.addWidget(self.queue_stack)

        for signal in (self.table_model.modelReset,
                       self.table_model.rowsInserted,
                       self.table_model.rowsRemoved):
            signal.connect(self._refresh_queue_empty_state)
        self._refresh_queue_empty_state()
        return card

    def _build_drop_zone(self):
        """What an empty Queue says instead of showing an empty grid.

        The three ways in were all in the Files menu, which is a fine
        place for them and a poor place to *discover* them: a first
        launch showed a blank table and a menu bar.
        """
        panel = QFrame()
        panel.setObjectName("DropZone")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(32, 24, 32, 24)
        self.queue_section_label = widgets.section_label("01 // Queue")
        layout.addWidget(self.queue_section_label, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)  # index 1: the weight above the block (E-03)

        # E-01: the mockup's `.drop-title` - the screen-title face at the
        # display size, with the full stop.
        title = widgets.display_title("Drop images here.")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        blurb = widgets.hint(
            "Or add them from a folder, or pull them out of a running Hydrus client. "
            "Anything added here is searched on IQDB and SauceNAO, and what comes back "
            "is yours to review before any of it reaches Hydrus."
        )
        blurb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Fixed width inside a stretch row, rather than a maximum width and
        # an alignment flag. A word-wrapped QLabel handed an alignment in a
        # box layout reports the height its text would need UNWRAPPED, so
        # the layout gives it one line's worth and the rest draws over
        # whatever is above it.
        blurb.setFixedWidth(520)
        blurb_row = QHBoxLayout()
        blurb_row.addStretch(1)
        blurb_row.addWidget(blurb)
        blurb_row.addStretch(1)
        layout.addSpacing(6)
        layout.addLayout(blurb_row)
        layout.addSpacing(18)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(widgets.pill_button(
            "Add files…", self.action_add_files, primary=True))
        buttons.addWidget(widgets.pill_button("Add folder…", self.action_add_folder))
        buttons.addWidget(widgets.pill_button(
            "Query Hydrus…", self.action_query_hydrus))
        buttons.addStretch(1)
        layout.addLayout(buttons)

        # E-03: the block is top-weighted, not centred. The mockup puts the
        # headline's top at ~37% of the window, which is 1 part of the free
        # height above it to DROP_ZONE_BELOW_WEIGHT below. Stretch factors
        # (not a fixed gap) so it holds as the window is resized.
        layout.addStretch(DROP_ZONE_BELOW_WEIGHT)
        # E-02: the dossier frame's corner brackets (P10 primitive).
        widgets.Brackets(panel)
        return panel

    def _refresh_queue_empty_state(self, *_args):
        stack = getattr(self, "queue_stack", None)
        if stack is None:
            return
        stack.setCurrentIndex(0 if self.entries else 1)
        # The Empty page has its own watermark (G-07: 空 where the list is
        # 力); only restyle it while Queue is the page on show.
        if self.current_mode() == "queue":
            self.set_ghost(self.ghost_glyph_for("queue"))

    def ghost_glyph_for(self, key):
        stack = getattr(self, "queue_stack", None)
        if key == "queue" and stack is not None and stack.currentIndex() == 1:
            return EMPTY_GHOST_GLYPH
        return super().ghost_glyph_for(key)



    def _apply_filter(self):
        """Applies the current filter and puts the selection back.

        A model reset drops the selection, and losing it on every
        keystroke while typing in the filter box would make the box
        unusable - so whatever is still visible is reselected.

        This is the half that stays here: the FilterBar knows what is
        ticked, and this knows what to do about it - the table, the
        selection and the thumbnails are all MainWindow's.
        """
        selected = self._selected_entries()
        self.table_model.set_filter(self.filter_bar.current_filter())
        self._reselect(selected)
        self._refresh_filter_ui()
        self._schedule_viewport_thumbnails("filter changed")

    def _refresh_filter_ui(self):
        self.filter_bar.refresh(
            self.table_model.visible_count(),
            self.table_model.total_count(),
            self.table_model.filter.is_active(),
        )

    def _selected_entries(self) -> List[ImageEntry]:
        return self.table_model.entries_at(
            i.row() for i in self.table.selectionModel().selectedRows()
        )

    def _reselect(self, entries: List[ImageEntry]):
        """Reselects the given entries by identity, skipping any the
        filter now hides."""
        if not entries:
            return
        rows = [r for r in (self.table_model.row_of(e) for e in entries) if r is not None]
        if not rows:
            self.table.clearSelection()
            self._on_selection_changed()
            return
        selection = QItemSelection()
        last_col = self.table_model.columnCount() - 1
        for row in rows:
            selection.select(self.table_model.index(row, 0),
                             self.table_model.index(row, last_col))
        self.table.selectionModel().select(
            selection,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        # A reset drops the current index too, and the arrow keys start
        # from it - left unset they would carry on from the top.
        self.table.selectionModel().setCurrentIndex(
            self.table_model.index(rows[0], 0),
            QItemSelectionModel.SelectionFlag.NoUpdate,
        )

    def _update_preview(self, entry: Optional[ImageEntry]):
        if entry is None:
            self.local_preview.setPixmap(QPixmap())
            self.local_preview.setText("No image selected")
            self.local_info_label.setText("")
            self.matched_preview.setPixmap(QPixmap())
            self.matched_preview.setText("No match yet")
            self.matched_info_label.setText("")
            self.set_comparison_readout(ComparisonReadout())
            self.matched_caption.setText("Matched image:")
            self._populate_candidate_combo(None)
            return

        local_pix = self._get_local_pixmap(entry)
        if local_pix is None:
            self.local_preview.setText("Could not load image")
        else:
            self.local_preview.setPixmap(local_pix)
        self.local_info_label.setText(local_image_info_text(entry.path, Image.open))

        self._populate_candidate_combo(entry)

        candidate = entry.selected_candidate
        self.set_comparison_readout(comparison_readout(entry, candidate))
        full_resolution = self.full_resolution_match(entry)
        if full_resolution is not None:
            # Already fetched by Wipe/Differences - sharper than the
            # sample, and redrawing this panel must not swap it back.
            self.matched_preview.setPixmap(full_resolution)
            self.matched_caption.setText(matched_caption(entry))
            self.matched_info_label.setText(matched_image_info_text(candidate))
        elif entry.matched_thumb_bytes:
            matched_pix = QPixmap()
            matched_pix.loadFromData(entry.matched_thumb_bytes)
            if matched_pix.isNull():
                self.matched_preview.setPixmap(QPixmap())
                self.matched_preview.setText("Match found but thumbnail failed to load")
            else:
                self.matched_preview.setPixmap(matched_pix)
            self.matched_caption.setText(matched_caption(entry))
            self.matched_info_label.setText(matched_image_info_text(candidate))
        elif entry.candidates:
            self.matched_preview.setPixmap(QPixmap())
            self.matched_preview.setText("Loading picture…")
            self.matched_caption.setText("Matched image:")
            self.matched_info_label.setText(matched_image_info_text(candidate))
        else:
            self.matched_preview.setPixmap(QPixmap())
            self.matched_preview.setText(no_candidate_text(entry))
            self.matched_caption.setText("Matched image:")
            self.matched_info_label.setText("")

        # Review shows the same entry from the other side of the window.
        self.refresh_review()

    def _on_mode_changed(self, key):
        """Whichever mode just came to the front gets brought up to date.

        The Queue page is always current - it is driven by the model - but
        Review describes one selected entry, and selections change while
        it is out of sight.
        """
        if key == "review":
            self.refresh_review()
            # The mode button just clicked keeps focus otherwise, and it
            # sits outside the page the review keys are bound to.
            self.review_page.setFocus()
        self.set_activity_live(key == "activity")

    def _populate_candidate_combo(self, entry: Optional[ImageEntry]):
        """(Re)fills the dropdown with every site found for this image,
        without triggering a spurious fetch - we block signals while the
        list is rebuilt, then select the entry's currently-active candidate."""
        self.candidate_combo.blockSignals(True)
        self.candidate_combo.clear()

        if not entry or not entry.candidates:
            self.candidate_combo.setEnabled(False)
            self.candidate_combo.setToolTip("")
            self.candidate_combo.blockSignals(False)
            return

        for i, c in enumerate(entry.candidates):
            site = c.source_name or _host_from_url(c.url) or c.engine
            # The dropdown is where candidates get compared against one
            # another, so it is the one place a ranking sitting next to a
            # real score is most likely to be read as the same kind of
            # number. Same "~" the table uses.
            score = similarity_label(c.similarity, c.similarity_measured)
            self.candidate_combo.addItem(f"{site} — {score} ({c.engine}) — {c.url}")
            self.candidate_combo.setItemData(i, c.url, Qt.ItemDataRole.ToolTipRole)

        self.candidate_combo.setEnabled(True)
        self.candidate_combo.setCurrentIndex(entry.selected_candidate_index)
        self._sync_candidate_combo_tooltip(entry)
        self.candidate_combo.blockSignals(False)

    def _sync_candidate_combo_tooltip(self, entry: Optional[ImageEntry]):
        """Shows the currently-selected candidate's full URL as the closed
        combo box's own tooltip - the box itself elides long text when
        collapsed, so hovering it is how you see the whole URL without
        having to open the dropdown."""
        candidate = entry.selected_candidate if entry else None
        self.candidate_combo.setToolTip(candidate.url if candidate else "")

    def _on_candidate_combo_changed(self, index: int):
        entry = self._current_entry()
        if not entry or index < 0 or index >= len(entry.candidates):
            return

        entry.select_candidate(index)
        self._sync_candidate_combo_tooltip(entry)
        self._refresh_entry_row(entry)
        self._update_preview(entry)
        self._refresh_tag_list(entry)

        self._ensure_candidate_loaded(entry, index)

    def _ensure_candidate_loaded(self, entry: ImageEntry, index: int):
        """Fetches the selected candidate's picture and tags if they're
        not already in memory.

        Needed from BOTH the dropdown and plain row selection. A restored
        session is the reason: thumbnail bytes can't go in a JSON file,
        so every candidate comes back from disk with thumb_bytes=None.
        Only the dropdown used to trigger a fetch, so a restored match
        showed no picture until you switched to another site and back -
        which quietly did the fetch as a side effect.
        """
        if not (0 <= index < len(entry.candidates)):
            return
        candidate = entry.candidates[index]
        if candidate.thumb_bytes is not None and candidate.booru_tags_fetched:
            return  # already have everything for this candidate, no fetch needed

        # Let a fast prior fetch finish rather than pile up threads - but never
        # destroy it if it's still going (that aborts the process outright).
        self._workers.retire(self.candidate_worker)

        log.debug(
            "Fetching details for the selected candidate %d of %s (thumb=%s, tags_fetched=%s)",
            index, entry.filename, candidate.thumb_bytes is not None, candidate.booru_tags_fetched,
        )
        self.status_label.setText("Fetching picture and tags for the selected site…")
        self.candidate_worker = CandidateFetchWorker(entry, index, self.settings)
        self.candidate_worker.done.connect(self._on_candidate_fetched)
        self.candidate_worker.wait_countdown.connect(self._on_wait_countdown)
        self.candidate_worker.start()

    def _run_dialog(self, dialog):
        """Runs a modal dialog and destroys it as soon as it closes.

        Every dialog here is parented to the window so it centres on it,
        which also means Qt keeps it alive until the window itself dies.
        Nothing ever closed them, so they accumulated for the whole
        session - and a comparison view is not a cheap thing to keep:
        it holds the local file and the match at FULL resolution, tens of
        megabytes a pair, for every image ever compared.

        Destroying them promptly also keeps their teardown inside the
        normal event loop, while the platform's native-dialog helper is
        still around to be told they are hiding. Left to be destroyed
        much later, a dialog can reach for a helper that has gone - which
        segfaults inside QMessageBox's destructor, with no Python
        traceback, because the fault is a call through a dead pointer.

        deleteLater (not an immediate delete) is what makes this safe to
        call around exec(): the object survives until control returns to
        the event loop, so reading a result or a field off the dialog on
        the next line still works.
        """
        try:
            return dialog.exec()
        finally:
            dialog.deleteLater()

    def _on_candidate_fetched(self, entry: ImageEntry, index: int):
        # The lazy fetch may have just discovered this candidate's source
        # is gone (a 404/410) - it was never checked during the search
        # itself, since only the first few candidates get fetched there.
        # Don't leave the user looking at a link already known to be dead.
        if (
            self.settings.drop_dead_matches
            and 0 <= index < len(entry.candidates)
            and entry.candidates[index].remote_available is False
        ):
            dead = entry.candidates[index]
            log.info(
                "Selected match for %s is gone (%s) - dropping it and moving to the next",
                entry.filename, dead.url,
            )
            self._remove_dead_candidates(entry)
            self.status_label.setText("That match is no longer available - it's been removed")
            return

        if entry.selected_candidate_index == index:
            entry.select_candidate(index)  # re-sync now that thumb_bytes/booru_tags are filled in
        current = self._current_entry()
        if current is entry:
            self._update_preview(entry)
            self._refresh_tag_list(entry)
        self._refresh_entry_row(entry)
        self.status_label.setText("Ready")

    def _refresh_tag_list(self, entry: Optional[ImageEntry]):
        self.tag_list.blockSignals(True)
        self.tag_list.clear()
        if entry:
            enabled = {TagSource(n) for n in self.settings.enabled_tag_sources if n in [s.value for s in TagSource]}
            for tag in entry.visible_tags(list(enabled)) if enabled else entry.tags:
                if self._tags_editable:
                    # Plain "namespace:name" while editable - no "[Source]"
                    # prefix, so there's nothing to accidentally mangle and
                    # what's shown is exactly what gets parsed back.
                    item = QListWidgetItem(tag.display)
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
                else:
                    item = QListWidgetItem(f"[{tag.source.value}] {tag.display}")
                color = get_tag_color(tag, self.settings)
                if color:
                    item.setForeground(QColor(color))
                item.setData(Qt.ItemDataRole.UserRole, tag)
                self.tag_list.addItem(item)
        self.tag_list.blockSignals(False)

    def _on_edit_tags_toggled(self, checked: bool):
        self._tags_editable = checked
        self.edit_tags_btn.setText("Editing Tags (click to stop)" if checked else "Edit Tags")
        self._refresh_tag_list(self._current_entry())

    def _on_tag_item_edited(self, item: QListWidgetItem):
        """Commits an inline tag edit. Clearing a tag's text entirely
        deletes it; otherwise the tag is renamed in place (namespace:name
        or just name, with the configured namespace remap rules applied),
        keeping its original source. If the result collides with another
        tag the entry already has, the older duplicate is dropped rather
        than leaving two identical tags."""
        entry = self._current_entry()
        if entry is None:
            return
        tag = item.data(Qt.ItemDataRole.UserRole)
        if tag is None:
            return

        old_display = tag.display
        result = apply_inline_edit(entry.tags, tag, item.text(), self.settings)
        entry.tags = result.tags
        if result.action == "removed":
            log.info("Removed tag %s from %s (cleared via inline edit)", old_display, entry.filename)
        else:
            if result.dropped_duplicate:
                log.debug("Inline edit of %s created a duplicate of an existing tag; "
                          "removed the older one", entry.filename)
            log.info("Renamed tag %s -> %s on %s", old_display, tag.display, entry.filename)

        self._refresh_tag_list(entry)
        self._refresh_table()

    def _show_matched_preview_menu(self, pos):
        entry = self._current_entry()
        if not entry or not entry.matched_url:
            return
        if not entry.matched_url.startswith(("http://", "https://")):
            message.warning(
                self, "Open image URL",
                "This match doesn't have a valid web address to open.",
            )
            return

        menu = QMenu(self)
        open_action = menu.addAction("Open image URL in browser")
        copy_action = menu.addAction("Copy image URL")
        chosen = menu.exec(self.matched_preview.mapToGlobal(pos))

        if chosen == open_action:
            self.action_open_matched_url()
        elif chosen == copy_action:
            QApplication.clipboard().setText(entry.matched_url)

    # ------------------------------------------------------------------
    # Drag & drop
    # ------------------------------------------------------------------
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls()]
        files = []
        for p in paths:
            if os.path.isdir(p):
                files.extend(scan_folder(p))
            elif os.path.splitext(p)[1].lower() in IMAGE_EXTENSIONS:
                files.append(p)
        self._import_files(files)

    # ------------------------------------------------------------------
    # File actions
    # ------------------------------------------------------------------
    def action_add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add images", "", FILE_DIALOG_FILTER
        )
        if paths:
            self._import_files(paths)

    def action_add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Add folder")
        if folder:
            self._import_files(scan_folder(folder))

    def _restore_saved_session(self):
        """Reloads the working list from the previous run.

        The point is skipping the re-hash: for a large batch on a network
        share that is minutes of work for files the app has already seen.
        Thumbnails are not persisted (binary, and they would dominate the
        file size) so they regenerate in the background, which does not
        block anything."""
        # Taken before load_session(), which doesn't say WHY it came back
        # empty - this is what tells "never saved anything" apart from
        # "a session exists but is empty/corrupt/unreadable" (DAN-487).
        had_session_file = has_saved_session()
        entries = load_session()
        if not entries:
            if had_session_file and self._unclean_shutdown:
                self.status_label.setText(
                    "Run interrupted — nothing survived from the last session"
                )
            return
        if session_db.SESSION_DB.exists():
            # Taken before anything touches the entries: whatever changes
            # from here on bumps a revision and is written by the next
            # autosave, and nothing else needs to be.
            self._autosaver.mark_saved(session_db.revisions_of(entries))
        self._register_new_entries(entries)
        self.entries.extend(entries)
        self._refresh_table()
        if self._unclean_shutdown:
            # The banner (DAN-660) replaces this status-bar line rather
            # than joining it - two notices for the same event is noise,
            # and this is the exact pair that collided once already
            # (commit 31478d5). _session_restore_status stays unset, so
            # _show_persisted_quota_pause (right after, in __init__)
            # shows just the quota text on its own if a pause is also
            # pending, instead of re-joining text the banner now owns.
            self._show_run_banner(entries)
        else:
            self._session_restore_status = (
                f"Restored {len(entries)} image(s) from your last session"
            )
            self.status_label.setText(self._session_restore_status)
        self._start_missing_file_check(entries)
        # Anything that outlived its import poll last run is still showing
        # Queued. Settle it now rather than leaving the Sent column wrong
        # for the rest of the session - it costs nothing when nothing is
        # waiting, since the worker then never contacts Hydrus at all.
        self._start_hydrus_reconcile(quiet=True)
        if self.settings.lazy_thumbnails:
            lazylog.info(
                "restored %d entry(s) - deferring thumbnails to the viewport pass", len(entries),
            )
            self._schedule_viewport_thumbnails("session restored")
        else:
            self._start_thumbnail_generation(entries)

    def _show_run_banner(self, entries: List[ImageEntry]):
        """Populates and shows the Queue page's crash-recovery banner
        (DAN-660) for an unclean-shutdown restore that brought something
        back. Per-category counts read the same per-row status the table
        already tracks - no new counting logic, matching
        _refresh_sent_count_label's own sent/queued split plus the
        unsearched count action_start_search already computes."""
        sent = sum(1 for e in entries if e.sent_to_hydrus and e.hydrus_import_confirmed)
        queued = sum(1 for e in entries if e.sent_to_hydrus and not e.hydrus_import_confirmed)
        unsearched = sum(1 for e in entries if e.status == MatchStatus.NOT_SEARCHED)
        self._run_banner_body.setText(
            f"{sent} sent, {queued} still queued, {unsearched} not yet searched"
        )
        self.run_banner.setVisible(True)

    def action_reconcile_with_hydrus(self):
        """Files > Re-check Queued Imports, on request."""
        waiting = entries_awaiting_confirmation(self.entries)
        if not waiting:
            self.status_label.setText("Nothing is waiting on Hydrus - every sent file is confirmed")
            return
        self._start_hydrus_reconcile(quiet=False)

    def _start_hydrus_reconcile(self, quiet: bool = False):
        """Asks Hydrus to settle entries still showing Queued.

        `quiet` is for the automatic pass after a session restore: it
        should say something when it actually changed the list and stay
        out of the way otherwise, rather than announcing itself over the
        "Restored N image(s)" message on every launch.
        """
        waiting = entries_awaiting_confirmation(self.entries)
        if not waiting:
            return
        self._workers.retire(self.hydrus_reconcile_worker)
        self._reconcile_was_quiet = quiet
        if not quiet:
            self.status_label.setText(f"Asking Hydrus about {len(waiting)} queued import(s)…")
        self.hydrus_reconcile_worker = HydrusReconcileWorker(self.entries, self.settings)
        self.hydrus_reconcile_worker.done.connect(self._on_hydrus_reconciled)
        self.hydrus_reconcile_worker.start()

    def _on_hydrus_reconciled(self, result):
        if result.confirmed:
            # The Sent column and its running count both describe this.
            self._refresh_table()
            self._refresh_sent_count_label()
        if result.confirmed or not self._reconcile_was_quiet:
            self.status_label.setText(result.summary)
        log.info("Hydrus reconcile: %s", result.summary)

    def _start_missing_file_check(self, entries: List[ImageEntry]):
        """Checks in the background whether a restored session's files are
        still on disk.

        A session restores paths, and the most common reason one goes
        stale is the file having been deleted from Hydrus. Everything
        about the row still restores correctly - status, tags, matched
        URL, sent state - so nothing gives the loss away until something
        tries to read the file. Flagging it up front turns "this row
        errors when I finally get to it" into something visible
        immediately.
        """
        if not entries:
            return
        self._workers.retire(self.missing_file_worker, grace_ms=2000)

        self.missing_file_worker = MissingFileWorker(list(entries))
        self.missing_file_worker.finished_checking.connect(self._on_missing_files_checked)
        self.missing_file_worker.start()

    def _on_missing_files_checked(self, missing_paths: set):
        if self._closing:
            # The check was running when the window started closing. It was
            # queued (cross-thread signal) by the time retire() disconnected
            # it, or arrived before retire() got to it at all - either way,
            # a modal "Missing files" box belonging to a window on its way
            # out would hang an offscreen run and startle a quitting user
            # (DAN-128; DAN-127 hit the same shape from a different cause).
            return
        if not missing_paths:
            log.debug("Missing-file check: every file in the session is still on disk")
            return

        affected = 0
        for entry in self.entries:
            if entry.path in missing_paths:
                entry.file_missing = True
                affected += 1

        log.info("Flagged %d entry(s) whose file is no longer on disk", affected)
        self._refresh_table()
        self.status_label.setText(
            f"{affected} image(s) in this list are no longer on disk - see the File column"
        )

        # Said once, plainly, rather than left for the user to discover as
        # a string of failures later. The URL-importer note matters: those
        # rows are still useful even with the file gone.
        message.information(
            self, "Missing files",
            f"{affected} of {len(self.entries)} image(s) in this session are no longer on "
            "disk - most likely deleted from Hydrus since the session was saved.\n\n"
            "They're still listed, marked \"missing\" in the File column, and their tags and "
            "matched URLs are intact. Anything needing the file itself (searching, uploading, "
            "the upscale check) will fail for them, but \"Send URL to Hydrus's URL Importer\" "
            "still works - Hydrus re-downloads from the source and applies the tags.",
        )

    def action_save_session_as(self):
        """Writes the current list to a file the user names, so several
        sessions can be kept side by side. Deliberately separate from the
        automatic session - saving one here never disturbs the list that
        gets restored at startup."""
        if not self.entries:
            message.information(self, "Save Session", "There's nothing in the list to save.")
            return

        default_name = time.strftime("hatate-session-%Y%m%d-%H%M.json")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Session As", str(Path.home() / default_name),
            "Session files (*.json);;All files (*)",
        )
        if not path:
            return
        if not path.lower().endswith(".json"):
            # Saves the user a surprise later: the Open dialog filters on
            # .json, so an extension-less file would be invisible there.
            path += ".json"

        if save_session(self.entries, path=path):
            log.info("Saved session to %s (%d entries)", path, len(self.entries))
            self.status_label.setText(f"Saved {len(self.entries)} image(s) to {os.path.basename(path)}")
        else:
            message.warning(
                self, "Save Session",
                f"Could not write the session to:\n{path}\n\nSee Help > View Logs for details.",
            )

    def action_export_results(self, entries: Optional[List[ImageEntry]] = None):
        """Writes the results out as CSV or JSON.

        `entries` is the selection when called from the right-click menu,
        and the whole list from the Files menu. Each entry point says its
        own scope in its label, so this never has to ask.

        The format comes from the extension the dialog's filter puts on,
        which means picking "CSV" and picking "JSON" differ by one click
        and no second question.
        """
        rows = list(self.entries if entries is None else entries)
        if not rows:
            message.information(self, "Export Results", "There's nothing in the list to export.")
            return

        default_name = time.strftime("hatate-results-%Y%m%d-%H%M.csv")
        path, chosen_filter = QFileDialog.getSaveFileName(
            self, "Export Results", str(Path.home() / default_name),
            "CSV spreadsheet (*.csv);;JSON (*.json);;All files (*)",
        )
        if not path:
            return
        # The filter names the format; the extension only decides it for a
        # name typed by hand under "All files". Without the first half,
        # choosing JSON and accepting the offered .csv name would quietly
        # write a CSV.
        if "json" in (chosen_filter or "").lower():
            fmt = "json"
        elif "csv" in (chosen_filter or "").lower():
            fmt = "csv"
        else:
            fmt = results_export.format_for_path(path)
        if not path.lower().endswith(f".{fmt}"):
            path += f".{fmt}"

        try:
            # newline="" is what the csv module requires: without it the
            # writer's \r\n is translated again on some platforms and
            # every row ends up separated by a blank line.
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(results_export.render(rows, fmt, self.settings))
        except OSError as exc:
            log.error("Could not export results to %s: %s", path, exc)
            message.warning(
                self, "Export Results",
                f"Could not write the results to:\n{path}\n\n{exc}",
            )
            return

        log.info("Exported %d result(s) as %s to %s", len(rows), fmt.upper(), path)
        self.status_label.setText(
            f"Exported {len(rows)} image(s) to {os.path.basename(path)}"
        )

    def action_write_tag_files(self, entries: Optional[List[ImageEntry]] = None):
        """Writes each image's tags to a text file beside the image.

        `entries` is the selection when called from the right-click menu,
        and the whole list from the Files menu, same as the export.

        Everything below the first two lines is about the fact that this
        is the only action in the app that writes into the user's own
        picture folders. There is no file dialog to review, because the
        destination is not one place the user picks - it is wherever every
        one of possibly thousands of images happens to live. So the
        confirmation has to do the job the file dialog would have done:
        say how many files, in which folders, under what name, and how
        many text files already there are in the way.
        """
        rows = list(self.entries) if entries is None else self._live_entries(entries)
        if not rows:
            message.information(
                self, "Write Tag Files",
                "There's nothing in the list to write tag files for.",
            )
            return

        plan = sidecar.plan(rows, self.settings)
        if not plan.writes:
            message.information(self, "Write Tag Files", _nothing_to_write_text(plan))
            return

        overwrite = self._ask_about_writing_tag_files(plan)
        if overwrite is None:
            return

        result = sidecar.write(plan.writes, overwrite=overwrite)

        if result.failures:
            first = result.failures[0]
            message.warning(
                self, "Write Tag Files",
                f"Wrote {len(result.written)} tag file(s), but {len(result.failures)} "
                f"could not be written.\n\nFor example:\n{first[0]}\n{first[1]}\n\n"
                "See Help > View Logs for the rest.",
            )

        self.status_label.setText(_written_summary(plan, result))

    def _ask_about_writing_tag_files(self, plan) -> Optional[bool]:
        """Confirms the write and settles what to do about existing files.

        Returns True to overwrite, False to leave existing files alone, or
        None if the user cancelled - three answers, because "go ahead" is
        genuinely two different instructions when some of the targets are
        already there.

        The `sidecar_overwrite` setting decides whether that second
        question gets asked at all; `ask` is the default, and the other two
        values still get a confirmation, just with the outcome stated in it
        rather than chosen in it. Someone who set `overwrite` deliberately
        should not have to answer the same question on every folder, but
        they should still be told what is about to happen.
        """
        count = len(plan.writes)
        folders = plan.directories
        where = (str(folders[0]) if len(folders) == 1
                 else f"{len(folders)} folders, the first being {folders[0]}")
        detail = [
            f"Write {count} tag file(s), one per image, each holding that "
            "image's tags one per line?",
            "",
            f"Into: {where}",
            f"Named like: {plan.writes[0].path.name}",
        ]
        for skipped, why in (
            (plan.without_tags, "have no tags to write"),
            (plan.missing_files, "are missing from disk"),
        ):
            if skipped:
                detail.append(f"Skipping {len(skipped)} image(s) that {why}.")

        existing = plan.existing
        if not existing:
            detail.append("")
            detail.append("No text file of yours is in the way.")
            answered = message.question(self, "Write Tag Files", "\n".join(detail))
            return False if answered == QMessageBox.StandardButton.Yes else None

        policy = sidecar.overwrite_policy_of(self.settings)
        detail.append("")
        detail.append(f"{len(existing)} of them already exist, for example:")
        detail.append(str(existing[0].path))

        if policy == sidecar.OVERWRITE_SKIP:
            detail.append("")
            detail.append("Those will be left exactly as they are "
                          "(Settings > General: existing tag files).")
            answered = message.question(self, "Write Tag Files", "\n".join(detail))
            return False if answered == QMessageBox.StandardButton.Yes else None

        if policy == sidecar.OVERWRITE_OVERWRITE:
            detail.append("")
            detail.append("Those will be REPLACED, and their current contents lost "
                          "(Settings > General: existing tag files).")
            answered = message.question(self, "Write Tag Files", "\n".join(detail))
            return True if answered == QMessageBox.StandardButton.Yes else None

        # The default: neither answer is safe to assume. A file this app
        # wrote on an earlier run wants replacing; a note the user typed
        # does not, and nothing here can tell those apart.
        box = message.build(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Write Tag Files")
        box.setText("\n".join(detail))
        box.setInformativeText(
            "This app cannot tell a tag file it wrote on an earlier run from a "
            "text file you wrote yourself."
        )
        skip = box.addButton("Keep existing files", QMessageBox.ButtonRole.AcceptRole)
        replace = box.addButton("Replace them", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(skip)
        try:
            box.exec()
            clicked = box.clickedButton()
        finally:
            box.deleteLater()
        if clicked is skip:
            return False
        if clicked is replace:
            return True
        return None

    def action_open_session(self):
        """Replaces the current list with a saved session file."""
        if self.worker and self.worker.isRunning():
            message.information(
                self, "Open Session",
                "A search is running. Stop it before opening a different session.",
            )
            return

        path, _ = QFileDialog.getOpenFileName(
            self, "Open Session", str(Path.home()), "Session files (*.json);;All files (*)",
        )
        if not path:
            return

        incoming_count = session_entry_count(path)
        if incoming_count is None:
            message.warning(
                self, "Open Session",
                f"That doesn't look like a session file this build can read:\n{path}\n\n"
                "See Help > View Logs for details.",
            )
            return

        # Replacing the list is destructive and not undoable, so say what's
        # being traded for what rather than just asking "are you sure".
        if self.entries:
            reply = message.question(
                self, "Open Session",
                f"Replace the current list of {len(self.entries)} image(s) with "
                f"{incoming_count} image(s) from {os.path.basename(path)}?\n\n"
                "Anything unsaved in the current list will be lost. Your files and "
                "anything already sent to Hydrus are untouched.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        entries = load_session(path=path)
        if not entries:
            message.warning(
                self, "Open Session",
                f"No usable entries were found in:\n{path}",
            )
            return

        self._stop_background_workers()
        self.entries = entries
        self._register_new_entries(entries)
        self._thumb_icon_cache.clear()
        self._local_pixmap_cache.clear()
        self._thumbs_in_flight.clear()
        self._last_viewport_range = None
        self._refresh_table()
        self._update_preview(None)
        self._refresh_tag_list(None)
        log.info("Opened session %s with %d entries", path, len(entries))
        self.status_label.setText(
            f"Opened {os.path.basename(path)} - {len(entries)} image(s)"
        )
        self._start_missing_file_check(entries)
        if self.settings.lazy_thumbnails:
            self._schedule_viewport_thumbnails("session opened")
        else:
            self._start_thumbnail_generation(entries)

    def _stop_background_workers(self):
        """Stops anything still working on the OLD list before it's
        replaced - otherwise a thumbnail or hash worker keeps running
        against entries that are no longer in the table, and its results
        arrive for objects nothing references any more."""
        # Retiring also drops each worker's signals, which is the point
        # here: one that outlives the grace wait must not deliver results
        # for entries that are no longer in the table.
        self._workers.retire_attrs(
            self, ("thumbnail_worker", "file_hash_worker", "availability_worker"),
            grace_ms=2000, clear=True,
        )

    def action_clear_session(self):
        if not self.entries and not clear_session():
            return
        reply = message.question(
            self, "Clear Saved Session",
            "Empty the current list and discard the saved session?\n\n"
            "This only affects this app's list - your files and anything already "
            "sent to Hydrus are untouched.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.entries = []
        self._thumb_icon_cache.clear()
        self._local_pixmap_cache.clear()
        clear_session()
        self._refresh_table()
        self._update_preview(None)
        self._refresh_tag_list(None)
        self.status_label.setText("Cleared the list and the saved session")

    def _import_files(self, paths: List[str]):
        if not paths:
            return
        if self.file_hash_worker and self.file_hash_worker.isRunning():
            message.information(
                self, "Add Files", "Please wait for the current batch to finish being added."
            )
            return

        # Hashing (not anything else in this flow) is the dominant cost
        # for a large batch, especially over a network share - do it in
        # the background with real progress feedback instead of freezing
        # the GUI. Covers both the new files and any already-added entry
        # that doesn't have a cached hash yet (opportunistic maintenance,
        # piggybacking on this pass rather than a separate one later).
        existing_needing_hash = [e.path for e in self.entries if not e.hydrus_hash]
        to_hash = existing_needing_hash + list(paths)

        self._pending_import_paths = list(paths)
        self.status_label.setText(f"Hashing {len(to_hash)} file(s)…")
        self.progress_bar.setMaximum(len(to_hash))
        self.progress_bar.setValue(0)

        self.file_hash_worker = FileHashWorker(
            to_hash, workers=self.settings.hash_workers,
            hash_source=self.settings.hash_source,
        )
        self.file_hash_worker.progress.connect(self._on_file_hash_progress)
        self.file_hash_worker.finished_hashing.connect(self._on_file_hash_finished)
        self.file_hash_worker.start()

    def _on_file_hash_progress(self, done: int, total: int, eta_seconds: float):
        self.progress_bar.setValue(done)
        self.status_label.setText(f"Hashing files… ({done}/{total})")
        # Shown separately from the main status text so the countdown sits
        # to the right of the progress numbers rather than jostling them.
        self.wait_countdown_label.setText(
            f"~{_format_duration(eta_seconds)} left" if eta_seconds >= 0 else ""
        )

    def _on_file_hash_finished(self, path_to_hash: Dict[str, str]):
        if self._closing:  # see _on_missing_files_checked (DAN-128)
            return
        self.wait_countdown_label.setText("")
        paths = self._pending_import_paths
        self._pending_import_paths = []
        self.progress_bar.setValue(0)

        # Apply freshly-computed hashes to already-added entries that
        # didn't have one cached yet.
        for e in self.entries:
            if not e.hydrus_hash and e.path in path_to_hash:
                e.hydrus_hash = path_to_hash[e.path]

        unique_paths, duplicate_count = filter_duplicate_paths(paths, path_to_hash, self.entries)
        if not unique_paths:
            if duplicate_count:
                log.info("All %d file(s) were already in the list, nothing added", duplicate_count)
                message.information(
                    self, "Add Files", f"All {duplicate_count} file(s) are already in the list."
                )
            else:
                self.status_label.setText("Ready")
            return

        log.info("Adding %d file(s) to the list (%d duplicate(s) skipped)", len(unique_paths), duplicate_count)
        new_entries = []
        for p in unique_paths:
            entry = ImageEntry(path=p)
            entry.hydrus_hash = path_to_hash.get(p)  # already computed - avoid re-hashing
            new_entries.append(entry)
        self._register_new_entries(new_entries)
        self.entries.extend(new_entries)
        self._refresh_table()
        status = f"Added {len(unique_paths)} file(s), {len(self.entries)} total"
        if duplicate_count:
            status += f" ({duplicate_count} duplicate(s) skipped)"
        self.status_label.setText(status)

        self._lookup_existing_hydrus_tags(new_entries)
        if self.settings.lazy_thumbnails:
            # Only the handful of rows actually on screen need thumbnails;
            # generating for the whole batch here is what made adding tens
            # of thousands of files read the entire library.
            lazylog.info(
                "added %d entry(s) - deferring thumbnails to the viewport pass", len(new_entries),
            )
            self._schedule_viewport_thumbnails("files added")
        else:
            self._start_thumbnail_generation(new_entries)

    def _start_thumbnail_generation(self, entries: List[ImageEntry], quiet: bool = False):
        """Generates row thumbnails in the background. Doing this on the
        GUI thread is what used to make a large batch look hung - the
        window couldn't repaint until every file had been read off disk.

        quiet=True is for the small, frequent passes lazy loading fires
        while scrolling: those must not commandeer the shared progress
        bar, which would otherwise flicker between 0 and 30 constantly
        and stomp on whatever a running search is reporting.
        """
        if not entries:
            return
        if self.settings.thumbnail_source == "off":
            # Nothing to do, and importantly nothing to READ - for a large
            # batch on a network share this is the difference between
            # pulling every byte across the wire and touching none of it.
            log.debug("Row thumbnails are off, skipping generation for %d entry(s)", len(entries))
            return
        self._workers.retire(self.thumbnail_worker, grace_ms=2000)

        self._thumbnails_quiet = quiet
        if not quiet:
            self.progress_bar.setMaximum(len(entries))
            self.progress_bar.setValue(0)
        self.thumbnail_worker = ThumbnailWorker(
            list(entries), THUMB_COLUMN_SIZE * THUMB_DECODE_QUALITY_FACTOR,
            settings=self.settings,
        )
        self.thumbnail_worker.thumbnail_ready.connect(self._on_thumbnail_ready)
        self.thumbnail_worker.progress.connect(self._on_thumbnail_progress)
        self.thumbnail_worker.finished_all.connect(self._on_thumbnails_finished)
        self.thumbnail_worker.start()

    def _on_thumbnail_ready(self, entry: ImageEntry, image):
        # QPixmap can only be built on the GUI thread, which is why the
        # worker hands back a QImage and the conversion happens here.
        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            return
        # Deliberately NOT written into _local_pixmap_cache: that cache
        # feeds the main preview panel, which decodes at PREVIEW_DECODE_SIZE. Storing this small thumbnail there made the preview show
        # a 48px image blown up to fill the panel.
        #
        # The worker decodes at a multiple of the final size, so scale down
        # here with a smooth transform - a direct scaled-decode straight to
        # 48px is noticeably rougher than decoding bigger and filtering down.
        if pixmap.width() > THUMB_COLUMN_SIZE or pixmap.height() > THUMB_COLUMN_SIZE:
            pixmap = pixmap.scaled(
                THUMB_COLUMN_SIZE, THUMB_COLUMN_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation,
            )
        self._thumb_icon_cache[id(entry)] = QIcon(pixmap)
        # A position on SCREEN, so it comes from the model. self.entries
        # is the UNFILTERED list, and its index would repaint whatever
        # row happens to sit there - which is how a filtered table ended
        # up showing thumbnails belonging to other images. None means the
        # filter hides this entry: the icon is cached either way, and
        # will be drawn if the filter is cleared.
        row = self.table_model.row_of(entry)
        if row is None:
            return
        # The icon now lives in the window's cache, which the model reads
        # through _get_row_thumbnail - so this only has to ask for a repaint.
        self.table_model.refresh_row(row)

    def _on_thumbnail_progress(self, done: int, total: int):
        if getattr(self, "_thumbnails_quiet", False):
            return  # a lazy scroll pass - don't touch the shared progress bar
        self.progress_bar.setValue(done)
        self.status_label.setText(f"Loading thumbnails… ({done}/{total})")

    def _on_thumbnails_finished(self):
        quiet = getattr(self, "_thumbnails_quiet", False)
        if self._thumbs_in_flight:
            lazylog.debug(
                "worker finished; clearing %d in-flight marker(s)", len(self._thumbs_in_flight),
            )
            self._thumbs_in_flight.clear()
        if quiet:
            # Another pass may have been requested while this one ran (the
            # user kept scrolling), so re-check rather than assuming the
            # viewport still matches what was just generated.
            self._schedule_viewport_thumbnails("previous lazy pass finished")
            return
        self.progress_bar.setValue(0)
        self.status_label.setText(f"Ready - {len(self.entries)} image(s) in the list")

    def _lookup_existing_hydrus_tags(self, entries: List[ImageEntry]):
        """Kicks off a background check: for each new entry, if Hydrus
        already has this exact file, pull in whatever tags it already has
        instead of asking the user to type tags for every import."""
        if not entries or not self.settings.hydrus.access_key:
            return
        self._workers.retire(self.hydrus_lookup_worker)

        self.hydrus_lookup_worker = HydrusTagLookupWorker(entries, self.settings)
        self.hydrus_lookup_worker.done.connect(self._on_hydrus_tags_looked_up)
        self.hydrus_lookup_worker.start()

    def _on_hydrus_tags_looked_up(self, result):
        self._refresh_table()
        current = self._current_entry()
        if current:
            self._refresh_tag_list(current)
        if result.error:
            self.status_label.setText(
                f"Could not look up existing Hydrus tags: {result.error}"
            )
        elif result.tagged_count:
            self.status_label.setText(
                f"Auto-imported existing Hydrus tags for {result.tagged_count} file(s)"
            )

    def action_query_hydrus(self):
        if not self.settings.hydrus.access_key:
            message.warning(self, "Query Hydrus", "Set your Hydrus access key in Settings first.")
            return
        existing_hashes = {e.hydrus_hash for e in self.entries if e.hydrus_hash}
        dialog = HydrusQueryDialog(self.settings, existing_hashes=existing_hashes, parent=self)
        if self._run_dialog(dialog):
            if dialog.imported_entries or dialog.skipped_duplicate_count:
                # These already carry whatever tags Hydrus has for them
                # (HydrusQueryDialog applies them directly) - no prompt needed.
                self._register_new_entries(dialog.imported_entries)
                self.entries.extend(dialog.imported_entries)
                self._refresh_table()
                status = f"Imported {len(dialog.imported_entries)} file(s) from Hydrus"
                if dialog.skipped_duplicate_count:
                    status += f" ({dialog.skipped_duplicate_count} duplicate(s) already in the list, skipped)"
                    log.info(
                        "Skipped %d duplicate(s) already in the list during Query Hydrus import",
                        dialog.skipped_duplicate_count,
                    )
                self.status_label.setText(status)

    def _add_tags_to_current(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            message.information(self, "Add tags", "Select at least one image first.")
            return

        entries = self.table_model.entries_at(r.row() for r in rows)
        if len(entries) == 1:
            description = entries[0].filename
        else:
            description = f"{len(entries)} selected images"

        dialog = AddTagsDialog(description, self)
        if self._run_dialog(dialog):
            new_tags = dialog.get_tags()
            if new_tags:
                for entry in entries:
                    entry.add_tags(new_tags)
                log.info("Added %d tag(s) to %d image(s)", len(new_tags), len(entries))
                current = self._current_entry()
                if current is not None:
                    self._refresh_tag_list(current)
                self._refresh_table()

    def action_open_log(self):
        path = self.settings.log_file_path
        if not os.path.exists(path):
            message.information(self, "Matched URLs Log", "The log file doesn't exist yet.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def action_parser_health(self):
        self._run_dialog(ParserHealthDialog(self.settings, self))

    def action_pawchive_index(self):
        """Not modal: indexing an artist takes minutes, and the list stays
        usable meanwhile. One window at a time - asking again raises it."""
        from gui.pawchive_index_dialog import PawchiveIndexDialog
        dialog = getattr(self, "_pawchive_index_dialog", None)
        if dialog is None:
            dialog = PawchiveIndexDialog(lambda: self.entries, parent=self)
            dialog.finished.connect(lambda _=None: setattr(self, "_pawchive_index_dialog", None))
            self._pawchive_index_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _warn_about_broken_parsers(self):
        """Say something the first time a site looks broken.

        Without this the only evidence is a log line, which is how three
        parsers came to return no tags at all for who knows how long.
        Reported once per site so a batch doesn't repeat it on every
        image, and only for a site that has NEVER succeeded this session -
        a run of genuinely tagless posts is not a fault.
        """
        for health in parser_health.newly_suspect():
            log.warning("Parser for %s looks broken: %s", health.site, health.summary())
            self.status_label.setText(
                f"{health.site}: no tags in {health.consecutive_empty} attempts - "
                f"see Help > Parser Health"
            )

    def action_view_logs(self):
        self._run_dialog(LogViewerDialog(self))

    def _open_crash_log(self):
        """The run-banner's "View Crash Log" action - opens the same
        dialog as action_view_logs, pre-switched to the crash log so the
        user doesn't have to click the in-dialog toggle a second time."""
        self._run_dialog(LogViewerDialog(self, show_crash_log=True))

    def _update_clear_cache_action_label(self):
        from core.search_cache import get_cache_size_bytes
        size_str = human_size(get_cache_size_bytes()) or "0 B"
        self.clear_cache_action.setText(f"Clear Search Cache ({size_str})…")

    def action_clear_search_cache(self):
        reply = message.question(
            self, "Clear Search Cache",
            "This deletes all saved search results. Images you've already searched "
            "will hit IQDB/SauceNAO fresh again next time. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        from core.search_cache import clear_cache
        removed = clear_cache()
        log.info("Search cache cleared via menu action (%d entries removed)", removed)
        message.information(self, "Clear Search Cache", f"Removed {removed} cached search result(s).")

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    def action_toggle_search(self):
        if self.worker and self.worker.isRunning():
            self.action_stop_search()
        else:
            self.action_start_search()

    def action_start_search(self):
        if self.worker and self.worker.isRunning():
            message.information(self, "Search", "A search is already running.")
            return
        pending = [e for e in self.entries if e.status == MatchStatus.NOT_SEARCHED]
        if not pending:
            message.information(self, "Search", "No unsearched images in the list.")
            return

        log.info("Start Search clicked: %d unsearched image(s)", len(pending))
        self._launch_search_worker(pending, status_prefix="Searching")

    def action_stop_search(self):
        if self.worker:
            log.info("Stop Search clicked")
            self.worker.stop()
            self.status_label.setText("Stopping after current image…")
            self.search_toggle_btn.setEnabled(False)  # re-enabled once the worker actually finishes

    def _on_worker_progress(self, done: int, total: int):
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(done)
        self._run_estimate.record(done, total)
        self._refresh_run_progress_label()

    def _refresh_run_progress_label(self):
        """The count, and what the pace so far says about the rest.

        The count goes up as soon as there is one; the estimate joins it
        a couple of images later, once there is enough of the run to
        average. The count is what the gauge draws - images SEARCHED -
        and the estimate lives in its own right-aligned readout. With no
        run to report the strip says so rather than going blank (R-05).
        Nothing here writes to `status_label`, which the rate-limit wait
        overwrites continuously.
        """
        done = self.progress_bar.value()
        total = self.progress_bar.maximum()
        if total <= 0 or (done <= 0 and not self._run_active):
            self.run_progress_label.setText(self._idle_run_text())
            self.run_eta_label.setText(IDLE_ETA)
            return
        self._run_counted = True
        self.run_progress_label.setText(f"{done:,}/{total:,} searched")
        remaining = self._run_estimate.seconds_remaining() if self._run_active else -1.0
        if remaining >= 0:
            eta = f"ETA ~{_format_duration(remaining)}"
            finish = _format_finish_time(remaining)
            if finish:
                eta += f" · {finish}"
            self.run_eta_label.setText(eta)
        else:
            self.run_eta_label.setText(IDLE_ETA)

    def _idle_run_text(self):
        """What the strip's count says when no run is under way.

        "nothing queued" is only true of an empty list. With images listed
        but not yet searched the strip says how many are waiting instead.
        """
        waiting = sum(1 for e in self.entries if e.status == MatchStatus.NOT_SEARCHED)
        return f"{waiting:,} not searched" if waiting else IDLE_PROGRESS

    def _on_worker_waiting(self, seconds: float):
        self.status_label.setText(f"Waiting {seconds:.0f}s before next search (rate limiting)…")

    def _on_wait_countdown(self, label: str, remaining: float, total: float):
        if not label or remaining <= 0:
            self.wait_countdown_label.setText("")
            return
        # The mockup's vocabulary: "waiting 38s", not "Rate limit: 38s".
        what = "waiting" if label == "Rate limit" else label.lower()
        self.wait_countdown_label.setText(f"{what} {remaining:.0f}s")

    def _on_auto_imported(self, entry: ImageEntry, result: ImportResult):
        """Handles the result of a background auto-import (Settings >
        General). The Hydrus API calls (and, for the URL importer method,
        the confirmation poll) already happened on the worker thread -
        this just applies the GUI-side consequences (removal from the
        list), which have to happen on the GUI thread."""
        if not result.success:
            self._refresh_entry_row(entry)
            return

        # result.confirmed is False specifically when the URL importer
        # method was used, remove_after_import is on, and Hydrus never
        # confirmed the import finished within the poll window - in that
        # case the file may still genuinely be downloading, so it's not
        # safe to remove yet. confirmed is None for the other two methods
        # (success there already means "genuinely done", no polling
        # involved) or True once url_importer's poll did confirm it.
        if result.confirmed is False:
            self._refresh_entry_row(entry)
            return

        if self.settings.remove_after_import:
            self._remove_entries([entry], reason="auto-imported")
        else:
            self._refresh_entry_row(entry)

    def _on_worker_image_updated(self, entry: ImageEntry):
        # Single-row update: during a search the row set never changes,
        # only this one entry's data does. Rebuilding all rows here meant
        # the per-image cost scaled with total list size.
        self._refresh_entry_row(entry)
        if entry.status == MatchStatus.SEARCHING:
            self.status_label.setText(f"Searching {entry.filename}…")
        else:
            self._refresh_saucenao_quota_label()
            self._warn_about_broken_parsers()
        # Same as above: a row to SELECT is a position on screen. Taking
        # it from the unfiltered list selected a different image than the
        # one being searched whenever a filter was on, so the preview and
        # the highlighted row disagreed.
        row = self.table_model.row_of(entry)
        if row is None:
            return   # the filter hides it; nothing to select or preview
        self.table.selectRow(row)
        self._update_preview(entry)
        # The tag panel has to be told explicitly. selectRow only emits a
        # selection change when the selection actually MOVES, and during a
        # search this row is already the selected one - the app selected it
        # when the search started. So the panel was only ever redrawn by
        # clicking away and back, which is exactly how the missing tags
        # looked from the outside.
        self._refresh_tag_list(entry)

    def _refresh_saucenao_quota_label(self):
        try:
            self._render_saucenao_quota_label()
        except Exception as exc:
            # This runs after every single search - an unexpected data
            # shape here (from SauceNAO's API or anything else) must never
            # be able to crash the whole application. Log it and just
            # leave the quota display blank rather than propagate.
            log.warning("Could not render SauceNAO quota display: %s", exc)
            self.saucenao_quota_label.setText("")

    def _render_saucenao_quota_label(self):
        quota = get_last_quota()
        # "SauceNAO 142/200 used today" - the mockup has no colon.
        self.saucenao_quota_label.setText(describe_quota(quota).replace("SauceNAO: ", "SauceNAO ", 1))
        self.saucenao_quota_label.setToolTip(describe_short_window(quota))

    def _on_paused_out_of_quota(self, searched: int, remaining: int):
        """SauceNAO's daily allowance ran out mid-batch. The remaining
        images were left untouched, so pressing Start Search again once
        the quota resets picks up exactly where this stopped."""
        quota = get_last_quota()
        limit = quota.long_limit if quota and quota.long_limit else None
        used = f" ({limit}/day used)" if limit else ""
        state = get_quota_pause_state()
        self.status_label.setText(
            describe_quota_pause(state) if state else
            f"Paused: SauceNAO daily quota exhausted{used} - "
            f"{searched} searched, {remaining} left unsearched"
        )
        box = message.build(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("SauceNAO quota exhausted")
        box.setText(
            f"SauceNAO's daily search allowance is spent{used}, so searching paused after "
            f"{searched} image(s).\n\n"
            f"The remaining {remaining} image(s) were left unsearched - press Start Search "
            "again once the quota resets and it'll carry on from here.\n\n"
            "Or continue right now without SauceNAO: the remaining images are searched with "
            "the other engines, marked Provisional, and re-searchable once the allowance "
            "resets - the same thing \"carry on without SauceNAO\" in Settings > SauceNAO "
            "does for future runs, just for this one without reopening Settings (DAN-486)."
        )
        continue_btn = box.addButton(
            "Continue with other engines", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        try:
            box.exec()
            clicked = box.clickedButton()
        finally:
            box.deleteLater()
        if clicked is continue_btn:
            self._resume_without_saucenao()

    def _resume_without_saucenao(self):
        """'Continue with other engines' from the quota-pause dialog:
        resumes immediately on whatever is still unsearched, skipping
        SauceNAO for just this run - the persisted Settings checkbox is
        left exactly as the user has it (DAN-486)."""
        if self.worker and self.worker.isRunning():
            return
        pending = [e for e in self.entries if e.status == MatchStatus.NOT_SEARCHED]
        if not pending:
            return
        # _launch_search_worker() below clears the persisted pause too;
        # nothing further to do about it here.
        log.info("Continuing without SauceNAO: %d unsearched image(s)", len(pending))
        self._launch_search_worker(
            pending, status_prefix="Searching", force_continue_without_saucenao=True)

    def _on_continuing_without_saucenao(self, searched: int, remaining: int):
        """The allowance went, and the user has asked to carry on anyway.

        Said once, in the status bar rather than a dialog: this runs
        unattended for hours by design, and a modal box would stop the
        batch dead waiting for someone to come back and dismiss it -
        which is the opposite of what enabling this asked for.
        """
        self.status_label.setText(
            f"SauceNAO's daily quota is spent - carrying on without it for the remaining "
            f"{remaining} image(s). Those results are marked Provisional and are not cached; "
            "re-search them once the allowance resets."
        )

    def _on_engine_alert(self, alert):
        """An engine cannot run at all - say so where it will be seen.

        Only raised for conditions that will not come right on the next
        image, and only once per condition per session (see
        core/engine_alerts.py), so this is a dialog rather than a line in
        the status bar: OBSERVED with Google Lens and Playwright absent,
        the log said so and nothing else did, and a whole batch finished
        looking as though Lens had searched every image and found nothing.

        The status bar is written too, because the dialog is dismissed and
        the run is not.
        """
        self.status_label.setText(f"{alert.engine} was skipped - {alert.title}")
        box = message.build(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(alert.title)
        box.setText(alert.body)
        if alert.remedy:
            # The install command goes in detailed text rather than the
            # body: it is several lines of paths, and it is the part the
            # user will want to select and copy.
            box.setDetailedText(alert.remedy)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()
        box.deleteLater()

    def _on_worker_finished(self):
        engine_alerts.unsubscribe(self._engine_alert_listener)
        self._run_active = False
        self._refresh_run_progress_label()
        self.status_label.setText("Search finished")
        self.search_toggle_btn.setText("▶  Start Search")
        self.search_toggle_btn.setEnabled(True)

    # ------------------------------------------------------------------
    # Table / tags
    # ------------------------------------------------------------------
    def _register_new_entries(self, entries: List[ImageEntry]):
        """Records insertion order for newly-added entries, so the 'original'
        state of the header-click sort cycle can restore it later."""
        for e in entries:
            self._entry_sequence[id(e)] = self._next_sequence
            self._next_sequence += 1

    def _on_header_clicked(self, column: int):
        """Cycles a clicked column through ascending -> descending ->
        original (insertion) order -> ascending -> ... Clicking a
        different column always starts that column fresh at ascending."""
        if self._sort_column != column:
            self._sort_column = column
            self._sort_ascending = True
        elif self._sort_ascending:
            self._sort_ascending = False
        else:
            self._sort_column = None  # back to original order

        self._apply_sort()

    def _apply_sort(self):
        previously_current = self._current_entry()

        if self._sort_column is None:
            self.entries.sort(key=lambda e: self._entry_sequence.get(id(e), 0))
            self.table.horizontalHeader().setSortIndicatorShown(False)
        else:
            key_func = sort_key_for_column(self._sort_column)
            self.entries.sort(key=key_func, reverse=not self._sort_ascending)
            order = Qt.SortOrder.AscendingOrder if self._sort_ascending else Qt.SortOrder.DescendingOrder
            self.table.horizontalHeader().setSortIndicatorShown(True)
            self.table.horizontalHeader().setSortIndicator(self._sort_column, order)

        self._refresh_table()
        self._schedule_viewport_thumbnails("table sorted")

        if previously_current is not None:
            # The row to reselect is a position on SCREEN, so it comes from
            # the model - self.entries.index() would be the position in the
            # unfiltered list and select the wrong row whenever a filter is
            # on. None means the sort left it hidden, so there is nothing
            # to select.
            row = self.table_model.row_of(previously_current)
            if row is not None:
                self.table.selectRow(row)

    def _restore_table_header_state(self):
        """Restores previously-saved column widths and order, if any."""
        state_b64 = self.settings.table_header_state
        if not state_b64:
            return
        if not header_layout_is_usable(state_b64, self.settings.table_header_columns):
            log.info(
                "Discarding saved column layout: saved with %s column(s), this build has %d",
                self.settings.table_header_columns or "an unrecorded number of",
                len(COLUMNS),
            )
            self.settings.table_header_state = ""
            self.settings.table_header_columns = 0
            return
        try:
            data = QByteArray.fromBase64(state_b64.encode("ascii"))
            if not self.table.horizontalHeader().restoreState(data):
                log.warning("Could not restore table column layout (invalid saved state)")
                self.settings.table_header_state = ""
                return
        except Exception as exc:
            log.warning("Could not restore table column layout: %s", exc)
            self.settings.table_header_state = ""
            return

        self._repair_header_after_restore()

    def _repair_header_after_restore(self):
        """Undoes any damage a restored layout may have left behind.

        Qt will happily apply a state that leaves sections hidden or
        zero-width, and a header cell with no width shows no label - it
        only appears as a tooltip when the divider is hovered, which is
        indistinguishable from "the titles are missing". Belt-and-braces
        alongside the column-count check above, because the failure is
        silent and looks like a rendering bug rather than a stale
        setting.
        """
        header = self.table.horizontalHeader()
        repaired = []
        for index in range(header.count()):
            if header.isSectionHidden(index):
                header.setSectionHidden(index, False)
                repaired.append(f"{COLUMNS[index]} (hidden)")
            if header.sectionSize(index) < MIN_COLUMN_WIDTH:
                header.resizeSection(index, DEFAULT_COLUMN_WIDTH)
                repaired.append(f"{COLUMNS[index]} (too narrow to show its title)")
        if repaired:
            log.info("Repaired restored column layout: %s", ", ".join(repaired))

    def _save_table_header_state(self):
        """Saves current column widths and order for next launch."""
        data = self.table.horizontalHeader().saveState()
        self.settings.table_header_state = bytes(data.toBase64()).decode("ascii")
        # Recorded so a future build that adds or removes a column can
        # tell this layout no longer applies.
        self.settings.table_header_columns = len(COLUMNS)

    def _refresh_sent_count_label(self):
        """Keeps the status-bar "N/M sent" readout in step with the list."""
        total = len(self.entries)
        if not total:
            self.sent_count_label.setText("")
            if not self._run_counted and not self._run_active:
                self.run_progress_label.setText(self._idle_run_text())
            return
        sent = sum(1 for e in self.entries if e.sent_to_hydrus and e.hydrus_import_confirmed)
        queued = sum(1 for e in self.entries if e.sent_to_hydrus and not e.hydrus_import_confirmed)
        # The total is the searched count's to give (`n/m searched`); what
        # is sent is its own number (R-04).
        text = f"{sent} sent"
        if queued:
            text += f" ({queued} queued)"
        # Only once something has actually been reviewed. Before that the
        # count is just the unsent count under another name, and the status
        # bar has no room for a second way of saying the same thing.
        if any(e.reviewed for e in self.entries):
            text += f" · {sum(1 for e in self.entries if e.needs_review)} to review"
        self.sent_count_label.setText(text)
        if not self._run_counted and not self._run_active:
            self.run_progress_label.setText(self._idle_run_text())

    def _refresh_entry_row(self, entry: ImageEntry):
        # Deliberately repaints in place rather than re-applying the
        # filter. A search updates thousands of entries one at a time, and
        # re-filtering per entry would reset the model - and drop the
        # selection - on each one, with rows vanishing from under the
        # cursor as they stopped matching. The filter is re-evaluated on
        # the next structural refresh instead.
        """Updates just this entry's row. Use whenever one entry changed
        but the list itself didn't - a full _refresh_table() is only
        needed when rows are added, removed, or reordered."""
        self.table_model.refresh_entry(entry)
        self._refresh_sent_count_label()

    def _refresh_table(self, keep_place: Optional[int] = None):
        """Tells the view the row SET changed - rows added, removed or
        reordered. For a single entry's data changing, use
        _refresh_entry_row().

        No longer builds anything: the model renders each row on demand
        as it is painted, so this costs one signal regardless of whether
        the list holds ten images or fifty thousand.

        A model reset drops the selection, so this puts it back. That used
        to be left to callers "that need it", which turned out to be a
        rule 16 of the 19 call sites broke - including every one that acts
        on the row you are looking AT. Dropping a dead match, editing a
        tag, adding one, sending to Hydrus: each one refreshed the table
        and left the row you were working on unselected, taking the
        preview and tag panel with it.

        Reselection is by identity, so an entry the refresh removed simply
        does not come back - which is correct, and is why replacing the
        whole list still ends up with nothing selected without needing to
        be told.

        `keep_place` is the row the reviewer was on, passed by the send
        actions: sending can make that row leave the list (Remove after
        import, or a filter that hides sent rows). If nothing survives to
        be reselected, the row that slid up into its place is selected
        instead, so the next J/K carries on from there rather than from
        the top or bottom of an empty selection.
        """
        selected = self._selected_entries()
        # Passes the list every time: several paths replace self.entries
        # with a new list object rather than mutating it, and the model
        # must follow the current one rather than the one it was built
        # with.
        self.table_model.refresh_all(self.entries)
        self._reselect(selected)
        if (keep_place is not None
                and not self.table.selectionModel().selectedRows()
                and self.table_model.rowCount()):
            self.select_table_row(min(keep_place, self.table_model.rowCount() - 1))
        self._refresh_sent_count_label()
        # The filter menus offer only the statuses and sites actually
        # present, so they follow the list. Guarded because the very first
        # refresh can land before the bar is built.
        if hasattr(self, "filter_bar"):
            self.filter_bar.rebuild_menus()
            self._refresh_filter_ui()

    def _get_local_pixmap(self, entry: ImageEntry) -> Optional[QPixmap]:
        """The decoded local image (scaled to PREVIEW_DECODE_SIZE), cached
        per entry. Shared by both the main preview panel and the table's
        thumbnail column - without this, selecting a large image would
        re-decode it from disk on every single click, and the thumbnail
        column would independently decode the same file all over again."""
        cached = self._local_pixmap_cache.get(id(entry))
        if cached is not None:
            return cached if not cached.isNull() else None

        pixmap = _load_preview_pixmap(entry.path)
        # Cache a null QPixmap as a "tried and failed" marker, distinct
        # from "not yet attempted" (key absent) - so a broken/corrupt file
        # isn't retried on every click either.
        self._local_pixmap_cache[id(entry)] = pixmap if pixmap is not None else QPixmap()
        return pixmap

    def _get_row_thumbnail(self, entry: ImageEntry) -> QIcon:
        """The row's cached thumbnail, or a blank placeholder.

        Deliberately does NOT decode here: _refresh_table() calls this for
        every row, so decoding on demand meant adding a large batch froze
        the GUI for as long as it took to read every file off disk. The
        background ThumbnailWorker fills these in instead, and each row
        updates as its thumbnail arrives.

        The icon cache is capped, so an entry scrolled far enough out of
        use can be evicted. Rather than leaving that row permanently
        blank, it's queued for the background worker to regenerate - still
        off the GUI thread, so scrolling stays smooth."""
        cached = self._thumb_icon_cache.get(id(entry))
        if cached is not None:
            return cached
        if self.settings.lazy_thumbnails:
            # Deliberately does NOT queue here. _refresh_table() calls this
            # for EVERY row, so queueing from here would ask for a
            # thumbnail for all 28,000 entries the moment the table is
            # built - precisely the eager behaviour lazy loading exists to
            # avoid. The viewport pass decides what's actually needed.
            return QIcon()
        self._queue_thumbnail_regeneration(entry)
        return QIcon()

    # ------------------------------------------------------------------
    # Lazy (viewport-driven) thumbnails
    # ------------------------------------------------------------------
    def _schedule_viewport_thumbnails(self, reason: str):
        """Debounced trigger for a viewport pass. Every caller says WHY,
        because when this misbehaves the useful question is almost always
        'what was it reacting to?'"""
        if not self.settings.lazy_thumbnails:
            return
        lazylog.debug("scheduled by %s (debounce %dms)", reason, THUMB_VIEWPORT_DEBOUNCE_MS)
        self._viewport_thumb_timer.start(THUMB_VIEWPORT_DEBOUNCE_MS)

    def _visible_row_range(self):
        """(first, last) row indices currently on screen, inclusive.

        rowAt() maps a viewport y-coordinate to a row, returning -1 when
        that coordinate is past the last row - which happens routinely
        when the list is shorter than the viewport, or when scrolled to
        the bottom, so it is a normal case rather than an error.
        """
        row_count = self.table_model.rowCount()
        if row_count == 0:
            return None

        viewport_height = self.table.viewport().height()
        first = self.table.rowAt(0)
        last = self.table.rowAt(max(0, viewport_height - 1))

        # Note the -1 cases are NOT normalized here - visible_range_with_buffer
        # handles them, and logging the raw values is what makes a
        # misbehaving viewport diagnosable from a log alone.
        lazylog.debug(
            "viewport: height=%dpx, rowAt(0)=%d, rowAt(%d)=%d, rowCount=%d "
            "(-1 means past the last row, which is normal)",
            viewport_height, first, max(0, viewport_height - 1), last, row_count,
        )
        return first, last

    def _update_visible_thumbnails(self):
        """Generates thumbnails for the rows on screen (plus a buffer).

        This is the whole point of lazy loading: a table shows roughly
        twenty rows, so generating thumbnails for every entry in a batch
        of tens of thousands reads the entire library to draw previews
        nobody is looking at.
        """
        self._viewport_pass_count += 1
        pass_id = self._viewport_pass_count
        if pass_id == 1:
            lazylog.info(
                "lazy thumbnails active: lazy=%s source=%s buffer=%d rows debounce=%dms "
                "icon_cache=%d entries",
                self.settings.lazy_thumbnails, self.settings.thumbnail_source,
                THUMB_VIEWPORT_BUFFER_ROWS, THUMB_VIEWPORT_DEBOUNCE_MS,
                THUMB_ICON_CACHE_ENTRIES,
            )

        if not self.settings.lazy_thumbnails:
            lazylog.debug("pass #%d: skipped - lazy thumbnails are off", pass_id)
            return
        if self.settings.thumbnail_source == "off":
            lazylog.debug("pass #%d: skipped - row thumbnails are off entirely", pass_id)
            return
        if not self.entries:
            lazylog.debug("pass #%d: skipped - no entries in the list", pass_id)
            return

        visible = self._visible_row_range()
        if visible is None:
            lazylog.debug("pass #%d: skipped - table has no rows yet", pass_id)
            return
        first, last = visible

        expanded = visible_range_with_buffer(
            # The number of rows the table HAS, which under a filter is
            # not the number of entries: clamping against the unfiltered
            # length lets the range run off the end of what is shown.
            first, last, self.table_model.rowCount(), THUMB_VIEWPORT_BUFFER_ROWS,
        )
        if expanded is None:
            lazylog.debug("pass #%d: skipped - no rows to expand into", pass_id)
            return
        first, last = expanded
        lazylog.debug(
            "pass #%d: range with +/-%d buffer = rows %d..%d (%d row(s))",
            pass_id, THUMB_VIEWPORT_BUFFER_ROWS, first, last, last - first + 1,
        )

        if self._last_viewport_range == (first, last) and not self._thumbs_in_flight:
            lazylog.debug("pass #%d: range unchanged since last completed pass, nothing to do", pass_id)
            return

        work = entries_needing_thumbnails(
            self.table_model.visible_entries()[first:last + 1],
            lambda eid: self._thumb_icon_cache.get(eid) is not None,
            self._thumbs_in_flight,
        )
        needed = work.needed

        lazylog.debug(
            "pass #%d: of %d row(s) in range - %d cached, %d already generating, %d to generate",
            pass_id, work.examined, work.already_cached, work.in_flight, len(needed),
        )

        if not needed:
            self._last_viewport_range = (first, last)
            return

        if self.thumbnail_worker and self.thumbnail_worker.isRunning():
            # Don't interrupt an in-progress pass - it would cancel work
            # that's probably for rows still on screen. Try again shortly.
            lazylog.debug(
                "pass #%d: a thumbnail worker is still running, retrying in %dms",
                pass_id, THUMB_VIEWPORT_DEBOUNCE_MS * 2,
            )
            self._viewport_thumb_timer.start(THUMB_VIEWPORT_DEBOUNCE_MS * 2)
            return

        self._last_viewport_range = (first, last)
        for entry in needed:
            self._thumbs_in_flight.add(id(entry))
        lazylog.info(
            "pass #%d: generating %d thumbnail(s) for rows %d..%d (%s%s)",
            pass_id, len(needed), first, last,
            os.path.basename(needed[0].path),
            f" … {os.path.basename(needed[-1].path)}" if len(needed) > 1 else "",
        )
        self._start_thumbnail_generation(needed, quiet=True)

    def _queue_thumbnail_regeneration(self, entry: ImageEntry):
        """Collects entries whose cached thumbnail was evicted so they can
        be regenerated in one background pass, rather than spawning a
        worker per row during a scroll."""
        if entry.path in self._pending_thumb_regen:
            return
        self._pending_thumb_regen.add(entry.path)
        self._thumb_regen_timer.start(250)  # coalesce a burst of scrolling into one pass

    def _run_thumbnail_regeneration(self):
        pending_paths = self._pending_thumb_regen
        self._pending_thumb_regen = set()
        if not pending_paths:
            return
        if self.thumbnail_worker and self.thumbnail_worker.isRunning():
            # A batch pass is already running; it'll repopulate these anyway.
            return
        entries = [e for e in self.entries if e.path in pending_paths]
        if entries:
            log.debug("Regenerating %d evicted thumbnail(s)", len(entries))
            self._start_thumbnail_generation(entries)

    def _show_table_context_menu(self, pos):
        table_context_menu.show(self, pos)

    def _select_rows_by_status(self, status: MatchStatus):
        """Selects every row with the given status, replacing whatever
        was selected before - the quick way to grab, say, every failed
        search to retry them without hunting through a sorted list."""
        self._select_rows_by_predicate(f'status "{status.label}"', lambda e: e.status == status)

    def _select_rows_by_predicate(self, description: str, predicate):
        """Shared by the Status and Sent State submenus - selects every
        row the predicate matches, replacing the current selection."""
        matching_rows = [i for i, e in enumerate(self.entries) if predicate(e)]
        self.table.clearSelection()
        if not matching_rows:
            self.status_label.setText(f"No images matching {description}")
            return

        selection = QItemSelection()
        model = self.table_model
        last_col = model.columnCount() - 1
        for row in matching_rows:
            selection.select(model.index(row, 0), model.index(row, last_col))
        self.table.selectionModel().select(
            selection, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
        )
        self.status_label.setText(f"Selected {len(matching_rows)} image(s) matching {description}")

    def _live_entries(self, entries: List[ImageEntry]) -> List[ImageEntry]:
        """Drops any of the given entries that are no longer in the list.

        The context menu resolves its selection to entries when it is
        built, but exec() keeps a nested event loop running while it is
        open, and the background import poller can remove rows in that
        window. Acting on a captured entry is what fixes picking the
        wrong FILE; it does nothing on its own about one of them having
        been removed in the meantime, so anything handed a selection like
        this filters it through here first and silently skips the rest -
        removed is removed, there is nothing left for these actions to do
        to it.
        """
        live_ids = {id(e) for e in self.entries}
        return [e for e in entries if id(e) in live_ids]

    def _reset_rows(self, entries: List[ImageEntry]):
        """Puts the given rows back to never-searched.

        Confirms first when anything would actually be lost. Re-running a
        search costs 45-75 seconds per image by default, so a mis-click
        on a large selection is hours of work, and this is the only entry
        in the menu that can quietly undo a whole overnight run."""
        # Imported here rather than at module level, matching how the
        # other search_cache calls in this file are done.
        from core.search_cache import drop_cached_result

        entries = self._live_entries(entries)
        with_results = [e for e in entries if e.has_result()]
        if not with_results:
            self.status_label.setText("Nothing to reset - none of those have been searched yet")
            return

        count = len(with_results)
        noun = "image" if count == 1 else "images"
        if message.question(
            self, "Reset results",
            f"Discard the search results for {count} {noun}?\n\n"
            "The match, its tags and any error are cleared and they go back to "
            "Not searched. Tags you typed and tags from Hydrus are kept, and "
            "nothing on disk or in Hydrus is touched.\n\n"
            "Searching them again takes as long as it did the first time.",
        ) != QMessageBox.StandardButton.Yes:
            return

        dropped_from_cache = 0
        for entry in with_results:
            entry.reset_result()
            # Drop the cached result too, or the next search would hand
            # back the very match just discarded and the reset would look
            # like it had not worked.
            if entry.hydrus_hash and drop_cached_result(entry.hydrus_hash):
                dropped_from_cache += 1

        log.info(
            "Reset %d result(s), dropping %d cached search result(s)",
            count, dropped_from_cache,
        )
        self._refresh_table()
        self._on_selection_changed()  # the preview and tag list describe a match that is gone
        status = f"Reset {count} {noun} to Not searched"
        if dropped_from_cache:
            status += f" ({dropped_from_cache} cached result(s) dropped)"
        self.status_label.setText(status)
        self._update_clear_cache_action_label()

    def _set_rows_reviewed(self, entries: List[ImageEntry], reviewed: bool):
        """Marks or unmarks rows as reviewed.

        No confirmation and no dialog: this is the cheapest, most
        reversible thing in the menu, and it is meant to be used a few
        thousand times in a sitting. Nothing leaves the machine, nothing
        on disk changes, and pressing it again puts it back.
        """
        entries = self._live_entries(entries)
        changed = [e for e in entries if e.reviewed != reviewed]
        if not changed:
            return
        for entry in changed:
            entry.reviewed = reviewed
            self._refresh_entry_row(entry)
        self._refresh_sent_count_label()
        verb = "Marked" if reviewed else "Unmarked"
        remaining = sum(1 for e in self.entries if e.needs_review)
        log.info("%s %d image(s) as reviewed (%d still to review)",
                 verb, len(changed), remaining)
        self.status_label.setText(
            f"{verb} {len(changed)} image(s) as reviewed — {remaining} still to review"
        )

    def _research_rows(self, entries: List[ImageEntry]):
        """Re-runs IQDB/SauceNAO search for the given rows, regardless of
        their current status - overwrites whatever candidates/tags they
        already had with fresh results. Always bypasses the search cache,
        since the whole point is to get a fresh look, not a saved one."""
        if self.worker and self.worker.isRunning():
            message.information(self, "Search", "A search is already running.")
            return
        entries = self._live_entries(entries)
        if not entries:
            return
        log.info("Re-search requested for %d image(s): %s",
                 len(entries), [e.filename for e in entries])
        self._launch_search_worker(entries, status_prefix="Re-searching", bypass_cache=True)

    def _research_rows_with_engine(self, entries: List[ImageEntry], engine: str):
        """Re-searches the given rows using ONE named engine, whatever
        their current state.

        Separate from the opposite-engine action because "the opposite"
        is undefined for a row that hasn't been searched yet - and
        refusing the request over that was unhelpful, since wanting to
        run IQDB on some unsearched images is a perfectly ordinary thing
        to want, especially once SauceNAO's daily quota is spent.
        """
        if self.worker and self.worker.isRunning():
            message.information(self, "Search", "A search is already running.")
            return
        entries = self._live_entries(entries)
        if not entries:
            return

        label = engine_label(engine)
        log.info("%s-only search requested for %d image(s)", label, len(entries))
        self._launch_search_worker(
            entries, status_prefix=f"Searching ({label} only)",
            override_engines={id(e): engine for e in entries}, bypass_cache=True,
        )

    def _research_rows_with_opposite_engine(self, passed_entries: List[ImageEntry]):
        """Re-searches each row with whichever engine did NOT find its
        current match - a SauceNAO match gets retried on IQDB and vice
        versa - ignoring the configured primary/fallback order.

        A row with no match has no "current engine" to flip. Rather than
        refuse the whole request over that, those rows use the opposite
        of the configured PRIMARY, which is the same thing the user is
        asking for: try the engine that hasn't been tried.
        """
        if self.worker and self.worker.isRunning():
            message.information(self, "Search", "A search is already running.")
            return

        entries: List[ImageEntry] = []
        override_engines: Dict[int, str] = {}
        primary = effective_primary(self.settings.primary_engine)
        fallback_for_unsearched = opposite_engine(None, primary)
        unsearched = 0

        for entry in self._live_entries(passed_entries):
            candidate = entry.selected_candidate
            current = candidate.engine if candidate else None
            if not (current and current.strip()):
                unsearched += 1
            entries.append(entry)
            override_engines[id(entry)] = opposite_engine(current, primary)

        if not entries:
            return

        if unsearched:
            log.info(
                "Opposite-engine search: %d row(s) had no match to flip, using %r "
                "(the opposite of the configured primary %r)",
                unsearched, fallback_for_unsearched, primary,
            )
        log.info(
            "Opposite-engine search requested for %d image(s): %s",
            len(entries), [(e.filename, override_engines[id(e)]) for e in entries],
        )
        self._launch_search_worker(
            entries, status_prefix="Searching (opposite engine)",
            override_engines=override_engines, bypass_cache=True,
        )

    def _check_match_availability(self, entry: ImageEntry):
        """Checks each of this image's matches to see whether the source
        content is still there, then offers to drop the ones that are
        definitely gone."""
        if self.availability_worker and self.availability_worker.isRunning():
            message.information(self, "Check Match Availability", "A check is already running.")
            return
        if not entry.candidates:
            message.information(
                self, "Check Match Availability", "This image has no matches to check.",
            )
            return

        self.status_label.setText(f"Checking availability of {len(entry.candidates)} match(es)…")
        self.progress_bar.setMaximum(len(entry.candidates))
        self.progress_bar.setValue(0)

        self.availability_worker = AvailabilityWorker(entry, self.settings)
        self.availability_worker.progress.connect(self._on_availability_progress)
        self.availability_worker.finished_checking.connect(self._on_availability_finished)
        self.availability_worker.start()

    def _on_availability_progress(self, done: int, total: int):
        self.progress_bar.setValue(done)
        self.status_label.setText(f"Checking match availability… ({done}/{total})")

    def _on_availability_finished(self, entry: ImageEntry, gone_count: int, checked_count: int):
        if self._closing:  # see _on_missing_files_checked (DAN-128)
            return
        self.progress_bar.setValue(0)
        unknown_count = sum(1 for c in entry.candidates if c.remote_available is None)

        if gone_count == 0:
            msg = f"All {checked_count} match(es) still appear to be available."
            if unknown_count:
                msg += (
                    f"\n\n({unknown_count} couldn't be checked conclusively - unreachable or "
                    "blocked - and were left alone.)"
                )
            self.status_label.setText("Availability check finished: nothing gone")
            message.information(self, "Check Match Availability", msg)
            return

        gone = [c for c in entry.candidates if c.remote_available is False]
        detail = "\n".join(f"{(c.source_name or _host_from_url(c.url) or '?')} — {c.url}" for c in gone)

        box = message.build(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Check Match Availability")
        text = f"{gone_count} of {checked_count} match(es) are no longer available.\n\nRemove them from this image's match list?"
        if unknown_count:
            text += (
                f"\n\n({unknown_count} other(s) couldn't be checked conclusively and will be kept.)"
            )
        box.setText(text)
        box.setDetailedText(detail)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if self._run_dialog(box) != QMessageBox.StandardButton.Yes:
            self.status_label.setText(f"Availability check finished: {gone_count} gone (kept)")
            return

        self._remove_dead_candidates(entry)

    def _remove_dead_candidates(self, entry: ImageEntry):
        """Drops confirmed-gone candidates, keeping the user's current
        selection pointed at the same match where possible - the index
        shifts when earlier entries are removed, so it's re-derived from
        the candidate object itself rather than the old number."""
        removed = entry.drop_unavailable_candidates()
        if not entry.candidates:
            log.info("All matches for %s were gone; entry now has no match", entry.filename)
        log.info("Removed %d dead match(es) from %s", removed, entry.filename)
        self.status_label.setText(f"Removed {removed} unavailable match(es)")
        self._refresh_table()
        self._populate_candidate_combo(entry)
        self._update_preview(entry)
        self._refresh_tag_list(entry)

    def _open_compare_dialog(self, entry: ImageEntry):
        if entry.selected_candidate is None:
            message.information(
                self, "Compare", "This image has no match to compare against yet.",
            )
            return
        if entry.file_missing or not os.path.exists(entry.path):
            message.information(
                self, "Compare",
                "The local file is no longer on disk, so there's nothing to compare "
                "the match against.",
            )
            return
        log.info("Opening comparison view for %s", entry.filename)
        self._run_dialog(CompareDialog(entry, self.settings, self))

    def _check_rows_for_upscaling(self, entries: List[ImageEntry]):
        """Runs the upscale-detection heuristics for the given rows in the
        background (a few resize operations per image, so it can take a
        moment for large ones) and shows a summary dialog when done."""
        if self.upscale_check_worker and self.upscale_check_worker.isRunning():
            message.information(self, "Check for Upscaling", "A check is already running.")
            return

        entries = self._live_entries(entries)
        if not entries:
            return

        self.status_label.setText(f"Checking {len(entries)} image(s) for upscaling…")
        self.upscale_check_worker = UpscaleCheckWorker(entries)
        self.upscale_check_worker.entry_checked.connect(self._on_upscale_entry_checked)
        self.upscale_check_worker.finished_all.connect(
            lambda: self._on_upscale_check_finished(entries)
        )
        self.upscale_check_worker.start()

    def _on_upscale_entry_checked(self, entry: ImageEntry, result):
        parts = []
        if result.metadata_signature:
            parts.append(f"⚠ {result.metadata_signature}")
        if result.source_comparison:
            marker = "⚠" if result.source_comparison.flagged else "✓"
            parts.append(f"{marker} {result.source_comparison.message}")
        if result.self_consistency and result.self_consistency.confidence != "none":
            marker = "⚠" if result.self_consistency.confidence == "likely" else "?"
            parts.append(f"{marker} {result.self_consistency.message}")
        elif result.self_consistency:
            parts.append(f"✓ {result.self_consistency.message}")

        # Persisted on the entry itself - like Similarity or Size
        # Difference - rather than kept only for this dialog, so the
        # verdict survives past the one moment this summary is on screen.
        detail = "\n".join(parts) if parts else "No checks could be run for this image."
        entry.upscale_verdict = "flagged" if "⚠" in detail else "clear"
        entry.upscale_check_detail = detail
        self._refresh_entry_row(entry)

    def _on_upscale_check_finished(self, entries: List[ImageEntry]):
        flagged_count = 0
        lines = []
        for entry in entries:
            summary = entry.upscale_check_detail or "No result."
            if entry.upscale_verdict == "flagged":
                flagged_count += 1
            lines.append(f"{entry.filename}:\n{summary}")

        self.status_label.setText(
            f"Upscale check finished: {flagged_count}/{len(entries)} flagged"
        )

        box = message.build(self)
        box.setIcon(QMessageBox.Icon.Warning if flagged_count else QMessageBox.Icon.Information)
        box.setWindowTitle("Upscale Check Results")
        box.setText(
            f"{flagged_count} of {len(entries)} image(s) show possible signs of upscaling.\n\n"
            "These are heuristics, not proof - see details for each image's specific result."
        )
        box.setDetailedText("\n\n".join(lines))
        self._run_dialog(box)

    def _launch_search_worker(
        self, entries: List[ImageEntry], status_prefix: str = "Searching",
        override_engines: Optional[Dict[int, str]] = None, bypass_cache: bool = False,
        force_continue_without_saucenao: bool = False,
    ):
        # Starting a search is the user saying "try again", so forget any
        # remembered quota exhaustion. If the allowance really hasn't
        # reset, SauceNAO's first response sets the flag again and the
        # batch pauses after one image - which also doubles as a cheap
        # check of whether the quota is actually back.
        reset_daily_limit_flag()
        # Same reasoning for a persisted quota-pause banner (DAN-486):
        # pressing Start Search is already the user acting on it, so a
        # stale "paused" banner from the last run would be misleading
        # rather than helpful. If the quota is still out, pausing persists
        # a fresh one after one image, same as the flag above.
        clear_quota_pause()
        # Likewise forget a previous ascii2d block: the site's bot check
        # comes and goes, so a new search is the right moment to find out
        # whether it is still refusing us. Google's consent wall and
        # "unusual traffic" check behave the same way, and a Lens robot
        # check the user closed last time should be offered again rather
        # than assumed refused forever.
        reset_ascii2d_blocked()
        reset_google_blocked()
        google_lens.reset_blocked_flag()
        yandex.reset_blocked_flag()

        # Callers all refuse to start while a search is running, so this
        # normally just releases the previous, finished worker - but it keeps
        # the "never overwrite a live QThread" rule true at every start point.
        self._workers.retire(self.worker)

        self.worker = SearchWorker(
            entries, self.settings, override_engines=override_engines, bypass_cache=bypass_cache,
            force_continue_without_saucenao=force_continue_without_saucenao,
        )
        self.worker.image_updated.connect(self._on_worker_image_updated)
        self.worker.progress.connect(self._on_worker_progress)
        self.worker.waiting.connect(self._on_worker_waiting)
        self.worker.wait_countdown.connect(self._on_wait_countdown)
        self.worker.auto_imported.connect(self._on_auto_imported)
        self.worker.paused_out_of_quota.connect(self._on_paused_out_of_quota)
        self.worker.continuing_without_saucenao.connect(
            self._on_continuing_without_saucenao)
        self.worker.finished_all.connect(self._on_worker_finished)
        # Open the engine-alert channel for the length of the run. Engines
        # raise these from the worker's own thread pool; see
        # core/engine_alerts.py and _on_engine_alert.
        engine_alerts.subscribe(self._engine_alert_listener)
        self.progress_bar.setMaximum(len(entries))
        self.progress_bar.setValue(0)
        self._run_estimate.reset()
        self._run_active = True
        self._refresh_run_progress_label()
        self.worker.start()
        self.search_toggle_btn.setText("■  Stop Search")
        self.status_label.setText(f"{status_prefix} {len(entries)} image(s)…")

    def _show_rows_in_hydrus(self, entries: List[ImageEntry]):
        """Opens a Hydrus page showing the selected local files.

        The other direction from everything else here: instead of sending
        a result to Hydrus, this jumps to what Hydrus already holds, so a
        row can be checked against the real thing - its other tags, its
        duplicates, its ratings - without hunting for it by hash.

        Only files Hydrus actually has can be shown, so the ones it does
        not know are filtered out first rather than being asked for and
        producing an empty page with nothing to say why.
        """
        entries = self._live_entries(entries)
        if not entries:
            return
        if not self.settings.hydrus.access_key:
            message.warning(self, "Show in Hydrus",
                            "Set your Hydrus access key in Settings first.")
            return

        client = HydrusClient(self.settings.hydrus)
        hashes = [e.hydrus_hash for e in entries if e.hydrus_hash]
        if not hashes:
            message.information(
                self, "Show in Hydrus",
                "None of the selected files have a hash yet, so there is nothing to "
                "look up.\n\nHashes are worked out when files are added; "
                "Settings > General > Hash source controls where they come from.",
            )
            return

        try:
            known = client.filter_known_hashes(hashes)
        except HydrusError as exc:
            message.warning(self, "Show in Hydrus", f"Could not reach Hydrus: {exc}")
            return

        showable = [h for h in hashes if h in known]
        if not showable:
            message.information(
                self, "Show in Hydrus",
                "Hydrus doesn't have any of the selected files, so there is nothing "
                "for it to show.\n\nThat usually means they were added from outside "
                "Hydrus and haven't been sent to it yet.",
            )
            return

        page_name = (
            entries[0].filename if len(showable) == 1
            else f"Hatate: {len(showable)} files"
        )
        try:
            client.show_files_in_client(showable, page_name)
        except HydrusError as exc:
            self._explain_show_in_hydrus_failure(exc)
            return

        skipped = len(entries) - len(showable)
        note = f" ({skipped} not in Hydrus)" if skipped else ""
        log.info("Opened a Hydrus page for %d file(s)%s", len(showable), note)
        self.status_label.setText(
            f"Showing {len(showable)} file(s) in Hydrus{note}"
        )

    def _explain_show_in_hydrus_failure(self, exc: HydrusError):
        """Turns the two failures peculiar to this action into advice.

        Both are configuration rather than faults, and both produce a
        message that means nothing on its own: a 403 because this is the
        only thing here needing the Manage Pages permission, so a key set
        up for importing has never needed it; a 404 because the endpoint
        that opens a page is a recent addition and an older client simply
        does not have it.
        """
        text = str(exc)
        if "403" in text:
            message.warning(
                self, "Show in Hydrus",
                "Hydrus refused this because the access key lacks the "
                "\"manage pages\" permission.\n\n"
                "This is the only thing here that needs it, so a key set up for "
                "importing won't have it yet. Add it in Hydrus under services > "
                "review services > client api, then try again.",
            )
            return
        if "404" in text:
            message.warning(
                self, "Show in Hydrus",
                "This Hydrus client is too old for this: opening a page through the "
                "API was added after your version.\n\n"
                "Everything else here works as before - only this one action needs "
                "the newer endpoint.",
            )
            return
        message.warning(self, "Show in Hydrus", f"Hydrus could not open the page: {exc}")

    def _delete_rows_from_hydrus(self, entries: List[ImageEntry]):
        """Asks Hydrus to delete these files from its own database.

        The point of going through Hydrus rather than deleting the files:
        for anything in Hydrus's store, the file on disk IS Hydrus's copy.
        Removing it behind Hydrus's back leaves the record in place, so
        Hydrus keeps expecting a file that no longer exists. Telling
        Hydrus instead keeps its database and its storage in agreement.

        Hydrus moves them to its trash rather than erasing them, so this
        stays undoable from Hydrus's own interface until the trash is
        emptied - which is why this asks once and doesn't belabour it.
        """
        entries = self._live_entries(entries)
        if not entries:
            return
        if not self.settings.hydrus.access_key:
            message.warning(self, "Delete from Hydrus",
                            "Set your Hydrus access key in Settings first.")
            return

        client = HydrusClient(self.settings.hydrus)
        hashes = [e.hydrus_hash for e in entries if e.hydrus_hash]
        try:
            states = client.deletion_states(hashes) if hashes else {}
        except HydrusError as exc:
            message.warning(self, "Delete from Hydrus", f"Could not reach Hydrus: {exc}")
            return

        deletable = [e for e in entries if states.get(e.hydrus_hash or "") == "present"]
        if not deletable:
            message.information(
                self, "Delete from Hydrus",
                "Hydrus isn't holding any of the selected files, so there is nothing for "
                "it to delete.\n\nThat usually means they were added from outside Hydrus, "
                "or have already been deleted from it.",
            )
            return

        skipped = len(entries) - len(deletable)
        detail = "\n".join(e.filename for e in deletable[:15])
        if len(deletable) > 15:
            detail += f"\n… and {len(deletable) - 15} more"

        box = message.build(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Delete from Hydrus")
        box.setText(
            f"Tell Hydrus to delete {len(deletable)} file(s)?"
            + (f"\n\n{skipped} of the selected file(s) aren't in Hydrus and will be left alone."
               if skipped else "")
        )
        box.setInformativeText(
            "Hydrus moves them to its trash, so you can still get them back from Hydrus "
            "until the trash is emptied.\n\n"
            "They'll also be taken out of this list."
        )
        box.setDetailedText(detail)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if self._run_dialog(box) != QMessageBox.StandardButton.Yes:
            self.status_label.setText("Delete cancelled")
            return

        try:
            asked = client.delete_files(
                # deletable only holds entries whose hash was a key in
                # `states`, and that dict's keys all came from `hashes`
                # above, which is already filtered to truthy hashes - but
                # hydrus_hash is Optional[str] on ImageEntry, so this
                # spells that out for the type checker too.
                [e.hydrus_hash for e in deletable if e.hydrus_hash],
                reason="Deleted from hatate-linux",
            )
        except HydrusError as exc:
            log.error("Delete from Hydrus failed: %s", exc)
            message.warning(self, "Delete from Hydrus", f"Hydrus refused the delete: {exc}")
            return

        # Confirm rather than assume: the delete endpoint answers with an
        # empty body whatever happened, so the only way to know is to ask
        # Hydrus what it now thinks of those files.
        try:
            after = client.deletion_states(asked)
        except HydrusError:
            after = {}
        gone = {h for h, state in after.items() if state in ("trashed", "deleted")}
        confirmed = [e for e in deletable if e.hydrus_hash in gone]
        stubborn = len(asked) - len(confirmed)

        log.info("Hydrus deleted %d of %d requested file(s)", len(confirmed), len(asked))
        if confirmed:
            self._remove_entries(confirmed, reason="deleted from Hydrus")

        summary = f"Hydrus deleted {len(confirmed)} file(s) (now in its trash)."
        if stubborn:
            summary += (f"\n\n{stubborn} did not end up deleted - Hydrus accepted the request "
                        "but still reports holding them. See Help > View Logs.")
        if skipped:
            summary += f"\n\n{skipped} were left alone because Hydrus doesn't hold them."
        message.information(self, "Delete from Hydrus", summary)
        self.status_label.setText(f"Hydrus deleted {len(confirmed)} file(s)")

    def _remove_rows(self, entries: List[ImageEntry]):
        """Removes the given rows from the working list (just this app's
        list - doesn't touch the files on disk or in Hydrus)."""
        # Removed by identity, not position - a row number is a position
        # on screen, so with a filter on it does not index self.entries at
        # all, and one may already be gone if it was removed elsewhere
        # while a menu offering this was still open.
        to_remove = self._live_entries(entries)
        if not to_remove:
            return
        removed_ids = {id(e) for e in to_remove}
        # Where to land once they're gone - see _remove_entries' keep_place.
        successor = self._successor_surviving(removed_ids)
        removed_names = [e.filename for e in to_remove]
        removed_count = len(removed_ids)

        self.entries[:] = [e for e in self.entries if id(e) not in removed_ids]

        for eid in removed_ids:
            self._thumb_icon_cache.pop(eid, None)
            self._local_pixmap_cache.pop(eid, None)

        log.info("Removed %d file(s) from the list: %s", removed_count, removed_names)
        self._refresh_table()
        if successor is not None and not self.table.selectionModel().selectedRows():
            row = self.table_model.row_of(successor)
            if row is not None:
                self.select_table_row(row)
        self._sync_preview_to_selection()

        self.status_label.setText(f"Removed {removed_count} file(s) from the list")

    def _remove_entries(self, entries_to_remove: List[ImageEntry], reason: str = "imported",
                        keep_place: bool = True):
        """Like _remove_rows, but takes entry objects directly instead of
        table row indices - used after a successful Hydrus send, where
        settings.remove_after_import may call for dropping just-sent
        entries regardless of what's currently selected in the table.

        `keep_place`: if the selected row is among those removed, select
        the image that followed it instead of leaving nothing selected.
        On by default: with nothing selected, J jumps to the top of the
        list and K to the bottom. It used to be off for auto-import, so
        re-searching the row you were on - and having it auto-imported
        and removed - sent the next J back to row 1.
        Worked out by identity before removing, not by row number - a
        batch arriving from Hydrus can take out rows above yours too, so
        "the same row number" would skip past images you never saw."""
        if not entries_to_remove:
            return
        ids_to_remove = {id(e) for e in entries_to_remove}
        successor = self._successor_surviving(ids_to_remove) if keep_place else None

        self.entries = [e for e in self.entries if id(e) not in ids_to_remove]

        for eid in ids_to_remove:
            self._thumb_icon_cache.pop(eid, None)
            self._local_pixmap_cache.pop(eid, None)

        log.info("Removed %d %s file(s) from the list", len(entries_to_remove), reason)
        self._refresh_table()
        if successor is not None and not self.table.selectionModel().selectedRows():
            row = self.table_model.row_of(successor)
            if row is not None:
                self.select_table_row(row)
        self._sync_preview_to_selection()

    def _successor_surviving(self, ids_to_remove) -> Optional[ImageEntry]:
        """The visible entry to land on if the selection is about to be
        removed: the next one down that survives, else the nearest above.
        None when the selection survives (it will simply be reselected)
        or there is no selection to keep a place for."""
        rows = sorted(i.row() for i in self.table.selectionModel().selectedRows())
        if not rows:
            return None
        selected = self.table_model.entries_at(rows)
        if any(id(e) not in ids_to_remove for e in selected):
            return None
        total = self.table_model.rowCount()
        for row in list(range(rows[0] + 1, total)) + list(range(rows[0] - 1, -1, -1)):
            entry = self.table_model.entry_at(row)
            if entry is not None and id(entry) not in ids_to_remove:
                return entry
        return None

    def _sync_preview_to_selection(self):
        """Points the preview panel at whatever is selected now - which
        after a removal is usually nothing.

        Rebuilding the table resets the model, and a reset drops the
        selection whether or not the selected row was one of the ones
        removed. The panel used to be cleared only when the selected
        entry had itself been removed, so removing any OTHER row left a
        picture on screen with nothing selected behind it - including
        after an auto-import, where rows disappear on their own.
        """
        self._on_selection_changed()

    def select_table_row(self, row: int) -> None:
        """Selects one visible row and scrolls it into view.

        `row` is a row of the FILTERED model, which is what the table
        shows - selecting by an index into self.entries would land
        somewhere else entirely whenever a filter is active.
        """
        if not 0 <= row < self.table_model.rowCount():
            return
        last_col = self.table_model.columnCount() - 1
        selection = QItemSelection(
            self.table_model.index(row, 0), self.table_model.index(row, last_col)
        )
        self.table.selectionModel().select(
            selection, QItemSelectionModel.SelectionFlag.ClearAndSelect
        )
        # The current index drives keyboard focus, so it has to follow the
        # selection or the arrow keys would carry on from where the
        # selection used to be.
        self.table.selectionModel().setCurrentIndex(
            self.table_model.index(row, 0),
            QItemSelectionModel.SelectionFlag.NoUpdate,
        )
        self.table.scrollTo(self.table_model.index(row, 0))

    def action_open_matched_url(self):
        """Opens the selected image's match in the browser."""
        review_shortcuts.open_matched_url(self, self._current_entry())

    def action_show_in_queue(self):
        """Switches to Queue mode and selects the current image's row (if any)."""
        entry = self._current_entry()
        row = None
        if entry is not None:
            row = self.table_model.row_of(entry)
        self.set_mode("queue")
        QApplication.processEvents()
        if row is not None:
            self.select_table_row(row)

    def _current_entry(self) -> Optional[ImageEntry]:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.table_model.entry_at(rows[0].row())

    def _on_selection_changed(self):
        entry = self._current_entry()
        self._update_preview(entry)
        self._refresh_tag_list(entry)
        if entry is not None and entry.selected_candidate is not None:
            # Covers a restored session, where the match's picture was
            # never on disk to begin with - see _ensure_candidate_loaded.
            self._ensure_candidate_loaded(entry, entry.selected_candidate_index)

    def _remove_selected_tags(self):
        entry = self._current_entry()
        if not entry:
            return
        to_remove = {item.data(Qt.ItemDataRole.UserRole).key() for item in self.tag_list.selectedItems()}
        entry.tags = [t for t in entry.tags if t.key() not in to_remove]
        self._on_selection_changed()
        self._refresh_table()

    # ------------------------------------------------------------------
    # Hydrus send
    # ------------------------------------------------------------------
    def action_send_to_hydrus(self):
        rows = self.table.selectionModel().selectedRows()
        place = min((i.row() for i in rows), default=None)
        if not rows:
            message.information(self, "Send to Hydrus", "Select at least one row.")
            return
        if not self.settings.hydrus.access_key:
            message.warning(self, "Send to Hydrus", "Set your Hydrus access key in Settings first.")
            return

        client = HydrusClient(self.settings.hydrus)
        sent, warned, failed = 0, 0, 0
        failures: List[str] = []
        warnings: List[str] = []
        succeeded_entries: List[ImageEntry] = []

        for entry in self.table_model.entries_at(idx.row() for idx in rows):
            result = send_file_upload(entry, client, self.settings)
            if not result.success:
                failed += 1
                failures.append(f"{entry.filename}: {result.error}")
                continue
            succeeded_entries.append(entry)
            if result.warning:
                warned += 1
                warnings.append(f"{entry.filename}: {result.warning}")
            else:
                sent += 1

        if self.settings.remove_after_import:
            self._remove_entries(succeeded_entries, reason="sent-to-Hydrus")
        self._refresh_table(keep_place=place)
        status = f"Sent {sent} file(s) to Hydrus"
        if warned:
            status += f", {warned} with warnings"
        if failed:
            status += f", {failed} failed"
        if self.settings.remove_after_import and succeeded_entries:
            status += f" ({len(succeeded_entries)} removed from list)"
        self.status_label.setText(status)

        problems = failures + warnings
        if problems:
            detail = "\n".join(problems[:10])
            if len(problems) > 10:
                detail += f"\n… and {len(problems) - 10} more (see Help > View Logs)"
            box = message.build(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("Send to Hydrus")
            summary_parts = []
            if failed:
                summary_parts.append(f"{failed} file(s) failed to import")
            if warned:
                summary_parts.append(f"{warned} file(s) sent with warnings")
            box.setText(", ".join(summary_parts) + ".")
            box.setDetailedText(detail)
            self._run_dialog(box)

    def action_import_url_to_hydrus(self):
        """Sends the matched URL to Hydrus's own downloader (POST
        /add_urls/add_url) instead of uploading our local copy - Hydrus
        fetches the file itself via its own site parsers. Doesn't touch
        the local file at all, so it works even for entries with no
        hydrus_hash yet."""
        rows = self.table.selectionModel().selectedRows()
        place = min((i.row() for i in rows), default=None)
        if not rows:
            message.information(self, "Send URL to Hydrus's Importer", "Select at least one row.")
            return
        if not self.settings.hydrus.access_key:
            message.warning(self, "Send URL to Hydrus's Importer", "Set your Hydrus access key in Settings first.")
            return

        client = HydrusClient(self.settings.hydrus)
        queued, skipped, failed = 0, 0, 0
        problems: List[str] = []
        queued_entries: List[ImageEntry] = []
        # Where Hydrus has no downloader for a site but Hatate can reach the
        # original, Hatate downloads and sends it instead - see
        # hydrus_import.send_url_or_download. Those are done on the spot.
        downloaded_entries: List[ImageEntry] = []
        tmp_dir = tempfile.mkdtemp(prefix="hatate-linux-dl-")

        try:
            for entry in self.table_model.entries_at(idx.row() for idx in rows):
                result = send_url_or_download(entry, client, self.settings, tmp_dir)
                if result.skipped_reason:
                    skipped += 1
                    continue
                if not result.success:
                    failed += 1
                    problems.append(f"{entry.filename}: {result.error}")
                    continue
                if result.confirmed:
                    downloaded_entries.append(entry)
                    continue
                queued += 1
                queued_entries.append(entry)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        downloaded = len(downloaded_entries)
        if downloaded and self.settings.remove_after_import:
            self._remove_entries(downloaded_entries, reason="downloaded-and-sent-to-Hydrus",
                                 keep_place=True)

        if problems:
            detail = "\n".join(problems[:10])
            if len(problems) > 10:
                detail += f"\n… and {len(problems) - 10} more (see Help > View Logs)"
            box = message.build(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("Send URL to Hydrus's Importer")
            box.setText(f"{failed} URL(s) were not sent to Hydrus.")
            box.setDetailedText(detail)
            self._run_dialog(box)

        # Reflect the new sent/queued state in the table - this path
        # previously never refreshed, so the Sent column would have gone
        # on showing these rows as unsent until something else redrew.
        self._refresh_table(keep_place=place)

        if not self.settings.remove_after_import or not queued_entries:
            status = f"Queued {queued} URL(s) with Hydrus's importer"
            if downloaded:
                status += f", {downloaded} downloaded by Hatate (Hydrus has no downloader for the site)"
            if skipped:
                status += f", {skipped} skipped (no matched URL)"
            if failed:
                status += f", {failed} not sent"
            self.status_label.setText(status)
            if not problems and (queued or downloaded):
                lines = []
                if queued:
                    lines.append(f"Queued {queued} URL(s). Check Hydrus's own downloader page for "
                                 "progress - it fetches and imports the file itself, which can "
                                 "take a moment.")
                if downloaded:
                    lines.append(f"Hydrus has no downloader for {downloaded} of them, so Hatate "
                                 "downloaded the original itself and sent it with its tags and link.")
                message.information(self, "Send URL to Hydrus's Importer", "\n\n".join(lines))
            return

        # remove_after_import is on: don't just trust that Hydrus *accepted*
        # the request - actually poll it to confirm the file was imported
        # before removing anything from the list, since Hydrus's downloader
        # runs asynchronously and "queued" is not the same as "done".
        self.status_label.setText(
            f"Queued {queued} URL(s)"
            + (f", {downloaded} downloaded by Hatate" if downloaded else "")
            + " - confirming with Hydrus before removing from the list…"
        )
        entries_with_urls = [(e, normalize_url_for_hydrus(e.matched_url)) for e in queued_entries]
        self._pending_poll_confirmed = []
        self._pending_poll_unconfirmed = []
        self._pending_poll_warnings = []
        self._workers.retire(self.hydrus_import_poll_worker)
        self.hydrus_import_poll_worker = HydrusImportPollWorker(
            client, entries_with_urls,
            timeout=self.settings.url_import_confirm_timeout,
            interval=self.settings.url_import_confirm_interval,
            # Only read for set_hydrus_duplicate_relationships and
            # write_hydrus_provenance_note - the worker does both itself, on
            # its own thread, because recording a relationship in the
            # handler below would download and hash a full-resolution file
            # on the GUI thread.
            settings=self.settings,
        )
        self.hydrus_import_poll_worker.entry_resolved.connect(self._on_hydrus_import_resolved)
        self.hydrus_import_poll_worker.wait_countdown.connect(self._on_wait_countdown)
        self.hydrus_import_poll_worker.finished_all.connect(self._on_hydrus_import_poll_finished)
        self.hydrus_import_poll_worker.start()

    def _on_hydrus_import_resolved(self, entry: ImageEntry, confirmed_hash: Optional[str],
                                   warning: Optional[str] = None):
        if confirmed_hash:
            entry.hydrus_hash = confirmed_hash
            entry.hydrus_import_confirmed = True
            if warning:
                # The import itself stands - this says only that something
                # that follows it didn't (a duplicate relationship that
                # couldn't be recorded). Collected for the summary rather
                # than raised as a modal per entry.
                entry.error_message = warning
                self._pending_poll_warnings.append(f"{entry.filename}: {warning}")
            self._pending_poll_confirmed.append(entry)
        else:
            entry.error_message = "Hydrus hadn't confirmed the import within the poll window"
            self._pending_poll_unconfirmed.append(entry)
        self._refresh_entry_row(entry)

    def _on_hydrus_import_poll_finished(self):
        if self._closing:
            # Skipping this drops the confirmed-imported entries this would
            # have removed from the list, but the session being saved right
            # now will carry them as "still queued" and the automatic
            # reconcile pass on next launch (_start_hydrus_reconcile) is
            # exactly what settles that - see _on_missing_files_checked for
            # why this bails at all (DAN-128).
            return
        confirmed = self._pending_poll_confirmed
        unconfirmed = self._pending_poll_unconfirmed
        warnings = self._pending_poll_warnings
        if confirmed:
            self._remove_entries(confirmed, reason="confirmed-imported-by-Hydrus",
                                 keep_place=True)

        status = f"Confirmed {len(confirmed)} Hydrus import(s), removed from list"
        if unconfirmed:
            status += f"; {len(unconfirmed)} still processing - kept in list, check Hydrus's downloader page"
        if warnings:
            status += f"; {len(warnings)} not marked as duplicates"
        self.status_label.setText(status)
        if warnings:
            # These rows imported fine and have been removed, so this is
            # the last chance to say the relationship the user asked for
            # wasn't recorded. Shown once for the batch, not per row.
            detail = "\n".join(warnings[:10])
            if len(warnings) > 10:
                detail += f"\n… and {len(warnings) - 10} more (see Help > View Logs)"
            box = message.build(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("Send URL to Hydrus's Importer")
            box.setText(f"{len(warnings)} file(s) imported, but were not marked as duplicates "
                        "of your copies.")
            box.setDetailedText(detail)
            self._run_dialog(box)
        self._pending_poll_confirmed = []
        self._pending_poll_unconfirmed = []
        self._pending_poll_warnings = []

    def action_download_and_send_to_hydrus(self):
        """Downloads the actual matched image's full-resolution file
        ourselves (via the direct_file_url a booru parser extracted, not
        the thumbnail and not the local file), then uploads those bytes
        to Hydrus with the source URL associated and tags attached -
        useful when your local copy is lower quality than the source, or
        when you'd rather not depend on Hydrus's own downloader working
        for that site."""
        rows = self.table.selectionModel().selectedRows()
        place = min((i.row() for i in rows), default=None)
        if not rows:
            message.information(self, "Download + Send to Hydrus", "Select at least one row.")
            return
        if not self.settings.hydrus.access_key:
            message.warning(self, "Download + Send to Hydrus", "Set your Hydrus access key in Settings first.")
            return

        client = HydrusClient(self.settings.hydrus)
        # Each matched file is downloaded here at FULL resolution before
        # being handed to Hydrus, so this fills up fast - and on most
        # Linux systems /tmp is RAM-backed, which means leaving these
        # behind costs memory, not just disk, until the next reboot.
        # Hydrus reads the bytes synchronously inside download_and_send,
        # so nothing needs them once the loop is done.
        tmp_dir = tempfile.mkdtemp(prefix="hatate-linux-dl-")
        sent, skipped, failed = 0, 0, 0
        problems: List[str] = []
        succeeded_entries: List[ImageEntry] = []

        try:
            for entry in self.table_model.entries_at(idx.row() for idx in rows):
                result = download_and_send(entry, client, self.settings.search_timeout, tmp_dir, self.settings)
                if result.skipped_reason:
                    skipped += 1
                    log.debug(
                        "No direct file URL available for %s, skipping download-and-send "
                        "(needs 'Retrieve tags from booru page' enabled and a supported site)",
                        entry.filename,
                    )
                    continue
                if not result.success:
                    failed += 1
                    problems.append(f"{entry.filename}: {result.error}")
                    continue
                sent += 1
                succeeded_entries.append(entry)
        finally:
            # In a finally so an early failure - the case that creates the
            # directory and then does least with it - is not the one that
            # leaks it.
            shutil.rmtree(tmp_dir, ignore_errors=True)
            log.debug("Cleaned up download staging directory %s", tmp_dir)

        if self.settings.remove_after_import:
            self._remove_entries(succeeded_entries, reason="downloaded-and-sent-to-Hydrus")
        self._refresh_table(keep_place=place)
        status = f"Downloaded + sent {sent} file(s) to Hydrus"
        if skipped:
            status += f", {skipped} skipped (no direct image URL available)"
        if self.settings.remove_after_import and succeeded_entries:
            status += f" ({len(succeeded_entries)} removed from list)"
        if failed:
            status += f", {failed} failed"
        self.status_label.setText(status)

        if problems:
            detail = "\n".join(problems[:10])
            if len(problems) > 10:
                detail += f"\n… and {len(problems) - 10} more (see Help > View Logs)"
            box = message.build(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("Download + Send to Hydrus")
            box.setText(f"{failed} file(s) failed.")
            box.setDetailedText(detail)
            self._run_dialog(box)
        elif skipped and not sent:
            message.information(
                self, "Download + Send to Hydrus",
                "None of the selected images have a known direct image URL to download. "
                "This needs 'Retrieve tags from booru page' enabled in Settings and a "
                "match from a supported site.",
            )

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def action_open_settings(self):
        dialog = SettingsDialog(self.settings, self)
        # Only enabled/port/token need the server itself to actually
        # rebind - every other MCP setting (the tier switches, dry_run)
        # is read live off self.settings.mcp by core/mcp_tools.gate() on
        # the very next tool call, with no restart at all. Restarting
        # unconditionally on every Settings save would drop a connected
        # MCP client's session over an unrelated change, e.g. a new
        # Hydrus key.
        old_mcp = (self.settings.mcp.enabled, self.settings.mcp.port, self.settings.mcp.token)
        if self._run_dialog(dialog):
            dialog.apply_to_settings()
            # Pick up a changed autosave interval (or an on/off flip) now
            # rather than at the next launch.
            self._autosaver.apply_settings(self.settings)
            # Same for edited keyboard bindings - a shortcut you just set
            # and then have to restart to use reads as one that failed.
            review_shortcuts.install(self)
            # The button labels and position-strip hint bake in the
            # bindings at construction time, so a rebind needs an explicit
            # refresh here too - otherwise the label keeps showing the old
            # key even though it stopped working the moment install() ran.
            self._refresh_review_strip()
            # The Activity page states the engine line-up, which this
            # dialog is exactly what changes.
            self.refresh_engines()
            new_mcp = (self.settings.mcp.enabled, self.settings.mcp.port, self.settings.mcp.token)
            if new_mcp != old_mcp:
                if not self._mcp_server.restart(self.settings.mcp):
                    if self.settings.mcp.enabled and self._mcp_server.last_error:
                        log.warning("MCP server did not restart: %s", self._mcp_server.last_error)

    def action_set_theme(self, mode: str):
        """Switches the theme now, without a restart.

        Qt re-polishes every widget when the application stylesheet is
        replaced, so there is nothing to rebuild - the running window
        simply comes back in the other palette, selection and all.
        apply_theme is a one-line setStyleSheet; there is no shadow or
        gradient machinery left to re-walk.
        """
        if mode not in theme.MODES:
            return
        self.settings.theme = mode
        self.settings.save()
        resolved = theme.resolve_mode(mode)
        widgets.apply_theme(self, resolved)
        action = getattr(self, "_theme_actions", {}).get(mode)
        if action is not None:
            action.setChecked(True)
        log.debug("Theme set to %s (drawn as %s)", mode, resolved)

    def action_edit_conditions(self):
        dialog = MatchConditionsDialog(self.settings.match_conditions, self)
        if self._run_dialog(dialog):
            dialog.apply_to(self.settings.match_conditions)
            self.settings.save()

    def eventFilter(self, obj, event):
        """Keeps the Queue's default column widths fitted to the viewport.

        Until the user touches a column divider (or a saved layout is
        restored) the widths are a share of the viewport, re-worked on
        every resize: the first layout is not the final size, and a
        maximised window should not leave the table short of its edge.
        """
        if self._default_widths_pending:
            if obj is self.table.horizontalHeader().viewport():
                if event.type() == QEvent.Type.MouseButtonPress:
                    self._default_widths_pending = False
            elif (obj is self.table.viewport() and event.type() == QEvent.Type.Resize
                    and event.size().width() >= MIN_VIEWPORT_FOR_DEFAULT_WIDTHS):
                thumb_width = THUMB_COLUMN_SIZE + 20
                self.table.setColumnWidth(COL_THUMB, thumb_width)
                for col, width in default_column_widths(event.size().width(), thumb_width).items():
                    self.table.setColumnWidth(col, width)
        return super().eventFilter(obj, event)

    def showEvent(self, event):
        """First real layout happens here - before this the table has no
        meaningful height, so rowAt() can't tell which rows are visible.
        A session restored at startup therefore has to wait until now to
        work out what to generate."""
        super().showEvent(event)
        lazylog.debug("window shown - triggering an initial viewport pass")
        self._schedule_viewport_thumbnails("window shown")

    def resizeEvent(self, event):
        """A taller window shows more rows, which need thumbnails too."""
        super().resizeEvent(event)
        self._schedule_viewport_thumbnails("window resized")

    def closeEvent(self, event):
        # First, so every worker-completion slot checked below sees it even
        # if its signal was already queued when we got here (DAN-128).
        self._closing = True
        # Stop accepting new MCP tool calls before anything else is torn
        # down - a call still marshaled onto this thread when a worker it
        # depends on (self.worker, the Hydrus client's event loop) goes
        # away would otherwise hit whatever that teardown leaves behind,
        # rather than a clean "the app is closing" refusal.
        self._mcp_server.stop()
        # The Pawchive Index window owns its own indexing thread; closing it
        # stops that thread before the window it belongs to goes away.
        pawchive_dialog = getattr(self, "_pawchive_index_dialog", None)
        if pawchive_dialog is not None:
            pawchive_dialog.close()
        # Before the workers are retired: a run stopped part-way never
        # reaches _on_worker_finished, and one of them can still raise an
        # engine alert on its way out.
        engine_alerts.unsubscribe(self._engine_alert_listener)
        # Ask every worker to stop and give it a couple of seconds. Anything
        # still running is kept referenced (see gui/worker_lifecycle.py)
        # rather than collected, since destroying a live QThread aborts the
        # process - a crash on quit that would otherwise land after the
        # session was saved.
        self._workers.retire_attrs(self, (
            "worker", "candidate_worker", "hydrus_lookup_worker",
            "hydrus_import_poll_worker", "hydrus_reconcile_worker", "missing_file_worker",
            "upscale_check_worker", "file_hash_worker", "availability_worker",
            "thumbnail_worker",
        ), grace_ms=2000)

        # Waits for an in-flight write and stops the timer. Before the
        # final save below, deliberately - see SessionAutosaver.shutdown.
        self._autosaver.shutdown()
        # Closes the Chromium the Google Lens engine drives, if one was
        # ever started.
        lens_browser.shutdown()
        self._save_table_header_state()
        if self.settings.restore_session_on_start:
            save_session(self.entries)
        self.settings.save()
        # Last, now that everything that could still fail has already run -
        # this is what tells the NEXT launch that this one ended cleanly
        # (DAN-485).
        crashlog.mark_clean_shutdown()
        event.accept()
