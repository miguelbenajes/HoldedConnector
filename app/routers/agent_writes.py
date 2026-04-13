"""
Agent Write router — /api/agent/* endpoints.

Fase 3 gateway-migrated write operations: create/update invoices, estimates,
contacts, purchases, approve invoices, send documents, convert estimates,
file attachments.
Extracted from api.py (Fase 4 router split, Task 10).
"""
from fastapi import APIRouter, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional, List
import connector
import os
import re as _re_mod
import requests
import logging
import time as _time
from app.db.connection import db_context, _q, _cursor
from app.domain.item_builder import build_holded_items, build_holded_items_with_accounts
from app.holded.write_wrappers import (
    create_purchase, attach_file_to_document, compute_file_hash,
    ALLOWED_ATTACH_TYPES, MAX_ATTACH_SIZE,
)
from app.holded.sync_single import sync_single_document
from write_gateway import gateway, RateLimiter

logger = logging.getLogger(__name__)
router = APIRouter()

# Feature flag: route /api/agent/* endpoints through Write Gateway (default: true)
# Set USE_GATEWAY_FOR_AGENT=false to rollback to direct connector calls (requires restart)
_USE_GATEWAY = os.getenv("USE_GATEWAY_FOR_AGENT", "true").lower() == "true"

# Rate limiter for approve_invoice endpoint (1/min safety gate, separate from gateway limits)
_approve_limiter = RateLimiter()


def _gw_error(result, default="Operation failed"):
    """Extract first error message from a gateway result dict."""
    errs = result.get("errors")
    return errs[0].get("msg", default) if errs else default


def _enrich_response(result, doc_type):
    """Add holded_url and warnings to gateway success response."""
    entity_id = result.get("entity_id", "")
    response = {
        "success": True,
        "id": entity_id,
        "safe_mode": result.get("safe_mode", False),
    }
    if entity_id:
        plural = "estimates" if doc_type == "estimate" else "invoices"
        response["holded_url"] = f"https://app.holded.com/invoicing/{plural}/{entity_id}"
    if result.get("warnings"):
        response["warnings"] = result["warnings"]
    return response


# ── Pydantic Models ─────────────────────────────────────────────────────

class CreateDocumentBody(BaseModel):
    contact_id: str = Field(..., max_length=100)
    desc: Optional[str] = Field("", max_length=500)
    items: list = Field(..., max_length=100)  # [{name, units, price, tax?}]
    date: Optional[int] = Field(None, description="Unix timestamp for document date. If omitted, defaults to now.")

class CreateContactBody(BaseModel):
    name: str = Field(..., max_length=200)
    email: Optional[str] = Field(None, max_length=200)
    phone: Optional[str] = Field(None, max_length=50)
    vatnumber: Optional[str] = Field(None, max_length=50)  # mapped to "code" for Holded API
    type: Optional[str] = Field("client", max_length=20)

class UpdateStatusBody(BaseModel):
    status: int = Field(..., ge=0, le=5)  # 0=draft, 1=issued, 3=paid, 5=cancelled

class SendDocumentBody(BaseModel):
    emails: Optional[list] = Field(None, max_length=20)
    subject: Optional[str] = Field(None, max_length=300)
    body: Optional[str] = Field(None, max_length=5000)

