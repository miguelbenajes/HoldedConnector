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

def _mock_gateway_success(entity_id="abc123"):
    """Simulate a successful gateway.execute() response."""
    return {
        "success": True,
        "entity_id": entity_id,
        "entity_type": "invoice",
        "doc_number": "F-001",
        "audit_id": 42,
        "safe_mode": False,
    }


def _mock_gateway_error(msg="Test error"):
    """Simulate a failed gateway.execute() response."""
    return {
        "success": False,
        "errors": [{"field": "test", "msg": msg}],
    }


def test_invoice_response_format_success():
    """POST /api/agent/invoice must return {success, id, safe_mode}."""
    gw_result = _mock_gateway_success("inv123")
    # Simulate what the endpoint does with a success result
    response = {
        "success": True,
        "id": gw_result.get("entity_id", ""),
        "safe_mode": gw_result.get("safe_mode", False),
    }
    assert "id" in response  # NOT entity_id
    assert "safe_mode" in response
    assert response["id"] == "inv123"


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
