#!/usr/bin/env python3
"""Renders the real MainWindow offscreen into files named like the mockup's.

DAN-1294. DAN-1155 ("production does not match the mockup") happened because
the mockup was HTML, production is Qt, and nothing ever put the two in the
same pixel space. This puts them there: a real `MainWindow`, fed synthetic
rows, at 1440x900 and device-pixel-ratio 1, in both themes, written as
`<state>-1440.png` (dark) and `<state>-light-1440.png` (light) - the exact
names of `screenshots/*-1440.png` in the mockup repo
(`Dromares/hatate-ryoku-mockup` @ b40a430), so a shot-for-shot diff needs no
mapping table. It is a harness for the design seat to diff against the
mockup after a merge; it is not a merge gate and asserts no pixels.

Derived from the design seat's acceptance drivers (DAN-1156, DAN-1272:
`acceptance/render_*.py` in the mockup repo), not re-derived.

Usage (no display needed; the offscreen platform is forced):

    python3 tools/render_states.py OUT_DIR
    python3 tools/render_states.py OUT_DIR --states queue,review --modes dark
    python3 tools/render_states.py --list

OUT_DIR is required and is never defaulted into the repo tree. Nothing is
read from or written to the real config: HOME and the XDG dirs are pointed
at a throwaway directory before the app is imported, because `core.paths`
fixes its locations at import time. The synthetic rows are written under
that same directory and removed on exit.

Not rendered, on purpose: the mockup's `spec`, `index`, `chip-semantics*` and
`status-glyph-crop` (mockup-only documents with no production surface), and
`queue-running-banner` (the "continuing without SauceNAO" banner does not
exist in production yet). `review-differences`, `review-no-match` and
`review-wipe` have no production driver yet. An unknown `--states` name is an
error, never a silent skip.
"""
from __future__ import annotations

import argparse
import io
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

PROJECT_DIR = Path(__file__).resolve().parent.parent

WIDTH, HEIGHT = 1440, 900
MODES = ("dark", "light")


def shot_name(state: str, mode: str) -> str:
    """The mockup's naming: dark is the unsuffixed default, light is `-light`."""
    suffix = "" if mode == "dark" else "-light"
    return f"{state}{suffix}-{WIDTH}.png"


def _isolate_environment(work: Path) -> None:
    """Point every per-user location at `work`. Must run before core.* is
    imported (core.paths computes CONFIG_DIR at import)."""
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    # DPR 1 whatever the invoking shell exports.
    os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
    os.environ["QT_SCALE_FACTOR"] = "1"
    os.environ["HOME"] = str(work)
    os.environ["XDG_CONFIG_HOME"] = str(work / "cfg")
    os.environ["XDG_DATA_HOME"] = str(work / "data")
    os.environ["XDG_CACHE_HOME"] = str(work / "cache")


