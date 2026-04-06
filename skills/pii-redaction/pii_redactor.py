"""
pii_redactor.py
============================================================
PURPOSE:
    Redacts PHI/PII from medical bill text before the text
    is passed to any external API (Anthropic, etc.) or stored.

DESIGN DECISIONS:
    - Pure Python stdlib (re, logging) — zero dependencies
    - Regex only — no ML, no network, no disk access
    - Returns both redacted text AND an audit log
    - Fails safe: pattern errors are logged, not raised
    - One function per concern — easy to audit and test

SECURITY GUARANTEE:
    Original PII values are never stored, logged, or returned.
    Only the redaction audit log (counts + types found) is kept.

USAGE:
    from services.pii_redactor import redact_pii

    result = redact_pii(raw_text)
    clean_text = result.redacted_text    # send this to LLM
    audit = result.audit_log             # log this for compliance
============================================================
"""

import re
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class RedactionResult:
    """
    Return type for redact_pii().

    WHY A DATACLASS:
        Returning a plain tuple like (text, count) makes callers
        rely on positional access — error-prone and unreadable.
        A dataclass is explicit, type-safe, and self-documenting.
    """
    redacted_text: str
    # Dict of {redaction_type: count_found}
    # Example: {"SSN": 1, "PHONE": 2, "EMAIL": 1}
    # NOTE: we log COUNTS only — never the actual values
    audit_log: dict[str, int] = field(default_factory=dict)

    @property
    def total_redactions(self) -> int:
        return sum(self.audit_log.values())

    @property
    def found_pii(self) -> bool:
        return self.total_redactions > 0


# ============================================================
# REDACTION PATTERNS
#
# Each entry is a tuple of:
#   (label, compiled_regex, replacement_token)
#
# ORDER MATTERS:
#   More specific patterns must come before general ones.
#   Example: Medicare ID before generic member ID, because
#   a Medicare ID could also match a loose member ID pattern.
#
# SECURITY NOTE ON PATTERNS:
#   All patterns use raw strings (r"...") to avoid accidental
#   escape interpretation. All use re.IGNORECASE where the
#   field label might vary in capitalization on real bills.
#   We avoid catastrophic backtracking by keeping alternations
#   simple and quantifiers bounded.
# ============================================================

_PATTERNS: list[tuple[str, re.Pattern, str]] = [

    # ----------------------------------------------------------
    # SSN — Social Security Number
    # Format: XXX-XX-XXXX or XXXXXXXXX
    # WHY FIRST: SSNs are the most sensitive identifier.
    # ----------------------------------------------------------
    (
        "SSN",
        re.compile(
            r"\b(?:ssn|social\s+security(?:\s+number)?)\s*[:\-#]?\s*"
            r"\d{3}[-\s]?\d{2}[-\s]?\d{4}\b",
            re.IGNORECASE,
        ),
        "[SSN]",
    ),
    # Bare SSN without label (less confident but still catch it)
    (
        "SSN",
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        "[SSN]",
    ),

    # ----------------------------------------------------------
    # Medicare / Medicaid ID
    # Medicare format: 1XX9-XX9-XX99 (alphanumeric, 11 chars)
    # Must come before generic MEMBER_ID pattern
    # ----------------------------------------------------------
    (
        "MEDICARE_ID",
        re.compile(
            r"\b(?:medicare|medicaid)\s*(?:id|number|#|no\.?)?\s*[:\-]?\s*"
            r"[A-Z0-9]{1,4}[-\s]?[A-Z0-9]{2,3}[-\s]?[A-Z0-9]{2,4}\b",
            re.IGNORECASE,
        ),
        "[MEDICARE_ID]",
    ),

    # ----------------------------------------------------------
    # Insurance Member ID
    # Typical format: letters + digits, 8-15 chars
    # ----------------------------------------------------------
    (
        "MEMBER_ID",
        re.compile(
            r"\b(?:member\s*id|policy\s*(?:id|number|#)|"
            r"subscriber\s*id|group\s*(?:id|number|#)|"
            r"insurance\s*id)\s*[:\-#]?\s*[A-Z0-9]{6,20}\b",
            re.IGNORECASE,
        ),
        "[MEMBER_ID]",
    ),

    # ----------------------------------------------------------
    # Date of Birth
    # Formats: MM/DD/YYYY, MM-DD-YYYY, Month DD YYYY
    # WHY CAREFUL: dates of service look similar but are NOT PHI.
    # We only redact when labeled as DOB / date of birth.
    # ----------------------------------------------------------
    (
        "DOB",
        re.compile(
            r"\b(?:dob|date\s+of\s+birth|birth\s*date)\s*[:\-]?\s*"
            r"(?:\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}"
            r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
            r"[a-z]*\.?\s+\d{1,2},?\s+\d{4})\b",
            re.IGNORECASE,
        ),
        "[DOB]",
    ),

    # ----------------------------------------------------------
    # Phone number
    # Formats: (XXX) XXX-XXXX, XXX-XXX-XXXX, XXX.XXX.XXXX
    # ----------------------------------------------------------
    (
        "PHONE",
        re.compile(
            # WHY NO LEADING \b: ( is not a word char so
            # \b before \( never matches. No anchor needed
            # since digits provide enough specificity.
            r"(?:\+?1[-.\s]?)?"
            r"(?:\(\d{3}\)\s?|\d{3}[-.\s])"
            r"\d{3}[-.\s]\d{4}"
        ),
        "[PHONE]",
    ),

    # ----------------------------------------------------------
    # Email address
    # ----------------------------------------------------------
    (
        "EMAIL",
        re.compile(
            r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
        ),
        "[EMAIL]",
    ),

    # ----------------------------------------------------------
    # Credit card number
    # Formats: with or without spaces/dashes, 13-19 digits
    # Uses Luhn-approximate pattern (starts with 3,4,5,6)
    # ----------------------------------------------------------
    (
        "CARD_NUMBER",
        re.compile(
            r"\b(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2}|6(?:011|5\d{2}))"
            r"[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}(?:[-\s]?\d{1,3})?\b"
        ),
        "[CARD_NUMBER]",
    ),

    # ----------------------------------------------------------
    # IP address
    # ----------------------------------------------------------
    (
        "IP_ADDRESS",
        re.compile(
            r"\b(?:\d{1,3}\.){3}\d{1,3}\b"
        ),
        "[IP_ADDRESS]",
    ),

    # ----------------------------------------------------------
    # Patient name
    # Looks for labeled name fields on medical bills.
    # WHY LABELED ONLY:
    #   Trying to catch all names without a label produces
    #   too many false positives (drug names, provider names).
    #   We catch labeled patient names reliably.
    # ----------------------------------------------------------
    (
        "NAME",
        re.compile(
            r"\b(?:patient\s*(?:name)?|member\s*name|"
            r"insured\s*name|subscriber\s*name|"
            r"guarantor\s*name|responsible\s*party)\s*[:\-]?\s*"
            r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\b",
            re.IGNORECASE,
        ),
        "[NAME]",
    ),

    # ----------------------------------------------------------
    # Street address
    # Pattern: number + street name + street type
    # Catches: "123 Main Street", "456 Oak Ave", "789 N 5th St"
    # ----------------------------------------------------------
    (
        "ADDRESS",
        re.compile(
            r"\b\d{1,5}\s+(?:[A-Z][a-z]+\s+){1,4}"
            r"(?:street|st|avenue|ave|boulevard|blvd|road|rd|"
            r"drive|dr|lane|ln|court|ct|place|pl|way|circle|cir)"
            r"(?:\s*,\s*(?:apt|suite|ste|unit|#)\s*[\w]+)?\b",
            re.IGNORECASE,
        ),
        "[ADDRESS]",
    ),
]


