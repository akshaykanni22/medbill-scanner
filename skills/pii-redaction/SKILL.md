# SKILL: PII Redaction

## What This Skill Does
Teaches Claude Code how to write a production-safe PII redaction
service for any project handling sensitive personal data — medical
bills, insurance forms, financial documents, HR records, etc.

## When To Use This Skill
Invoke this skill when a project needs to strip personally identifiable
information from text before that text is sent to any external API,
stored in a database, or logged anywhere.

Trigger phrases:
- "add PII redaction"
- "strip patient data before the API call"
- "redact sensitive information"
- "implement PHI removal"

---

## Core Principles (read before writing any code)

**1. Redact early, redact once**
PII redaction must happen as the FIRST step in any pipeline that
handles user-submitted text. Never pass raw text to downstream
services and rely on them to handle it safely.

**2. Regex over ML models for this task**
Do not use spaCy, Presidio, or any NLP model for basic PII redaction
unless the project explicitly requires entity-level classification.
Reasons:
- Regex is auditable — you can read exactly what it catches
- ML models have false negatives that are invisible and unpredictable
- Regex has zero cold start time, no model download, no GPU needed
- For structured PII (SSN, phone, email, dates) regex is more reliable
  than NLP anyway

**3. Replace don't delete**
Always replace PII with a labeled placeholder like [SSN], [NAME],
[DATE_OF_BIRTH]. Deleting makes the redacted text lose structure
and confuses downstream LLM reasoning. Labeled placeholders let
the LLM understand what was there without seeing the actual value.

**4. Log redaction counts, never redacted values**
Log how many items were redacted per category for debugging.
Never log the original PII values even in debug mode.

**5. Validate redaction before sending to any API**
After redacting, run a second-pass check to catch any PII that
slipped through. Fail loudly if the second pass finds anything.

---

## Implementation

### Patterns to cover (in order of priority)

```python
import re

# Order matters — run more specific patterns before general ones

PII_PATTERNS = [
    # --- US Social Security Number ---
    # Covers: 123-45-6789, 123 45 6789, 123456789
    (re.compile(
        r'\b(?!000|666|9\d{2})\d{3}[-\s]?(?!00)\d{2}[-\s]?(?!0000)\d{4}\b'
    ), "[SSN]"),

    # --- Email addresses ---
    (re.compile(
        r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b'
    ), "[EMAIL]"),

    # --- US Phone numbers ---
    # Covers: (555) 123-4567, 555-123-4567, 5551234567, +1 555 123 4567
    (re.compile(
        r'\b(\+1[\s\-.]?)?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}\b'
    ), "[PHONE]"),

    # --- Dates of birth and service dates ---
    # Covers: MM/DD/YYYY, MM-DD-YYYY, Month DD YYYY
    (re.compile(
        r'\b(0?[1-9]|1[0-2])[\/\-](0?[1-9]|[12]\d|3[01])[\/\-](19|20)\d{2}\b'
        r'|'
        r'\b(January|February|March|April|May|June|July|August|September|'
        r'October|November|December)\s+(0?[1-9]|[12]\d|3[01]),?\s+(19|20)\d{2}\b',
        re.IGNORECASE
    ), "[DATE]"),

    # --- Insurance / Member ID numbers (label-anchored) ---
    # WHY label-anchored: pure alphanumeric has too many false positives
    (re.compile(
        r'(?i)(?:member\s*(?:id|#|number)|policy\s*(?:id|#|number)|'
        r'insurance\s*(?:id|#|number)|subscriber\s*(?:id|#))'
        r'\s*:?\s*([A-Z0-9\-]{6,20})'
    ), "[INSURANCE_ID]"),

    # --- Medical Record Numbers ---
    (re.compile(
        r'(?i)(?:mrn|medical\s*record\s*(?:number|#|no))\s*:?\s*([A-Z0-9\-]{4,20})'
    ), "[MRN]"),

    # --- ZIP+4 (narrower than ZIP alone, can identify ~20 households) ---
    (re.compile(r'\b\d{5}-\d{4}\b'), "[ZIP+4]"),

    # --- Credit card numbers ---
    (re.compile(
        r'\b(?:4[0-9]{12}(?:[0-9]{3})?'   # Visa
        r'|5[1-5][0-9]{14}'                 # Mastercard
        r'|3[47][0-9]{13}'                  # Amex
        r'|6(?:011|5[0-9]{2})[0-9]{12})\b' # Discover
    ), "[CREDIT_CARD]"),
]

# Names: label-anchored only — avoids false positives like "Emergency Room"
NAME_LABELS = re.compile(
    r'(?i)(?:patient|guarantor|subscriber|insured|member|'
    r'responsible\s*party)\s*(?:name)?\s*:?\s*'
    r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})'
)
```

### The redaction function