class UpdateContactBody(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    vatnumber: Optional[str] = None
    phone: Optional[str] = None
    country: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    postal_code: Optional[str] = None
    province: Optional[str] = None
    trade_name: Optional[str] = None
    discount: Optional[float] = None

class ConvertEstimateBody(BaseModel):
    estimate_id: str = Field(..., min_length=24, max_length=24, pattern=r'^[a-f0-9]{24}$')


# ── Endpoints ───────────────────────────────────────────────────────────

@router.post("/api/agent/invoice")
def agent_create_invoice(body: CreateDocumentBody):
    if not _USE_GATEWAY:
        import time as _time
        items_out = build_holded_items(body.items, sanitize=False, apply_default_iva=True)
        payload = {"contactId": body.contact_id, "desc": body.desc, "items": items_out, "date": int(_time.time())}
        result = connector.create_invoice(payload)
        safe = connector.SAFE_MODE
        if result and not isinstance(result, dict):
            return {"success": True, "id": result, "safe_mode": safe}
        if isinstance(result, dict) and result.get("error"):
            return {"success": False, "error": f"Failed to create invoice: {result.get('detail', 'Unknown error')}", "safe_mode": safe}
        return {"success": False, "error": "Failed to create invoice", "safe_mode": safe}
    params = {"contact_id": body.contact_id, "desc": body.desc, "items": body.items, "date": body.date}
    result = gateway.execute("create_invoice", params, source="rest_api", skip_confirm=True)
    if result.get("success"):
        return _enrich_response(result, "invoice")
    return {"success": False, "error": _gw_error(result, "Failed to create invoice"), "safe_mode": connector.SAFE_MODE}

# ACCOUNT_IDS and _resolve_account moved to app.domain.item_builder

@router.post("/api/agent/estimate")
def agent_create_estimate(body: CreateDocumentBody):
    if not _USE_GATEWAY:
        import time as _time
        items_out = build_holded_items(body.items, sanitize=False, apply_default_iva=True)
        payload = {"contactId": body.contact_id, "desc": body.desc, "items": items_out, "date": int(_time.time())}
        result = connector.create_estimate(payload)
        safe = connector.SAFE_MODE
        if result and not isinstance(result, dict):
            return {"success": True, "id": result, "safe_mode": safe}
        if isinstance(result, dict) and result.get("error"):
            return {"success": False, "error": f"Failed to create estimate: {result.get('detail', 'Unknown error')}", "safe_mode": safe}
        return {"success": False, "error": "Failed to create estimate", "safe_mode": safe}
    params = {"contact_id": body.contact_id, "desc": body.desc, "items": body.items, "date": body.date}
    result = gateway.execute("create_estimate", params, source="rest_api", skip_confirm=True)
    if result.get("success"):
        return _enrich_response(result, "estimate")
    return {"success": False, "error": _gw_error(result, "Failed to create estimate"), "safe_mode": connector.SAFE_MODE}

@router.put("/api/agent/estimate/{estimate_id}")
def agent_update_estimate(estimate_id: str, body: CreateDocumentBody):
    """Update an estimate's products in Holded. Used by job-automation after YES confirmation."""
    if not _re_mod.match(r'^[a-f0-9]{24}$', estimate_id):
        return JSONResponse({"error": "Invalid estimate ID"}, status_code=400)
    if not _USE_GATEWAY:
        items_list = build_holded_items_with_accounts(body.items, sanitize=False)
        payload = {"items": items_list}
        if body.contact_id:
            payload["contactId"] = body.contact_id
        result = connector.update_estimate(estimate_id, payload)
        if result:
            return {"success": True, "estimate_id": estimate_id}
        return {"success": False, "error": "Failed to update estimate"}
    params = {"items": body.items, "doc_id": estimate_id}
    if body.contact_id:
        params["contact_id"] = body.contact_id
    result = gateway.execute("update_estimate_items", params, source="rest_api", skip_confirm=True)
    if result.get("success"):
        return {"success": True, "estimate_id": estimate_id, "id": estimate_id}
    return {"success": False, "error": _gw_error(result, "Failed to update estimate")}


@router.post("/api/agent/contact")
def agent_create_contact(body: CreateContactBody):
    if not _USE_GATEWAY:
        payload = {"name": body.name}
        for key in ["email", "phone", "type"]:
            val = getattr(body, key, None)
            if val:
                payload[key] = val
        if body.vatnumber:
            payload["code"] = body.vatnumber
        result = connector.create_contact(payload)
        safe = connector.SAFE_MODE
        if result and not isinstance(result, dict):
            return {"success": True, "id": result, "safe_mode": safe}
        if isinstance(result, dict) and result.get("error"):
            return {"success": False, "error": f"Failed to create contact: {result.get('detail', 'Unknown error')}", "safe_mode": safe}
        return {"success": False, "error": "Failed to create contact", "safe_mode": safe}
    params = {"name": body.name}
    for key in ["email", "phone", "type"]:
        val = getattr(body, key, None)
        if val:
            params[key] = val
    if body.vatnumber:
        params["vat"] = body.vatnumber
    result = gateway.execute("create_contact", params, source="rest_api", skip_confirm=True)
    if result.get("success"):
        return {"success": True, "id": result.get("entity_id", ""), "safe_mode": result.get("safe_mode", False)}
    return {"success": False, "error": _gw_error(result, "Failed to create contact"), "safe_mode": connector.SAFE_MODE}


@router.put("/api/agent/invoice/{invoice_id}/approve")
def agent_approve_invoice(invoice_id: str, request: Request):
    """Approve a draft invoice — assigns number, locks editing.
    CRITICAL: This submits the invoice to Hacienda/SII. Irreversible and legally binding.
    Requires X-Confirm-Hacienda: true header."""
    if not _re_mod.match(r'^[a-zA-Z0-9]+$', invoice_id):
        return {"success": False, "error": "Invalid invoice ID"}

    # Safety: require explicit confirmation header (REST-only gate, not in gateway)
    if request.headers.get("x-confirm-hacienda") != "true":
        return JSONResponse(status_code=400, content={
            "success": False,
            "error": "Invoice approval submits to Hacienda/SII (irreversible). "
                     "Set header X-Confirm-Hacienda: true to confirm."
        })

    # Rate limit: max 1 invoice approval per minute (safety gate)
    if not _approve_limiter.check("approve_invoice", 1, 60):
        return JSONResponse(status_code=429, content={
            "success": False,
            "error": "Rate limit: max 1 invoice approval per minute"
        })

    if not _USE_GATEWAY:
        audit_id = connector.insert_audit_log(
            source="rest_api", operation="approve_invoice", entity_type="invoice",
            payload_sent={"approveDoc": True, "invoice_id": invoice_id},
        )
        logger.warning(f"[APPROVE] Invoice {invoice_id} approval requested — Hacienda/SII submission (audit:{audit_id})")
        result = connector.holded_put(f"/invoicing/v1/documents/invoice/{invoice_id}", {"approveDoc": True})
        if result and result.get("status") == 1:
            connector.update_audit_log(audit_id, status="success", entity_id=invoice_id, response_received=result)
            return {
                "success": True, "info": "Invoice approved",
                "hacienda_warning": True,
                "hacienda_detail": "Invoice submitted to Hacienda/SII. This is irreversible.",
                "audit_id": audit_id,
            }
        error_msg = result.get("info", "Failed to approve") if result else "No response from Holded API"
        connector.update_audit_log(audit_id, status="failed", error_detail=error_msg)
        return {"success": False, "error": error_msg, "audit_id": audit_id}

    # Gateway path
    logger.warning(f"[APPROVE] Invoice {invoice_id} approval requested — Hacienda/SII submission (via gateway)")
    params = {"doc_id": invoice_id}
    result = gateway.execute("approve_invoice", params, source="rest_api", skip_confirm=True)
    if result.get("success"):
        return {
            "success": True, "info": "Invoice approved",
            "hacienda_warning": True,
            "hacienda_detail": "Invoice submitted to Hacienda/SII. This is irreversible.",
            "audit_id": result.get("audit_id", ""),
        }
    return {"success": False, "error": _gw_error(result, "Failed to approve"), "audit_id": result.get("audit_id", "")}



@router.get("/api/agent/contact/{contact_id}")
def agent_get_contact(contact_id: str):
    """Get full contact details + check for missing required fields."""
    if not _re_mod.match(r'^[a-zA-Z0-9]+$', contact_id):
        return {"success": False, "error": "Invalid contact ID"}
    with db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,))
        row = cursor.fetchone()
        if not row:
            return {"success": False, "error": "Contact not found"}
        contact = dict(row)

    # Check missing fields
    missing = []
    if not contact.get("code"): missing.append("NIF/CIF (code)")
    if not contact.get("email"): missing.append("email")
    if not contact.get("country"): missing.append("country (pais)")
    if not contact.get("address"): missing.append("address (direccion)")

    # Determine tax regime
    country = (contact.get("country") or "").upper()
    eu_countries = {"DE","FR","IT","NL","BE","PT","AT","IE","FI","SE","DK","PL","CZ","SK","HU","RO","BG","HR","SI","EE","LV","LT","LU","MT","CY","GR"}
    if country == "ES":
        tax_regime = "spain"
    elif country in eu_countries:
        tax_regime = "eu"
    elif country:
        tax_regime = "extra_eu"
    else:
        tax_regime = "unknown"

    return {
        "success": True,
        "contact": contact,
        "missing_fields": missing,
        "tax_regime": tax_regime,
        "tax_note": {
            "spain": "IVA 21% + IRPF segun tipo item",
            "eu": "Sin IVA, sin IRPF (intracomunitario). Requiere VAT valido.",
            "extra_eu": "Sin IVA, sin IRPF (exportacion)",
            "unknown": "Pais desconocido — preguntar a Miguel"
        }.get(tax_regime, "")
    }


