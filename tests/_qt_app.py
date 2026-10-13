"""One process-wide QApplication that nothing can garbage-collect.

PyQt owns a QApplication it created, so the last *Python* reference decides
when it is destroyed. ``app = QApplication.instance() or QApplication([])``
bound to a function local, or to a TestCase attribute (unittest releases
TestCase instances after each test), drops that reference as soon as the
local dies. PyQt then tears the QApplication down, and the next test or
module rebuilds one in the same process. Rebuilding a QApplication
in-process corrupts Qt's global state; the next SettingsDialog segfaulted in
``QLabel.text()`` (DAN-1351, then DAN-1361 for the rest of the suite).

CI never saw it because ``discover`` happens to run a module that holds the
app (``setUpModule``) first. Reorder or shard the suite and the crash comes
back, so every test module gets its QApplication from here.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_keepalive = None


def ensure_app():
    """Return the process's QApplication, creating it once and pinning it.

    Safe to call from anywhere, any number of times. The module-level
    reference is what keeps the app alive for the rest of the process.
    """
    # Imported here, not at module level: several test modules skip on a
    # missing PyQt6 and must still be able to import this one.
    from PyQt6.QtWidgets import QApplication

    global _keepalive
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    _keepalive = app
    return app
