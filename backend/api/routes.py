"""
backend/api/routes.py
============================================================
PURPOSE:
    HTTP endpoint definitions for the MedBill Scanner API.

ENDPOINTS:
    POST /api/analyze  — upload a bill, get anomalies + dispute letter
    GET  /health       — liveness + ChromaDB connectivity check

PIPELINE (POST /api/analyze):
    UploadFile
        │
        ▼
    validate_upload()      — magic bytes + size check (middleware.py)
        │
        ▼
    ocr.extract_text()     — pdfplumber or pytesseract
        │
        ▼
    pii_redactor.redact_pii()  — strip PII, assert_no_pii_leak()
        │
        ▼
    anomaly_detector.detect_anomalies()  — RAG enrichment + ReAct agent
        │
        ▼
    dispute_generator.generate()         — draft dispute letter
        │
        ▼
    AnalysisResponse                     — returned to frontend

SECURITY NOTES:
    - File is read into memory in one shot and NEVER written to disk.
    - PII redaction runs before any Anthropic API call.
    - Rate limiting is applied via @limiter.limit() on each endpoint.
    - All error responses use the structured ErrorResponse model.
    - Specific exceptions are caught and mapped to HTTP status codes;
      unexpected exceptions return 500 without leaking internals.
============================================================
"""

import logging
import time
from typing import Annotated

import magic
from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import JSONResponse

from backend.api.middleware import limiter, validate_upload
from backend.api.models import (
    AnalysisResponse,
    ErrorResponse,
    HealthResponse,
    RedactedBill,
)
from backend.config import settings
from backend.rag import retriever
from backend.services import anomaly_detector, dispute_generator, pii_redactor
from backend.services.ocr import OCRError, extract_text as ocr_extract_text

log = logging.getLogger(__name__)

router = APIRouter()


# ============================================================
# POST /api/analyze
# ============================================================

