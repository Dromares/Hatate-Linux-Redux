"""Tests for core/hydrus_import.py download validation (BA-02).

These tests verify that download_and_send validates downloaded files
with magic bytes, size checks, and PIL decoding, even when no remote
size is known.
"""
import hashlib
import io
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from . import _path  # noqa: F401

# Generate valid test images using PIL
from PIL import Image

# Create a minimal valid JPEG (large enough to pass size check)
_jpeg_buffer = io.BytesIO()
Image.new("RGB", (100, 100), color="red").save(_jpeg_buffer, format="JPEG")
VALID_JPEG = _jpeg_buffer.getvalue()

# Create a minimal valid PNG (large enough to pass size check)
_png_buffer = io.BytesIO()
Image.new("RGB", (100, 100), color="blue").save(_png_buffer, format="PNG")
VALID_PNG = _png_buffer.getvalue()

# Minimal valid WebM (EBML header + padding to pass 100-byte check)
# EBML header: \x1aE\xdf\xa3 followed by minimal valid structure
VALID_WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 100

# Minimal valid MP4 (ftyp box with isom brand + padding)
# Box size (4 bytes) + "ftyp" + major_brand (4 bytes) + minor_version (4) + compatible_brands
# Size = 0x1c = 28 bytes total for this box
# ftyp at offset 4, brand "isom" at offset 8
VALID_MP4 = b"\x00\x00\x00\x1c" + b"ftypisom" + b"\x00\x00\x00\x00" + b"isom" + b"\x00" * 80

# Valid GIF for sniffing test
_gif_buffer = io.BytesIO()
Image.new("RGB", (100, 100), color="green").save(_gif_buffer, format="GIF")
VALID_GIF = _gif_buffer.getvalue()


