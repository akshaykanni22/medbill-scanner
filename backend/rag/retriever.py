"""
backend/rag/retriever.py
============================================================
PURPOSE:
    Provides semantic search and exact lookup over the ChromaDB
    collection of HCPCS codes built by ingest.py.

    Called at request time — once per user-uploaded bill —
    by the anomaly detector and ReAct agent.

PUBLIC API:
    search(query, n_results)  — semantic search by natural language or code
    lookup_by_code(code)      — exact HCPCS code lookup, no embedding needed

WHAT CALLERS RECEIVE (per result):
    {
        "code":                     str    "99213"
        "long_description":         str    "Office or other outpatient visit..."
        "short_description":        str    "Office/outpatient visit, est"
        "medicare_reference_price": float  89.03
        "total_rvu":                float  2.72
        "has_price_data":           bool   True
        "similarity_score":         float  0.87    # search() only, omitted for lookup_by_code
    }

DESIGN: SINGLETON COLLECTION
    Loading sentence-transformers takes 1-2 seconds on first call.
    We cache the ChromaDB collection object at module level after
    first use. Subsequent requests pay only the query cost (~50ms).
    If ChromaDB is unreachable at startup, the first call raises —
    the container will restart via docker-compose restart: on-failure.

SECURITY NOTES:
    - Query text comes from redacted bill content — PII is already
      stripped before anomaly_detector.py calls this module.
    - No patient data is stored here. ChromaDB holds CMS public data only.
    - Queries never leave Docker (embeddings computed locally).
============================================================
"""

import logging
import os
from typing import Optional

import chromadb
from chromadb.api.models.Collection import Collection

# WHY IMPORT FROM INGEST (not re-declare constants):
#   COLLECTION_NAME and EMBEDDING_MODEL MUST be identical between
#   ingest time and query time or embeddings will not match.
#   Importing from ingest.py makes that contract enforced by Python
#   rather than relying on a developer keeping two files in sync.
from backend.rag.ingest import (
    COLLECTION_NAME,
    EMBEDDING_MODEL,
    get_chroma_client,
    get_embedding_fn,
)

log = logging.getLogger(__name__)

# Default number of results to return per search.
# WHY env var: docker-compose.yml sets RAG_TOP_K=5. Configurable
# without code changes — useful for tuning agent context window size.
_RAG_TOP_K = int(os.getenv("RAG_TOP_K", "5"))

# ---- Singleton state ----
# WHY module-level (not class instance):
#   retriever.py is imported once and lives for the lifetime of the
#   process. A module-level variable is the simplest correct singleton.
#   No locking needed — uvicorn's async event loop means only one
#   coroutine runs Python at a time (no true parallelism for this state).
_collection: Optional[Collection] = None


def _get_collection() -> Collection:
    """
    Return the cached ChromaDB collection, connecting on first call.

    WHAT:
        Lazy-initializes the module-level _collection singleton.
        Subsequent calls return the cached object immediately.

    WHY LAZY (not at import time):
        The backend container starts before ChromaDB is healthy
        (healthcheck takes up to 50s). Importing retriever.py at
        startup would fail. Lazy init defers the connection until
        the first actual query, by which time ChromaDB is up.

    RAISES:
        chromadb.errors.ChromaError: if ChromaDB is unreachable
        ValueError: if the collection does not exist (ingest hasn't run)
    """
    global _collection
    if _collection is not None:
        return _collection

    client = get_chroma_client()
    embedding_fn = get_embedding_fn()

    # get_collection (not get_or_create_collection) is intentional here.
    # WHY: If the collection doesn't exist, ingest.py hasn't run yet.
    # Silently creating an empty collection would give wrong search results
    # with no error. We want a loud failure that tells the operator to
    # run ingest first.
    try:
        _collection = client.get_collection(
            name=COLLECTION_NAME,
            embedding_function=embedding_fn,
        )
    except Exception as exc:
        raise ValueError(
            f"ChromaDB collection '{COLLECTION_NAME}' not found. "
            "Run ingest.py first: "
            "docker-compose run backend python -m backend.rag.ingest"
        ) from exc

    count = _collection.count()
    log.info(
        f"RAG retriever ready: collection '{COLLECTION_NAME}' "
        f"with {count:,} HCPCS codes (model: {EMBEDDING_MODEL})"
    )
    return _collection


# ---- Result formatting ----

def _format_result(
    document: str,
    metadata: dict,
    distance: Optional[float] = None,
) -> dict:
    """
    Convert a raw ChromaDB result into the clean dict callers expect.

    WHAT:
        Extracts long_description from the document text (which is stored
        as "{code}: {long_description}"), combines with all metadata fields,
        and optionally adds similarity_score.

    WHY PARSE LONG DESCRIPTION FROM DOCUMENT TEXT:
        We chose not to store long_description in ChromaDB metadata
        (only short_description is there) to avoid data duplication —
        the full description IS the document text. Splitting on ": "
        with maxsplit=1 recovers it cleanly.

    WHY similarity_score = 1 - distance:
        ChromaDB with hnsw:space=cosine returns cosine distance in [0, 1]
        for typical sentence embeddings (which produce non-negative vectors).
        0 = identical, 1 = completely unrelated.
        1 - distance gives an intuitive score where 1.0 = perfect match.
        We round to 4 decimal places to avoid floating point noise in logs.
    """
    # Parse long_description from document text.
    # Document format: "99213: Office or other outpatient visit..."
    # For the rare case where no long description was available,
    # the document is just the code — handle both shapes safely.
    parts = document.split(": ", maxsplit=1)
    long_description = parts[1] if len(parts) == 2 else ""

    result = {
        "code": metadata.get("code", ""),
        "long_description": long_description,
        "short_description": metadata.get("short_description", ""),
        "medicare_reference_price": float(metadata.get("medicare_reference_price", 0.0)),
        "total_rvu": float(metadata.get("total_rvu", 0.0)),
        "has_price_data": bool(metadata.get("has_price_data", False)),
    }

    if distance is not None:
        result["similarity_score"] = round(1.0 - distance, 4)

    return result


