/**
 * frontend/src/types/index.ts
 * ============================================================
 * PURPOSE:
 *   TypeScript types mirroring the backend Pydantic models.
 *   Only HTTP-facing types are defined here — internal pipeline
 *   models (RedactedBill, RAGResult) are backend-only and must
 *   never appear in frontend code.
 *
 * SOURCE OF TRUTH:
 *   backend/api/models.py — if a field changes there, update here.
 *   Sections mirror the models.py section comments exactly so
 *   diffs are easy to spot.
 *
 * SECTIONS (in order):
 *   1. Enums           — AnomalyType, AnomalySeverity
 *   2. Domain models   — BillLineItem, Anomaly, BillSummary, DisputeLetter
 *   3. HTTP responses  — AnalysisResponse, ErrorResponse, HealthResponse
 *
 * NOTE ON OPTIONAL FIELDS:
 *   Python's Optional[X] = X | None maps to `X | null` in TypeScript.
 *   Fields with `default=None` in Pydantic arrive as null in JSON,
 *   not undefined — use `=== null` checks, not `=== undefined`.
 * ============================================================
 */

// ============================================================
// 1. ENUMS
// ============================================================

/**
 * Category of billing anomaly detected by the ReAct agent.
 * Maps to backend AnomalyType enum (str values).
 */
export type AnomalyType =
  | 'price_overcharge'
  | 'duplicate_charge'
  | 'unbundling'
  | 'upcoding'
  | 'unknown_code';

/**
 * How urgently the patient should act on this anomaly.
 * Maps to backend AnomalySeverity enum (str values).
 *
 * Display guidance:
 *   high   → red,    "Dispute immediately"
 *   medium → orange, "Request clarification"
 *   low    → yellow, "Worth reviewing"
 *   info   → blue,   "For your information"
 */
export type AnomalySeverity = 'high' | 'medium' | 'low' | 'info';

// ============================================================
// 2. DOMAIN MODELS
//    These appear nested inside HTTP responses.
//    They are NOT called directly as API endpoints.
// ============================================================

/**
 * One charge line item as extracted from the bill by the agent.
 * Nested inside Anomaly — represents the raw charge from the bill.
 *
 * WHY billed_amount and code are nullable:
 *   OCR may fail to parse amounts (poor scan quality) or codes
 *   (description-only line items). null means "not found on bill",
 *   not "zero". Render as "N/A" in the UI, never as "$0".
 */
export interface BillLineItem {
  /** HCPCS/CPT code as it appears on the bill. null if not found. */
  code: string | null;
  /** Service description exactly as it appears on the bill. */
  description: string;
  /** Number of units billed. Always >= 1. */
  quantity: number;
  /** Charge for this line in USD. null if OCR could not parse it. */
  billed_amount: number | null;
  /** Service date string from the bill. null if not found. */
  service_date: string | null;
}

/**
 * One billing anomaly identified by the ReAct agent.
 *
 * WHY medicare_reference_price and overcharge_ratio are nullable:
 *   Some codes (drug codes, unlisted procedures) have no Medicare
 *   reference price in the CMS database. null means "no price data",
 *   not "free". Never display $0 — display "No reference price available."
 *
 * overcharge_ratio interpretation:
 *   2.0 = billed at twice the Medicare reference rate.
 *   null = either billed_amount or medicare_reference_price is unknown.
 */
export interface Anomaly {
  /** The raw charge from the bill that triggered this anomaly. */
  line_item: BillLineItem;
  /** What category of billing problem was found. */
  anomaly_type: AnomalyType;
  /** How urgently the patient should act. */
  severity: AnomalySeverity;
  /** Plain-English explanation of why this charge is anomalous. */
  explanation: string;
  /** Medicare reference price for this code in USD. null if unavailable. */
  medicare_reference_price: number | null;
  /**
   * billed_amount / medicare_reference_price.
   * null if either value is unavailable.
   */
  overcharge_ratio: number | null;
  /** Specific action the patient should take for this charge. */
  suggested_action: string;
}