@router.put("/api/agent/contact/{contact_id}")
def agent_update_contact(contact_id: str, body: UpdateContactBody):
    """Update contact fields in Holded + local DB."""
    if not _re_mod.match(r'^[a-zA-Z0-9]+$', contact_id):
        return {"success": False, "error": "Invalid contact ID"}

    # Build Holded API payload
    payload = {}
    if body.name: payload["name"] = body.name
    if body.email: payload["email"] = body.email
    if body.phone: payload["phone"] = body.phone
    if body.trade_name: payload["tradeName"] = body.trade_name
    if body.vatnumber: payload["code"] = body.vatnumber
    if body.discount is not None: payload["discount"] = body.discount

    # Address fields
    addr = {}
    if body.country: addr["country"] = body.country
    if body.address: addr["address"] = body.address
    if body.city: addr["city"] = body.city
    if body.postal_code: addr["postalCode"] = body.postal_code
    if body.province: addr["province"] = body.province
    if addr:
        payload["billAddress"] = addr

    if not payload:
        return {"success": False, "error": "No fields to update"}

    # Update in Holded
    import requests as _req
    resp = _req.put(
        f"https://api.holded.com/api/invoicing/v1/contacts/{contact_id}",
        headers={"key": connector.API_KEY, "Content-Type": "application/json"},
        json=payload
    )

    if resp.status_code != 200 or resp.json().get("status") != 1:
        return {"success": False, "error": f"Holded update failed: {resp.text[:200]}"}

    # Trigger sync to update local DB
    try:
        connector.sync_contacts()
    except Exception:
        pass

    return {"success": True, "updated_fields": list(payload.keys())}


