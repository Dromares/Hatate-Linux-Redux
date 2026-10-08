#!/usr/bin/env python3
"""Hatate-linux: native Linux rewrite of nostrenz/hatate-iqdb-tagger.

Search images via IQDB/SauceNAO, retrieve tags from the matched booru page,
review/edit them, and send files + tags + URLs to Hydrus over its Client API.
"""
import sys

from PIL import Image as PILImage
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QIcon, QImageReader
from PyQt6.QtWidgets import QApplication

from core.applog import setup_logging, get_logger
from core.crashlog import (
    check_unclean_shutdown,
    install as install_crash_handlers,
    mark_running,
)
from core.config import Settings
from core.paths import RESOURCES_DIR
from gui import theme
from gui.fonts import register_fonts
from gui.main_window import MainWindow
from workers.search_worker import register_cleanup_handlers

log = get_logger("main")


def main():
    setup_logging()
    # Immediately after logging, before anything that could fall over:
    # this is what makes a crash explain itself in app.log rather than
    # leaving the log stopped mid-sentence.
    install_crash_handlers()

    # Checked before this run's own marker goes down: still finding the
    # previous one means that run never reached a clean shutdown (DAN-485).
    unclean_shutdown = check_unclean_shutdown()
    mark_running()

    # Register atexit and signal handlers for staging directory cleanup.
    # Must be called from the main thread for signal.signal() to work.
    register_cleanup_handlers()

    # Qt (since 6.3) and Pillow both refuse to decode very large images by
    # default, as a defense against "decompression bomb" attacks (a tiny
    # file that decodes to gigabytes of pixel data). That's the right
    # default for untrusted downloads, but it also silently blocks
    # perfectly legitimate high-resolution scans/wallpapers/art the user
    # adds themselves - QPixmap(path) just returns a null pixmap with no
    # visible error, which looks like "the image won't display". Since the
    # user is deliberately adding their own local files here, raise/disable
    # both limits at startup rather than rejecting large-but-valid images.
    QImageReader.setAllocationLimit(0)  # 0 = no limit (Qt default is 256 MiB)
    PILImage.MAX_IMAGE_PIXELS = None    # disable Pillow's equivalent guard
    log.debug("Disabled Qt/Pillow image size limits for large local files")

    app = QApplication(sys.argv)
    app.setApplicationName("Hatate-linux")

    # Set before any window exists, so the taskbar, the alt-tab switcher
    # and every dialog inherit it. Nothing set this at all before, which
    # is why the app showed a generic placeholder everywhere but its own
    # .desktop entry.
    icon = QIcon(str(RESOURCES_DIR / "icon.svg"))
    if not icon.isNull():
        app.setWindowIcon(icon)

    # Before the stylesheet: it names these families in its font-family
    # rules, and a name Qt hasn't registered yet just falls through to
    # the stack's next entry with no error - see gui/fonts.py.
    register_fonts()

    # The sheet goes on the application, not the window: Qt cascades it to
    # every widget including the dialogs, which are built later and would
    # otherwise each have to remember to ask for it. Read straight from
    # the config rather than from the window, because the window builds
    # its widgets in __init__ and they should be born styled.
    mode = theme.resolve_mode(Settings.load().theme)
    app.setStyleSheet(theme.stylesheet(mode))
    log.debug("Applied %s theme", mode)

    window = MainWindow(unclean_shutdown=unclean_shutdown)
    window.show()

    # register_cleanup_handlers() installs SIGINT/SIGTERM handlers above, but
    # they can't fire on their own: Python only checks for pending signals
    # between bytecode instructions, and QApplication.exec() blocks in a
    # native event loop that never returns to the interpreter on its own. A
    # no-op timer forces that periodic return so a Ctrl+C actually reaches
    # the handler instead of leaving the process needing a SIGKILL.
    signal_wakeup = QTimer()
    signal_wakeup.timeout.connect(lambda: None)
    signal_wakeup.start(200)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
