"""
backend/agent/react_agent.py
============================================================
PURPOSE:
    The ReAct (Reasoning + Acting) agent that analyses a redacted
    medical bill and returns a structured list of billing anomalies.

    This is the ONLY file in the codebase that drives a multi-turn
    LLM loop. All other LLM calls are single-turn via llm_client.complete().

PIPELINE POSITION:
    anomaly_detector.py calls:
        analyze(bill, rag_context) → (list[Anomaly], total_line_items)

    This file calls llm_client.complete_with_tools() in a loop until
    the agent invokes the report_anomalies tool (signalling it is done).

REACT PATTERN USED HERE:
    Classic ReAct loop:
        1. Send bill text + pre-fetched RAG context to the model.
        2. Model reasons about the bill, calls search_hcpcs for any
           codes not already in the pre-fetched context.
        3. We execute the tool call, append the result, repeat.
        4. When the model has enough data, it calls report_anomalies
           with a structured anomaly list — that ends the loop.

    WHY TOOL-BASED OUTPUT (not parsing free text):
        Parsing free-form LLM text into Anomaly models is fragile —
        format drift across Claude versions breaks the parser.
        Using a tool with a JSON schema forces the model to produce
        machine-readable output validated at the schema level.
        We get structured output without post-processing heuristics.

TOOLS EXPOSED TO THE AGENT:
    search_hcpcs(query, n_results)
        — Semantic search over the HCPCS ChromaDB collection.
        — Used when the agent encounters a code or description not
          already in the pre-fetched rag_context.

    report_anomalies(anomalies, total_line_items)
        — Structured output tool. Calling this ends the ReAct loop.
        — The agent MUST call this to return results, even if the
          anomaly list is empty (clean bill).

LOOP SAFETY:
    _MAX_REACT_TURNS caps the loop at 10 turns. If the model keeps
    calling search_hcpcs without ever calling report_anomalies,
    we raise after the cap. This should never happen with a correctly
    prompted model, but defensive coding prevents infinite billing.

SECURITY NOTE:
    All text passed to the Anthropic API in this file comes from
    bill.redacted_text (which has already passed assert_no_pii_leak()
    in pii_redactor.py) or from ChromaDB results (public CMS data).
    No raw patient data ever reaches this file.
============================================================
"""

import json
import logging
from typing import Any, Optional

import anthropic

from backend.api.models import (
    Anomaly,
    AnomalySeverity,
    AnomalyType,
    BillLineItem,
    RAGResult,
    RedactedBill,
)
from backend.rag import retriever
from backend.services import llm_client

log = logging.getLogger(__name__)

# ============================================================
# CONSTANTS
# ============================================================

# Maximum ReAct turns before we abort.
# WHY 10: a complex bill with many unknown codes might need several
# search_hcpcs calls, but 10 turns is already very generous. A model
# stuck in a search loop (not calling report_anomalies) would burn
# API cost without producing output. We fail loudly after the cap.
_MAX_REACT_TURNS = 20

# Tool names — defined as constants so changes propagate everywhere.
_TOOL_SEARCH = "search_hcpcs"
_TOOL_REPORT = "report_anomalies"


# ============================================================
# TOOL DEFINITIONS (Anthropic tool schema format)
# ============================================================