@router.put("/api/agent/invoice/{invoice_id}/status")
def agent_update_invoice_status(invoice_id: str, body: UpdateStatusBody):
    if not _re_mod.match(r'^[a-zA-Z0-9]+$', invoice_id):
        return {"success": False, "error": "Invalid invoice ID"}
    if not _USE_GATEWAY:
        result = connector.put_data(
            f"/invoicing/v1/documents/invoice/{invoice_id}",
            {"status": body.status}
        )
        safe = connector.SAFE_MODE
        logger.info(f"[agent] Invoice {invoice_id} status→{body.status} result: {result}")
        if result and not result.get("error"):
            return {"success": True, "safe_mode": safe}
        detail = result.get("detail", "Unknown error") if result else "No response"
        return {"success": False, "error": f"Failed to update status: {detail}", "safe_mode": safe}
    params = {"doc_type": "invoice", "doc_id": invoice_id, "status": body.status}
    result = gateway.execute("update_document_status", params, source="rest_api", skip_confirm=True)
    logger.info(f"[agent] Invoice {invoice_id} status→{body.status} via gateway: {result.get('success')}")
    if result.get("success"):
        return {"success": True, "safe_mode": result.get("safe_mode", False)}
    return {"success": False, "error": f"Failed to update status: {_gw_error(result, 'Unknown error')}", "safe_mode": connector.SAFE_MODE}

@router.post("/api/agent/send/{doc_type}/{doc_id}")
def agent_send_document(doc_type: str, doc_id: str, body: SendDocumentBody):
    allowed_types = {"invoice", "purchase", "estimate", "creditnote", "proforma"}
    if doc_type not in allowed_types:
        return {"success": False, "error": "Invalid document type"}
    if not _re_mod.match(r'^[a-zA-Z0-9]+$', doc_id):
        return {"success": False, "error": "Invalid document ID"}
    if not _USE_GATEWAY:
        payload = {}
        if body.emails:
            payload["emails"] = body.emails
        if body.subject:
            payload["subject"] = body.subject
        if body.body:
            payload["body"] = body.body
        result = connector.post_data(
            f"/invoicing/v1/documents/{doc_type}/{doc_id}/send",
            payload
        )
        safe = connector.SAFE_MODE
        if result and not result.get("error"):
            return {"success": True, "safe_mode": safe}
        detail = result.get("detail", "Unknown error") if result else "No response"
        return {"success": False, "error": f"Failed to send document: {detail}", "safe_mode": safe}
    params = {"doc_type": doc_type, "doc_id": doc_id}
    if body.emails:
        params["emails"] = body.emails
    if body.subject:
        params["subject"] = body.subject
    if body.body:
        params["message"] = body.body  # gateway uses "message", not "body"
    result = gateway.execute("send_document", params, source="rest_api", skip_confirm=True)
    if result.get("success"):
        return {"success": True, "safe_mode": result.get("safe_mode", False)}
    return {"success": False, "error": f"Failed to send document: {_gw_error(result, 'Unknown error')}", "safe_mode": connector.SAFE_MODE}


