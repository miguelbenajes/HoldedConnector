"""Tests for write_preview.py — preview builder and warnings."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import write_preview as wp


def test_calculate_items_basic():
    items = [{"name": "Camera", "price": 100, "units": 2, "tax": 21}]
    result = wp._calculate_items(items)
    assert len(result) == 1
    assert result[0]["line_subtotal"] == 200.00
    assert result[0]["line_tax"] == 42.00
    assert result[0]["line_total"] == 242.00

def test_calculate_items_with_discount():
    items = [{"name": "Camera", "price": 100, "units": 1, "tax": 21, "discount": 10}]
    result = wp._calculate_items(items)
    assert result[0]["line_subtotal"] == 100.00
    assert result[0]["line_discount"] == 10.00
    assert result[0]["line_tax"] == 18.90  # (100-10) * 0.21
    assert result[0]["line_total"] == 108.90  # 90 + 18.90

def test_calculate_items_zero_tax():
    items = [{"name": "Service", "price": 50, "units": 1, "tax": 0}]
    result = wp._calculate_items(items)
    assert result[0]["line_tax"] == 0
    assert result[0]["line_total"] == 50.00

def test_item_warnings_high_amount():
    items = [{"name": "Expensive", "line_total": 6000, "units": 1, "stock": None, "kind": None,
              "line_subtotal": 6000, "line_discount": 0, "line_tax": 0}]
    warnings = wp._get_item_warnings(items, high_amount_threshold=5000)
    codes = [w["code"] for w in warnings]
    assert "HIGH_AMOUNT" in codes

def test_item_warnings_zero_stock():
    items = [{"name": "Camera", "line_total": 100, "units": 1, "stock": 0, "kind": "simple",
              "line_subtotal": 100, "line_discount": 0, "line_tax": 0}]
    warnings = wp._get_item_warnings(items)
    codes = [w["code"] for w in warnings]
    assert "ZERO_STOCK" in codes

def test_item_warnings_low_stock():
    items = [{"name": "Camera", "line_total": 100, "units": 5, "stock": 2, "kind": "simple",
              "line_subtotal": 100, "line_discount": 0, "line_tax": 0}]
    warnings = wp._get_item_warnings(items)
    codes = [w["code"] for w in warnings]
    assert "LOW_STOCK" in codes

def test_item_warnings_pack():
    items = [{"name": "Kit", "line_total": 100, "units": 1, "stock": 10, "kind": "pack",
              "line_subtotal": 100, "line_discount": 0, "line_tax": 0}]
    warnings = wp._get_item_warnings(items)
    codes = [w["code"] for w in warnings]
    assert "PRODUCT_IS_PACK" in codes

def test_reversibility_invoice():
    assert wp.REVERSIBILITY["create_invoice"]["can_reverse"] is True

def test_reversibility_send():
    assert wp.REVERSIBILITY["send_document"]["can_reverse"] is False


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