def _build_tools() -> list[dict]:
    """
    Return the Anthropic tool schema for both agent tools.

    WHAT:
        Defines the two tools the agent can call:
        1. search_hcpcs   — semantic HCPCS lookup
        2. report_anomalies — structured output / loop terminator

    WHY FUNCTION (not module-level constant):
        Python evaluates default arguments and module-level assignments
        at import time. Keeping these as a function makes it explicit
        that the schemas are constructed freshly — important if we ever
        make them dynamic. The cost is negligible (called once per request).
    """
    return [
        {
            "name": _TOOL_SEARCH,
            "description": (
                "Search the HCPCS/CPT code database for Medicare reference prices "
                "and procedure descriptions. Use this when you encounter a code or "
                "procedure description on the bill that is NOT already in the "
                "pre-fetched context below. Returns the top matching codes with "
                "Medicare reference prices and descriptions."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "The HCPCS/CPT code (e.g., '99213') or a natural language "
                            "description of the procedure (e.g., 'office visit established patient'). "
                            "Do NOT include dollar amounts or dates."
                        ),
                    },
                    "n_results": {
                        "type": "integer",
                        "description": "Number of results to return. Default 5, max 10.",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 10,
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": _TOOL_REPORT,
            "description": (
                "Submit your final anomaly findings. Call this ONCE when you have "
                "finished analysing all line items on the bill. You MUST call this "
                "even if no anomalies were found (pass an empty anomalies list). "
                "This ends the analysis."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "total_line_items": {
                        "type": "integer",
                        "minimum": 0,
                        "description": (
                            "Total number of charge line items found on the bill, "
                            "including clean items with no anomaly."
                        ),
                    },
                    "anomalies": {
                        "type": "array",
                        "description": "List of billing anomalies found. Empty list if bill is clean.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "code": {
                                    "type": "string",
                                    "description": "HCPCS/CPT code from the bill. Omit if not present.",
                                },
                                "description": {
                                    "type": "string",
                                    "description": "Service description as it appears on the bill.",
                                },
                                "quantity": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "description": "Units billed. Default 1 if not specified.",
                                },
                                "billed_amount": {
                                    "type": "number",
                                    "minimum": 0,
                                    "description": "Billed amount in USD. Omit if unreadable.",
                                },
                                "service_date": {
                                    "type": "string",
                                    "description": "Service date as it appears on the bill.",
                                },
                                "anomaly_type": {
                                    "type": "string",
                                    "enum": [
                                        "price_overcharge",
                                        "duplicate_charge",
                                        "unbundling",
                                        "upcoding",
                                        "unknown_code",
                                    ],
                                    "description": "Category of the billing anomaly.",
                                },
                                "severity": {
                                    "type": "string",
                                    "enum": ["high", "medium", "low", "info"],
                                    "description": (
                                        "high: strong evidence, dispute immediately. "
                                        "medium: suspicious, request clarification. "
                                        "low: worth reviewing. "
                                        "info: informational only."
                                    ),
                                },
                                "explanation": {
                                    "type": "string",
                                    "description": (
                                        "Plain-English explanation for the patient of why "
                                        "this charge is anomalous. Be specific and factual."
                                    ),
                                },
                                "medicare_reference_price": {
                                    "type": "number",
                                    "minimum": 0,
                                    "description": "Medicare reference price for this code in USD. Omit if unknown.",
                                },
                                "overcharge_ratio": {
                                    "type": "number",
                                    "minimum": 0,
                                    "description": (
                                        "billed_amount / medicare_reference_price. "
                                        "2.0 means billed at twice the Medicare rate. "
                                        "Omit if price data is unavailable."
                                    ),
                                },
                                "suggested_action": {
                                    "type": "string",
                                    "description": (
                                        "Specific, actionable next step for the patient. "
                                        "Example: 'Request an itemized bill and ask your "
                                        "provider to explain the charge for code 99213.'"
                                    ),
                                },
                            },
                            "required": [
                                "description",
                                "anomaly_type",
                                "severity",
                                "explanation",
                                "suggested_action",
                            ],
                        },
                    },
                },
                "required": ["total_line_items", "anomalies"],
            },
        },
    ]


# ============================================================
# SYSTEM PROMPT
# ============================================================

