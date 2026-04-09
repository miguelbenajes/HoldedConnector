"""
Entities router — contacts, products, invoices, purchases, estimates, and PDF proxy.

Covers: recent activity, contact/product/invoice/purchase/estimate listing,
line items, web product visibility toggle, and Holded PDF proxy.
16 endpoints extracted from api.py (Fase 4 router split).
"""
from fastapi import APIRouter, Response, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import connector
import requests
import os
import re as _re_mod
import logging
from app.db.connection import db_context
from app.routers._shared import assert_valid_table

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/recent")
def get_recent_activity(start: Optional[int] = None, end: Optional[int] = None):
    query = """
        SELECT type, contact_name, amount, date, status FROM (
            SELECT 'income' as type, contact_name, amount, date, status FROM invoices
            UNION ALL
            SELECT 'expense' as type, contact_name, amount, date, status FROM purchase_invoices
        )
    """
    if start and end:
        query += " WHERE date >= ? AND date <= ?"
        params = [start, end]
    else:
        params = []

    query += " ORDER BY date DESC LIMIT 10"

    with db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

@router.get("/api/entities/contacts")
def get_contacts():
    with db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM contacts ORDER BY name ASC")
        return [dict(row) for row in cursor.fetchall()]

@router.get("/api/entities/contacts/{contact_id}/history")
def get_contact_history(contact_id: str):
    with db_context() as conn:
        cursor = conn.cursor()
        query = """
            SELECT 'income' as type, id, contact_name, amount, date, status FROM invoices WHERE contact_id = ?
            UNION ALL
            SELECT 'expense' as type, id, contact_name, amount, date, status FROM purchase_invoices WHERE contact_id = ?
            ORDER BY date DESC
        """
        cursor.execute(query, (contact_id, contact_id))
        return [dict(row) for row in cursor.fetchall()]

@router.get("/api/entities/products")
def get_products():
    with db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM products ORDER BY name ASC")
        return [dict(row) for row in cursor.fetchall()]

@router.get("/api/products/web")
def get_web_products():
    """Products marked for website inclusion — price, stock, and identifiers only."""
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)
        cursor.execute(connector._q(
            'SELECT id, name, sku, price, stock, kind FROM products WHERE web_include = 1 ORDER BY name ASC'
        ))
        return [dict(row) for row in cursor.fetchall()]
    finally:
        connector.release_db(conn)

class WebIncludeToggle(BaseModel):
    web_include: bool = True

@router.patch("/api/entities/products/{product_id}/web-include")
def toggle_web_include(product_id: str, payload: WebIncludeToggle):
    web_include = 1 if payload.web_include else 0
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)
        cursor.execute(connector._q('UPDATE products SET web_include = ? WHERE id = ?'), (web_include, product_id))
        conn.commit()
        connector.insert_audit_log(
            source="rest_api",
            operation="toggle_web_include",
            entity_type="product",
            entity_id=product_id,
            payload_sent={"web_include": payload.web_include},
            status="success",
        )
        return {"ok": True, "web_include": bool(web_include)}
    finally:
        connector.release_db(conn)

@router.get("/api/entities/products/{product_id}/history")
def get_product_history(product_id: str):
    with db_context() as conn:
        cursor = conn.cursor()
        query = """
            SELECT 'income' as type, i.id as doc_id, i.date, it.units, it.price, it.subtotal,
                   i.desc as doc_desc, i.contact_name
            FROM invoice_items it
            JOIN invoices i ON it.invoice_id = i.id
            WHERE it.product_id = ?
            UNION ALL
            SELECT 'expense' as type, p.id as doc_id, p.date, pit.units, pit.price, pit.subtotal,
                   p.desc as doc_desc, p.contact_name
            FROM purchase_items pit
            JOIN purchase_invoices p ON pit.purchase_id = p.id
            WHERE pit.product_id = ?
            ORDER BY date DESC
        """
        cursor.execute(query, (product_id, product_id))
        return [dict(row) for row in cursor.fetchall()]

@router.get("/api/entities/invoices")
def get_all_invoices():
    with db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM invoices ORDER BY date DESC")
        return [dict(row) for row in cursor.fetchall()]

@router.get("/api/entities/invoices/{invoice_id}/items")
def get_invoice_items(invoice_id: str):
    with db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
        return [dict(row) for row in cursor.fetchall()]

@router.get("/api/entities/purchases")
def get_all_purchases():
    with db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM purchase_invoices ORDER BY date DESC")
        return [dict(row) for row in cursor.fetchall()]

@router.get("/api/entities/purchases/{purchase_id}/items")
def get_purchase_items(purchase_id: str):
    with db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM purchase_items WHERE purchase_id = ?", (purchase_id,))
        return [dict(row) for row in cursor.fetchall()]

@router.get("/api/entities/estimates")
def get_all_estimates():
    with db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM estimates ORDER BY date DESC")
        return [dict(row) for row in cursor.fetchall()]

@router.get("/api/entities/estimates/{estimate_id}/items")
def get_estimate_items(estimate_id: str):
    with db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM estimate_items WHERE estimate_id = ?", (estimate_id,))
        return [dict(row) for row in cursor.fetchall()]