class _Renderer:
    """Everything that needs Qt, built only after the environment is isolated."""

    def __init__(self, work: Path, out_dir: Path):
        from PyQt6.QtWidgets import QApplication

        self.work = work
        self.out_dir = out_dir
        self.app = QApplication.instance() or QApplication([])
        from core import paths
        from gui.fonts import register_fonts

        if not str(paths.CONFIG_DIR).startswith(str(work)):
            raise RuntimeError(f"config dir {paths.CONFIG_DIR} escaped the sandbox {work}")
        register_fonts()
        self.config_dir = Path(paths.CONFIG_DIR)
        self.images = work / "images"
        self.images.mkdir(parents=True, exist_ok=True)

    # -- fixtures -----------------------------------------------------------

    def _art(self, name: str, size, base) -> str:
        from PIL import Image, ImageDraw

        im = Image.new("RGB", size, base)
        d = ImageDraw.Draw(im)
        w, h = size
        ink = (200, 190, 170)
        d.ellipse([w * .2, h * .15, w * .8, h * .6], outline=ink, width=6)
        d.ellipse([w * .1, h * .45, w * .45, h * .75], outline=ink, width=6)
        path = self.images / name
        im.save(path)
        return str(path)

    def _png_bytes(self, size, base) -> bytes:
        from PIL import Image

        buf = io.BytesIO()
        Image.open(self._art("matched.png", size, base)).save(buf, "PNG")
        return buf.getvalue()

    def entries(self):
        """The mockup's eight Queue rows (sample_0 .. sample_7). Rows that
        found a match carry a real candidate, thumbnail and tags so the
        Review surface has something to show."""
        from core.models import ImageEntry, MatchCandidate, MatchStatus, Tag, TagSource

        good, poor, nf = MatchStatus.GOOD, MatchStatus.POOR, MatchStatus.NOT_FOUND
        err, uns = MatchStatus.ERROR, MatchStatus.NOT_SEARCHED
        # status, engine, booru, similarity, measured, sent, reviewed, tag count
        rows = [
            (good, "SauceNAO", "Danbooru", 96, True, True, True, 14),
            (poor, "IQDB", "Gelbooru", 81, True, False, False, 4),
            (good, "SauceNAO", "Pixiv", 94, True, True, False, 14),
            (nf, None, None, None, True, False, False, 0),
            (poor, "ascii2d", "Yande.re", 77, False, False, True, 3),
            (good, "IQDB", "Safebooru", 98, True, False, False, 14),
            (err, None, None, None, True, False, False, 0),
            (uns, None, None, None, True, False, False, 0),
        ]
        names = ["solo", "outdoors", "sky", "cloud", "smile", "sitting", "long_hair", "blue_eyes",
                 "flower", "dress", "looking_at_viewer", "tree", "grass", "1girl"]
        out = []
        for i, (status, engine, booru, sim, measured, sent, reviewed, ntags) in enumerate(rows):
            path = self._art(f"sample_{i}.png", (560, 900), (30 + i * 8, 40, 70))
            e = ImageEntry(path=path, status=status)
            if engine is None:
                out.append(e)
                continue
            tags = [Tag(n, TagSource.BOORU) for n in names[:ntags]]
            thumb = self._png_bytes((1400, 2200), (40, 60, 90))
            c = MatchCandidate(
                url=f"https://example.invalid/post/{i}", source_name=booru, similarity=sim,
                similarity_measured=measured, width=1400, height=2200, engine=engine,
                booru_tags=tags, booru_tags_fetched=True, thumb_bytes=thumb)
            e.booru_name = booru
            e.similarity = sim
            e.similarity_measured = measured
            e.match_width, e.match_height = 1400, 2200
            e.local_width, e.local_height = 560, 900
            e.tags = tags
            e.candidates = [c]
            e.matched_url = c.url
            e.matched_thumb_bytes = thumb
            e.sent_to_hydrus = sent
            e.hydrus_import_confirmed = sent
            e.reviewed = reviewed
            out.append(e)
        return out

    # -- window plumbing ------------------------------------------------------

    def _fresh_config(self) -> None:
        # A MainWindow restores the saved session on start; clearing the
        # config dir keeps one state's rows from doubling into the next.
        shutil.rmtree(self.config_dir, ignore_errors=True)
        self.config_dir.mkdir(parents=True)

    def _build(self, mode: str, entries=None, unclean: bool = False, corrupt_session: bool = False):
        from unittest.mock import patch

        from gui.main_window import MainWindow

        self._fresh_config()
        if corrupt_session:
            from core import session

            Path(session.SESSION_FILE).write_text("{ not json", encoding="utf-8")
        # Both reach for the network / disk of the machine; neither is a
        # surface under comparison.
        with patch.object(MainWindow, "_start_missing_file_check"), \
                patch.object(MainWindow, "_start_hydrus_reconcile"):
            win = MainWindow(unclean_shutdown=unclean)
        win.action_set_theme(mode)
        win.resize(WIDTH, HEIGHT)
        if entries:
            win._register_new_entries(entries)
            win.entries.extend(entries)
            win._refresh_table()
        win.show()
        self._pump()
        return win

    def _pump(self, rounds: int = 8) -> None:
        from PyQt6.QtCore import QCoreApplication, QEvent

        for _ in range(rounds):
            self.app.processEvents()
            time.sleep(0.02)
        # Without this a replaced widget (engine chips, banner meta) is still
        # painted from its pending-delete self.
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def _save(self, win, state: str, mode: str) -> Path:
        self._pump()
        pix = win.grab()
        if (pix.width(), pix.height()) != (WIDTH, HEIGHT) or pix.devicePixelRatio() != 1.0:
            raise RuntimeError(
                f"{state}/{mode}: rendered {pix.width()}x{pix.height()} @ DPR "
                f"{pix.devicePixelRatio()}, expected {WIDTH}x{HEIGHT} @ DPR 1")
        path = self.out_dir / shot_name(state, mode)
        if not pix.save(str(path)):
            raise RuntimeError(f"could not write {path}")
        return path

    def _run_strip(self, win) -> None:
        """A run in flight, populated through production's own setters."""
        win.progress_bar.setMaximum(8)
        win.progress_bar.setValue(7)
        win._run_active = True
        win._refresh_run_progress_label()
        win._on_wait_countdown("Rate limit", 38, 60)
        win.saucenao_quota_label.setText("SauceNAO 142/200 used today")
        win._refresh_sent_count_label()

    def _select(self, win, row: int) -> None:
        win.table.selectRow(row)
        win.table.setFocus()
        self.app.processEvents()

    def _review(self, win, row: int) -> None:
        self._select(win, row)
        win.set_mode("review")
        win._on_selection_changed()
        win.refresh_review()

    # -- states: each takes a theme mode and returns the written path ---------

    def queue(self, mode: str) -> Path:
        win = self._build(mode, self.entries())
        self._run_strip(win)
        self._select(win, 0)
        return self._finish(win, "queue", mode)

    def queue_interrupted_paused(self, mode: str) -> Path:
        win = self._build(mode, self.entries())
        win.status_label.setText(
            "Paused: SauceNAO daily quota exhausted (200/day used) - 330 searched, 82 left unsearched")
        win.saucenao_quota_label.setText("SauceNAO 200/200 used today")
        return self._finish(win, "queue-interrupted-paused", mode)

    def queue_interrupted_crashed(self, mode: str) -> Path:
        # The real restore path, with the crash facts it reads stubbed.
        from unittest.mock import patch

        from core.models import ImageEntry, MatchStatus
        from gui import main_window as mw

        hour = 3600
        spec = [(MatchStatus.GOOD, True), (MatchStatus.POOR, False), (MatchStatus.NOT_SEARCHED, False),
                (MatchStatus.SEARCHING, False), (MatchStatus.NOT_SEARCHED, False)]
        restored = [
            ImageEntry(path=self._art(f"restored_{i}.png", (560, 900), (30 + i * 8, 40, 70)), status=s,
                       sent_to_hydrus=sent, hydrus_import_confirmed=sent,
                       booru_name="Danbooru" if s != MatchStatus.NOT_SEARCHED else None,
                       similarity=96 if s == MatchStatus.GOOD else None)
            for i, (s, sent) in enumerate(spec)]
        self._fresh_config()
        with patch.object(mw, "has_saved_session", return_value=True), \
                patch.object(mw, "load_session", return_value=restored), \
                patch.object(mw, "get_quota_pause_state", return_value=None), \
                patch.object(mw.crashlog, "faults_in_previous_run", return_value=1), \
                patch.object(mw.crashlog, "last_clean_exit_age_seconds",
                             return_value=3 * 86400 + 4 * hour), \
                patch.object(mw.crashlog, "previous_run_stopped_at",
                             return_value=time.time() - (2 * hour + 14 * 60)), \
                patch.object(mw, "saved_session_age_seconds", return_value=2 * hour + 9 * 60), \
                patch.object(mw.MainWindow, "_start_missing_file_check"), \
                patch.object(mw.MainWindow, "_start_hydrus_reconcile"):
            win = mw.MainWindow(unclean_shutdown=True)
        win.action_set_theme(mode)
        win.resize(WIDTH, HEIGHT)
        win.show()
        return self._finish(win, "queue-interrupted-crashed", mode)

    def queue_failed_empty(self, mode: str) -> Path:
        win = self._build(mode, unclean=True, corrupt_session=True)
        return self._finish(win, "queue-failed-empty", mode)

    def empty(self, mode: str) -> Path:
        win = self._build(mode)
        return self._finish(win, "empty", mode)

    def review(self, mode: str) -> Path:
        win = self._build(mode, self.entries())
        self._run_strip(win)
        self._review(win, 0)
        return self._finish(win, "review", mode)

    def review_ranking(self, mode: str) -> Path:
        win = self._build(mode, self.entries())
        self._run_strip(win)
        self._review(win, 4)
        return self._finish(win, "review-ranking", mode)

    def activity(self, mode: str) -> Path:
        import gui.activity_view as av
        from unittest.mock import patch

        # The Log panel prints the log file's path; keep the sandbox's out.
        with patch.object(av, "get_log_file_path", lambda: "~/.config/hatate-linux/app.log"):
            win = self._build(mode, self.entries())
            self._run_strip(win)
            win.set_mode("activity")
            return self._finish(win, "activity", mode)

    def _finish(self, win, state: str, mode: str) -> Path:
        try:
            return self._save(win, state, mode)
        finally:
            win.close()


