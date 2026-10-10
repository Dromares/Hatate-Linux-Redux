"""The theme, and the one rule it must never grow back."""
import ast
import os
import re
import unittest
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests import _path  # noqa: F401  (puts the project root on sys.path)

from gui import theme


class UnresolvedPaletteSubscript(Exception):
    """A `palette(...)[key]` whose key this walker can't resolve to a
    literal string at scan time. Raised instead of silently skipping it,
    because a subscript this guard can't see is one it can't check - see
    TestPaletteSubscriptReachability for why that matters."""


def _is_palette_call(node):
    """True for a `Call` literally named `palette` - `palette(mode)` or
    `theme.palette(mode)`. Qt's own `QWidget.palette()` answers a
    QPalette, which supports no `[...]` access at all, so "named palette"
    plus "immediately subscripted" (checked by the caller) can only be
    this module's dict-returning function - no import-resolution needed
    to tell the two apart."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "palette"
    if isinstance(func, ast.Attribute):
        return func.attr == "palette"
    return False


def _subscript_key_node(node):
    """The expression inside `[...]`, unwrapped from the `ast.Index`
    wrapper Python <3.9 used (harmless to keep; makes no difference on
    the 3.10/3.13 this project tests against)."""
    key = node.slice
    if isinstance(key, ast.Index):  # pragma: no cover - pre-3.9 only
        key = key.value
    return key


def _attach_parents(tree):
    """So a `Subscript` can look up its own enclosing function - plain
    `ast` gives no parent links by default."""
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child.parent = parent


def _enclosing_function_param_names(node):
    """The parameter names of the nearest enclosing function, or an empty
    set at module scope. Requires `_attach_parents()` to have run first."""
    current = getattr(node, "parent", None)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = current.args
            names = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
            if args.vararg:
                names.add(args.vararg.arg)
            if args.kwarg:
                names.add(args.kwarg.arg)
            return names
        current = getattr(current, "parent", None)
    return set()


def _module_string_constants(tree):
    """Every module-level `NAME = "literal"` as a dict, so a subscript
    key passed by name (rather than written inline) can still be
    resolved - e.g. gui/preview_text.py's `BANNER_GOOD = "ink_65"`."""
    consts = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not (isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                consts[target.id] = node.value.value
    return consts


def _resolve_literal_strings(node, consts):
    """Every literal string `node` can evaluate to - a plain string
    literal, a Name resolved via `consts`, or (recursively) either branch
    of a conditional expression - or None if `node` is some other shape
    this walker doesn't know how to follow. The `IfExp` case is what
    gui/preview_text.py's `comparison_banner_tier()` needs: its final
    `return` is `BANNER_GOOD if ... else BANNER_UNCERTAIN`, not a single
    name."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name) and node.id in consts:
        return {consts[node.id]}
    if isinstance(node, ast.IfExp):
        body = _resolve_literal_strings(node.body, consts)
        orelse = _resolve_literal_strings(node.orelse, consts)
        if body is None or orelse is None:
            return None
        return body | orelse
    return None


def _function_return_literals(tree, func_name, consts, path):
    """Every literal string a module-level function named `func_name`
    can return, by resolving each `return` statement in its body the
    same way a direct subscript key is resolved - covers
    gui/preview_text.py's `comparison_banner_tier()`, which hands
    `palette(mode)[...]` a value computed by branching between three
    named constants rather than a literal written at the call site.
    Raises `UnresolvedPaletteSubscript` for a `return` this is too simple
    to follow (another call, a subscript, an f-string, ...) instead of
    quietly reporting an incomplete key set."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            literals = set()
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Return) or sub.value is None:
                    continue
                resolved = _resolve_literal_strings(sub.value, consts)
                if resolved is None:
                    raise UnresolvedPaletteSubscript(
                        f"{path}:{sub.lineno}: {func_name}() returns a value "
                        "this guard can't statically resolve - teach "
                        "_resolve_literal_strings() the new shape, or "
                        "rewrite the call site to pass a literal key"
                    )
                literals |= resolved
            return literals
    raise UnresolvedPaletteSubscript(
        f"{path}: no module-level function named {func_name!r} to resolve "
        "a palette subscript key against"
    )


def discover_palette_subscript_keys(scan_dirs=("gui", "core", "workers")):
    """Every palette key reached by a direct `palette(mode)["key"]` /
    `theme.palette(...)[...]` subscript anywhere under `scan_dirs`,
    found by walking each file's AST rather than hand-listing call
    sites - see TestPaletteSubscriptReachability for why a hand-written
    list is the wrong shape for this particular guard. Raises
    `UnresolvedPaletteSubscript` the moment it meets a subscript key it
    cannot resolve to a literal, so the gap is a loud test failure, not a
    silently incomplete key set."""
    keys = set()
    for dirname in scan_dirs:
        root = Path(_path.ROOT) / dirname
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            _attach_parents(tree)
            consts = _module_string_constants(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Subscript):
                    continue
                if not _is_palette_call(node.value):
                    continue
                key_node = _subscript_key_node(node)
                resolved = _resolve_literal_strings(key_node, consts)
                if resolved is not None:
                    keys |= resolved
                elif isinstance(key_node, ast.Call) and isinstance(key_node.func, ast.Name):
                    keys |= _function_return_literals(tree, key_node.func.id, consts, path)
                elif (
                    isinstance(key_node, ast.Name)
                    and key_node.id in _enclosing_function_param_names(key_node)
                ):
                    # A generic forwarding accessor - gui/theme.py's own
                    # `ink_color(mode, tier)` is the one instance, where
                    # `tier` is just whatever the caller passed in - not a
                    # consumer hardcoding one key, so there is no literal
                    # here to check. DAN-199's
                    # TestInkColorTierReachability below traces that
                    # indirection explicitly, from the caller's side of
                    # `ink_color()` rather than from inside it, so this
                    # branch stays a deliberate skip rather than a blind
                    # spot.
                    continue
                else:
                    raise UnresolvedPaletteSubscript(
                        f"{path}:{node.lineno}: palette subscript key is not "
                        "a literal, a module constant, or a call to a "
                        "module-level function returning either - teach "
                        "discover_palette_subscript_keys() this shape, or "
                        "rewrite the call site to pass a literal key"
                    )
    return keys


def _is_ink_color_call(node):
    """True for a `Call` literally named `ink_color` - `ink_color(mode,
    tier)` or `theme.ink_color(mode, tier)`. Every known call site uses
    the latter (`from gui import theme; theme.ink_color(...)`); the bare
    name is accepted too in case a future call site imports the function
    directly, the same tolerance `_is_palette_call()` above gives
    `palette`."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "ink_color"
    if isinstance(func, ast.Attribute):
        return func.attr == "ink_color"
    return False


def _ink_color_tier_arg(node):
    """The `tier` expression of an `ink_color(mode, tier)` call - the
    second positional argument (every known call site) or the `tier`
    keyword, matching `ink_color`'s own `(mode, tier)` signature. None if
    neither is present."""
    if len(node.args) >= 2:
        return node.args[1]
    for kw in node.keywords:
        if kw.arg == "tier":
            return kw.value
    return None


def _is_status_weight_call(node):
    """True for a call to `status_weight` - `status_weight(key)` or
    `theme.status_weight(key)` - the one `ink_color()` tier argument that
    is itself a call rather than a literal, a module constant, or a call
    to a module-level function with plain `return` statements."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "status_weight"
    if isinstance(func, ast.Attribute):
        return func.attr == "status_weight"
    return False


