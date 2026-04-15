# app/domain/smart_estimate.py
"""Smart Estimate orchestrator.

Receives minimal input (client name, product names, shooting date),
resolves everything deterministically, creates a complete estimate in Holded.
Zero LLM involvement in business logic.
"""

import re
import time
import base64
import logging
import requests as _requests
from datetime import datetime, timezone

from app.domain.fiscal_rules import (
    determine_tax_regime,
    compute_item_fiscality,
    generate_project_code,
)
from app.domain.contact_resolver import (
    resolve_contact,
    resolve_contact_by_id,
    validate_contact_fiscal,
)
from app.domain.product_resolver import resolve_products
from app.domain.item_builder import build_holded_items_with_accounts
from app.holded.client import (
    post_data,
    PROYECTO_PRODUCT_ID,
    SHOOTING_DATES_PRODUCT_ID,
)
from app.db.connection import HEADERS, BASE_URL  # [IMPROVED: finding #C5]

logger = logging.getLogger(__name__)


def create_smart_estimate(input_data: dict) -> dict:
    """Create a complete estimate from minimal input.

    Args:
        input_data: {
            client_name: str (or client_id: str),
            products: [{name, units?, price?, item_type?, create_if_missing?}],
            shooting_date: str,
            notes: str (optional)
        }

    Returns:
        Success: {success: True, id, holded_url, pdf_base64, project_code, contact, items_summary, totals, warnings, safe_mode}
        Error: {success: False, error_type, error, details?}
    """
    warnings = []

    # ── Step 1: Resolve contact ──────────────────────────────────────
    client_id = input_data.get("client_id")
    client_name = input_data.get("client_name")

    if not client_id and not client_name:
        return _error("validation_failed", "Either client_name or client_id is required")

    if client_id:
        contact_result = resolve_contact_by_id(client_id)
    else:
        contact_result = resolve_contact(client_name)

    if not contact_result.get("ok"):
        return _error(contact_result.get("error_type", "contact_not_found"),
                      contact_result.get("error", "Contact resolution failed"),
                      contact_result.get("matches"))

    contact = contact_result["contact"]

    # Validate fiscal completeness
    missing = validate_contact_fiscal(contact)
    if missing:
        return _error("contact_incomplete",
                      f"Contact '{contact['name']}' is missing: {', '.join(missing)}. "
                      f"Update in Holded before creating estimates.")

    # ── Step 2: Resolve products ─────────────────────────────────────
    products_result = resolve_products(input_data.get("products", []))

    if not products_result.get("ok"):
        return _error(products_result.get("error_type", "product_not_found"),
                      products_result.get("error", products_result.get("message", "Product resolution failed")),
                      products_result.get("details"))

    resolved_items = products_result.get("items", [])

    # ── Step 3: Compute fiscality per item ───────────────────────────
    tax_regime = determine_tax_regime(contact.get("country", ""))
    items_for_holded = []

    # [IMPROVED: finding #M1] — compute fiscality once per item, store result
    item_fiscalities = []
    for item in resolved_items:
        fiscal = compute_item_fiscality(item["item_type"], tax_regime)
        item_fiscalities.append(fiscal)
        holded_item = {
            "name": item.get("name", ""),
            "units": item.get("units", 1),
            "price": item.get("price", 0),
            "tax": fiscal["tax"],
            "retention": fiscal["retention"],
        }
        if fiscal["account_id"]:
            holded_item["account"] = fiscal["account_id"]
        if item.get("product_id"):
            holded_item["productId"] = item["product_id"]
        if item.get("service_id"):
            holded_item["serviceId"] = item["service_id"]
        items_for_holded.append(holded_item)

    # ── Step 4: Add mandatory tracking items ─────────────────────────
    shooting_date = input_data.get("shooting_date", "")
    project_code = generate_project_code(
        contact["name"] if not client_name else client_name,
        shooting_date,
    )

    items_for_holded.append({
        "name": "Proyect REF:",
        "units": 1,
        "price": 0,
        "tax": 0,
        "desc": project_code,
        "productId": PROYECTO_PRODUCT_ID,
    })
    items_for_holded.append({
        "name": "Shooting Dates:",
        "units": 1,
        "price": 0,
        "tax": 0,
        "desc": shooting_date,
        "productId": SHOOTING_DATES_PRODUCT_ID,
    })

    # ── Step 5: Build Holded payload ─────────────────────────────────
    holded_items = build_holded_items_with_accounts(items_for_holded)

    # Parse shooting date to unix timestamp for document date
    doc_date = _parse_date_to_timestamp(shooting_date)
    if not doc_date:
        doc_date = int(time.time())
        warnings.append({"code": "DATE_PARSE_FAILED",
                         "msg": f"Could not parse '{shooting_date}' as date. Using today."})

    payload = {
        "contactId": contact["id"],
        "desc": input_data.get("notes", ""),
        "items": holded_items,
        "date": doc_date,
    }

    # ── Step 6: Create in Holded ─────────────────────────────────────
    result = post_data("/invoicing/v1/documents/estimate", payload)

    # [IMPROVED: finding #C5] — propagate safe_mode from post_data() response
    is_safe_mode = False
    if isinstance(result, dict) and result.get("dry_run"):
        is_safe_mode = True

    # post_data() returns {"error": True, "detail": ...} on HTTP/network errors.
    # Do NOT check status==0 — that is not used as an error indicator in client.py.
    if not result or (isinstance(result, dict) and result.get("error")):
        detail = result.get("detail", "unknown") if isinstance(result, dict) else str(result)
        return _error("holded_api_error", f"Holded API error: {detail}")

    estimate_id = result.get("id", "") if isinstance(result, dict) else str(result)
    if not estimate_id:
        return _error("holded_api_error", "Holded returned success but no estimate ID")

    # ── Step 7: Download PDF ─────────────────────────────────────────
    # [IMPROVED: finding #H7] — use direct requests.get() for binary PDF,
    # NOT fetch_data() which parses JSON and would corrupt binary content.
    pdf_base64 = ""
    if not is_safe_mode:
        try:
            pdf_url = f"{BASE_URL}/invoicing/v1/documents/estimate/{estimate_id}/pdf"
            pdf_resp = _requests.get(pdf_url, headers=HEADERS, timeout=30)
            if pdf_resp.status_code == 200 and pdf_resp.content:
                pdf_base64 = base64.b64encode(pdf_resp.content).decode()
            else:
                logger.warning(f"PDF download returned {pdf_resp.status_code} for {estimate_id}")
                warnings.append({"code": "PDF_DOWNLOAD_FAILED",
                                 "msg": f"PDF returned status {pdf_resp.status_code}. Fetch manually."})
        except Exception as e:
            logger.warning(f"PDF download failed for {estimate_id}: {e}")
            warnings.append({"code": "PDF_DOWNLOAD_FAILED",
                             "msg": "PDF download failed. Fetch manually."})

    # ── Step 8: Build response ───────────────────────────────────────
    holded_url = f"https://app.holded.com/sales/estimates#open:estimate-{estimate_id}"

    # [IMPROVED: finding #M1] — reuse pre-computed fiscalities instead of calling 4x per item
    subtotal = sum(item.get("price", 0) * item.get("units", 1) for item in resolved_items)
    iva_total = sum(
        item.get("price", 0) * item.get("units", 1) * fiscal["tax"] / 100
        for item, fiscal in zip(resolved_items, item_fiscalities)
    )
    irpf_total = sum(
        item.get("price", 0) * item.get("units", 1) * fiscal["retention"] / 100
        for item, fiscal in zip(resolved_items, item_fiscalities)
    )

    return {
        "success": True,
        "id": estimate_id,
        "holded_url": holded_url,
        "pdf_base64": pdf_base64,
        "project_code": project_code,
        "contact": {
            "name": contact.get("name", ""),
            "country": contact.get("country", ""),
            "tax_regime": tax_regime,
        },
        "items_summary": [
            {"name": item.get("name", ""), "units": item.get("units", 1), "price": item.get("price", 0),
             "tax": fiscal["tax"], "retention": fiscal["retention"]}
            for item, fiscal in zip(resolved_items, item_fiscalities)
        ],
        "totals": {
            "subtotal": round(subtotal, 2),
            "iva": round(iva_total, 2),
            "irpf": round(-irpf_total, 2),
            "total": round(subtotal + iva_total - irpf_total, 2),
        },
        "warnings": warnings,
        "safe_mode": is_safe_mode,  # [IMPROVED: finding #C5] — propagated from post_data()
    }


def _error(error_type: str, error: str, details=None) -> dict:
    return {"success": False, "error_type": error_type,
            "error": error, "details": details or {}}


def _parse_date_to_timestamp(date_str: str) -> int | None:
    """Parse European date string to Unix timestamp. Returns None on failure."""
    # Try DD-DD/MM/YYYY (range — use first date) — try range pattern FIRST
    m2 = re.search(r"(\d{1,2})-\d{1,2}/(\d{1,2})/(\d{4})", date_str)
    if m2:
        try:
            dt = datetime(int(m2.group(3)), int(m2.group(2)), int(m2.group(1)),
                          tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            pass

    # Try DD/MM/YYYY (simple date)
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", date_str)
    if m:
        try:
            dt = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)),
                          tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            pass

    return None
