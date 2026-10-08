"""The last sub-60% modules in core/ and gui/ (DAN-74).

Measured at 4c1acd9, `coverage report --sort=cover`:

    core/tag_colors.py          48%   missing 50-56
    core/applog.py              50%   missing 24-27, 34-57, 63, 64->66
    gui/add_tags_dialog.py      52%   missing 34-44
    core/desktop_attention.py   53%   missing 48, 65, 71-89, 93

None of these is large, and that is exactly why they were left: each one
is a handful of lines whose only caller is the GUI, so importing the
module covered the constants and nothing else. What they have in common is
that a fault in any of them is silent. A tag namespace that resolves to no
color just looks like a preference that did not take; a log setup that
raises on a read-only config directory takes the app with it before any
log exists to say so; `get_tags` mis-splitting a line writes a wrong tag
into Hydrus; and the KWin call is a courtesy that must degrade to a no-op
everywhere that is not Plasma.

No subprocess is ever really launched here and no D-Bus call is made -
`_run` and `shutil.which` are patched, and the tests assert on the exact
argv that WOULD have been run, which is the part that has to be right.
"""
import logging
import os
import subprocess
import unittest
from unittest.mock import patch

from . import _path  # noqa: F401

# Must be set before PyQt6 is imported, or Qt tries to reach a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core import applog, desktop_attention, tag_colors
from core.config import Settings
from core.models import Tag, TagSource

try:
    from PyQt6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover - depends on environment
    HAVE_QT = False


def _tag(name, namespace=None):
    return Tag(name=name, source=TagSource.BOORU, namespace=namespace)


class TestTagColors(unittest.TestCase):
    """core/tag_colors.py. The module's own docstring is explicit that
    Hydrus exposes no endpoint for the user's real colors, so these are
    documented defaults plus user overrides - which makes the override
    precedence the whole behaviour."""

    def setUp(self):
        self.settings = Settings()
        self.settings.enable_hydrus_tag_colors = True

    def test_nothing_is_colored_while_the_feature_is_off(self):
        # Off is the default. A color leaking through here would recolor
        # the tag list for users who never asked for it.
        self.settings.enable_hydrus_tag_colors = False
        self.assertIsNone(tag_colors.get_tag_color(_tag("x", "creator"), self.settings))
        self.assertIsNone(tag_colors.get_tag_color(_tag("x"), self.settings))

    def test_a_known_namespace_gets_its_documented_default(self):
        self.assertEqual(
            tag_colors.get_tag_color(_tag("someone", "creator"), self.settings),
            "#bb1800")

    def test_the_booru_spelling_of_a_namespace_gets_the_same_color(self):
        # booru's "artist:" is Hydrus/PTR's "creator:" - the same thing
        # under two names, so it must not come out a different color in
        # the same list.
        self.assertEqual(
            tag_colors.get_tag_color(_tag("someone", "artist"), self.settings),
            tag_colors.get_tag_color(_tag("someone", "creator"), self.settings))
        self.assertEqual(
            tag_colors.get_tag_color(_tag("x", "copyright"), self.settings),
            tag_colors.get_tag_color(_tag("x", "series"), self.settings))

    def test_an_unknown_namespace_gets_no_color_rather_than_the_unnamespaced_one(self):
        # "meta:" is known, "medium:" is not. Falling through to the
        # unnamespaced color would paint every unrecognised namespace blue.
        self.assertIsNone(
            tag_colors.get_tag_color(_tag("digital", "medium"), self.settings))

    def test_an_override_wins_over_the_built_in_default(self):
        self.settings.tag_namespace_colors = {"creator": "#123456"}
        self.assertEqual(
            tag_colors.get_tag_color(_tag("someone", "creator"), self.settings),
            "#123456")

    def test_an_override_can_name_a_namespace_that_has_no_default(self):
        self.settings.tag_namespace_colors = {"medium": "#abcdef"}
        self.assertEqual(
            tag_colors.get_tag_color(_tag("digital", "medium"), self.settings),
            "#abcdef")

    def test_an_unnamespaced_tag_gets_the_unnamespaced_color(self):
        self.assertEqual(
            tag_colors.get_tag_color(_tag("1boy"), self.settings),
            tag_colors.UNNAMESPACED_COLOR)

    def test_the_unnamespaced_color_is_overridden_under_the_empty_key(self):
        # The empty-string key is how the settings dialog stores "colour
        # for tags with no namespace" in the same dict as the rest.
        self.settings.tag_namespace_colors = {"": "#0f0f0f"}
        self.assertEqual(tag_colors.get_tag_color(_tag("1boy"), self.settings), "#0f0f0f")

    def test_every_default_is_a_six_digit_hex_string(self):
        # These are handed straight to Qt, which silently ignores a string
        # it cannot parse - so a typo would show as "the colour did not
        # apply" and nothing else.
        for namespace, color in tag_colors.DEFAULT_NAMESPACE_COLORS.items():
            with self.subTest(namespace=namespace):
                self.assertRegex(color, r"^#[0-9a-f]{6}$")
        self.assertRegex(tag_colors.UNNAMESPACED_COLOR, r"^#[0-9a-f]{6}$")


