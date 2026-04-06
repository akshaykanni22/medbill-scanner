"""
tests/test_pii_redactor.py
Unit tests for backend/services/pii_redactor.py — PII redaction logic.
"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real")

import pytest
from backend.services.pii_redactor import redact_pii, assert_no_pii_leak, RedactionResult


class TestRedactPii:
    """Tests for redact_pii()."""

    # ---- SSN ----

    def test_redacts_ssn_with_dashes(self):
        result = redact_pii("SSN: 123-45-6789")
        assert "123-45-6789" not in result.redacted_text
        assert "[SSN]" in result.redacted_text

    def test_redacts_bare_ssn_with_dashes(self):
        result = redact_pii("Patient number 123-45-6789 on record")
        assert "123-45-6789" not in result.redacted_text

    def test_redacts_ssn_labeled(self):
        result = redact_pii("Social Security Number: 987-65-4321")
        assert "987-65-4321" not in result.redacted_text

    def test_redacts_ssn_labeled_abbreviated(self):
        result = redact_pii("SSN 456-78-9012 is on file")
        assert "456-78-9012" not in result.redacted_text

    # ---- Date of Birth ----

    def test_redacts_dob_slash_format(self):
        result = redact_pii("DOB: 01/15/1980")
        assert "01/15/1980" not in result.redacted_text
        assert "[DOB]" in result.redacted_text

    def test_redacts_dob_date_of_birth_label(self):
        result = redact_pii("Date of birth: 03-22-1975")
        assert "03-22-1975" not in result.redacted_text

    def test_redacts_dob_month_name_format(self):
        result = redact_pii("Birth date: January 15, 1980")
        assert "January 15, 1980" not in result.redacted_text

    def test_service_date_without_label_not_redacted(self):
        """Service dates without a DOB label must NOT be redacted."""
        result = redact_pii("Service date: 05/01/2023\nCharge: $150")
        # Service dates should be preserved (no DOB label)
        assert "05/01/2023" in result.redacted_text

    # ---- Phone number ----

    def test_redacts_phone_with_dashes(self):
        result = redact_pii("Phone: 555-123-4567")
        assert "555-123-4567" not in result.redacted_text
        assert "[PHONE]" in result.redacted_text

    def test_redacts_phone_with_parens(self):
        result = redact_pii("Call (555) 123-4567 for questions")
        assert "555) 123-4567" not in result.redacted_text

    def test_redacts_phone_with_dots(self):
        result = redact_pii("Contact 555.123.4567")
        assert "555.123.4567" not in result.redacted_text

    # ---- Email ----

    def test_redacts_email(self):
        result = redact_pii("Email: patient@example.com")
        assert "patient@example.com" not in result.redacted_text
        assert "[EMAIL]" in result.redacted_text

    # ---- IP address ----

    def test_redacts_ip_address(self):
        result = redact_pii("Login from 192.168.1.100")
        assert "192.168.1.100" not in result.redacted_text

    # ---- Name ----

    def test_redacts_labeled_patient_name(self):
        result = redact_pii("Patient Name: John Smith")
        assert "John Smith" not in result.redacted_text
        assert "[NAME]" in result.redacted_text

    def test_redacts_member_name(self):
        result = redact_pii("Member Name: Jane Doe")
        assert "Jane Doe" not in result.redacted_text

    # ---- Address ----

    def test_redacts_street_address(self):
        result = redact_pii("Address: 123 Main Street, Apt 4B")
        assert "123 Main Street" not in result.redacted_text

    # ---- Insurance / Member ID ----

    def test_redacts_member_id(self):
        result = redact_pii("Member ID: ABC123456789")
        assert "ABC123456789" not in result.redacted_text

    def test_redacts_policy_number(self):
        result = redact_pii("Policy Number: XYZ987654")
        assert "XYZ987654" not in result.redacted_text

    # ---- Empty / clean input ----

    def test_empty_text_returns_unchanged(self):
        result = redact_pii("")
        assert result.redacted_text == ""
        assert result.audit_log == {}

    def test_whitespace_only_returns_unchanged(self):
        result = redact_pii("   \n   ")
        assert result.total_redactions == 0

    def test_clean_text_unchanged(self):
        text = "Procedure 99213 — Office visit — $89.03"
        result = redact_pii(text)
        assert result.redacted_text == text
        assert result.total_redactions == 0
        assert result.found_pii is False

    # ---- Audit log ----

    def test_audit_log_counts_ssn(self):
        result = redact_pii("SSN: 123-45-6789 and also 456-78-9012")
        # Both should be counted under SSN
        assert result.audit_log.get("SSN", 0) > 0

    def test_found_pii_true_when_redactions_made(self):
        result = redact_pii("patient@test.com")
        assert result.found_pii is True

    def test_total_redactions_reflects_count(self):
        result = redact_pii("patient@test.com and patient2@test.com")
        assert result.total_redactions >= 2


class TestAssertNoPiiLeak:
    """Tests for assert_no_pii_leak()."""

    def test_passes_on_clean_redacted_text(self):
        original = "SSN: 123-45-6789"
        redacted = "SSN: [SSN]"
        # Run the real redactor to produce the truly redacted version
        result = redact_pii(original)
        assert assert_no_pii_leak(original, result.redacted_text) is True

    def test_passes_on_text_with_no_pii(self):
        text = "Procedure 99213 billed $89.03"
        assert assert_no_pii_leak(text, text) is True

    def test_fails_if_ssn_still_in_redacted(self):
        """If a raw SSN is still in the 'redacted' output, check should fail."""
        original = "SSN: 123-45-6789"
        # Simulate a failed redaction — SSN still present in output
        bad_redacted = "SSN: 123-45-6789"
        result = assert_no_pii_leak(original, bad_redacted)
        assert result is False

    def test_fails_if_phone_still_in_redacted(self):
        original = "Call 555-123-4567"
        bad_redacted = "Call 555-123-4567"
        assert assert_no_pii_leak(original, bad_redacted) is False

    def test_fails_if_email_still_in_redacted(self):
        original = "Email: user@example.com"
        bad_redacted = "Email: user@example.com"
        assert assert_no_pii_leak(original, bad_redacted) is False

    def test_passes_after_real_redaction(self):
        """End-to-end: redact then check should always pass."""
        original = (
            "Patient: John Smith\n"
            "SSN: 123-45-6789\n"
            "DOB: 01/01/1970\n"
            "Phone: 555-999-8888\n"
            "Code 99213 billed $150"
        )
        result = redact_pii(original)
        assert assert_no_pii_leak(original, result.redacted_text) is True