_SYSTEM_PROMPT = """You are a medical billing specialist helping patients identify potential overcharges and errors in their medical bills.

You have access to a database of HCPCS/CPT codes with Medicare reference prices computed from CMS Relative Value Units (RVU × $32.74 conversion factor). These are government reference prices — not the only valid price, but a strong baseline for identifying outliers.

## Your task

1. Read the redacted medical bill text provided.
2. Identify all charge line items on the bill.
3. For each line item, use the pre-fetched HCPCS reference data provided (or call search_hcpcs for codes not pre-fetched) to check for anomalies.
4. Call report_anomalies ONCE with your complete findings.

## Anomaly types to look for

- **price_overcharge**: Billed amount is significantly above the Medicare reference price.
  - HIGH severity: >3x the Medicare reference rate
  - MEDIUM severity: 1.5x–3x the Medicare reference rate
  - LOW severity: 1.2x–1.5x the Medicare reference rate
- **duplicate_charge**: Same HCPCS code billed more than once for the same or adjacent dates with no clear clinical justification.
- **unbundling**: Procedures billed separately that should be bundled into a single code under CMS guidelines.
- **upcoding**: The billed code implies a higher complexity or cost than the description suggests.
- **unknown_code**: The code is not in the HCPCS database — possible typo, internal code, or fabricated charge.

## Guidelines

- Focus on facts. Do not speculate about intent.
- Be specific: name the exact code and the exact dollar amounts in explanations.
- If a code is in the pre-fetched context, use that data directly — do not call search_hcpcs for it.
- If a code is NOT in the pre-fetched context, call search_hcpcs to look it up before deciding.
- If a charge has no code and no close HCPCS match, flag it as unknown_code only if the description itself is suspicious or unrecognisable.
- If the bill appears clean after your analysis, call report_anomalies with an empty anomalies list.
- Never invent or guess Medicare prices. Only use prices from the pre-fetched context or search_hcpcs results.
- If the bill contains NO HCPCS/CPT codes (only department-level descriptions
  like "ROOM & BOARD", "PHARMACY", "OPERATING ROOM SERVICES"), do NOT call
  search_hcpcs. These are summary bills — individual procedure codes are on
  a separate itemized statement. Call report_anomalies immediately with
  an info anomaly for each summary line item advising the patient to request
  a full itemized bill with HCPCS codes before disputing.
- Never call search_hcpcs more than once per unique code or description.
- If search_hcpcs returns low-confidence results (no exact code match),
  do not keep searching — flag as unknown_code and move on.

## Overcharge ratio computation

overcharge_ratio = billed_amount / medicare_reference_price

Only include this field when you have both the billed_amount and the medicare_reference_price.

## Suggested actions

Keep these specific and actionable:
- "Request an itemized bill and ask your provider to explain the charge for code {code}."
- "Ask your provider why code {code} appears twice on dates {date1} and {date2}."
- "Contact your insurance company to confirm this code should not be bundled with {other_code}."
"""


# ============================================================
# USER MESSAGE BUILDER
# ============================================================

def _build_user_message(
    bill: RedactedBill,
    rag_context: dict[str, RAGResult],
) -> str:
    """
    Build the initial user message sent to the agent.

    WHAT:
        Combines the redacted bill text with the pre-fetched RAG context
        into a single message. The pre-fetched context is formatted as a
        readable table so the agent can reference it without tool calls.

    WHY INCLUDE RAG CONTEXT IN THE MESSAGE (not just as tool results):
        Putting the pre-fetched data directly in the first message avoids
        extra round-trips for codes already on the bill. The agent can
        immediately start reasoning about prices without calling search_hcpcs
        for every code. Tool calls are reserved for codes the regex missed.

    WHY TABLE FORMAT (not JSON):
        The model reasons better over tabular data than raw JSON for this
        task. JSON would add noise (braces, quotes) that the model has to
        parse mentally. A plain table with | separators is easier to scan.

    ARGS:
        bill:        The redacted bill to analyse.
        rag_context: Pre-fetched HCPCS reference data keyed by code.

    RETURNS:
        A formatted string ready to use as the user message content.
    """
    lines: list[str] = []

    # ---- Bill text ----
    lines.append("## Redacted Medical Bill")
    lines.append("")
    lines.append(f"File: {bill.original_filename}  |  Type: {bill.file_type}")
    lines.append("")
    lines.append("```")
    lines.append(bill.redacted_text)
    lines.append("```")
    lines.append("")

    # ---- Pre-fetched HCPCS context ----
    if rag_context:
        lines.append("## Pre-fetched HCPCS Reference Data")
        lines.append("")
        lines.append(
            "The following codes were found in the bill text and pre-fetched "
            "from the CMS HCPCS database. Use this data directly — "
            "do NOT call search_hcpcs for these codes."
        )
        lines.append("")
        lines.append("| Code | Short Description | Medicare Ref Price | Has Price Data |")
        lines.append("|------|------------------|--------------------|----------------|")
        for code, result in rag_context.items():
            price_str = (
                f"${result.medicare_reference_price:.2f}"
                if result.has_price_data
                else "N/A"
            )
            lines.append(
                f"| {code} | {result.short_description[:50]} | "
                f"{price_str} | {'Yes' if result.has_price_data else 'No'} |"
            )
        lines.append("")
    else:
        lines.append("## Pre-fetched HCPCS Reference Data")
        lines.append("")
        lines.append(
            "No HCPCS codes were pre-fetched (none found via regex in the bill text). "
            "Use search_hcpcs to look up any codes or descriptions you find."
        )
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "Please analyse this bill for billing anomalies. "
        "When you have finished your analysis, call report_anomalies with your findings."
    )

    return "\n".join(lines)


