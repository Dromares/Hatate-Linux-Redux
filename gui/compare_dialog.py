from __future__ import annotations

import os
from typing import Optional

from PyQt6.QtCore import Qt, QPointF, QRectF, QSizeF, pyqtSignal
from PyQt6.QtGui import QImage, QPainter, QPen, QPixmap, QColor
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QRadioButton, QSizePolicy,
    QSlider, QVBoxLayout, QWidget,
)

from core.applog import get_logger
from core.config import Settings
from core.image_compare import (
    compare_file_sizes, compare_sizes, describe_perceptual_match, dhash, hamming_distance,
)
from core.models import ImageEntry
from core.search_engine import download_bytes, fetch_candidate_details
from gui import theme, widgets

log = get_logger("gui.compare")


class WipeView(QWidget):
    """Draws two images in one frame, split by a movable vertical wipe.

    Both are scaled into the same box first, so the comparison works even
    though the two are almost always different resolutions - which is the
    normal case, and the reason a pixel-aligned overlay would be the
    wrong tool. What this shows is composition, crop and edit
    differences; the perceptual hash answers "is it the same image".

    The view zooms and pans: fitting a 4000px scan into a 500px box hides
    exactly the compression artefacts and resampling softness that decide
    which copy is better, so the box has to be able to show real pixels.
    Zoom applies to the shared target rectangle rather than to either
    image, so the two stay registered with each other at every zoom level
    and the wipe keeps landing on the same feature in both.
    """

    zoom_changed = pyqtSignal(float)  # current scale as a % of the match's real pixels

    MIN_ZOOM = 0.25   # relative to "fits the box"
    MAX_ZOOM = 40.0

    def __init__(self, parent=None, mode_getter=None):
        super().__init__(parent)
        # A callable, not a value, same reasoning as ChipDelegate's
        # (gui/table_delegates.py): the theme can change while this view
        # is open. Defaults to always-dark for a caller (tests, mainly)
        # that has no settings to resolve a mode from.
        self._mode_getter = mode_getter or (lambda: "dark")
        self.left_pixmap: Optional[QPixmap] = None
        self.right_pixmap: Optional[QPixmap] = None
        # Differences view: both pictures side by side with the highlight
        # overlay swept across BOTH by the same wipe, rather than one
        # picture split down the middle. See set_difference_view.
        self.diff_local: Optional[QPixmap] = None
        self.diff_match: Optional[QPixmap] = None
        self.diff_overlay: Optional[QPixmap] = None
        self.position = 0.5
        self._zoom = 1.0                 # 1.0 = scaled to fit the widget
        self._pan = QPointF(0.0, 0.0)    # offset from centred, in widget pixels
        self._drag_from: Optional[QPointF] = None
        self._drag_pan = QPointF(0.0, 0.0)
        self.setMinimumSize(420, 420)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)  # so +/-/0 reach us
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def _wipe_line_colour(self) -> QColor:
        """The split-line's own colour: `on_accent`, the one ink-ramp
        token that is the same fixed white in both DARK and LIGHT
        (gui/theme.py) rather than flipping with the mode - this line is
        drawn over a photograph of unknown brightness, not over the
        app's own chrome, so it needs to stay visible regardless of
        either the theme or the picture beneath it, the same invariant
        the old near-white literal was chosen for."""
        return QColor(theme.palette(self._mode_getter())["on_accent"])

    def _pane_divider_colour(self) -> QColor:
        """The secondary line between the two Differences panes: the
        same invariant colour as the wipe line, dimmed - carried by
        alpha the way the ink ramp's own ink_65/ink_45/etc. tiers carry
        "less emphasis" by alpha, rather than by a second hue."""
        colour = self._wipe_line_colour()
        colour.setAlpha(140)
        return colour

    # -- geometry -------------------------------------------------------
    def _reference(self) -> Optional[QPixmap]:
        """The pixmap whose aspect ratio defines the shared box. The local
        file wins where we have it: it's the thing being judged, so it's
        the one that must not be the distorted side."""
        for pixmap in (self.left_pixmap, self.right_pixmap):
            if pixmap is not None and not pixmap.isNull():
                return pixmap
        return None

    def showing_differences(self) -> bool:
        return self.diff_local is not None and self.diff_match is not None

    def _reference_size(self) -> Optional[QSizeF]:
        """The shape the view has to frame.

        Side by side that is the pair, so the box is twice as wide as one
        picture - otherwise fit-to-window would frame a single image and
        push half the comparison off screen.
        """
        local = self.diff_local
        if self.showing_differences() and local is not None:
            size = local.size()
            return QSizeF(size.width() * 2.0, float(size.height()))
        reference = self._reference()
        return QSizeF(reference.size()) if reference is not None else None

    def _fit_size(self) -> Optional[QSizeF]:
        reference = self._reference_size()
        if reference is None:
            return None
        return reference.scaled(
            QSizeF(self.size()), Qt.AspectRatioMode.KeepAspectRatio,
        )

    def _target_rect(self) -> Optional[QRectF]:
        fit = self._fit_size()
        if fit is None:
            return None
        width = fit.width() * self._zoom
        height = fit.height() * self._zoom
        return QRectF(
            (self.width() - width) / 2.0 + self._pan.x(),
            (self.height() - height) / 2.0 + self._pan.y(),
            width, height,
        )

    def _clamp_pan(self):
        """Keeps the image's edges from being dragged inside the viewport,
        so it can't be lost off-screen. An axis that already fits is
        pinned to centre - panning it would only add wobble."""
        fit = self._fit_size()
        if fit is None:
            return
        slack_x = max(0.0, (fit.width() * self._zoom - self.width()) / 2.0)
        slack_y = max(0.0, (fit.height() * self._zoom - self.height()) / 2.0)
        self._pan = QPointF(
            max(-slack_x, min(slack_x, self._pan.x())),
            max(-slack_y, min(slack_y, self._pan.y())),
        )

    # -- zoom API -------------------------------------------------------
    def zoom_percent(self) -> float:
        """Current scale as a percentage of the reference image's real
        pixels - 100% means one image pixel per screen pixel, which is the
        number that actually matters when judging detail."""
        reference = self._reference()
        target = self._target_rect()
        if reference is None or target is None or reference.width() == 0:
            return 0.0   # nothing loaded - the caller shows no figure at all
        return target.width() / reference.width() * 100.0

    def _apply_zoom(self, anchor: QPointF, new_zoom: float):
        """Zooms so whatever sits under `anchor` stays under it."""
        fit = self._fit_size()
        if fit is None:
            return
        new_zoom = max(self.MIN_ZOOM, min(self.MAX_ZOOM, new_zoom))
        target = self._target_rect()
        if target is None or abs(new_zoom - self._zoom) < 1e-9:
            return

        fx = (anchor.x() - target.left()) / target.width()
        fy = (anchor.y() - target.top()) / target.height()
        self._zoom = new_zoom
        width = fit.width() * new_zoom
        height = fit.height() * new_zoom
        self._pan = QPointF(
            anchor.x() - fx * width - (self.width() - width) / 2.0,
            anchor.y() - fy * height - (self.height() - height) / 2.0,
        )
        self._clamp_pan()
        self.zoom_changed.emit(self.zoom_percent())
        self.update()

    def _centre(self) -> QPointF:
        return QPointF(self.width() / 2.0, self.height() / 2.0)

    def zoom_in(self):
        self._apply_zoom(self._centre(), self._zoom * 1.25)

    def zoom_out(self):
        self._apply_zoom(self._centre(), self._zoom / 1.25)

    def zoom_to_fit(self):
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self.zoom_changed.emit(self.zoom_percent())
        self.update()

    def zoom_to_actual(self):
        """One image pixel per screen pixel."""
        fit = self._fit_size()
        reference = self._reference()
        if fit is None or reference is None or fit.width() == 0:
            return
        self._apply_zoom(self._centre(), reference.width() / fit.width())

    # -- content --------------------------------------------------------
    def set_images(self, left: Optional[QPixmap], right: Optional[QPixmap]):
        self.left_pixmap = left
        self.right_pixmap = right
        self.zoom_to_fit()   # a new pair starts framed, not wherever the last one was

    def set_difference_view(self, local: Optional[QPixmap], match: Optional[QPixmap],
                            overlay: Optional[QPixmap]):
        """Show both pictures side by side, with `overlay` swept across
        both by the wipe.

        The wipe stops being a split between two images here and becomes a
        reveal: at 30% the leftmost 30% of EACH picture carries the
        highlights, so the same region is marked on both at once and can
        be read against what is actually there.
        """
        self.diff_local = local
        self.diff_match = match
        self.diff_overlay = overlay
        self.zoom_to_fit()

    def clear_difference_view(self):
        self.diff_local = self.diff_match = self.diff_overlay = None
        self.zoom_to_fit()

    def set_position(self, fraction: float):
        self.position = max(0.0, min(1.0, fraction))
        self.update()

    # -- interaction ----------------------------------------------------
    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta:
            # Exponential so each notch feels the same at every zoom level,
            # and so a trackpad's many small deltas stay smooth.
            self._apply_zoom(event.position(), self._zoom * (1.0015 ** delta))
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = event.position()
            self._drag_pan = QPointF(self._pan)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self._drag_from is not None:
            self._pan = self._drag_pan + (event.position() - self._drag_from)
            self._clamp_pan()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._drag_from is not None:
            self._drag_from = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)

    def mouseDoubleClickEvent(self, event):
        self.zoom_to_fit()

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.zoom_in()
        elif key in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
            self.zoom_out()
        elif key == Qt.Key.Key_0:
            self.zoom_to_fit()
        elif key == Qt.Key.Key_1:
            self.zoom_to_actual()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # The fit size just changed, so the same zoom factor now means a
        # different percentage of real pixels.
        self._clamp_pan()
        self.zoom_changed.emit(self.zoom_percent())

    # -- painting -------------------------------------------------------
    def _draw_side(self, painter: QPainter, pixmap: QPixmap,
                   target: QRectF, clip: QRectF):
        """Draws only the part of `pixmap` that is actually visible.

        Handing Qt a target rectangle far larger than the window would
        make it scale the whole image every repaint just to throw most of
        it away, which at high zoom on a 4000px scan is enough to stutter
        while panning."""
        area = target.intersected(clip).intersected(QRectF(self.rect()))
        if area.isEmpty() or target.width() <= 0 or target.height() <= 0:
            return
        x_scale = pixmap.width() / target.width()
        y_scale = pixmap.height() / target.height()
        source = QRectF(
            (area.left() - target.left()) * x_scale,
            (area.top() - target.top()) * y_scale,
            area.width() * x_scale,
            area.height() * y_scale,
        )
        # Smooth while shrinking; past 1:1 show the real pixels, since at
        # that point the artefacts are the thing being looked at.
        painter.setRenderHint(
            QPainter.RenderHint.SmoothPixmapTransform, x_scale >= 1.0,
        )
        painter.drawPixmap(area, pixmap, source)

    def _paint_differences(self, painter: QPainter, target: QRectF):
        """Both pictures side by side, highlights swept across both.

        Each pane is the LOCAL picture's shape. The match is drawn into
        that shape rather than its own, which is the same thing the
        comparison itself did to line the two up - so what is on screen
        matches what was actually compared.
        """
        half = target.width() / 2.0
        panes = (
            QRectF(target.left(), target.top(), half, target.height()),
            QRectF(target.left() + half, target.top(), half, target.height()),
        )
        for pane, pixmap in zip(panes, (self.diff_local, self.diff_match), strict=True):
            if pixmap is not None and not pixmap.isNull():
                self._draw_side(painter, pixmap, pane, pane)

        if self.diff_overlay is not None and not self.diff_overlay.isNull():
            for pane in panes:
                revealed = QRectF(
                    pane.left(), pane.top(), pane.width() * self.position, pane.height(),
                )
                if revealed.width() > 0:
                    self._draw_side(painter, self.diff_overlay, pane, revealed)

        # One line per pane, at the same place in each, so the eye can
        # carry a feature straight across.
        pen = QPen(self._wipe_line_colour())
        pen.setWidth(2)
        painter.setPen(pen)
        top = max(target.top(), 0.0)
        bottom = min(target.bottom(), float(self.height()))
        if bottom > top:
            for pane in panes:
                x = pane.left() + pane.width() * self.position
                painter.drawLine(QPointF(x, top), QPointF(x, bottom))
        # And a divider between the two, so they read as two pictures.
        if bottom > top:
            pen.setColor(self._pane_divider_colour())
            painter.setPen(pen)
            painter.drawLine(QPointF(target.left() + half, top),
                             QPointF(target.left() + half, bottom))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().window())

        target = self._target_rect()
        if target is None:
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Nothing to compare")
            return

        if self.showing_differences():
            self._paint_differences(painter, target)
            return

        split_x = target.left() + target.width() * self.position

        if self.left_pixmap is not None and not self.left_pixmap.isNull():
            self._draw_side(
                painter, self.left_pixmap, target,
                QRectF(target.left(), target.top(),
                       split_x - target.left(), target.height()),
            )
        if self.right_pixmap is not None and not self.right_pixmap.isNull():
            self._draw_side(
                painter, self.right_pixmap, target,
                QRectF(split_x, target.top(),
                       target.right() - split_x, target.height()),
            )
        elif self.left_pixmap is not None:
            # There IS a local image but no matched one. Leaving this half
            # blank looked like the match simply hadn't changed, so say
            # what actually happened.
            empty = QRectF(split_x, target.top(),
                           target.right() - split_x, target.height())
            painter.save()
            painter.setClipRect(empty)
            painter.setPen(self.palette().windowText().color())
            painter.drawText(empty, Qt.AlignmentFlag.AlignCenter,
                             "The match's picture\ncould not be fetched")
            painter.restore()

        pen = QPen(self._wipe_line_colour())
        pen.setWidth(2)
        painter.setPen(pen)
        top = max(target.top(), 0.0)
        bottom = min(target.bottom(), float(self.height()))
        if bottom > top:
            painter.drawLine(QPointF(split_x, top), QPointF(split_x, bottom))