def _status_weight_tiers():
    """Every tier `theme.status_weight()` can return: every value in its
    own `STATUS_WEIGHTS` table, plus the `'ink_45'` fallback both its
    docstring and its `STATUS_WEIGHTS.get(key, 'ink_45')` body name for
    an unrecognised key. Read from the live table rather than copied by
    hand - `_function_return_literals()` can't walk this one itself,
    because `status_weight()`'s body is a dict `.get()` call, not a
    `return` of a literal/constant/conditional."""
    return set(theme.STATUS_WEIGHTS.values()) | {"ink_45"}


def discover_ink_color_tiers(scan_dirs=("gui",)):
    """Every tier `theme.ink_color(mode, tier)` can be called with,
    anywhere under `scan_dirs` - found by walking each file's AST rather
    than hand-listing call sites, same reasoning as
    `discover_palette_subscript_keys()` above. `ink_color()` forwards
    `tier` straight into a `palette(mode)[tier]` subscript internally
    (see its docstring in gui/theme.py) and is itself called from
    `ImageTableModel`'s `ForegroundRole` and `ChipDelegate` - inside Qt
    paint/model paths - so an unresolvable or missing tier here fails
    the exact way DAN-194 did: a Python `KeyError` that `qFatal()`s the
    whole process instead of raising something catchable.

    Resolves three shapes seen at real call sites: a literal string (or
    either branch of a conditional expression built from
    literals/module constants, via `_resolve_literal_strings()`), a call
    to `status_weight()` (via `_status_weight_tiers()`, since its body
    is a dict `.get()` rather than a `return` this walker's generic
    resolver already follows), and a call to a module-level function
    defined in the same file - `gui/image_table_model.py`'s
    `_size_delta_weight()`/`_upscale_weight()` - resolved from that
    function's own `return` statements via `_function_return_literals()`,
    not a hand-copied list of what they currently return. Any other
    shape raises loudly rather than silently omitting a tier from the
    checked set.
    """
    tiers = set()
    for dirname in scan_dirs:
        root = Path(_path.ROOT) / dirname
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            _attach_parents(tree)
            consts = _module_string_constants(tree)
            for node in ast.walk(tree):
                if not _is_ink_color_call(node):
                    continue
                tier_node = _ink_color_tier_arg(node)
                if tier_node is None:
                    raise UnresolvedPaletteSubscript(
                        f"{path}:{node.lineno}: ink_color() called without "
                        "a resolvable tier argument - teach "
                        "_ink_color_tier_arg() this call shape"
                    )
                resolved = _resolve_literal_strings(tier_node, consts)
                if resolved is not None:
                    tiers |= resolved
                elif _is_status_weight_call(tier_node):
                    tiers |= _status_weight_tiers()
                elif isinstance(tier_node, ast.Call) and isinstance(tier_node.func, ast.Name):
                    tiers |= _function_return_literals(tree, tier_node.func.id, consts, path)
                else:
                    raise UnresolvedPaletteSubscript(
                        f"{path}:{node.lineno}: ink_color()'s tier argument "
                        "is not a literal, a module constant, a call to "
                        "status_weight(), or a call to a module-level "
                        "function returning one of those - teach "
                        "discover_ink_color_tiers() this shape, or rewrite "
                        "the call site to pass a literal tier"
                    )
    return tiers


class TestPalettes(unittest.TestCase):
    def test_both_modes_define_the_same_tokens(self):
        """A token in one palette and not the other formats as a KeyError
        the moment someone switches theme, which is the worst time."""
        self.assertEqual(set(theme.DARK), set(theme.LIGHT))

    def test_every_token_is_substituted(self):
        """A stray {token} reaches Qt as literal text and silently kills
        the rest of the rule it is in."""
        for mode in ("dark", "light"):
            with self.subTest(mode=mode):
                leftover = re.findall(r"\{[a-z_]+\}", theme.stylesheet(mode))
                self.assertEqual(leftover, [], f"unsubstituted: {leftover}")

    def test_every_token_is_a_colour(self):
        pattern = r"^(#[0-9a-fA-F]{6}|rgba\(\d{1,3},\d{1,3},\d{1,3},[0-9.]+\))$"
        for mode, table in (("dark", theme.DARK), ("light", theme.LIGHT)):
            for name, value in table.items():
                with self.subTest(mode=mode, token=name):
                    self.assertRegex(value, pattern)

    def test_resolve_mode_pins_what_it_is_given(self):
        self.assertEqual(theme.resolve_mode("dark"), "dark")
        self.assertEqual(theme.resolve_mode("light"), "light")

    def test_resolve_mode_always_answers_something_drawable(self):
        """Including for junk out of a hand-edited config."""
        for setting in ("system", "", "Dark", "nonsense", None):
            with self.subTest(setting=setting):
                self.assertIn(theme.resolve_mode(setting), ("dark", "light"))


class TestPaletteSubscriptReachability(unittest.TestCase):
    """REGRESSION GUARD (DAN-198): a palette key read by direct subscript
    is invisible to a sweep that only checks `stylesheet()`'s `{}`
    interpolation.

    `stylesheet()` interpolates every DARK/LIGHT key into one format
    string, so a key it stops using still shows up as "unused" the moment
    someone greps the sheet for it - but a couple of call sites
    (`gui/compare_dialog.py`'s `_wipe_line_colour`,
    `gui/preview_text.py`'s `comparison_banner_colour`) reach the palette
    through `palette(mode)["key"]` instead, specifically because that
    value must NOT follow the stylesheet's dark/light split (`on_accent`
    is the same literal in both tables on purpose - see gui/theme.py's
    comment on it). A dead-token sweep that only reads `stylesheet()`
    cannot see these call sites at all.

    That exact blind spot shipped twice. DAN-185 deleted `on_accent` as
    an apparently-dead token; nothing caught it until `_wipe_line_colour`
    ran inside a Qt `paintEvent`, which can't propagate a Python
    exception, so PyQt6 called `qFatal()` and the whole process aborted
    instead of raising a catchable `KeyError`. DAN-162 restored the key
    and added a render test for `on_accent` by name, and DAN-194 asked
    for the general-class guard alongside it - this class is that
    guard, for whichever key is read this way next, not just this one.

    `discover_palette_subscript_keys()` finds those call sites itself by
    walking the AST of `gui/`, `core/`, and `workers/` instead of
    hand-listing them, and raises loudly (failing this test) rather than
    silently skipping anything it cannot resolve to a literal key - a
    hand-maintained list that silently stops covering a new call site
    would be worse than no guard at all for exactly this failure mode.
    """

    def test_every_subscript_reachable_key_exists_in_both_palettes(self):
        try:
            keys = discover_palette_subscript_keys()
        except UnresolvedPaletteSubscript as exc:
            self.fail(str(exc))
        self.assertTrue(
            keys,
            "found no palette(...)[...] subscripts at all - either the "
            "scan is broken, or every call site above has already moved "
            "to {}-interpolation and this guard (and its docstring) "
            "should be revisited",
        )
        for key in sorted(keys):
            with self.subTest(key=key):
                self.assertIn(key, theme.DARK, f"{key!r} is read by direct "
                              "subscript but missing from DARK")
                self.assertIn(key, theme.LIGHT, f"{key!r} is read by direct "
                              "subscript but missing from LIGHT")


