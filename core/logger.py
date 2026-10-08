"""Logs matched booru URLs to a plain text file, one per line, so the user
can review what was found (Files > Open Matched URLs menu).

Rotated like app.log: past MAX_BYTES the file becomes .1 (and .1 becomes
.2, up to BACKUPS), so a long-running library doesn't grow it without
bound - one real one had reached 5MB in five weeks."""
from __future__ import annotations

import datetime
from pathlib import Path

from .applog import get_logger

log = get_logger("matched_urls_log")

MAX_BYTES = 10 * 1024 * 1024
BACKUPS = 3


def _rotate(path: Path) -> None:
    try:
        if path.stat().st_size < MAX_BYTES:
            return
    except OSError:
        return                                  # nothing there yet
    for index in range(BACKUPS - 1, 0, -1):
        older = path.with_name(f"{path.name}.{index}")
        if older.exists():
            older.replace(path.with_name(f"{path.name}.{index + 1}"))
    path.replace(path.with_name(f"{path.name}.1"))


def log_matched_url(log_path: str, image_filename: str, url: str):
    path = Path(log_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate(path)
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{timestamp}] {image_filename} -> {url}\n")
        log.debug("Logged matched URL for %s to %s", image_filename, path)
    except OSError as exc:
        log.error("Could not write matched-URL log entry to %s: %s", path, exc)
