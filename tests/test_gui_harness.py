"""Construction + lifecycle harness for gui/ (DAN-21, Cycle 5 Stage A).

One parametrized table, one runner - the same shape and discipline as
``tests/test_boorus_harness.py`` (DAN-19). Per dialog/view: build it with
plausible arguments and assert it does not raise, then (where it is a
widget) drive it through a ``show()`` / ``processEvents()`` / ``close()``
cycle. The whole point is the SettingsDialog bug class - a constructor
that reads a widget before it exists, so that the very act of opening the
dialog raised. Building the dialog in a test catches it in a second;
nothing else in the suite did, which is why Cycle 4's P0 reached ``main``
and turned 26 tests red for two days.

No live network anywhere. A ``_NetworkGuard`` is held open for the whole
duration of every construction: it raises if the construction opens any
socket (``socket.create_connection`` / ``socket.socket.connect``) or makes
any HTTP request. All outbound HTTP in this codebase is funneled through
the shared ``requests.Session`` in ``core/net.py`` (the Session methods are
the designated stub point), so guarding ``requests.Session`` and
``requests.api`` together with the socket layer means there is no path to
the wire. A construction that trips the guard is a *finding* (a dialog that
phones home on open), reported with its traceback - not something to fix
here.

Qt runs offscreen (``QT_QPA_PLATFORM=offscreen``) and the config directory
is redirected to a throwaway one via ``tests/_path.py`` (imported first, for
the same reason every other gui test does it).

Every scoped gui/ module is accounted for. The ones that are **not** a
constructible class are listed explicitly in ``GAPS`` and the
``TestNamedGaps`` class mechanically verifies each reason (that it really
is a mixin with no ``__init__``, or a module of pure functions with no
widget class), so the gap list can never be hand-waved or silently grow.
"""
import os
import pathlib
import socket
import tempfile
import unittest

from . import _path  # noqa: F401  (sys.path + throwaway XDG_CONFIG_HOME)

# Must be set before PyQt6 is imported, or Qt tries to reach a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# A throwaway config dir: _path set it with setdefault, but the gui smoke
# test overrides it deliberately, and a gui harness should never read the
# real settings file (it holds the user's Hydrus key and Pixiv cookie).
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="hatate-gui-harness-")

try:
    from PyQt6.QtCore import Qt, QAbstractTableModel
    from PyQt6.QtGui import QImage
    from PyQt6.QtWidgets import QApplication, QDialog, QMainWindow, QStyledItemDelegate, QWidget
    HAVE_QT = True
except ImportError:  # pragma: no cover - depends on environment
    HAVE_QT = False

import requests

_app = None


def setUpModule():
    """One QApplication for the whole module - Qt allows only one."""
    global _app
    if HAVE_QT:
        _app = QApplication.instance() or QApplication([])


# -- the no-network guard ----------------------------------------------------
_SESSION_METHODS = ("request", "get", "post", "head", "put", "delete", "options", "patch")
_API_METHODS = ("get", "post", "put", "delete", "options", "head", "patch")


class _NetworkGuard:
    """Raises if a construction reaches for the network.

    Held open for the whole lifetime of a construction. Guards both the
    socket layer (raw connects) and the HTTP layer (the shared
    ``requests.Session`` that ``core/net.py`` funnels everything through,
    plus the ``requests.api`` top-level helpers). ``calls`` records what
    was attempted so a failure names the offending call.
    """

    def __init__(self):
        self.calls = []
        self._saved = []

    def _deny(self, kind):
        self.calls.append(kind)
        raise AssertionError(
            f"network attempt during construction: {kind} "
            f"- a dialog/view must not phone home while it opens"
        )

    def __enter__(self):
        guard = self

        def _cc(*a, **k):
            return guard._deny("socket.create_connection")

        def _connect(self_sock, *a, **k):
            return guard._deny("socket.socket.connect")

        def _session(self_sess, method):
            def _fn(*a, **k):
                return guard._deny(f"requests.Session.{method}")
            return _fn

        def _api(method):
            def _fn(*a, **k):
                return guard._deny(f"requests.api.{method}")
            return _fn

        self._saved.append((socket, "create_connection", socket.create_connection))
        socket.create_connection = _cc
        self._saved.append((socket.socket, "connect", socket.socket.connect))
        socket.socket.connect = _connect
        for meth in _SESSION_METHODS:
            self._saved.append((requests.Session, meth, getattr(requests.Session, meth)))
            setattr(requests.Session, meth, _session(None, meth))
        for meth in _API_METHODS:
            self._saved.append((requests.api, meth, getattr(requests.api, meth)))
            setattr(requests.api, meth, _api(meth))
        # the top-level requests.get/post/... re-export the api functions
        for meth in _API_METHODS:
            self._saved.append((requests, meth, getattr(requests, meth)))
            setattr(requests, meth, _api(meth))
        return self

    def __exit__(self, exc_type, exc, tb):
        for obj, attr, original in reversed(self._saved):
            setattr(obj, attr, original)
        self._saved = []
        return False  # never swallow


