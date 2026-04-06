"""
test_pii_redactor.py
============================================================
PURPOSE:
    Verifies every pattern in pii_redactor.py works correctly.
    Run before deploying to any environment.

    python test_pii_redactor.py

MENTOR NOTE FOR AKSHAY:
    Every pattern added to pii_redactor.py MUST have a test here.
    Tests prove the pattern catches what it should AND does not
    catch what it shouldn't (false positive tests are as important
    as true positive tests in security-sensitive code).
============================================================
"""

import sys
from pii_redactor import redact_pii, assert_no_pii_leak

# ---- Test helpers ----

passed = 0
failed = 0


def test(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        print(f"  PASS  {name}")
        passed += 1
    else:
        print(f"  FAIL  {name}" + (f" — {detail}" if detail else ""))
        failed += 1


def section(title: str):
    print(f"\n{title}")
    print("-" * 50)


# ============================================================
# TRUE POSITIVE TESTS
# These MUST be redacted
# ============================================================

section("SSN — must be redacted")
r = redact_pii("SSN: 123-45-6789")
test("labeled SSN with dashes", "[SSN]" in r.redacted_text)
test("SSN in audit log", r.audit_log.get("SSN", 0) >= 1)

r = redact_pii("Social Security Number: 123-45-6789")
test("full label 'Social Security Number'", "[SSN]" in r.redacted_text)

r = redact_pii("Patient SSN 123456789")
test("SSN no dashes (bare format)", "[SSN]" in r.redacted_text)

section("Medicare / Medicaid ID — must be redacted")
r = redact_pii("Medicare ID: 1EG4-TE5-MK72")
test("Medicare ID labeled", "[MEDICARE_ID]" in r.redacted_text)

r = redact_pii("Medicaid Number: 9XW3-AB4-CD56")
test("Medicaid Number labeled", "[MEDICARE_ID]" in r.redacted_text)

section("Insurance Member ID — must be redacted")
r = redact_pii("Member ID: XYZ123456789")
test("Member ID", "[MEMBER_ID]" in r.redacted_text)

r = redact_pii("Policy Number: ABC987654")
test("Policy Number", "[MEMBER_ID]" in r.redacted_text)

r = redact_pii("Group ID: GRP00112233")
test("Group ID", "[MEMBER_ID]" in r.redacted_text)

section("Date of Birth — must be redacted")
r = redact_pii("DOB: 01/15/1980")
test("DOB MM/DD/YYYY", "[DOB]" in r.redacted_text)

r = redact_pii("Date of Birth: 03-22-1975")
test("Date of Birth with dashes", "[DOB]" in r.redacted_text)

r = redact_pii("Birth Date: January 5, 1990")
test("Birth Date written month", "[DOB]" in r.redacted_text)

section("Phone — must be redacted")
r = redact_pii("Tel: (804) 555-1234")
test("phone with parens", "[PHONE]" in r.redacted_text)

r = redact_pii("Phone: 804-555-1234")
test("phone dashes", "[PHONE]" in r.redacted_text)

r = redact_pii("Call us at 804.555.1234")
test("phone dots", "[PHONE]" in r.redacted_text)

section("Email — must be redacted")
r = redact_pii("Contact: john.smith@email.com")
test("standard email", "[EMAIL]" in r.redacted_text)

r = redact_pii("Email: patient+billing@hospital.org")
test("email with plus sign", "[EMAIL]" in r.redacted_text)

section("Credit card — must be redacted")
r = redact_pii("Card: 4111 1111 1111 1111")
test("Visa with spaces", "[CARD_NUMBER]" in r.redacted_text)

r = redact_pii("Payment: 5500-0000-0000-0004")
test("Mastercard with dashes", "[CARD_NUMBER]" in r.redacted_text)

section("Patient name — must be redacted")
r = redact_pii("Patient Name: John Smith")
test("Patient Name labeled", "[NAME]" in r.redacted_text)

r = redact_pii("Insured Name: Mary Jane Watson")
test("Insured Name labeled", "[NAME]" in r.redacted_text)

section("Street address — must be redacted")
r = redact_pii("Address: 123 Main Street, Richmond VA")
test("standard street address", "[ADDRESS]" in r.redacted_text)

r = redact_pii("Billing address: 456 Oak Avenue Suite 200")
test("address with suite", "[ADDRESS]" in r.redacted_text)

section("IP address — must be redacted")
r = redact_pii("Connected from IP: 192.168.1.100")
test("IPv4 address", "[IP_ADDRESS]" in r.redacted_text)

# ============================================================
# FALSE POSITIVE TESTS
# These MUST NOT be redacted
# ============================================================

section("CPT / HCPCS codes — must NOT be redacted")
r = redact_pii("Procedure: 99213 Office visit")
test("CPT code 99213 preserved", "99213" in r.redacted_text,
     f"got: {r.redacted_text}")

r = redact_pii("HCPCS: G0008 Flu vaccine admin")
test("HCPCS code G0008 preserved", "G0008" in r.redacted_text)

section("Dollar amounts — must NOT be redacted")
r = redact_pii("Amount due: $1,250.00")
test("dollar amount preserved", "$1,250.00" in r.redacted_text)

r = redact_pii("Charge: 450.00")
test("plain amount preserved", "450.00" in r.redacted_text)

section("Dates of service — must NOT be redacted")
r = redact_pii("Date of Service: 03/15/2025")
test("date of service preserved", "03/15/2025" in r.redacted_text,
     f"got: {r.redacted_text}")

section("Provider info — must NOT be redacted")
r = redact_pii("Provider: Richmond Medical Center NPI: 1234567890")
test("provider name preserved", "Richmond Medical Center" in r.redacted_text)

section("ICD-10 codes — must NOT be redacted")
r = redact_pii("Diagnosis: J06.9 Acute upper respiratory infection")
test("ICD-10 code preserved", "J06.9" in r.redacted_text)

# ============================================================
# EDGE CASES
# ============================================================

section("Edge cases")

r = redact_pii("")
test("empty string returns safely", r.redacted_text == "")

r = redact_pii("   ")
test("whitespace-only returns safely", r.found_pii is False)

# Multiple PII types in one document
bill_text = """
PATIENT BILLING STATEMENT
Patient Name: Jane Doe
DOB: 07/04/1985
SSN: 987-65-4321
Member ID: BCBS123456789
Phone: (555) 867-5309
Email: jane.doe@email.com

Date of Service: 01/10/2025
Procedure: 99214 Office visit established patient
Diagnosis: Z00.00 Encounter for general adult medical exam
Charge: $350.00
"""
r = redact_pii(bill_text)
test("NAME redacted in full bill", "[NAME]" in r.redacted_text)
test("DOB redacted in full bill", "[DOB]" in r.redacted_text)
test("SSN redacted in full bill", "[SSN]" in r.redacted_text)
test("MEMBER_ID redacted in full bill", "[MEMBER_ID]" in r.redacted_text)
test("PHONE redacted in full bill", "[PHONE]" in r.redacted_text)
test("EMAIL redacted in full bill", "[EMAIL]" in r.redacted_text)
test("CPT code preserved in full bill", "99214" in r.redacted_text,
     f"CPT code was unexpectedly removed")
test("date of service preserved in full bill", "01/10/2025" in r.redacted_text,
     f"date of service was unexpectedly removed")
test("dollar amount preserved in full bill", "$350.00" in r.redacted_text)
test("ICD-10 preserved in full bill", "Z00.00" in r.redacted_text)
test("multiple PII types found", r.total_redactions >= 5)

# assert_no_pii_leak sanity check
test(
    "assert_no_pii_leak passes on clean text",
    assert_no_pii_leak(bill_text, r.redacted_text)
)

# ============================================================
# SUMMARY
# ============================================================

print(f"\n{'=' * 50}")
print(f"Results: {passed} passed, {failed} failed")
print(f"{'=' * 50}")

if failed > 0:
    print("\nFAILED — do not deploy until all tests pass.")
    sys.exit(1)
else:
    print("\nAll tests passed. Safe to deploy.")
    sys.exit(0)
