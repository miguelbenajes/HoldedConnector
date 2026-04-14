"""
write_preview.py — Preview builder and warning generator for the Safe Write Gateway.

Builds rich preview objects with calculated totals and contextual warnings.
Receives pre-fetched data from validation context to avoid duplicate queries.
"""

import logging
import time
from datetime import datetime, timezone
import connector
from write_validators import _row_to_dict

logger = logging.getLogger(__name__)


# ── Line Item Calculations ───────────────────────────────────────────

DEFAULT_TAX_PCT = 21  # Spanish standard IVA rate

def _calculate_items(items, products_map=None):
    """Calculate line-by-line totals for invoice/estimate items."""
    calculated = []
    for item in items:
        raw_units = item.get("units")
        raw_price = item.get("price")
        raw_tax = item.get("tax")
        raw_discount = item.get("discount")
        units = float(raw_units) if raw_units is not None else 1.0
        price = float(raw_price) if raw_price is not None else 0.0
        tax_pct = float(raw_tax) if raw_tax is not None else float(DEFAULT_TAX_PCT)
        discount_pct = float(raw_discount) if raw_discount is not None else 0.0

        line_subtotal = units * price
        line_discount = line_subtotal * (discount_pct / 100)
        taxable = line_subtotal - line_discount
        line_tax = taxable * (tax_pct / 100)
        line_total = taxable + line_tax

        product_id = item.get("product_id")
        product_data = products_map.get(product_id) if products_map and product_id else None

        calculated.append({
            "name": item.get("name", ""),
            "product_id": product_id,
            "units": units,
            "price": price,
            "tax_pct": tax_pct,
            "discount_pct": discount_pct,
            "line_subtotal": round(line_subtotal, 2),
            "line_discount": round(line_discount, 2),
            "line_tax": round(line_tax, 2),
            "line_total": round(line_total, 2),
            "stock": product_data.get("stock") if product_data else None,
            "kind": product_data.get("kind") if product_data else None,
        })

    return calculated


# ── Warning Generator ────────────────────────────────────────────────

def _get_contact_warnings(contact, doc_type="invoice"):
    """Generate warnings related to a contact."""
    warnings = []
    if not contact:
        return warnings

    contact_id = contact.get("id")
    contact_type = contact.get("type", "")

    # Check unpaid/overdue invoices in a single batch query
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)
        cursor.execute(connector._q('''
            SELECT
                SUM(CASE WHEN status IN (1,2) THEN 1 ELSE 0 END) as unpaid_count,
                COALESCE(SUM(CASE WHEN status IN (1,2) THEN payments_pending ELSE 0 END), 0) as unpaid_total,
                SUM(CASE WHEN status = 4 THEN 1 ELSE 0 END) as overdue_count,
                COALESCE(SUM(CASE WHEN status = 4 THEN payments_pending ELSE 0 END), 0) as overdue_total,
                COUNT(*) as total_invoices
            FROM invoices WHERE contact_id = ?
        '''), (contact_id,))
        row = cursor.fetchone()
        if row:
            r = _row_to_dict(cursor, row) or {}
            unpaid = r.get('unpaid_count', 0) or 0
            unpaid_total = r.get('unpaid_total', 0) or 0
            overdue = r.get('overdue_count', 0) or 0
            overdue_total = r.get('overdue_total', 0) or 0
            total = r.get('total_invoices', 0) or 0

            if overdue and overdue > 0:
                warnings.append({
                    "level": "critical",
                    "code": "OVERDUE_INVOICES",
                    "msg": f"Contact has {overdue} overdue invoice(s) totaling EUR {float(overdue_total):,.2f}"
                })
            if unpaid and unpaid > 0:
                warnings.append({
                    "level": "warning",
                    "code": "UNPAID_INVOICES",
                    "msg": f"Contact has {unpaid} unpaid invoice(s) totaling EUR {float(unpaid_total):,.2f}"
                })
            if total == 0:
                warnings.append({
                    "level": "info",
                    "code": "FIRST_INVOICE",
                    "msg": "First invoice for this contact"
                })
    except Exception as e:
        logger.warning(f"Warning query failed for contact {contact_id}: {e}")
    finally:
        connector.release_db(conn)

    # Contact type mismatch
    if doc_type in ("invoice", "estimate") and contact_type == "supplier":
        warnings.append({
            "level": "info",
            "code": "CONTACT_IS_SUPPLIER",
            "msg": "Creating sales document for a supplier-type contact"
        })

    return warnings


