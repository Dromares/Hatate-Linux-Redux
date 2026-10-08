"""Lightweight in-app logging so failures are debuggable without a
terminal open. Sets up standard Python logging to a rotating file under
the config directory, plus a small in-memory ring buffer the GUI's log
viewer dialog can read live without re-reading the file from disk.
"""
from __future__ import annotations

import logging
import logging.handlers
from collections import deque
from typing import Deque

from .paths import CONFIG_DIR

LOG_FILE = CONFIG_DIR / "app.log"
_RING_SIZE = 4000

_ring: Deque[str] = deque(maxlen=_RING_SIZE)
_configured = False


class _RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord):
        try:
            _ring.append(self.format(record))
        except Exception:
            pass  # logging must never itself crash the app


def setup_logging(level: int = logging.DEBUG) -> logging.Logger:
    """Call once at startup. Safe to call more than once - only configures
    handlers the first time."""
    global _configured
    logger = logging.getLogger("hatate")
    if _configured:
        return logger
    _configured = True

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    logger.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    ring_handler = _RingHandler()
    ring_handler.setFormatter(fmt)
    logger.addHandler(ring_handler)

    logger.info("Logging started, writing to %s", LOG_FILE)
    return logger


def get_logger(name: str = "hatate") -> logging.Logger:
    """Returns a child logger, e.g. get_logger('hatate.hydrus')."""
    if name == "hatate":
        return logging.getLogger("hatate")
    if not name.startswith("hatate."):
        name = f"hatate.{name}"
    return logging.getLogger(name)


def get_recent_logs() -> str:
    return "\n".join(_ring)


def get_log_file_path() -> str:
    return str(LOG_FILE)
