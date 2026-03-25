"""
write_gateway.py — Safe Write Gateway for Holded Connector.

All write operations (AI agent, REST API, CLI scripts) route through this
gateway. Provides a 6-stage pipeline: validate → preview → confirm/log →
execute → sync-back → audit.

Usage:
    from write_gateway import gateway
    result = gateway.execute("create_invoice", params, source="ai_agent")
"""

import hashlib
import hmac
import json
import logging
import os
import threading
import time

import connector
import write_validators
from write_validators import _sanitize_text
import write_preview

logger = logging.getLogger(__name__)


# ── Operation Registry ───────────────────────────────────────────────

OPERATIONS = {
    "create_invoice": {
        "method": "POST",
        "endpoint": "/invoicing/v1/documents/invoice",
        "entity_type": "invoice",
        "sync_type": "document",
        "sync_doc_type": "invoice",
    },
    "create_estimate": {
        "method": "POST",
        "endpoint": "/invoicing/v1/documents/estimate",
        "entity_type": "estimate",
        "sync_type": "document",
        "sync_doc_type": "estimate",
    },
    "create_contact": {
        "method": "POST",
        "endpoint": "/invoicing/v1/contacts",
        "entity_type": "contact",
        "sync_type": "contact",
    },
    "update_document_status": {
        "method": "PUT",
        "endpoint": "/invoicing/v1/documents/{doc_type}/{doc_id}",
        "entity_type": "document",
        "sync_type": "document",
    },
    "send_document": {
        "method": "POST",
        "endpoint": "/invoicing/v1/documents/{doc_type}/{doc_id}/send",
        "entity_type": "document",
        "sync_type": None,  # No sync needed
    },
    "upload_file": {
        "method": None,  # Local only
        "entity_type": "file",
        "sync_type": None,
    },
    "convert_estimate_to_invoice": {
        "method": None,  # Custom multi-step execution
        "entity_type": "invoice",
        "sync_type": "document",
        "sync_doc_type": "invoice",
    },
    "update_estimate_items": {
        "method": "PUT",
        "endpoint": "/invoicing/v1/documents/estimate/{doc_id}",
        "entity_type": "estimate",
        "sync_type": "document",
        "sync_doc_type": "estimate",
    },
}


# ── Rate Limiting ────────────────────────────────────────────────────

class RateLimiter:
    """In-memory sliding window rate limiter."""

    def __init__(self):
        self._windows = {}  # key: (scope, window_seconds) → list of timestamps
        self._lock = threading.Lock()

    def check(self, scope, limit, window_seconds):
        """Returns True if allowed, False if rate limited."""
        now = time.time()
        key = (scope, window_seconds)
        with self._lock:
            if key not in self._windows:
                self._windows[key] = []
            # Clean old entries
            self._windows[key] = [t for t in self._windows[key] if t > now - window_seconds]
            if len(self._windows[key]) >= limit:
                return False
            self._windows[key].append(now)
            return True


_rate_limiter = RateLimiter()

# Operations that bypass the OPERATIONS registry (handled by api.py directly)
PASSTHROUGH_OPERATIONS = frozenset({
    "create_amortization", "update_amortization", "delete_amortization",
    "toggle_web_include", "link_amortization_purchase",
})

# Daily budget counter
_daily_budget = {"date": "", "count": 0, "lock": threading.Lock()}


def _check_daily_budget(max_budget=50):
    """Check and increment daily write budget for AI agent. Returns True if allowed."""
    today = time.strftime("%Y-%m-%d")
    with _daily_budget["lock"]:
        if _daily_budget["date"] != today:
            _daily_budget["date"] = today
            _daily_budget["count"] = 0
        if _daily_budget["count"] >= max_budget:
            return False
        _daily_budget["count"] += 1
        return True


# ── Audit Checksum ───────────────────────────────────────────────────

_AUDIT_SECRET = os.getenv("AUDIT_HMAC_SECRET", "").encode() or None

def _compute_checksum(audit_id, timestamp, operation, entity_id, payload):
    """HMAC-SHA256 checksum for audit log tamper detection.
    Falls back to plain SHA-256 if AUDIT_HMAC_SECRET env var is not set."""
    data = f"{audit_id}|{timestamp}|{operation}|{entity_id}|{json.dumps(payload, sort_keys=True)}"
    if _AUDIT_SECRET:
        return hmac.new(_AUDIT_SECRET, data.encode(), hashlib.sha256).hexdigest()
    return hashlib.sha256(data.encode()).hexdigest()


# ── Payload Builders ─────────────────────────────────────────────────