def _get_item_warnings(calculated_items, high_amount_threshold=5000):
    """Generate warnings related to line items and totals."""
    warnings = []
    grand_total = sum(i["line_total"] for i in calculated_items)

    for item in calculated_items:
        stock = item.get("stock")
        if stock is not None:
            if stock == 0:
                warnings.append({
                    "level": "warning",
                    "code": "ZERO_STOCK",
                    "msg": f"Product '{item['name']}' has zero stock"
                })
            elif stock < item["units"]:
                warnings.append({
                    "level": "warning",
                    "code": "LOW_STOCK",
                    "msg": f"Product '{item['name']}' stock: {stock} (requested: {item['units']})"
                })

        if item.get("kind") == "pack":
            warnings.append({
                "level": "info",
                "code": "PRODUCT_IS_PACK",
                "msg": f"'{item['name']}' is a pack product"
            })

    if grand_total > high_amount_threshold:
        warnings.append({
            "level": "warning",
            "code": "HIGH_AMOUNT",
            "msg": f"Total EUR {grand_total:,.2f} exceeds threshold of EUR {high_amount_threshold:,.2f}"
        })

    return warnings


def _get_document_warnings(params, context):
    """Generate document-level warnings: date, retention, price modifications."""
    warnings = []
    contact = context.get("contact", {})
    items = params.get("items", [])
    products_map = context.get("products", {})

    # Date check: if date is today or not provided, warn
    doc_date = params.get("date")
    if doc_date:
        doc_dt = datetime.fromtimestamp(doc_date, tz=timezone.utc)
        now_dt = datetime.now(tz=timezone.utc)
        if doc_dt.date() == now_dt.date():
            warnings.append({
                "level": "warning",
                "code": "DATE_IS_TODAY",
                "msg": "Document date is today. Should it be the shooting/service date instead?"
            })
    else:
        warnings.append({
            "level": "warning",
            "code": "DATE_NOT_SET",
            "msg": "No date provided — will default to today. Consider setting the shooting/service date."
        })

    # Retention: now enforced as hard gate in write_validators._validate_retention()
    # No soft warning needed — validator rejects if wrong.

    # Price modification check: item price differs from catalog without discount
    for idx, item in enumerate(items):
        pid = item.get("product_id")
        if pid and pid in products_map:
            catalog_price = float(products_map[pid].get("price") or 0)
            item_price = float(item.get("price") or 0)
            discount = float(item.get("discount") or 0)
            if catalog_price > 0 and item_price != catalog_price and discount == 0:
                warnings.append({
                    "level": "warning",
                    "code": "PRICE_MODIFIED_DIRECTLY",
                    "msg": (f"Item '{item.get('name', '')}' price ({item_price}€) differs from "
                            f"catalog ({catalog_price}€) with no discount. Use original price + discount instead?")
                })

    return warnings


def _check_duplicate_recent(contact_id, grand_total, window_hours=24):
    """Check for similar documents created recently."""
    if not contact_id or grand_total <= 0:
        return None
    cutoff = int(time.time()) - (window_hours * 3600)
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)
        cursor.execute(connector._q('''
            SELECT doc_number, amount, date FROM invoices
            WHERE contact_id = ? AND date >= ? AND ABS(amount - ?) < ?
            ORDER BY date DESC LIMIT 3
        '''), (contact_id, cutoff, grand_total, grand_total * 0.1))
        rows = cursor.fetchall()
        if rows:
            return {
                "level": "warning",
                "code": "DUPLICATE_RECENT",
                "msg": f"Found {len(rows)} similar invoice(s) in last {window_hours}h for this contact"
            }
        return None
    except Exception as e:
        logger.warning(f"Duplicate check failed for contact {contact_id}: {e}")
        return None
    finally:
        connector.release_db(conn)


# ── Reversibility Assessment ─────────────────────────────────────────

REVERSIBILITY = {
    "create_invoice": {
        "can_reverse": True,
        "method": "DELETE",
        "endpoint": "/invoicing/v1/documents/invoice/{id}",
        "conditions": "While status is draft (0)",
    },
    "create_estimate": {
        "can_reverse": True,
        "method": "DELETE",
        "endpoint": "/invoicing/v1/documents/estimate/{id}",
        "conditions": "While status is draft (0)",
    },
    "create_contact": {
        "can_reverse": True,
        "method": "DELETE",
        "endpoint": "/invoicing/v1/contacts/{id}",
        "conditions": "No linked invoices",
    },
    "send_document": {
        "can_reverse": False,
        "conditions": "Email cannot be unsent",
    },
    "update_document_status": {
        "can_reverse": False,
        "conditions": "Status transitions are generally one-way",
    },
    "upload_file": {
        "can_reverse": True,
        "conditions": "File can be deleted from filesystem",
    },
    "convert_estimate_to_invoice": {
        "can_reverse": True,
        "method": "DELETE",
        "endpoint": "/invoicing/v1/documents/invoice/{id}",
        "conditions": "Invoice can be deleted while in borrador. Estimate status change is permanent.",
    },
    "approve_invoice": {
        "can_reverse": False,
        "conditions": "Invoice approval submits to Hacienda/SII. IRREVERSIBLE and legally binding.",
    },
}


# ── Public API ───────────────────────────────────────────────────────

