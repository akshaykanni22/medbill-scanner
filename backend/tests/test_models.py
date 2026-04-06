"""
tests/test_models.py
Unit tests for backend/api/models.py — Pydantic model validation.
"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real")

import pytest
from pydantic import ValidationError

from backend.api.models import (
    AnomalyType,
    AnomalySeverity,
    RedactedBill,
    RAGResult,
    BillLineItem,
    Anomaly,
    BillSummary,
    DisputeLetter,
    AnalysisResponse,
    ErrorResponse,
    HealthResponse,
)


# ---- AnomalyType ----

class TestAnomalyType:
    def test_all_values_are_strings(self):
        for member in AnomalyType:
            assert isinstance(member.value, str)

    def test_price_overcharge_value(self):
        assert AnomalyType.PRICE_OVERCHARGE == "price_overcharge"

    def test_duplicate_charge_value(self):
        assert AnomalyType.DUPLICATE_CHARGE == "duplicate_charge"

    def test_unknown_code_value(self):
        assert AnomalyType.UNKNOWN_CODE == "unknown_code"


# ---- AnomalySeverity ----

class TestAnomalySeverity:
    def test_all_values_are_strings(self):
        for member in AnomalySeverity:
            assert isinstance(member.value, str)

    def test_high_value(self):
        assert AnomalySeverity.HIGH == "high"

    def test_info_value(self):
        assert AnomalySeverity.INFO == "info"


# ---- RedactedBill ----

class TestRedactedBill:
    def _valid_data(self):
        return {
            "redacted_text": "Procedure: 99213  Amount: $150",
            "original_filename": "bill.pdf",
            "file_type": "pdf",
            "char_count": 30,
        }

    def test_valid_construction(self):
        bill = RedactedBill(**self._valid_data())
        assert bill.redacted_text == "Procedure: 99213  Amount: $150"
        assert bill.file_type == "pdf"

    def test_image_file_type_accepted(self):
        data = self._valid_data()
        data["file_type"] = "image"
        bill = RedactedBill(**data)
        assert bill.file_type == "image"

    def test_invalid_file_type_rejected(self):
        data = self._valid_data()
        data["file_type"] = "docx"
        with pytest.raises(ValidationError):
            RedactedBill(**data)

    def test_negative_char_count_rejected(self):
        data = self._valid_data()
        data["char_count"] = -1
        with pytest.raises(ValidationError):
            RedactedBill(**data)

    def test_missing_required_field_raises(self):
        data = self._valid_data()
        del data["redacted_text"]
        with pytest.raises(ValidationError):
            RedactedBill(**data)

    def test_frozen_model_cannot_be_mutated(self):
        bill = RedactedBill(**self._valid_data())
        with pytest.raises(Exception):
            bill.char_count = 999  # type: ignore[misc]


# ---- RAGResult ----

class TestRAGResult:
    def _valid_data(self):
        return {
            "code": "99213",
            "long_description": "Office or other outpatient visit, established patient",
            "short_description": "Office/outpatient visit, est",
            "medicare_reference_price": 89.03,
            "total_rvu": 2.72,
            "has_price_data": True,
            "similarity_score": None,
        }

    def test_valid_construction(self):
        result = RAGResult(**self._valid_data())
        assert result.code == "99213"
        assert result.has_price_data is True

    def test_similarity_score_optional(self):
        result = RAGResult(**self._valid_data())
        assert result.similarity_score is None

    def test_similarity_score_in_range(self):
        data = self._valid_data()
        data["similarity_score"] = 0.87
        result = RAGResult(**data)
        assert result.similarity_score == 0.87

    def test_similarity_score_above_1_rejected(self):
        data = self._valid_data()
        data["similarity_score"] = 1.5
        with pytest.raises(ValidationError):
            RAGResult(**data)

    def test_negative_price_rejected(self):
        data = self._valid_data()
        data["medicare_reference_price"] = -10.0
        with pytest.raises(ValidationError):
            RAGResult(**data)

    def test_frozen_model(self):
        result = RAGResult(**self._valid_data())
        with pytest.raises(Exception):
            result.code = "00000"  # type: ignore[misc]


# ---- BillLineItem ----

class TestBillLineItem:
    def test_valid_construction(self):
        item = BillLineItem(description="Office visit", billed_amount=150.0)
        assert item.description == "Office visit"
        assert item.quantity == 1  # default

    def test_code_optional(self):
        item = BillLineItem(description="Unknown service")
        assert item.code is None

    def test_billed_amount_optional(self):
        item = BillLineItem(description="Lab test")
        assert item.billed_amount is None

    def test_service_date_optional(self):
        item = BillLineItem(description="Lab test")
        assert item.service_date is None

    def test_quantity_must_be_at_least_1(self):
        with pytest.raises(ValidationError):
            BillLineItem(description="Test", quantity=0)

    def test_negative_billed_amount_rejected(self):
        with pytest.raises(ValidationError):
            BillLineItem(description="Test", billed_amount=-5.0)

    def test_missing_description_raises(self):
        with pytest.raises(ValidationError):
            BillLineItem()  # type: ignore[call-arg]


# ---- Anomaly ----

class TestAnomaly:
    def _make_line_item(self):
        return BillLineItem(
            code="99213",
            description="Office visit",
            billed_amount=300.0,
        )

    def _valid_data(self):
        return {
            "line_item": self._make_line_item(),
            "anomaly_type": AnomalyType.PRICE_OVERCHARGE,
            "severity": AnomalySeverity.HIGH,
            "explanation": "Charge is 3x the Medicare reference rate.",
            "suggested_action": "Request itemized bill.",
            "medicare_reference_price": 100.0,
            "overcharge_ratio": 3.0,
        }

    def test_valid_construction(self):
        anomaly = Anomaly(**self._valid_data())
        assert anomaly.anomaly_type == AnomalyType.PRICE_OVERCHARGE
        assert anomaly.severity == AnomalySeverity.HIGH

    def test_medicare_reference_price_optional(self):
        data = self._valid_data()
        data["medicare_reference_price"] = None
        anomaly = Anomaly(**data)
        assert anomaly.medicare_reference_price is None

    def test_overcharge_ratio_optional(self):
        data = self._valid_data()
        data["overcharge_ratio"] = None
        anomaly = Anomaly(**data)
        assert anomaly.overcharge_ratio is None

    def test_missing_explanation_raises(self):
        data = self._valid_data()
        del data["explanation"]
        with pytest.raises(ValidationError):
            Anomaly(**data)

    def test_missing_suggested_action_raises(self):
        data = self._valid_data()
        del data["suggested_action"]
        with pytest.raises(ValidationError):
            Anomaly(**data)


# ---- BillSummary ----

class TestBillSummary:
    def _valid_data(self):
        return {
            "total_line_items": 10,
            "total_billed_amount": None,
            "anomaly_count": 3,
            "high_severity_count": 1,
            "medium_severity_count": 2,
            "potential_overcharge_total": 450.0,
        }

    def test_valid_construction(self):
        summary = BillSummary(**self._valid_data())
        assert summary.total_line_items == 10
        assert summary.anomaly_count == 3

    def test_total_billed_amount_optional(self):
        summary = BillSummary(**self._valid_data())
        assert summary.total_billed_amount is None

    def test_potential_overcharge_total_optional(self):
        data = self._valid_data()
        data["potential_overcharge_total"] = None
        summary = BillSummary(**data)
        assert summary.potential_overcharge_total is None

    def test_negative_counts_rejected(self):
        data = self._valid_data()
        data["total_line_items"] = -1
        with pytest.raises(ValidationError):
            BillSummary(**data)


# ---- DisputeLetter ----

class TestDisputeLetter:
    def test_valid_construction(self):
        letter = DisputeLetter(
            subject_line="Dispute: Possible Overcharge",
            body="Dear Billing Department,\n\nI am writing to dispute...",
            anomaly_codes=["99213"],
        )
        assert letter.subject_line == "Dispute: Possible Overcharge"
        assert "99213" in letter.anomaly_codes

    def test_anomaly_codes_defaults_to_empty_list(self):
        letter = DisputeLetter(
            subject_line="Dispute",
            body="Letter body.",
        )
        assert letter.anomaly_codes == []

    def test_missing_subject_line_raises(self):
        with pytest.raises(ValidationError):
            DisputeLetter(body="Body text.")  # type: ignore[call-arg]


# ---- AnalysisResponse ----

class TestAnalysisResponse:
    def _make_summary(self):
        return BillSummary(
            total_line_items=5,
            total_billed_amount=None,
            anomaly_count=0,
            high_severity_count=0,
            medium_severity_count=0,
            potential_overcharge_total=None,
        )

    def test_valid_construction_no_anomalies(self):
        response = AnalysisResponse(
            anomalies=[],
            dispute_letter=None,
            bill_summary=self._make_summary(),
            processing_time_seconds=2.5,
        )
        assert response.anomalies == []
        assert response.dispute_letter is None

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            AnalysisResponse(
                anomalies=[],
                dispute_letter=None,
                bill_summary=self._make_summary(),
                processing_time_seconds=1.0,
                unexpected_field="bad",
            )

    def test_negative_processing_time_rejected(self):
        with pytest.raises(ValidationError):
            AnalysisResponse(
                anomalies=[],
                dispute_letter=None,
                bill_summary=self._make_summary(),
                processing_time_seconds=-1.0,
            )


# ---- ErrorResponse ----

class TestErrorResponse:
    def test_valid_construction(self):
        err = ErrorResponse(error="file_too_large", detail="File exceeds the limit.")
        assert err.error == "file_too_large"
        assert "limit" in err.detail

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            ErrorResponse(error="err", detail="msg", extra="bad")


# ---- HealthResponse ----

class TestHealthResponse:
    def test_ok_status(self):
        h = HealthResponse(status="ok", chromadb_connected=True, collection_size=1000)
        assert h.status == "ok"

    def test_degraded_status(self):
        h = HealthResponse(status="degraded", chromadb_connected=True, collection_size=0)
        assert h.status == "degraded"

    def test_unavailable_status(self):
        h = HealthResponse(status="unavailable", chromadb_connected=False, collection_size=0)
        assert h.chromadb_connected is False

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            HealthResponse(status="unknown", chromadb_connected=True, collection_size=0)

    def test_negative_collection_size_rejected(self):
        with pytest.raises(ValidationError):
            HealthResponse(status="ok", chromadb_connected=True, collection_size=-1)

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            HealthResponse(status="ok", chromadb_connected=True, collection_size=0, extra="bad")
