"""
tests/test_middleware.py
Unit tests for backend/api/middleware.py — file validation logic.
"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real")

import pytest
from unittest.mock import MagicMock, patch

from backend.api.middleware import validate_upload


def _make_upload_file(filename: str = "bill.pdf") -> MagicMock:
    """Return a minimal UploadFile mock with the given filename."""
    mock = MagicMock()
    mock.filename = filename
    return mock


class TestValidateUpload:
    """Tests for validate_upload() in middleware.py."""

    # ---- Size checks ----

    def test_accepts_file_within_size_limit(self):
        """A 1-byte file under the 10 MB limit should pass."""
        small_bytes = b"fake pdf content"
        upload = _make_upload_file("bill.pdf")
        with patch("backend.api.middleware.magic.from_buffer", return_value="application/pdf"):
            result = validate_upload(upload, small_bytes)
        assert result is None

    def test_rejects_file_over_size_limit(self):
        """A file over MAX_UPLOAD_SIZE_MB (default 10 MB) should return an error string."""
        # Create 11 MB of bytes
        oversized_bytes = b"x" * (11 * 1024 * 1024)
        upload = _make_upload_file("huge.pdf")
        with patch("backend.api.middleware.magic.from_buffer", return_value="application/pdf"):
            result = validate_upload(upload, oversized_bytes)
        assert result is not None
        assert "exceeds the" in result

    def test_size_error_message_includes_filename(self):
        oversized_bytes = b"x" * (11 * 1024 * 1024)
        upload = _make_upload_file("myfile.pdf")
        with patch("backend.api.middleware.magic.from_buffer", return_value="application/pdf"):
            result = validate_upload(upload, oversized_bytes)
        assert "myfile.pdf" in result

    # ---- MIME type checks ----

    def test_accepts_pdf(self):
        upload = _make_upload_file("bill.pdf")
        with patch("backend.api.middleware.magic.from_buffer", return_value="application/pdf"):
            result = validate_upload(upload, b"fake")
        assert result is None

    def test_accepts_jpeg(self):
        upload = _make_upload_file("bill.jpg")
        with patch("backend.api.middleware.magic.from_buffer", return_value="image/jpeg"):
            result = validate_upload(upload, b"fake")
        assert result is None

    def test_accepts_png(self):
        upload = _make_upload_file("bill.png")
        with patch("backend.api.middleware.magic.from_buffer", return_value="image/png"):
            result = validate_upload(upload, b"fake")
        assert result is None

    def test_rejects_html(self):
        upload = _make_upload_file("evil.html")
        with patch("backend.api.middleware.magic.from_buffer", return_value="text/html"):
            result = validate_upload(upload, b"<html>evil</html>")
        assert result is not None
        assert "text/html" in result

    def test_rejects_zip(self):
        upload = _make_upload_file("archive.zip")
        with patch("backend.api.middleware.magic.from_buffer", return_value="application/zip"):
            result = validate_upload(upload, b"PK fake")
        assert result is not None

    def test_rejects_executable(self):
        upload = _make_upload_file("malware.exe")
        with patch("backend.api.middleware.magic.from_buffer", return_value="application/x-executable"):
            result = validate_upload(upload, b"MZ fake")
        assert result is not None

    def test_type_error_message_includes_filename(self):
        upload = _make_upload_file("evil.html")
        with patch("backend.api.middleware.magic.from_buffer", return_value="text/html"):
            result = validate_upload(upload, b"fake")
        assert "evil.html" in result

    def test_type_error_mentions_allowed_types(self):
        upload = _make_upload_file("bad.gif")
        with patch("backend.api.middleware.magic.from_buffer", return_value="image/gif"):
            result = validate_upload(upload, b"GIF89a")
        # Should mention at least one of the allowed types
        assert any(t in result for t in ["PDF", "JPEG", "PNG"])

    # ---- Priority: size check before MIME ----

    def test_size_error_detected_before_type_check(self):
        """If both size and type fail, size error is returned (size is checked first)."""
        oversized_bytes = b"x" * (11 * 1024 * 1024)
        upload = _make_upload_file("evil.html")
        # Magic is called after size — if size fails, magic should still be called
        # but the size error string is returned. Either way: result is non-None.
        with patch("backend.api.middleware.magic.from_buffer", return_value="text/html"):
            result = validate_upload(upload, oversized_bytes)
        assert result is not None
        assert "exceeds the" in result

    # ---- Exactly at the limit ----

    def test_file_exactly_at_limit_is_accepted(self):
        """A file exactly equal to MAX_UPLOAD_SIZE_MB should NOT be rejected."""
        exact_bytes = b"x" * (10 * 1024 * 1024)
        upload = _make_upload_file("exact.pdf")
        with patch("backend.api.middleware.magic.from_buffer", return_value="application/pdf"):
            result = validate_upload(upload, exact_bytes)
        assert result is None
