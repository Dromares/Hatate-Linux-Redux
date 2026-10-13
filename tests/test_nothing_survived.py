"""The "Nothing survived." Queue (DAN-1165 / P11b, gap rows S-05 and R-06).

An unclean shutdown whose saved session came back empty used to land on the
generic drop zone plus one status-bar line. Each claim in the acceptance
list is measured here on a real, themed MainWindow at the mockup's 1440x900
in both modes: the boxed ✕, the headline and prose, the meta line, the
three actions, the strip's failed text - and that the status-bar echo (G-06)
is still there.
"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests import _path  # noqa: F401  (puts the project root on sys.path)

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication, QLabel, QPushButton

from core import crashlog, session as session_mod, session_db
from core.models import ImageEntry, MatchStatus
from gui import shell, theme, widgets
from tests.test_flat_surfaces import MODES, _near, _rgb
from tests.test_ghost_kanji import _ink_over
from tests.test_gui_smoke import GuiTestCase
from tests.test_theme import _contrast_ratio

MARK = crashlog.SESSION_START_MARK
FAULT = f"{crashlog.FAULT_HEADER} Segmentation fault\n\nThread 0x1 (most recent call first):\n  File x\n"
HEADLINE_TOP_SHARE = 0.37
HEADLINE_TOP_TOLERANCE = 0.02


class TestFaultsInPreviousRun(unittest.TestCase):
    """The meta line's "CRASH LOG n NEW ENTRY": what the crashed run left."""

    def _count(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "crash.log"
            path.write_text(text, encoding="utf-8")
            return crashlog.faults_in_previous_run(path)

    def test_a_fault_in_the_previous_runs_section_counts(self):
        self.assertEqual(self._count(f"\n{MARK}\n{FAULT}\n{MARK}\n"), 1)

    def test_one_per_fault_not_per_thread_stack(self):
        self.assertEqual(self._count(f"\n{MARK}\n{FAULT}\n{FAULT}\n{MARK}\n"), 2)

    def test_a_kill_that_wrote_nothing_is_zero(self):
        self.assertEqual(self._count(f"\n{MARK}\n\n{MARK}\n"), 0)

    def test_this_runs_own_section_is_not_counted(self):
        # Anything after the last mark belongs to the run that is starting.
        self.assertEqual(self._count(f"\n{MARK}\n\n{MARK}\n{FAULT}"), 0)

    def test_older_runs_are_not_counted(self):
        self.assertEqual(self._count(f"\n{MARK}\n{FAULT}\n{MARK}\n\n{MARK}\n"), 0)

    def test_a_first_ever_run_has_no_previous_run(self):
        self.assertEqual(self._count(f"\n{MARK}\n"), 0)

    def test_a_missing_or_empty_log_is_zero(self):
        self.assertEqual(self._count(""), 0)
        self.assertEqual(crashlog.faults_in_previous_run(Path("/nonexistent/crash.log")), 0)

    def test_the_mark_is_what_install_writes(self):
        import inspect
        self.assertIn("SESSION_START_MARK", inspect.getsource(crashlog._install_faulthandler))


class TestSavedSessionAge(unittest.TestCase):
    def test_age_is_read_from_the_store_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "session.db"
            db.write_bytes(b"")
            stamp = time.time() - 3 * 86400
            os.utime(db, (stamp, stamp))
            with patch.object(session_db, "SESSION_DB", db), \
                 patch.object(session_mod, "SESSION_FILE", Path(tmp) / "session.json"):
                age = session_mod.saved_session_age_seconds()
        self.assertAlmostEqual(age, 3 * 86400, delta=60)

    def test_no_store_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(session_db, "SESSION_DB", Path(tmp) / "a.db"), \
                 patch.object(session_mod, "SESSION_FILE", Path(tmp) / "a.json"):
                self.assertIsNone(session_mod.saved_session_age_seconds())


class _SurvivedWindow(GuiTestCase):
    def _window(self, mode, *, unclean=True, had_file=True, entries=(), faults=1,
                age=3 * 86400 + 4 * 3600):
        from gui import main_window as mw
        from gui.fonts import register_fonts
        app = QApplication.instance() or QApplication([])
        register_fonts()
        app.setStyleSheet(theme.stylesheet(mode))
        self.addCleanup(lambda: app.setStyleSheet(""))
        with patch.object(mw, "has_saved_session", return_value=had_file), \
             patch.object(mw, "load_session", return_value=list(entries)), \
             patch.object(mw, "get_quota_pause_state", return_value=None), \
             patch.object(mw.crashlog, "faults_in_previous_run", return_value=faults), \
             patch.object(mw, "saved_session_age_seconds", return_value=age):
            win = mw.MainWindow(unclean_shutdown=unclean)
        self.addCleanup(win.deleteLater)
        win.action_set_theme(mode)
        win.resize(1440, 900)
        win.show()
        self.addCleanup(win.close)
        for _ in range(3):
            QApplication.processEvents()
        return win

    @staticmethod
    def _page(win):
        return win.queue_stack.currentWidget()

    @staticmethod
    def _texts(page, name):
        return [label.text() for label in page.findChildren(QLabel, name)]


class TestVariantShown(_SurvivedWindow):
    def test_the_unclean_empty_restore_renders_the_variant(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                page = self._page(win)
                self.assertIs(page, win._nothing_survived_page)
                self.assertEqual(self._texts(page, "DisplayTitle"), ["Nothing survived."])
                # Not the generic empty state.
                self.assertNotIn("Drop images here.", [
                    label.text() for label in page.findChildren(QLabel)])

    def test_every_other_restore_keeps_the_generic_drop_zone(self):
        cases = {
            "clean exit, empty file": dict(unclean=False, had_file=True),
            "unclean, nothing was ever saved": dict(unclean=True, had_file=False),
            "clean, nothing saved": dict(unclean=False, had_file=False),
        }
        for label, kwargs in cases.items():
            with self.subTest(case=label):
                win = self._window("dark", **kwargs)
                self.assertIsNone(getattr(win, "_nothing_survived_page", None))
                self.assertEqual(self._texts(self._page(win), "DisplayTitle"),
                                 ["Drop images here."])
                self.assertEqual(win.run_note_label.text(), "")

    def test_the_status_bar_echo_stays(self):
        win = self._window("dark")
        self.assertEqual(win.status_label.text(),
                         "Run interrupted — nothing survived from the last session")

    def test_the_headline_is_serif_36px(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                title = self._page(win).findChild(QLabel, "DisplayTitle")
                title.ensurePolished()
                self.assertEqual(title.font().family(), "Source Serif 4")
                self.assertEqual(title.font().pixelSize(), 36)

    def test_it_explains_itself_in_prose(self):
        win = self._window("dark")
        body = self._page(win).findChild(QLabel, "Hint")
        self.assertIn("no queue to restore", body.text())
        self.assertGreater(len(body.text()), 80)

    def test_the_block_sits_where_the_empty_state_puts_it(self):
        """E-03's top-weighting is shared, so the page does not jump when one
        variant replaces the other: the block starts at ~37% down."""
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                glyph = self._page(win).findChild(QLabel, "SurvivedGlyph")
                top = glyph.mapTo(win, glyph.rect().topLeft()).y()
                share = top / win.height()
                self.assertAlmostEqual(
                    share, HEADLINE_TOP_SHARE, delta=HEADLINE_TOP_TOLERANCE,
                    msg=f"{mode}: block top at {top}px = {share:.3f}")

    def test_it_keeps_the_frame_and_brackets(self):
        win = self._window("dark")
        page = self._page(win)
        self.assertIsNotNone(page.findChild(widgets.Brackets))
        self.assertEqual(page.size(), win.queue_stack.size())
        self.assertEqual(self._texts(page, "SectionLabel"), ["01 // Queue"])

    def test_the_watermark_is_the_lists_not_the_empty_pages(self):
        win = self._window("dark")
        self.assertEqual(win.ghost.glyph(), shell.GHOST_GLYPHS["queue"])


class TestActionGap(_SurvivedWindow):
    def test_the_three_actions_are_12px_apart(self):  # DAN-1275: `--space-3`, not Qt's 6
        win = self._window("dark")
        buttons = self._page(win).findChildren(QPushButton)
        self.assertEqual(len(buttons), 3)
        for left, right in zip(buttons, buttons[1:], strict=False):
            gap = (right.mapTo(win, right.rect().topLeft()).x()
                   - left.mapTo(win, left.rect().topRight()).x() - 1)
            self.assertEqual(gap, 12, (left.text(), right.text()))


class TestBoxedGlyph(_SurvivedWindow):
    def test_it_is_a_boxed_cross(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                glyph = self._page(win).findChild(QLabel, "SurvivedGlyph")
                self.assertEqual(glyph.text(), "✕")
                self.assertEqual(glyph.width(), widgets.SURVIVED_GLYPH_BOX)
                self.assertEqual(glyph.height(), widgets.SURVIVED_GLYPH_BOX)

    def test_the_box_is_drawn_and_the_cross_is_inked(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                glyph = self._page(win).findChild(QLabel, "SurvivedGlyph")
                img = win.grab().toImage()
                origin = glyph.mapTo(win, glyph.rect().topLeft())
                x0, y0, size = origin.x(), origin.y(), glyph.width()
                box = _ink_over(mode, "ink_46", over="card")
                for dx, dy in ((0, size // 2), (size - 1, size // 2),
                               (size // 2, 0), (size // 2, size - 1)):
                    got = _rgb(img, x0 + dx, y0 + dy)
                    self.assertTrue(_near(got, box), f"{mode}: edge ({dx},{dy}) {got} != {box}")
                # The cross itself reaches the loudest ink somewhere inside.
                loud = _ink_over(mode, "ink_100", over="card")
                inked = [(x, y) for x in range(x0 + 4, x0 + size - 4)
                         for y in range(y0 + 4, y0 + size - 4)
                         if _near(_rgb(img, x, y), loud, tol=40)]
                self.assertGreater(len(inked), 12, f"{mode}: no ✕ ink in the box")

    def test_the_box_and_the_cross_clear_their_contrast_floors(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                card = _card(mode)
                self.assertGreaterEqual(
                    _contrast_ratio(_ink_over(mode, "ink_100", over="card"), card), 4.5)
                self.assertGreaterEqual(
                    _contrast_ratio(_ink_over(mode, "ink_46", over="card"), card), 3.0)


def _card(mode):
    from tests.test_theme import _parse_hex
    return _parse_hex(theme.palette(mode)["card"])


class TestMetaLine(_SurvivedWindow):
    def test_it_reads_crash_log_and_last_saved_session(self):
        win = self._window("dark", faults=1, age=3 * 86400 + 4 * 3600)
        labels = win._nothing_survived_parts["meta_labels"]
        self.assertEqual([label.text() for label in labels],
                         ["Crash log", "1 new entry", "Last saved session", "3d 4h ago"])

    def test_figures_are_bright_and_labels_dim(self):
        win = self._window("dark")
        parts = win._nothing_survived_parts["meta_labels"]
        self.assertEqual([p.objectName() for p in parts],
                         ["MetaLabel", "MetaFigure", "MetaLabel", "MetaFigure"])

    def test_it_is_uppercase_mono(self):
        win = self._window("dark")
        for label in win._nothing_survived_parts["meta_labels"]:
            label.ensurePolished()
            self.assertEqual(label.font().capitalization(),
                             QFont.Capitalization.AllUppercase, label.text())

    def test_the_count_follows_what_the_log_holds(self):
        for faults, want in ((0, "no new entry"), (1, "1 new entry"), (3, "3 new entries")):
            with self.subTest(faults=faults):
                win = self._window("dark", faults=faults)
                labels = win._nothing_survived_parts["meta_labels"]
                self.assertEqual(labels[1].text(), want)

    def test_an_unreadable_session_age_is_left_out_not_invented(self):
        win = self._window("dark", age=None)
        labels = win._nothing_survived_parts["meta_labels"]
        self.assertEqual([label.text() for label in labels], ["Crash log", "1 new entry"])

    def test_the_text_clears_the_body_contrast_floor(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                for tier in ("ink_65", "ink_100"):
                    self.assertGreaterEqual(
                        _contrast_ratio(_ink_over(mode, tier, over="card"), _card(mode)), 4.5)


class TestActions(_SurvivedWindow):
    def test_the_three_actions_in_order_uppercase_mono(self):
        win = self._window("dark")
        buttons = self._page(win).findChildren(QPushButton)
        self.assertEqual([b.text() for b in buttons],
                         ["Start new search", "View crash log", "Add files…"])
        for button in buttons:
            button.ensurePolished()
            self.assertEqual(button.font().capitalization(),
                             QFont.Capitalization.AllUppercase, button.text())
        self.assertEqual(buttons[0].objectName(), "Primary")
        self.assertNotEqual(buttons[1].objectName(), "Primary")

    def test_the_brackets_do_not_swallow_the_actions(self):
        win = self._window("dark")
        page = self._page(win)
        for button in page.findChildren(QPushButton):
            self.assertIs(page.childAt(button.mapTo(page, button.rect().center())),
                          button, button.text())

    def test_view_crash_log_opens_the_crash_log(self):
        win = self._window("dark")
        with patch.object(win, "_run_dialog") as run_dialog:
            win._nothing_survived_parts["crash_log_button"].click()
        dialog = run_dialog.call_args.args[0]
        self.assertTrue(dialog._showing_crash_log)
        dialog.deleteLater()

    def test_add_files_adds_files(self):
        win = self._window("dark")
        with patch("gui.main_window.QFileDialog.getOpenFileNames", return_value=([], "")) as pick:
            win._nothing_survived_parts["add_files_button"].click()
        pick.assert_called_once()

    def test_start_new_search_drops_back_to_the_ordinary_drop_zone(self):
        win = self._window("dark")
        win._nothing_survived_parts["new_search_button"].click()
        self.assertIsNone(win._nothing_survived_page)
        self.assertEqual(self._texts(self._page(win), "DisplayTitle"), ["Drop images here."])
        self.assertEqual(win.run_note_label.text(), "")
        self.assertEqual(win.ghost.glyph(), shell.EMPTY_GHOST_GLYPH)


class TestRunStrip(_SurvivedWindow):
    """R-06: the failed text on the strip."""

    def test_the_strip_says_the_last_run_failed(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                self.assertEqual(win.run_note_label.text(), "last run failed — see crash log")
                self.assertTrue(win.run_note_label.isVisible())
                self.assertTrue(win.run_strip.isAncestorOf(win.run_note_label))

    def test_it_sits_beside_the_idle_count(self):
        win = self._window("dark")
        self.assertEqual(win.run_progress_label.text(), shell.IDLE_PROGRESS)
        left = win.run_progress_label.mapTo(win, win.run_progress_label.rect().topRight()).x()
        note = win.run_note_label.mapTo(win, win.run_note_label.rect().topLeft()).x()
        self.assertGreater(note, left)

    def test_it_is_absent_on_an_ordinary_start(self):
        win = self._window("dark", unclean=False, had_file=False)
        self.assertEqual(win.run_note_label.text(), "")
        self.assertFalse(win.run_note_label.isVisible())


class TestDismissal(_SurvivedWindow):
    def test_a_list_replaces_the_notice_and_the_strip_note(self):
        win = self._window("dark")
        path = os.path.join(os.path.dirname(__file__), "_path.py")
        win.entries.append(ImageEntry(path=path, status=MatchStatus.NOT_SEARCHED))
        win.table_model.refresh_all(win.entries)
        win._refresh_queue_empty_state()
        self.assertEqual(win.queue_stack.currentIndex(), 0)
        self.assertIsNone(win._nothing_survived_page)
        self.assertEqual(win.run_note_label.text(), "")

    def test_emptying_the_list_later_is_an_ordinary_empty_queue(self):
        win = self._window("dark")
        path = os.path.join(os.path.dirname(__file__), "_path.py")
        win.entries.append(ImageEntry(path=path, status=MatchStatus.NOT_SEARCHED))
        win.table_model.refresh_all(win.entries)
        win._refresh_queue_empty_state()
        win.entries.clear()
        win.table_model.refresh_all(win.entries)
        win._refresh_queue_empty_state()
        self.assertEqual(self._texts(self._page(win), "DisplayTitle"), ["Drop images here."])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