def state_names() -> List[str]:
    return list(_STATES)


_STATES: Dict[str, Callable[[_Renderer, str], Path]] = {
    "queue": _Renderer.queue,
    "queue-interrupted-paused": _Renderer.queue_interrupted_paused,
    "queue-interrupted-crashed": _Renderer.queue_interrupted_crashed,
    "queue-failed-empty": _Renderer.queue_failed_empty,
    "empty": _Renderer.empty,
    "review": _Renderer.review,
    "review-ranking": _Renderer.review_ranking,
    "activity": _Renderer.activity,
}


def render(out_dir: Path, states: Optional[Sequence[str]] = None,
           modes: Sequence[str] = MODES) -> List[Path]:
    """Renders `states` x `modes` into `out_dir` and returns the files written.
    Raises ValueError on an unknown state or mode before rendering anything."""
    chosen = list(states) if states else state_names()
    unknown = [s for s in chosen if s not in _STATES]
    if unknown:
        raise ValueError(f"unknown state(s) {unknown}; known: {state_names()}")
    bad_modes = [m for m in modes if m not in MODES]
    if bad_modes:
        raise ValueError(f"unknown mode(s) {bad_modes}; known: {list(MODES)}")
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    work = Path(tempfile.mkdtemp(prefix="hatate-render-states-"))
    try:
        _isolate_environment(work)
        sys.path.insert(0, str(PROJECT_DIR))
        renderer = _Renderer(work, out_dir)
        written = []
        for state in chosen:
            for mode in modes:
                written.append(_STATES[state](renderer, mode))
        return written
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out_dir", nargs="?", help="directory to write the PNGs into (created if missing)")
    ap.add_argument("--states", help="comma-separated subset of states (default: all)")
    ap.add_argument("--modes", default=",".join(MODES), help="comma-separated: dark,light")
    ap.add_argument("--list", action="store_true", help="print the output filenames and exit")
    args = ap.parse_args(argv)

    states = [s for s in args.states.split(",") if s] if args.states else None
    modes = [m for m in args.modes.split(",") if m]
    if args.list:
        for state in states or state_names():
            for mode in modes:
                print(shot_name(state, mode))
        return 0
    if not args.out_dir:
        ap.error("OUT_DIR is required")
    try:
        written = render(Path(args.out_dir), states, modes)
    except ValueError as exc:
        ap.error(str(exc))
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