def build_difference_overlay(local_pixmap, matched_pixmap):
    """(overlay, DifferenceMap) - a transparent red highlight layer at
    the local image's size, or (None, None) if it cannot be built.

    Transparent rather than a finished composite, because it is now
    painted OVER two different pictures - the local one and the match
    - so it cannot carry either of them baked in.
    """
    from PIL import Image as PILImage
    from core.image_diff import difference_map

    local = _pil_from_pixmap(local_pixmap)
    match = _pil_from_pixmap(matched_pixmap)
    if local is None or match is None:
        return None, None
    try:
        result = difference_map(local, match)
    except Exception as exc:
        log.warning("Could not build the difference view: %s", exc)
        return None, None
    if result.mask is None:
        return None, None

    # Composed at the LOCAL image's own size rather than the smaller
    # size the comparison ran at, so the clean and highlighted views
    # are the same picture at the same resolution - which is what lets
    # the wipe land on the same feature in both, at any zoom.
    mask = result.mask
    if mask.size != local.size:
        mask = mask.resize(local.size, PILImage.Resampling.LANCZOS)
    # Red where the mask is bright, clear where it is dark. The alpha
    # follows the mask rather than being flat, so a strong difference
    # reads as solid and a marginal one as a tint - which keeps the
    # borderline cases from looking as certain as the obvious ones.
    overlay = PILImage.merge("RGBA", (
        PILImage.new("L", mask.size, 255),
        PILImage.new("L", mask.size, 40),
        PILImage.new("L", mask.size, 40),
        mask.point(lambda v: int(v * 0.82)),   # never fully opaque: the
    ))                                          # picture underneath is the point
    return _pixmap_from_pil_rgba(overlay), result