@router.post("/api/agent/convert-estimate")
def agent_convert_estimate(body: ConvertEstimateBody):
    """Convert an estimate to a draft invoice via the Safe Write Gateway."""
    result = gateway.execute(
        "convert_estimate_to_invoice",
        {"estimate_id": body.estimate_id},
        source="rest_api",
        skip_confirm=True,
    )
    if result.get("success"):
        return {
            "success": True,
            "invoice_id": result.get("result", {}).get("invoice_id", ""),
        }
    return JSONResponse(
        status_code=400,
        content={
            "success": False,
            "error": result.get("error", "Conversion failed"),
            "errors": result.get("errors", []),
        }
    )


# ── Purchase (expense) endpoints ───────────────────────────────────────────

class CreatePurchaseBody(BaseModel):
    contact_id: Optional[str] = Field(None, max_length=100)
    contact_name: Optional[str] = Field(None, max_length=200)
    desc: Optional[str] = Field("", max_length=500)
    notes: Optional[str] = Field(None, max_length=2000)
    items: list = Field(..., max_length=100)
    date: Optional[int] = None
    tags: Optional[list] = None


@router.post("/api/agent/purchase")
def agent_create_purchase(body: CreatePurchaseBody):
    """Create a purchase (supplier invoice / expense) in Holded.

    Always created as draft. Never approveDoc.
    Requires at least contact_id or contact_name.
    """
    if not body.contact_id and not body.contact_name:
        return JSONResponse(status_code=400, content={
            "success": False, "error": "Either contact_id or contact_name is required"
        })

    if not _USE_GATEWAY:
        items_out = build_holded_items(body.items, sanitize=False, apply_default_iva=True)
        payload = {
            "items": items_out,
            "date": body.date or int(_time.time()),
        }
        if body.contact_id:
            payload["contactId"] = body.contact_id
        if body.contact_name:
            payload["contactName"] = body.contact_name
        if body.desc:
            payload["desc"] = body.desc
        if body.notes:
            payload["notes"] = body.notes
        if body.tags:
            payload["tags"] = body.tags

        result = create_purchase(payload)
        safe = connector.SAFE_MODE
        if result and not isinstance(result, dict):
            # Sync back the new purchase
            try:
                sync_single_document(result, "purchase", "purchase_invoices", "purchase_items", "purchase_id")
            except Exception as e:
                logger.warning(f"Sync-back failed for purchase {result}: {e}")
            return {"success": True, "id": result, "safe_mode": safe}
        if isinstance(result, dict) and result.get("error"):
            return {"success": False, "error": f"Failed to create purchase: {result.get('detail', 'Unknown error')}", "safe_mode": safe}
        return {"success": False, "error": "Failed to create purchase", "safe_mode": safe}

    params = {
        "items": body.items,
        "date": body.date or int(_time.time()),
    }
    if body.contact_id:
        params["contact_id"] = body.contact_id
    if body.contact_name:
        params["contact_name"] = body.contact_name
    if body.desc:
        params["desc"] = body.desc
    if body.notes:
        params["notes"] = body.notes
    if body.tags:
        params["tags"] = body.tags

    result = gateway.execute("create_purchase", params, source="rest_api", skip_confirm=True)
    if result.get("success"):
        return {"success": True, "id": result.get("entity_id", ""), "safe_mode": result.get("safe_mode", False)}
    return {"success": False, "error": _gw_error(result, "Failed to create purchase"), "safe_mode": connector.SAFE_MODE}


# ── Document attachment endpoints ──────────────────────────────────────────

