"""Message boxes that never become native dialogs.

On KDE the platform theme shows a QMessageBox as a *native* dialog. The
helper object behind that native dialog can already be gone by the time
the box is destroyed, and Qt still calls hide() on it - a virtual call
through a dead pointer, which segfaults inside QMessageBox's destructor
with no Python traceback and nothing in the log. Confirmed from a core
dump: QMessageBox::~QMessageBox -> QMessageBoxPrivate::setVisible(false)
-> QDialogPrivate::setNativeDialogVisible(false) -> call *0x78(vptr),
with the dialog's `nativeDialogInUse` flag set.

Qt's own message box is indistinguishable here and cannot take that
path, so these wrappers ask for it explicitly. They mirror the static
QMessageBox helpers they replace, including their default buttons.

File dialogs are deliberately left native: KDE's is genuinely better on
the remote/network paths this app is used with, and has never shown this
problem.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtWidgets import QMessageBox, QWidget

_OK = QMessageBox.StandardButton.Ok
_YES_NO = QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
_NONE = QMessageBox.StandardButton.NoButton


def build(parent: Optional[QWidget] = None) -> QMessageBox:
    """A QMessageBox that will not be shown as a native dialog.

    Use this instead of QMessageBox(parent) anywhere the box needs
    building up by hand (detailed text, custom buttons)."""
    box = QMessageBox(parent)
    box.setOption(QMessageBox.Option.DontUseNativeDialog, True)
    return box


def _show(icon, parent, title, text, buttons, default):
    box = build(parent)
    box.setIcon(icon)
    box.setWindowTitle(title)
    box.setText(text)
    box.setStandardButtons(buttons)
    if default != _NONE:
        box.setDefaultButton(default)
    try:
        return box.exec()
    finally:
        # Don't leave it parented to the window for the rest of the
        # session; deleteLater keeps the result above readable.
        #
        # box.exec() runs its own nested event loop, and a parent whose
        # deleteLater() was already posted (e.g. a window a stale worker
        # signal is still reporting to, long after the window moved on)
        # can be destroyed from inside that loop - which destroys this
        # box right along with it, since it is parented to that window.
        # deleteLater() on the now-gone C/C++ object then raises
        # RuntimeError, and PyQt cannot carry that out through the
        # (indirectly) signal-driven call that got us here: it calls
        # qFatal() and aborts the whole process instead. Nothing is left
        # to delete at that point, so there is nothing to do but accept
        # it.
        try:
            box.deleteLater()
        except RuntimeError:
            pass


def information(parent, title, text, buttons=_OK, default=_NONE):
    return _show(QMessageBox.Icon.Information, parent, title, text, buttons, default)


def warning(parent, title, text, buttons=_OK, default=_NONE):
    return _show(QMessageBox.Icon.Warning, parent, title, text, buttons, default)


def critical(parent, title, text, buttons=_OK, default=_NONE):
    return _show(QMessageBox.Icon.Critical, parent, title, text, buttons, default)


def question(parent, title, text, buttons=_YES_NO, default=_NONE):
    return _show(QMessageBox.Icon.Question, parent, title, text, buttons, default)
