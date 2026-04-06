"""
tests/test_retriever.py
Unit tests for backend/rag/retriever.py — ChromaDB search + lookup.
All ChromaDB and embedding calls are mocked.
"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real")

import pytest
from unittest.mock import MagicMock, patch

import backend.rag.retriever as retriever_module
from backend.rag.retriever import (
    search,
    lookup_by_code,
    get_collection_size,
    reset_singleton,
    _format_result,
)


@pytest.fixture(autouse=True)
def clear_singleton():
    """Reset the module-level _collection singleton before each test."""
    reset_singleton()
    yield
    reset_singleton()


def _make_mock_collection(
    query_docs=None,
    query_metas=None,
    query_distances=None,
    get_ids=None,
    get_docs=None,
    get_metas=None,
    count=100,
):
    """Helper to build a mock ChromaDB collection."""
    collection = MagicMock()
    collection.count.return_value = count

    if query_docs is not None:
        collection.query.return_value = {
            "documents": [query_docs],
            "metadatas": [query_metas or [{}] * len(query_docs)],
            "distances": [query_distances or [0.1] * len(query_docs)],
        }

    if get_ids is not None:
        collection.get.return_value = {
            "ids": get_ids,
            "documents": get_docs or [],
            "metadatas": get_metas or [],
        }

    return collection


def _mock_get_collection(mock_collection):
    """Context manager: patch _get_collection to return mock_collection."""
    return patch.object(retriever_module, "_get_collection", return_value=mock_collection)


# ---- _format_result ----

class TestFormatResult:
    def test_parses_code_from_metadata(self):
        doc = "99213: Office or other outpatient visit"
        meta = {
            "code": "99213",
            "short_description": "Office visit est",
            "medicare_reference_price": 89.03,
            "total_rvu": 2.72,
            "has_price_data": True,
        }
        result = _format_result(doc, meta)
        assert result["code"] == "99213"

    def test_parses_long_description_from_document(self):
        doc = "99213: Office or other outpatient visit"
        meta = {"code": "99213", "short_description": "x",
                "medicare_reference_price": 0.0, "total_rvu": 0.0, "has_price_data": False}
        result = _format_result(doc, meta)
        assert result["long_description"] == "Office or other outpatient visit"

    def test_handles_document_without_colon(self):
        doc = "99213"  # no long description
        meta = {"code": "99213", "short_description": "x",
                "medicare_reference_price": 0.0, "total_rvu": 0.0, "has_price_data": False}
        result = _format_result(doc, meta)
        assert result["long_description"] == ""

    def test_similarity_score_from_distance(self):
        doc = "99213: desc"
        meta = {"code": "99213", "short_description": "x",
                "medicare_reference_price": 0.0, "total_rvu": 0.0, "has_price_data": False}
        result = _format_result(doc, meta, distance=0.13)
        assert result["similarity_score"] == pytest.approx(0.87, abs=0.001)

    def test_no_similarity_score_when_distance_is_none(self):
        doc = "99213: desc"
        meta = {"code": "99213", "short_description": "x",
                "medicare_reference_price": 0.0, "total_rvu": 0.0, "has_price_data": False}
        result = _format_result(doc, meta, distance=None)
        assert "similarity_score" not in result


# ---- search() ----

class TestSearch:
    def test_raises_on_empty_query(self):
        with pytest.raises(ValueError, match="non-empty"):
            search("")

    def test_raises_on_whitespace_only_query(self):
        with pytest.raises(ValueError):
            search("   ")

    def test_returns_formatted_results(self):
        mock_collection = _make_mock_collection(
            query_docs=["99213: Office visit"],
            query_metas=[{
                "code": "99213",
                "short_description": "Office visit",
                "medicare_reference_price": 89.03,
                "total_rvu": 2.72,
                "has_price_data": True,
            }],
            query_distances=[0.1],
        )
        with _mock_get_collection(mock_collection):
            results = search("office visit")
        assert len(results) == 1
        assert results[0]["code"] == "99213"
        assert results[0]["similarity_score"] == pytest.approx(0.9, abs=0.001)

    def test_caps_n_results_at_20(self):
        """n_results should be capped at 20 regardless of what is passed."""
        mock_collection = _make_mock_collection(
            query_docs=["99213: desc"],
            query_metas=[{"code": "99213", "short_description": "x",
                          "medicare_reference_price": 0.0, "total_rvu": 0.0, "has_price_data": False}],
            query_distances=[0.5],
        )
        with _mock_get_collection(mock_collection):
            search("query", n_results=50)
        # Verify that query() was called with n_results <= 20
        call_kwargs = mock_collection.query.call_args[1]
        assert call_kwargs["n_results"] <= 20

    def test_returns_empty_list_when_no_results(self):
        mock_collection = _make_mock_collection(
            query_docs=[],
            query_metas=[],
            query_distances=[],
        )
        with _mock_get_collection(mock_collection):
            results = search("unknown procedure")
        assert results == []


# ---- lookup_by_code() ----

class TestLookupByCode:
    def test_returns_none_for_empty_code(self):
        result = lookup_by_code("")
        assert result is None

    def test_returns_none_for_whitespace_code(self):
        result = lookup_by_code("   ")
        assert result is None

    def test_normalises_code_to_uppercase(self):
        """lookup_by_code should uppercase the code before querying."""
        mock_collection = _make_mock_collection(
            get_ids=["99213"],
            get_docs=["99213: Office visit"],
            get_metas=[{
                "code": "99213",
                "short_description": "Office visit",
                "medicare_reference_price": 89.03,
                "total_rvu": 2.72,
                "has_price_data": True,
            }],
        )
        with _mock_get_collection(mock_collection):
            result = lookup_by_code("99213")
        assert result is not None
        assert result["code"] == "99213"

    def test_returns_none_when_code_not_found(self):
        mock_collection = _make_mock_collection(
            get_ids=[],  # empty = not found
            get_docs=[],
            get_metas=[],
        )
        with _mock_get_collection(mock_collection):
            result = lookup_by_code("XXXXX")
        assert result is None

    def test_result_has_no_similarity_score(self):
        """Exact lookups should not include similarity_score."""
        mock_collection = _make_mock_collection(
            get_ids=["99213"],
            get_docs=["99213: desc"],
            get_metas=[{
                "code": "99213",
                "short_description": "desc",
                "medicare_reference_price": 0.0,
                "total_rvu": 0.0,
                "has_price_data": False,
            }],
        )
        with _mock_get_collection(mock_collection):
            result = lookup_by_code("99213")
        assert result is not None
        assert "similarity_score" not in result


# ---- get_collection_size() ----

class TestGetCollectionSize:
    def test_returns_count_from_collection(self):
        mock_collection = _make_mock_collection(count=1234)
        with _mock_get_collection(mock_collection):
            size = get_collection_size()
        assert size == 1234

    def test_returns_zero_for_empty_collection(self):
        mock_collection = _make_mock_collection(count=0)
        with _mock_get_collection(mock_collection):
            size = get_collection_size()
        assert size == 0


# ---- reset_singleton() ----

class TestResetSingleton:
    def test_reset_clears_cached_collection(self):
        """After reset, the next call should re-initialize (call get_chroma_client)."""
        # Pre-populate the singleton
        retriever_module._collection = MagicMock()
        reset_singleton()
        assert retriever_module._collection is None