# ============================================================
# TOOL EXECUTION
# ============================================================

def _execute_search_hcpcs(tool_input: dict[str, Any]) -> str:
    """
    Execute a search_hcpcs tool call and return the result as a string.

    WHAT:
        Calls retriever.search() with the query from the tool input,
        and formats the results as a readable table for the next agent turn.

    WHY RETURN STRING (not dict):
        Tool results in the Anthropic API must be strings. The model reads
        this result in its next turn and incorporates it into its reasoning.
        A formatted table is easier for the model to parse than raw JSON.

    ARGS:
        tool_input: The tool call input dict from the model.

    RETURNS:
        Formatted search results as a string, or an error message string.
    """
    query = tool_input.get("query", "").strip()
    n_results = int(tool_input.get("n_results", 5))
    n_results = max(1, min(n_results, 10))  # enforce min/max

    if not query:
        return "Error: search query was empty. Please provide a code or description."

    log.debug(f"Agent tool call: search_hcpcs(query='{query}', n_results={n_results})")

    try:
        results = retriever.search(query, n_results=n_results)
    except Exception as exc:
        log.warning(f"search_hcpcs tool call failed: {exc}")
        return f"Search failed: {exc}. Try a different query."

    if not results:
        return (
            f"No results found for '{query}'. "
            "The code or procedure may not be in the HCPCS database."
        )

    lines: list[str] = [
        f"Search results for '{query}' ({len(results)} found):",
        "",
        "| Code | Short Description | Medicare Ref Price | Has Price Data | Similarity |",
        "|------|------------------|--------------------|----------------|------------|",
    ]

    for r in results:
        price_str = (
            f"${r['medicare_reference_price']:.2f}"
            if r["has_price_data"]
            else "N/A"
        )
        sim_str = (
            f"{r['similarity_score']:.2f}"
            if r.get("similarity_score") is not None
            else "n/a"
        )
        lines.append(
            f"| {r['code']} | {r['short_description'][:50]} | "
            f"{price_str} | {'Yes' if r['has_price_data'] else 'No'} | {sim_str} |"
        )

    return "\n".join(lines)


# ============================================================
# OUTPUT PARSING
# ============================================================

def _parse_anomaly(raw: dict[str, Any]) -> Optional[Anomaly]:
    """
    Parse one anomaly dict from the report_anomalies tool call into an Anomaly model.

    WHAT:
        Builds a BillLineItem from the line-item fields and an Anomaly
        from the anomaly fields in the tool input dict.

    WHY OPTIONAL RETURN (not raise on bad data):
        The model may occasionally produce a slightly malformed anomaly
        (e.g., a missing optional field). We skip malformed items and log
        a warning rather than aborting the entire analysis. One bad anomaly
        should not discard the rest of the findings.

    ARGS:
        raw: One element from the 'anomalies' array in the tool input.

    RETURNS:
        Anomaly model if parsing succeeded. None if any required field
        is missing or has an invalid value.
    """
    try:
        line_item = BillLineItem(
            code=raw.get("code"),
            description=raw["description"],
            quantity=int(raw.get("quantity", 1)),
            billed_amount=float(raw["billed_amount"]) if "billed_amount" in raw and raw["billed_amount"] is not None else None,
            service_date=raw.get("service_date"),
        )

        anomaly = Anomaly(
            line_item=line_item,
            anomaly_type=AnomalyType(raw["anomaly_type"]),
            severity=AnomalySeverity(raw["severity"]),
            explanation=raw["explanation"],
            medicare_reference_price=float(raw["medicare_reference_price"]) if "medicare_reference_price" in raw and raw["medicare_reference_price"] is not None else None,
            overcharge_ratio=float(raw["overcharge_ratio"]) if "overcharge_ratio" in raw and raw["overcharge_ratio"] is not None else None,
            suggested_action=raw["suggested_action"],
        )
        return anomaly

    except (KeyError, ValueError, TypeError) as exc:
        log.warning(f"Skipping malformed anomaly from agent output: {exc} — raw={raw}")
        return None