```python
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class RedactionResult:
    """
    WHY a dataclass not just a string:
    Callers need to know WHAT was redacted to log it and
    to verify it is safe before sending downstream.
    """
    redacted_text: str
    counts: dict[str, int] = field(default_factory=dict)
    total_redacted: int = 0
    is_safe: bool = False  # True only after second-pass validation passes


def redact_pii(raw_text: str) -> RedactionResult:
    """
    Redact all PII from raw text using pattern matching.

    WHAT: Replaces known PII patterns with labeled placeholders.
    WHY: Ensures no PHI reaches the Anthropic API or any external service.
    SECURITY: Runs a second-pass validation after redaction to catch slippage.

    Args:
        raw_text: Raw extracted text from a document. May contain PHI.

    Returns:
        RedactionResult with redacted text, counts per category,
        and is_safe=True only if second-pass validation passes.

    Raises:
        ValueError: If second-pass validation finds remaining PII.
                    Callers MUST handle this and must never proceed.
    """
    if not raw_text or not raw_text.strip():
        return RedactionResult(redacted_text="", is_safe=True)

    text = raw_text
    counts: dict[str, int] = {}

    # Pass 1: Apply all regex patterns
    for pattern, placeholder in PII_PATTERNS:
        matches = pattern.findall(text)
        if matches:
            category = placeholder.strip("[]")
            counts[category] = counts.get(category, 0) + len(matches)
            text = pattern.sub(placeholder, text)

    # Handle names separately (label-anchored)
    name_matches = NAME_LABELS.findall(text)
    if name_matches:
        counts["NAME"] = len(name_matches)
        text = NAME_LABELS.sub(
            lambda m: m.group(0).replace(m.group(1), "[NAME]"),
            text
        )

    total = sum(counts.values())

    # Log counts but NEVER log the actual PII values
    if total > 0:
        log.info(f"Redacted {total} PII items: {counts}")
    else:
        log.info("No PII patterns detected in document")

    # Pass 2: Validation sweep on highest-risk patterns
    # If anything is found here, redaction slipped — fail loudly
    HIGH_RISK = [p for p, _ in PII_PATTERNS[:3]]  # SSN, email, phone
    slippage = [
        pattern.pattern[:40]
        for pattern in HIGH_RISK
        if pattern.search(text)
    ]

    if slippage:
        log.error(f"PII SLIPPAGE DETECTED. Patterns: {slippage}")
        raise ValueError(
            "PII redaction validation failed — potential PHI remains in text. "
            "This text must NOT be sent to any external service."
        )

    return RedactionResult(
        redacted_text=text,
        counts=counts,
        total_redacted=total,
        is_safe=True,
    )
```

### How callers must use this

```python
# CORRECT
try:
    result = redact_pii(extracted_text)
except ValueError as e:
    log.error(f"Aborting pipeline: {e}")
    raise HTTPException(status_code=500, detail="Document processing failed")

assert result.is_safe  # never skip this
send_to_claude(result.redacted_text)

# WRONG — never do either of these
send_to_claude(extracted_text)        # raw text bypasses redaction
send_to_claude(result.redacted_text)  # skipped the is_safe assertion
```

---

## Test Cases (required — ship both files together)

```python
# tests/test_pii_redactor.py

def test_ssn_redacted():
    result = redact_pii("Patient SSN: 123-45-6789")
    assert "[SSN]" in result.redacted_text
    assert "123-45-6789" not in result.redacted_text
    assert result.is_safe

def test_email_redacted():
    result = redact_pii("Contact: john.doe@gmail.com for billing")
    assert "[EMAIL]" in result.redacted_text
    assert "gmail.com" not in result.redacted_text

def test_phone_redacted():
    result = redact_pii("Call (555) 123-4567 for questions")
    assert "[PHONE]" in result.redacted_text

def test_date_redacted():
    result = redact_pii("DOB: 01/15/1980, Date of service: March 5, 2024")
    assert "1980" not in result.redacted_text
    assert "2024" not in result.redacted_text

def test_name_label_anchored():
    result = redact_pii("Patient Name: John Smith\nRoom: Emergency Room")
    assert "John Smith" not in result.redacted_text
    assert "Emergency Room" in result.redacted_text  # must NOT be flagged

def test_empty_input_safe():
    result = redact_pii("")
    assert result.is_safe
    assert result.total_redacted == 0

def test_no_pii_passthrough():
    text = "Procedure: Blood panel. Code: 80053. Amount: $245.00"
    result = redact_pii(text)
    assert result.redacted_text == text
    assert result.total_redacted == 0
```

---

## What This Skill Does NOT Cover

- **NER-based name detection** for names not near a label (mid-sentence names).
  If needed post-MVP, use spaCy `en_core_web_sm` — but only after full
  6-check library vetting per project CLAUDE.md policy.

- **Image redaction — PLANNED POST-MVP**
  This skill redacts extracted TEXT only. It does not black out PII regions
  in the source image before OCR runs.

  The planned post-MVP enhancement adds a Pillow-based image pre-redaction
  layer BEFORE OCR. Design:
  - Library: Pillow only (already vetted, already in requirements.txt)
  - Approach: positional heuristic — blacks out top 15-20% of image
  - Why safe: US medical bill formats (UB-04, CMS-1500) place patient
    name, DOB, SSN in a predictable header region
  - Configurable via env var: IMAGE_REDACTION_HEADER_PCT=0.18
  - Eliminates the regex bypass risk entirely at pixel level
  - Files to create when ready:
      backend/services/image_redactor.py
      skills/pii-redaction/image_redactor.py
      skills/pii-redaction/test_image_redactor.py
  - DO NOT build during MVP phase — document here, build after ship

  Why two layers matter: text redaction catches labeled fields reliably.
  Image redaction catches creative formatting that bypasses regex.
  Together they provide defense in depth with zero extra dependencies.

- **Non-US formats** — patterns are US-centric. International projects need
  NHS numbers, EU national ID formats, etc.

- **HIPAA compliance certification** — this skill helps but does not make a
  product HIPAA compliant. Full compliance requires BAAs, audit logs,
  access controls, encryption at rest, and more.

---

## Files This Skill Produces

Always create both files together. The skill is incomplete without tests.

- `backend/services/pii_redactor.py`
- `tests/test_pii_redactor.py`