class LoadedComparison:
    """The two pictures to compare, and what had to be settled for.

    `used_fallback` matters: it means the match came back as a downscaled
    sample rather than the real file, and a comparison against a sample
    is close to worthless for judging quality. Whoever shows it has to
    say so.
    """

    __slots__ = ("local", "matched", "matched_bytes", "used_fallback", "candidate")

    def __init__(self, local, matched, matched_bytes, used_fallback, candidate):
        self.local = local
        self.matched = matched
        self.matched_bytes = matched_bytes
        self.used_fallback = used_fallback
        self.candidate = candidate


def load_comparison(entry: ImageEntry, settings: Settings) -> LoadedComparison:
    """Fetches an entry's match at FULL resolution, beside the local file.

    Deliberately not the sample shown in the preview panel: that is a
    downscaled copy, so comparing against it would misrepresent exactly
    the quality difference a comparison exists to judge.

    Synchronous, and it can spend a moment on the network. It always has
    been - the dialog called this from its own __init__ before showing -
    so anywhere else that calls it pays the same price the dialog did,
    and should say "Loading…" first for the same reason.
    """
    candidate = entry.selected_candidate
    loaded_pixmap = QPixmap(entry.path)
    local_pixmap: Optional[QPixmap] = loaded_pixmap
    if loaded_pixmap.isNull():
        log.warning("Could not load local image for comparison: %s", entry.path)
        local_pixmap = None

    matched_pixmap = None
    matched_bytes = None
    used_fallback = False
    if candidate is not None:
        # Only the candidate the user has actually visited has had its
        # details fetched. Picking a different one from the dropdown
        # and opening this window went straight to the search engine's
        # thumbnail - and those URLs are signed and expire, so for
        # anything more than a few hours old it fetched nothing at all
        # and the window showed no matched image while saying nothing
        # about why. Fetch on demand instead, exactly as the main
        # window does when a candidate is selected.
        if not candidate.direct_file_url and not candidate.preview_url:
            log.info("Fetching details for the selected match before comparing: %s",
                     candidate.url)
            try:
                fetch_candidate_details(candidate, settings, local_path=entry.path)
            except Exception as exc:
                log.warning("Could not fetch match details for %s: %s", candidate.url, exc)

        # Prefer the real file; fall back to whatever preview we
        # already have rather than showing nothing - but remember
        # that we did, so the caller can say so. A comparison
        # against a downscaled sample is close to worthless, and
        # silently substituting one is how it becomes confusing
        # rather than obviously limited.
        url = candidate.direct_file_url
        if not url:
            url = candidate.preview_url or candidate.thumb_url
            used_fallback = True
        if url:
            log.info("Fetching match at full resolution for comparison: %s", url)
            matched_bytes = download_bytes(
                url, settings.hydrus.timeout,
                referer=candidate.url if url == candidate.direct_file_url else None,
            )
        if matched_bytes:
            image = QImage()
            if image.loadFromData(matched_bytes):
                matched_pixmap = QPixmap.fromImage(image)
            else:
                log.warning("Fetched %d bytes for the match but Qt couldn't decode it",
                            len(matched_bytes))

    return LoadedComparison(local_pixmap, matched_pixmap, matched_bytes,
                            used_fallback, candidate)


