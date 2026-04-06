"""
backend/services/anomaly_detector.py
============================================================
PURPOSE:
    Orchestrates the RAG enrichment step and hands structured
    context to the ReAct agent for anomaly analysis.

    This is the bridge between the RAG layer (retriever.py) and
    the LLM layer (react_agent.py). It does NOT call the
    Anthropic API — that is exclusively react_agent.py's job.

PIPELINE POSITION:
    RedactedBill
        │
        ▼
    [THIS FILE]
        1. Validate bill has usable content
        2. Extract candidate HCPCS codes from redacted text (regex)
        3. Batch-lookup all found codes via retriever.lookup_by_code()
        4. Build rag_context dict passed to the ReAct agent
        ▼
    react_agent.analyze(bill, rag_context)
        │  (LLM call — Anthropic API)
        ▼
    list[Anomaly] + total_line_items
        │
        ▼
    _compute_summary()  →  BillSummary
        │
        ▼
    (list[Anomaly], BillSummary)  →  returned to routes.py

REACT AGENT CONTRACT:
    This file calls react_agent.analyze() with this signature:

        async def analyze(
            bill: RedactedBill,
            rag_context: dict[str, RAGResult],
        ) -> tuple[list[Anomaly], int]:

    Where:
        bill         — the redacted bill text and metadata
        rag_context  — pre-fetched RAG data keyed by HCPCS code
                       (e.g., {"99213": RAGResult(...), "J0696": RAGResult(...)})
        Returns      — (anomaly_list, total_line_items_found_on_bill)

    react_agent.py must implement this interface exactly.

SECURITY NOTE:
    All text passed to react_agent (and from there to the Anthropic API)
    comes from bill.redacted_text, which has already passed through
    pii_redactor.py and assert_no_pii_leak(). This file does not
    re-validate — it trusts the RedactedBill type contract.
============================================================
"""

import logging
import re
from typing import Optional

from backend.agent import react_agent
from backend.api.models import (
    Anomaly,
    AnomalySeverity,
    AnomalyType,
    BillSummary,
    RAGResult,
    RedactedBill,
)
from backend.rag import retriever

log = logging.getLogger(__name__)


# ============================================================
# CONSTANTS
# ============================================================

# Minimum characters in redacted_text to attempt analysis.
# WHY 50: a bill with fewer than 50 chars after redaction almost
# certainly had a failed OCR pass (blank page, unreadable scan).
# Sending near-empty text to the LLM wastes API cost and returns
# meaningless output. Fail early with a clear error.
_MIN_BILL_CHARS = 50

# Maximum unique HCPCS codes to pre-fetch from RAG per bill.
# WHY 100: real bills have 5-100 line items. More than 100 unique
# codes is either a pathological input or a very unusual hospital
# bill. We cap, warn, and proceed — the agent handles any codes
# not in the pre-fetched context via its reasoning.
_MAX_CODES_TO_FETCH = 100

# Regex pattern for HCPCS/CPT code candidates in bill text.
# HCPCS Level I  (CPT): 5 digits         e.g., "99213", "36415"
# HCPCS Level II (CMS): letter + 4 digits e.g., "J0696", "A0428"
#
# WHY \b WORD BOUNDARIES:
#   Without them, "99213" inside "Invoice#99213" would match.
#   \b anchors to the edge of the alphanumeric token.
#
# WHY IGNORECASE:
#   OCR can produce lowercase codes ("j0696"). We normalise to
#   uppercase after extraction before calling retriever.
_HCPCS_PATTERN = re.compile(
    r"\b([A-Z]\d{4}|\d{5})\b",
    re.IGNORECASE,
)

# Severity sort order for anomaly list — HIGH first, INFO last.
# WHY MODULE LEVEL: detect_anomalies() is called once per request.
# Recreating this dict on every call is wasteful; it never changes.
_SEVERITY_ORDER: dict[AnomalySeverity, int] = {
    AnomalySeverity.HIGH: 0,
    AnomalySeverity.MEDIUM: 1,
    AnomalySeverity.LOW: 2,
    AnomalySeverity.INFO: 3,
}


