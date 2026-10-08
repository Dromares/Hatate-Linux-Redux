from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QSpinBox, QVBoxLayout,
)

from core.config import MatchConditions
from gui import widgets


class MatchConditionsDialog(QDialog):
    """"Edit conditions under which a match is considered better than the
    local image" - controls whether a found image is flagged ● (good,
    trust it) or ◐ (review manually)."""

    def __init__(self, conditions: MatchConditions, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit match conditions")
        self.conditions = conditions

        form = QFormLayout()

        self.min_tags = QSpinBox()
        self.min_tags.setRange(0, 200)
        self.min_tags.setValue(conditions.min_tags_for_good)
        form.addRow("Minimum tags for a 'good' match:", self.min_tags)

        self.width_gain = QDoubleSpinBox()
        self.width_gain.setRange(0, 1000)
        self.width_gain.setSuffix(" %")
        self.width_gain.setValue(conditions.min_width_gain_percent)
        form.addRow("Width gain to flag for review:", self.width_gain)

        self.height_gain = QDoubleSpinBox()
        self.height_gain.setRange(0, 1000)
        self.height_gain.setSuffix(" %")
        self.height_gain.setValue(conditions.min_height_gain_percent)
        form.addRow("Height gain to flag for review:", self.height_gain)

        self.require_larger = QCheckBox("Only flag for review when the match is larger than the local image")
        self.require_larger.setChecked(conditions.require_larger_than_local)
        form.addRow(self.require_larger)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(*widgets.PAGE_MARGINS)
        layout.setSpacing(widgets.PAGE_SPACING)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def apply_to(self, conditions: MatchConditions):
        conditions.min_tags_for_good = self.min_tags.value()
        conditions.min_width_gain_percent = self.width_gain.value()
        conditions.min_height_gain_percent = self.height_gain.value()
        conditions.require_larger_than_local = self.require_larger.isChecked()
