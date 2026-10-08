"""McpSettings: defaults, round-tripping through save()/load(), and an
old config (saved before DAN-707 existed) loading with the server off -
the same migration-free guarantee engine_delays and the enable_* search
engine flags already rely on (see core/config.py's CURRENT_SCHEMA_VERSION
comment).
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import _path  # noqa: F401
from core import config
from core.config import McpSettings, Settings


class TestDefaults(unittest.TestCase):
    def test_disabled_by_default(self):
        self.assertFalse(McpSettings().enabled)

    def test_blank_token_by_default(self):
        self.assertEqual(McpSettings().token, "")

    def test_every_write_tier_is_off_by_default_except_hydrus_writes(self):
        settings = McpSettings()
        self.assertFalse(settings.allow_research)
        self.assertFalse(settings.allow_destructive)

    def test_hydrus_writes_default_on_is_a_board_decision(self):
        """DAN-702 (2026-10-07): the board asked for this explicitly -
        "im gonna need hydrus writes turned on" - after accepting a plan
        that originally had it off. This default is now the board's call,
        not the cautious engineering default, so it gets its own test: a
        later "surely this should be off by default" refactor must fail
        this test instead of silently reverting the board. allow_research
        and allow_destructive are untouched by that decision and stay off -
        see test_every_write_tier_is_off_by_default_except_hydrus_writes."""
        self.assertTrue(McpSettings().allow_hydrus_writes)

    def test_dry_run_is_off_by_default(self):
        self.assertFalse(McpSettings().dry_run)

    def test_settings_has_an_mcp_field(self):
        # Not assertIsInstance(..., McpSettings): tests/test_session.py and
        # others importlib.reload(core.config) elsewhere in the same
        # process, which rebinds core.config.McpSettings to a new class
        # object - a class-identity check here is a coin flip on test
        # order, not a property of the code. The attribute is what matters.
        self.assertEqual(type(Settings().mcp).__name__, "McpSettings")

    def test_host_is_not_a_field(self):
        """127.0.0.1 is a module constant in core/mcp_server.py, never a
        setting - see McpSettings' own docstring for why."""
        self.assertFalse(hasattr(McpSettings(), "host"))


class TestRoundTrip(unittest.TestCase):
    def _load_from(self, stored: dict) -> Settings:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(stored), encoding="utf-8")
            with patch.object(config, "CONFIG_FILE", path), \
                 patch.object(config, "CONFIG_DIR", Path(tmp)), \
                 patch.object(config, "CONFIG_BACKUP_FILE", Path(tmp) / "config.json.bak"):
                return Settings.load()

    def test_a_saved_mcp_block_round_trips(self):
        stored = {
            "schema_version": config.CURRENT_SCHEMA_VERSION,
            "mcp": {
                "enabled": True, "port": 9999, "token": "abc123",
                "allow_research": True, "allow_hydrus_writes": True,
                "allow_destructive": True, "dry_run": True,
                "audit_log_max_bytes": 1234,
            },
        }
        loaded = self._load_from(stored)
        self.assertTrue(loaded.mcp.enabled)
        self.assertEqual(loaded.mcp.port, 9999)
        self.assertEqual(loaded.mcp.token, "abc123")
        self.assertTrue(loaded.mcp.allow_research)
        self.assertTrue(loaded.mcp.allow_hydrus_writes)
        self.assertTrue(loaded.mcp.allow_destructive)
        self.assertTrue(loaded.mcp.dry_run)
        self.assertEqual(loaded.mcp.audit_log_max_bytes, 1234)

    def test_a_partial_mcp_block_fills_in_the_rest_from_defaults(self):
        """Mirrors the saucenao/hydrus/match_conditions merge pattern:
        the stored dict only overrides what it actually names."""
        stored = {"schema_version": config.CURRENT_SCHEMA_VERSION,
                  "mcp": {"enabled": True, "port": 9000}}
        loaded = self._load_from(stored)
        self.assertTrue(loaded.mcp.enabled)
        self.assertEqual(loaded.mcp.port, 9000)
        self.assertEqual(loaded.mcp.token, "")  # untouched default
        self.assertTrue(loaded.mcp.allow_hydrus_writes)  # untouched default (on by board decision)

    def test_a_config_from_before_this_existed_loads_disabled(self):
        """An old config has no "mcp" key at all - the whole point of
        NOT bumping the schema version for this field (see
        core/config.py's comment): absent means McpSettings()'s
        defaults, which is the state every pre-DAN-707 config was
        already effectively in."""
        stored = {"schema_version": 13, "enabled_sites": ["Danbooru"]}
        loaded = self._load_from(stored)
        # Not assertIsInstance(..., McpSettings) - see the comment on
        # test_settings_has_an_mcp_field above for why that is unsafe here.
        self.assertEqual(type(loaded.mcp).__name__, "McpSettings")
        self.assertFalse(loaded.mcp.enabled)
        self.assertEqual(loaded.schema_version, config.CURRENT_SCHEMA_VERSION)

    def test_save_then_load_preserves_mcp_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            with patch.object(config, "CONFIG_FILE", path), \
                 patch.object(config, "CONFIG_DIR", Path(tmp)), \
                 patch.object(config, "CONFIG_BACKUP_FILE", Path(tmp) / "config.json.bak"):
                original = Settings()
                original.mcp.enabled = True
                original.mcp.port = 8800
                original.mcp.token = "round-trip-token"
                original.mcp.allow_hydrus_writes = True
                original.save()
                reloaded = Settings.load()
        self.assertTrue(reloaded.mcp.enabled)
        self.assertEqual(reloaded.mcp.port, 8800)
        self.assertEqual(reloaded.mcp.token, "round-trip-token")
        self.assertTrue(reloaded.mcp.allow_hydrus_writes)


if __name__ == "__main__":
    unittest.main()
