"""Unit tests for fiscal_rules.py — pure IRPF/IVA/accounts logic.

Tests:
  - TestTaxRegime (4 cases): Spain variants, EU, extra-EU, empty
  - TestItemFiscality (5 cases): rental/service/expense Spain + EU/extra-EU
  - TestProjectCode (5 cases): simple, multi-word, lowercase, date range, short date

All tests are pure — no API calls, no DB.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from app.domain.fiscal_rules import (
    determine_tax_regime,
    compute_item_fiscality,
    generate_project_code,
)


# ── Tax Regime ───────────────────────────────────────────────────────────────

class TestTaxRegime:
    def test_spain_iso(self):
        assert determine_tax_regime("ES") == "spain"

    def test_spain_full_name(self):
        assert determine_tax_regime("ESPAÑA") == "spain"

    def test_eu_de(self):
        assert determine_tax_regime("DE") == "eu"

    def test_eu_fr(self):
        assert determine_tax_regime("FR") == "eu"

    def test_extra_eu_us(self):
        assert determine_tax_regime("US") == "extra_eu"

    def test_extra_eu_gb(self):
        assert determine_tax_regime("GB") == "extra_eu"

    def test_empty_string(self):
        assert determine_tax_regime("") == "unknown"


# ── Item Fiscality ───────────────────────────────────────────────────────────

class TestItemFiscality:
    def test_rental_spain(self):
        result = compute_item_fiscality("rental", "spain")
        assert result["tax"] == 21
        assert result["retention"] == 19
        assert result["account_id"] == "69cccc2bd9d45db3170b99a6"

    def test_service_spain(self):
        result = compute_item_fiscality("service", "spain")
        assert result["tax"] == 21
        assert result["retention"] == 15
        assert result["account_id"] == "69cccc2bd9d45db3170b99a8"

    def test_expense_spain(self):
        result = compute_item_fiscality("expense", "spain")
        assert result["tax"] == 21
        assert result["retention"] == 0
        assert result["account_id"] is None

    def test_rental_eu(self):
        result = compute_item_fiscality("rental", "eu")
        assert result["tax"] == 0
        assert result["retention"] == 0
        assert result["account_id"] is None

    def test_rental_extra_eu(self):
        result = compute_item_fiscality("rental", "extra_eu")
        assert result["tax"] == 0
        assert result["retention"] == 0
        assert result["account_id"] is None


# ── Project Code Generation ──────────────────────────────────────────────────

class TestProjectCode:
    def test_simple(self):
        assert generate_project_code("LLUMM", "16/04/2026") == "LLUMM-16042026"

    def test_multi_word(self):
        # Only first word used as prefix
        assert generate_project_code("HOFF BRAND", "16/04/2026") == "HOFF-16042026"

    def test_lowercase(self):
        # Input is lowercased — should be uppercased in output
        assert generate_project_code("llumm studios", "16/04/2026") == "LLUMM-16042026"

    def test_date_range(self):
        # "25-26/05/2026" → first date (25) is used
        assert generate_project_code("LLUMM", "25-26/05/2026") == "LLUMM-25052026"

    def test_short_date(self):
        # Single-digit day and month must be zero-padded
        assert generate_project_code("LLUMM", "17/3/2026") == "LLUMM-17032026"