def redact_pii(text: str) -> RedactionResult:
    """
    Redact all PII/PHI from the given text.

    WHAT THIS DOES:
        Applies each regex pattern in _PATTERNS sequentially.
        Counts how many matches each pattern finds.
        Returns the fully redacted text plus an audit log.

    WHY SEQUENTIAL (not parallel):
        Some patterns must run before others to avoid partial
        matches interfering. Order in _PATTERNS is intentional.

    WHAT THIS DOES NOT DO:
        - Does not store original values
        - Does not make network calls
        - Does not raise exceptions (fails safe per pattern)

    Args:
        text: Raw extracted text from a medical bill.
              Should be plain text (not HTML or binary).

    Returns:
        RedactionResult with redacted_text and audit_log.
    """
    if not text or not text.strip():
        log.warning("redact_pii called with empty text")
        return RedactionResult(redacted_text=text, audit_log={})

    redacted = text
    audit: dict[str, int] = {}

    for label, pattern, token in _PATTERNS:
        try:
            # findall first so we can count matches
            # without storing the actual matched values
            matches = pattern.findall(redacted)
            count = len(matches)

            if count > 0:
                redacted = pattern.sub(token, redacted)
                # Accumulate counts per label
                audit[label] = audit.get(label, 0) + count
                log.debug(f"Redacted {count} instance(s) of {label}")

        except re.error as e:
            # A regex error should never crash the pipeline.
            # Log it clearly so it can be fixed, but continue.
            log.error(f"Regex error in pattern '{label}': {e}")
            continue

    if audit:
        log.info(f"PII redaction complete. Found: {audit}")
    else:
        log.info("PII redaction complete. No PII detected.")

    return RedactionResult(redacted_text=redacted, audit_log=audit)


def assert_no_pii_leak(original: str, redacted: str) -> bool:
    """
    Sanity check: verify the redacted text does not contain
    any content from the original that matched our patterns.

    WHY THIS EXISTS:
        Belt-and-suspenders check before sending to external API.
        If a pattern replacement somehow failed silently, this
        catches it. Call this right before the Anthropic API call.

    Args:
        original: The raw unredacted text
        redacted: The output from redact_pii()

    Returns:
        True if safe to proceed, False if potential leak detected.
    """
    # Re-run all patterns on the redacted text
    # None should match — if they do, redaction failed somewhere
    for label, pattern, _ in _PATTERNS:
        try:
            if pattern.search(redacted):
                log.error(
                    f"SECURITY: Pattern '{label}' still matches in "
                    f"redacted text. Possible redaction failure. "
                    f"Blocking API call."
                )
                return False
        except re.error:
            continue

    return True