@router.get("/api/entities/estimates/{estimate_id}/items/fresh")
def get_estimate_items_fresh(estimate_id: str):
    """Read estimate items directly from Holded API (not local DB cache)."""
    if not _re_mod.match(r'^[a-f0-9]{24}$', estimate_id):
        return JSONResponse({"error": "Invalid estimate ID"}, status_code=400)
    try:
        items = connector.fetch_estimate_fresh(estimate_id)
        return items
    except Exception as e:
        logger.error(f"Fresh read failed for estimate {estimate_id}: {e}")
        return JSONResponse({"error": f"Holded API error: {str(e)}"}, status_code=502)

@router.get("/api/entities/{doc_type}/{doc_id}/pdf")
def get_document_pdf(doc_type: str, doc_id: str):
    # Accept both singular (Brain sends these) and plural forms
    type_map = {
        "invoice": "invoice",
        "invoices": "invoice",
        "purchase": "purchase",
        "purchases": "purchase",
        "estimate": "estimate",
        "estimates": "estimate",
        "creditnote": "creditnote",
        "proforma": "proform",
    }
    holded_type = type_map.get(doc_type)
    if not holded_type:
        return Response(status_code=400, content="Invalid document type")
    if not _re_mod.match(r'^[a-f0-9]{24}$', doc_id):
        return Response(status_code=400, content="Invalid document ID — must be 24-char hex")

    # Build a meaningful filename from local DB data
    def _make_pdf_filename(doc_type: str, doc_id: str) -> str:
        try:
            import re
            db_table = {
                "invoice": "invoices", "invoices": "invoices",
                "purchase": "purchase_invoices", "purchases": "purchase_invoices",
                "estimate": "estimates", "estimates": "estimates",
            }.get(doc_type)
            if not db_table:
                return f"document_{doc_id}.pdf"
            conn = connector.get_db()
            try:
                cur = connector._cursor(conn)
                cur.execute(connector._q(f"SELECT contact_name, doc_number FROM {db_table} WHERE id = ?"), (doc_id,))
                row = cur.fetchone()
            finally:
                connector.release_db(conn)
            if not row:
                return f"document_{doc_id}.pdf"
            contact_name = row["contact_name"] if isinstance(row, dict) else row[0]
            doc_number = row["doc_number"] if isinstance(row, dict) else row[1]
            # Slugify: keep alphanumeric + spaces/slash, map to underscores
            def slug(s):
                if not s:
                    return ""
                s = s.strip()
                s = re.sub(r'[^\w\s/-]', '', s, flags=re.UNICODE)
                s = re.sub(r'[\s/]+', '_', s)
                return s[:40]  # cap length
            prefix = {
                "invoice": "Factura", "invoices": "Factura",
                "purchase": "Compra", "purchases": "Compra",
                "estimate": "Presupuesto", "estimates": "Presupuesto",
                "creditnote": "Abono", "proforma": "Proforma",
            }.get(doc_type, "Doc")
            parts = [slug(contact_name)]
            if doc_number:
                parts.append(slug(doc_number))
            else:
                parts.append(prefix)
            return "_".join(p for p in parts if p) + ".pdf"
        except Exception:
            return f"document_{doc_id}.pdf"

    pdf_filename = _make_pdf_filename(doc_type, doc_id)
    # RFC 6266: use filename* (UTF-8 percent-encoded) for non-ASCII names,
    # plus plain filename= as fallback for older clients
    from urllib.parse import quote as url_quote
    encoded_name = url_quote(pdf_filename, safe="._-")
    content_disposition = (
        f'inline; filename="{pdf_filename}"; filename*=UTF-8\'\'{encoded_name}'
    )

    url = f"https://api.holded.com/api/invoicing/v1/documents/{holded_type}/{doc_id}/pdf"
    headers = {"key": connector.API_KEY}

    try:
        response = requests.get(url, headers=headers, timeout=30, stream=False)
    except requests.exceptions.Timeout:
        logger.warning(f"PDF fetch timed out for {doc_type}/{doc_id}")
        return Response(content="PDF download timed out", status_code=504)
    except requests.exceptions.RequestException as e:
        logger.warning(f"PDF fetch network error for {doc_type}/{doc_id}: {e}")
        return Response(content="PDF download failed", status_code=502)

    if response.status_code == 200:
        # Cap PDF size at 20MB to prevent memory abuse
        max_pdf_size = 20 * 1024 * 1024
        resp_headers = {"Content-Disposition": content_disposition}
        # Holded API may return base64-encoded PDF in JSON wrapper or raw PDF bytes.
        # Detect JSON response and extract base64 data if present.
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                json_data = response.json()
                if isinstance(json_data, dict) and "data" in json_data:
                    import base64
                    pdf_bytes = base64.b64decode(json_data["data"])
                    if len(pdf_bytes) > max_pdf_size:
                        return Response(content="PDF exceeds size limit", status_code=413)
                    return Response(content=pdf_bytes, media_type="application/pdf", headers=resp_headers)
            except Exception:
                logger.warning(f"Failed to decode base64 PDF for {doc_type}/{doc_id}")
            # JSON response without valid base64 data — not a valid PDF
            return Response(content="Holded returned non-PDF response", status_code=502)
        # Raw PDF bytes (non-JSON response)
        if len(response.content) > max_pdf_size:
            return Response(content="PDF exceeds size limit", status_code=413)
        return Response(content=response.content, media_type="application/pdf", headers=resp_headers)
    else:
        logger.warning(f"PDF fetch failed for {doc_type}/{doc_id}: HTTP {response.status_code}")
        return Response(content="Failed to fetch PDF document", status_code=502)
