"""
tests/test_react_agent.py
Unit tests for backend/agent/react_agent.py — ReAct agent loop and helpers.
Anthropic API is mocked via llm_client.complete_with_tools.
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
    RAGResult,
    RedactedBill,
)
from backend.agent.react_agent import (
    _build_tools,
    _build_user_message,
    _execute_search_hcpcs,
    _handle_report_anomalies,
    _parse_anomaly,
    analyze,
)


# ---- Fixtures ----

@pytest.fixture
def redacted_bill():
    return RedactedBill(
        redacted_text="Service 99213 Office visit $150",
        original_filename="bill.pdf",
        file_type="pdf",
        char_count=40,
    )


@pytest.fixture
def rag_context():
    return {
        "99213": RAGResult(
            code="99213",
            long_description="Office or other outpatient visit",
            short_description="Office visit est",
            medicare_reference_price=89.03,
            total_rvu=2.72,
            has_price_data=True,
        )
    }


# ---- _build_tools() ----

class TestBuildTools:
    def test_returns_two_tools(self):
        tools = _build_tools()
        assert len(tools) == 2

    def test_tool_names(self):
        tools = _build_tools()
        names = {t["name"] for t in tools}
        assert "search_hcpcs" in names
        assert "report_anomalies" in names

    def test_report_anomalies_requires_total_line_items(self):
        tools = _build_tools()
        report_tool = next(t for t in tools if t["name"] == "report_anomalies")
        required = report_tool["input_schema"]["required"]
        assert "total_line_items" in required
        assert "anomalies" in required

    def test_search_hcpcs_requires_query(self):
        tools = _build_tools()
        search_tool = next(t for t in tools if t["name"] == "search_hcpcs")
        required = search_tool["input_schema"]["required"]
        assert "query" in required


# ---- _build_user_message() ----

class TestBuildUserMessage:
    def test_includes_bill_text(self, redacted_bill, rag_context):
        msg = _build_user_message(redacted_bill, rag_context)
        assert redacted_bill.redacted_text in msg

    def test_includes_filename(self, redacted_bill, rag_context):
        msg = _build_user_message(redacted_bill, rag_context)
        assert redacted_bill.original_filename in msg

    def test_includes_rag_context_codes(self, redacted_bill, rag_context):
        msg = _build_user_message(redacted_bill, rag_context)
        assert "99213" in msg

    def test_no_codes_message_when_rag_empty(self, redacted_bill):
        msg = _build_user_message(redacted_bill, {})
        assert "No HCPCS codes were pre-fetched" in msg


# ---- _execute_search_hcpcs() ----

class TestExecuteSearchHcpcs:
    def test_returns_error_on_empty_query(self):
        result = _execute_search_hcpcs({"query": ""})
        assert "Error" in result

    def test_returns_formatted_table_on_success(self):
        mock_results = [{
            "code": "99213",
            "short_description": "Office visit",
            "medicare_reference_price": 89.03,
            "has_price_data": True,
            "similarity_score": 0.9,
        }]
        with patch("backend.rag.retriever.search", return_value=mock_results):
            result = _execute_search_hcpcs({"query": "office visit"})
        assert "99213" in result
        assert "$89.03" in result

    def test_returns_no_results_message_when_empty(self):
        with patch("backend.rag.retriever.search", return_value=[]):
            result = _execute_search_hcpcs({"query": "unknown procedure"})
        assert "No results found" in result

    def test_returns_error_string_when_search_raises(self):
        with patch("backend.rag.retriever.search", side_effect=Exception("ChromaDB down")):
            result = _execute_search_hcpcs({"query": "test"})
        assert "Search failed" in result

    def test_caps_n_results_at_10(self):
        """n_results from tool input should be capped at 10."""
        with patch("backend.rag.retriever.search", return_value=[]) as mock_search:
            _execute_search_hcpcs({"query": "test", "n_results": 50})
        mock_search.assert_called_once()
        call_kwargs = mock_search.call_args
        assert call_kwargs[1]["n_results"] <= 10


# ---- _parse_anomaly() ----

class TestParseAnomaly:
    def _valid_raw(self):
        return {
            "code": "99213",
            "description": "Office visit",
            "quantity": 1,
            "billed_amount": 300.0,
            "anomaly_type": "price_overcharge",
            "severity": "high",
            "explanation": "Billed 3x Medicare rate.",
            "medicare_reference_price": 100.0,
            "overcharge_ratio": 3.0,
            "suggested_action": "Request itemized bill.",
        }

    def test_valid_anomaly_parsed_successfully(self):
        anomaly = _parse_anomaly(self._valid_raw())
        assert anomaly is not None
        assert anomaly.anomaly_type == AnomalyType.PRICE_OVERCHARGE
        assert anomaly.severity == AnomalySeverity.HIGH

    def test_missing_description_returns_none(self):
        data = self._valid_raw()
        del data["description"]
        result = _parse_anomaly(data)
        assert result is None

    def test_missing_explanation_returns_none(self):
        data = self._valid_raw()
        del data["explanation"]
        result = _parse_anomaly(data)
        assert result is None

    def test_invalid_anomaly_type_returns_none(self):
        data = self._valid_raw()
        data["anomaly_type"] = "not_a_real_type"
        result = _parse_anomaly(data)
        assert result is None

    def test_optional_fields_can_be_omitted(self):
        data = {
            "description": "Lab test",
            "anomaly_type": "unknown_code",
            "severity": "info",
            "explanation": "Code not in HCPCS database.",
            "suggested_action": "Ask provider.",
        }
        result = _parse_anomaly(data)
        assert result is not None
        assert result.line_item.code is None
        assert result.line_item.billed_amount is None


# ---- _handle_report_anomalies() ----

class TestHandleReportAnomalies:
    def test_returns_empty_list_for_empty_anomalies(self):
        anomalies, total = _handle_report_anomalies({
            "total_line_items": 5,
            "anomalies": [],
        })
        assert anomalies == []
        assert total == 5

    def test_parses_valid_anomalies(self):
        raw = {
            "total_line_items": 3,
            "anomalies": [{
                "code": "99213",
                "description": "Office visit",
                "anomaly_type": "price_overcharge",
                "severity": "high",
                "explanation": "Overpriced.",
                "suggested_action": "Dispute it.",
            }],
        }
        anomalies, total = _handle_report_anomalies(raw)
        assert len(anomalies) == 1
        assert total == 3

    def test_skips_malformed_anomaly_keeps_valid_ones(self):
        raw = {
            "total_line_items": 5,
            "anomalies": [
                {
                    "description": "Valid charge",
                    "anomaly_type": "duplicate_charge",
                    "severity": "medium",
                    "explanation": "Duplicate.",
                    "suggested_action": "Review.",
                },
                {
                    # Missing required fields — should be skipped
                    "anomaly_type": "unknown_code",
                },
            ],
        }
        anomalies, total = _handle_report_anomalies(raw)
        assert len(anomalies) == 1  # one valid, one skipped
        assert total == 5

    def test_defaults_total_line_items_to_zero(self):
        _, total = _handle_report_anomalies({"anomalies": []})
        assert total == 0


# ---- analyze() (the full ReAct loop) ----

class TestAnalyze:
    """Tests for the async analyze() entry point."""

    def _make_tool_use_response(self, tool_name, tool_input, tool_id="tu_123"):
        """Build a mock Message with stop_reason='tool_use'."""
        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.name = tool_name
        tool_block.input = tool_input
        tool_block.id = tool_id

        response = MagicMock()
        response.stop_reason = "tool_use"
        response.content = [tool_block]
        return response

    def _make_end_turn_response(self):
        """Build a mock Message with stop_reason='end_turn'."""
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Analysis complete."

        response = MagicMock()
        response.stop_reason = "end_turn"
        response.content = [text_block]
        return response

    @pytest.mark.asyncio
    async def test_agent_calls_report_anomalies_and_returns_results(
        self, redacted_bill, rag_context
    ):
        """Happy path: agent calls report_anomalies on the first turn."""
        report_input = {
            "total_line_items": 2,
            "anomalies": [{
                "code": "99213",
                "description": "Office visit",
                "anomaly_type": "price_overcharge",
                "severity": "high",
                "explanation": "Billed 3x Medicare rate.",
                "suggested_action": "Request itemized bill.",
                "billed_amount": 300.0,
                "medicare_reference_price": 100.0,
                "overcharge_ratio": 3.0,
            }],
        }
        mock_response = self._make_tool_use_response("report_anomalies", report_input)

        with patch("backend.services.llm_client.complete_with_tools", new=AsyncMock(return_value=mock_response)):
            anomalies, total = await analyze(redacted_bill, rag_context)

        assert total == 2
        assert len(anomalies) == 1
        assert anomalies[0].anomaly_type == AnomalyType.PRICE_OVERCHARGE

    @pytest.mark.asyncio
    async def test_agent_search_then_report(self, redacted_bill, rag_context):
        """Agent calls search_hcpcs first, then report_anomalies."""
        search_response = self._make_tool_use_response(
            "search_hcpcs", {"query": "office visit"}, tool_id="tu_search"
        )
        report_response = self._make_tool_use_response(
            "report_anomalies",
            {"total_line_items": 1, "anomalies": []},
            tool_id="tu_report",
        )

        call_count = 0

        async def mock_complete_with_tools(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return search_response
            return report_response

        with patch("backend.services.llm_client.complete_with_tools", side_effect=mock_complete_with_tools):
            with patch("backend.rag.retriever.search", return_value=[]):
                anomalies, total = await analyze(redacted_bill, rag_context)

        assert call_count == 2
        assert anomalies == []
        assert total == 1

    @pytest.mark.asyncio
    async def test_max_iterations_raises_runtime_error(self, redacted_bill, rag_context):
        """If agent never calls report_anomalies, RuntimeError is raised after max turns."""
        # Always return a search call — never a report call
        search_response = self._make_tool_use_response(
            "search_hcpcs", {"query": "test"}
        )

        with patch("backend.services.llm_client.complete_with_tools", new=AsyncMock(return_value=search_response)):
            with patch("backend.rag.retriever.search", return_value=[]):
                with pytest.raises(RuntimeError, match="did not call report_anomalies"):
                    await analyze(redacted_bill, rag_context)

    @pytest.mark.asyncio
    async def test_end_turn_without_report_returns_empty(self, redacted_bill, rag_context):
        """If agent ends turn without calling report_anomalies, returns empty list."""
        end_turn_response = self._make_end_turn_response()

        with patch("backend.services.llm_client.complete_with_tools", new=AsyncMock(return_value=end_turn_response)):
            anomalies, total = await analyze(redacted_bill, rag_context)

        assert anomalies == []
        assert total == 0