def _build_holded_payload(operation, params):
    """Convert gateway params to Holded API payload format."""
    if operation in ("create_invoice", "create_estimate"):
        items = params.get("items", [])
        products = []
        for item in items:
            p = {"name": _sanitize_text(item.get("name"), 200),
                 "units": item.get("units", 1),
                 "subtotal": item.get("price")}
            if "tax" in item:
                p["tax"] = item["tax"]
            if "desc" in item:
                p["desc"] = _sanitize_text(item["desc"], 500)
            products.append(p)

        payload = {"contactId": params.get("contact_id"), "products": products}
        if params.get("desc"):
            payload["desc"] = _sanitize_text(params["desc"], 1000)
        if params.get("date"):
            payload["date"] = params["date"]
        if params.get("notes"):
            payload["notes"] = _sanitize_text(params["notes"], 2000)
        return payload

    elif operation == "create_contact":
        payload = {"name": _sanitize_text(params.get("name"), 200)}
        # Holded API uses 'code' for NIF/CIF/VAT — map both 'code' and 'vat' params
        for field in ("email", "type", "phone", "mobile", "code", "tradeName", "isperson"):
            if params.get(field):
                payload[field] = params[field]
        # If user provides 'vat' but not 'code', use 'vat' as the Holded 'code' field
        if params.get("vat") and not params.get("code"):
            payload["code"] = params["vat"]
        return payload

    elif operation == "update_document_status":
        return {"status": params.get("status")}

    elif operation == "send_document":
        payload = {"emails": params.get("emails")}
        if params.get("subject"):
            payload["subject"] = _sanitize_text(params["subject"], 200)
        if params.get("message"):
            payload["message"] = _sanitize_text(params["message"], 2000)
        return payload

    return None


def _resolve_endpoint(operation, params):
    """Resolve endpoint template with params. Raises ValueError if required params missing."""
    op = OPERATIONS[operation]
    endpoint = op.get("endpoint", "")
    if "{doc_type}" in endpoint:
        doc_type = params.get("doc_type", "invoice")
        endpoint = endpoint.replace("{doc_type}", doc_type)
    if "{doc_id}" in endpoint:
        doc_id = params.get("doc_id")
        if not doc_id:
            raise ValueError(f"doc_id required for operation {operation}")
        endpoint = endpoint.replace("{doc_id}", doc_id)
    return endpoint


# ── Sync-Back (Async) ────────────────────────────────────────────────

def _sync_back_async(operation, entity_id, params, audit_id):
    """Run sync-back in a background thread."""
    def _do_sync():
        try:
            op = OPERATIONS.get(operation, {})
            sync_type = op.get("sync_type")
            if not sync_type:
                return

            tables = []
            if sync_type == "document":
                doc_type = op.get("sync_doc_type") or params.get("doc_type", "invoice")
                tables = connector.sync_single_document(doc_type, entity_id)
            elif sync_type == "contact":
                tables = connector.sync_single_contact(entity_id)
            elif sync_type == "product":
                tables = connector.sync_single_product(entity_id)

            if tables and audit_id:
                connector.update_audit_log(audit_id, tables_synced=tables)
                logger.info(f"[GATEWAY] Sync-back complete for {operation}/{entity_id}: {tables}")

            # Flush job note queue (if a job was created/updated by this sync)
            try:
                from skills.job_tracker import flush_note_queue
                flush_note_queue()
            except Exception as e:
                logger.warning("Sync-back flush_note_queue failed: %s", e)
        except Exception as e:
            logger.error(f"[GATEWAY] Sync-back failed for {operation}/{entity_id}: {e}")
            if audit_id:
                connector.update_audit_log(audit_id, error_detail=f"Sync-back failed: {e}")

    thread = threading.Thread(target=_do_sync, daemon=True)
    thread.start()


# ── Gateway Class ────────────────────────────────────────────────────