/**
 * Aggregate statistics about the analyzed bill.
 * Used to render the summary header on the results page.
 *
 * WHY total_billed_amount is always null (MVP):
 *   The agent only returns anomalous line items, not all line items.
 *   Summing only flagged items would show a misleading partial total.
 *   Display "N/A" — this will be populated post-MVP.
 *
 * potential_overcharge_total is the key number to show the patient:
 *   "You may have been overcharged by approximately $X."
 *   null means no price-overcharge anomalies had price data available.
 */
export interface BillSummary {
  /** Total charge lines found on the bill (includes clean lines). */
  total_line_items: number;
  /** Sum of all billed amounts. Always null in MVP — see note above. */
  total_billed_amount: number | null;
  /** Total number of anomalies found. */
  anomaly_count: number;
  /** Number of HIGH severity anomalies. */
  high_severity_count: number;
  /** Number of MEDIUM severity anomalies. */
  medium_severity_count: number;
  /**
   * Sum of (billed - Medicare reference) for PRICE_OVERCHARGE anomalies
   * where both values are known. null if no such data is available.
   */
  potential_overcharge_total: number | null;
}

/**
 * A professional dispute letter generated by the backend.
 *
 * The body is ready to copy/paste or print — no patient name
 * or provider name is included (addressed generically to
 * "Billing Department").
 */
export interface DisputeLetter {
  /** Short subject line, suitable as an email subject. */
  subject_line: string;
  /** Complete dispute letter body. Ready to use as-is. */
  body: string;
  /**
   * HCPCS codes referenced in the letter.
   * Use these to cross-highlight the corresponding anomalies in the UI.
   * Empty array if no specific codes were referenced.
   */
  anomaly_codes: string[];
}

// ============================================================
// 3. HTTP RESPONSES
//    These are the shapes returned directly by API endpoints.
// ============================================================

/**
 * Response from POST /api/analyze.
 * The top-level object received after a successful bill upload.
 *
 * anomalies is ordered HIGH → MEDIUM → LOW → INFO by the backend.
 * An empty anomalies array means the bill appears clean.
 *
 * WHY dispute_letter is nullable:
 *   If no anomalies are found, no letter is generated.
 *   null = clean bill. Show a "No issues found" message instead.
 */
export interface AnalysisResponse {
  /** Anomalies ordered by severity (HIGH first). Empty if bill is clean. */
  anomalies: Anomaly[];
  /** Dispute letter template. null if no anomalies were found. */
  dispute_letter: DisputeLetter | null;
  /** Aggregate stats for the results page header. */
  bill_summary: BillSummary;
  /** Wall-clock processing time in seconds. Use for "Analyzed in Xs" display. */
  processing_time_seconds: number;
}

/**
 * Consistent error shape for all non-200 API responses.
 *
 * Branch on `error` (machine-readable) for logic.
 * Display `detail` (human-readable) to the user.
 *
 * Known error codes:
 *   upload_read_failed    — could not read the file from the request
 *   file_too_large        — exceeds MAX_UPLOAD_SIZE_MB (HTTP 413)
 *   invalid_file_type     — not PDF/JPEG/PNG (HTTP 415)
 *   ocr_failed            — file unreadable or blank (HTTP 422 or 500)
 *   bill_too_short        — extracted text too short to analyze (HTTP 400)
 *   pii_redaction_failed  — PII check failed, request aborted (HTTP 500)
 *   analysis_failed       — agent or LLM error (HTTP 500)
 *   rate_limit_exceeded   — too many requests (HTTP 429)
 */
export interface ErrorResponse {
  /** Machine-readable error code. Stable across releases. */
  error: string;
  /** Human-readable message. Suitable for display to the user. */
  detail: string;
}

/**
 * Response from GET /api/health.
 * Used to detect startup readiness and ingest status.
 *
 * status values:
 *   'ok'          — ready to serve requests
 *   'degraded'    — ChromaDB connected but HCPCS collection empty
 *                   (ingest.py has not been run yet)
 *   'unavailable' — ChromaDB unreachable
 */
export interface HealthResponse {
  status: 'ok' | 'degraded' | 'unavailable';
  /** Whether the backend can reach ChromaDB. */
  chromadb_connected: boolean;
  /**
   * Number of HCPCS codes loaded in ChromaDB.
   * 0 means ingest.py has not run — price comparisons will not work.
   */
  collection_size: number;
}
