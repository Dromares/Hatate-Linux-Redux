"""The bridge between the MCP server's own thread and the Qt GUI thread.

core/mcp_server.py's tool handlers run on the MCP server's own thread
(its streamable-HTTP transport, not the Qt event loop) - a tool call can
arrive at any moment, including in the middle of the user coming back
and clicking around. Nothing in a handler may touch a QWidget or the
table model directly from that thread; Qt's own rule, not a style
preference. MainThreadInvoker is the one bridge across that line: it
runs a callable on the Qt main thread and blocks the calling thread
until the result (or exception) comes back, with a timeout so a wedged
GUI thread cannot hang an MCP tool call forever.

McpToolHandlers is everything downstream of that: it gates each call
against the live McpSettings, applies dry_run, audits every call
(allowed, refused, or dry-run alike), and implements the tool bodies
themselves - mostly by calling the SAME MainWindow/core functions the
menu and keyboard shortcuts already use (gui/review_shortcuts.py), minus
anything that would pop a modal dialog. A dialog waits for a click that,
with the user away, is never coming - and because Qt's dialog exec()
runs its own nested event loop on the GUI thread, it would not just
hang this one call, it would hang every other queued GUI event,
including the next tool call's marshaled invocation.
"""
from __future__ import annotations

import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot

from core import mcp_audit, mcp_images, mcp_tools, ranking
from core.applog import get_logger
from core.hydrus_client import HydrusClient
from core.hydrus_import import download_and_send, send_file_upload, send_url_or_download
from core.models import ImageEntry
from core.search_cache import drop_cached_result

log = get_logger("gui.mcp_bridge")

# How long a marshaled call may take before MainThreadInvoker gives up on
# it. Generous: these calls are meant to be quick (no network I/O inside
# them, except the Hydrus-send tools below, where Hydrus is normally
# loopback itself), but a wedged GUI thread must produce a clear error
# rather than hang the MCP server's own thread forever.
DEFAULT_TIMEOUT_SECONDS = 20.0
HYDRUS_SEND_TIMEOUT_SECONDS = 60.0
# How long research() waits for the search it starts to finish, beyond
# which it reports "still running" rather than blocking the tool call
# indefinitely - a batch of several images at the configured pace is
# genuinely minutes long, not a hung call.
RESEARCH_TIMEOUT_PER_IMAGE_SECONDS = 90.0
RESEARCH_TIMEOUT_FLOOR_SECONDS = 60.0


class MainThreadTimeout(RuntimeError):
    """The Qt main thread did not run the marshaled call within its timeout."""