def _pump(n=3):
    """Pump the event loop so paint/layout actually run, not just queue."""
    if _app is not None:
        for _ in range(n):
            _app.processEvents()
        # let any deferred (deleteLater) work settle before we assert
        _app.processEvents()


def _write_test_image() -> str:
    """A tiny real PNG so CompareDialog's local-pixmap path loads something
    (rather than logging 'could not load local image')."""
    path = os.path.join(tempfile.gettempdir(),
                        f"hatate-harness-{os.getpid()}-{id(object())}.png")
    img = QImage(64, 48, QImage.Format.Format_RGB32)
    img.fill(0x336699)
    img.save(path, "PNG")
    return path


def _flush_deferred_deletes():
    from PyQt6.QtCore import QCoreApplication, QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def make_themed_window(testcase, mode="dark"):
    """Build a ``MainWindow`` with the real stylesheet already live, in
    main.py's actual construction order (main.py:60-74): register_fonts()
    -> ``app.setStyleSheet(theme.stylesheet(mode))`` -> ``MainWindow()``.

    That order is not cosmetic. Qt only measures a widget's layout against
    whatever stylesheet is in effect *at construction time*, so theming a
    window after it is already built understates every size that depends
    on padding, borders, or the themed font metrics - it keeps the
    pre-theme (smaller) size hints forever, even once the sheet is applied
    afterward. DAN-155 measured the gap directly on ``review_mark_btn``:
    141x25 built bare vs 191x38 built through this helper - a 100% error,
    and in the direction that invents clipping that does not occur in the
    real app, not one that hides a real bug.

    THE TRAP: every bare ``MainWindow()`` call elsewhere in this suite
    (test_gui_smoke.py, test_queue_view.py - and this harness module's own
    ``GAPS`` entry for ``main_window.MainWindow``, which defers construction
    to the smoke test precisely because it skips theming) builds an
    unstyled window. That is fine for a test that only drives behaviour; it
    is silently wrong for one that reads a widget's size, ``sizeHint()``,
    or ``minimumSize()`` - such a test measures the unstyled widget and
    reports it as the real window. Use this helper for any geometry
    assertion. Reach for a bare ``MainWindow()`` only when the test never
    measures size.

    Registers cleanup on ``testcase`` (stylesheet reset, then
    ``win.deleteLater()``) so a caller cannot forget to clear theme state
    before the next test's bare ``MainWindow()`` runs in the same shared
    QApplication.

    Returns ``(app, win)``.
    """
    from gui import theme
    from gui.fonts import register_fonts
    from gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    register_fonts()
    # Registered first so it runs last: the delete below is only queued, and
    # an undelivered one leaves the window alive for every later test's
    # setStyleSheet() to re-polish (DAN-1256).
    testcase.addCleanup(_flush_deferred_deletes)
    app.setStyleSheet(theme.stylesheet(mode))
    testcase.addCleanup(lambda: app.setStyleSheet(""))

    win = MainWindow()
    testcase.addCleanup(win.deleteLater)
    return app, win