class CompareDialog(QDialog):
    """Local file against its matched image.

    Fetches the match at FULL resolution rather than reusing the sample
    shown in the main preview: the sample is deliberately a downscaled
    copy, so comparing against it would misrepresent exactly the quality
    difference this window exists to judge.
    """

    def __init__(self, entry: ImageEntry, settings: Settings, parent=None):
        super().__init__(parent)
        self.entry = entry
        self.settings = settings
        self.setWindowTitle(f"Compare - {entry.filename}")
        self.resize(900, 780)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(*widgets.PAGE_MARGINS)
        layout.setSpacing(widgets.PAGE_SPACING)

        self.summary_label = QLabel("Loading…")
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextFormat(Qt.TextFormat.RichText)
        # The dialog's one load-bearing readout - promoted off the plain
        # body weight so it reads as the verdict, not more mode/zoom chrome.
        widgets.restyle(self.summary_label, 'Heading')
        layout.addWidget(self.summary_label)

        self.view = WipeView(mode_getter=lambda: theme.resolve_mode(self.settings.theme))
        self.view.setToolTip(
            "Scroll to zoom where the pointer is, drag to pan, double-click to fit.\n"
            "+ / - zoom, 0 fits, 1 shows actual pixels."
        )
        self.view.zoom_changed.connect(self._on_zoom_changed)
        layout.addWidget(self.view, stretch=1)

        mode_row = QHBoxLayout()
        self.wipe_radio = QRadioButton("Wipe")
        self.wipe_radio.setChecked(True)
        self.wipe_radio.toggled.connect(self._apply_mode)
        self.local_radio = QRadioButton("Local only")
        self.local_radio.toggled.connect(self._apply_mode)
        self.matched_radio = QRadioButton("Match only")
        self.matched_radio.toggled.connect(self._apply_mode)
        self.diff_radio = QRadioButton("Differences")
        self.diff_radio.setToolTip(
            "Shows both pictures side by side - your local copy on the left, the match "
            "on the right - and sweeps a red highlight of their differences across both "
            "as you drag the wipe: a watermark, a signature, added text, a censoring "
            "bar.\n\n"
            "Deliberately approximate. The two are almost always different resolutions, "
            "so they are scaled to a common size and compared structurally: it shows "
            "WHICH AREAS differ, not which exact pixels. Re-encoding noise is filtered "
            "out, which is why a mere re-save shows as no difference at all.\n\n"
            "Whether it is the same image is a separate question, and the perceptual "
            "match in the summary above answers it better."
        )
        self.diff_radio.toggled.connect(self._apply_mode)
        for widget in (self.wipe_radio, self.local_radio, self.matched_radio, self.diff_radio):
            mode_row.addWidget(widget)
        mode_row.addStretch()

        # Zoom sits on the same row as the view modes: both are "how am I
        # looking at this", as opposed to the wipe slider below, which is
        # "what am I looking at".
        zoom_out_button = QPushButton("−")
        zoom_out_button.setFixedWidth(32)
        zoom_out_button.setToolTip("Zoom out (-)")
        zoom_out_button.clicked.connect(self.view.zoom_out)

        zoom_in_button = QPushButton("+")
        zoom_in_button.setFixedWidth(32)
        zoom_in_button.setToolTip("Zoom in (+)")
        zoom_in_button.clicked.connect(self.view.zoom_in)

        fit_button = QPushButton("Fit")
        fit_button.setToolTip("Fit the whole image in the window (0, or double-click)")
        fit_button.clicked.connect(self.view.zoom_to_fit)

        actual_button = QPushButton("100%")
        actual_button.setToolTip(
            "One image pixel per screen pixel (1) - the view that shows compression "
            "artefacts and resampling softness as they really are."
        )
        actual_button.clicked.connect(self.view.zoom_to_actual)

        self.zoom_label = QLabel("")
        self.zoom_label.setMinimumWidth(56)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        for zoom_widget in (zoom_out_button, zoom_in_button, fit_button,
                            actual_button, self.zoom_label):
            mode_row.addWidget(zoom_widget)
        layout.addLayout(mode_row)

        # What the differences view found. Hidden in the other modes, so
        # it never describes something that is not on screen.
        self.diff_note = QLabel("")
        self.diff_note.setWordWrap(True)
        self.diff_note.setVisible(False)
        layout.addWidget(self.diff_note)

        slider_row = QHBoxLayout()
        # Named per mode: the right-hand side is the matched image under
        # the wipe, but the highlighted copy in the differences view, and
        # a label that says "Match" while showing highlights is a small
        # lie the user has to work around.
        self.slider_left_label = QLabel("Local")
        self.slider_right_label = QLabel("Match")
        slider_row.addWidget(self.slider_left_label)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setValue(50)
        self.slider.valueChanged.connect(lambda v: self.view.set_position(v / 100.0))
        slider_row.addWidget(self.slider, stretch=1)
        slider_row.addWidget(self.slider_right_label)
        layout.addLayout(slider_row)

        button_row = QHBoxLayout()
        button_row.addStretch()
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)
        button_row.addWidget(close_button)
        layout.addLayout(button_row)

        self._load()

    def _on_zoom_changed(self, percent: float):
        self.zoom_label.setText(f"{percent:.0f}%" if percent else "")

    def _apply_mode(self):
        """Wipe / local-only / match-only. The single-image modes exist
        because flicking between two full views is often a better way to
        spot a subtle difference than a static split."""
        if self.diff_radio.isChecked():
            self.slider.setEnabled(True)
            self.slider_right_label.setText("Differences")
            self._show_differences()
            return
        self.slider_right_label.setText("Match")
        self.diff_note.setVisible(False)
        if self.view.showing_differences():
            self.view.clear_difference_view()
        if self.local_radio.isChecked():
            self.slider.setEnabled(False)
            self.view.set_position(1.0)
        elif self.matched_radio.isChecked():
            self.slider.setEnabled(False)
            self.view.set_position(0.0)
        else:
            self.slider.setEnabled(True)
            self.view.set_position(self.slider.value() / 100.0)

    def _show_differences(self):
        """Wipes between the clean local image and the highlighted one.

        Over the LOCAL copy deliberately: that is the one being kept or
        replaced, so "what would I be losing or gaining" reads against it.

        The wipe earns its keep here rather than being a no-op. Dragging
        it back and forth over a highlighted patch is how you tell a real
        watermark from a smear the comparison invented - the clean picture
        and the marked one are the same image at the same size, so the
        split lands on the same feature in both at every zoom level.

        The overlay is built once and reused, since recomputing it on
        every mode switch would stall the window for no reason.
        """
        if self._diff_pixmap is None:
            self._diff_pixmap, self._diff_map = self._build_difference_overlay()
        if self._diff_pixmap is None:
            self.diff_note.setText("Could not compare these two.")
            self.diff_note.setVisible(True)
            return
        self.view.set_difference_view(
            self._local_pixmap, self._matched_pixmap, self._diff_pixmap,
        )
        self.view.set_position(self.slider.value() / 100.0)
        note = self._diff_map.verdict if self._diff_map else ""
        self.diff_note.setText(
            f"{note}  ·  Local on the left, match on the right - drag the wipe to sweep "
            "the highlights across both."
        )
        self.diff_note.setVisible(True)

    def _build_difference_overlay(self):
        return build_difference_overlay(self._local_pixmap, self._matched_pixmap)

    def _load(self):
        loaded = load_comparison(self.entry, self.settings)
        self._used_fallback = loaded.used_fallback
        self._local_pixmap = loaded.local
        self._matched_pixmap = loaded.matched
        self._diff_pixmap = None      # built on demand, the first time it is asked for
        self._diff_map = None
        self.view.set_images(loaded.local, loaded.matched)
        self.summary_label.setText(self._build_summary(
            loaded.candidate, loaded.matched_bytes, loaded.local, loaded.matched))
        self.diff_radio.setEnabled(loaded.local is not None and loaded.matched is not None)

    def _build_summary(self, candidate, matched_bytes, local_pixmap, matched_pixmap) -> str:
        if candidate is None:
            return "<b>No match selected for this image.</b>"

        local_w = local_pixmap.width() if local_pixmap else (self.entry.local_width or 0)
        local_h = local_pixmap.height() if local_pixmap else (self.entry.local_height or 0)
        remote_w = matched_pixmap.width() if matched_pixmap else (candidate.width or 0)
        remote_h = matched_pixmap.height() if matched_pixmap else (candidate.height or 0)

        size_cmp = compare_sizes(local_w, local_h, remote_w, remote_h)

        try:
            local_bytes = os.path.getsize(self.entry.path)
        except OSError:
            local_bytes = None
        file_cmp = compare_file_sizes(
            local_bytes, len(matched_bytes) if matched_bytes else candidate.remote_size_bytes,
        )

        distance = None
        if matched_bytes:
            distance = hamming_distance(dhash(self.entry.path), dhash(matched_bytes))
        verdict = describe_perceptual_match(distance)

        mode = theme.resolve_mode(self.settings.theme)
        # ink_65 ("good"/"sent"'s tier) when the match is the bigger
        # copy, ink_100 ("needs a decision" - poor/not_found/error's
        # tier) otherwise - the same reading gui/preview_text.py's
        # banner gives this exact comparison in the preview panel.
        good = theme.ink_color(mode, "ink_65").name()
        uncertain = theme.ink_color(mode, "ink_100").name()
        colour = good if size_cmp.remote_is_bigger else uncertain
        lines = [
            f"<b>Local:</b> {local_w}\u00d7{local_h}"
            + (f" &nbsp;·&nbsp; {_human(local_bytes)}" if local_bytes else ""),
            f"<b>Match:</b> {remote_w}\u00d7{remote_h}"
            + (f" &nbsp;·&nbsp; {_human(len(matched_bytes))}" if matched_bytes else ""),
        ]
        if size_cmp.verdict:
            lines.append(f'<b style="color:{colour}">The match is {size_cmp.verdict}</b>'
                         + (f" &nbsp;·&nbsp; {file_cmp}" if file_cmp else ""))
        lines.append(verdict)
        if matched_pixmap is None:
            lines.append(
                f'<b style="color:{uncertain}">The match\'s picture could not be fetched, so '
                'there is nothing to compare against.</b> The site may be refusing it, or the '
                'link may have expired - see Help &gt; View Logs.'
            )
        if getattr(self, "_used_fallback", False):
            # Italic + ink_65, same as the Status column's "searching"
            # treatment (gui/table_delegates.py): informational, not a
            # decision pending on the user.
            lines.append(
                f'<i style="color:{good}">No full-resolution URL was available for this match, '
                'so this is the site\'s downscaled preview - the quality difference shown here '
                'understates the real one.</i>'
            )
        if size_cmp.aspect_differs:
            lines.append(
                '<i>The two have different proportions, so the match may be cropped '
                'differently or be a different edit.</i>'
            )
        return "<br>".join(lines)