class TestInkColorTierReachability(unittest.TestCase):
    """REGRESSION GUARD (DAN-199): `ink_color(mode, tier)`'s `tier` is a
    second blind spot the same shape as `TestPaletteSubscriptReachability`
    above - `discover_palette_subscript_keys()` deliberately does not
    trace it, because `ink_color()`'s own `palette(mode)[tier]` subscript
    forwards a generic parameter rather than hardcoding one key (see the
    "generic forwarding accessor" branch in `discover_palette_subscript_keys()`).
    That left every `ink_color()` call site - `ImageTableModel`'s
    `ForegroundRole` and `ChipDelegate`, both inside Qt paint/model paths
    where a `KeyError` reaches `qFatal()` the same way DAN-194's did -
    checked only by coincidence: every tier currently in use happens to
    also appear as a literal `{ink_NN}` placeholder in `stylesheet()`,
    so deleting one from both palettes already fails
    `test_every_token_is_substituted` today. That coincidence is not a
    guard; a future tier that is ink_color()-only (never spelled out in
    the stylesheet template) would have no safety net at all.

    `discover_ink_color_tiers()` finds every `ink_color()` call site
    itself by walking the AST of `gui/`, including the two that reach
    the tier through a function call rather than a literal -
    `gui/table_delegates.py`'s `status_weight(key)` and
    `gui/image_table_model.py`'s `_size_delta_weight()`/
    `_upscale_weight()` - and raises loudly (failing this test) rather
    than silently skipping anything it cannot resolve, same contract as
    `TestPaletteSubscriptReachability`.
    """

    def test_every_ink_color_tier_exists_in_both_palettes(self):
        try:
            tiers = discover_ink_color_tiers()
        except UnresolvedPaletteSubscript as exc:
            self.fail(str(exc))
        self.assertTrue(
            tiers,
            "found no ink_color(mode, tier) calls at all - either the "
            "scan is broken, or every call site has moved away from "
            "ink_color() and this guard should be revisited",
        )
        for tier in sorted(tiers):
            with self.subTest(tier=tier):
                self.assertIn(tier, theme.DARK, f"{tier!r} is passed to "
                              "ink_color() but missing from DARK")
                self.assertIn(tier, theme.LIGHT, f"{tier!r} is passed to "
                              "ink_color() but missing from LIGHT")


class TestTableItemsAreNeverStyled(unittest.TestCase):
    """REGRESSION GUARD: styling QTableView::item silently discards the
    model's colours.

    The image list codes match quality into the Status cell's background
    - green for a good match, amber for one worth reviewing, red for
    none - and it did so long before the app had a theme. Qt switches a
    view to styled-item painting as soon as a stylesheet touches
    `QTableView::item`, and from then on it draws its own background and
    ignores Qt.ItemDataRole.BackgroundRole entirely. Nothing errors. The
    colour just stops being there, and the one place this app asks
    colour to carry meaning on its own goes quiet.

    Measured when this was first written: with the rule present, a cell
    the model had painted #2e7d32 came out #1b1b24 (the card colour);
    with it gone, #2e7d32.
    """

    @staticmethod
    def _without_comments(sheet):
        """The sheet's rules alone.

        The comment where this rule used to live names it, deliberately,
        so the next person reads why before reaching for it - which means
        a plain substring search finds the warning and calls it the
        offence.
        """
        return re.sub(r"/\*.*?\*/", "", sheet, flags=re.S)

    def test_the_sheet_has_no_table_item_rule(self):
        for mode in ("dark", "light"):
            with self.subTest(mode=mode):
                rules = self._without_comments(theme.stylesheet(mode))
                self.assertNotRegex(
                    rules,
                    r"QTableView::item[^{]*\{",
                    "styling QTableView::item discards the model's status colours",
                )

    def test_the_model_background_survives_the_sheet(self):
        """The same measurement, made rather than remembered."""
        from PyQt6.QtGui import QColor
        from PyQt6.QtWidgets import QApplication, QTableWidget, QTableWidgetItem

        app = QApplication.instance() or QApplication([])
        painted = QColor("#2e7d32")

        app.setStyleSheet(theme.stylesheet("dark"))
        table = QTableWidget(1, 1)
        try:
            item = QTableWidgetItem("Found (good)")
            item.setBackground(painted)
            table.setItem(0, 0, item)
            table.resize(300, 80)
            table.show()
            shown = table.grab().toImage().pixelColor(60, 40)
            self.assertEqual(
                shown.name(), painted.name(),
                "the stylesheet is overriding the model's BackgroundRole",
            )
        finally:
            table.deleteLater()
            app.setStyleSheet("")


class TestEngineChipsCarryNoHue(unittest.TestCase):
    """REGRESSION GUARD (DAN-149): the Activity page's engine on/off row
    used to render as a solid fill - QLabel#ChipGood painted
    `background: {success}` (`#10b981` dark / `#059669` light), the last
    fully-saturated colour left in an otherwise single-ink app. The three-
    hue budget (`success`/`warning`/`danger`) that backed it, plus the
    styles for roles nothing ever instantiated (ChipPoor/ChipBad,
    QLabel#Warning, QPushButton#Danger), are retired outright rather than
    left as a latent colour a future change could silently re-wire. The
    chips now draw as an outlined hairline with no background fill
    (EngineChipOn/Off), told apart by ink tier and border weight.
    """

    def test_the_hue_keys_are_gone_from_both_palettes(self):
        for mode, table in (("dark", theme.DARK), ("light", theme.LIGHT)):
            with self.subTest(mode=mode):
                self.assertFalse(
                    {"success", "warning", "danger"} & set(table),
                    "a hue key survived the Ryoku migration",
                )

    def test_the_dead_chip_and_danger_styles_are_gone_from_the_sheet(self):
        for mode in ("dark", "light"):
            sheet = theme.stylesheet(mode)
            for dead_id in ("ChipGood", "ChipPoor", "ChipBad", "ChipIdle",
                             "Danger", "Warning"):
                with self.subTest(mode=mode, role=dead_id):
                    self.assertNotIn(
                        f"#{dead_id} ", sheet,
                        f"#{dead_id} still has a style rule but nothing "
                        "instantiates it",
                    )

    def test_engine_chips_render_with_no_background_fill(self):
        """Renders an EngineChipOn/Off label on a themed host and samples
        a pixel inside its padding (away from the border and any text) -
        proof the chip shows the surface behind it rather than painting
        its own solid colour block, in both themes."""
        from PyQt6.QtGui import QColor
        from PyQt6.QtWidgets import QApplication, QLabel, QWidget

        app = QApplication.instance() or QApplication([])
        try:
            for mode in ("dark", "light"):
                app.setStyleSheet(theme.stylesheet(mode))
                page_color = QColor(theme.palette(mode)["page"])
                for role in ("EngineChipOn", "EngineChipOff"):
                    with self.subTest(mode=mode, role=role):
                        host = QWidget()
                        host.resize(200, 80)
                        label = QLabel("", host)
                        label.setObjectName(role)
                        label.setGeometry(10, 10, 160, 24)
                        host.show()
                        pixel = host.grab().toImage().pixelColor(13, 13)
                        self.assertEqual(
                            pixel.name(), page_color.name(),
                            f"{role} painted a filled background instead "
                            "of the page behind it",
                        )
                        host.deleteLater()
        finally:
            app.setStyleSheet("")