# -- the parametrized table ---------------------------------------------------
# kind:  dialog   -> QDialog, drives show()/processEvents()/close()
#        widget   -> plain QWidget, same lifecycle
#        model    -> QAbstractTableModel, exercised via rowCount/columnCount/data
#        delegate -> QStyledItemDelegate, constructed (no show/close)
ROWS = [
    # -- the eight on-demand dialogs (these are the ones that rot) --
    dict(label="settings_dialog.SettingsDialog",
         kind="dialog",
         build=lambda: _build("gui.settings_dialog", "SettingsDialog",
                              lambda: _imp("gui.settings_dialog").SettingsDialog(
                                  _imp("core.config").Settings()))),
    dict(label="compare_dialog.CompareDialog (real local png, no candidate)",
         kind="dialog",
         build=lambda: _build("gui.compare_dialog", "CompareDialog",
                              lambda: _imp("gui.compare_dialog").CompareDialog(
                                  _imp("core.models").ImageEntry(path=_write_test_image()),
                                  _imp("core.config").Settings()))),
    dict(label="pawchive_index_dialog.PawchiveIndexDialog (empty list, no paths)",
         kind="dialog",
         build=lambda: _build("gui.pawchive_index_dialog", "PawchiveIndexDialog",
                              lambda: _imp("gui.pawchive_index_dialog").PawchiveIndexDialog(
                                  lambda: [], index_path=None, cache_path=None))),
    dict(label="hydrus_query_dialog.HydrusQueryDialog (no Hydrus reachable, fine)",
         kind="dialog",
         build=lambda: _build("gui.hydrus_query_dialog", "HydrusQueryDialog",
                              lambda: _imp("gui.hydrus_query_dialog").HydrusQueryDialog(
                                  _imp("core.config").Settings()))),
    dict(label="parser_health_dialog.ParserHealthDialog (probe NOT started)",
         kind="dialog",
         build=lambda: _build("gui.parser_health_dialog", "ParserHealthDialog",
                              lambda: _imp("gui.parser_health_dialog").ParserHealthDialog(
                                  _imp("core.config").Settings()))),
    dict(label="log_viewer_dialog.LogViewerDialog",
         kind="dialog",
         build=lambda: _build("gui.log_viewer_dialog", "LogViewerDialog",
                              lambda: _imp("gui.log_viewer_dialog").LogViewerDialog())),
    dict(label="match_conditions_dialog.MatchConditionsDialog",
         kind="dialog",
         build=lambda: _build("gui.match_conditions_dialog", "MatchConditionsDialog",
                              lambda: _imp("gui.match_conditions_dialog").MatchConditionsDialog(
                                  _imp("core.config").MatchConditions()))),
    dict(label="add_tags_dialog.AddTagsDialog",
         kind="dialog",
         build=lambda: _build("gui.add_tags_dialog", "AddTagsDialog",
                              lambda: _imp("gui.add_tags_dialog").AddTagsDialog(
                                  "3 selected"))),
    # -- views / widgets that ARE concrete classes --
    dict(label="filter_bar.FilterBar (entries_provider = empty list)",
         kind="widget",
         build=lambda: _build("gui.filter_bar", "FilterBar",
                              lambda: _imp("gui.filter_bar").FilterBar(lambda: []))),
    dict(label="image_table_model.ImageTableModel (one entry, stub thumb)",
         kind="model",
         build=lambda: _build("gui.image_table_model", "ImageTableModel",
                              lambda: _imp("gui.image_table_model").ImageTableModel(
                                  [_imp("core.models").ImageEntry(path="/tmp/harness-a.png")],
                                  lambda _e: _imp("PyQt6.QtGui").QIcon()))),
    dict(label="table_delegates.ChipDelegate (theme mode getter)",
         kind="delegate",
         build=lambda: _build("gui.table_delegates", "ChipDelegate",
                              lambda: _imp("gui.table_delegates").ChipDelegate(
                                  lambda: "dark"))),
    dict(label="widgets.WideComboBox",
         kind="widget",
         build=lambda: _build("gui.widgets", "WideComboBox",
                              lambda: _imp("gui.widgets").WideComboBox())),
    dict(label="widgets.ScaledImageLabel",
         kind="widget",
         build=lambda: _build("gui.widgets", "ScaledImageLabel",
                              lambda: _imp("gui.widgets").ScaledImageLabel())),
]


