"""
backend/services/ocr.py
============================================================
PURPOSE:
    Extracts raw text from an uploaded medical bill (PDF or image).
    This is step [1] of the pipeline — the output feeds directly
    into pii_redactor.py, which strips PHI before any further use.

PUBLIC API:
    extract_text(file_bytes, mime_type) -> str

SUPPORTED INPUT TYPES:
    application/pdf  — digital PDFs via pdfplumber (preferred path)
    image/jpeg       — scanned images via pytesseract
    image/png        — scanned images via pytesseract

UNSUPPORTED (graceful error):
    Scanned PDFs (PDFs that are purely image-based with no embedded text)
    require pdf2image + poppler to render pages before OCR. Those
    libraries are not in requirements.txt. Users with scanned PDFs
    should photograph the bill and upload as JPEG/PNG instead.
    A clear error message directs them to do this.

SECURITY NOTES:
    - File type has already been validated by python-magic in middleware
      before this function is called. We trust that validation.
    - file_bytes is processed in memory only — no temp files written
      by our code. pytesseract does write to /tmp internally (the
      Tesseract binary), but that /tmp is a tmpfs RAM disk in Docker
      (see docker-compose.yml) and is never touched by our code directly.
    - The returned string is RAW bill text — it contains patient PII.
      It MUST pass immediately to pii_redactor.py and MUST NOT be
      logged, stored, or passed to any other service.

LIBRARY NOTES:
    pdfplumber==0.11.1  — wraps pdfminer.six, handles complex layouts
    pytesseract==0.3.10 — Python wrapper for Tesseract OCR binary
    Pillow==10.3.0      — required by pytesseract for image loading
============================================================
"""

import io
import logging

import pdfplumber
import pytesseract
from PIL import Image

log = logging.getLogger(__name__)


# ============================================================
# EXCEPTION
# ============================================================

class OCRError(Exception):
    """
    Raised when text extraction fails or produces no usable output.

    WHY A CUSTOM EXCEPTION:
        routes.py maps OCRError to HTTP 422 (Unprocessable Entity).
        A 422 tells the client "the file was received and validated
        but its content could not be processed." This is distinct
        from a 400 (bad request) or 500 (server error).
        Using a plain ValueError or RuntimeError would make routes.py
        catch all exceptions indiscriminately.
    """


# ============================================================
# PUBLIC API
# ============================================================

def extract_text(file_bytes: bytes, mime_type: str) -> str:
    """
    Extract raw text from a medical bill PDF or image.

    WHAT:
        Routes to the appropriate extractor based on MIME type,
        then returns the full bill text as a single string.
        Multi-page PDFs have page separators included.

    SECURITY NOTE:
        The returned string contains raw patient PII.
        It must be passed immediately to pii_redactor.py.
        NEVER log this string. NEVER pass it to llm_client.py.

    ARGS:
        file_bytes: Raw bytes of the uploaded file. In-memory only —
                    no file path, no disk write by our code.
        mime_type:  MIME type as determined by python-magic in middleware.
                    One of: "application/pdf", "image/jpeg", "image/png".
                    We trust this has already been validated.

    RETURNS:
        Extracted text as a string. May contain newlines and [Page N]
        markers for multi-page PDFs. Never empty (raises instead).

    RAISES:
        OCRError:   if extraction yields no usable text, or if the
                    Tesseract binary is not found, or if the file
                    is corrupt and cannot be parsed.
        ValueError: if mime_type is not one of the supported types.
                    (Should not happen if middleware validated correctly.)
    """
    log.info(f"OCR: extracting text from {mime_type} ({len(file_bytes):,} bytes)")

    if mime_type == "application/pdf":
        text = _extract_from_pdf(file_bytes)
    elif mime_type in ("image/jpeg", "image/png"):
        text = _extract_from_image(file_bytes)
    else:
        # Should never reach here — middleware rejects unsupported types.
        # Defensive check in case this function is called directly in tests.
        raise ValueError(
            f"Unsupported MIME type for OCR: {mime_type!r}. "
            "Expected one of: application/pdf, image/jpeg, image/png."
        )

    log.info(f"OCR: extracted {len(text):,} characters")
    # WARNING: raw PII — must pass to pii_redactor.py immediately.
    # Never log, never store, never pass to llm_client.py.
    # Post-MVP: wrap in SensitiveString type that overrides __repr__
    # to return "[REDACTED]" preventing accidental logging.
    return text


# ============================================================
# PRIVATE EXTRACTORS
# ============================================================