@router.post("/api/agent/document/{doc_type}/{doc_id}/attach")
async def agent_attach_file(doc_type: str, doc_id: str, file: UploadFile = File(...)):
    """Attach a file to a Holded document.

    Accepts multipart/form-data with a 'file' field.
    Supported types: JPEG, PNG, PDF. Max size: 20MB.

    Holded API limitations (confirmed probe 2026-04-11):
    - No list/delete attachment API
    - No attachment ID returned
    - Attachments only visible in Holded web UI
    - We track uploads locally in file_attachments table
    """
    # Validate doc_type
    allowed_types = {"invoice", "estimate", "purchase", "creditnote", "proform"}
    if doc_type not in allowed_types:
        return JSONResponse(status_code=400, content={
            "success": False, "error": f"Invalid doc_type. Allowed: {', '.join(sorted(allowed_types))}"
        })

    # Validate doc_id format
    if not _re_mod.match(r'^[a-f0-9]{24}$', doc_id):
        return JSONResponse(status_code=400, content={
            "success": False, "error": "Invalid document ID (must be 24-char hex)"
        })

    # Validate content type
    content_type = file.content_type or "application/octet-stream"
    if content_type not in ALLOWED_ATTACH_TYPES:
        return JSONResponse(status_code=400, content={
            "success": False,
            "error": f"File type '{content_type}' not supported. Allowed: JPEG, PNG, PDF"
        })

    # Read file content
    file_bytes = await file.read()

    if len(file_bytes) == 0:
        return JSONResponse(status_code=400, content={
            "success": False, "error": "Empty file"
        })

    if len(file_bytes) > MAX_ATTACH_SIZE:
        mb = len(file_bytes) / (1024 * 1024)
        return JSONResponse(status_code=400, content={
            "success": False, "error": f"File too large ({mb:.1f}MB). Max: 20MB"
        })

    filename = file.filename or "attachment"

    # Check for duplicate (same file + same document)
    file_hash = compute_file_hash(file_bytes)
    with db_context() as conn:
        cur = _cursor(conn)
        cur.execute(_q("SELECT id FROM file_attachments WHERE file_hash = ? AND document_id = ?"),
                     (file_hash, doc_id))
        existing = cur.fetchone()
        if existing:
            return JSONResponse(status_code=409, content={
                "success": False,
                "error": "This file is already attached to this document",
                "hash": file_hash,
            })

    # Upload to Holded
    result = attach_file_to_document(doc_type, doc_id, filename, file_bytes, content_type)

    if result and not result.get("error"):
        # Track locally
        try:
            with db_context() as conn:
                cur = _cursor(conn)
                cur.execute(_q(
                    "INSERT INTO file_attachments (file_hash, document_type, document_id, filename, content_type, file_size) "
                    "VALUES (?, ?, ?, ?, ?, ?)"
                ), (file_hash, doc_type, doc_id, filename, content_type, len(file_bytes)))
                conn.commit()
        except Exception as e:
            logger.warning(f"Failed to track attachment locally: {e}")

        return {
            "success": True,
            "hash": file_hash,
            "filename": result.get("filename", filename),
            "size": len(file_bytes),
            "safe_mode": result.get("dry_run", False),
        }

    error_detail = result.get("detail", "Unknown error") if result else "No response"
    return JSONResponse(status_code=500, content={
        "success": False, "error": f"Failed to attach file: {error_detail}"
    })


@router.get("/api/agent/document/{doc_type}/{doc_id}/attachments")
def agent_list_attachments(doc_type: str, doc_id: str):
    """List locally-tracked attachments for a document.

    NOTE: This queries the local file_attachments table, NOT the Holded API
    (which has no list endpoint). Only shows files uploaded through this API.
    """
    if not _re_mod.match(r'^[a-f0-9]{24}$', doc_id):
        return JSONResponse(status_code=400, content={
            "success": False, "error": "Invalid document ID"
        })

    with db_context() as conn:
        cur = _cursor(conn)
        cur.execute(_q(
            "SELECT file_hash, filename, content_type, file_size, uploaded_at, uploaded_by "
            "FROM file_attachments WHERE document_type = ? AND document_id = ? "
            "ORDER BY uploaded_at DESC"
        ), (doc_type, doc_id))
        rows = cur.fetchall()

    attachments = [dict(r) for r in rows]
    return {"success": True, "count": len(attachments), "attachments": attachments}
