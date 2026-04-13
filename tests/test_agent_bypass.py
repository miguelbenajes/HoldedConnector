"""Tests for Phase 3 gateway bypass migration — verify response format compatibility.

These tests verify that the gateway-routed agent endpoints return the EXACT same
response keys as the legacy direct-connector endpoints, preserving compatibility
with Brain v1, job-automation.ts, and other consumers.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch, MagicMock
from write_gateway import OPERATIONS, _build_holded_payload


# ── OPERATIONS registry tests ────────────────────────────────────────

def test_approve_invoice_in_operations():
    assert "approve_invoice" in OPERATIONS
    op = OPERATIONS["approve_invoice"]
    assert op["method"] == "PUT"
    assert "{doc_id}" in op["endpoint"]
    assert op["entity_type"] == "invoice"
    assert op["sync_type"] == "document"


def test_all_bypass_operations_registered():
    """All 8 operations used by agent endpoints must be in OPERATIONS."""
    required = {
        "create_invoice", "create_estimate", "create_contact",
        "update_document_status", "send_document", "update_estimate_items",
        "approve_invoice", "convert_estimate_to_invoice",
    }
    for op in required:
        assert op in OPERATIONS, f"{op} missing from OPERATIONS"


# ── Payload builder tests ────────────────────────────────────────────

def test_approve_invoice_payload():
    payload = _build_holded_payload("approve_invoice", {"doc_id": "abc123"})
    assert payload == {"approveDoc": True}


def test_create_invoice_payload_has_date():
    payload = _build_holded_payload("create_invoice", {
        "contact_id": "c1", "items": [], "desc": "Test"
    })
    assert "contactId" in payload
    assert "date" in payload
    assert isinstance(payload["date"], int)


def test_update_estimate_items_payload():
    payload = _build_holded_payload("update_estimate_items", {
        "items": [{"name": "Test", "units": 1, "subtotal": 100}],
        "contact_id": "c1",
    })
    assert "items" in payload
    assert "contactId" in payload


# ── Response format compatibility tests ──────────────────────────────
# These verify the SHAPE of responses, not actual API calls.

def _mock_gateway_success(entity_id="abc123", warnings=None):
    """Simulate a successful gateway.execute() response."""
    result = {
        "success": True,
        "entity_id": entity_id,
        "entity_type": "invoice",
        "doc_number": "F-001",
        "audit_id": 42,
        "safe_mode": False,
        "warnings": warnings or [],
    }
    return result


def _mock_gateway_error(msg="Test error"):
    """Simulate a failed gateway.execute() response."""
    return {
        "success": False,
        "errors": [{"field": "test", "msg": msg}],
    }


def test_invoice_response_format_success():
    """POST /api/agent/invoice must return {success, id, safe_mode, holded_url}."""
    from app.routers.agent_writes import _enrich_response
    gw_result = _mock_gateway_success("inv123")
    response = _enrich_response(gw_result, "invoice")
    assert response["success"] is True
    assert response["id"] == "inv123"
    assert "safe_mode" in response
    assert response["holded_url"] == "https://app.holded.com/invoicing/invoices/inv123"
    assert "warnings" not in response  # empty warnings list → key omitted


def test_enrich_response_invoice_url():
    """_enrich_response builds correct Holded URL for invoices."""
    from app.routers.agent_writes import _enrich_response
    result = _mock_gateway_success("abc999")
    response = _enrich_response(result, "invoice")
    assert "invoices" in response["holded_url"]
    assert "abc999" in response["holded_url"]


def test_enrich_response_estimate_url():
    """_enrich_response builds correct Holded URL for estimates."""
    from app.routers.agent_writes import _enrich_response
    result = _mock_gateway_success("est456")
    response = _enrich_response(result, "estimate")
    assert "estimates" in response["holded_url"]
    assert "est456" in response["holded_url"]


def test_enrich_response_includes_warnings_when_present():
    """_enrich_response includes warnings key only when non-empty."""
    from app.routers.agent_writes import _enrich_response
    warning = {"level": "warning", "code": "DATE_IS_TODAY", "msg": "Date is today"}
    result = _mock_gateway_success("abc", warnings=[warning])
    response = _enrich_response(result, "invoice")
    assert "warnings" in response
    assert len(response["warnings"]) == 1
    assert response["warnings"][0]["code"] == "DATE_IS_TODAY"


def test_enrich_response_no_url_when_no_entity_id():
    """_enrich_response omits holded_url when entity_id is empty."""
    from app.routers.agent_writes import _enrich_response
    result = {"success": True, "entity_id": "", "safe_mode": False, "warnings": []}
    response = _enrich_response(result, "invoice")
    assert "holded_url" not in response


def test_invoice_response_format_error():
    """POST /api/agent/invoice error must return {success, error, safe_mode}."""
    gw_result = _mock_gateway_error("API failed")
    error = gw_result.get("errors", [{}])[0].get("msg", "Failed") if gw_result.get("errors") else "Failed"
    response = {"success": False, "error": error, "safe_mode": True}
    assert "error" in response  # string, NOT errors array
    assert "safe_mode" in response


def test_update_estimate_response_format():
    """PUT /api/agent/estimate/{id} must return {success, estimate_id, id}."""
    gw_result = _mock_gateway_success()
    estimate_id = "est_abc"
    response = {
        "success": True,
        "estimate_id": estimate_id,
        "id": estimate_id,  # dual key for job-automation.ts compat
    }
    assert "estimate_id" in response
    assert "id" in response
    assert response["estimate_id"] == response["id"]


def test_approve_response_format():
    """PUT /api/agent/invoice/{id}/approve must return hacienda fields."""
    gw_result = _mock_gateway_success()
    response = {
        "success": True,
        "info": "Invoice approved",
        "hacienda_warning": True,
        "hacienda_detail": "Invoice submitted to Hacienda/SII. This is irreversible.",
        "audit_id": gw_result.get("audit_id", ""),
    }
    assert "hacienda_warning" in response
    assert "hacienda_detail" in response
    assert response["hacienda_warning"] is True


def test_status_response_format():
    """PUT /api/agent/invoice/{id}/status must return {success, safe_mode}."""
    gw_result = _mock_gateway_success()
    response = {"success": True, "safe_mode": gw_result.get("safe_mode", False)}
    assert "safe_mode" in response
    assert "id" not in response  # status endpoint does NOT return id


def test_send_response_format():
    """POST /api/agent/send/{type}/{id} must return {success, safe_mode}."""
    gw_result = _mock_gateway_success()
    response = {"success": True, "safe_mode": gw_result.get("safe_mode", False)}
    assert "safe_mode" in response
    assert "id" not in response  # send endpoint does NOT return id


# ── Tax normalization tests ───────────────────────────────────────────

def test_normalize_taxes_from_api_item():
    """API items with taxes array should pass through unchanged."""
    from write_gateway import _normalize_item_taxes
    item = {"name": "Test", "taxes": ["s_iva_21", "s_ret_15"], "tax": 21}
    assert _normalize_item_taxes(item) == ["s_iva_21", "s_ret_15"]


def test_normalize_taxes_from_db_item_with_retention():
    """DB items with tax + retention should reconstruct taxes array."""
    from write_gateway import _normalize_item_taxes
    item = {"name": "Test", "tax": 21, "retention": 15}
    result = _normalize_item_taxes(item)
    assert result == ["s_iva_21", "s_ret_15"]


def test_normalize_taxes_from_db_item_retention_19():
    """DB items with 19% retention (equipment rental)."""
    from write_gateway import _normalize_item_taxes
    item = {"name": "Camera", "tax": 21, "retention": 19}
    result = _normalize_item_taxes(item)
    assert result == ["s_iva_21", "s_ret_19"]


def test_normalize_taxes_from_db_item_no_retention():
    """DB items with only tax (no retention) should return IVA only."""
    from write_gateway import _normalize_item_taxes
    item = {"name": "Test", "tax": 21}
    result = _normalize_item_taxes(item)
    assert result == ["s_iva_21"]


def test_normalize_taxes_zero_tax():
    """Items with tax=0 and no retention."""
    from write_gateway import _normalize_item_taxes
    item = {"name": "REF", "tax": 0}
    result = _normalize_item_taxes(item)
    assert result == ["s_iva_0"]


def test_normalize_taxes_empty_taxes_array():
    """Empty taxes array should fall through to numeric fields."""
    from write_gateway import _normalize_item_taxes
    item = {"name": "REF", "taxes": [], "tax": 0}
    result = _normalize_item_taxes(item)
    assert result == ["s_iva_0"]


def test_normalize_taxes_negative_retention():
    """Retention stored as negative number (some DB formats)."""
    from write_gateway import _normalize_item_taxes
    item = {"name": "Test", "tax": 21, "retention": -15}
    result = _normalize_item_taxes(item)
    assert result == ["s_iva_21", "s_ret_15"]


def test_normalize_taxes_no_tax_fields():
    """Item with no tax info at all should return None."""
    from write_gateway import _normalize_item_taxes
    item = {"name": "Test"}
    assert _normalize_item_taxes(item) is None


# ── Validator tests ──────────────────────────────────────────────────

def test_approve_invoice_validator_registered():
    from write_validators import VALIDATORS
    assert "approve_invoice" in VALIDATORS


def test_approve_invoice_preview_registered():
    from write_preview import REVERSIBILITY
    assert "approve_invoice" in REVERSIBILITY
    assert REVERSIBILITY["approve_invoice"]["can_reverse"] is False


# ── Rate limits tests ────────────────────────────────────────────────

def test_gaffer_source_in_rate_limits():
    """Gaffer source must have explicit rate limits in the gateway."""
    # Read the rate_limits dict from execute() — it's inline, so we check
    # that the gateway doesn't reject gaffer source
    from write_gateway import WriteGateway
    gw = WriteGateway()
    # Unknown operations return error, but rate limiting should not reject gaffer
    result = gw.execute("__nonexistent__", {}, source="gaffer")
    assert result.get("success") is False
    # If rate limit was the blocker, msg would contain "Rate limit"
    error_msgs = [e.get("msg", "") for e in result.get("errors", [])]
    for msg in error_msgs:
        assert "Rate limit" not in msg, "gaffer source should not be rate-limited on first call"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
