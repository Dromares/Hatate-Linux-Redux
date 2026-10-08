"""Tests for workers/search_worker.py auto-import staging directory cleanup.

These tests verify the staging directory is created in a persistent cache
location and cleaned up properly on exit, signals, and startup.
This is a regression test for BA-05.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from . import _path  # noqa: F401

# Test the paths module directly (no PyQt6 dependency)
from core.paths import AUTO_IMPORT_STAGING_DIR, CACHE_DIR


class TestSearchWorkerStagingPaths(unittest.TestCase):
    """Tests for the auto-import staging directory paths."""

    def test_cache_dir_defined(self):
        """CACHE_DIR should be defined and point to XDG_CACHE_HOME."""
        self.assertIsNotNone(CACHE_DIR)
        self.assertTrue(str(CACHE_DIR).endswith("hatate-linux"))
        # Should be under .cache, not .config
        self.assertIn(".cache", str(CACHE_DIR))

    def test_staging_dir_under_cache(self):
        """AUTO_IMPORT_STAGING_DIR should be under CACHE_DIR."""
        self.assertTrue(str(AUTO_IMPORT_STAGING_DIR).startswith(str(CACHE_DIR)))
        self.assertTrue(str(AUTO_IMPORT_STAGING_DIR).endswith("auto_import_staging"))

    def test_staging_dir_not_in_tmp(self):
        """Staging directory should be under CACHE_DIR, not created via tempfile.gettempdir()/bare mkdtemp()."""
        # Intent: AUTO_IMPORT_STAGING_DIR is explicitly derived from CACHE_DIR (XDG_CACHE_HOME),
        # not from tempfile.gettempdir() or a bare mkdtemp() call. The test verifies the
        # structural relationship, not the absolute path (which depends on host XDG_CACHE_HOME).
        self.assertTrue(
            str(AUTO_IMPORT_STAGING_DIR).startswith(str(CACHE_DIR)),
            "AUTO_IMPORT_STAGING_DIR should be a subdirectory of CACHE_DIR"
        )
        self.assertTrue(
            str(AUTO_IMPORT_STAGING_DIR).endswith("auto_import_staging"),
            "AUTO_IMPORT_STAGING_DIR should end with 'auto_import_staging'"
        )


class TestSearchWorkerStagingCleanup(unittest.TestCase):
    """Tests for the auto-import staging directory cleanup logic.
    
    These test the cleanup functions by importing them with PyQt6 mocked.
    """

    def setUp(self):
        # Mock PyQt6 before importing the worker module
        self.qt_patcher = patch.dict('sys.modules', {
            'PyQt6': MagicMock(),
            'PyQt6.QtCore': MagicMock(),
        })
        self.qt_patcher.start()
        
        # Now import the worker module
        import workers.search_worker as sw_module
        import core.paths as paths_module
        self.sw_module = sw_module
        self.paths_module = paths_module
        
        # Use a temporary directory for testing
        self.test_cache_dir = Path(tempfile.mkdtemp(prefix="hatate-test-cache-"))
        self.original_staging_dir = sw_module.AUTO_IMPORT_STAGING_DIR
        self.original_paths_staging = paths_module.AUTO_IMPORT_STAGING_DIR
        self.original_paths_cache = paths_module.CACHE_DIR
        
        # Monkey-patch the cache dir for testing
        test_staging = self.test_cache_dir / "auto_import_staging"
        test_cache = self.test_cache_dir
        sw_module.AUTO_IMPORT_STAGING_DIR = test_staging
        paths_module.AUTO_IMPORT_STAGING_DIR = test_staging
        paths_module.CACHE_DIR = test_cache
        
        # Reset module-level state
        sw_module._current_staging_dir = None
        sw_module._cleanup_registered = False

    def tearDown(self):
        self.qt_patcher.stop()
        # Restore original
        self.sw_module.AUTO_IMPORT_STAGING_DIR = self.original_staging_dir
        self.paths_module.AUTO_IMPORT_STAGING_DIR = self.original_paths_staging
        self.paths_module.CACHE_DIR = self.original_paths_cache
        self.sw_module._current_staging_dir = None
        self.sw_module._cleanup_registered = False
        shutil.rmtree(self.test_cache_dir, ignore_errors=True)

    def test_staging_dir_created_in_cache_not_tmp(self):
        """BA-05: Staging directory should be created in XDG_CACHE_HOME, not /tmp."""
        # Create a staging dir using the same logic as _run()
        self.sw_module.AUTO_IMPORT_STAGING_DIR.mkdir(parents=True, exist_ok=True)
        import tempfile
        staging_dir = tempfile.mkdtemp(prefix="hatate-linux-auto-", dir=self.sw_module.AUTO_IMPORT_STAGING_DIR)
        
        # Verify it's in the cache directory
        self.assertTrue(staging_dir.startswith(str(self.test_cache_dir)))
        # The parent should be the staging dir, not /tmp directly
        self.assertEqual(Path(staging_dir).parent, self.sw_module.AUTO_IMPORT_STAGING_DIR)
        
        # Clean up
        shutil.rmtree(staging_dir, ignore_errors=True)

    def test_cleanup_stale_dirs_on_startup(self):
        """BA-05: Stale staging directories from previous runs should be cleaned up on startup."""
        # Create a fake stale directory
        stale_dir = self.sw_module.AUTO_IMPORT_STAGING_DIR / "hatate-linux-auto-stale123"
        stale_dir.mkdir(parents=True)
        (stale_dir / "dummy_file").write_text("test")
        
        # Verify it exists before cleanup
        self.assertTrue(stale_dir.exists())
        
        # Run cleanup
        self.sw_module._cleanup_stale_staging_dirs()
        
        # Verify it's gone
        self.assertFalse(stale_dir.exists())

    def test_cleanup_ignores_non_hatate_dirs(self):
        """Cleanup should only remove hatate-linux-auto- directories."""
        other_dir = self.sw_module.AUTO_IMPORT_STAGING_DIR / "other-app-dir"
        other_dir.mkdir(parents=True)
        (other_dir / "file").write_text("test")
        
        self.sw_module._cleanup_stale_staging_dirs()
        
        # Other directory should remain
        self.assertTrue(other_dir.exists())

    def test_register_cleanup_handlers_idempotent(self):
        """register_cleanup_handlers should be safe to call multiple times."""
        self.sw_module.register_cleanup_handlers()
        self.sw_module.register_cleanup_handlers()  # Should not raise or double-register
        # If we reach here without error, the test passes

    def test_sigint_handler_exits_via_systemexit(self):
        """DAN-3 launch smoke found SIGINT left the app running until SIGKILL.

        The old handler restored signal.default_int_handler and re-raised
        SIGINT, which surfaces as a KeyboardInterrupt inside whatever Qt slot
        happened to be dispatching when the signal was checked (in practice,
        main.py's periodic wakeup timer). PyQt forwards that to
        sys.excepthook and keeps the event loop running instead of letting it
        propagate, so the process never exits. SystemExit does propagate
        through that same path (SIGTERM's handler already relied on this),
        so the fix is for SIGINT to exit the same way, with SIGINT's
        conventional 128+n code.
        """
        import signal
        self.sw_module.register_cleanup_handlers()
        handler = signal.getsignal(signal.SIGINT)
        with self.assertRaises(SystemExit) as ctx:
            handler(signal.SIGINT, None)
        self.assertEqual(ctx.exception.code, 128 + signal.SIGINT)

    def test_current_staging_dir_tracked(self):
        """Module-level _current_staging_dir should track the active staging dir."""
        # Initially None
        self.assertIsNone(self.sw_module._current_staging_dir)
        
        # Simulate setting it
        test_dir = str(self.test_cache_dir / "test_staging")
        self.sw_module._current_staging_dir = test_dir
        self.assertEqual(self.sw_module._current_staging_dir, test_dir)

    def test_write_and_read_pid_file(self):
        """PID file should be written and readable."""
        test_dir = self.test_cache_dir / "test_staging_pid"
        test_dir.mkdir(parents=True)
        
        # Write PID file
        self.sw_module._write_pid_file(str(test_dir))
        
        # Read it back
        pid = self.sw_module._read_pid_file(str(test_dir))
        self.assertEqual(pid, os.getpid())
        
        # Clean up
        shutil.rmtree(test_dir, ignore_errors=True)

    def test_cleanup_skips_live_pid(self):
        """BA-05: Cleanup should skip directories with a live PID."""
        live_dir = self.sw_module.AUTO_IMPORT_STAGING_DIR / "hatate-linux-auto-live123"
        live_dir.mkdir(parents=True)
        (live_dir / "dummy_file").write_text("test")
        
        # Write current PID to mark as live
        self.sw_module._write_pid_file(str(live_dir))
        
        # Run cleanup
        self.sw_module._cleanup_stale_staging_dirs()
        
        # Live directory should remain
        self.assertTrue(live_dir.exists(), "Live staging directory should not be cleaned up")

    def test_cleanup_removes_dead_pid(self):
        """BA-05: Cleanup should remove directories with a dead PID."""
        dead_dir = self.sw_module.AUTO_IMPORT_STAGING_DIR / "hatate-linux-auto-dead456"
        dead_dir.mkdir(parents=True)
        (dead_dir / "dummy_file").write_text("test")
        
        # Write a fake PID that doesn't exist (very high number unlikely to be in use)
        fake_pid = 999999
        pid_file = dead_dir / self.sw_module._PID_FILE_NAME
        pid_file.write_text(str(fake_pid))
        
        # Run cleanup
        self.sw_module._cleanup_stale_staging_dirs()
        
        # Dead directory should be removed
        self.assertFalse(dead_dir.exists(), "Dead staging directory should be cleaned up")

    def test_cleanup_removes_dir_without_pid(self):
        """BA-05: Cleanup should remove directories without a PID file (backward compat)."""
        no_pid_dir = self.sw_module.AUTO_IMPORT_STAGING_DIR / "hatate-linux-auto-nopid789"
        no_pid_dir.mkdir(parents=True)
        (no_pid_dir / "dummy_file").write_text("test")
        
        # No PID file written
        
        # Run cleanup
        self.sw_module._cleanup_stale_staging_dirs()
        
        # Directory without PID should be removed (treated as stale)
        self.assertFalse(no_pid_dir.exists(), "Directory without PID file should be cleaned up")


if __name__ == "__main__":
    unittest.main()