# ============================================================
# PUBLIC API
# ============================================================

async def detect_anomalies(
    bill: RedactedBill,
) -> tuple[list[Anomaly], BillSummary]:
    """
    Full anomaly detection pipeline for one redacted bill.

    WHAT:
        1. Validates the bill has enough content to analyse.
        2. Extracts candidate HCPCS codes from redacted text.
        3. Batch-fetches Medicare reference data from ChromaDB.
        4. Calls the ReAct agent with bill text + RAG context.
        5. Computes aggregate BillSummary from the anomaly list.
        6. Returns (anomalies, summary) to routes.py.

    WHY ASYNC:
        react_agent.analyze() is async — it awaits Anthropic API calls.
        This function must be async to await it.
        RAG lookups inside are synchronous (ChromaDB Python client),
        which briefly blocks the event loop. For MVP this is acceptable
        (~5-20ms per code × up to 100 codes = up to 2s max).
        Post-MVP: wrap RAG calls in asyncio.get_event_loop().run_in_executor()
        to release the event loop during the lookup batch.

    ARGS:
        bill: RedactedBill produced by pii_redactor.py.
              Its redacted_text is safe to send to the LLM.

    RETURNS:
        (anomalies, summary) where anomalies is sorted HIGH → MEDIUM → LOW → INFO
        and summary contains aggregate statistics for the frontend header.

    RAISES:
        ValueError: if the bill content is too short to analyse.
        Any exception from react_agent.analyze() propagates up to routes.py.
    """
    log.info(
        f"detect_anomalies: starting for '{bill.original_filename}' "
        f"({bill.char_count:,} chars, type={bill.file_type})"
    )

    _validate_bill(bill)

    # Step 1: Extract candidate HCPCS codes from the redacted text.
    candidate_codes = _extract_candidate_codes(bill.redacted_text)
    log.info(f"Extracted {len(candidate_codes)} candidate HCPCS code(s) from bill text")

    # Step 2: Batch-fetch RAG context for all found codes.
    # The agent gets this pre-loaded so it doesn't need to call
    # the retriever itself for the obvious codes on the bill.
    rag_context = _enrich_with_rag(candidate_codes)
    log.info(
        f"RAG enrichment: {len(rag_context)}/{len(candidate_codes)} codes found "
        f"in HCPCS database ({len(candidate_codes) - len(rag_context)} unknown)"
    )

    # Step 3: Run the ReAct agent.
    # The agent reads the full redacted bill text, uses the pre-fetched
    # rag_context for price comparisons, and returns structured anomalies.
    # It also tells us how many total line items it found on the bill,
    # which we need for BillSummary but can't derive from anomalies alone
    # (clean line items don't appear in the anomaly list).
    anomalies, total_line_items = await react_agent.analyze(
        bill=bill,
        rag_context=rag_context,
    )

    log.info(
        f"ReAct agent returned {len(anomalies)} anomaly(ies) "
        f"from {total_line_items} total line item(s)"
    )

    # Step 4: Sort anomalies by severity for consistent frontend rendering.
    # HIGH first — the most actionable items appear at the top of the list.
    anomalies = sorted(anomalies, key=lambda a: _SEVERITY_ORDER[a.severity])

    # Step 5: Compute summary statistics.
    summary = _compute_summary(anomalies, total_line_items)

    log.info(
        f"detect_anomalies complete: {summary.anomaly_count} anomalies "
        f"({summary.high_severity_count} HIGH, {summary.medium_severity_count} MEDIUM), "
        f"potential overcharge=${summary.potential_overcharge_total or 0:.2f}"
    )

    return anomalies, summary


# ============================================================
# PRIVATE HELPERS
# ============================================================

