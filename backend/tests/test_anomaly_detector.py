"""
tests/test_anomaly_detector.py
Unit tests for backend/services/anomaly_detector.py — orchestration layer.
The ReAct agent and retriever are mocked.
"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real")

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.api.models import (
    Anomaly,
    AnomalySeverity,
    AnomalyType,
    BillLineItem,
    BillSummary,
    RAGResult,
    RedactedBill,
)
from backend.services.anomaly_detector import (
    _compute_summary,
    _extract_candidate_codes,
    _validate_bill,
    detect_anomalies,
)


# ---- Fixtures ----

def _make_bill(text: str, filename: str = "bill.pdf", file_type: str = "pdf") -> RedactedBill:
    return RedactedBill(
        redacted_text=text,
        original_filename=filename,
        file_type=file_type,
        char_count=len(text),
    )


def _make_anomaly(
    code: str,
    anomaly_type: AnomalyType = AnomalyType.PRICE_OVERCHARGE,
    severity: AnomalySeverity = AnomalySeverity.HIGH,
    billed_amount: float = 300.0,
    overcharge_ratio: float = 3.0,
) -> Anomaly:
    return Anomaly(
        line_item=BillLineItem(code=code, description="Test service", billed_amount=billed_amount),
        anomaly_type=anomaly_type,
        severity=severity,
        explanation="Test explanation",
        suggested_action="Test action",
        medicare_reference_price=billed_amount / overcharge_ratio if overcharge_ratio else None,
        overcharge_ratio=overcharge_ratio,
    )


# ---- _validate_bill() ----

class TestValidateBill:
    def test_passes_for_bill_with_sufficient_content(self):
        bill = _make_bill("A" * 100)
        _validate_bill(bill)  # should not raise

    def test_raises_for_too_short_bill(self):
        bill = _make_bill("short")
        with pytest.raises(ValueError, match="too short"):
            _validate_bill(bill)

    def test_raises_for_whitespace_only_bill(self):
        bill = _make_bill("   \n   \t   ")
        with pytest.raises(ValueError):
            _validate_bill(bill)

    def test_boundary_exactly_50_chars_passes(self):
        bill = _make_bill("A" * 50)
        _validate_bill(bill)  # should not raise

    def test_boundary_49_chars_fails(self):
        bill = _make_bill("A" * 49)
        with pytest.raises(ValueError):
            _validate_bill(bill)


# ---- _extract_candidate_codes() ----

class TestExtractCandidateCodes:
    def test_extracts_5_digit_cpt_codes(self):
        codes = _extract_candidate_codes("Procedure 99213 and 36415 were billed")
        assert "99213" in codes
        assert "36415" in codes

    def test_extracts_hcpcs_level_2_codes(self):
        codes = _extract_candidate_codes("Code J0696 administered")
        assert "J0696" in codes

    def test_normalises_to_uppercase(self):
        codes = _extract_candidate_codes("code j0696 billed")
        assert "J0696" in codes
        assert "j0696" not in codes

    def test_deduplicates_repeated_codes(self):
        codes = _extract_candidate_codes("99213 then 99213 again and 99213 once more")
        assert codes.count("99213") == 1

    def test_returns_empty_list_when_no_codes(self):
        codes = _extract_candidate_codes("No codes in this text at all")
        assert codes == []

    def test_caps_at_100_codes(self):
        """If more than 100 unique codes are found, only the first 100 are returned."""
        # Generate 110 unique codes: 00000-00110
        text = " ".join(f"{i:05d}" for i in range(110))
        codes = _extract_candidate_codes(text)
        assert len(codes) <= 100

    def test_preserves_first_seen_order(self):
        codes = _extract_candidate_codes("99213 then 36415 then 99213 again")
        # 99213 first, 36415 second
        assert codes[0] == "99213"
        assert codes[1] == "36415"


# ---- _compute_summary() ----

class TestComputeSummary:
    def test_empty_anomaly_list(self):
        summary = _compute_summary([], total_line_items=5)
        assert summary.total_line_items == 5
        assert summary.anomaly_count == 0
        assert summary.high_severity_count == 0
        assert summary.medium_severity_count == 0
        assert summary.potential_overcharge_total is None

    def test_counts_severities(self):
        anomalies = [
            _make_anomaly("A", severity=AnomalySeverity.HIGH),
            _make_anomaly("B", severity=AnomalySeverity.HIGH),
            _make_anomaly("C", severity=AnomalySeverity.MEDIUM),
            _make_anomaly("D", severity=AnomalySeverity.LOW),
        ]
        summary = _compute_summary(anomalies, total_line_items=10)
        assert summary.anomaly_count == 4
        assert summary.high_severity_count == 2
        assert summary.medium_severity_count == 1

    def test_computes_potential_overcharge_total(self):
        """For PRICE_OVERCHARGE anomalies with price data, compute the overcharge delta."""
        # billed=300, ratio=3.0, medicare_ref=100, delta=200
        anomaly = _make_anomaly(
            "99213",
            anomaly_type=AnomalyType.PRICE_OVERCHARGE,
            billed_amount=300.0,
            overcharge_ratio=3.0,
        )
        summary = _compute_summary([anomaly], total_line_items=5)
        assert summary.potential_overcharge_total is not None
        assert summary.potential_overcharge_total == pytest.approx(200.0, abs=0.01)

    def test_potential_overcharge_none_for_non_price_anomalies(self):
        anomalies = [
            _make_anomaly("A", anomaly_type=AnomalyType.DUPLICATE_CHARGE),
            _make_anomaly("B", anomaly_type=AnomalyType.UNKNOWN_CODE),
        ]
        summary = _compute_summary(anomalies, total_line_items=5)
        assert summary.potential_overcharge_total is None

    def test_total_billed_amount_always_none(self):
        """Per design, total_billed_amount is never populated by this function."""
        summary = _compute_summary([], total_line_items=0)
        assert summary.total_billed_amount is None


# ---- detect_anomalies() integration ----

class TestDetectAnomalies:
    """Test the full detect_anomalies() pipeline with mocked sub-components."""

    def _make_long_bill(self) -> RedactedBill:
        return _make_bill(
            "Service 99213 Office visit $150\n" * 5
        )

    @pytest.mark.asyncio
    async def test_happy_path_returns_anomalies_and_summary(self):
        bill = self._make_long_bill()
        mock_anomaly = _make_anomaly("99213")
        mock_rag_result = RAGResult(
            code="99213",
            long_description="Office visit",
            short_description="Office visit",
            medicare_reference_price=89.03,
            total_rvu=2.72,
            has_price_data=True,
        )

        with patch("backend.rag.retriever.lookup_by_code", return_value={
            "code": "99213",
            "long_description": "Office visit",
            "short_description": "Office visit",
            "medicare_reference_price": 89.03,
            "total_rvu": 2.72,
            "has_price_data": True,
        }):
            with patch(
                "backend.agent.react_agent.analyze",
                new=AsyncMock(return_value=([mock_anomaly], 5)),
            ):
                anomalies, summary = await detect_anomalies(bill)

        assert len(anomalies) == 1
        assert summary.anomaly_count == 1

    @pytest.mark.asyncio
    async def test_raises_value_error_for_short_bill(self):
        bill = _make_bill("short")
        with pytest.raises(ValueError, match="too short"):
            await detect_anomalies(bill)

    @pytest.mark.asyncio
    async def test_anomalies_sorted_high_first(self):
        bill = self._make_long_bill()
        anomalies_from_agent = [
            _make_anomaly("A", severity=AnomalySeverity.LOW),
            _make_anomaly("B", severity=AnomalySeverity.HIGH),
            _make_anomaly("C", severity=AnomalySeverity.MEDIUM),
        ]

        with patch("backend.rag.retriever.lookup_by_code", return_value=None):
            with patch(
                "backend.agent.react_agent.analyze",
                new=AsyncMock(return_value=(anomalies_from_agent, 3)),
            ):
                anomalies, _ = await detect_anomalies(bill)

        severities = [a.severity for a in anomalies]
        assert severities == [AnomalySeverity.HIGH, AnomalySeverity.MEDIUM, AnomalySeverity.LOW]

    @pytest.mark.asyncio
    async def test_rag_errors_are_tolerated(self):
        """A ChromaDB error on one code should not abort the whole request."""
        bill = self._make_long_bill()

        with patch("backend.rag.retriever.lookup_by_code", side_effect=Exception("ChromaDB down")):
            with patch(
                "backend.agent.react_agent.analyze",
                new=AsyncMock(return_value=([], 5)),
            ):
                anomalies, summary = await detect_anomalies(bill)

        assert anomalies == []
        assert summary.anomaly_count == 0