def build_preview(operation, params, context=None):
    """Build a rich preview object for a write operation.

    Args:
        operation: e.g. 'create_invoice'
        params: operation parameters
        context: pre-fetched data from validation stage

    Returns:
        dict with 'preview', 'warnings', 'reversibility' keys
    """
    context = context or {}
    preview = {"operation": operation}
    warnings = []

    if operation in ("create_invoice", "create_estimate"):
        contact = context.get("contact", {})
        products_map = context.get("products", {})
        items = params.get("items", [])
        calculated = _calculate_items(items, products_map)

        subtotal = sum(i["line_subtotal"] for i in calculated)
        total_discount = sum(i["line_discount"] for i in calculated)
        total_tax = sum(i["line_tax"] for i in calculated)
        grand_total = sum(i["line_total"] for i in calculated)

        preview["contact"] = {
            "id": contact.get("id", params.get("contact_id")),
            "name": contact.get("name", "Unknown"),
            "code": contact.get("code", ""),
            "type": contact.get("type", ""),
        }
        preview["items"] = calculated
        preview["subtotal"] = round(subtotal, 2)
        preview["total_discount"] = round(total_discount, 2)
        preview["total_tax"] = round(total_tax, 2)
        preview["grand_total"] = round(grand_total, 2)
        preview["currency"] = "EUR"

        warnings.extend(_get_contact_warnings(contact, operation.split("_")[1]))
        warnings.extend(_get_item_warnings(calculated))

        doc_warnings = _get_document_warnings(params, context or {})
        warnings.extend(doc_warnings)

        dup = _check_duplicate_recent(contact.get("id"), grand_total)
        if dup:
            warnings.append(dup)

        if not params.get("due_date") and operation == "create_invoice":
            warnings.append({"level": "info", "code": "NO_DUE_DATE", "msg": "Invoice created without due date"})

    elif operation == "create_contact":
        preview["contact_name"] = params.get("name", "")
        preview["contact_type"] = params.get("type", "client")
        preview["email"] = params.get("email", "")

    elif operation == "update_document_status":
        doc = context.get("document", {})
        status_labels = {0: "borrador", 1: "aprobada", 2: "partial", 3: "paid", 4: "overdue", 5: "cancelled"}
        old_status = doc.get("status", 0)
        new_status = params.get("status")
        preview["document"] = {
            "id": params.get("doc_id"),
            "doc_number": doc.get("doc_number", ""),
            "current_status": status_labels.get(old_status, "unknown"),
            "new_status": status_labels.get(new_status, "unknown"),
        }
        # CRITICAL: Approving an invoice (borrador→aprobada) submits it to Hacienda via SII
        if old_status == 0 and new_status == 1:
            warnings.append({
                "level": "critical",
                "code": "HACIENDA_SUBMISSION",
                "msg": "APROBAR esta factura la enviará a Hacienda (SII). Esta acción es IRREVERSIBLE."
            })

    elif operation == "send_document":
        doc = context.get("document", {})
        preview["document"] = {
            "id": params.get("doc_id"),
            "doc_number": doc.get("doc_number", ""),
            "type": params.get("doc_type"),
        }
        preview["recipients"] = params.get("emails", [])

    elif operation == "approve_invoice":
        doc = context.get("document", {})
        preview["document"] = {
            "id": params.get("doc_id"),
            "doc_number": doc.get("doc_number", ""),
            "current_status": "borrador",
        }
        preview["action"] = "Aprobar factura — envío a Hacienda/SII irreversible"
        warnings.append({
            "level": "critical",
            "code": "HACIENDA_SUBMISSION",
            "msg": "APROBAR esta factura la enviará a Hacienda (SII). Esta acción es IRREVERSIBLE y legalmente vinculante."
        })

    elif operation == "convert_estimate_to_invoice":
        estimate = context.get("estimate", {})
        contact = context.get("contact", {})
        estimate_items = context.get("estimate_items", [])

        # Build calculated items from estimate_items
        calc_items = []
        for item in estimate_items:
            calc_items.append({
                "name": item.get("name", ""),
                "units": float(item.get("units", 1)),
                "price": float(item.get("price", 0)),
                "tax_pct": float(item.get("tax", DEFAULT_TAX_PCT)),
            })
        calculated = _calculate_items(calc_items)

        grand_total = sum(i["line_total"] for i in calculated)

        preview["estimate"] = {
            "id": estimate.get("id"),
            "doc_number": estimate.get("doc_number", ""),
            "status": estimate.get("status", 0),
        }
        preview["contact"] = {
            "id": contact.get("id", estimate.get("contact_id")),
            "name": contact.get("name", estimate.get("contact_name", "Unknown")),
        }
        preview["items"] = calculated
        preview["grand_total"] = round(grand_total, 2)
        preview["currency"] = "EUR"
        preview["note"] = "Invoice will be created as BORRADOR. Estimate will be marked as invoiced."

        warnings.extend(_get_contact_warnings(contact, "invoice"))
        warnings.extend(_get_item_warnings(calculated))

    reversibility = REVERSIBILITY.get(operation, {"can_reverse": False, "conditions": "Unknown"})

    return {
        "operation": operation,
        "preview": preview,
        "warnings": warnings,
        "reversibility": reversibility,
    }