def _validate_bill(bill: RedactedBill) -> None:
    """
    Reject bills that are too short to contain meaningful content.

    WHAT:
        Raises ValueError if redacted_text has fewer than _MIN_BILL_CHARS
        printable characters (ignoring whitespace).

    WHY NOT JUST TRUST char_count:
        char_count counts all characters including whitespace. A page
        of newlines has a high char_count but zero useful content.
        We strip whitespace before checking.

    WHY RAISE (not return empty results):
        Returning zero anomalies for an empty bill would look like
        a clean bill — a false negative. An exception surfaces the
        problem clearly to the user: "your upload didn't contain
        readable text."
    """
    meaningful_chars = len(bill.redacted_text.strip())
    if meaningful_chars < _MIN_BILL_CHARS:
        raise ValueError(
            f"Bill text is too short to analyse ({meaningful_chars} characters "
            f"after stripping whitespace, minimum is {_MIN_BILL_CHARS}). "
            "The OCR may have failed to extract text from this file. "
            "Try uploading a clearer image."
        )


def _extract_candidate_codes(text: str) -> list[str]:
    """
    Find all HCPCS/CPT code candidates in the bill text using regex.

    WHAT:
        Returns a deduplicated, uppercased list of 5-character strings
        matching the HCPCS/CPT code format. Capped at _MAX_CODES_TO_FETCH.

    WHY REGEX (not asking the agent to find codes):
        The agent will read the full bill text and identify line items
        itself. But pre-fetching known codes with regex is cheap and
        gives the agent immediate access to Medicare prices without
        needing extra tool call rounds. The two approaches complement
        each other — regex catches obvious codes, the agent handles
        everything else (descriptions, unlisted codes, context).

    WHY DEDUPLICATE:
        A code appearing 3 times on a bill (e.g., same procedure on
        3 dates) still only needs one RAG lookup. We fetch the data
        once and the agent handles the repetition in its reasoning.

    WHY UPPERCASE:
        OCR can produce lowercase ("j0696"). The ChromaDB collection
        stores all codes as uppercase. Normalising here prevents
        retriever.lookup_by_code() from silently returning None for
        a valid code that OCR lowercased.

    RETURNS:
        List of unique HCPCS code strings, e.g., ["99213", "J0696"].
        Empty list if no codes found (the agent will handle this case
        using semantic search within its ReAct loop).
    """
    raw_matches = _HCPCS_PATTERN.findall(text)

    # Deduplicate while preserving first-seen order.
    # WHY preserve order: codes that appear earlier in the bill
    # are more likely to be primary procedure codes. If we hit
    # the cap, first-seen codes are more valuable to pre-fetch.
    seen: set[str] = set()
    unique_codes: list[str] = []
    for code in raw_matches:
        normalised = code.upper()
        if normalised not in seen:
            seen.add(normalised)
            unique_codes.append(normalised)

    if len(unique_codes) > _MAX_CODES_TO_FETCH:
        log.warning(
            f"Found {len(unique_codes)} unique code candidates — "
            f"capping at {_MAX_CODES_TO_FETCH}. Bill may have unusually many codes."
        )
        unique_codes = unique_codes[:_MAX_CODES_TO_FETCH]

    return unique_codes


