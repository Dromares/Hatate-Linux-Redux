"""Whether the Lens browser window may call for the user - on KDE Plasma.

The Lens window asks for attention during an ordinary search. CONFIRMED
on Plasma 6 (Wayland): KWin's focus-stealing prevention turns that into
"demands attention", and an auto-hide panel then stays up until the
window is clicked - on every search, for a window that needs nothing.

So the window is kept quiet - off the taskbar, its attention flag
cleared - except while Google is asking for a robot check, which is the
one time it does need the user: then it is put back on the taskbar and
flagged, and the panel coming up is the point.

Done with a one-line KWin script sent over D-Bus, which changes only the
window whose class is lens_browser.WINDOW_CLASS. Anywhere that isn't
Plasma, or without qdbus6, every call is a no-op: this is a courtesy,
and a search must never depend on it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from .applog import get_logger

log = get_logger("desktop_attention")

SCRIPT_NAME = "hatate-lens-attention"
QDBUS_TIMEOUT = 5.0

_lock = threading.Lock()


def available() -> bool:
    return "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper() and bool(_qdbus())


def quiet(window_class: str) -> None:
    """Off the taskbar, and any attention it has asked for dropped."""
    _apply(window_class, skip_taskbar=True, demands_attention=False)


def ask_for_input(window_class: str) -> None:
    """Back on the taskbar and flagged, so the panel shows it."""
    _apply(window_class, skip_taskbar=False, demands_attention=True)


def script_for(window_class: str, skip_taskbar: bool, demands_attention: bool) -> str:
    """The KWin script. Public for tests."""
    js = lambda value: "true" if value else "false"   # noqa: E731
    return (
        "const ws = workspace.windowList ? workspace.windowList() : workspace.clientList();\n"
        "for (const w of ws) {\n"
        f"  if (w.resourceClass !== {window_class!r}) continue;\n"
        f"  w.skipTaskbar = {js(skip_taskbar)};\n"
        f"  w.demandsAttention = {js(demands_attention)};\n"
        "}\n"
    )


def _qdbus():
    return shutil.which("qdbus6") or shutil.which("qdbus-qt6")


def _apply(window_class: str, skip_taskbar: bool, demands_attention: bool) -> None:
    if not available():
        return
    qdbus = _qdbus()
    script = script_for(window_class, skip_taskbar, demands_attention)
    with _lock:
        path = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
                fh.write(script)
                path = fh.name
            base = [qdbus, "org.kde.KWin", "/Scripting"]
            # A previous run's script of the same name would block loading.
            _run(base + ["org.kde.kwin.Scripting.unloadScript", SCRIPT_NAME])
            _run(base + ["org.kde.kwin.Scripting.loadScript", path, SCRIPT_NAME])
            _run(base + ["org.kde.kwin.Scripting.start"])
            _run(base + ["org.kde.kwin.Scripting.unloadScript", SCRIPT_NAME])
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("Could not set the Lens window's taskbar state: %s", exc)
        finally:
            if path:
                Path(path).unlink(missing_ok=True)


def _run(command) -> None:
    subprocess.run(command, capture_output=True, timeout=QDBUS_TIMEOUT, check=False)
