"""Tests for purchase (expense) creation.

Unit tests for payload construction and validation.
Integration tests (marked with @pytest.mark.integration) hit real Holded API.
Run integration: pytest tests/test_create_purchase.py -m integration
"""
import os
import time
import pytest

from app.holded.write_wrappers import create_purchase
from app.holded.client import SAFE_MODE, post_data, delete_data, fetch_data
from app.domain.item_builder import build_holded_items


class TestBuildPurchasePayload:
    """Test payload construction (no API calls)."""

    def test_basic_items_build(self):
        """Build line items with default IVA."""
        items = [{"name": "Taxi", "units": 1, "price": 25.50, "tax": 21}]
        built = build_holded_items(items, sanitize=False, apply_default_iva=True)
        assert len(built) == 1
        assert built[0]["name"] == "Taxi"

    def test_multiple_items_build(self):
        """Build multiple items with different IVA rates."""
        items = [
            {"name": "Food", "units": 1, "price": 45.00, "tax": 10},
            {"name": "Drinks", "units": 1, "price": 12.00, "tax": 21},
        ]
        built = build_holded_items(items, sanitize=False, apply_default_iva=True)
        assert len(built) == 2

    def test_zero_tax_item(self):
        """Build item with 0% tax."""
        items = [{"name": "EU Export", "units": 1, "price": 500, "tax": 0}]
        built = build_holded_items(items, sanitize=False, apply_default_iva=False)
        assert len(built) == 1


@pytest.mark.integration
class TestCreatePurchaseReal:
    """Integration tests against real Holded API.

    Run: pytest tests/test_create_purchase.py -m integration -v
    Creates real purchases then cleans up.
    """

    def test_create_and_cleanup(self):
        """Create a real purchase, verify response, then delete."""
        result = create_purchase({
            "contactName": "TEST_PYTEST_CLEANUP",
            "date": int(time.time()),
            "desc": "Pytest integration test — auto-cleanup",
            "items": [{"name": "Test expense", "units": 1, "subtotal": 0.01, "tax": 0}],
        })

        # Should return a purchase ID string
        assert result is not None
        assert isinstance(result, str)
        assert len(result) == 24  # Holded IDs are 24 hex chars

        purchase_id = result

        # Cleanup: delete the purchase
        del_result = delete_data(f"/invoicing/v1/documents/purchase/{purchase_id}")
        assert del_result.get("status") == 1

        # Cleanup: delete auto-created contact
        contacts = fetch_data("/invoicing/v1/contacts")
        for c in contacts:
            if c.get("name") == "TEST_PYTEST_CLEANUP":
                delete_data(f"/invoicing/v1/contacts/{c['id']}")

    def test_create_with_existing_contact_name(self):
        """Create purchase with a known contact name."""
        result = create_purchase({
            "contactName": "TEST_PYTEST_CLEANUP_2",
            "date": int(time.time()),
            "items": [
                {"name": "Taxi", "units": 1, "subtotal": 10.00, "tax": 21},
                {"name": "Parking", "units": 1, "subtotal": 5.00, "tax": 21},
            ],
        })

        assert isinstance(result, str) and len(result) == 24

        # Cleanup
        delete_data(f"/invoicing/v1/documents/purchase/{result}")
        contacts = fetch_data("/invoicing/v1/contacts")
        for c in contacts:
            if c.get("name") == "TEST_PYTEST_CLEANUP_2":
                delete_data(f"/invoicing/v1/contacts/{c['id']}")

    def test_create_without_contact_fails(self):
        """Purchase without any contact info should fail."""
        result = create_purchase({
            "date": int(time.time()),
            "items": [{"name": "Test", "units": 1, "subtotal": 1.00, "tax": 0}],
        })
        # Should return error dict
        assert isinstance(result, dict)
        assert result.get("error") is True