class TestDownloadValidation(unittest.TestCase):
    """Tests for the download validation logic in download_and_send."""

    def setUp(self):
        # Mock PyQt6 before importing hydrus_import (which may import search_engine)
        self.qt_patcher = patch.dict('sys.modules', {
            'PyQt6': MagicMock(),
            'PyQt6.QtCore': MagicMock(),
        })
        self.qt_patcher.start()
        
        # Create a temp dir for staging
        self.test_cache_dir = tempfile.mkdtemp(prefix="hatate-test-cache-")
        os.makedirs(os.path.join(self.test_cache_dir, "auto_import_staging"), exist_ok=True)
        
        # Import and patch paths
        import core.paths as paths_module
        import core.hydrus_import as hi_module
        self.paths_module = paths_module
        self.hi_module = hi_module
        self.original_staging = paths_module.AUTO_IMPORT_STAGING_DIR
        self.original_cache = paths_module.CACHE_DIR
        paths_module.CACHE_DIR = self.test_cache_dir
        paths_module.AUTO_IMPORT_STAGING_DIR = os.path.join(self.test_cache_dir, "auto_import_staging")

    def tearDown(self):
        self.qt_patcher.stop()
        self.paths_module.AUTO_IMPORT_STAGING_DIR = self.original_staging
        self.paths_module.CACHE_DIR = self.original_cache
        import shutil
        shutil.rmtree(self.test_cache_dir, ignore_errors=True)

    def _make_candidate(self, direct_url, remote_format=None, remote_size_bytes=None):
        """Create a mock candidate with the given attributes."""
        from core.models import MatchCandidate
        candidate = MatchCandidate(
            url="http://example.com/post/123",
            source_name="test",
            similarity=95.0,
            direct_file_url=direct_url,
        )
        candidate.remote_format = remote_format
        candidate.remote_size_bytes = remote_size_bytes
        return candidate

    def _make_entry(self, path="/fake/test.jpg", candidate=None):
        """Create a mock ImageEntry."""
        from core.models import ImageEntry
        entry = ImageEntry(path=path)
        if candidate:
            entry.candidates = [candidate]
            entry.select_candidate(0)
        entry.tags = []
        entry.matched_url = "http://example.com/post/123"
        return entry

    def _make_client(self):
        """Create a mock HydrusClient whose import_file reports the real hash
        of the file it was handed, matching real (non-transcoding) Hydrus
        behavior for a direct file upload (DAN-17)."""
        from core.hydrus_tag_lookup import hash_file
        client = MagicMock()
        client.import_file.side_effect = lambda path: {"hash": hash_file(path), "status": "success"}
        return client

    @patch("core.remote.download_bytes")
    def test_rejects_empty_file(self, mock_download):
        """BA-02: Zero-length downloads should be rejected."""
        mock_download.return_value = b""
        
        candidate = self._make_candidate("http://example.com/image.jpg")
        entry = self._make_entry(candidate=candidate)
        client = self._make_client()
        
        result = self.hi_module.download_and_send(entry, client, 30.0, self.paths_module.AUTO_IMPORT_STAGING_DIR)
        
        self.assertFalse(result.success)
        self.assertIn("could not download", result.error.lower())
        mock_download.assert_called_once()

    @patch("core.remote.download_bytes")
    def test_rejects_implausibly_small_file(self, mock_download):
        """BA-02: Implausibly small downloads (< 100 bytes) should be rejected."""
        mock_download.return_value = b"x" * 50  # 50 bytes
        
        candidate = self._make_candidate("http://example.com/image.jpg")
        entry = self._make_entry(candidate=candidate)
        client = self._make_client()
        
        result = self.hi_module.download_and_send(entry, client, 30.0, self.paths_module.AUTO_IMPORT_STAGING_DIR)
        
        self.assertFalse(result.success)
        self.assertIn("implausibly small", result.error.lower())
        mock_download.assert_called_once()

    @patch("core.remote.download_bytes")
    def test_rejects_wrong_magic_bytes_jpeg(self, mock_download):
        """BA-02: Download with wrong magic bytes for JPEG should be rejected."""
        # PNG magic bytes but expecting JPEG
        mock_download.return_value = b"\x89PNG\r\n\x1a\n" + b"x" * 100
        
        candidate = self._make_candidate("http://example.com/image.jpg", remote_format="JPEG")
        entry = self._make_entry(candidate=candidate)
        client = self._make_client()
        
        result = self.hi_module.download_and_send(entry, client, 30.0, self.paths_module.AUTO_IMPORT_STAGING_DIR)
        
        self.assertFalse(result.success)
        self.assertIn("magic bytes", result.error.lower())

    @patch("core.remote.download_bytes")
    def test_accepts_valid_jpeg(self, mock_download):
        """Valid JPEG with correct magic bytes should be accepted."""
        mock_download.return_value = VALID_JPEG
        
        candidate = self._make_candidate("http://example.com/image.jpg", remote_format="JPEG")
        entry = self._make_entry(candidate=candidate)
        client = self._make_client()
        
        result = self.hi_module.download_and_send(entry, client, 30.0, self.paths_module.AUTO_IMPORT_STAGING_DIR)
        
        self.assertTrue(result.success)
        client.import_file.assert_called_once()

    @patch("core.remote.download_bytes")
    def test_accepts_valid_png(self, mock_download):
        """Valid PNG with correct magic bytes should be accepted."""
        mock_download.return_value = VALID_PNG
        
        candidate = self._make_candidate("http://example.com/image.png", remote_format="PNG")
        entry = self._make_entry(path="/fake/test.png", candidate=candidate)
        client = self._make_client()
        
        result = self.hi_module.download_and_send(entry, client, 30.0, self.paths_module.AUTO_IMPORT_STAGING_DIR)
        
        self.assertTrue(result.success)
        client.import_file.assert_called_once()

    @patch("core.remote.download_bytes")
    def test_infers_format_from_extension_when_no_remote_format(self, mock_download):
        """When no remote_format, format should be inferred from file extension."""
        # Valid JPEG data but no remote_format - should infer from .jpg extension
        mock_download.return_value = VALID_JPEG
        
        candidate = self._make_candidate("http://example.com/image.jpg", remote_format=None)
        entry = self._make_entry(candidate=candidate)
        client = self._make_client()
        
        result = self.hi_module.download_and_send(entry, client, 30.0, self.paths_module.AUTO_IMPORT_STAGING_DIR)
        
        self.assertTrue(result.success)
        client.import_file.assert_called_once()

    @patch("core.remote.download_bytes")
    def test_rejects_html_page(self, mock_download):
        """HTML page masquerading as image should be rejected by magic bytes."""
        html_data = b"<!DOCTYPE html><html><body>Error</body></html>" + b"x" * 100
        mock_download.return_value = html_data
        
        candidate = self._make_candidate("http://example.com/image.jpg", remote_format="JPEG")
        entry = self._make_entry(candidate=candidate)
        client = self._make_client()
        
        result = self.hi_module.download_and_send(entry, client, 30.0, self.paths_module.AUTO_IMPORT_STAGING_DIR)
        
        self.assertFalse(result.success)
        # Could be caught by _looks_like_a_web_page or magic bytes
        self.assertTrue(
            "web page" in result.error.lower() or "magic bytes" in result.error.lower()
        )

    @patch("core.remote.download_bytes")
    def test_rejects_corrupted_image(self, mock_download):
        """Corrupted image data that PIL cannot decode should be rejected."""
        # Valid JPEG header but corrupted data
        jpeg_data = VALID_JPEG[:20] + b"corrupted" * 50
        mock_download.return_value = jpeg_data
        
        candidate = self._make_candidate("http://example.com/image.jpg", remote_format="JPEG")
        entry = self._make_entry(candidate=candidate)
        client = self._make_client()
        
        result = self.hi_module.download_and_send(entry, client, 30.0, self.paths_module.AUTO_IMPORT_STAGING_DIR)
        
        self.assertFalse(result.success)
        self.assertIn("decode", result.error.lower())

    @patch("core.remote.download_bytes")
    def test_size_check_still_works_when_known(self, mock_download):
        """Size mismatch should still be caught when remote_size_bytes is known."""
        # Correct JPEG data but wrong size
        mock_download.return_value = VALID_JPEG
        
        candidate = self._make_candidate(
            "http://example.com/image.jpg", 
            remote_format="JPEG", 
            remote_size_bytes=9999  # Wrong size
        )
        entry = self._make_entry(candidate=candidate)
        client = self._make_client()
        
        result = self.hi_module.download_and_send(entry, client, 30.0, self.paths_module.AUTO_IMPORT_STAGING_DIR)
        
        self.assertFalse(result.success)
        self.assertIn("expected", result.error.lower())

    # --- Regression tests for DAN-26 cycle 2 fix ---
    # These test the fix for: extension-less URL + no remote_format
    # should not default to JPEG and reject valid other formats

    @patch("core.remote.download_bytes")
    def test_extensionless_url_no_remote_format_valid_png_imports(self, mock_download):
        """Extension-less URL + remote_format=None + valid PNG bytes → imports successfully."""
        mock_download.return_value = VALID_PNG
        
        # No extension in URL, no remote_format
        candidate = self._make_candidate("http://example.com/image?id=123", remote_format=None)
        entry = self._make_entry(candidate=candidate)
        client = self._make_client()
        
        result = self.hi_module.download_and_send(entry, client, 30.0, self.paths_module.AUTO_IMPORT_STAGING_DIR)
        
        self.assertTrue(result.success, f"Expected success but got: {result.error}")
        client.import_file.assert_called_once()

    @patch("core.remote.download_bytes")
    def test_extensionless_url_no_remote_format_valid_webm_imports(self, mock_download):
        """Extension-less URL + remote_format=None + valid webm bytes → imports successfully."""
        mock_download.return_value = VALID_WEBM
        
        candidate = self._make_candidate("http://example.com/video?id=456", remote_format=None)
        entry = self._make_entry(candidate=candidate)
        client = self._make_client()
        
        result = self.hi_module.download_and_send(entry, client, 30.0, self.paths_module.AUTO_IMPORT_STAGING_DIR)
        
        self.assertTrue(result.success, f"Expected success but got: {result.error}")
        client.import_file.assert_called_once()

    @patch("core.remote.download_bytes")
    def test_extensionless_url_no_remote_format_valid_mp4_imports(self, mock_download):
        """Extension-less URL + remote_format=None + valid mp4 bytes → imports successfully."""
        mock_download.return_value = VALID_MP4
        
        candidate = self._make_candidate("http://example.com/video?id=789", remote_format=None)
        entry = self._make_entry(candidate=candidate)
        client = self._make_client()
        
        result = self.hi_module.download_and_send(entry, client, 30.0, self.paths_module.AUTO_IMPORT_STAGING_DIR)
        
        self.assertTrue(result.success, f"Expected success but got: {result.error}")
        client.import_file.assert_called_once()

    @patch("core.remote.download_bytes")
    def test_extensionless_url_no_remote_format_html_rejected(self, mock_download):
        """Extension-less URL + remote_format=None + HTML/garbage payload → still rejected."""
        html_data = b"<!DOCTYPE html><html><body>Error</body></html>" + b"x" * 100
        mock_download.return_value = html_data
        
        candidate = self._make_candidate("http://example.com/image?id=999", remote_format=None)
        entry = self._make_entry(candidate=candidate)
        client = self._make_client()
        
        result = self.hi_module.download_and_send(entry, client, 30.0, self.paths_module.AUTO_IMPORT_STAGING_DIR)
        
        self.assertFalse(result.success)
        # Caught by _looks_like_a_web_page check (runs before magic bytes)
        self.assertIn("web page", result.error.lower())

    @patch("core.remote.download_bytes")
    def test_claimed_format_png_with_jpeg_bytes_rejected(self, mock_download):
        """remote_format="PNG" + JPEG bytes → still rejected (real claim contradicted)."""
        mock_download.return_value = VALID_JPEG
        
        # Explicit claim of PNG, but data is JPEG
        candidate = self._make_candidate("http://example.com/image.png", remote_format="PNG")
        entry = self._make_entry(candidate=candidate)
        client = self._make_client()
        
        result = self.hi_module.download_and_send(entry, client, 30.0, self.paths_module.AUTO_IMPORT_STAGING_DIR)
        
        self.assertFalse(result.success)
        self.assertIn("magic bytes", result.error.lower())
        self.assertIn("png", result.error.lower())

    @patch("core.remote.download_bytes")
    def test_extensionless_url_no_remote_format_valid_gif_imports(self, mock_download):
        """Extension-less URL + remote_format=None + valid GIF bytes → imports successfully (sniffing)."""
        mock_download.return_value = VALID_GIF
        
        candidate = self._make_candidate("http://example.com/image?id=gif123", remote_format=None)
        entry = self._make_entry(candidate=candidate)
        client = self._make_client()
        
        result = self.hi_module.download_and_send(entry, client, 30.0, self.paths_module.AUTO_IMPORT_STAGING_DIR)
        
        self.assertTrue(result.success, f"Expected success but got: {result.error}")
        client.import_file.assert_called_once()

    @patch("core.remote.download_bytes")
    def test_rejects_hydrus_hash_mismatch(self, mock_download):
        """DAN-17: if Hydrus reports a hash that doesn't match the file we sent,
        reject the import instead of associating the URL/tags with the wrong hash."""
        mock_download.return_value = VALID_JPEG

        candidate = self._make_candidate("http://example.com/image.jpg", remote_format="JPEG")
        entry = self._make_entry(candidate=candidate)
        client = self._make_client()
        # Simulate Hydrus returning a hash that doesn't match the uploaded bytes.
        client.import_file.side_effect = None
        client.import_file.return_value = {"hash": "deadbeef" * 8, "status": "success"}

        result = self.hi_module.download_and_send(entry, client, 30.0, self.paths_module.AUTO_IMPORT_STAGING_DIR)

        self.assertFalse(result.success)
        self.assertIn("hash mismatch", result.error.lower())
        # Both hashes must be present so a false rejection is a one-line diagnosis.
        expected_hash = hashlib.sha256(VALID_JPEG).hexdigest()
        self.assertIn(expected_hash, result.error)
        self.assertIn("deadbeef" * 8, result.error)
        client.associate_url.assert_not_called()
        client.add_tags.assert_not_called()
        self.assertFalse(entry.hydrus_import_confirmed)

    @patch("core.remote.download_bytes")
    def test_accepts_matching_hydrus_hash(self, mock_download):
        """DAN-17 (other direction): when Hydrus returns the same hash as the file we
        sent, the import still succeeds and is confirmed - the reject path added for
        the mismatch case must not reject a legitimate match."""
        mock_download.return_value = VALID_JPEG

        candidate = self._make_candidate("http://example.com/image.jpg", remote_format="JPEG")
        entry = self._make_entry(candidate=candidate)
        client = self._make_client()  # _make_client reports the real sha256 of the file sent

        result = self.hi_module.download_and_send(entry, client, 30.0, self.paths_module.AUTO_IMPORT_STAGING_DIR)

        self.assertTrue(result.success, f"Expected success but got: {result.error}")
        self.assertTrue(entry.hydrus_import_confirmed)
        client.associate_url.assert_called_once()


if __name__ == "__main__":
    unittest.main()