"""Filesystem paths shared across core modules. Kept free of any other
core.* imports so that config.py and applog.py can both depend on this
without creating an import cycle between each other.
"""
from __future__ import annotations

import os
from pathlib import Path

# Where the app itself lives, as opposed to where its config does: paths.py
# sits in core/, so the project root is one level up. Resolved rather than
# assumed relative to the working directory, because the launcher can be
# started from anywhere (run.sh, the applications menu, or a shell in some
# other folder).
PROJECT_DIR = Path(__file__).resolve().parent.parent
RESOURCES_DIR = PROJECT_DIR / "resources"

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "hatate-linux"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_LOG_FILE = CONFIG_DIR / "matched_urls.log"
SEARCH_CACHE_DIR = CONFIG_DIR / "search_cache"
SESSION_FILE = CONFIG_DIR / "session.json"  # legacy single-document session; still read, and
                                            # migrated to SESSION_DB on first load
SESSION_DB = CONFIG_DIR / "session.db"      # the working list, so a restart skips re-hashing.
                                            # SQLite so an autosave rewrites only what changed
                                            # instead of re-encoding the whole list every time

# Cache directory for temporary files that should persist across restarts
# (e.g. auto-import staging files). This avoids /tmp which is often RAM-backed
# and cleared on reboot, leaking memory from killed processes.
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "hatate-linux"
AUTO_IMPORT_STAGING_DIR = CACHE_DIR / "auto_import_staging"
