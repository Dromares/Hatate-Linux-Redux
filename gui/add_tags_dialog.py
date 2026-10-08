from __future__ import annotations

from typing import List, Optional

from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QVBoxLayout

from core.models import Tag, TagSource
from gui import widgets


class AddTagsDialog(QDialog):
    """Opened on demand via the 'Add tags…' button next to the tag list -
    lets the user type tags to add to the currently selected image(s)
    (one per line, optionally namespace:tag)."""

    def __init__(self, target_description: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add tags")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(*widgets.PAGE_MARGINS)
        layout.setSpacing(widgets.PAGE_SPACING)
        layout.addWidget(QLabel(
            f"Add tags to {target_description} (one per line, optionally namespace:tag):"
        ))
        self.edit = QPlainTextEdit()
        layout.addWidget(self.edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_tags(self) -> List[Tag]:
        tags = []
        for line in self.edit.toPlainText().splitlines():
            line = line.strip()
            if not line:
                continue
            namespace: Optional[str] = None
            name = line
            if ":" in line:
                # Both halves, not just the name: "character : reimu" would
                # otherwise keep the space in the namespace and reach Hydrus
                # as "character :reimu", a second namespace beside the real
                # one. An empty left half is no namespace at all, so the tag
                # de-duplicates against the same one typed without a colon.
                head, name = line.split(":", 1)
                namespace = head.strip() or None
                name = name.strip()
            tags.append(Tag(name=name, source=TagSource.USER, namespace=namespace))
        return tags