# ---- Public API ----

def search(query: str, n_results: int = _RAG_TOP_K) -> list[dict]:
    """
    Semantic search over HCPCS codes by natural language or code string.

    WHAT:
        Embeds the query locally using the same sentence-transformers model
        used at ingest time, then asks ChromaDB for the top-k nearest
        neighbors by cosine similarity.

    WHEN TO USE:
        - Agent is exploring a bill and wants all procedures matching
          "anesthesia" or "blood test" to find anomalies by category.
        - A code on the bill is unknown and we need the closest match.
        - The bill lists a description but no code.

    WHY NOT ALWAYS USE lookup_by_code:
        Many bills have incomplete or non-standard code descriptions.
        Semantic search catches "IV therapy" → J-code matches even
        when the exact code is absent or mistyped on the bill.

    ARGS:
        query:     Natural language query or HCPCS code string.
                   Must be non-empty. Will be stripped of whitespace.
        n_results: Number of results to return. Defaults to RAG_TOP_K env var.
                   Capped at 20 to prevent the agent context window blowing up.

    RETURNS:
        List of result dicts sorted by similarity_score descending.
        Empty list if the collection is empty (should not happen post-ingest).

    RAISES:
        ValueError: if query is empty
        chromadb.errors.ChromaError: if ChromaDB is unreachable
    """
    query = query.strip()
    if not query:
        raise ValueError("search() query must be a non-empty string")

    # Cap n_results defensively — ChromaDB will error if n_results
    # exceeds the collection size, and the agent doesn't need more than 20.
    n_results = min(n_results, 20)

    collection = _get_collection()

    log.debug(f"RAG search: '{query}' (n_results={n_results})")

    # ChromaDB query() embeds the query text using the same embedding_fn
    # that was passed to get_collection() above, then does ANN search.
    # include= controls what comes back — we need documents for the
    # long description text, metadatas for price data, distances for scoring.
    results = collection.query(
        query_texts=[query],
        n_results=n_results,
        include=["documents", "metadatas", "distances"],
    )

    # ChromaDB wraps results in an outer list (one list per query text).
    # We always send exactly one query, so index [0] is our results.
    documents = results["documents"][0]
    metadatas = results["metadatas"][0]
    distances = results["distances"][0]

    formatted = [
        _format_result(doc, meta, dist)
        for doc, meta, dist in zip(documents, metadatas, distances)
    ]

    log.debug(
        f"RAG search complete: {len(formatted)} results, "
        f"top score={formatted[0]['similarity_score'] if formatted else 'n/a'}"
    )
    return formatted


def lookup_by_code(code: str) -> Optional[dict]:
    """
    Exact HCPCS code lookup — no embedding, no semantic search.

    WHAT:
        Calls ChromaDB's get() by document ID (which equals the HCPCS code).
        This is an index lookup, not a vector search — effectively O(1).

    WHEN TO USE:
        The anomaly detector has already identified a specific HCPCS code
        from the bill (e.g., "99213") and needs the Medicare reference price
        for that exact code. There is no reason to embed and search here —
        we know the exact ID.

    WHY SEPARATE FROM search():
        1. Speed: no embedding computation (~50ms saved per lookup).
        2. Precision: search() returns nearest neighbors, which may not
           be the exact code. If "99213" is on the bill, we want "99213",
           not whatever is semantically closest.
        3. Clarity: the caller's intent is different — "I have a code,
           give me its data" vs "I have a description, find related codes."

    ARGS:
        code: HCPCS code string, e.g., "99213". Case-insensitive.

    RETURNS:
        Result dict (without similarity_score) if the code exists.
        None if the code is not in the collection (unknown or unlisted code).

    RAISES:
        chromadb.errors.ChromaError: if ChromaDB is unreachable
    """
    code = code.strip().upper()
    if not code:
        return None

    collection = _get_collection()

    log.debug(f"RAG exact lookup: '{code}'")

    # get() by ID — no vector search, just index lookup.
    # include= matches search() for consistent result shape (minus distance).
    result = collection.get(
        ids=[code],
        include=["documents", "metadatas"],
    )

    if not result["ids"]:
        log.debug(f"RAG lookup: code '{code}' not found in collection")
        return None

    doc = result["documents"][0]
    meta = result["metadatas"][0]

    log.debug(f"RAG lookup: found '{code}'")
    # No distance for an exact lookup — similarity_score not included.
    return _format_result(doc, meta, distance=None)


def get_collection_size() -> int:
    """
    Return the number of HCPCS codes loaded in the ChromaDB collection.

    WHAT:
        Used by the /health endpoint to detect whether ingest.py has run.
        A size of 0 means the collection is empty — the agent cannot
        do price comparisons.

    RAISES:
        chromadb.errors.ChromaError: if ChromaDB is unreachable.
    """
    collection = _get_collection()
    return collection.count()


def reset_singleton() -> None:
    """
    Clear the cached collection singleton.

    WHAT: Forces the next call to _get_collection() to reconnect.

    WHEN TO USE:
        - Tests that need a clean state between test cases.
        - Not for production use — the singleton is intentional there.
    """
    global _collection
    _collection = None
    log.debug("RAG retriever singleton cleared")