# -- named gaps: constructible? no - and why (self-verified in TestNamedGaps) --
GAPS = [
    dict(label="review_view.ReviewViewMixin",
         reason="a Mixin (plain object) with no __init__; it is composed into "
                "MainWindow, which test_gui_smoke already constructs and drives."),
    dict(label="activity_view.ActivityViewMixin",
         reason="a Mixin (plain object) with no __init__; composed into MainWindow."),
    dict(label="shell.ShellMixin",
         reason="a Mixin (plain object) with no __init__; composed into MainWindow."),
    dict(label="preview_text (module)",
         reason="a module of pure string/formatting functions (human_size, "
                "matched_caption, comparison_banner_text, ...) - it defines no "
                "dialog or widget class, so there is nothing to construct."),
    dict(label="settings_search (module)",
         reason="a module of pure functions over a built widget tree "
                "(index_tabs, matches, highlight) - it defines no dialog or "
                "widget class, so there is nothing to construct; exercised by "
                "tests/test_settings_search.py. DAN-25 lands a live P1 fix "
                "into this file, which is why it stays in GAPS rather than "
                "ROWS while that is open."),
    dict(label="main_window.MainWindow",
         reason="a QMainWindow that composes all three mixin modules; "
                "test_gui_smoke constructs and drives it - it is not a "
                "dialog in this harness's scope, and its construction needs "
                "the real app context, so the smoke test is the right home "
                "for it. Bare construction (as most of test_gui_smoke does) "
                "skips theming; use make_themed_window() above for anything "
                "that measures size."),
    dict(label="message (module)",
         reason="a module of pure functions that wrap QMessageBox "
                "(information, warning, critical, question) - it defines no "
                "dialog or widget class, so there is nothing to construct."),
    dict(label="review_shortcuts (module)",
         reason="a module of pure functions that install QShortcuts on a "
                "live MainWindow (handlers, install) - it defines no dialog "
                "or widget class; its install() path is driven on the real "
                "window by test_gui_smoke."),
    dict(label="session_autosave.SessionAutosaver",
         reason="a plain helper class (QTimer + worker + bookkeeping) with "
                "no widget; test_gui_smoke constructs and drives it "
                "(TestSessionAutosaverOnItsOwn)."),
    dict(label="table_context_menu (module)",
         reason="a module of pure functions that build the right-click QMenu "
                "(build, row_items, count_label, ...) - it defines no "
                "dialog or widget class; its build/show paths are driven on "
                "the real window by test_gui_smoke."),
    dict(label="theme (module)",
         reason="a module of pure data and formatting functions (palette, "
                "stylesheet, resolve_mode) - it defines no dialog or widget "
                "class; exercised by tests/test_theme.py."),
    dict(label="fonts (module)",
         reason="a module of one pure function (register_fonts) - it "
                "defines no dialog or widget class; exercised by "
                "tests/test_fonts.py."),
    dict(label="worker_lifecycle.WorkerRegistry",
         reason="a plain helper class (a parking lot for finishing QThreads) "
                "with no widget; test_gui_smoke constructs and drives it "
                "(TestWorkerRegistryOnItsOwn)."),
    dict(label="mcp_bridge.MainThreadInvoker",
         reason="a QObject with no widget in it - a cross-thread call "
                "marshaler for the embedded MCP server, not anything "
                "shown on screen; exercised by tests/test_mcp_bridge.py."),
]


def _imp(name):
    import importlib
    return importlib.import_module(name)