@router.post(
    "/analyze",
    response_model=AnalysisResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Bill too short / unreadable"},
        413: {"model": ErrorResponse, "description": "File too large"},
        415: {"model": ErrorResponse, "description": "Unsupported file type"},
        429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
    summary="Analyze a medical bill for anomalies",
    description=(
        "Upload a PDF or image of a medical bill. "
        "Returns detected anomalies and a dispute letter template. "
        "Patient PII is stripped before any AI processing."
    ),
)
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def analyze_bill(
    request: Request,
    file: Annotated[UploadFile, File(description="Medical bill (PDF, JPEG, or PNG)")],
) -> AnalysisResponse | JSONResponse:
    """
    Main bill analysis endpoint.

    WHAT:
        Runs the full pipeline: OCR → PII redaction → RAG → ReAct agent
        → dispute letter generation. Returns structured results.

    WHY request PARAMETER:
        slowapi's @limiter.limit() requires the FastAPI Request object
        as the first parameter after self (or as a named parameter).
        It is not used directly in this function body.

    ARGS:
        request: FastAPI Request (required by slowapi rate limiter).
        file:    The uploaded bill file from the multipart form.

    RETURNS:
        AnalysisResponse on success (HTTP 200).
        JSONResponse with ErrorResponse body on failure.
    """
    start_time = time.monotonic()
    log.info(f"POST /api/analyze — file='{file.filename}', content_type='{file.content_type}'")

    # --- Step 1: Read file into memory ---
    # WHY read all at once: we need the full bytes for magic byte validation
    # before doing anything else. Never write to disk.
    try:
        file_bytes = await file.read()
    except Exception as exc:
        log.error(f"Failed to read uploaded file: {exc}")
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error="upload_read_failed",
                detail="Could not read the uploaded file. Please try again.",
            ).model_dump(),
        )

    # --- Step 2: Validate file type and size ---
    validation_error = validate_upload(file, file_bytes)
    if validation_error:
        # Distinguish size errors (413) from type errors (415)
        if "exceeds the" in validation_error:
            status = 413
            error_code = "file_too_large"
        else:
            status = 415
            error_code = "invalid_file_type"
        log.warning(f"File validation failed: {validation_error}")
        return JSONResponse(
            status_code=status,
            content=ErrorResponse(error=error_code, detail=validation_error).model_dump(),
        )

    # --- Step 3: Detect MIME type (already validated, now used for OCR routing) ---
    mime_type = magic.from_buffer(file_bytes, mime=True)

    # --- Step 4: OCR ---
    try:
        raw_text = ocr_extract_text(file_bytes=file_bytes, mime_type=mime_type)
    except OCRError as exc:
        # OCRError = file is readable but unextractable (blank page, corrupt scan).
        # This is a user error — 422 with the specific reason from ocr.py.
        log.warning(f"OCR could not extract text from '{file.filename}': {exc}")
        return JSONResponse(
            status_code=422,
            content=ErrorResponse(
                error="ocr_failed",
                detail=str(exc),
            ).model_dump(),
        )
    except Exception as exc:
        # Unexpected failure (e.g., pdfplumber crash, missing Tesseract binary).
        log.error(f"Unexpected OCR error for '{file.filename}': {exc}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error="ocr_failed",
                detail="Could not extract text from the bill. Try a clearer image.",
            ).model_dump(),
        )

    # --- Step 5: PII Redaction ---
    # SECURITY: PII must be stripped before text reaches the Anthropic API.
    redaction_result = pii_redactor.redact_pii(raw_text)

    # assert_no_pii_leak is a belt-and-suspenders check before the LLM call.
    # If it returns False, the redaction may have missed something — abort.
    if not pii_redactor.assert_no_pii_leak(raw_text, redaction_result.redacted_text):
        log.error(
            f"PII leak detected in redacted output for '{file.filename}'. "
            "Aborting — redacted text will NOT be sent to Anthropic API."
        )
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error="pii_redaction_failed",
                detail="Could not safely redact patient information. Please try again.",
            ).model_dump(),
        )

    file_type = "pdf" if mime_type == "application/pdf" else "image"
    redacted_bill = RedactedBill(
        redacted_text=redaction_result.redacted_text,
        original_filename=file.filename or "unknown",
        file_type=file_type,
        char_count=len(redaction_result.redacted_text),
    )
    log.info(
        f"OCR+redaction complete: {redacted_bill.char_count} chars, "
        f"{redaction_result.total_redactions} PII item(s) redacted"
    )

    # --- Step 6: Anomaly detection (RAG + ReAct agent) ---
    try:
        anomalies, bill_summary = await anomaly_detector.detect_anomalies(redacted_bill)
    except ValueError as exc:
        # Bill content too short / unreadable
        log.warning(f"Analysis rejected short bill '{file.filename}': {exc}")
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                error="bill_too_short",
                detail=str(exc),
            ).model_dump(),
        )
    except Exception as exc:
        log.error(f"Anomaly detection failed for '{file.filename}': {exc}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error="analysis_failed",
                detail="Bill analysis failed. Please try again.",
            ).model_dump(),
        )

    # --- Step 7: Dispute letter ---
    dispute_letter = None
    if anomalies:
        try:
            dispute_letter = await dispute_generator.generate(
                anomalies=anomalies,
                bill_summary=bill_summary,
            )
        except Exception as exc:
            # Non-fatal: return anomalies even if letter generation fails.
            # The patient gets their findings; they just won't have a draft letter.
            log.error(
                f"Dispute letter generation failed for '{file.filename}': {exc}",
                exc_info=True,
            )

    elapsed = round(time.monotonic() - start_time, 2)
    log.info(
        f"Analysis complete for '{file.filename}': "
        f"{bill_summary.anomaly_count} anomalies, "
        f"letter={'yes' if dispute_letter else 'no'}, "
        f"elapsed={elapsed}s"
    )

    return AnalysisResponse(
        anomalies=anomalies,
        dispute_letter=dispute_letter,
        bill_summary=bill_summary,
        processing_time_seconds=elapsed,
    )


# ============================================================
# GET /health
# ============================================================

@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
    description="Returns ChromaDB connectivity status and HCPCS collection size.",
)
async def health_check() -> HealthResponse:
    """
    Liveness + readiness check endpoint.

    WHAT:
        1. Pings ChromaDB and counts documents in the HCPCS collection.
        2. Returns status="ok" if connected and collection non-empty.
           status="degraded" if connected but collection is empty (ingest not run).
           status="unavailable" if ChromaDB is unreachable.

    WHY NO RATE LIMIT HERE:
        /health is called by docker-compose healthcheck, load balancers,
        and monitoring systems. Rate limiting it would cause false
        "unhealthy" readings from legitimate infrastructure.

    WHY NOT JUST A 200/503:
        The HealthResponse body tells the operator WHY the service
        is unhealthy (ChromaDB down vs. collection empty). A plain
        200/503 requires reading logs to distinguish the two cases.
    """
    try:
        collection_size = retriever.get_collection_size()
        connected = True
    except Exception as exc:
        log.warning(f"ChromaDB health check failed: {exc}")
        return HealthResponse(
            status="unavailable",
            chromadb_connected=False,
            collection_size=0,
        )

    if collection_size == 0:
        log.warning("ChromaDB connected but HCPCS collection is empty. Run ingest.py.")
        return HealthResponse(
            status="degraded",
            chromadb_connected=True,
            collection_size=0,
        )

    return HealthResponse(
        status="ok",
        chromadb_connected=True,
        collection_size=collection_size,
    )
