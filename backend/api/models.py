"""
backend/api/models.py
============================================================
PURPOSE:
    All Pydantic request/response models for MedBill Scanner.
    Every service in the pipeline uses types from this file.
    Nothing passes between services as a raw dict.

    This file is intentionally the ONE place to understand
    what data looks like at every stage of the pipeline.

SECTIONS (in order):
    1. Enums               — AnomalyType, AnomalySeverity
    2. Internal pipeline   — RedactedBill, RAGResult, BillLineItem
    3. Domain models       — Anomaly, BillSummary, DisputeLetter
    4. HTTP responses      — AnalysisResponse, ErrorResponse, HealthResponse

PYDANTIC VERSION: 2.x (pydantic==2.7.1 per requirements.txt)
    Uses ConfigDict, model_config, Field — NOT the v1 class Config style.

SECURITY NOTES:
    - HTTP-facing response models use extra="forbid" to reject
      unexpected fields, preventing accidental data leakage.
    - No model in this file ever stores raw bill text (pre-redaction).
      RedactedBill stores the REDACTED text only.
    - No patient-identifying fields anywhere in this file.
      The pipeline is designed so PII never reaches a model instance.
============================================================
"""

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ============================================================
# 1. ENUMS
# ============================================================

class AnomalyType(str, Enum):
    """
    Category of billing anomaly detected.

    WHY str MIXIN:
        Pydantic serializes str-enum values as plain strings (e.g., "duplicate_charge")
        rather than {"value": "duplicate_charge"}. The frontend and dispute generator
        receive clean strings without any unwrapping logic.

    TYPES DEFINED:
        PRICE_OVERCHARGE — billed amount is significantly above Medicare reference rate.
                           "Significant" is defined by the agent (typically >2x).
        DUPLICATE_CHARGE — same HCPCS code billed more than once for the same
                           service date with no clinical justification.
        UNBUNDLING       — procedures billed separately that Medicare requires
                           to be bundled into a single code (and single payment).
                           Unbundling is a known billing fraud pattern.
        UPCODING         — the billed code implies a higher complexity/cost service
                           than the description or context supports.
        UNKNOWN_CODE     — the code is not in the HCPCS database. Could be a typo,
                           an internal code, or a fabricated charge.
    """

    PRICE_OVERCHARGE = "price_overcharge"
    DUPLICATE_CHARGE = "duplicate_charge"
    UNBUNDLING = "unbundling"
    UPCODING = "upcoding"
    UNKNOWN_CODE = "unknown_code"