def _pil_from_pixmap(pixmap: Optional[QPixmap]):
    """A QPixmap as a PIL image, or None.

    Goes through a known 24-bit format rather than whatever the pixmap
    happens to hold: Qt pads scanlines, so reading the buffer without
    fixing the format and stride produces a skewed image.
    """
    if pixmap is None or pixmap.isNull():
        return None
    from PIL import Image as PILImage
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGB888)
    width, height = image.width(), image.height()
    if not width or not height:
        return None
    buffer = image.constBits()
    if buffer is None:
        return None
    buffer.setsize(image.sizeInBytes())
    return PILImage.frombytes(
        "RGB", (width, height), bytes(buffer),  # type: ignore[call-overload]  # PyQt6/sip stub: voidptr isn't in bytes()'s overloads though the buffer protocol supports it
        "raw", "RGB", image.bytesPerLine(),
    )


def _pixmap_from_pil_rgba(image) -> Optional[QPixmap]:
    """A PIL RGBA image as a QPixmap, transparency intact."""
    if image is None:
        return None
    rgba = image.convert("RGBA")
    data = rgba.tobytes("raw", "RGBA")
    qimage = QImage(data, rgba.width, rgba.height, rgba.width * 4,
                    QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(qimage.copy())


def _pixmap_from_pil(image) -> Optional[QPixmap]:
    """A PIL RGB image as a QPixmap."""
    if image is None:
        return None
    rgb = image.convert("RGB")
    data = rgb.tobytes("raw", "RGB")
    qimage = QImage(data, rgb.width, rgb.height, rgb.width * 3, QImage.Format.Format_RGB888)
    # copy(): QImage does not own `data`, and letting it go out of scope
    # under a live QPixmap is a use-after-free.
    return QPixmap.fromImage(qimage.copy())


def _human(num_bytes: Optional[int]) -> str:
    if not num_bytes:
        return ""
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""