class MainThreadInvoker(QObject):
    """Runs a callable on the thread that owns this QObject (the GUI
    thread, since it is always constructed there) and returns its result
    to whichever thread asked.

    Implemented with a BlockingQueuedConnection signal rather than
    QMetaObject.invokeMethod: PyQt already marshals a cross-thread signal
    emission onto the receiving thread automatically, and
    BlockingQueuedConnection additionally blocks the emitting thread
    until the slot has run - which is exactly "a queued signal ... with
    a result handed back" from the ticket, without the C++-shaped
    Q_ARG/Q_RETURN_ARG boilerplate invokeMethod needs for an arbitrary
    Python callable.
    """

    _invoke = pyqtSignal(object, dict)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._invoke.connect(  # type: ignore[call-arg]  # PyQt6 stub gap: connect()'s stub
            # takes only a slot, but the real binding also accepts the
            # connection-type positional argument used here.
            self._run, Qt.ConnectionType.BlockingQueuedConnection,
        )
        # A small pool rather than a single lock: this emits the BLOCKING
        # signal from a throwaway helper thread instead of the caller's
        # own thread, so a caller that gives up after `timeout` is not
        # left permanently stuck inside a blocking Qt emit that nothing
        # will ever cancel - only this helper thread is.
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mcp-invoke")

    @pyqtSlot(object, dict)
    def _run(self, fn: Callable[[], Any], box: Dict[str, Any]) -> None:
        try:
            box["result"] = fn()
        except Exception as exc:  # noqa: BLE001 - reported to the caller, not swallowed
            box["error"] = exc

    def call(self, fn: Callable[[], Any], timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Any:
        """Runs `fn` on the GUI thread and returns what it returned.

        Safe to call from the GUI thread itself too (BlockingQueuedConnection
        is only blocking across threads; PyQt delivers a same-thread emit
        directly, re-entrantly), though in practice every MCP tool call
        arrives from the server's own thread.
        """
        if threading.current_thread() is threading.main_thread():
            return fn()

        box: Dict[str, Any] = {}

        def _emit() -> None:
            self._invoke.emit(fn, box)

        future = self._executor.submit(_emit)
        try:
            future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            raise MainThreadTimeout(
                f"Timed out after {timeout:.0f}s waiting for the GUI thread"
            ) from exc
        if "error" in box:
            raise RuntimeError(str(box["error"])) from box["error"]
        return box.get("result")

    def shutdown(self) -> None:
        """Releases the helper thread pool. Called from
        McpServerController.stop() - after the server has stopped
        accepting new calls, nothing should still be submitting work
        through this invoker, so this does not wait for anything."""
        self._executor.shutdown(wait=False, cancel_futures=True)


class McpToolHandlers:
    """Implements every MCP tool body, gated and audited.

    One instance lives for as long as the server does, holding the
    MainWindow it drives. Every public method here corresponds to
    exactly one tool in core/mcp_tools.TOOLS; core/mcp_server.py's job is
    purely to adapt these plain-dict results into MCP's content-block
    wire format, so this class has no `mcp` import and can be exercised
    in tests without one.
    """

    def __init__(self, window: Any) -> None:
        self.window = window
        self.invoker = MainThreadInvoker(window)
        # Overridable in tests so the audit log never touches the real
        # config directory.
        self.audit_path: Path = mcp_audit.AUDIT_LOG_FILE

    # ------------------------------------------------------------------
    # Gating, dry-run and audit plumbing shared by every tool
    # ------------------------------------------------------------------
    def _mcp_settings(self):
        return self.window.settings.mcp

    def _run_tool(
        self, tool_name: str, arguments: Dict[str, Any],
        perform: Callable[[], Dict[str, Any]],
        row: Optional[int] = None, candidate_index: Optional[int] = None,
    ) -> Dict[str, Any]:
        settings = self._mcp_settings()
        spec = mcp_tools.TOOLS_BY_NAME[tool_name]
        refusal = mcp_tools.gate(tool_name, settings)
        if refusal:
            mcp_audit.record(
                tool=tool_name, tier=spec.tier, allowed=False, arguments=arguments,
                reason=refusal, row=row, candidate_index=candidate_index,
                path=self.audit_path, max_bytes=settings.audit_log_max_bytes,
            )
            return {"allowed": False, "reason": refusal}

        dry_run = settings.dry_run and mcp_tools.is_dry_runnable(tool_name)
        if dry_run:
            result: Dict[str, Any] = {
                "dry_run": True,
                "would_call": tool_name,
                "arguments": arguments,
                "note": "dry_run is on: nothing was changed.",
            }
        else:
            result = perform()
            result.setdefault("dry_run", False)

        mcp_audit.record(
            tool=tool_name, tier=spec.tier, allowed=True, arguments=arguments,
            dry_run=dry_run, row=row, candidate_index=candidate_index, outcome=result,
            path=self.audit_path, max_bytes=settings.audit_log_max_bytes,
        )
        return {"allowed": True, **result}

    def _entry_at(self, row: int) -> Optional[ImageEntry]:
        """Looks up a row by its index into the FULL queue (window.entries),
        not the GUI's currently-filtered table view - so MCP addressing
        never shifts under the AI because a human left a filter active,
        or changed it mid-pass."""
        entries = self.window.entries
        if 0 <= row < len(entries):
            return entries[row]
        return None

    # ------------------------------------------------------------------
    # Read tools
    # ------------------------------------------------------------------
    def list_queue(
        self, offset: int = 0, limit: int = 50, filter_text: Optional[str] = None,
    ) -> Dict[str, Any]:
        arguments = {"offset": offset, "limit": limit, "filter": filter_text}

        def perform() -> Dict[str, Any]:
            def work() -> Dict[str, Any]:
                rows: List[Dict[str, Any]] = []
                matched = 0
                for index, entry in enumerate(self.window.entries):
                    if filter_text and not _matches_filter(entry, filter_text):
                        continue
                    if matched < offset:
                        matched += 1
                        continue
                    if len(rows) >= limit:
                        matched += 1
                        continue
                    rows.append(_queue_row(index, entry))
                    matched += 1
                return {"rows": rows, "total_matching": matched, "total_in_queue": len(self.window.entries)}
            return self.invoker.call(work)

        return self._run_tool("list_queue", arguments, perform)

    def get_entry(self, row: int) -> Dict[str, Any]:
        arguments = {"row": row}

        def perform() -> Dict[str, Any]:
            def work() -> Dict[str, Any]:
                entry = self._entry_at(row)
                if entry is None:
                    return {"error": f"No row {row} (queue has {len(self.window.entries)})"}
                return {"row": row, **_entry_detail(entry)}
            return self.invoker.call(work)

        return self._run_tool("get_entry", arguments, perform, row=row)

    def get_candidates(self, row: int) -> Dict[str, Any]:
        arguments = {"row": row}

        def perform() -> Dict[str, Any]:
            def work() -> Dict[str, Any]:
                entry = self._entry_at(row)
                if entry is None:
                    return {"error": f"No row {row} (queue has {len(self.window.entries)})"}
                scored = []
                for index, candidate in enumerate(entry.candidates):
                    breakdown = ranking.score_breakdown(
                        candidate, entry.local_width, entry.local_height)
                    scored.append({
                        "candidate_index": index,
                        "url": candidate.url,
                        "source_name": candidate.source_name,
                        "engine": candidate.engine,
                        "width": candidate.width,
                        "height": candidate.height,
                        "is_selected": index == entry.selected_candidate_index,
                        "remote_available": candidate.remote_available,
                        "restricted": candidate.restricted,
                        **breakdown,
                    })
                scored.sort(key=lambda item: -item["score"])
                return {"row": row, "candidates": scored}
            return self.invoker.call(work)

        return self._run_tool("get_candidates", arguments, perform, row=row)

    def get_images(self, row: int, candidate_index: int) -> Dict[str, Any]:
        arguments = {"row": row, "candidate_index": candidate_index}

        def perform() -> Dict[str, Any]:
            def lookup() -> Any:
                entry = self._entry_at(row)
                if entry is None:
                    return None
                if not (0 <= candidate_index < len(entry.candidates)):
                    return None
                return entry, entry.candidates[candidate_index], self.window.settings

            found = self.invoker.call(lookup)
            if found is None:
                return {"error": f"No row {row} / candidate {candidate_index} to fetch"}
            entry, candidate, settings = found

            local_bytes = mcp_images.local_image_bytes(entry)
            match = mcp_images.fetch_full_resolution_match(candidate, settings, local_path=entry.path)
            return {
                "row": row,
                "candidate_index": candidate_index,
                "local_image": {"bytes": local_bytes, "mime_type": _guess_mime(entry.path)},
                "match_image": {
                    "bytes": match.data,
                    "mime_type": _guess_mime(match.url or ""),
                    "url": match.url,
                    "is_full_resolution": match.is_full_resolution,
                    "fallback_reason": match.fallback_reason,
                },
            }

        return self._run_tool("get_images", arguments, perform, row=row, candidate_index=candidate_index)

    def get_diff(self, row: int, candidate_index: int) -> Dict[str, Any]:
        arguments = {"row": row, "candidate_index": candidate_index}

        def perform() -> Dict[str, Any]:
            def lookup() -> Any:
                entry = self._entry_at(row)
                if entry is None:
                    return None
                if not (0 <= candidate_index < len(entry.candidates)):
                    return None
                return entry, entry.candidates[candidate_index], self.window.settings

            found = self.invoker.call(lookup)
            if found is None:
                return {"error": f"No row {row} / candidate {candidate_index} to compare"}
            entry, candidate, settings = found

            local_bytes = mcp_images.local_image_bytes(entry)
            match = mcp_images.fetch_full_resolution_match(candidate, settings, local_path=entry.path)
            diff = mcp_images.compute_diff(local_bytes, match.data)
            return {
                "row": row,
                "candidate_index": candidate_index,
                "diff_image": {"bytes": diff.png_bytes, "mime_type": "image/png"},
                "changed_percent": diff.changed_percent,
                "verdict": diff.verdict,
                "reliable": diff.reliable,
                "match_is_full_resolution": match.is_full_resolution,
            }

        return self._run_tool("get_diff", arguments, perform, row=row, candidate_index=candidate_index)

    def list_actions(self) -> Dict[str, Any]:
        def perform() -> Dict[str, Any]:
            settings = self._mcp_settings()
            tools = []
            for spec in mcp_tools.TOOLS:
                refusal = mcp_tools.gate(spec.name, settings)
                tools.append({
                    "tool": spec.name,
                    "tier": spec.tier,
                    "review_action_id": spec.review_action_id,
                    "enabled": refusal is None,
                    "refusal_reason": refusal,
                })
            return {"tools": tools, "dry_run": settings.dry_run}

        return self._run_tool("list_actions", {}, perform)

    # ------------------------------------------------------------------
    # Reversible local writes
    # ------------------------------------------------------------------
    def select_candidate(self, row: int, candidate_index: int) -> Dict[str, Any]:
        arguments = {"row": row, "candidate_index": candidate_index}

        def perform() -> Dict[str, Any]:
            def work() -> Dict[str, Any]:
                entry = self._entry_at(row)
                if entry is None:
                    return {"error": f"No row {row}"}
                if not (0 <= candidate_index < len(entry.candidates)):
                    return {"error": f"No candidate {candidate_index} for row {row}"}
                entry.select_candidate(candidate_index)
                self.window._refresh_entry_row(entry)
                if self.window._current_entry() is entry:
                    self.window._on_selection_changed()
                return {"row": row, "selected_candidate_index": entry.selected_candidate_index}
            return self.invoker.call(work)

        return self._run_tool(
            "select_candidate", arguments, perform, row=row, candidate_index=candidate_index)

    def toggle_reviewed(self, row: int, reviewed: bool) -> Dict[str, Any]:
        arguments = {"row": row, "reviewed": reviewed}

        def perform() -> Dict[str, Any]:
            def work() -> Dict[str, Any]:
                entry = self._entry_at(row)
                if entry is None:
                    return {"error": f"No row {row}"}
                self.window._set_rows_reviewed([entry], reviewed)
                return {"row": row, "reviewed": entry.reviewed}
            return self.invoker.call(work)

        return self._run_tool("toggle_reviewed", arguments, perform, row=row)

    # ------------------------------------------------------------------
    # Research (spends third-party search engine quota)
    # ------------------------------------------------------------------
    def research(self, rows: List[int]) -> Dict[str, Any]:
        arguments = {"rows": rows}

        def perform() -> Dict[str, Any]:
            return self._perform_research(rows)

        return self._run_tool(
            "research", arguments, perform, row=rows[0] if rows else None)

    def _perform_research(self, rows: List[int]) -> Dict[str, Any]:
        def start() -> Dict[str, Any]:
            if self.window.worker is not None and self.window.worker.isRunning():
                return {"started": False, "reason": "A search is already running"}
            looked_up = (self._entry_at(r) for r in rows)
            entries: List[ImageEntry] = [e for e in looked_up if e is not None]
            if not entries:
                return {"started": False, "reason": "No valid rows to search"}

            done_event = threading.Event()
            self.window._research_rows(entries)
            worker = self.window.worker
            if worker is None or not worker.isRunning():
                # _research_rows re-checks "already running" itself and can
                # decline for the same reason start() just did, if the GUI
                # raced it between the check above and this call.
                return {"started": False, "reason": "Search did not start"}
            worker.finished_all.connect(done_event.set)
            return {
                "started": True,
                "filenames": [e.filename for e in entries],
                "_event": done_event,
            }

        outcome = self.invoker.call(start)
        if not outcome.get("started"):
            return {"research_started": False, "reason": outcome.get("reason")}

        event: threading.Event = outcome.pop("_event")
        count = len(outcome["filenames"])
        timeout = max(
            RESEARCH_TIMEOUT_FLOOR_SECONDS, count * RESEARCH_TIMEOUT_PER_IMAGE_SECONDS)
        completed = event.wait(timeout=timeout)
        if not completed:
            return {
                "research_started": True, "completed": False,
                "filenames": outcome["filenames"],
                "note": f"Still running after {timeout:.0f}s - check get_entry/get_candidates again shortly.",
            }

        def read_results() -> List[Dict[str, Any]]:
            results = []
            for row in rows:
                entry = self._entry_at(row)
                if entry is not None:
                    results.append({"row": row, **_entry_detail(entry)})
            return results

        return {
            "research_started": True, "completed": True,
            "results": self.invoker.call(read_results),
        }

    # ------------------------------------------------------------------
    # Hydrus writes
    # ------------------------------------------------------------------
    def send_upload(self, row: int) -> Dict[str, Any]:
        arguments = {"row": row}

        def perform() -> Dict[str, Any]:
            def work() -> Dict[str, Any]:
                entry = self._entry_at(row)
                if entry is None:
                    return {"error": f"No row {row}"}
                settings = self.window.settings
                if not settings.hydrus.access_key:
                    return {"row": row, "success": False, "error": "No Hydrus access key configured"}
                client = HydrusClient(settings.hydrus)
                result = send_file_upload(entry, client, settings)
                if result.success and settings.remove_after_import:
                    self.window._remove_entries([entry], reason="sent-to-Hydrus")
                else:
                    self.window._refresh_entry_row(entry)
                return {
                    "row": row, "success": result.success,
                    "warning": result.warning, "error": result.error,
                }
            return self.invoker.call(work, timeout=HYDRUS_SEND_TIMEOUT_SECONDS)

        return self._run_tool("send_upload", arguments, perform, row=row)

    def send_url(self, row: int) -> Dict[str, Any]:
        arguments = {"row": row}

        def perform() -> Dict[str, Any]:
            def work() -> Dict[str, Any]:
                entry = self._entry_at(row)
                if entry is None:
                    return {"error": f"No row {row}"}
                settings = self.window.settings
                if not settings.hydrus.access_key:
                    return {"row": row, "success": False, "error": "No Hydrus access key configured"}
                client = HydrusClient(settings.hydrus)
                tmp_dir = tempfile.mkdtemp(prefix="hatate-linux-mcp-")
                try:
                    result = send_url_or_download(entry, client, settings, tmp_dir)
                finally:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                if result.skipped_reason:
                    return {"row": row, "success": False, "skipped_reason": result.skipped_reason}
                removed = False
                if result.success and result.confirmed and settings.remove_after_import:
                    self.window._remove_entries(
                        [entry], reason="downloaded-and-sent-to-Hydrus", keep_place=True)
                    removed = True
                else:
                    self.window._refresh_entry_row(entry)
                return {
                    "row": row, "success": result.success, "confirmed": result.confirmed,
                    "error": result.error, "removed_from_queue": removed,
                    "removal_pending_confirmation": bool(
                        result.success and not result.confirmed and settings.remove_after_import),
                }
            return self.invoker.call(work, timeout=HYDRUS_SEND_TIMEOUT_SECONDS)

        return self._run_tool("send_url", arguments, perform, row=row)

    def download_send(self, row: int) -> Dict[str, Any]:
        arguments = {"row": row}

        def perform() -> Dict[str, Any]:
            def work() -> Dict[str, Any]:
                entry = self._entry_at(row)
                if entry is None:
                    return {"error": f"No row {row}"}
                settings = self.window.settings
                if not settings.hydrus.access_key:
                    return {"row": row, "success": False, "error": "No Hydrus access key configured"}
                client = HydrusClient(settings.hydrus)
                tmp_dir = tempfile.mkdtemp(prefix="hatate-linux-mcp-")
                try:
                    result = download_and_send(
                        entry, client, settings.search_timeout, tmp_dir, settings)
                finally:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                if result.skipped_reason:
                    return {"row": row, "success": False, "skipped_reason": result.skipped_reason}
                if result.success and settings.remove_after_import:
                    self.window._remove_entries([entry], reason="downloaded-and-sent-to-Hydrus")
                else:
                    self.window._refresh_entry_row(entry)
                return {"row": row, "success": result.success, "error": result.error}
            return self.invoker.call(work, timeout=HYDRUS_SEND_TIMEOUT_SECONDS)

        return self._run_tool("download_send", arguments, perform, row=row)

    # ------------------------------------------------------------------
    # Destructive
    # ------------------------------------------------------------------
    def remove_row(self, row: int) -> Dict[str, Any]:
        arguments = {"row": row}

        def perform() -> Dict[str, Any]:
            def work() -> Dict[str, Any]:
                entry = self._entry_at(row)
                if entry is None:
                    return {"error": f"No row {row}"}
                filename = entry.filename
                self.window._remove_rows([entry])
                return {"row": row, "removed": filename}
            return self.invoker.call(work)

        return self._run_tool("remove_row", arguments, perform, row=row)

    def reset_result(self, row: int) -> Dict[str, Any]:
        arguments = {"row": row}

        def perform() -> Dict[str, Any]:
            def work() -> Dict[str, Any]:
                entry = self._entry_at(row)
                if entry is None:
                    return {"error": f"No row {row}"}
                if not entry.has_result():
                    return {"row": row, "reset": False, "reason": "Nothing to reset - not searched yet"}
                entry.reset_result()
                dropped = bool(entry.hydrus_hash and drop_cached_result(entry.hydrus_hash))
                self.window._refresh_table()
                self.window._on_selection_changed()
                return {"row": row, "reset": True, "dropped_from_cache": dropped}
            return self.invoker.call(work)

        return self._run_tool("reset_result", arguments, perform, row=row)


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------
def _queue_row(index: int, entry: ImageEntry) -> Dict[str, Any]:
    return {
        "row": index,
        "filename": entry.filename,
        "status": entry.status.value,
        "reviewed": entry.reviewed,
        "sent_to_hydrus": entry.sent_to_hydrus,
        "similarity": entry.similarity,
        "candidate_count": len(entry.candidates),
    }


def _entry_detail(entry: ImageEntry) -> Dict[str, Any]:
    return {
        "filename": entry.filename,
        "path": entry.path,
        "status": entry.status.value,
        "reviewed": entry.reviewed,
        "sent_to_hydrus": entry.sent_to_hydrus,
        "matched_url": entry.matched_url,
        "booru_name": entry.booru_name,
        "similarity": entry.similarity,
        "similarity_measured": entry.similarity_measured,
        "candidate_count": len(entry.candidates),
        "selected_candidate_index": entry.selected_candidate_index,
        "local_width": entry.local_width,
        "local_height": entry.local_height,
        "match_width": entry.match_width,
        "match_height": entry.match_height,
        "upscale_verdict": entry.upscale_verdict,
        "upscale_check_detail": entry.upscale_check_detail,
        "error_message": entry.error_message,
        "tags": [
            {"namespace": tag.namespace, "name": tag.name, "source": tag.source.value}
            for tag in entry.tags
        ],
    }


def _matches_filter(entry: ImageEntry, text: str) -> bool:
    """A deliberately small filter language for list_queue: a bare word
    matches the filename (case-insensitively), and three keywords match
    the same review state the Shortcuts tab's N / Space already track.
    Not core/entry_filter.py's structured query - that is the GUI filter
    bar's own, considerably larger, vocabulary (site, status set, upscale
    verdict...), and reusing it here would mean parsing a GUI-facing
    query language back out of free text for a tool call that is just as
    often going to want "everything still unreviewed"."""
    needle = text.strip().casefold()
    if not needle:
        return True
    if needle == "unreviewed":
        return entry.needs_review
    if needle == "reviewed":
        return entry.reviewed
    if needle == "sent":
        return entry.sent_to_hydrus
    if needle == entry.status.value:
        return True
    return needle in entry.filename.casefold()


_MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}


def _guess_mime(path_or_url: str) -> str:
    suffix = Path(path_or_url.split("?", 1)[0]).suffix.lower()
    return _MIME_BY_SUFFIX.get(suffix, "image/jpeg")
