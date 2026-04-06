"""
backend/api/middleware.py
============================================================
PURPOSE:
    FastAPI middleware and app-level configuration:
    1. CORS — locked to FRONTEND_URL, never wildcard *
    2. Rate limiting — 10 req/min/IP via slowapi
    3. File validation helpers — magic bytes + size + type checks
       (called from routes.py, not applied as middleware)

    This file is intentionally thin. Security constraints live
    here as a single, auditable location. Adding a new route
    does NOT require touching this file — the middleware applies
    globally once attached in main.py.

SECURITY NOTES:
    - CORS wildcard (*) is never allowed. See configure_cors().
    - Rate limiter uses the real client IP, not X-Forwarded-For,
      unless the app is behind a trusted proxy (not MVP scope).
    - File validation checks magic bytes (content) not extension
      (filename). Extensions can be forged; magic bytes cannot.
    - MAX_UPLOAD_SIZE_MB is enforced before any OCR attempt to
      prevent memory exhaustion from large files.
============================================================
"""

import logging
from typing import Optional

import magic
from fastapi import Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter
from slowapi.util import get_remote_address

from backend.config import settings

log = logging.getLogger(__name__)


# ============================================================
# RATE LIMITER
# ============================================================

# Module-level limiter singleton.
# WHY MODULE-LEVEL: slowapi requires the Limiter to be created once
# and referenced by routes via the @limiter.limit() decorator.
# Importing this object in routes.py is the standard slowapi pattern.
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[f"{settings.rate_limit_per_minute}/minute"],
)


# ============================================================
# CORS
# ============================================================

def configure_cors(app) -> None:
    """
    Attach CORSMiddleware to the FastAPI app, locked to FRONTEND_URL.

    WHAT:
        Allows cross-origin requests only from the configured
        FRONTEND_URL (e.g., http://localhost:3000). All other origins
        are rejected with HTTP 403.

    WHY NOT WILDCARD:
        A wildcard CORS policy (*) allows any website to call the API
        from a user's browser. This would expose the rate-limited API
        to any third-party page the user visits. Locking to FRONTEND_URL
        ensures only our frontend can call our backend.

    SECURITY NOTE:
        This only restricts browser-originated cross-origin requests.
        Direct HTTP calls (curl, Postman, scripts) are not blocked by
        CORS — that is expected. CORS is a browser security feature,
        not an authentication mechanism.

    ARGS:
        app: The FastAPI application instance from main.py.
    """
    log.info(f"CORS: allowing origin '{settings.frontend_url}'")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_url],
        allow_credentials=True,
        allow_methods=["GET", "POST"],  # only methods our API uses
        allow_headers=["Content-Type"],
        max_age=3600,
    )


# ============================================================
# FILE VALIDATION
# ============================================================

# Magic byte signatures for allowed file types.
# WHY DICT (not set): we need the MIME string for error messages
# ("expected image/jpeg, got application/zip").
_ALLOWED_MIME_TYPES: dict[str, str] = {
    "application/pdf": "PDF",
    "image/jpeg": "JPEG",
    "image/png": "PNG",
}

# Tesseract accepts JPEG and PNG natively.
# pdfplumber accepts application/pdf.
# Nothing else should ever reach the OCR layer.


def validate_upload(
    file: UploadFile,
    file_bytes: bytes,
) -> Optional[str]:
    """
    Validate an uploaded file's type and size. Returns error string or None.

    WHAT:
        1. Checks file size against MAX_UPLOAD_SIZE_MB.
        2. Checks magic bytes using python-magic to determine true MIME type.
        3. Rejects MIME types not in _ALLOWED_MIME_TYPES.
        Returns None if all checks pass, or an error message string if any fail.

    WHY RETURN STRING (not raise):
        routes.py needs to return a structured ErrorResponse with a specific
        HTTP status code (413 for size, 415 for type). The caller decides
        the response shape; this function decides pass/fail.

    WHY MAGIC BYTES (not Content-Type header or file extension):
        - Content-Type header is set by the browser/client, trivially forged.
        - File extension is part of the filename, trivially forged.
        - Magic bytes are the first bytes of the file content.
          A PNG will always start with \x89PNG regardless of what
          the sender claims it is. An attacker cannot rename a shell
          script to "bill.pdf" and fool python-magic.

    SECURITY NOTE:
        file_bytes must be the full file contents. Do NOT pass a partial
        read — python-magic needs enough bytes to identify the format
        (typically 8-16 bytes, but 4KB+ is safer for edge cases).

    ARGS:
        file:       The UploadFile from FastAPI (used for filename in errors).
        file_bytes: The complete file contents already read into memory.

    RETURNS:
        None if file passes all checks.
        Error string describing the failure if any check fails.
    """
    # --- Size check ---
    size_mb = len(file_bytes) / (1024 * 1024)
    max_mb = settings.max_upload_size_mb
    if size_mb > max_mb:
        return (
            f"File '{file.filename}' is {size_mb:.1f} MB, "
            f"which exceeds the {max_mb} MB limit. "
            "Please reduce the file size and try again."
        )

    # --- Magic bytes type check ---
    detected_mime = magic.from_buffer(file_bytes, mime=True)
    if detected_mime not in _ALLOWED_MIME_TYPES:
        allowed_names = ", ".join(_ALLOWED_MIME_TYPES.values())
        return (
            f"File '{file.filename}' was detected as '{detected_mime}', "
            f"which is not supported. Allowed types: {allowed_names}."
        )

    log.debug(
        f"File validation passed: '{file.filename}' "
        f"({detected_mime}, {size_mb:.2f} MB)"
    )
    return None