# ============================================================
# PUBLIC API — called by anomaly_detector.py
# ============================================================

async def analyze(
    bill: RedactedBill,
    rag_context: dict[str, RAGResult],
) -> tuple[list[Anomaly], int]:
    """
    Run the ReAct agent over a redacted bill and return structured anomalies.

    WHAT:
        Drives a multi-turn LLM loop where the agent:
          1. Reads the redacted bill text + pre-fetched RAG context.
          2. Calls search_hcpcs for any codes not already pre-fetched.
          3. Calls report_anomalies once to submit structured findings.

        The loop ends when the agent calls report_anomalies or when
        _MAX_REACT_TURNS is reached (defensive cap).

    WHY THIS SIGNATURE (bill + rag_context not just bill):
        The rag_context is pre-fetched by anomaly_detector.py via a batch
        of exact code lookups. Passing it here avoids the agent calling
        search_hcpcs for every code on the bill — exact lookup is faster
        and more precise than semantic search. This is an optimisation that
        keeps the number of tool call turns low for typical bills.

    ARGS:
        bill:        The redacted bill (text is safe for the Anthropic API).
        rag_context: Pre-fetched HCPCS reference data keyed by code string.

    RETURNS:
        (anomalies, total_line_items) where:
          anomalies         — list of Anomaly models (may be empty = clean bill)
          total_line_items  — count of charge lines the agent found on the bill

    RAISES:
        RuntimeError: if the agent does not call report_anomalies within
                      _MAX_REACT_TURNS turns.
        llm_client.LLMError and subclasses: propagate from llm_client.
    """
    tools = _build_tools()
    system = _SYSTEM_PROMPT
    user_message_text = _build_user_message(bill, rag_context)

    # Initial conversation: one user turn.
    messages: list[dict] = [
        {"role": "user", "content": user_message_text}
    ]

    log.info(
        f"ReAct agent starting: bill='{bill.original_filename}' "
        f"pre_fetched_codes={len(rag_context)} max_turns={_MAX_REACT_TURNS}"
    )

    for turn in range(_MAX_REACT_TURNS):
        log.debug(f"ReAct turn {turn + 1}/{_MAX_REACT_TURNS}")

        # Call the LLM — this may return tool_use or end_turn.
        response: anthropic.types.Message = await llm_client.complete_with_tools(
            messages=messages,
            system=system,
            tools=tools,
        )

        # Append assistant's response to conversation history.
        # WHY: The Anthropic API requires the full conversation history
        # including the assistant's turn when submitting tool results.
        # We append the raw content list (not just text) so tool_use
        # blocks are preserved for the subsequent tool_result turn.
        messages.append({
            "role": "assistant",
            "content": response.content,  # list of content blocks
        })

        # ---- Case 1: Model is done (no more tool calls needed) ----
        if response.stop_reason == "end_turn":
            # The model should always call report_anomalies instead of
            # ending its turn with text. If it ends without calling the tool,
            # something went wrong with the prompt or the model ignored instructions.
            log.warning(
                "ReAct agent ended its turn without calling report_anomalies. "
                "This should not happen — the system prompt requires the tool call. "
                "Returning empty anomaly list."
            )
            return [], 0

        # ---- Case 2: Model wants to call a tool ----
        if response.stop_reason == "tool_use":
            # Collect all tool_use blocks in this response.
            # WHY COLLECT ALL: the model might theoretically call multiple
            # tools in one turn (though in practice it usually calls one at a time).
            tool_use_blocks = [
                block
                for block in response.content
                if block.type == "tool_use"
            ]

            # Build the tool_result turn to append to messages.
            tool_results: list[dict] = []

            for tool_block in tool_use_blocks:
                tool_name = tool_block.name
                tool_input = tool_block.input  # dict, validated by Anthropic SDK

                log.debug(
                    f"Agent tool call: name={tool_name} "
                    f"input_keys={list(tool_input.keys())}"
                )

                # ---- report_anomalies: end the loop ----
                if tool_name == _TOOL_REPORT:
                    return _handle_report_anomalies(tool_input)

                # ---- search_hcpcs: execute and continue ----
                elif tool_name == _TOOL_SEARCH:
                    result_text = _execute_search_hcpcs(tool_input)

                    # Append this tool result so the model can use it next turn.
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_block.id,
                        "content": result_text,
                    })

                else:
                    # Unknown tool — the model called something that doesn't exist.
                    # This should never happen with a correctly defined tool list.
                    log.error(f"Agent called unknown tool: '{tool_name}'")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_block.id,
                        "content": (
                            f"Error: tool '{tool_name}' does not exist. "
                            f"Available tools: {_TOOL_SEARCH}, {_TOOL_REPORT}"
                        ),
                        "is_error": True,
                    })

            # Append tool results as a user turn (Anthropic API requirement).
            if tool_results:
                messages.append({
                    "role": "user",
                    "content": tool_results,
                })

            continue  # Next ReAct turn

        # Unexpected stop_reason — log and break.
        log.error(
            f"Unexpected stop_reason from LLM: '{response.stop_reason}'. "
            "Aborting ReAct loop."
        )
        break

    # If we get here, the loop exhausted _MAX_REACT_TURNS without report_anomalies.
    raise RuntimeError(
        f"ReAct agent did not call report_anomalies within {_MAX_REACT_TURNS} turns. "
        "This may indicate a model prompt issue or an unexpectedly complex bill. "
        "Check logs for details."
    )


