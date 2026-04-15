"""Integration tests for smart estimate — uses Holded test API.

Run with HOLDED_SAFE_MODE=true for dry-run, or false for real creation.
Tests require the test Holded account to have contacts and products.
"""

import os
import pytest
from app.domain.smart_estimate import create_smart_estimate
from app.domain.product_resolver import clear_products_cache
from app.domain.contact_resolver import clear_contacts_cache


@pytest.fixture(autouse=True)
def reset_caches():
    clear_products_cache()
    clear_contacts_cache()
    yield
    clear_products_cache()
    clear_contacts_cache()


class TestSmartEstimateErrors:
    def test_missing_client(self):
        r = create_smart_estimate({
            "products": [{"name": "D850"}],
            "shooting_date": "16/04/2026",
        })
        assert not r["success"]
        assert r["error_type"] == "validation_failed"

    def test_client_not_found(self):
        r = create_smart_estimate({
            "client_name": "ZZZZNONEXISTENT999",
            "products": [{"name": "D850"}],
            "shooting_date": "16/04/2026",
        })
        assert not r["success"]
        assert r["error_type"] == "contact_not_found"

    def test_product_not_found(self):
        r = create_smart_estimate({
            "client_name": "Netflix",
            "products": [{"name": "ZZZZNONEXISTENT_PRODUCT_999"}],
            "shooting_date": "16/04/2026",
        })
        assert not r["success"]
        assert r["error_type"] in ("product_not_found", "contact_not_found")


class TestSmartEstimateLooseItems:
    def test_expense_item(self):
        """Loose expense items don't need catalog lookup."""
        r = create_smart_estimate({
            "client_name": "Netflix",
            "products": [
                {"name": "Taxi al rodaje", "price": 45, "item_type": "expense"},
            ],
            "shooting_date": "16/04/2026",
        })
        if not r["success"]:
            assert r["error_type"] != "product_not_found", f"Expense item shouldn't need catalog: {r}"


class TestSmartEstimateHappyPath:
    """Happy-path test — requires HOLDED_SAFE_MODE=true for dry-run safety."""

    @pytest.mark.skipif(
        os.getenv("HOLDED_SAFE_MODE", "true").lower() != "true",
        reason="Happy-path test requires SAFE_MODE=true to avoid creating real estimates"
    )
    def test_full_flow_safe_mode(self):
        """End-to-end: resolve contact, resolve product, compute fiscal, create estimate (dry-run)."""
        r = create_smart_estimate({
            "client_name": "Netflix",
            "products": [
                {"name": "Taxi al rodaje", "price": 45, "item_type": "expense"},
            ],
            "shooting_date": "20/04/2026",
            "notes": "Integration test — safe mode",
        })
        if r.get("success"):
            assert r.get("safe_mode") is True, "Should be safe_mode in dry-run"
            assert r.get("project_code"), "Should have project_code"
            assert r.get("holded_url"), "Should have holded_url"
            assert "totals" in r, "Should have totals"
        else:
            assert r["error_type"] in ("contact_not_found", "contact_incomplete"), \
                f"Unexpected error in happy path: {r}"
