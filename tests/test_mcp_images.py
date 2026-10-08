"""get_images/get_diff's core logic: the tools must return the real
picture at FULL resolution, not the downscaled preview (DAN-707) -
mirroring gui/compare_dialog.py's load_comparison() without the Qt
types, so this is testable with no display.
"""
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import _path  # noqa: F401
from core import mcp_images
from core.config import Settings
from core.models import MatchCandidate


def _png_bytes(size=(20, 20), color=(255, 0, 0)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


class TestLocalImageBytes(unittest.TestCase):
    def test_reads_the_real_file(self):
        data = _png_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "local.png"
            path.write_bytes(data)

            class FakeEntry:
                pass
            entry = FakeEntry()
            entry.path = str(path)
            self.assertEqual(mcp_images.local_image_bytes(entry), data)

    def test_a_missing_file_returns_none_rather_than_raising(self):
        class FakeEntry:
            pass
        entry = FakeEntry()
        entry.path = "/nonexistent/does-not-exist.png"
        self.assertIsNone(mcp_images.local_image_bytes(entry))


class TestFetchFullResolutionMatch(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()

    def test_prefers_the_direct_file_url_over_the_preview(self):
        candidate = MatchCandidate(
            url="https://example.com/post/1",
            direct_file_url="https://example.com/full.jpg",
            preview_url="https://example.com/sample.jpg",
        )
        with patch.object(mcp_images, "download_bytes", return_value=b"full-bytes") as fetch, \
             patch.object(mcp_images, "fetch_candidate_details") as details:
            result = mcp_images.fetch_full_resolution_match(candidate, self.settings)
        details.assert_not_called()  # already had a direct URL - nothing to look up
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[0], "https://example.com/full.jpg")
        self.assertTrue(result.is_full_resolution)
        self.assertIsNone(result.fallback_reason)
        self.assertEqual(result.data, b"full-bytes")

    def test_falls_back_to_the_preview_when_no_direct_url_exists(self):
        candidate = MatchCandidate(
            url="https://example.com/post/1", preview_url="https://example.com/sample.jpg",
        )
        with patch.object(mcp_images, "download_bytes", return_value=b"preview-bytes"), \
             patch.object(mcp_images, "fetch_candidate_details") as details:
            result = mcp_images.fetch_full_resolution_match(candidate, self.settings)
        details.assert_not_called()  # the preview_url branch skips the lookup, matching load_comparison
        self.assertFalse(result.is_full_resolution)
        self.assertIsNotNone(result.fallback_reason)

    def test_tries_to_fetch_details_when_neither_url_is_known_yet(self):
        candidate = MatchCandidate(url="https://example.com/post/1")

        def _populate(c, settings, local_path=None):
            c.direct_file_url = "https://example.com/full.jpg"

        with patch.object(mcp_images, "fetch_candidate_details", side_effect=_populate) as details, \
             patch.object(mcp_images, "download_bytes", return_value=b"full-bytes") as fetch:
            result = mcp_images.fetch_full_resolution_match(candidate, self.settings)
        details.assert_called_once()
        fetch.assert_called_once_with(
            "https://example.com/full.jpg", self.settings.hydrus.timeout,
            referer="https://example.com/post/1",
        )
        self.assertTrue(result.is_full_resolution)

    def test_no_url_at_all_is_reported_rather_than_attempted(self):
        candidate = MatchCandidate(url="https://example.com/post/1")
        with patch.object(mcp_images, "fetch_candidate_details"), \
             patch.object(mcp_images, "download_bytes") as fetch:
            result = mcp_images.fetch_full_resolution_match(candidate, self.settings)
        fetch.assert_not_called()
        self.assertIsNone(result.data)
        self.assertIsNone(result.url)
        self.assertIsNotNone(result.fallback_reason)

    def test_a_failed_download_is_reported_rather_than_crashing(self):
        candidate = MatchCandidate(
            url="https://example.com/post/1", direct_file_url="https://example.com/full.jpg",
        )
        with patch.object(mcp_images, "download_bytes", return_value=None):
            result = mcp_images.fetch_full_resolution_match(candidate, self.settings)
        self.assertIsNone(result.data)
        self.assertEqual(result.url, "https://example.com/full.jpg")
        self.assertIsNotNone(result.fallback_reason)


class TestComputeDiff(unittest.TestCase):
    def test_identical_images_report_no_difference(self):
        data = _png_bytes()
        result = mcp_images.compute_diff(data, data)
        self.assertIsNotNone(result.png_bytes)
        self.assertLess(result.changed_percent, 1.0)

    def test_missing_local_bytes_is_reported_rather_than_crashing(self):
        result = mcp_images.compute_diff(None, _png_bytes())
        self.assertIsNone(result.png_bytes)
        self.assertFalse(result.reliable)

    def test_undecodable_bytes_are_reported_rather_than_crashing(self):
        result = mcp_images.compute_diff(b"not an image", _png_bytes())
        self.assertIsNone(result.png_bytes)
        self.assertFalse(result.reliable)


if __name__ == "__main__":
    unittest.main()