class AnomalySeverity(str, Enum):
    """
    How urgently the patient should act on this anomaly.

    WHY NOT A NUMERIC SCORE:
        A numeric score (0.0-1.0) requires the frontend to define thresholds
        for display. An enum makes the threshold decision explicit here,
        in the domain layer where it belongs. The agent applies these labels;
        the frontend just renders them.

    LEVELS:
        HIGH   — Strong evidence of overcharge or billing violation.
                 Patient should dispute immediately.
                 Example: duplicate charge for same service, >3x Medicare rate.
        MEDIUM — Suspicious but may have a legitimate explanation
                 (geographic adjustment, facility fee, etc.).
                 Patient should request itemized bill and ask for clarification.
                 Example: 1.5-3x Medicare reference rate.
        LOW    — Worth reviewing, but unlikely to be actionable alone.
                 Could be legitimate given context the agent doesn't have.
                 Example: slightly above average but within normal variation.
        INFO   — Not a charge anomaly, but something the patient should know.
                 Example: a charge category that is often waived for hardship.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


# ============================================================
# 2. INTERNAL PIPELINE MODELS
#    These are NOT returned directly in HTTP responses.
#    They carry data between services inside one request.
# ============================================================

class RedactedBill(BaseModel):
    """
    Output of the OCR + PII redaction stage.

    WHAT THIS REPRESENTS:
        The text of the bill after all patient-identifying information
        has been stripped by pii_redactor.py. This is the ONLY form
        of bill text that is ever passed further down the pipeline.

    WHY NO RAW TEXT FIELD:
        We intentionally do not model the pre-redaction text.
        Not storing it in a typed model prevents it from being
        accidentally passed to a service that calls the Anthropic API.
        The redacted_text field is the only text that exists after
        the OCR + redaction step completes.

    SECURITY NOTE:
        assert_no_pii_leak() must be called on redacted_text before
        this model instance is created (enforced in pii_redactor.py).
        Creating this model is the signal that PII checks passed.

    FIELDS:
        redacted_text    — bill text with PII replaced by [REDACTED] tokens.
        original_filename — the filename the user uploaded. NOT PII.
                            Used only for logging and error messages.
        file_type        — how this bill was processed: "pdf" used pdfplumber,
                           "image" used pytesseract OCR.
        char_count       — length of redacted_text. Useful for logging and
                           for detecting suspiciously short/empty OCR output.
    """

    model_config = ConfigDict(frozen=True)
    # WHY frozen=True: once created, a RedactedBill must not be mutated.
    # Immutability is a safety property here — it prevents a downstream
    # service from accidentally adding fields back to this object.

    redacted_text: str = Field(
        description="Bill text with all PII replaced by [REDACTED] tokens."
    )
    original_filename: str = Field(
        description="Filename of the uploaded file, for logging only."
    )
    file_type: Literal["pdf", "image"] = Field(
        description="'pdf' if processed with pdfplumber, 'image' if OCR via pytesseract."
    )
    char_count: int = Field(
        ge=0,
        description="Character count of redacted_text. Used to detect empty OCR output.",
    )


class RAGResult(BaseModel):
    """
    One result from retriever.search() or retriever.lookup_by_code().

    WHY THIS EXISTS AS A MODEL (not just a dict):
        retriever.py returns dicts. The anomaly detector and agent need
        to pass these results through the call stack without accidentally
        treating them as something else. A typed model makes the contract
        explicit and catches bugs at parse time.

    RELATIONSHIP TO retriever.py:
        The fields here exactly match the keys in the dict returned by
        retriever._format_result(). If one changes, the other must too.

    FIELDS:
        code                    — HCPCS code, e.g., "99213"
        long_description        — full CMS description from the bill text column
        short_description       — abbreviated CMS description
        medicare_reference_price — Medicare payment rate in USD.
                                   0.0 if has_price_data is False.
        total_rvu               — total Relative Value Units.
                                   0.0 if has_price_data is False.
        has_price_data          — False for codes without RVU data (drug codes, etc.).
                                   When False, price comparisons are not meaningful.
        similarity_score        — cosine similarity to the search query, 0.0-1.0.
                                   None for exact lookups via lookup_by_code().
    """

    model_config = ConfigDict(frozen=True)

    code: str
    long_description: str
    short_description: str
    medicare_reference_price: float = Field(ge=0.0)
    total_rvu: float = Field(ge=0.0)
    has_price_data: bool
    similarity_score: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Cosine similarity to query. None for exact code lookups.",
    )


class BillLineItem(BaseModel):
    """
    One line item as extracted from the redacted bill by the ReAct agent.

    WHAT THIS IS:
        The agent reads the redacted bill text and identifies individual
        charge lines. Each line becomes a BillLineItem. These are then
        checked against RAG results to produce Anomaly instances.

    WHY code IS OPTIONAL:
        Many real medical bills list a description without a code, or
        the OCR misreads the code. Forcing a non-null code would make
        the agent hallucinate one. None means "code not found on bill" —
        the retriever.search() semantic path handles these cases.

    WHY billed_amount IS OPTIONAL:
        OCR can fail to parse dollar amounts (especially on scanned bills
        with poor contrast). None means "could not parse amount" —
        the agent notes this but still checks other anomaly types.

    FIELDS:
        code            — HCPCS/CPT code as it appears on the bill.
                          Normalized to uppercase by the agent.
        description     — service description as it appears on the bill.
        quantity        — units billed. Defaults to 1. Must be positive.
        billed_amount   — charge for this line in USD. None if unparseable.
        service_date    — date of service as it appears on the bill (string,
                          not date type — OCR output format is unpredictable).
    """

    code: Optional[str] = Field(
        default=None,
        description="HCPCS code from the bill. None if not found or unreadable.",
    )
    description: str = Field(
        description="Service description exactly as it appears on the bill."
    )
    quantity: int = Field(
        default=1,
        ge=1,
        description="Number of units billed. Must be at least 1.",
    )
    billed_amount: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Charge for this line in USD. None if OCR could not parse it.",
    )
    service_date: Optional[str] = Field(
        default=None,
        description="Service date string from the bill. Not parsed — OCR format varies.",
    )


# ============================================================
# 3. DOMAIN MODELS
#    Core business objects. Used inside HTTP responses.
# ============================================================

class Anomaly(BaseModel):
    """
    One billing anomaly identified by the ReAct agent.

    DESIGN PHILOSOPHY:
        An Anomaly pairs a raw BillLineItem (what the bill says) with
        the agent's judgment about why it's problematic. The line_item
        is always the ground truth from the bill; the other fields are
        the agent's analysis.

    FIELDS:
        line_item               — the raw charge from the bill.
        anomaly_type            — category of problem found.
        severity                — how urgently the patient should act.
        explanation             — plain-English explanation for the patient.
                                  Written by the agent. Goes into the dispute letter.
                                  Should be factual and specific, not alarming.
        medicare_reference_price — Medicare reference rate for this code. None
                                   if the code has no price data or was not found.
        overcharge_ratio        — billed_amount / medicare_reference_price.
                                  None if either value is unavailable.
                                  2.0 means the patient was billed twice the
                                  Medicare reference rate.
        suggested_action        — specific next step for the patient.
                                  Example: "Request itemized bill and ask your
                                  provider to explain the charge for code 99213."
    """

    line_item: BillLineItem
    anomaly_type: AnomalyType
    severity: AnomalySeverity
    explanation: str = Field(
        description="Plain-English explanation of why this charge is anomalous."
    )
    medicare_reference_price: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Medicare reference price for this code in USD.",
    )
    overcharge_ratio: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "billed_amount / medicare_reference_price. "
            "2.0 means billed at twice the Medicare rate. "
            "None if price data is unavailable."
        ),
    )
    suggested_action: str = Field(
        description="Specific action the patient should take regarding this charge."
    )


class BillSummary(BaseModel):
    """
    Aggregate statistics about the analyzed bill.

    WHY THIS EXISTS SEPARATE FROM THE ANOMALY LIST:
        The frontend needs summary numbers for the top of the results page
        without iterating through every anomaly. The agent computes these
        from the full anomaly list; the frontend just renders them.

    FIELDS:
        total_line_items        — how many charge lines were found on the bill.
        total_billed_amount     — sum of all line item amounts. None if any
                                  line item had an unparseable amount.
        anomaly_count           — total number of anomalies found.
        high_severity_count     — anomalies with severity == HIGH.
        medium_severity_count   — anomalies with severity == MEDIUM.
        potential_overcharge_total — sum of (billed - medicare_reference) for all
                                     PRICE_OVERCHARGE anomalies where both values
                                     are known. Gives the patient a dollar figure
                                     to reference in their dispute. None if no
                                     price-overcharge anomalies have price data.
    """

    total_line_items: int = Field(ge=0)
    total_billed_amount: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Total of all billed amounts. None if any amount was unparseable.",
    )
    anomaly_count: int = Field(ge=0)
    high_severity_count: int = Field(ge=0)
    medium_severity_count: int = Field(ge=0)
    potential_overcharge_total: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Sum of (billed - Medicare reference) for price-overcharge anomalies. "
            "None if no price data is available for any overcharge."
        ),
    )


class DisputeLetter(BaseModel):
    """
    A professional dispute letter generated by the dispute_generator service.

    WHY STRUCTURED (not just a string):
        The frontend needs to render the subject line separately from the body
        (e.g., pre-fill a mailto: subject). It also needs anomaly_codes to
        highlight referenced charges in the anomaly list UI.
        A single opaque string would require the frontend to parse the letter.

    FIELDS:
        subject_line    — short subject for the letter (for email subject / heading).
        body            — full letter text, ready to copy/paste or print.
                          Addressed to "Billing Department" generically, since
                          we do not store patient name or provider name.
        anomaly_codes   — HCPCS codes mentioned in the letter. The frontend
                          uses this to cross-reference with the anomaly list.
                          Empty list if no codes could be referenced.
    """

    subject_line: str = Field(
        description="Short subject for the dispute letter, suitable as an email subject."
    )
    body: str = Field(
        description=(
            "Complete dispute letter body. Ready to use — no PII, "
            "addressed generically to 'Billing Department'."
        )
    )
    anomaly_codes: list[str] = Field(
        default_factory=list,
        description="HCPCS codes referenced in the letter body.",
    )


# ============================================================
# 4. HTTP RESPONSE MODELS
#    These are what the API returns to the frontend.
#    extra="forbid" prevents accidental field leakage.
# ============================================================

class AnalysisResponse(BaseModel):
    """
    The complete response from POST /api/analyze.

    This is the top-level object the frontend receives after
    uploading a bill. It contains everything needed to render
    the results page: anomaly list, dispute letter, summary stats.

    WHY dispute_letter IS OPTIONAL:
        If no anomalies are found, there is nothing to dispute.
        Generating a letter with zero anomalies would be misleading.
        None signals "clean bill — no letter needed."

    FIELDS:
        anomalies               — list of anomalies found, ordered by severity
                                  (HIGH first). Empty list means clean bill.
        dispute_letter          — ready-to-use dispute letter. None if no anomalies.
        bill_summary            — aggregate stats for the results page header.
        processing_time_seconds — wall-clock time from upload to response.
                                  Useful for monitoring and user feedback.
    """

    model_config = ConfigDict(extra="forbid")
    # WHY extra="forbid": we construct this response ourselves, so extra fields
    # would be a bug in our code, not a user input issue. Failing loudly is
    # better than silently including unexpected data in the API response.

    anomalies: list[Anomaly] = Field(
        description="Anomalies ordered by severity (HIGH first). Empty if none found."
    )
    dispute_letter: Optional[DisputeLetter] = Field(
        default=None,
        description="Dispute letter template. None if no anomalies were found.",
    )
    bill_summary: BillSummary
    processing_time_seconds: float = Field(
        ge=0.0,
        description="Wall-clock processing time in seconds.",
    )


class ErrorResponse(BaseModel):
    """
    Consistent error format for all non-200 API responses.

    WHY STRUCTURED:
        A plain string error message forces the frontend to parse text
        to distinguish error types (rate limit vs bad file vs server error).
        A structured model lets the frontend branch on `error` field.

    FIELDS:
        error   — machine-readable error code. Stable across releases.
                  Examples: "rate_limit_exceeded", "invalid_file_type",
                  "file_too_large", "ocr_failed", "analysis_failed"
        detail  — human-readable explanation. May change without notice.
                  Suitable for displaying to the user.
    """

    model_config = ConfigDict(extra="forbid")

    error: str = Field(description="Machine-readable error code.")
    detail: str = Field(description="Human-readable error message.")


class HealthResponse(BaseModel):
    """
    Response from GET /health.

    WHY collection_size:
        The single most important thing to know at startup is whether
        ingest.py has run. collection_size == 0 means RAG will return
        no results and the agent cannot do price comparisons.
        Exposing this in /health lets an operator catch this immediately.

    FIELDS:
        status              — "ok" if all systems are ready to serve requests.
                              "degraded" if ChromaDB is up but collection is empty.
                              "unavailable" if ChromaDB is unreachable.
        chromadb_connected  — whether the backend can reach ChromaDB.
        collection_size     — number of HCPCS codes loaded in ChromaDB.
                              0 means ingest.py has not run yet.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "degraded", "unavailable"] = Field(
        description=(
            "'ok' = ready. "
            "'degraded' = ChromaDB up but collection empty (run ingest.py). "
            "'unavailable' = ChromaDB unreachable."
        )
    )
    chromadb_connected: bool
    collection_size: int = Field(ge=0)