class TestFontWeightsMatchTheRegisteredFace(unittest.TestCase):
    """REGRESSION GUARD: a `font-weight` a family doesn't ship is not an
    error and not emphasis - Qt silently serves the nearest weight it has
    (DAN-148). `font-weight: 600` on Space Grotesk (Regular only, 400)
    renders identically to no declaration at all; the same value on
    JetBrains Mono (400/500/700) silently renders as 700. Measured with a
    pixel diff, not reasoned about: see DAN-148's before/after screenshots.

    This parses every rule in the live sheet, resolves the family that
    actually applies to it (the rule's own `font-family`, or the sheet's
    base `QWidget` family when a rule doesn't set one), and checks the
    declared weight against the weights `QFontDatabase` reports for the
    registered face - so the next pinned family can't reintroduce the same
    silent gap.
    """

    _RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")
    _WEIGHT_RE = re.compile(r"font-weight:\s*(\d+)\s*;")
    _FAMILY_RE = re.compile(r"font-family:\s*([^;]+);")
    _QUOTED_RE = re.compile(r"'([^']+)'")

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])
        from gui.fonts import register_fonts

        register_fonts()

    @classmethod
    def _primary_family(cls, font_family_value):
        match = cls._QUOTED_RE.search(font_family_value)
        return match.group(1) if match else font_family_value.split(",")[0].strip()

    @classmethod
    def _registered_weights(cls, family):
        from PyQt6.QtGui import QFontDatabase

        return {
            QFontDatabase.weight(family, style)
            for style in QFontDatabase.styles(family)
        }

    def test_every_declared_weight_is_a_weight_the_family_ships(self):
        for mode in ("dark", "light"):
            with self.subTest(mode=mode):
                sheet = re.sub(
                    r"/\*.*?\*/", "", theme.stylesheet(mode), flags=re.S
                )
                for selector, body in self._RULE_RE.findall(sheet):
                    weight_match = self._WEIGHT_RE.search(body)
                    if not weight_match:
                        continue
                    weight = int(weight_match.group(1))
                    family_match = self._FAMILY_RE.search(body)
                    family_value = (
                        family_match.group(1) if family_match else theme.FONT_SANS
                    )
                    family = self._primary_family(family_value)
                    registered = self._registered_weights(family)
                    self.assertIn(
                        weight,
                        registered,
                        f"{selector.strip()!r} asks {family!r} for weight "
                        f"{weight}, which it doesn't ship (has {sorted(registered)}) "
                        "- Qt will silently substitute the nearest one it has",
                    )


class TestStatusGlyphFontCoverage(unittest.TestCase):
    """DAN-186: the routing table between JetBrains Mono and the bundled
    Ryoku Status subset (gui/theme.py's `_NEEDS_GLYPH_FONT`) was only ever
    narrated by hand in a planning doc, never checked - the same blind
    spot that let U+955C ship unnoticed in the kanji subset (see
    test_fonts.py's TestRyokuKanjiSubset). Codepoints are pulled from
    STATUS_GLYPHS itself rather than hardcoded, so a status added later
    without a font check fails here instead of rendering as tofu."""

    @classmethod
    def setUpClass(cls):
        from fontTools.ttLib import TTFont

        from gui.fonts import FONTS_DIR

        cls.status_cmap = TTFont(
            FONTS_DIR / "ryoku-status" / "RyokuStatus.woff2"
        ).getBestCmap()
        cls.mono_cmap = TTFont(
            FONTS_DIR / "jetbrains-mono" / "JetBrainsMono-Regular.woff2"
        ).getBestCmap()

    def test_every_status_glyph_is_covered_by_its_routed_font(self):
        for key, glyph in theme.STATUS_GLYPHS.items():
            codepoint = ord(glyph)
            with self.subTest(key=key, glyph=glyph, codepoint=hex(codepoint)):
                if theme.status_glyph_font(key) is not None:
                    self.assertIn(
                        codepoint,
                        self.status_cmap,
                        f"{key!r}'s glyph {glyph!r} (U+{codepoint:04X}) routes to "
                        f"{theme.STATUS_GLYPH_FONT!r} but is missing from its cmap",
                    )
                else:
                    self.assertIn(
                        codepoint,
                        self.mono_cmap,
                        f"{key!r}'s glyph {glyph!r} (U+{codepoint:04X}) stays on "
                        "JetBrains Mono but is missing from its cmap",
                    )