class WriteGateway:
    """Centralized write gateway with 6-stage safety pipeline."""

    def execute(self, operation, params, source="ai_agent",
                conversation_id=None, skip_confirm=False):
        """Execute a write operation through the safety pipeline.

        Args:
            operation: Operation name (e.g., 'create_invoice')
            params: Operation parameters
            source: 'ai_agent' | 'rest_api' | 'cli_script'
            conversation_id: Chat conversation ID (for audit trail)
            skip_confirm: If True, skip confirmation (used after user confirms)

        Returns:
            dict with result data. For AI source without skip_confirm:
            returns preview for confirmation flow.
        """
        start_time = time.time()

        if operation not in OPERATIONS and operation not in PASSTHROUGH_OPERATIONS:
            return {"success": False, "errors": [{"field": "operation", "msg": f"Unknown operation: {operation}"}]}

        # ── Rate Limiting ────────────────────────────────────
        # All sources get basic rate limiting; AI agent has tighter limits + daily budget
        rate_limits = {"ai_agent": (5, 60), "rest_api": (30, 60), "cli_script": (60, 60), "brain_confirmed": (10, 60)}
        limit, window = rate_limits.get(source, (10, 60))
        if not _rate_limiter.check(source, limit, window):
            return {"success": False, "errors": [{"field": "rate_limit", "msg": f"Rate limit exceeded: max {limit} writes per minute for {source}"}]}
        if source == "ai_agent" and not _check_daily_budget():
            return {"success": False, "errors": [{"field": "daily_budget", "msg": "Daily write budget exhausted (max 50 AI writes per day)"}]}

        # ── Stage 1: Validate ────────────────────────────────
        is_valid, errors, context = write_validators.validate(operation, params)
        if not is_valid:
            return {"success": False, "errors": errors}

        # ── Stage 2: Preview + Warnings ──────────────────────
        preview_result = write_preview.build_preview(operation, params, context)

        # ── Stage 3: Confirm or Log ──────────────────────────
        if source == "ai_agent" and not skip_confirm:
            # Return preview for confirmation flow — execution happens later
            return {
                "needs_confirmation": True,
                "preview": preview_result["preview"],
                "warnings": preview_result["warnings"],
                "reversibility": preview_result["reversibility"],
            }

        # ── Insert audit log (pending) ───────────────────────
        holded_payload = _build_holded_payload(operation, params) if operation in OPERATIONS else None
        audit_id = connector.insert_audit_log(
            source=source,
            operation=operation,
            entity_type=OPERATIONS.get(operation, {}).get("entity_type", "unknown"),
            payload_sent=holded_payload,
            preview_data=preview_result,
            warnings=preview_result.get("warnings"),
            status="pending",
            safe_mode=connector.SAFE_MODE,
            conversation_id=conversation_id,
        )

        # ── Stage 4: Execute on Holded API ───────────────────

        # Custom multi-step: convert estimate to invoice
        if operation == "convert_estimate_to_invoice":
            return self._execute_convert_estimate(
                params, context, preview_result, audit_id, start_time, source, conversation_id
            )

        if operation not in OPERATIONS or OPERATIONS[operation].get("method") is None:
            # Local-only operation — mark success immediately
            duration = int((time.time() - start_time) * 1000)
            connector.update_audit_log(audit_id, status="success", duration_ms=duration)
            return {"success": True, "audit_id": audit_id, "local_only": True}

        # Re-validate before execution (TOCTOU protection)
        is_valid2, errors2, _ = write_validators.validate(operation, params)
        if not is_valid2:
            connector.update_audit_log(audit_id, status="failed",
                                        error_detail=f"Re-validation failed: {errors2}")
            return {"success": False, "errors": errors2, "audit_id": audit_id}

        endpoint = _resolve_endpoint(operation, params)
        method = OPERATIONS[operation]["method"]

        if method == "POST":
            result = connector.post_data(endpoint, holded_payload)
        elif method == "PUT":
            result = connector.put_data(endpoint, holded_payload)
        elif method == "DELETE":
            result = connector.delete_data(endpoint)
        else:
            result = None

        # ── Handle result ────────────────────────────────────
        duration = int((time.time() - start_time) * 1000)

        if not result:
            connector.update_audit_log(audit_id, status="failed",
                                        error_detail="No response from Holded API",
                                        duration_ms=duration)
            return {"success": False, "errors": [{"field": "api", "msg": "No response from Holded API"}], "audit_id": audit_id}

        if result.get("error"):
            detail = result.get("detail", "Unknown API error")
            status = "timeout" if "timed out" in str(detail) else "failed"
            connector.update_audit_log(audit_id, status=status,
                                        error_detail=detail,
                                        response_received=result,
                                        duration_ms=duration)
            return {"success": False, "errors": [{"field": "api", "msg": str(detail)}], "audit_id": audit_id}

        # Success
        entity_id = result.get("id", "")
        is_dry_run = result.get("dry_run", False)

        # Build reverse action
        rev = preview_result.get("reversibility", {})
        reverse_action = None
        if rev.get("can_reverse") and entity_id:
            reverse_action = {
                "method": rev.get("method"),
                "endpoint": rev.get("endpoint", "").replace("{id}", entity_id),
            }

        # Compute checksum
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        checksum = _compute_checksum(audit_id, ts, operation, entity_id, holded_payload)

        connector.update_audit_log(
            audit_id,
            status="dry_run" if is_dry_run else "success",
            entity_id=entity_id,
            response_received=result,
            reverse_action=reverse_action,
            checksum=checksum,
            duration_ms=duration,
        )

        # ── Stage 5: Sync-back (async) ───────────────────────
        if not is_dry_run and entity_id:
            _sync_back_async(operation, entity_id, params, audit_id)

        # ── Stage 6: Audit already updated above ─────────────

        # Log pipeline timing
        logger.info(
            f"[GATEWAY] {operation} | duration:{duration}ms | "
            f"status:{'dry_run' if is_dry_run else 'success'} | "
            f"entity:{entity_id}"
        )

        return {
            "success": True,
            "entity_id": entity_id,
            "entity_type": OPERATIONS[operation]["entity_type"],
            "doc_number": result.get("invoiceNum", ""),
            "audit_id": audit_id,
            "safe_mode": is_dry_run,
        }

    def _execute_convert_estimate(self, params, context, preview_result,
                                   audit_id, start_time, source, conversation_id):
        """Convert an estimate to an invoice (multi-step execution).

        Steps:
        1. Create invoice as borrador with estimate's items
        2. Approve the estimate (status changes are safe for quotes)
        3. Mark estimate as invoiced (status 4)
        4. Sync-back the new invoice
        """
        estimate = context.get("estimate", {})
        estimate_items = context.get("estimate_items", [])
        estimate_id = estimate.get("id")

        # Build invoice payload from estimate data
        products = []
        for item in estimate_items:
            p = {
                "name": _sanitize_text(item.get("name"), 200),
                "units": item.get("units", 1),
                "subtotal": item.get("price", 0),
            }
            if item.get("tax") is not None:
                p["tax"] = item["tax"]
            if item.get("desc"):
                p["desc"] = _sanitize_text(item["desc"], 500)
            if item.get("sku"):
                p["sku"] = item["sku"]
            products.append(p)

        invoice_payload = {
            "contactId": estimate.get("contact_id"),
            "products": products,
        }
        if estimate.get("desc"):
            invoice_payload["desc"] = _sanitize_text(estimate["desc"], 1000)
        if estimate.get("notes"):
            invoice_payload["notes"] = _sanitize_text(estimate["notes"], 2000)
        if estimate.get("date"):
            invoice_payload["date"] = estimate["date"]

        # Step 1: Create invoice as borrador (never approveDoc!)
        result = connector.post_data("/invoicing/v1/documents/invoice", invoice_payload)

        duration = int((time.time() - start_time) * 1000)

        if not result or result.get("error"):
            detail = result.get("detail", "No response") if result else "No response from Holded API"
            connector.update_audit_log(audit_id, status="failed",
                                        error_detail=f"Invoice creation failed: {detail}",
                                        duration_ms=duration)
            return {"success": False, "errors": [{"field": "api", "msg": str(detail)}], "audit_id": audit_id}

        invoice_id = result.get("id", "")
        is_dry_run = result.get("dry_run", False)

        # Step 2+3: Approve estimate and mark as invoiced (safe — quotes don't go to Hacienda)
        if not is_dry_run and estimate_id:
            # Transition estimate through valid states to invoiced (4)
            # Current status → need to reach 4: accepted(2) → invoiced(4)
            current_status = estimate.get("status", 0)
            transitions_needed = []
            if current_status == 0:
                transitions_needed = [1, 2, 4]  # draft → pending → accepted → invoiced
            elif current_status == 1:
                transitions_needed = [2, 4]      # pending → accepted → invoiced
            elif current_status == 2:
                transitions_needed = [4]          # accepted → invoiced
            # status 3 (rejected) or 4 (already invoiced) — skip

            for new_status in transitions_needed:
                connector.put_data(
                    f"/invoicing/v1/documents/estimate/{estimate_id}",
                    {"status": new_status}
                )

        # Update audit log
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        checksum = _compute_checksum(audit_id, ts, "convert_estimate_to_invoice",
                                      invoice_id, invoice_payload)

        connector.update_audit_log(
            audit_id,
            status="dry_run" if is_dry_run else "success",
            entity_id=invoice_id,
            response_received=result,
            checksum=checksum,
            duration_ms=duration,
        )

        # Sync-back the new invoice
        if not is_dry_run and invoice_id:
            _sync_back_async("convert_estimate_to_invoice", invoice_id, {}, audit_id)

        logger.info(
            f"[GATEWAY] convert_estimate_to_invoice | estimate:{estimate_id} → "
            f"invoice:{invoice_id} | duration:{duration}ms | "
            f"status:{'dry_run' if is_dry_run else 'success'}"
        )

        return {
            "success": True,
            "entity_id": invoice_id,
            "entity_type": "invoice",
            "doc_number": result.get("invoiceNum", ""),
            "estimate_id": estimate_id,
            "audit_id": audit_id,
            "safe_mode": is_dry_run,
        }


# ── Singleton ────────────────────────────────────────────────────────

gateway = WriteGateway()
