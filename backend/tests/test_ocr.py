"""
tests/test_ocr.py
Unit tests for backend/services/ocr.py — text extraction.
All external libs (pdfplumber, pytesseract, PIL) are mocked.
"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real")

import io
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from backend.services.ocr import extract_text, OCRError


class TestExtractTextRouting:
    """Test that extract_text() routes to the correct extractor."""

    def test_pdf_routes_to_pdfplumber(self):
        with patch("backend.services.ocr._extract_from_pdf", return_value="pdf text") as mock_pdf:
            result = extract_text(b"fake pdf bytes", "application/pdf")
        mock_pdf.assert_called_once_with(b"fake pdf bytes")
        assert result == "pdf text"

    def test_jpeg_routes_to_image_extractor(self):
        with patch("backend.services.ocr._extract_from_image", return_value="image text") as mock_img:
            result = extract_text(b"fake jpeg bytes", "image/jpeg")
        mock_img.assert_called_once_with(b"fake jpeg bytes")
        assert result == "image text"

    def test_png_routes_to_image_extractor(self):
        with patch("backend.services.ocr._extract_from_image", return_value="image text") as mock_img:
            result = extract_text(b"fake png bytes", "image/png")
        mock_img.assert_called_once_with(b"fake png bytes")
        assert result == "image text"

    def test_unsupported_mime_raises_value_error(self):
        with pytest.raises(ValueError, match="Unsupported MIME type"):
            extract_text(b"fake bytes", "text/plain")

    def test_unsupported_mime_does_not_call_extractors(self):
        with patch("backend.services.ocr._extract_from_pdf") as mock_pdf:
            with patch("backend.services.ocr._extract_from_image") as mock_img:
                with pytest.raises(ValueError):
                    extract_text(b"fake", "application/zip")
        mock_pdf.assert_not_called()
        mock_img.assert_not_called()


class TestExtractFromPdf:
    """Tests for _extract_from_pdf() via the public extract_text() interface."""

    def _make_pdf_page(self, text: str) -> MagicMock:
        page = MagicMock()
        page.extract_text.return_value = text
        return page

    def test_extracts_text_from_single_page_pdf(self):
        page = self._make_pdf_page("Service 99213 $150.00")
        mock_pdf = MagicMock()
        mock_pdf.pages = [page]
        mock_pdf.__enter__ = lambda s: mock_pdf
        mock_pdf.__exit__ = MagicMock(return_value=False)

        with patch("pdfplumber.open", return_value=mock_pdf):
            result = extract_text(b"fake", "application/pdf")

        assert "Service 99213" in result
        assert "[Page 1]" in result

    def test_extracts_text_from_multi_page_pdf(self):
        pages = [
            self._make_pdf_page("Page one content"),
            self._make_pdf_page("Page two content"),
        ]
        mock_pdf = MagicMock()
        mock_pdf.pages = pages
        mock_pdf.__enter__ = lambda s: mock_pdf
        mock_pdf.__exit__ = MagicMock(return_value=False)

        with patch("pdfplumber.open", return_value=mock_pdf):
            result = extract_text(b"fake", "application/pdf")

        assert "[Page 1]" in result
        assert "[Page 2]" in result
        assert "Page one content" in result
        assert "Page two content" in result

    def test_raises_ocr_error_for_empty_pdf(self):
        """A PDF where all pages return empty text → OCRError (scanned PDF)."""
        page = self._make_pdf_page("")  # empty text = scanned
        mock_pdf = MagicMock()
        mock_pdf.pages = [page]
        mock_pdf.__enter__ = lambda s: mock_pdf
        mock_pdf.__exit__ = MagicMock(return_value=False)

        with patch("pdfplumber.open", return_value=mock_pdf):
            with pytest.raises(OCRError, match="scanned PDF"):
                extract_text(b"fake", "application/pdf")

    def test_raises_ocr_error_for_zero_page_pdf(self):
        """A PDF with no pages → OCRError."""
        mock_pdf = MagicMock()
        mock_pdf.pages = []
        mock_pdf.__enter__ = lambda s: mock_pdf
        mock_pdf.__exit__ = MagicMock(return_value=False)

        with patch("pdfplumber.open", return_value=mock_pdf):
            with pytest.raises(OCRError, match="no pages"):
                extract_text(b"fake", "application/pdf")

    def test_raises_ocr_error_when_pdfplumber_fails(self):
        """An exception from pdfplumber should be wrapped in OCRError."""
        with patch("pdfplumber.open", side_effect=Exception("corrupt PDF")):
            with pytest.raises(OCRError, match="Failed to parse PDF"):
                extract_text(b"fake", "application/pdf")

    def test_skips_bad_page_and_continues(self):
        """A page that raises during extraction should be skipped, not abort all pages."""
        bad_page = MagicMock()
        bad_page.extract_text.side_effect = Exception("bad xref")
        good_page = self._make_pdf_page("Valid page content")

        mock_pdf = MagicMock()
        mock_pdf.pages = [bad_page, good_page]
        mock_pdf.__enter__ = lambda s: mock_pdf
        mock_pdf.__exit__ = MagicMock(return_value=False)

        with patch("pdfplumber.open", return_value=mock_pdf):
            result = extract_text(b"fake", "application/pdf")

        assert "Valid page content" in result


class TestExtractFromImage:
    """Tests for _extract_from_image() via the public extract_text() interface."""

    def test_extracts_text_from_image(self):
        mock_image = MagicMock()
        mock_image.mode = "RGB"

        with patch("PIL.Image.open", return_value=mock_image):
            with patch("pytesseract.image_to_string", return_value="OCR extracted text"):
                result = extract_text(b"fake image bytes", "image/jpeg")

        assert result == "OCR extracted text"

    def test_converts_rgba_to_rgb(self):
        """RGBA images should be converted to RGB before OCR."""
        mock_image = MagicMock()
        mock_image.mode = "RGBA"
        converted_image = MagicMock()
        converted_image.mode = "RGB"
        mock_image.convert.return_value = converted_image

        with patch("PIL.Image.open", return_value=mock_image):
            with patch("pytesseract.image_to_string", return_value="some text") as mock_ocr:
                extract_text(b"fake", "image/png")

        # convert("RGB") should be called on the original image
        mock_image.convert.assert_called_once_with("RGB")

    def test_raises_ocr_error_when_image_open_fails(self):
        with patch("PIL.Image.open", side_effect=Exception("corrupt image")):
            with pytest.raises(OCRError, match="Failed to open image"):
                extract_text(b"fake", "image/jpeg")

    def test_raises_ocr_error_when_tesseract_not_found(self):
        import pytesseract
        mock_image = MagicMock()
        mock_image.mode = "RGB"

        with patch("PIL.Image.open", return_value=mock_image):
            with patch(
                "pytesseract.image_to_string",
                side_effect=pytesseract.TesseractNotFoundError(),
            ):
                with pytest.raises(OCRError, match="Tesseract OCR binary not found"):
                    extract_text(b"fake", "image/jpeg")

    def test_raises_ocr_error_when_tesseract_fails(self):
        import pytesseract
        mock_image = MagicMock()
        mock_image.mode = "RGB"

        with patch("PIL.Image.open", return_value=mock_image):
            with patch(
                "pytesseract.image_to_string",
                side_effect=pytesseract.TesseractError(1, "tesseract failed"),
            ):
                with pytest.raises(OCRError, match="Tesseract OCR failed"):
                    extract_text(b"fake", "image/jpeg")

    def test_raises_ocr_error_when_no_text_extracted(self):
        """Empty OCR output should raise OCRError."""
        mock_image = MagicMock()
        mock_image.mode = "RGB"
        mock_image.size = (100, 100)

        with patch("PIL.Image.open", return_value=mock_image):
            with patch("pytesseract.image_to_string", return_value="   "):
                with pytest.raises(OCRError, match="No text could be extracted"):
                    extract_text(b"fake", "image/jpeg")

    def test_strips_leading_trailing_whitespace(self):
        mock_image = MagicMock()
        mock_image.mode = "RGB"

        with patch("PIL.Image.open", return_value=mock_image):
            with patch("pytesseract.image_to_string", return_value="\n\n  hello  \n\n"):
                result = extract_text(b"fake", "image/png")

        assert result == "hello"