def _channel_to_linear(channel_255):
    """One sRGB channel (0-255) to its linear-light value, the WCAG 2.x
    relative-luminance formula's own gamma step."""
    c = channel_255 / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _relative_luminance(rgb):
    r, g, b = (_channel_to_linear(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(rgb_a, rgb_b):
    lum_a, lum_b = _relative_luminance(rgb_a), _relative_luminance(rgb_b)
    lighter, darker = max(lum_a, lum_b), min(lum_a, lum_b)
    return (lighter + 0.05) / (darker + 0.05)


def _parse_hex(value):
    value = value.lstrip('#')
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _parse_rgba(value):
    r, g, b, a = value[len('rgba('):-1].split(',')
    return (int(r), int(g), int(b)), float(a)


def _composite_over(fg_rgb, alpha, bg_rgb):
    """The flattened colour an `rgba()` ink tier actually paints as once
    Qt lays it over `bg_rgb` - the same compositing a screenshot would
    show, which is what WCAG's contrast formula needs as input, not the
    ink colour's own undiluted RGB."""
    return tuple(
        round(fg * alpha + bg * (1 - alpha))
        for fg, bg in zip(fg_rgb, bg_rgb, strict=True)
    )


def _resolved_rgb(palette_table, token):
    """A token's painted RGB against this same palette's `card` - opaque
    `#rrggbb` tokens resolve to themselves; `rgba(...)` ink tiers
    composite over `card`, the real `QTableView` paint surface
    ChipDelegate draws on (not `page` - see TestStatusWeightContrast)."""
    value = palette_table[token]
    if value.startswith('#'):
        return _parse_hex(value)
    ink_rgb, alpha = _parse_rgba(value)
    return _composite_over(ink_rgb, alpha, _parse_hex(palette_table['card']))


class TestStatusWeightContrast(unittest.TestCase):
    """REGRESSION GUARD (DAN-211): `not_searched`/`estimated` used to sit
    at `ink_45` - 3.10:1 (dark) / 3.14:1 (light) against the `card`
    surface ChipDelegate actually paints status chips on, both short of
    WCAG AA's 4.5:1 body-text floor (this text paints at 14px regular, so
    it gets no "large text" relaxation). `{page}` is the wrong surface to
    measure against - the audit mistake this ticket named by name - this
    test composites every ink tier over `card` specifically because
    that's the real paint surface, not a parallel assertion against
    `{page}` that would pass by measuring the wrong thing.

    Computes WCAG relative luminance from the live `DARK`/`LIGHT` dicts
    rather than asserting a specific tier name, so the floor holds even
    if a future change swaps which tier a status maps to - a contrast
    floor that lives only in a ticket or a comment is one that silently
    regresses the next time someone edits STATUS_WEIGHTS."""

    AA_BODY_TEXT_FLOOR = 4.5

    def test_every_status_weight_clears_aa_against_card(self):
        for mode, table in (("dark", theme.DARK), ("light", theme.LIGHT)):
            card_rgb = _parse_hex(table['card'])
            for key, tier in theme.STATUS_WEIGHTS.items():
                with self.subTest(mode=mode, key=key, tier=tier):
                    text_rgb = _resolved_rgb(table, tier)
                    ratio = _contrast_ratio(text_rgb, card_rgb)
                    self.assertGreaterEqual(
                        ratio,
                        self.AA_BODY_TEXT_FLOOR,
                        f"{mode}/{key!r} ({tier!r}) is {ratio:.2f}:1 against "
                        f"{table['card']!r}, short of the {self.AA_BODY_TEXT_FLOOR}:1 "
                        "AA body-text floor",
                    )


class TestBorderTierContrast(unittest.TestCase):
    """REGRESSION GUARD (DAN-224): 12 `{ink_18}` borders (plus their 3
    `:disabled` overrides) measured 1.40-1.48:1 dark / 1.91-1.97:1 light
    against the surface they actually sit on - short of WCAG 1.4.11's
    3:1 non-text floor in both themes. Moved to the dedicated `ink_46`
    (resting) / `ink_58` (hover/focus) tiers instead of raising `ink_18`
    itself - 3 other selectors (QLabel#EngineChipOff most notably) still
    read at `ink_18` on purpose, see gui/theme.py's comment there.

    Computes WCAG relative luminance from the live `DARK`/`LIGHT` dicts,
    the same reasoning TestStatusWeightContrast uses above, so the floor
    holds even if a background token's own colour changes later."""

    NON_TEXT_FLOOR = 3.0
    # The three backgrounds the must-raise selectors actually paint on.
    BACKGROUNDS = ('page', 'card', 'card_alt')

    def test_border_tiers_clear_non_text_floor(self):
        for mode, table in (("dark", theme.DARK), ("light", theme.LIGHT)):
            for tier in ('ink_46', 'ink_58'):
                ink_rgb, alpha = _parse_rgba(table[tier])
                for bg_key in self.BACKGROUNDS:
                    with self.subTest(mode=mode, tier=tier, bg=bg_key):
                        bg_rgb = _parse_hex(table[bg_key])
                        border_rgb = _composite_over(ink_rgb, alpha, bg_rgb)
                        ratio = _contrast_ratio(border_rgb, bg_rgb)
                        self.assertGreaterEqual(
                            ratio,
                            self.NON_TEXT_FLOOR,
                            f"{mode}/{tier!r} is {ratio:.2f}:1 against "
                            f"{table[bg_key]!r} ({bg_key}), short of the "
                            f"{self.NON_TEXT_FLOOR}:1 WCAG 1.4.11 floor",
                        )

    def test_hover_focus_tier_is_louder_than_resting_tier(self):
        """The ladder the 5 escalating selectors rely on - hover/focus
        must read at least as present as rest, in both themes."""
        for mode, table in (("dark", theme.DARK), ("light", theme.LIGHT)):
            with self.subTest(mode=mode):
                _, resting_alpha = _parse_rgba(table['ink_46'])
                _, hover_alpha = _parse_rgba(table['ink_58'])
                self.assertGreater(hover_alpha, resting_alpha)

    def test_ink_18_itself_is_unchanged(self):
        """ink_18 keeps 3 deliberately-quiet consumers (EngineChipOff
        most notably, whose on/off encoding depends on it staying below
        ink_46) - raising it was explicitly out of scope for DAN-224."""
        self.assertEqual(theme.DARK['ink_18'], 'rgba(205,196,186,0.18)')
        self.assertEqual(theme.LIGHT['ink_18'], 'rgba(33,29,23,0.32)')

    # Known quiet, intentionally under 3:1: DAN-241's design call looked at
    # two `ink_18` borders outside DAN-224's own 12-selector audit above and
    # judged them one at a time. QStatusBar's border-top is the "not
    # load-bearing" half of that call - a passive single-label strip with
    # no interactive affordance, same reasoning as QGroupBox. Keep it here,
    # asserted rather than silently skipped, so a future contrast sweep
    # can't "fix" it without re-reading why (see gui/theme.py's QStatusBar
    # comment for the full reasoning).
    KNOWN_QUIET_BORDERS = {
        'QStatusBar': ('ink_18', 'page'),
    }

    def test_header_section_border_clears_non_text_floor(self):
        """QHeaderView::section border-right (DAN-241/DAN-244): header
        sections are ResizeMode.Interactive + setSectionsMovable(True) +
        setSectionsClickable(True) (gui/main_window.py), so this boundary
        is a hit target on an interactive control, same category as the
        QListWidget / QPushButton#Segment edges DAN-224 already raised.
        Moved from ink_18 to ink_46; pinned here by selector rather than
        relying only on the generic ink_46-tier sweep above."""
        for mode, table in (("dark", theme.DARK), ("light", theme.LIGHT)):
            with self.subTest(mode=mode):
                ink_rgb, alpha = _parse_rgba(table['ink_46'])
                bg_rgb = _parse_hex(table['card_alt'])
                border_rgb = _composite_over(ink_rgb, alpha, bg_rgb)
                ratio = _contrast_ratio(border_rgb, bg_rgb)
                self.assertGreaterEqual(
                    ratio,
                    self.NON_TEXT_FLOOR,
                    f"{mode}: QHeaderView::section border is {ratio:.2f}:1 "
                    f"against card_alt, short of the {self.NON_TEXT_FLOOR}:1 "
                    f"WCAG 1.4.11 floor",
                )

    def test_status_bar_border_is_documented_quiet_exception(self):
        """QStatusBar's border-top (DAN-241) stays below the floor on
        purpose. If this ever starts clearing 3:1, KNOWN_QUIET_BORDERS
        and the comment above gui/theme.py's QStatusBar rule are both
        stale and need a fresh look, not a silent pass."""
        self.assertIn('QStatusBar', self.KNOWN_QUIET_BORDERS)
        token, bg_key = self.KNOWN_QUIET_BORDERS['QStatusBar']
        for mode, table in (("dark", theme.DARK), ("light", theme.LIGHT)):
            with self.subTest(mode=mode):
                ink_rgb, alpha = _parse_rgba(table[token])
                bg_rgb = _parse_hex(table[bg_key])
                border_rgb = _composite_over(ink_rgb, alpha, bg_rgb)
                ratio = _contrast_ratio(border_rgb, bg_rgb)
                self.assertLess(
                    ratio,
                    self.NON_TEXT_FLOOR,
                    f"{mode}: QStatusBar border now measures {ratio:.2f}:1 "
                    f"against {bg_key} - it clears the floor, so the "
                    f"documented quiet exception (DAN-241) is stale",
                )


def _resolved_rgb_over(value, bg_rgb):
    """`value`'s painted RGB once composited over `bg_rgb` - opaque
    `#rrggbb` resolves to itself; `rgba(...)` composites over the
    background it actually sits on. Generalises `_resolved_rgb` above,
    which always composites over this palette's own `card`, to take the
    background explicitly - the generic sweep below resolves a different
    background per rule, not always `card`."""
    if value.startswith('#'):
        return _parse_hex(value)
    ink_rgb, alpha = _parse_rgba(value)
    return _composite_over(ink_rgb, alpha, bg_rgb)


def _sweep_token_name_lookup(table):
    """value -> token name(s), for readable findings. `stamp_fg` and
    `page` are the same literal `#0d0d0d` in DARK by design (the primary
    button's text colour is deliberately the page's own charcoal) - a
    real, deliberate collision, not a bug, so this joins names with `/`
    rather than treating it as an error."""
    out = {}
    for name, value in table.items():
        out[value] = f"{out[value]}/{name}" if value in out else name
    return out


def _sweep_strip_comments(sheet):
    return re.sub(r"/\*.*?\*/", "", sheet, flags=re.S)


def _sweep_base_selector(selector):
    """Strips a trailing pseudo-state (single `:`) while keeping a
    sub-control (`::`) - `QPushButton#Primary:disabled` ->
    `QPushButton#Primary`; `QHeaderView::section:hover` ->
    `QHeaderView::section`."""
    i, n = 0, len(selector)
    while i < n:
        if selector[i] == ":":
            if i + 1 < n and selector[i + 1] == ":":
                i += 2
                continue
            return selector[:i]
        i += 1
    return selector


def _sweep_first(pattern, body):
    match = pattern.search(body)
    return match.group(1) if match else None


_SWEEP_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_SWEEP_HEX_RE = re.compile(r"#[0-9a-fA-F]{6}")
_SWEEP_RGBA_RE = re.compile(r"rgba\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*[0-9.]+\s*\)")
_SWEEP_COLOR_RE = re.compile(f"({_SWEEP_HEX_RE.pattern}|{_SWEEP_RGBA_RE.pattern})")
_SWEEP_COLOR_PROP_RE = re.compile(r"(?<![-\w])color:\s*([^;]+);")
_SWEEP_BG_PROP_RE = re.compile(r"background(?:-color)?:\s*([^;]+);")
_SWEEP_BORDER_PROP_RE = re.compile(r"border[a-z-]*:\s*([^;]+);")
_SWEEP_SELECTION_BG_RE = re.compile(r"selection-background-color:\s*([^;]+);")
_SWEEP_SELECTION_FG_RE = re.compile(r"selection-color:\s*([^;]+);")
_SWEEP_FONT_SIZE_RE = re.compile(r"font-size:\s*(\d+(?:\.\d+)?)px;")
_SWEEP_FONT_WEIGHT_RE = re.compile(r"font-weight:\s*(\d+);")

_SWEEP_AA_BODY_TEXT_FLOOR = 4.5
_SWEEP_AA_LARGE_TEXT_FLOOR = 3.0
_SWEEP_WCAG_NON_TEXT_FLOOR = 3.0  # WCAG 1.4.11 - no large-text relaxation exists for this one.
_SWEEP_SURFACES = ("page", "card", "card_alt")


@dataclass
class _SweepRule:
    selectors: list
    body: str
    color: str | None
    background: str | None
    border_colors: list
    selection_bg: str | None
    selection_fg: str | None
    font_size: float | None
    font_weight: int | None


def _sweep_parse_rules(sheet):
    rules = []
    for selector_str, body in _SWEEP_RULE_RE.findall(_sweep_strip_comments(sheet)):
        selectors = [s.strip() for s in selector_str.split(",") if s.strip()]
        bg = _sweep_first(_SWEEP_BG_PROP_RE, body)
        if bg is not None and bg.strip() in ("transparent", "none"):
            bg = None
        color = _sweep_first(_SWEEP_COLOR_PROP_RE, body)
        if color is not None and color.strip() in ("transparent", "none"):
            # QProgressBar: color: transparent - the percentage readout is
            # suppressed on purpose (the bar communicates progress
            # spatially, not numerically). Not a contrast pair at all -
            # there is no text to measure.
            color = None
        border_colors = [m.group(0) for decl in _SWEEP_BORDER_PROP_RE.findall(body)
                          for m in _SWEEP_COLOR_RE.finditer(decl)]
        rules.append(_SweepRule(
            selectors=selectors,
            body=body,
            color=color,
            background=bg,
            border_colors=border_colors,
            selection_bg=_sweep_first(_SWEEP_SELECTION_BG_RE, body),
            selection_fg=_sweep_first(_SWEEP_SELECTION_FG_RE, body),
            font_size=float(m.group(1)) if (m := _SWEEP_FONT_SIZE_RE.search(body)) else None,
            font_weight=int(m.group(1)) if (m := _SWEEP_FONT_WEIGHT_RE.search(body)) else None,
        ))
    return rules


def _sweep_background_for(rule, base_backgrounds):
    """A rule's own background if it declared one; else its base
    selector's background (pseudo-state rules inherit the plain rule's
    background in QSS, the same way CSS pseudo-classes do); else None,
    meaning "ambient - resolve against every surface this app has"."""
    if rule.background is not None:
        return rule.background
    for sel in rule.selectors:
        base = _sweep_base_selector(sel)
        if base in base_backgrounds and base_backgrounds[base] is not None:
            return base_backgrounds[base]
    return None


def _sweep_is_large_text(font_size, font_weight):
    """WCAG 2.2's Large Text threshold (18pt regular / 14pt bold),
    converted to the px sizes this sheet is actually written in
    (1pt = 4/3px at the 96dpi WCAG's guidance assumes)."""
    if font_size is None:
        return False
    bold = (font_weight or 400) >= 700
    if bold:
        return font_size >= 14 * (4 / 3)
    return font_size >= 18 * (4 / 3)


@dataclass
class _SweepFinding:
    kind: str  # "text" | "border" | "selection"
    selectors: list
    fg_token: str
    bg_token: str
    ratio: float
    floor: float
    mode: str

    @property
    def passed(self):
        return self.ratio >= self.floor


def _sweep_opaque_candidates(bg_value, table):
    """`bg_value`'s possible painted RGBs as a BACKGROUND, not as text/
    border ink over a known surface. An opaque `#rrggbb` token paints as
    itself regardless of what is behind it - one candidate. A translucent
    `rgba()` token used AS a background (QPushButton#Primary:disabled's
    `background: {ink_18}`, a scrim over whatever surface the disabled
    button happens to sit on) has no single answer either, for the exact
    same "ambient" reason plain text does - so it gets the same
    worst-case treatment, composited over every surface this app has."""
    if bg_value.startswith("#"):
        return [bg_value]
    ink_rgb, alpha = _parse_rgba(bg_value)
    return [_composite_over(ink_rgb, alpha, _parse_hex(table[s])) for s in _SWEEP_SURFACES]


def _sweep_is_disabled(selectors):
    """True if every selector in this rule is a `:disabled` state.
    WCAG 2.x's Contrast (Minimum) (1.4.3) and Non-text Contrast (1.4.11)
    both explicitly exclude inactive/disabled interface components from
    the requirement - see the Understanding docs for each criterion.
    That is a standards-level carve-out, not a per-widget design call, so
    it is applied as a blanket rule here rather than as individually
    hand-listed exemptions."""
    return bool(selectors) and all(":disabled" in sel for sel in selectors)


def _contrast_sweep(mode):
    """DAN-290: a generic WCAG contrast sweep over the live stylesheet,
    replacing the pattern that produced eight reactive contrast tickets
    (DAN-210, 211, 212, 224, 241, 243, 244, 275) - a human notices one bad
    pair, files a ticket, a hand-written test gets added for that one
    pair only. This walks every rule block `theme.stylesheet(mode)`
    actually renders and checks every (text, border, selection) pair it
    finds against the background it resolves, named ones and anonymous
    ones alike. See tests/../../../contrast-audit/REPORT.md (DAN-290) for
    the full methodology; ChipDelegate/ImageTableModel's Python-painted
    colour is NOT covered here - TestStatusWeightContrast above remains
    the guard for that code path."""
    table = theme.palette(mode)
    names = _sweep_token_name_lookup(table)
    sheet = theme.stylesheet(mode)
    rules = _sweep_parse_rules(sheet)

    base_backgrounds = {}
    for rule in rules:
        if rule.background is None:
            continue
        for sel in rule.selectors:
            if sel != _sweep_base_selector(sel):
                continue  # a pseudo-state/sub-control rule - not a base rule, don't seed from it
            base_backgrounds.setdefault(sel, rule.background)

    findings = []
    for rule in rules:
        if _sweep_is_disabled(rule.selectors):
            continue
        resolved_bg = _sweep_background_for(rule, base_backgrounds)
        if resolved_bg is not None:
            bg_candidates = [(v, False) for v in _sweep_opaque_candidates(resolved_bg, table)]
        else:
            bg_candidates = [(table[s], True) for s in _SWEEP_SURFACES]
        large = _sweep_is_large_text(rule.font_size, rule.font_weight)
        text_floor = _SWEEP_AA_LARGE_TEXT_FLOOR if large else _SWEEP_AA_BODY_TEXT_FLOOR

        if rule.color is not None:
            for bg_value, ambient in bg_candidates:
                bg_rgb = bg_value if isinstance(bg_value, tuple) else _parse_hex(bg_value)
                fg_rgb = _resolved_rgb_over(rule.color, bg_rgb)
                ratio = _contrast_ratio(fg_rgb, bg_rgb)
                findings.append(_SweepFinding(
                    kind="text", selectors=rule.selectors,
                    fg_token=names.get(rule.color, rule.color),
                    bg_token=(names.get(bg_value, bg_value) if isinstance(bg_value, str) else str(bg_value))
                    + (" (ambient)" if ambient else ""),
                    ratio=ratio, floor=text_floor, mode=mode,
                ))

        for border_value in rule.border_colors:
            for bg_value, ambient in bg_candidates:
                bg_rgb = bg_value if isinstance(bg_value, tuple) else _parse_hex(bg_value)
                border_rgb = _resolved_rgb_over(border_value, bg_rgb)
                ratio = _contrast_ratio(border_rgb, bg_rgb)
                findings.append(_SweepFinding(
                    kind="border", selectors=rule.selectors,
                    fg_token=names.get(border_value, border_value),
                    bg_token=(names.get(bg_value, bg_value) if isinstance(bg_value, str) else str(bg_value))
                    + (" (ambient)" if ambient else ""),
                    ratio=ratio, floor=_SWEEP_WCAG_NON_TEXT_FLOOR, mode=mode,
                ))

        if rule.selection_fg and rule.selection_bg:
            bg_rgb = (_parse_hex(rule.selection_bg) if rule.selection_bg.startswith("#")
                      else _resolved_rgb_over(rule.selection_bg, _parse_hex(table["page"])))
            fg_rgb = _resolved_rgb_over(rule.selection_fg, bg_rgb)
            ratio = _contrast_ratio(fg_rgb, bg_rgb)
            findings.append(_SweepFinding(
                kind="selection", selectors=rule.selectors,
                fg_token=names.get(rule.selection_fg, rule.selection_fg),
                bg_token=names.get(rule.selection_bg, rule.selection_bg),
                ratio=ratio, floor=text_floor, mode=mode,
            ))

    return findings


# Exemptions: a pair that is a known, deliberate design call to stay under
# the floor. Every entry MUST carry a reason - a bare suppression is exactly
# what DAN-290 asked this NOT to be. Keyed on (kind, frozenset(selectors),
# fg_token, bg_token-prefix) - bg_token carries " (ambient)" as a suffix for
# ambient pairs, so exemptions match on a prefix rather than the exact
# string. Ported verbatim from DAN-290's contrast-audit/REPORT.md sec 3.
_SWEEP_EXEMPTIONS = [
    {
        "kind": "text", "selectors": frozenset({"QWidget#GhostKanji"}),
        "fg_token": "ink_05", "bg_prefix": None,
        "reason": "DAN-1163 / gap register G-07: the ghost kanji is a typographic "
                  "ornament painted behind the page, not text anyone reads - "
                  "'about 6% ink' IS the design, and raising it to 4.5:1 would "
                  "make it the loudest thing on the screen. WCAG 1.4.3 exempts "
                  "pure decoration. It is wired to `color` only because that is "
                  "how a custom-painted widget takes its ink from the sheet; "
                  "tests/test_ghost_kanji.py pins its alpha instead.",
    },
    {
        "kind": "border", "selectors": frozenset({"QStatusBar"}),
        "fg_token": "ink_18", "bg_prefix": "page",
        "reason": "DAN-241/244: a passive single-label strip with no interactive "
                  "affordance, same reasoning as QGroupBox's border. Quiet on "
                  "purpose; TestBorderTierContrast above already pins this by "
                  "name (test_status_bar_border_is_documented_quiet_exception).",
    },
    {
        "kind": "border", "selectors": frozenset({"QGroupBox"}),
        "fg_token": "ink_18", "bg_prefix": None,  # ambient - a settings-dialog grouping frame
        "reason": "Same call as QStatusBar (DAN-241's own comparison): a passive "
                  "grouping frame, not an interactive hit target. Never audited by "
                  "name before this sweep - flagged here as the sweep's own first "
                  "finding, not inherited from an existing test.",
    },
    {
        "kind": "border", "selectors": frozenset({"QLabel#EngineChipOff"}),
        "fg_token": "ink_18", "bg_prefix": None,
        "reason": "DAN-224: ink_18 vs EngineChipOn's ink_46 IS the on/off encoding "
                  "for the Activity engine chips - raising it would erase the "
                  "distinction, not fix it. One of DAN-212's 3 deliberately-quiet "
                  "ink_18 consumers.",
    },
    {
        "kind": "border", "selectors": frozenset({"QToolTip"}),
        "fg_token": "ink_18", "bg_prefix": "card",
        "reason": "New finding from this sweep (never audited before DAN-290), "
                  "decided here rather than deferred: a tooltip's border frames "
                  "transient, hover-only text - it is never itself a hit target or "
                  "an interactive edge, the same category DAN-241 already put "
                  "QStatusBar and QGroupBox in. Quiet on purpose, same reasoning.",
    },
    # DAN-1160 / G-08: the flat hairline surfaces. The mockup (the approved
    # spec) draws region edges at ink_18 on a flat page; these are passive
    # grouping frames, the same category as QGroupBox above - nothing on
    # the line is a hit target, the contents carry their own (3:1) edges.
    {
        "kind": "border", "selectors": frozenset({"QFrame#Card"}),
        "fg_token": "ink_18", "bg_prefix": None,
        "reason": "DAN-1160 / G-08: a Card is a transparent grouping region whose "
                  "1px ink_18 hairline replaces the old filled panel (mockup spec). "
                  "Passive frame, same call as QGroupBox: quiet on purpose, the "
                  "controls inside keep their own ink_46 edges.",
    },
    {
        "kind": "border", "selectors": frozenset({"QFrame#FilterBand"}),
        "fg_token": "ink_18", "bg_prefix": None,
        "reason": "DAN-1160 / G-08: the Queue filter band is a hairline region, not a "
                  "fill (mockup spec). Passive grouping frame like QGroupBox; the "
                  "inputs inside it are the interactive edges and are ink_46.",
    },
    {
        "kind": "border", "selectors": frozenset({"QFrame#DropZone"}),
        "fg_token": "ink_18", "bg_prefix": "card",
        "reason": "DAN-1160 / G-08: the empty-Queue drop zone's outline. It is a "
                  "passive region frame (the whole page is the drop target, not "
                  "the line) - same call as QGroupBox.",
    },
    {
        "kind": "border", "selectors": frozenset({"QTableView"}),
        "fg_token": "ink_18", "bg_prefix": "card",
        "reason": "DAN-1160 / G-08: the hairline that outlines the Queue table region "
                  "(mockup spec). The line is not a hit target - rows, headers and "
                  "selection carry their own, separately-audited contrast.",
    },
    # DAN-292's two entries (QLabel#EngineChipOn border ink_28, QLabel#EngineChipOff
    # text ink_45) are deliberately absent here: DAN-297 landed the fix
    # (ink_28 -> ink_46, ink_45 -> ink_65) on main before this sweep did, so
    # per DAN-297's own instructions both pairs now pass outright with no
    # exemption needed - see TestGenericContrastSweep below.
    #
    # DAN-293's QPushButton:checked border entry (ink_28 vs card_alt) is
    # likewise absent: DAN-384 landed the ink_28 -> ink_46 fix, so the pair
    # now passes outright with no exemption needed.
]


def _is_seamless_fill_border(finding):
    """A border whose colour is literally the same token as the
    background it is drawn on - `QPushButton#Segment:checked`,
    `QPushButton#Mode:checked`, `QCheckBox::indicator:checked` all set
    `border-color: {stamp_bg}` on `background: {stamp_bg}`. That is not
    an invisible REQUIRED border - the checked/filled state is
    deliberately a seamless block of colour (the border declaration
    exists only to keep the box model identical to the unchecked state's
    1px bordered box, so the control doesn't resize by a pixel when
    toggled). A contrast floor on a border that is never meant to be a
    visible edge is a category error, not a design call about
    visibility - hence a blanket rule, not a per-selector exemption."""
    return finding.kind == "border" and finding.fg_token == finding.bg_token.replace(" (ambient)", "")


def _is_sweep_exempt(finding):
    if _is_seamless_fill_border(finding):
        return ("border-color is deliberately the same token as the fill it sits on - a "
                "sizing-only declaration for a seamless filled/checked state, not a visible "
                "edge WCAG 1.4.11 is meant to measure. See _is_seamless_fill_border()'s docstring.")
    for ex in _SWEEP_EXEMPTIONS:
        if finding.kind != ex["kind"]:
            continue
        if frozenset(finding.selectors) != ex["selectors"]:
            continue
        if finding.fg_token != ex["fg_token"]:
            continue
        if ex["bg_prefix"] is None:
            if "(ambient)" not in finding.bg_token:
                continue
        elif not finding.bg_token.startswith(ex["bg_prefix"]):
            continue
        return ex["reason"]
    return None


class TestGenericContrastSweep(unittest.TestCase):
    """DAN-290: replaces incident-by-incident contrast tests with a sweep
    of every (text, border, selection) pair the live stylesheet can
    render, in both themes. See contrast-audit/REPORT.md (DAN-290) for
    the full methodology, the exemption rationale, and this guard's own
    known limitations - Python-painted ChipDelegate/ImageTableModel
    colour is NOT covered here, TestStatusWeightContrast above remains
    the guard for that path."""

    def test_every_rendered_pair_clears_its_floor_or_is_exempt(self):
        for mode in ("dark", "light"):
            for finding in _contrast_sweep(mode):
                if finding.passed:
                    continue
                with self.subTest(mode=mode, selectors=finding.selectors,
                                   fg=finding.fg_token, bg=finding.bg_token):
                    reason = _is_sweep_exempt(finding)
                    self.assertIsNotNone(
                        reason,
                        f"{'/'.join(finding.selectors)}: {finding.fg_token} on "
                        f"{finding.bg_token} is {finding.ratio:.2f}:1, short of "
                        f"{finding.floor}:1, and not in _SWEEP_EXEMPTIONS",
                    )


if __name__ == "__main__":
    unittest.main()
