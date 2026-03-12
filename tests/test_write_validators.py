"""Tests for write_validators.py — input sanitization and validation rules."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import write_validators as wv


# ── Input Sanitization Tests ─────────────────────────────────────────

def test_sanitize_text_strips_html():
    assert wv._sanitize_text("<b>hello</b>") == "hello"
    assert wv._sanitize_text("<script>alert(1)</script>") == "alert(1)"

def test_sanitize_text_strips_whitespace():
    assert wv._sanitize_text("  hello  ") == "hello"

def test_sanitize_text_enforces_max_length():
    long = "a" * 600
    assert len(wv._sanitize_text(long)) == 500

def test_validate_holded_id_valid():
    assert wv._validate_holded_id("5ab391071d6d820034294783", "id") is None

def test_validate_holded_id_invalid():
    err = wv._validate_holded_id("not-a-valid-id", "id")
    assert err is not None
    assert "24-char hex" in err["msg"]

def test_validate_holded_id_empty():
    err = wv._validate_holded_id("", "id")
    assert err is not None

def test_validate_email_valid():
    assert wv._validate_email("test@example.com") is None

def test_validate_email_invalid():
    err = wv._validate_email("not-an-email")
    assert err is not None

def test_validate_email_optional():
    assert wv._validate_email(None) is None
    assert wv._validate_email("") is None

def test_validate_amount_valid():
    assert wv._validate_amount(100, "price") is None
    assert wv._validate_amount(0, "price") is None

def test_validate_amount_negative():
    err = wv._validate_amount(-1, "price")
    assert err is not None

def test_validate_amount_too_high():
    err = wv._validate_amount(1000001, "price")
    assert err is not None

def test_validate_date_valid():
    assert wv._validate_date(1710000000, "date") is None

def test_validate_date_too_old():
    err = wv._validate_date(1000000000, "date")  # 2001
    assert err is not None

def test_validate_date_optional():
    assert wv._validate_date(None, "date") is None


# ── Status Transition Tests ──────────────────────────────────────────

def test_invoice_valid_transitions():
    assert 1 in wv.INVOICE_TRANSITIONS[0]  # draft → issued
    assert 3 in wv.INVOICE_TRANSITIONS[1]  # issued → paid
    assert 5 in wv.INVOICE_TRANSITIONS[2]  # partial → cancelled

def test_invoice_terminal_states():
    assert len(wv.INVOICE_TRANSITIONS[3]) == 0  # paid = terminal
    assert len(wv.INVOICE_TRANSITIONS[5]) == 0  # cancelled = terminal

def test_estimate_transitions():
    assert 1 in wv.ESTIMATE_TRANSITIONS[0]  # draft → pending
    assert 2 in wv.ESTIMATE_TRANSITIONS[1]  # pending → accepted
    assert 4 in wv.ESTIMATE_TRANSITIONS[2]  # accepted → invoiced
    assert len(wv.ESTIMATE_TRANSITIONS[3]) == 0  # rejected = terminal


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
