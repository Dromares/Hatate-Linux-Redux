"""Review: one image, your copy against its match, and the decision.

This is the half of the app a person actually sits and does. The README
has always been clear about the split - a search you start and walk away
from for a day, and a review you do a few thousand times - and until now
the review half got a pair of 280x260 labels in the bottom third of a
window whose top two thirds were a list, while the tool for actually
judging a match lived behind a modal dialog.

So: the pictures get the window. The comparison is one of the views here
rather than a separate window, the decisions are buttons next to it, and
the keys that were already bound do the same things from the same place.

Three views of the same pair, on one segmented control:

- **Side by side** is the old preview panel, at the size it should always
  have been. Free - both pictures are already in hand.
- **Wipe** and **Differences** are the comparison. These cost a network
  fetch, because they need the match at FULL resolution: the preview is a
  downscaled sample, and comparing against a sample misrepresents exactly
  the quality difference you opened the comparison to judge. So they load
  on demand, the first time you ask for one.

That fetch is synchronous, which sounds worse than it is: the dialog has
always done it this way, from its own __init__, before showing. The cost
is the same one, minus a window to manage. What is new is that the view
says "Loading…" while it happens instead of a window taking a moment to
appear.

Every widget that existed before keeps its name. `tag_list` is still
`tag_list`, `local_preview` is still `local_preview`. They have moved
parents, not identities.
"""
from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QListWidget, QSizePolicy,
    QSlider, QSplitter, QStackedWidget, QVBoxLayout, QWidget,
)

from core.applog import get_logger
from core.shortcuts import ACTIONS_BY_ID, resolve
from gui import theme, widgets
from gui.compare_dialog import (
    WipeView, build_difference_overlay, load_comparison,
)

if TYPE_CHECKING:
    from core.config import Settings

log = get_logger("gui.review")

VIEWS = (
    ("pair", "Side by side",
     "Your copy and the match, each shown whole. Already loaded, so it costs nothing."),
    ("wipe", "Wipe",
     "The two in one frame, split by a slider you drag across them.\n"
     "Fetches the match at full resolution the first time, so the quality\n"
     "difference is the real one and not a downscaled sample's."),
    ("diff", "Differences",
     "Both pictures side by side with a red highlight of where they differ -\n"
     "a watermark, a signature, added text, a censoring bar - swept across\n"
     "both at once, so the same region is marked on each.\n\n"
     "Deliberately structural: the two are almost always different\n"
     "resolutions, so it says which AREAS differ, never which exact pixels."),
)