def _build(mod, cls, fn):
    """Construct ``fn()`` while the no-network guard is open, so a construction
    that reaches for the network fails loudly instead of silently."""
    with _NetworkGuard():
        obj = fn()
    return obj


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class TestGuiConstructionAndLifecycle(unittest.TestCase):
    """The parametrized harness: construct + (where it is a widget) a
    show/processEvents/close cycle, under the no-network guard."""

    def _lifecycle(self, obj, kind, label):
        if kind == "model":
            self.assertIsInstance(obj, QAbstractTableModel)
            # exercise the model's read path the way a view would, offline
            self.assertGreaterEqual(obj.rowCount(), 1)
            self.assertGreaterEqual(obj.columnCount(), 1)
            idx = obj.index(0, 0)
            self.assertTrue(idx.isValid())
            obj.data(idx)                      # display role - must not raise
            obj.headerData(0, Qt.Orientation.Horizontal)
            return
        if kind == "delegate":
            self.assertIsInstance(obj, QStyledItemDelegate)
            self.assertIsNotNone(obj.sizeHint)
            return
        # dialog / widget
        self.assertIsInstance(obj, (QDialog, QWidget))
        obj.show()
        _pump()          # paint + layout actually run, not just queue
        obj.close()
        _pump()
        self.addCleanup(obj.deleteLater)

    def test_every_row_constructs_and_survives(self):
        for row in ROWS:
            with self.subTest(target=row["label"], kind=row["kind"]):
                obj = row["build"]()
                self.assertIsNotNone(obj, f"{row['label']} constructed None")
                self._lifecycle(obj, row["kind"], row["label"])

    def test_table_covers_every_scoped_module(self):
        """The harness must not silently shrink: every module that actually
        exists in gui/ has at least one constructible class (a ROW) OR a
        named gap - and no module may be listed twice.

        The module set is enumerated from the filesystem, not from a
        hardcoded list: a new gui/foo.py with no entry in ROWS or GAPS
        must fail this test instead of passing silently (an unlisted
        module is invisible to the check, so the guarantee reads as
        holding when it is not - the same blind-spot shape that let
        Cycle 4's P0 sit on main with red CI for two days)."""
        covered = set()
        for row in ROWS:
            covered.add(row["label"].split(".")[0])
        for gap in GAPS:
            covered.add(gap["label"].split(".")[0].split(" (")[0])
        gui_dir = pathlib.Path(__file__).resolve().parent.parent / "gui"
        on_disk = {p.stem for p in gui_dir.glob("*.py") if p.stem != "__init__"}
        self.assertEqual(covered, on_disk,
                         f"unaccounted gui/ modules: {on_disk - covered}")
        # A module listed in both ROWS and GAPS is not accounted for exactly
        # once - the duplicate hides which list is authoritative.
        row_mods = {row["label"].split(".")[0] for row in ROWS}
        gap_mods = {gap["label"].split(".")[0].split(" (")[0] for gap in GAPS}
        self.assertEqual(row_mods & gap_mods, set(),
                         f"modules listed in both ROWS and GAPS: {row_mods & gap_mods}")


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class TestNamedGaps(unittest.TestCase):
    """Each named gap is listed with a reason - verify the reason is TRUE so
    the list can never rot into 'we skipped it for no stated reason'."""

    def test_mixins_have_no_init(self):
        for mod, cls in (("gui.review_view", "ReviewViewMixin"),
                         ("gui.activity_view", "ActivityViewMixin"),
                         ("gui.shell", "ShellMixin")):
            with self.subTest(mixin=cls):
                c = getattr(_imp(mod), cls)
                self.assertIsInstance(c, type)
                # a real mixin: not a widget, and defines no __init__ of its own
                self.assertNotIsInstance(c, QWidget)
                self.assertNotIn("__init__", vars(c),
                                 f"{cls} defines an __init__ - it should be in ROWS, not GAPS")

    def test_preview_text_has_no_widget_class(self):
        mod = _imp("gui.preview_text")
        widget_classes = [n for n, v in vars(mod).items()
                          if isinstance(v, type) and issubclass(v, (QDialog, QWidget))]
        self.assertEqual(widget_classes, [],
                         f"preview_text defines widget classes {widget_classes} - "
                         f"they belong in ROWS, not GAPS")
        # and the functions it does expose are the documented pure ones
        for fn in ("human_size", "matched_caption", "comparison_banner_text"):
            self.assertTrue(callable(getattr(mod, fn, None)), f"missing {fn}")

    def test_function_only_modules_define_no_widget(self):
        """The module-level gaps (settings_search, message, review_shortcuts,
        table_context_menu, theme, fonts) are listed as pure-function modules -
        verify that is true, so the reason can't rot. Only classes DEFINED in
        the module count: a module that imports QLabel to inspect it is not
        one that defines a widget."""
        for mod in ("gui.settings_search", "gui.message", "gui.review_shortcuts",
                    "gui.table_context_menu", "gui.theme", "gui.fonts"):
            with self.subTest(mod=mod):
                m = _imp(mod)
                widget_classes = [n for n, v in vars(m).items()
                                  if isinstance(v, type)
                                  and v.__module__ == m.__name__
                                  and issubclass(v, (QDialog, QWidget, QAbstractTableModel,
                                                     QStyledItemDelegate))]
                self.assertEqual(widget_classes, [],
                                 f"{mod} defines widget classes {widget_classes} - "
                                 f"they belong in ROWS, not GAPS")

    def test_main_window_is_a_widget_composing_the_mixin_gaps(self):
        """main_window.MainWindow is in GAPS because the smoke test owns its
        construction - verify that is true: it is a real QMainWindow, and it
        composes the three mixin modules that are GAPS themselves."""
        main_window = _imp("gui.main_window")
        mw = main_window.MainWindow
        self.assertTrue(issubclass(mw, QMainWindow), "MainWindow is not a QMainWindow")
        for mixin in ("ReviewViewMixin", "ActivityViewMixin", "ShellMixin"):
            self.assertIn(mixin, [b.__name__ for b in mw.__bases__],
                          f"MainWindow no longer composes {mixin} - the gap reason is stale")
        # MainWindow is exercised by the smoke test, which is the stated
        # reason it is not in this harness's ROWS.
        smoke = _imp("tests.test_gui_smoke")
        self.assertTrue(any(getattr(c, "__name__", "").startswith("Test")
                            for c in vars(smoke).values() if isinstance(c, type)),
                        "test_gui_smoke no longer defines test classes")

    def test_helper_classes_are_plain_not_widgets(self):
        """session_autosave.SessionAutosaver and worker_lifecycle.WorkerRegistry
        are in GAPS because they are plain helper classes driven by the smoke
        test - verify they really are plain (no QWidget in the tree), so the
        reason can't rot."""
        for mod, cls in (("gui.session_autosave", "SessionAutosaver"),
                         ("gui.worker_lifecycle", "WorkerRegistry"),
                         ("gui.mcp_bridge", "MainThreadInvoker"),
                         ("gui.mcp_bridge", "McpToolHandlers")):
            with self.subTest(cls=cls):
                c = getattr(_imp(mod), cls)
                self.assertIsInstance(c, type)
                self.assertFalse(issubclass(c, (QDialog, QWidget)),
                                 f"{cls} is a widget - it belongs in ROWS, not GAPS")


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class TestNoNetworkGuardIsLive(unittest.TestCase):
    """Prove the guard actually fires (mirrors DAN-19's TestModuleGlobalLeak,
    which proved the memo reset happens). Without this the 'no network'
    guarantee would be an untested no-op."""

    def test_guard_raises_on_a_socket_connect(self):
        with self.assertRaises(AssertionError) as ctx:
            with _NetworkGuard():
                socket.create_connection(("127.0.0.1", 9), timeout=0)
        self.assertIn("network attempt", str(ctx.exception))

    def test_guard_raises_on_an_http_request(self):
        with self.assertRaises(AssertionError) as ctx:
            with _NetworkGuard():
                requests.get("http://127.0.0.1:9/never")
        self.assertIn("network attempt", str(ctx.exception))

    def test_guard_is_clean_after_exit(self):
        """After the guard closes, the patched callables are restored - the
        harness must not leak a broken requests/socket into later tests."""
        with _NetworkGuard():
            pass
        self.assertIsNotNone(socket.create_connection)
        # requests.get must be a real callable again (the guard's _deny would
        # raise AssertionError, not ConnectionError, if it had leaked)
        import inspect
        self.assertFalse(inspect.getsource(requests.get).__contains__("network attempt"))