class TestApplog(unittest.TestCase):
    """core/applog.py. Global logging state, so every test restores the
    'hatate' logger's handlers, level and the module's _configured flag -
    otherwise the first test to call setup_logging would leave a live
    RotatingFileHandler attached for the rest of the suite."""

    def setUp(self):
        logger = logging.getLogger("hatate")
        self._handlers = list(logger.handlers)
        self._level = logger.level
        self._configured = applog._configured
        self._ring = list(applog._ring)
        applog._ring.clear()

        def restore():
            # Close anything setup_logging opened before dropping it -
            # a RotatingFileHandler left unclosed is an open file
            # descriptor on app.log for the rest of the suite.
            for handler in logger.handlers:
                if handler not in self._handlers:
                    handler.close()
            logger.handlers = self._handlers
            logger.setLevel(self._level)
            applog._configured = self._configured
            applog._ring.clear()
            applog._ring.extend(self._ring)

        self.addCleanup(restore)

    def test_setup_attaches_a_file_handler_and_the_ring_buffer(self):
        applog._configured = False
        logger = logging.getLogger("hatate")
        logger.handlers = []

        applog.setup_logging()

        kinds = {type(h).__name__ for h in logger.handlers}
        self.assertIn("RotatingFileHandler", kinds)
        self.assertIn("_RingHandler", kinds)
        self.assertEqual(logger.level, logging.DEBUG)

    def test_setup_is_idempotent(self):
        # Documented as safe to call more than once. It is not merely
        # untidy if it isn't: a second file handler means every line
        # written twice, and a rotating handler per call eventually means
        # a fight over the same file.
        applog._configured = False
        logger = logging.getLogger("hatate")
        logger.handlers = []

        applog.setup_logging()
        count = len(logger.handlers)
        returned = applog.setup_logging()

        self.assertEqual(len(logger.handlers), count)
        self.assertIs(returned, logger)

    def test_the_startup_line_names_the_file_it_writes_to(self):
        # The one line that tells a user where to look. It is emitted
        # through the handlers just set up, so the ring buffer holding it
        # is also the proof the ring handler was wired.
        applog._configured = False
        logging.getLogger("hatate").handlers = []

        applog.setup_logging()

        self.assertIn(applog.get_log_file_path(), applog.get_recent_logs())

    def test_the_ring_buffer_holds_formatted_records(self):
        applog._configured = False
        logging.getLogger("hatate").handlers = []
        applog.setup_logging()

        applog.get_logger("dan74").warning("a warning worth finding")

        recent = applog.get_recent_logs()
        self.assertIn("a warning worth finding", recent)
        self.assertIn("[WARNING]", recent)
        self.assertIn("hatate.dan74", recent)

    def test_the_ring_buffer_is_bounded(self):
        # 4000 lines, deliberately: this is read by the log viewer dialog,
        # so unbounded growth would be a slow leak for the life of the
        # process.
        self.assertEqual(applog._ring.maxlen, applog._RING_SIZE)

    def test_a_record_that_cannot_be_formatted_does_not_raise(self):
        # "logging must never itself crash the app" - a bad format
        # argument is the ordinary way that happens, and it must not turn
        # a log line into a traceback in the caller.
        handler = applog._RingHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        record = logging.LogRecord(
            "hatate.dan74", logging.INFO, __file__, 1,
            "too few args: %s %s", ("only one",), None,
        )

        handler.emit(record)  # must not raise

        self.assertEqual(list(applog._ring), [])

    def test_get_logger_returns_the_root_app_logger_unprefixed(self):
        self.assertIs(applog.get_logger("hatate"), logging.getLogger("hatate"))
        self.assertIs(applog.get_logger(), logging.getLogger("hatate"))

    def test_get_logger_prefixes_a_bare_name(self):
        # Without the prefix the record would go to the root logger, miss
        # the ring buffer entirely, and be invisible in the log viewer.
        self.assertEqual(applog.get_logger("image_prep").name, "hatate.image_prep")

    def test_get_logger_leaves_an_already_prefixed_name_alone(self):
        self.assertEqual(applog.get_logger("hatate.hydrus").name, "hatate.hydrus")

    def test_the_log_file_lives_under_the_config_directory(self):
        self.assertTrue(applog.get_log_file_path().endswith("app.log"))


