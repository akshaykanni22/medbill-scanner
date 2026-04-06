"""
backend/services/dispute_generator.py
============================================================
PURPOSE:
    Generates a professional dispute letter template for a patient
    based on the anomalies identified by the ReAct agent.

    This is a single-turn LLM call (no ReAct loop needed).
    The prompt carries structured anomaly data; the model writes prose.

PIPELINE POSITION:
    routes.py calls:
        generate(anomalies, bill_summary) → DisputeLetter

    This file calls llm_client.complete() (single-turn text completion).

WHY SEPARATE FROM react_agent.py:
    The dispute letter task is fundamentally different from anomaly
    detection — it needs the FULL anomaly list as context and must
    produce a coherent prose letter, not structured tool output.
    Keeping it separate maintains the one-task-per-file discipline
    and makes each file independently testable.

SECURITY CONTRACT:
    The only patient-specific content passed to the Anthropic API here
    is the anomaly data returned by the ReAct agent. That data came from
    bill.redacted_text, which passed assert_no_pii_leak() before the
    ReAct agent saw it. No additional PII can enter this function because
    it accepts Anomaly models (not raw bill text) as input.

    The letter is intentionally addressed generically to "Billing Department"
    — this file never stores or receives patient name or provider name.
    Patients fill in those fields manually before sending.
============================================================
"""

import logging
from typing import Optional

from backend.api.models import (
    Anomaly,
    AnomalyType,
    BillSummary,
    DisputeLetter,
)
from backend.services import llm_client

log = logging.getLogger(__name__)


# ============================================================
# SYSTEM PROMPT
# ============================================================

_SYSTEM_PROMPT = """You are a patient advocate helping patients write professional, factual dispute letters for medical billing errors.

Your letters must be:
- Professional and polite — not accusatory
- Specific — name the exact codes and dollar amounts
- Factual — cite only what is in the anomaly data provided
- Actionable — request a specific response from the billing department
- Brief — 300-500 words maximum
- Addressed generically to "Billing Department" (the patient will add names)

Do NOT:
- Accuse the provider of fraud or intentional wrongdoing
- Speculate beyond the facts given
- Use legal threats or aggressive language
- Include any patient-identifying information (the patient adds this later)
- Invent dollar amounts not in the data

Structure the letter as:
1. Opening paragraph: state the purpose of the letter
2. One paragraph per major anomaly (group minor ones together)
3. Closing paragraph: request itemized bill review and written response within 30 days

Format:
- Plain text, no markdown
- Paragraphs separated by blank lines
- No headers or bullet points inside the letter body
"""


# ============================================================
# PROMPT BUILDER
# ============================================================

def _build_prompt(
    anomalies: list[Anomaly],
    bill_summary: BillSummary,
) -> str:
    """
    Build the user message for the dispute letter generation call.

    WHAT:
        Formats the anomaly list and summary statistics into a structured
        prompt that gives the model all the facts it needs to write the letter.

    WHY NOT PASS THE FULL BILL TEXT:
        The dispute letter is based on the anomaly findings, not the raw bill.
        The bill text may be verbose and hard for the model to use as letter
        source material. Structured anomaly data is the right input here.

    ARGS:
        anomalies:    Sorted anomaly list from detect_anomalies().
        bill_summary: Aggregate statistics for the bill.

    RETURNS:
        Formatted prompt string.
    """
    lines: list[str] = []

    # ---- Summary ----
    lines.append("## Bill Analysis Summary")
    lines.append("")
    lines.append(f"Total line items analysed: {bill_summary.total_line_items}")
    lines.append(f"Anomalies found: {bill_summary.anomaly_count}")
    lines.append(f"High severity: {bill_summary.high_severity_count}")
    lines.append(f"Medium severity: {bill_summary.medium_severity_count}")

    if bill_summary.potential_overcharge_total is not None:
        lines.append(
            f"Estimated potential overcharge: ${bill_summary.potential_overcharge_total:.2f}"
        )

    lines.append("")

    # ---- Anomaly detail ----
    lines.append("## Anomalies to Address in the Letter")
    lines.append("")

    for i, anomaly in enumerate(anomalies, start=1):
        item = anomaly.line_item
        code_str = f" (code: {item.code})" if item.code else ""
        amount_str = f"${item.billed_amount:.2f}" if item.billed_amount is not None else "unknown amount"

        lines.append(f"### Anomaly {i}: {anomaly.anomaly_type.value.upper()} — {anomaly.severity.value.upper()} severity")
        lines.append(f"Service: {item.description}{code_str}")
        lines.append(f"Billed: {amount_str}")

        if anomaly.medicare_reference_price is not None:
            lines.append(f"Medicare reference price: ${anomaly.medicare_reference_price:.2f}")

        if anomaly.overcharge_ratio is not None:
            lines.append(f"Overcharge ratio: {anomaly.overcharge_ratio:.1f}x Medicare rate")

        if item.service_date:
            lines.append(f"Service date: {item.service_date}")

        lines.append(f"Explanation: {anomaly.explanation}")
        lines.append(f"Suggested action: {anomaly.suggested_action}")
        lines.append("")

    # ---- Instruction ----
    lines.append("---")
    lines.append("")
    lines.append(
        "Please write a professional dispute letter based on these findings. "
        "The letter should be addressed to 'Billing Department' — the patient will add "
        "their name, provider name, and account number before sending. "
        "Use plain text with no markdown formatting."
    )

    return "\n".join(lines)


# ============================================================
# OUTPUT PARSING
# ============================================================