def _extract_from_pdf(file_bytes: bytes) -> str:
    """
    Extract text from a digital PDF using pdfplumber.

    WHAT:
        Opens the PDF from bytes, iterates all pages, and extracts
        text from each page. Pages are joined with double newlines
        and labelled "[Page N]" so the agent can orient itself.

    WHY pdfplumber OVER PyPDF2 OR pdfminer DIRECTLY:
        1. Table extraction — pdfplumber understands table structure,
           which matters for bills where all the charges are in a table.
        2. Layout preservation — extract_text(x_tolerance, y_tolerance)
           gives fine control over how close characters must be to be
           treated as the same word or line.
        3. Active maintenance — PyPDF2 had long periods of abandonment.

    WHY x_tolerance=3, y_tolerance=3:
        pdfplumber defaults are x=3, y=3 for extract_text.
        Medical bills often have tightly spaced columns (code | description
        | amount). Keeping tolerances at 3px prevents adjacent columns
        from being merged into one run of text.

    SCANNED PDF DETECTION:
        If every page returns empty text, the PDF contains no embedded
        text — it is a scanned image rendered as PDF. We cannot OCR it
        without pdf2image + poppler (not in requirements.txt).
        We raise OCRError with a clear user-facing message.

    RAISES:
        OCRError: if no text found on any page (scanned PDF),
                  or if pdfplumber cannot parse the file.
    """
    pages_text: list[str] = []

    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            if not pdf.pages:
                raise OCRError("The uploaded PDF has no pages.")

            log.debug(f"PDF has {len(pdf.pages)} page(s)")

            for page_num, page in enumerate(pdf.pages, start=1):
                # x_tolerance=3, y_tolerance=3 are pdfplumber's defaults
                # but we set them explicitly so intent is clear and any
                # future change to pdfplumber defaults doesn't silently
                # affect our output.
                try:
                    text = page.extract_text(x_tolerance=3, y_tolerance=3)
                except Exception as page_exc:
                    # One bad page (corrupt, malformed xref, etc.) should not
                    # abort extraction of the rest of the bill. Skip and continue.
                    log.warning(
                        f"Page {page_num}: extraction failed "
                        f"({type(page_exc).__name__}: {page_exc}) — skipping"
                    )
                    continue

                if text and text.strip():
                    pages_text.append(f"[Page {page_num}]\n{text.strip()}")
                else:
                    log.debug(f"Page {page_num}: no text found (blank or image-only)")

    except OCRError:
        raise
    except Exception as exc:
        # pdfplumber/pdfminer can raise a variety of exceptions for
        # corrupt, password-protected, or malformed PDFs.
        # We wrap them all into OCRError with enough context to debug.
        raise OCRError(
            f"Failed to parse PDF: {type(exc).__name__}: {exc}. "
            "The file may be corrupt, password-protected, or not a valid PDF."
        ) from exc

    if not pages_text:
        # Every page was empty — this is a scanned PDF (image inside PDF container).
        # We could attempt OCR if we had pdf2image to render pages to images,
        # but that requires poppler which is not in requirements.txt.
        # For MVP, direct users to photograph and upload as JPEG instead.
        raise OCRError(
            "This appears to be a scanned PDF. Please photograph "
            "your bill and upload as JPEG for best results."
        )

    return "\n\n".join(pages_text)


def _extract_from_image(file_bytes: bytes) -> str:
    """
    Extract text from a JPEG or PNG image using pytesseract.

    WHAT:
        Loads the image with Pillow, then runs Tesseract OCR
        with settings tuned for structured document layouts.

    WHY PSM 3 (fully automatic page segmentation):
        Medical bills have mixed content — a letterhead at the top,
        a procedure table in the middle, totals and disclaimers at
        the bottom. PSM 3 (Tesseract's fully automatic mode) handles
        multi-section documents better than PSM 6 (single text block),
        which can merge columns or fail on non-uniform layouts.

    WHY OEM 3 (LSTM engine only):
        OEM 3 uses Tesseract's neural network (LSTM) engine, which is
        significantly more accurate than the legacy engine (OEM 0).
        The Docker base image ships Tesseract 4+ which includes LSTM.
        OEM 1 (LSTM only, no legacy) is the same as OEM 3 on Tesseract 4.

    WHY PIL.Image THEN pytesseract (not pytesseract directly on bytes):
        pytesseract.image_to_string() accepts a PIL.Image, file path,
        or numpy array. Passing a PIL.Image avoids writing a temp file
        ourselves — we let the Tesseract binary manage its own temp
        files in /tmp (which is a tmpfs RAM disk in Docker).

    RAISES:
        OCRError: if Tesseract binary is not found, if the image
                  cannot be opened, or if no text is extracted.
    """
    try:
        image = Image.open(io.BytesIO(file_bytes))
    except Exception as exc:
        raise OCRError(
            f"Failed to open image: {type(exc).__name__}: {exc}. "
            "The file may be corrupt or not a valid JPEG/PNG."
        ) from exc

    # Convert to RGB if needed.
    # WHY: Tesseract works best on RGB or grayscale. RGBA images (PNG with
    # transparency) can produce artifacts. Converting to RGB is safe for
    # document images — there are no meaningful transparency layers on a bill.
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    log.debug(f"Image mode={image.mode} size={image.size}")

    try:
        # config breakdown:
        #   --psm 3  — fully automatic page segmentation (handles mixed layouts)
        #   --oem 3  — LSTM neural engine (most accurate, requires Tesseract 4+)
        text = pytesseract.image_to_string(image, config="--psm 3 --oem 3")
    except pytesseract.TesseractNotFoundError as exc:
        raise OCRError(
            "Tesseract OCR binary not found. "
            "It must be installed in the Docker image (apt-get install tesseract-ocr)."
        ) from exc
    except pytesseract.TesseractError as exc:
        raise OCRError(
            f"Tesseract OCR failed: {exc}. "
            "The image may be too small, too blurry, or in an unsupported format."
        ) from exc

    if not text.strip():
        log.warning(f"Tesseract returned empty string for image size={image.size} mode={image.mode}")
        raise OCRError(
            "No text could be extracted from the image. "
            "Try a clearer, higher-resolution photo of the bill, "
            "with good lighting and no glare."
        )

    return text.strip()