class ReviewViewMixin:
    """The Review mode. A mixin, in MUR's manner - it assigns to `self`."""

    # Provided by the class this is mixed into (MainWindow) - declared here
    # only so mypy knows it exists when checking a fully-typed method below.
    settings: "Settings"

    # ------------------------------------------------------------------
    # Assembly
    # ------------------------------------------------------------------
    def _build_review_page(self):
        self._review_loaded_for = None   # (id(entry), candidate url) the wipe holds
        self._review_loading = False

        page = QWidget()
        # Focusable so the review keys (bound to this page as well as the
        # table - see review_shortcuts) have somewhere to land when the
        # mode is entered and nothing on the page has been clicked yet.
        page.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.review_page = page
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(widgets.PAGE_SPACING)
        outer.addWidget(widgets.screen_title("Review."))

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(widgets.SPLITTER_HANDLE)
        splitter.addWidget(self._build_comparison_card())
        splitter.addWidget(self._build_decision_card())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        self.review_splitter = splitter
        outer.addWidget(splitter, 1)

        outer.addWidget(self._build_position_strip())
        return page

    # -- the pictures ---------------------------------------------------
    def _build_comparison_card(self):
        card, layout = widgets.card()

        header = QHBoxLayout()
        header.setSpacing(10)
        self.review_view_switch, self.review_view_buttons = widgets.segmented(
            VIEWS, on_change=self._on_review_view_changed, current="pair",
        )
        header.addWidget(self.review_view_switch)
        header.addStretch(1)

        self.review_zoom_widgets = []
        for glyph, tip, slot in (
            ("−", "Zoom out (-)", lambda: self.review_wipe.zoom_out()),
            ("+", "Zoom in (+)", lambda: self.review_wipe.zoom_in()),
            ("Fit", "Fit the whole image (0, or double-click)",
             lambda: self.review_wipe.zoom_to_fit()),
            ("100%", "One image pixel per screen pixel (1) - the view that shows\n"
                     "compression artefacts and resampling softness as they really are.",
             lambda: self.review_wipe.zoom_to_actual()),
        ):
            button = widgets.icon_button(glyph, tip, slot)
            self.review_zoom_widgets.append(button)
            header.addWidget(button)

        self.review_zoom_label = widgets.muted("")
        self.review_zoom_label.setMinimumWidth(56)
        self.review_zoom_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.review_zoom_widgets.append(self.review_zoom_label)
        header.addWidget(self.review_zoom_label)

        header.addWidget(widgets.icon_button(
            "⧉", "Open the comparison in its own window - for a second monitor.",
            self._pop_out_comparison,
        ))
        header.addWidget(widgets.icon_button(
            "⌨", "Show all review keyboard shortcuts.",
            self._show_shortcuts_dialog,
        ))
        header.addWidget(widgets.icon_button(
            "⮐", "Show this image in the Queue list.",
            self.action_show_in_queue,
        ))
        layout.addLayout(header)

        # Above both views, because it describes the RELATIONSHIP between
        # the two pictures; under either one it would read as belonging
        # to that side.
        self.comparison_banner = QLabel("")
        self.comparison_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.comparison_banner.setWordWrap(True)
        self.comparison_banner.setToolTip(
            "How the selected match compares with your local file."
        )
        layout.addWidget(self.comparison_banner)

        self.review_stack = QStackedWidget()
        self.review_stack.addWidget(self._build_pair_view())
        self.review_stack.addWidget(self._build_wipe_view())
        layout.addWidget(self.review_stack, 1)

        self.wipe_slider = QSlider(Qt.Orientation.Horizontal)
        self.wipe_slider.setRange(0, 100)
        self.wipe_slider.setValue(50)
        self.wipe_slider.valueChanged.connect(
            lambda v: self.review_wipe.set_position(v / 100.0))
        layout.addWidget(self.wipe_slider)

        self._show_comparison_controls(False)
        return card

    def _build_pair_view(self):
        """The old preview panel, given the room it never had."""
        panel = QWidget()
        grid = QGridLayout(panel)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(2, 1)

        grid.addWidget(widgets.muted("Local image (being searched):"), 0, 0)
        self.local_info_label = widgets.muted("")
        grid.addWidget(self.local_info_label, 1, 0)
        self.local_preview = widgets.ScaledImageLabel("No image selected")
        self.local_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.local_preview.setMinimumSize(280, 260)
        self.local_preview.setSizePolicy(QSizePolicy.Policy.Expanding,
                                         QSizePolicy.Policy.Expanding)
        self.local_preview.setFrameShape(QFrame.Shape.NoFrame)
        grid.addWidget(self.local_preview, 2, 0)

        self.matched_caption = widgets.muted("Matched image:")
        grid.addWidget(self.matched_caption, 0, 1)
        self.matched_info_label = widgets.muted("")
        # A parser that knows WHY it produced no tags says so on a second
        # line here, and those reasons are sentences rather than the
        # "1920x1080 - JPEG" this otherwise holds.
        self.matched_info_label.setWordWrap(True)
        grid.addWidget(self.matched_info_label, 1, 1)

        self.matched_preview = widgets.ScaledImageLabel("No match yet")
        self.matched_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.matched_preview.setMinimumSize(280, 260)
        self.matched_preview.setSizePolicy(QSizePolicy.Policy.Expanding,
                                           QSizePolicy.Policy.Expanding)
        self.matched_preview.setFrameShape(QFrame.Shape.NoFrame)
        self.matched_preview.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.matched_preview.customContextMenuRequested.connect(
            self._show_matched_preview_menu)
        self.matched_preview.setCursor(Qt.CursorShape.PointingHandCursor)
        grid.addWidget(self.matched_preview, 2, 1)
        return panel

    def _build_wipe_view(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.review_wipe = WipeView(mode_getter=lambda: theme.resolve_mode(self.settings.theme))
        self.review_wipe.setToolTip(
            "Scroll to zoom where the pointer is, drag to pan, double-click to fit.\n"
            "+ / - zoom, 0 fits, 1 shows actual pixels."
        )
        self.review_wipe.zoom_changed.connect(
            lambda percent: self.review_zoom_label.setText(f"{percent:.0f}%"))
        layout.addWidget(self.review_wipe, 1)

        self.review_note = widgets.hint("")
        self.review_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.review_note)
        return panel

    # -- the decision ---------------------------------------------------
    def _build_decision_card(self):
        card, layout = widgets.card()
        card.setMinimumWidth(320)
        card.setMaximumWidth(460)

        nav = QHBoxLayout()
        self.review_match_label = widgets.heading("No match")
        nav.addWidget(self.review_match_label)
        nav.addStretch(1)
        nav.addWidget(widgets.icon_button(
            "◂", "Previous match candidate ([)", lambda: self._step_review_candidate(-1)))
        nav.addWidget(widgets.icon_button(
            "▸", "Next match candidate (])", lambda: self._step_review_candidate(+1)))
        layout.addLayout(nav)

        self.candidate_combo = widgets.WideComboBox()
        self.candidate_combo.setEnabled(False)
        self.candidate_combo.setSizePolicy(QSizePolicy.Policy.Expanding,
                                           QSizePolicy.Policy.Fixed)
        self.candidate_combo.currentIndexChanged.connect(self._on_candidate_combo_changed)
        layout.addWidget(self.candidate_combo)

        layout.addWidget(widgets.heading("Tags"))
        self.tag_list = QListWidget()
        self.tag_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.tag_list.itemChanged.connect(self._on_tag_item_edited)
        layout.addWidget(self.tag_list, 1)

        tag_row = QHBoxLayout()
        tag_row.addWidget(widgets.pill_button("Add tags…", self._add_tags_to_current))
        tag_row.addWidget(widgets.pill_button("Remove", self._remove_selected_tags))
        self.edit_tags_btn = widgets.pill_button("Edit")
        self.edit_tags_btn.setCheckable(True)
        self.edit_tags_btn.setToolTip(
            "When on, double-click a tag to edit it in place (namespace:name, or "
            "just name). Clear a tag's text entirely to delete it. Its source "
            "(User/Booru/etc.) is kept as-is. Your Tag Namespaces remap rules "
            "(Settings) apply to what you type here too."
        )
        self.edit_tags_btn.toggled.connect(self._on_edit_tags_toggled)
        tag_row.addWidget(self.edit_tags_btn)
        layout.addLayout(tag_row)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(line)

        # Each of these calls exactly what the right-click entry and the
        # key of the same name call - confirmation prompts included.
        send = widgets.pill_button(
            self._shortcut_label("Send to Hydrus", "send_upload"), self.action_send_to_hydrus, primary=True)
        send.setToolTip("Send the file, its URL and its tags to Hydrus.")
        layout.addWidget(send)

        grid = QGridLayout()
        grid.setSpacing(8)

        url_btn = widgets.pill_button(self._shortcut_label("Send URL", "send_url"), self.action_import_url_to_hydrus)
        url_btn.setToolTip("Hand the URL to Hydrus's own downloader.")
        grid.addWidget(url_btn, 0, 0)

        download_btn = widgets.pill_button(
            self._shortcut_label("Download", "download_send"), self.action_download_and_send_to_hydrus)
        download_btn.setToolTip(
            "Download the full-resolution match and send that to Hydrus with the tags.")
        grid.addWidget(download_btn, 0, 1)

        self.review_mark_btn = widgets.pill_button(
            self._shortcut_label("Mark reviewed", "toggle_reviewed"), self._toggle_reviewed_from_review)
        self.review_mark_btn.setToolTip(
            "Records that you looked at this one and decided against sending it.\n\n"
            "Separate from Sent because both are decisions, and a row with neither "
            "mark is one nobody has got to yet - which is what makes a long review "
            "pass resumable."
        )
        grid.addWidget(self.review_mark_btn, 1, 0)

        open_btn = widgets.pill_button(self._shortcut_label("Open match", "open_match"), self.action_open_matched_url)
        open_btn.setToolTip("Open the matched page in your browser.")
        grid.addWidget(open_btn, 1, 1)

        layout.addLayout(grid)

        # 320 was picked for the tag list/combo above, not for this grid -
        # whichever of "Mark reviewed (<key>)" / "Open match (<key>)" is
        # wider sets the real floor. Deriving it from the live grid
        # (actual button sizeHints, under the actual registered font and
        # stylesheet) rather than hardcoding a number means a future font
        # swap or a longer shortcut label can't silently reopen this: the
        # card simply refuses to be squeezed narrower than its buttons need.
        margins = layout.contentsMargins()
        grid_min_width = grid.minimumSize().width() + margins.left() + margins.right()
        if grid_min_width > card.minimumWidth():
            card.setMinimumWidth(grid_min_width)

        return card

    def _build_position_strip(self):
        """Where you are in the list, and the buttons that move through it.

        These call the same handlers as J / K / N, so a click and a key
        can never disagree about what "next" means.
        """
        strip = QWidget()
        row = QHBoxLayout(strip)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self.review_prev_btn = widgets.pill_button(
            self._shortcut_label("◂ Previous", "prev_row"), lambda: self._run_review_action("prev_row"))
        self.review_prev_btn.setToolTip("Previous image.")
        row.addWidget(self.review_prev_btn)

        self.review_position_label = widgets.muted("")
        self.review_position_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self.review_position_label, 1)

        self.review_next_unreviewed_btn = widgets.pill_button(
            self._shortcut_label("Next unreviewed", "next_unreviewed"), lambda: self._run_review_action("next_unreviewed"))
        self.review_next_unreviewed_btn.setToolTip(
            "Skip ahead to the next image you haven't sent or marked reviewed.")
        row.addWidget(self.review_next_unreviewed_btn)

        self.review_next_btn = widgets.pill_button(
            self._shortcut_label("Next ▸", "next_row"), lambda: self._run_review_action("next_row"))
        self.review_next_btn.setToolTip("Next image.")
        row.addWidget(self.review_next_btn)
        return strip

    def _run_review_action(self, action_id):
        from gui import review_shortcuts

        review_shortcuts.handlers(self)[action_id]()

    def _resolved_shortcuts(self) -> dict[str, str]:
        """Returns the effective key binding for each review action."""
        return resolve(getattr(self.settings, "review_shortcuts", None))

    def _shortcut_label(self, base: str, action_id: str) -> str:
        """Returns the button label with the effective key binding appended."""
        bindings = self._resolved_shortcuts()
        key = bindings.get(action_id, "")
        if key:
            return f"{base} ({key})"
        return base

    def _position_strip_hint(self) -> str:
        """Builds the dynamic hint string from resolved bindings.

        Terser than a literal per-action label (e.g. "j/k move" rather than
        "k previous image  ·  j next image"), matching the README's own
        description of the review keyboard.
        """
        bindings = self._resolved_shortcuts()
        parts = []

        move_keys = [k for k in (bindings.get("next_row", ""), bindings.get("prev_row", "")) if k]
        if move_keys:
            parts.append(f"{'/'.join(move_keys)} move")

        next_unreviewed_key = bindings.get("next_unreviewed", "")
        if next_unreviewed_key:
            parts.append(f"{next_unreviewed_key} next unreviewed")

        toggle_key = bindings.get("toggle_reviewed", "")
        if toggle_key:
            parts.append(f"{toggle_key} marks")

        compare_key = bindings.get("compare", "")
        if compare_key:
            parts.append(f"{compare_key} compares")

        return "  ·  ".join(parts)

    def _show_shortcuts_dialog(self):
        """Shows a dialog with all review keyboard shortcuts and their current bindings."""
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QDialogButtonBox, QScrollArea, QWidget

        dialog = QDialog(self)
        dialog.setWindowTitle("Review Keyboard Shortcuts")
        dialog.setMinimumWidth(400)
        layout = QVBoxLayout(dialog)

        title = QLabel("Review Mode Keyboard Shortcuts")
        title.setObjectName("Heading")
        layout.addWidget(title)

        subtitle = QLabel("Current effective bindings (from Settings → Shortcuts):")
        subtitle.setObjectName("Muted")
        layout.addWidget(subtitle)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setSpacing(6)

        bindings = self._resolved_shortcuts()
        for action in ACTIONS_BY_ID.values():
            key = bindings.get(action.id, "")
            display_key = key if key else "(unbound)"
            row = QHBoxLayout()
            label = QLabel(f"{action.label}:")
            label.setObjectName("Muted")
            key_label = QLabel(display_key)
            key_label.setObjectName("Hint")
            key_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row.addWidget(label, 1)
            row.addWidget(key_label)
            content_layout.addLayout(row)

        content_layout.addStretch(1)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)

        dialog.exec()
    def _show_comparison_controls(self, shown):
        for widget in getattr(self, "review_zoom_widgets", ()):
            widget.setVisible(shown)
        if hasattr(self, "wipe_slider"):
            self.wipe_slider.setVisible(shown)

    def _current_review_key(self):
        entry = self._current_entry()
        if entry is None:
            return None
        candidate = entry.selected_candidate
        return (id(entry), getattr(candidate, "url", None))

    def _on_review_view_changed(self, key):
        """Switches view, fetching the full-resolution pair if one is needed."""
        if not hasattr(self, "review_stack"):
            return
        if key == "pair":
            self.review_stack.setCurrentIndex(0)
            self._show_comparison_controls(False)
            return

        self.review_stack.setCurrentIndex(1)
        self._show_comparison_controls(True)
        if not self._ensure_comparison_loaded():
            return
        self._apply_wipe_mode(key)

    def _ensure_comparison_loaded(self):
        """Fetches the match at full resolution, once per entry+candidate."""
        entry = self._current_entry()
        if entry is None:
            self.review_note.setText("Nothing selected.")
            self.review_wipe.set_images(None, None)
            return False

        key = self._current_review_key()
        if key == self._review_loaded_for:
            return True
        if self._review_loading:
            return False

        self._review_loading = True
        self.review_note.setText("Loading the match at full resolution…")
        self.review_wipe.set_images(None, None)
        # Let the label actually paint before the fetch blocks the thread.
        from PyQt6.QtWidgets import QApplication
        QApplication.processEvents()
        try:
            loaded = load_comparison(entry, self.settings)
        except Exception as exc:
            log.warning("Could not load the comparison for %s: %s", entry.filename, exc)
            self.review_note.setText(f"Could not load the match: {exc}")
            return False
        finally:
            self._review_loading = False

        self._review_local = loaded.local
        self._review_matched = loaded.matched
        self._review_loaded_for = key
        self._review_diff_for = None
        self.review_wipe.clear_difference_view()
        self.review_wipe.set_images(loaded.local, loaded.matched)
        if loaded.matched is not None and not loaded.used_fallback:
            # The full original is in hand now, so Side by side shows it
            # too instead of the 150-850px sample it started with.
            self.matched_preview.setPixmap(loaded.matched)

        notes = []
        if loaded.matched is None:
            notes.append("No match image could be fetched.")
        elif loaded.used_fallback:
            notes.append(
                "Showing the downscaled preview - the full file was not available, "
                "so this understates the quality difference."
            )
        self.review_note.setText("  ".join(notes))
        self.review_view_buttons["diff"].setEnabled(
            loaded.local is not None and loaded.matched is not None)
        return True

    def full_resolution_match(self, entry):
        """The match at full resolution, if the comparison has already
        fetched it for this entry and its selected candidate; else None."""
        if entry is None or getattr(self, "_review_loaded_for", None) is None:
            return None
        candidate = entry.selected_candidate
        if self._review_loaded_for != (id(entry), getattr(candidate, "url", None)):
            return None
        return getattr(self, "_review_matched", None)

    def _apply_wipe_mode(self, key):
        if key == "diff":
            self._show_differences()
        else:
            self.review_wipe.clear_difference_view()

    def _show_differences(self):
        """Builds the difference overlay, reusing the dialog's own maths.

        Cached per loaded pair: rebuilding it on every switch back to this
        view would stall the window for nothing.
        """
        local = getattr(self, "_review_local", None)
        matched = getattr(self, "_review_matched", None)
        if local is None or matched is None:
            self.review_note.setText("Both pictures are needed to compare them.")
            return

        if getattr(self, "_review_diff_for", None) != self._review_loaded_for:
            self._review_diff_overlay, self._review_diff_map = build_difference_overlay(
                local, matched)
            self._review_diff_for = self._review_loaded_for

        if self._review_diff_overlay is None:
            self.review_note.setText(
                "Could not compare these two - too much of the frame has moved "
                "for a difference map to mean anything."
            )
            self.review_wipe.clear_difference_view()
            return

        self.review_wipe.set_difference_view(local, matched, self._review_diff_overlay)
        self.review_wipe.set_position(self.wipe_slider.value() / 100.0)
        verdict = self._review_diff_map.verdict if self._review_diff_map else ""
        self.review_note.setText(
            f"{verdict}  ·  Local on the left, match on the right - drag the wipe to "
            "sweep the highlights across both."
        )

    def _step_review_candidate(self, delta):
        combo = self.candidate_combo
        if not combo.isEnabled() or combo.count() == 0:
            return
        combo.setCurrentIndex(max(0, min(combo.count() - 1, combo.currentIndex() + delta)))

    def _toggle_reviewed_from_review(self):
        from gui import review_shortcuts

        review_shortcuts.handlers(self)["toggle_reviewed"]()
        self._refresh_review_strip()

    def _pop_out_comparison(self):
        self._open_compare_dialog(self._current_entry())

    def refresh_review(self):
        """Brings the Review page up to date with the selection.

        Called on every selection change and on entering the mode. The
        expensive half - the full-resolution fetch - is deliberately NOT
        done here: it happens only when a view that needs it is asked for.
        """
        if not hasattr(self, "review_stack"):
            return
        entry = self._current_entry()

        if self._current_review_key() != self._review_loaded_for:
            # The pair on screen is no longer the pair selected.
            self._review_loaded_for = None
            self.review_wipe.clear_difference_view()
            self.review_wipe.set_images(None, None)
            self.review_note.setText("")
            if self.review_stack.currentIndex() == 1:
                # Already looking at the comparison, so fetch the new one.
                key = next((k for k, b in self.review_view_buttons.items()
                            if b.isChecked()), "wipe")
                self._on_review_view_changed(key)

        combo = self.candidate_combo
        if entry is not None and combo.count():
            self.review_match_label.setText(
                f"Match {combo.currentIndex() + 1} of {combo.count()}")
        else:
            self.review_match_label.setText("No match")

        if entry is not None:
            self.review_mark_btn.setText(
                "Unmark reviewed" if entry.reviewed else "Mark reviewed")
        self._refresh_review_strip()

    def _refresh_review_strip(self):
        """The line along the bottom: where you are, and how much is left."""
        if not hasattr(self, "review_position_label"):
            return
        total = self.table_model.visible_count()
        entry = self._current_entry()
        position = ""
        row = None
        if entry is not None:
            row = self.table_model.row_of(entry)
            if row is not None:
                position = f"{row + 1} of {total}"
        self.review_prev_btn.setEnabled(total > 0 and row != 0)
        self.review_next_btn.setEnabled(total > 0 and (row is None or row < total - 1))
        self.review_prev_btn.setText(self._shortcut_label("◂ Previous", "prev_row"))
        self.review_next_btn.setText(self._shortcut_label("Next ▸", "next_row"))
        self.review_next_unreviewed_btn.setText(
            self._shortcut_label("Next unreviewed", "next_unreviewed"))
        left = sum(1 for e in self.entries if e.needs_review)
        parts = [p for p in (position, f"{left} still to review" if left else "") if p]
        parts.append(self._position_strip_hint())
        self.review_position_label.setText("  ·  ".join(parts))