def _extract_anomaly_codes(anomalies: list[Anomaly]) -> list[str]:
    """
    Collect all unique HCPCS codes referenced in the anomaly list.

    WHAT:
        Returns a deduplicated list of HCPCS codes from anomaly line items.
        Used to populate DisputeLetter.anomaly_codes for the frontend
        to cross-reference with the anomaly list UI.

    WHY NOT PARSE THE LETTER TEXT:
        Extracting codes from the letter body would require regex and could
        miss codes the model paraphrased or mis-formatted. Using the source
        anomaly data directly is precise and does not depend on LLM output format.

    ARGS:
        anomalies: List of Anomaly models.

    RETURNS:
        List of unique HCPCS code strings in first-seen order.
        Codes are included only if item.code is not None.
    """
    seen: set[str] = set()
    codes: list[str] = []
    for anomaly in anomalies:
        code = anomaly.line_item.code
        if code and code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


def _build_subject_line(
    anomalies: list[Anomaly],
    bill_summary: BillSummary,
) -> str:
    """
    Build a short subject line for the dispute letter.

    WHAT:
        Produces a concise subject line describing the nature of the dispute.
        The frontend uses this as a mailto: subject or document heading.

    WHY NOT LLM-GENERATED:
        A short subject line is deterministic — the same inputs should always
        produce the same output. Using a template here keeps it predictable and
        avoids a second LLM call for a 10-word output.

    ARGS:
        anomalies:    Anomaly list.
        bill_summary: Bill summary for the anomaly count.

    RETURNS:
        Subject line string, e.g.:
        "Dispute: 3 Billing Anomalies Found — Review Requested"
    """
    count = bill_summary.anomaly_count

    # Determine the dominant anomaly type to make the subject more specific.
    # WHY: "Dispute: 2 Billing Anomalies Found" is generic.
    # "Dispute: Possible Overcharge on 2 Items" is more compelling for the patient.
    type_counts: dict[AnomalyType, int] = {}
    for anomaly in anomalies:
        type_counts[anomaly.anomaly_type] = type_counts.get(anomaly.anomaly_type, 0) + 1

    dominant_type: Optional[AnomalyType] = None
    if type_counts:
        dominant_type = max(type_counts, key=lambda t: type_counts[t])

    type_label_map = {
        AnomalyType.PRICE_OVERCHARGE: "Possible Overcharge",
        AnomalyType.DUPLICATE_CHARGE: "Duplicate Charge",
        AnomalyType.UNBUNDLING: "Unbundling Concern",
        AnomalyType.UPCODING: "Upcoding Concern",
        AnomalyType.UNKNOWN_CODE: "Unknown Charge Code",
    }

    if dominant_type and count == 1:
        label = type_label_map.get(dominant_type, "Billing Anomaly")
        return f"Dispute: {label} — Itemized Review Requested"

    if dominant_type and count > 1:
        label = type_label_map.get(dominant_type, "Billing Anomaly")
        return f"Dispute: {count} Billing Issues ({label} and Others) — Review Requested"

    # Fallback for zero anomalies (should not reach generate() in this case,
    # but be defensive).
    return "Medical Bill Review Request"


# ============================================================
# PUBLIC API — called by routes.py
# ============================================================

async def generate(
    anomalies: list[Anomaly],
    bill_summary: BillSummary,
) -> Optional[DisputeLetter]:
    """
    Generate a professional dispute letter from a list of billing anomalies.

    WHAT:
        Calls the Anthropic API (single-turn, no tool use) with a structured
        prompt containing the anomaly findings, and parses the response into
        a DisputeLetter model.

    WHEN TO CALL:
        routes.py calls this after detect_anomalies() when anomalies is non-empty.
        If anomalies is empty, routes.py skips this call and returns None for
        the dispute_letter field of AnalysisResponse.

    WHY SINGLE-TURN (not ReAct):
        Letter generation is a straightforward writing task — give the model
        the facts, get prose back. No tool use or multi-step reasoning needed.
        ReAct overhead (extra turns, tool schemas) would add latency and cost
        with no benefit.

    ARGS:
        anomalies:    Non-empty list of Anomaly models from detect_anomalies().
                      Caller is responsible for ensuring this is non-empty.
        bill_summary: Aggregate statistics, used in the prompt and subject line.

    RETURNS:
        DisputeLetter model if generation succeeded.
        None if the anomaly list is empty (no letter needed — caller should
        check before calling, but we handle it gracefully).

    RAISES:
        llm_client.LLMError and subclasses: propagate to routes.py for
        HTTP error mapping.
    """
    if not anomalies:
        log.info("generate() called with empty anomaly list — returning None")
        return None

    log.info(
        f"Generating dispute letter for {len(anomalies)} anomaly(ies) "
        f"({bill_summary.high_severity_count} HIGH, "
        f"{bill_summary.medium_severity_count} MEDIUM)"
    )

    prompt = _build_prompt(anomalies, bill_summary)

    # Single-turn text completion — temperature=0.0 for reproducibility.
    # WHY 2048 max_tokens: letters are 300-500 words (~400-650 tokens).
    # 2048 gives comfortable headroom. See llm_client._DEFAULT_MAX_TOKENS_TEXT.
    letter_body = await llm_client.complete(
        messages=[{"role": "user", "content": prompt}],
        system=_SYSTEM_PROMPT,
        # max_tokens and temperature use llm_client defaults (2048, 0.0)
    )

    subject_line = _build_subject_line(anomalies, bill_summary)
    anomaly_codes = _extract_anomaly_codes(anomalies)

    letter = DisputeLetter(
        subject_line=subject_line,
        body=letter_body.strip(),
        anomaly_codes=anomaly_codes,
    )

    log.info(
        f"Dispute letter generated: subject='{subject_line}' "
        f"body_chars={len(letter.body)} codes={anomaly_codes}"
    )

    return letter
