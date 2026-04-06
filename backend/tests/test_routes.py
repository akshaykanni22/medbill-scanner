"""
tests/test_routes.py
Unit tests for backend/api/routes.py — HTTP endpoint behavior.
Uses FastAPI TestClient. All external services are mocked.
"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real")

import io
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.models import (
    Anomaly,
    AnomalySeverity,
    AnomalyType,
    BillLineItem,
    BillSummary,
    DisputeLetter,
)
from backend.api.routes import router
from backend.services.ocr import OCRError


# ---- App setup ----

def _make_app() -> FastAPI:
    """Create a minimal FastAPI app with the routes and rate limiter attached."""
    from backend.api.middleware import limiter
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded

    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.include_router(router, prefix="/api")
    return app


@pytest.fixture
def client():
    app = _make_app()
    with TestClient(app) as c:
        yield c


# ---- Helpers ----

def _make_bill_summary(
    anomaly_count: int = 0,
    high_count: int = 0,
    medium_count: int = 0,
) -> BillSummary:
    return BillSummary(
        total_line_items=5,
        total_billed_amount=None,
        anomaly_count=anomaly_count,
        high_severity_count=high_count,
        medium_severity_count=medium_count,
        potential_overcharge_total=None,
    )


def _make_anomaly() -> Anomaly:
    return Anomaly(
        line_item=BillLineItem(code="99213", description="Office visit", billed_amount=300.0),
        anomaly_type=AnomalyType.PRICE_OVERCHARGE,
        severity=AnomalySeverity.HIGH,
        explanation="Overpriced.",
        suggested_action="Dispute it.",
        medicare_reference_price=100.0,
        overcharge_ratio=3.0,
    )


def _make_pdf_upload(content: bytes = b"fake pdf content"):
    return {"file": ("bill.pdf", io.BytesIO(content), "application/pdf")}


# ---- GET /api/health ----

class TestHealthEndpoint:
    def test_returns_200_when_chromadb_ok(self, client):
        with patch("backend.rag.retriever.get_collection_size", return_value=1000):
            response = client.get("/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["chromadb_connected"] is True
        assert data["collection_size"] == 1000

    def test_returns_degraded_when_collection_empty(self, client):
        with patch("backend.rag.retriever.get_collection_size", return_value=0):
            response = client.get("/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "degraded"

    def test_returns_unavailable_when_chromadb_down(self, client):
        with patch(
            "backend.rag.retriever.get_collection_size",
            side_effect=Exception("connection refused"),
        ):
            response = client.get("/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "unavailable"
        assert data["chromadb_connected"] is False


# ---- POST /api/analyze ----

class TestAnalyzeEndpoint:
    def _patch_pipeline(
        self,
        mime_type: str = "application/pdf",
        raw_text: str = "Procedure 99213 $150",
        anomalies=None,
        summary=None,
        dispute_letter=None,
    ):
        """Context manager stack that mocks the entire pipeline."""
        if anomalies is None:
            anomalies = []
        if summary is None:
            summary = _make_bill_summary()

        return {
            "magic": patch("backend.api.routes.magic.from_buffer", return_value=mime_type),
            "validate": patch("backend.api.middleware.validate_upload", return_value=None),
            "ocr": patch(
                "backend.api.routes.ocr_extract_text",
                return_value=raw_text,
            ),
            "redact": patch(
                "backend.api.routes.pii_redactor.redact_pii",
                return_value=MagicMock(
                    redacted_text=raw_text,
                    total_redactions=0,
                    found_pii=False,
                ),
            ),
            "no_leak": patch(
                "backend.api.routes.pii_redactor.assert_no_pii_leak",
                return_value=True,
            ),
            "detect": patch(
                "backend.api.routes.anomaly_detector.detect_anomalies",
                new=AsyncMock(return_value=(anomalies, summary)),
            ),
            "letter": patch(
                "backend.api.routes.dispute_generator.generate",
                new=AsyncMock(return_value=dispute_letter),
            ),
        }

    def test_happy_path_returns_200(self, client):
        patches = self._patch_pipeline()
        with (
            patches["magic"],
            patches["validate"],
            patches["ocr"],
            patches["redact"],
            patches["no_leak"],
            patches["detect"],
            patches["letter"],
        ):
            response = client.post("/api/analyze", files=_make_pdf_upload())
        assert response.status_code == 200
        data = response.json()
        assert "anomalies" in data
        assert "bill_summary" in data
        assert "processing_time_seconds" in data

    def test_returns_422_when_ocr_fails(self, client):
        with patch("backend.api.routes.magic.from_buffer", return_value="application/pdf"):
            with patch("backend.api.middleware.validate_upload", return_value=None):
                with patch(
                    "backend.api.routes.ocr_extract_text",
                    side_effect=OCRError("blank page"),
                ):
                    response = client.post("/api/analyze", files=_make_pdf_upload())
        assert response.status_code == 422
        data = response.json()
        assert data["error"] == "ocr_failed"

    def test_returns_415_for_disallowed_file_type(self, client):
        type_error_msg = "detected as 'text/html', which is not supported"
        with patch("backend.api.routes.magic.from_buffer", return_value="text/html"):
            with patch(
                "backend.api.middleware.validate_upload",
                return_value=type_error_msg,
            ):
                response = client.post(
                    "/api/analyze",
                    files={"file": ("bad.html", io.BytesIO(b"<html>"), "text/html")},
                )
        assert response.status_code == 415
        data = response.json()
        assert data["error"] == "invalid_file_type"

    def test_returns_413_for_oversized_file(self, client):
        # Send a file that's genuinely over the 10 MB limit so the real
        # validate_upload size check triggers without any mocking.
        # Size check runs before magic bytes in validate_upload, so no
        # magic mock is needed — the function returns early on size alone.
        oversized_bytes = b"x" * (10 * 1024 * 1024 + 1)
        response = client.post(
            "/api/analyze",
            files={"file": ("big_bill.pdf", io.BytesIO(oversized_bytes), "application/pdf")},
        )
        assert response.status_code == 413
        data = response.json()
        assert data["error"] == "file_too_large"

    def test_returns_400_when_bill_too_short(self, client):
        patches = self._patch_pipeline()
        with (
            patches["magic"],
            patches["validate"],
            patches["ocr"],
            patches["redact"],
            patches["no_leak"],
        ):
            with patch(
                "backend.api.routes.anomaly_detector.detect_anomalies",
                new=AsyncMock(side_effect=ValueError("Bill text is too short")),
            ):
                response = client.post("/api/analyze", files=_make_pdf_upload())
        assert response.status_code == 400
        data = response.json()
        assert data["error"] == "bill_too_short"

    def test_returns_500_when_pii_leak_detected(self, client):
        with patch("backend.api.routes.magic.from_buffer", return_value="application/pdf"):
            with patch("backend.api.middleware.validate_upload", return_value=None):
                with patch(
                    "backend.api.routes.ocr_extract_text",
                    return_value="SSN: 123-45-6789 in bill",
                ):
                    with patch(
                        "backend.api.routes.pii_redactor.redact_pii",
                        return_value=MagicMock(
                            redacted_text="SSN: 123-45-6789 in bill",  # "forgot" to redact
                            total_redactions=0,
                        ),
                    ):
                        with patch(
                            "backend.api.routes.pii_redactor.assert_no_pii_leak",
                            return_value=False,
                        ):
                            response = client.post("/api/analyze", files=_make_pdf_upload())
        assert response.status_code == 500
        data = response.json()
        assert data["error"] == "pii_redaction_failed"

    def test_returns_anomalies_and_letter_when_found(self, client):
        mock_anomaly = _make_anomaly()
        mock_summary = _make_bill_summary(anomaly_count=1, high_count=1)
        mock_letter = DisputeLetter(
            subject_line="Dispute: Possible Overcharge",
            body="Dear Billing Department,\n\nI dispute charge 99213...",
            anomaly_codes=["99213"],
        )
        patches = self._patch_pipeline(
            anomalies=[mock_anomaly],
            summary=mock_summary,
            dispute_letter=mock_letter,
        )
        with (
            patches["magic"],
            patches["validate"],
            patches["ocr"],
            patches["redact"],
            patches["no_leak"],
            patches["detect"],
            patches["letter"],
        ):
            response = client.post("/api/analyze", files=_make_pdf_upload())
        assert response.status_code == 200
        data = response.json()
        assert len(data["anomalies"]) == 1
        assert data["dispute_letter"] is not None
        assert data["dispute_letter"]["subject_line"] == "Dispute: Possible Overcharge"

    def test_returns_results_even_when_letter_generation_fails(self, client):
        """Dispute letter generation failure should not abort the response."""
        from backend.services.llm_client import LLMError
        mock_anomaly = _make_anomaly()
        mock_summary = _make_bill_summary(anomaly_count=1, high_count=1)
        patches = self._patch_pipeline(
            anomalies=[mock_anomaly],
            summary=mock_summary,
        )
        with (
            patches["magic"],
            patches["validate"],
            patches["ocr"],
            patches["redact"],
            patches["no_leak"],
            patches["detect"],
        ):
            with patch(
                "backend.api.routes.dispute_generator.generate",
                new=AsyncMock(side_effect=LLMError("API down")),
            ):
                response = client.post("/api/analyze", files=_make_pdf_upload())

        # Should still return 200 with anomalies but no letter
        assert response.status_code == 200
        data = response.json()
        assert len(data["anomalies"]) == 1
        assert data["dispute_letter"] is None
