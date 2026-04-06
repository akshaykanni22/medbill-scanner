"""
tests/test_dispute_generator.py
Unit tests for backend/services/dispute_generator.py.
The LLM client is mocked — no real Anthropic API calls.
"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real")

import pytest
from unittest.mock import AsyncMock, patch

from backend.api.models import (
    Anomaly,
    AnomalySeverity,
    AnomalyType,
    BillLineItem,
    BillSummary,
    DisputeLetter,
)
from backend.services.dispute_generator import (
    _build_prompt,
    _build_subject_line,
    _extract_anomaly_codes,
    generate,
)


# ---- Helpers ----

def _make_anomaly(
    code: str | None = "99213",
    anomaly_type: AnomalyType = AnomalyType.PRICE_OVERCHARGE,
    severity: AnomalySeverity = AnomalySeverity.HIGH,
    billed_amount: float | None = 300.0,
) -> Anomaly:
    return Anomaly(
        line_item=BillLineItem(
            code=code,
            description="Office visit",
            billed_amount=billed_amount,
        ),
        anomaly_type=anomaly_type,
        severity=severity,
        explanation="Billed 3x Medicare rate.",
        suggested_action="Request itemized bill.",
        medicare_reference_price=100.0,
        overcharge_ratio=3.0 if billed_amount else None,
    )


def _make_summary(
    anomaly_count: int = 1,
    high_count: int = 1,
    medium_count: int = 0,
    overcharge_total: float | None = 200.0,
) -> BillSummary:
    return BillSummary(
        total_line_items=5,
        total_billed_amount=None,
        anomaly_count=anomaly_count,
        high_severity_count=high_count,
        medium_severity_count=medium_count,
        potential_overcharge_total=overcharge_total,
    )


# ---- _extract_anomaly_codes() ----

class TestExtractAnomalyCodes:
    def test_extracts_code_from_anomaly(self):
        anomaly = _make_anomaly(code="99213")
        codes = _extract_anomaly_codes([anomaly])
        assert "99213" in codes

    def test_deduplicates_repeated_codes(self):
        anomalies = [_make_anomaly(code="99213"), _make_anomaly(code="99213")]
        codes = _extract_anomaly_codes(anomalies)
        assert codes.count("99213") == 1

    def test_skips_anomalies_without_code(self):
        anomaly = _make_anomaly(code=None)
        codes = _extract_anomaly_codes([anomaly])
        assert codes == []

    def test_preserves_order(self):
        anomalies = [_make_anomaly(code="99213"), _make_anomaly(code="36415")]
        codes = _extract_anomaly_codes(anomalies)
        assert codes == ["99213", "36415"]

    def test_empty_list_returns_empty(self):
        codes = _extract_anomaly_codes([])
        assert codes == []


# ---- _build_subject_line() ----

class TestBuildSubjectLine:
    def test_single_price_overcharge(self):
        anomaly = _make_anomaly(anomaly_type=AnomalyType.PRICE_OVERCHARGE)
        summary = _make_summary(anomaly_count=1)
        subject = _build_subject_line([anomaly], summary)
        assert "Overcharge" in subject or "Dispute" in subject

    def test_multiple_anomalies_includes_count(self):
        anomalies = [
            _make_anomaly(code="A", anomaly_type=AnomalyType.PRICE_OVERCHARGE),
            _make_anomaly(code="B", anomaly_type=AnomalyType.DUPLICATE_CHARGE),
        ]
        summary = _make_summary(anomaly_count=2)
        subject = _build_subject_line(anomalies, summary)
        assert "2" in subject

    def test_empty_anomalies_returns_fallback(self):
        subject = _build_subject_line([], _make_summary(anomaly_count=0))
        assert len(subject) > 0

    def test_subject_is_a_string(self):
        anomaly = _make_anomaly()
        subject = _build_subject_line([anomaly], _make_summary())
        assert isinstance(subject, str)


# ---- _build_prompt() ----

class TestBuildPrompt:
    def test_includes_anomaly_count(self):
        anomaly = _make_anomaly()
        summary = _make_summary(anomaly_count=1)
        prompt = _build_prompt([anomaly], summary)
        assert "1" in prompt

    def test_includes_code_in_prompt(self):
        anomaly = _make_anomaly(code="99213")
        summary = _make_summary()
        prompt = _build_prompt([anomaly], summary)
        assert "99213" in prompt

    def test_includes_billed_amount(self):
        anomaly = _make_anomaly(billed_amount=300.0)
        summary = _make_summary()
        prompt = _build_prompt([anomaly], summary)
        assert "300" in prompt

    def test_includes_potential_overcharge_when_available(self):
        summary = _make_summary(overcharge_total=450.0)
        anomaly = _make_anomaly()
        prompt = _build_prompt([anomaly], summary)
        assert "450" in prompt

    def test_handles_none_overcharge_total(self):
        summary = _make_summary(overcharge_total=None)
        anomaly = _make_anomaly()
        prompt = _build_prompt([anomaly], summary)  # should not raise


# ---- generate() ----

class TestGenerate:
    @pytest.mark.asyncio
    async def test_returns_none_for_empty_anomalies(self):
        result = await generate([], _make_summary(anomaly_count=0))
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_dispute_letter_on_success(self):
        anomaly = _make_anomaly()
        summary = _make_summary()
        mock_letter_body = "Dear Billing Department,\n\nI am writing to dispute..."

        with patch(
            "backend.services.llm_client.complete",
            new=AsyncMock(return_value=mock_letter_body),
        ):
            result = await generate([anomaly], summary)

        assert result is not None
        assert isinstance(result, DisputeLetter)
        assert "Billing Department" in result.body

    @pytest.mark.asyncio
    async def test_letter_has_subject_line(self):
        anomaly = _make_anomaly()
        summary = _make_summary()

        with patch(
            "backend.services.llm_client.complete",
            new=AsyncMock(return_value="Letter body text."),
        ):
            result = await generate([anomaly], summary)

        assert result is not None
        assert len(result.subject_line) > 0

    @pytest.mark.asyncio
    async def test_letter_includes_anomaly_codes(self):
        anomaly = _make_anomaly(code="99213")
        summary = _make_summary()

        with patch(
            "backend.services.llm_client.complete",
            new=AsyncMock(return_value="Letter body."),
        ):
            result = await generate([anomaly], summary)

        assert result is not None
        assert "99213" in result.anomaly_codes

    @pytest.mark.asyncio
    async def test_letter_body_is_stripped(self):
        """The letter body should not have leading/trailing whitespace."""
        anomaly = _make_anomaly()
        summary = _make_summary()

        with patch(
            "backend.services.llm_client.complete",
            new=AsyncMock(return_value="  \n\nLetter text.\n\n  "),
        ):
            result = await generate([anomaly], summary)

        assert result is not None
        assert result.body == result.body.strip()

    @pytest.mark.asyncio
    async def test_llm_error_propagates(self):
        """LLM errors should propagate to the caller, not be swallowed."""
        from backend.services.llm_client import LLMError
        anomaly = _make_anomaly()
        summary = _make_summary()

        with patch(
            "backend.services.llm_client.complete",
            new=AsyncMock(side_effect=LLMError("API error")),
        ):
            with pytest.raises(LLMError):
                await generate([anomaly], summary)