# ============================================================
# REPORT HANDLER (separated for readability)
# ============================================================

def _handle_report_anomalies(
    tool_input: dict[str, Any],
) -> tuple[list[Anomaly], int]:
    """
    Parse the report_anomalies tool call and return (anomalies, total_line_items).

    WHAT:
        Extracts total_line_items and the anomalies list from the tool input,
        parses each anomaly into a typed Anomaly model, and returns the results.
        Malformed individual anomalies are skipped (logged as warnings).

    WHY SEPARATE FUNCTION (not inline in the loop):
        Separating this keeps the main loop readable and makes the parsing
        logic independently testable without running the full ReAct loop.

    ARGS:
        tool_input: The input dict from the model's report_anomalies tool call.

    RETURNS:
        (anomalies, total_line_items) ready to return to anomaly_detector.py.
    """
    total_line_items = int(tool_input.get("total_line_items", 0))
    raw_anomalies = tool_input.get("anomalies", [])

    log.info(
        f"Agent called report_anomalies: "
        f"total_line_items={total_line_items} raw_anomaly_count={len(raw_anomalies)}"
    )

    anomalies: list[Anomaly] = []
    for raw in raw_anomalies:
        parsed = _parse_anomaly(raw)
        if parsed is not None:
            anomalies.append(parsed)

    if len(anomalies) < len(raw_anomalies):
        skipped = len(raw_anomalies) - len(anomalies)
        log.warning(
            f"{skipped} anomaly(ies) were skipped due to parse errors. "
            "Check DEBUG logs for details."
        )

    log.info(
        f"ReAct agent complete: {len(anomalies)} valid anomaly(ies) parsed "
        f"from {len(raw_anomalies)} reported, total_line_items={total_line_items}"
    )

    return anomalies, total_line_items