def _enrich_with_rag(codes: list[str]) -> dict[str, RAGResult]:
    """
    Batch-fetch Medicare reference data for a list of HCPCS codes.

    WHAT:
        Calls retriever.lookup_by_code() for each code.
        Returns only codes that were found — unknown codes are absent
        from the returned dict (not an error; the agent handles them).

    WHY lookup_by_code (not search):
        We have exact codes from the bill text. lookup_by_code() is
        an O(1) index lookup — no embedding, no vector search.
        Using search() here would be slower and might return a
        DIFFERENT code than what's on the bill (nearest neighbour,
        not the exact match). Exact lookup is both faster and safer.

    WHY DICT (not list):
        The agent needs O(1) lookup: "what is the Medicare price
        for code X?" A list would require a linear scan per question.
        A dict keyed by code makes the agent's reasoning simpler and
        the prompt context more structured.

    WHY VALIDATE WITH RAGResult MODEL:
        retriever.py returns plain dicts. We parse each dict into
        a RAGResult Pydantic model to catch any schema drift between
        retriever.py and models.py at the earliest possible point —
        here, before the agent sees the data.

    ARGS:
        codes: List of normalised HCPCS code strings.

    RETURNS:
        Dict mapping code → RAGResult for every code found in ChromaDB.
        Codes not in the database are silently omitted.
    """
    if not codes:
        return {}

    rag_context: dict[str, RAGResult] = {}

    # POST-MVP: wrap this loop in asyncio.get_event_loop().run_in_executor()
    # to run ChromaDB lookups in a thread pool without blocking the event loop.
    # At MVP scale (~5-100 codes × ~5-20ms each = up to 2s max) this is
    # acceptable. At production scale with concurrent users it becomes
    # a bottleneck — each blocked lookup prevents other requests from
    # being handled by the async event loop.
    # See: https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.loop.run_in_executor
    for code in codes:
        try:
            result_dict = retriever.lookup_by_code(code)
        except Exception as exc:
            # A ChromaDB error on one code should not abort the whole bill.
            # Log and continue — the agent will note the missing data.
            log.warning(f"RAG lookup failed for code '{code}': {exc}")
            continue

        if result_dict is None:
            # Code not in the HCPCS database — unknown or unlisted.
            log.debug(f"Code '{code}' not found in HCPCS database")
            continue

        # Parse into typed model to catch schema drift early.
        try:
            rag_context[code] = RAGResult(**result_dict)
        except Exception as exc:
            log.warning(
                f"RAGResult parse failed for code '{code}': {exc}. "
                "Check that retriever.py and models.RAGResult are in sync."
            )
            continue

    return rag_context


def _compute_summary(
    anomalies: list[Anomaly],
    total_line_items: int,
) -> BillSummary:
    """
    Compute aggregate BillSummary statistics from the anomaly list.

    WHAT:
        Counts severities, computes potential overcharge total where
        price data is available, and assembles a BillSummary.

    WHY total_billed_amount IS ALWAYS None HERE:
        The anomaly list contains only flagged line items. Clean line
        items (no anomaly found) do not appear in the list. Summing
        only anomalous items' amounts would be a partial, misleading
        total. Setting it to None is honest — the frontend renders
        "N/A" rather than showing a number that looks like the full
        bill total but isn't.
        Post-MVP: have the agent return all line items (not just
        anomalous ones), then sum them here.

    WHY potential_overcharge_total CAN BE COMPUTED:
        Unlike total_billed_amount, this field is specifically about
        anomalous charges. We can accurately compute it from the
        PRICE_OVERCHARGE anomalies in the list where both
        billed_amount and medicare_reference_price are known.

    ARGS:
        anomalies:        Sorted anomaly list from the ReAct agent.
        total_line_items: Total charge lines found on the bill,
                          as reported by the agent (includes clean lines).
    """
    high_count = sum(1 for a in anomalies if a.severity == AnomalySeverity.HIGH)
    medium_count = sum(1 for a in anomalies if a.severity == AnomalySeverity.MEDIUM)

    # Potential overcharge: sum (billed - medicare_ref) for PRICE_OVERCHARGE
    # anomalies where we have both values. This gives the patient a dollar
    # figure to anchor their dispute letter ("you may have been overcharged
    # by approximately $X").
    overcharge_deltas: list[float] = []
    for anomaly in anomalies:
        if anomaly.anomaly_type != AnomalyType.PRICE_OVERCHARGE:
            continue
        billed = anomaly.line_item.billed_amount
        ratio = anomaly.overcharge_ratio
        # Derive medicare_ref from the ratio: overcharge_ratio = billed / medicare_ref
        # so medicare_ref = billed / ratio. Both fields exist on Anomaly.
        if billed is not None and ratio is not None and ratio > 0:
            medicare_ref = billed / ratio
            delta = billed - medicare_ref
            if delta > 0:
                overcharge_deltas.append(delta)

    potential_overcharge_total: Optional[float] = (
        round(sum(overcharge_deltas), 2) if overcharge_deltas else None
    )

    return BillSummary(
        total_line_items=total_line_items,
        total_billed_amount=None,   # see docstring — partial sum would be misleading
        anomaly_count=len(anomalies),
        high_severity_count=high_count,
        medium_severity_count=medium_count,
        potential_overcharge_total=potential_overcharge_total,
    )