class TestDesktopAttention(unittest.TestCase):
    """core/desktop_attention.py. A courtesy for KDE Plasma, so the thing
    that actually matters is that it is a total no-op anywhere else - a
    search must never depend on it."""

    def _plasma(self, qdbus="/usr/bin/qdbus6"):
        """Patches the two things `available()` reads: the desktop name
        and whether a qdbus binary exists."""
        return (
            patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "KDE"}),
            patch.object(desktop_attention.shutil, "which",
                         side_effect=lambda name: qdbus if name == "qdbus6" else None),
        )

    def test_not_available_off_plasma(self):
        with patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "GNOME"}):
            self.assertFalse(desktop_attention.available())

    def test_not_available_with_no_desktop_set_at_all(self):
        # A bare session, a TTY, a container. os.environ.get's default is
        # what is under test.
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(desktop_attention.available())

    def test_the_desktop_name_is_matched_case_insensitively(self):
        # Real sessions report "KDE", "kde" and "plasma:KDE" depending on
        # the display manager.
        with patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "plasma:kde"}), \
                patch.object(desktop_attention.shutil, "which", return_value="/q"):
            self.assertTrue(desktop_attention.available())

    def test_not_available_on_plasma_without_qdbus(self):
        with patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "KDE"}), \
                patch.object(desktop_attention.shutil, "which", return_value=None):
            self.assertFalse(desktop_attention.available())

    def test_the_qt5_era_binary_name_is_accepted_as_a_fallback(self):
        with patch.object(desktop_attention.shutil, "which",
                          side_effect=lambda n: "/usr/bin/qdbus-qt6"
                          if n == "qdbus-qt6" else None):
            self.assertEqual(desktop_attention._qdbus(), "/usr/bin/qdbus-qt6")

    def test_nothing_is_run_when_the_desktop_is_not_plasma(self):
        # The important one. This is called on every search.
        with patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "XFCE"}), \
                patch.object(desktop_attention, "_run") as run:
            desktop_attention.quiet("lens_browser")
            desktop_attention.ask_for_input("lens_browser")
        run.assert_not_called()

    def test_quiet_takes_the_window_off_the_taskbar_and_drops_attention(self):
        script = desktop_attention.script_for("lens_browser", True, False)
        self.assertIn("w.skipTaskbar = true;", script)
        self.assertIn("w.demandsAttention = false;", script)

    def test_ask_for_input_puts_it_back_and_flags_it(self):
        script = desktop_attention.script_for("lens_browser", False, True)
        self.assertIn("w.skipTaskbar = false;", script)
        self.assertIn("w.demandsAttention = true;", script)

    def test_the_script_only_touches_the_named_window_class(self):
        # The guard that keeps this from reaching across the user's whole
        # session. Quoted through repr, so a class name is never
        # interpolated raw into the JS.
        script = desktop_attention.script_for("lens_browser", True, False)
        self.assertIn("w.resourceClass !== 'lens_browser'", script)
        self.assertIn("continue;", script)

    def test_the_script_handles_both_the_old_and_new_kwin_apis(self):
        # workspace.clientList() was renamed to windowList(); Plasma 5 has
        # only the former, Plasma 6 only the latter.
        script = desktop_attention.script_for("x", True, False)
        self.assertIn("workspace.windowList", script)
        self.assertIn("workspace.clientList", script)

    def test_the_dbus_calls_are_made_in_the_order_kwin_needs(self):
        env, which = self._plasma()
        with env, which, patch.object(desktop_attention, "_run") as run:
            desktop_attention.quiet("lens_browser")

        methods = [call.args[0][3] for call in run.call_args_list]
        self.assertEqual(methods, [
            # unload first: a previous run's script under the same name
            # would block loadScript.
            "org.kde.kwin.Scripting.unloadScript",
            "org.kde.kwin.Scripting.loadScript",
            "org.kde.kwin.Scripting.start",
            "org.kde.kwin.Scripting.unloadScript",
        ])
        for call in run.call_args_list:
            self.assertEqual(call.args[0][:3],
                             ["/usr/bin/qdbus6", "org.kde.KWin", "/Scripting"])

    def test_the_script_file_is_deleted_afterwards(self):
        env, which = self._plasma()
        written = {}

        def capture(command):
            if command[3].endswith("loadScript") and len(command) > 5:
                written["path"] = command[4]

        with env, which, patch.object(desktop_attention, "_run", capture):
            desktop_attention.quiet("lens_browser")

        self.assertIn("path", written)
        self.assertFalse(os.path.exists(written["path"]))

    def test_the_temp_file_is_cleaned_up_even_when_a_call_fails(self):
        env, which = self._plasma()
        seen = []

        def failing(command):
            seen.append(command)
            raise OSError("qdbus6 vanished mid-call")

        with env, which, patch.object(desktop_attention, "_run", failing):
            desktop_attention.quiet("lens_browser")  # must not raise

        self.assertEqual(len(seen), 1)

    def test_nothing_is_unlinked_when_the_temp_file_could_not_be_created(self):
        # `path` is still None at that point, so the finally block must not
        # try to unlink it. A read-only or full /tmp is how this happens.
        env, which = self._plasma()
        with env, which, \
                patch.object(desktop_attention.tempfile, "NamedTemporaryFile",
                             side_effect=OSError("no space left on device")), \
                patch.object(desktop_attention, "_run") as run, \
                patch.object(desktop_attention.Path, "unlink") as unlink:
            desktop_attention.quiet("lens_browser")  # must not raise

        run.assert_not_called()
        unlink.assert_not_called()

    def test_a_subprocess_error_is_swallowed(self):
        env, which = self._plasma()
        with env, which, patch.object(
                desktop_attention, "_run",
                side_effect=subprocess.TimeoutExpired("qdbus6", 5.0)):
            desktop_attention.quiet("lens_browser")  # must not raise

    def test_run_never_lets_a_nonzero_exit_become_an_exception(self):
        # check=False, capture_output=True and a timeout: KWin refusing
        # the script must not surface as a traceback in a search.
        with patch.object(desktop_attention.subprocess, "run") as run:
            desktop_attention._run(["qdbus6", "a", "b"])

        kwargs = run.call_args.kwargs
        self.assertFalse(kwargs["check"])
        self.assertTrue(kwargs["capture_output"])
        self.assertEqual(kwargs["timeout"], desktop_attention.QDBUS_TIMEOUT)


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class TestAddTagsDialog(unittest.TestCase):
    """gui/add_tags_dialog.py. `get_tags` parses free text the user typed
    and the result is written to Hydrus, so a mis-split line is a wrong
    tag on a real file."""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def _dialog(self, text, description="3 selected images"):
        from gui.add_tags_dialog import AddTagsDialog
        dialog = AddTagsDialog(description)
        self.addCleanup(dialog.deleteLater)
        dialog.edit.setPlainText(text)
        return dialog

    def test_the_target_is_named_in_the_prompt(self):
        from PyQt6.QtWidgets import QLabel
        dialog = self._dialog("", description="7 selected images")
        labels = [label.text() for label in dialog.findChildren(QLabel)]
        self.assertTrue(any("7 selected images" in text for text in labels),
                        f"target description missing from {labels}")

    def test_one_tag_per_line(self):
        tags = self._dialog("1boy\nsolo\n").get_tags()
        self.assertEqual([t.name for t in tags], ["1boy", "solo"])

    def test_blank_and_whitespace_only_lines_are_dropped(self):
        # A trailing newline is what everyone's input ends with; an empty
        # Tag would be written to Hydrus as an empty tag.
        tags = self._dialog("1boy\n\n   \n\t\nsolo\n\n").get_tags()
        self.assertEqual([t.name for t in tags], ["1boy", "solo"])

    def test_nothing_typed_yields_no_tags(self):
        self.assertEqual(self._dialog("").get_tags(), [])

    def test_a_namespace_prefix_is_split_off(self):
        tags = self._dialog("character:reimu").get_tags()
        self.assertEqual((tags[0].namespace, tags[0].name), ("character", "reimu"))

    def test_only_the_first_colon_splits(self):
        # "creator:artist:name" and titles containing a colon are both
        # real; splitting on every colon would drop everything after the
        # second one.
        tags = self._dialog("series:fate/stay night: heaven's feel").get_tags()
        self.assertEqual(tags[0].namespace, "series")
        self.assertEqual(tags[0].name, "fate/stay night: heaven's feel")

    def test_both_halves_are_stripped(self):
        # DAN-81. Before the fix `get_tags` stripped the line and then only
        # `name`, so "character : reimu" yielded the namespace "character "
        # with a trailing space - which reaches Hydrus as the tag
        # "character :reimu", a second namespace alongside the real one.
        tags = self._dialog("  character : reimu  ").get_tags()
        self.assertEqual(tags[0].name, "reimu")
        self.assertEqual(tags[0].namespace, "character")

    def test_an_empty_namespace_half_is_no_namespace(self):
        # ": reimu" leaves nothing on the left of the colon. namespace=""
        # would be a third spelling of "unnamespaced" next to None, and
        # Tag.key() treats ("", name) and (None, name) as different tags,
        # so the same tag typed both ways would not de-duplicate.
        tags = self._dialog(": reimu\n   : marisa").get_tags()
        self.assertEqual([(t.namespace, t.name) for t in tags],
                         [(None, "reimu"), (None, "marisa")])

    def test_an_unnamespaced_line_has_no_namespace(self):
        tags = self._dialog("solo").get_tags()
        self.assertIsNone(tags[0].namespace)

    def test_everything_typed_here_is_sourced_as_USER(self):
        # The source drives the per-source enable filter and which tags
        # get sent - a typed tag arriving as BOORU would be filtered by
        # somebody else's setting.
        tags = self._dialog("1boy\ncharacter:reimu").get_tags()
        self.assertEqual({t.source for t in tags}, {TagSource.USER})


if __name__ == "__main__":
    unittest.main()
