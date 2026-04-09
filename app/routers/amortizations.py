"""
Amortizations router — ROI tracking, purchase links, analysis, and audit log.

Covers: amortization CRUD, purchase cost links, purchase search, inventory
analysis (status/run/matches/categories/invoices), and write audit log.
18+ endpoints extracted from api.py (Fase 4 router split).
"""
from fastapi import APIRouter, BackgroundTasks, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional
import connector
import json
import logging
from app.db.connection import db_context
from write_validators import _row_to_dict

logger = logging.getLogger(__name__)
router = APIRouter()

VALID_PRODUCT_TYPES = {"alquiler", "venta", "servicio", "gasto"}


# ────────────── Amortizations Endpoints ──────────────

@router.get("/api/products/{product_id}/pack-info")
def get_product_pack_info(product_id: str):
    """Return pack composition (if pack) or packs containing this product (if component)."""
    info = connector.get_pack_info(product_id)
    if info is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Product not found")
    return info


@router.get("/api/amortizations")
def get_amortizations():
    """Return all amortization entries with calculated revenue/profit/ROI + fiscal type."""
    return connector.get_amortizations()

@router.get("/api/amortizations/summary")
def get_amortizations_summary():
    """Global summary: total invested, recovered, global ROI."""
    return connector.get_amortization_summary()

@router.get("/api/amortizations/types")
def get_product_types():
    """Return all product type fiscal rules (alquiler, venta, servicio, gasto)."""
    return connector.get_product_type_rules()

class AmortizationCreate(BaseModel):
    product_id: str = Field(..., max_length=100)
    product_name: str = Field(..., max_length=300)
    purchase_price: float = Field(..., ge=0)
    purchase_date: str = Field(..., max_length=20)
    notes: Optional[str] = Field("", max_length=500)
    product_type: Optional[str] = Field("alquiler", max_length=30)

@router.post("/api/amortizations")
def create_amortization(body: AmortizationCreate):
    from fastapi import HTTPException
    ptype = body.product_type or "alquiler"
    if ptype not in VALID_PRODUCT_TYPES:
        raise HTTPException(status_code=400, detail="Invalid product type")
    try:
        new_id = connector.add_amortization(
            body.product_id, body.product_name,
            body.purchase_price, body.purchase_date,
            body.notes or "", ptype
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if new_id is None:
        raise HTTPException(status_code=409, detail="Product already tracked in amortizations")
    connector.insert_audit_log(
        source="rest_api",
        operation="create_amortization",
        entity_type="amortization",
        entity_id=str(new_id),
        payload_sent=body.model_dump(),
        status="success",
    )
    return {"status": "success", "id": new_id}

class AmortizationUpdate(BaseModel):
    purchase_price: Optional[float] = None
    purchase_date: Optional[str] = Field(None, max_length=20)
    notes: Optional[str] = Field(None, max_length=500)
    product_type: Optional[str] = Field(None, max_length=30)

@router.put("/api/amortizations/{amort_id}")
def update_amortization(amort_id: int, body: AmortizationUpdate):
    if body.product_type and body.product_type not in VALID_PRODUCT_TYPES:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Invalid product type")
    ok = connector.update_amortization(
        amort_id, body.purchase_price, body.purchase_date,
        body.notes, body.product_type
    )
    if not ok:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Amortization not found")
    connector.insert_audit_log(
        source="rest_api",
        operation="update_amortization",
        entity_type="amortization",
        entity_id=str(amort_id),
        payload_sent=body.model_dump(),
        status="success",
    )
    return {"status": "success"}

@router.delete("/api/amortizations/{amort_id}")
def delete_amortization(amort_id: int):
    ok = connector.delete_amortization(amort_id)
    if not ok:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Amortization not found")
    connector.insert_audit_log(
        source="rest_api",
        operation="delete_amortization",
        entity_type="amortization",
        entity_id=str(amort_id),
        payload_sent={"amort_id": amort_id},
        status="success",
    )
    return {"status": "success"}


# ── Purchase invoice search (for cost-source picker) ──────────────────────────

@router.get("/api/purchases/search")
def search_purchases(q: str = "", limit: int = Query(20, ge=1, le=200)):
    """
    Search purchase invoices by supplier name or description.
    Returns matching invoices with their line items for the picker UI.
    """
    with db_context() as conn:
        cursor = conn.cursor()
        like = f"%{q}%"
        cursor.execute("""
            SELECT pi.id, pi.contact_name AS supplier, pi.desc, pi.date, pi.amount,
                   pit.id AS item_id, pit.name AS item_name, pit.units AS item_units, pit.price AS item_price
            FROM purchase_invoices pi
            LEFT JOIN purchase_items pit ON pit.purchase_id = pi.id
            WHERE pi.contact_name LIKE ? OR pi.desc LIKE ? OR pit.name LIKE ?
            ORDER BY pi.date DESC
            LIMIT ?
        """, (like, like, like, limit))
        rows = cursor.fetchall()

    # Group items under their parent invoice
    invoices = {}
    for r in rows:
        r = dict(r)
        pid = r['id']
        if pid not in invoices:
            invoices[pid] = {
                'id': pid, 'supplier': r['supplier'] or '—',
                'desc': r['desc'] or '', 'date': r['date'], 'amount': r['amount'],
                'items': []
            }
        if r['item_id']:
            invoices[pid]['items'].append({
                'id': r['item_id'], 'name': r['item_name'],
                'units': r['item_units'], 'price': r['item_price']
            })
    return list(invoices.values())


# ── Amortization Purchase Links ───────────────────────────────────────────────

@router.get("/api/amortizations/{amort_id}/purchases")
def get_amortization_purchases(amort_id: int):
    """Return all purchase links (cost sources) for one amortization."""
    return connector.get_amortization_purchases(amort_id)


class PurchaseLinkCreate(BaseModel):
    cost_override: float
    allocation_note: Optional[str] = ""
    purchase_id: Optional[str] = None       # purchase_invoices.id
    purchase_item_id: Optional[int] = None  # purchase_items.id


@router.post("/api/amortizations/{amort_id}/purchases")
def add_amortization_purchase(amort_id: int, body: PurchaseLinkCreate):
    """Add a purchase cost source to an amortization. Recalculates total cost."""
    new_id = connector.add_amortization_purchase(
        amortization_id=amort_id,
        cost_override=body.cost_override,
        allocation_note=body.allocation_note or "",
        purchase_id=body.purchase_id,
        purchase_item_id=body.purchase_item_id,
    )
    if new_id is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Could not add purchase link")
    return {"status": "success", "id": new_id}


class PurchaseLinkUpdate(BaseModel):
    cost_override: Optional[float] = None
    allocation_note: Optional[str] = None
    purchase_id: Optional[str] = None
    purchase_item_id: Optional[int] = None


@router.put("/api/amortizations/purchases/{link_id}")
def update_amortization_purchase(link_id: int, body: PurchaseLinkUpdate):
    """Edit cost or note of a purchase link. Recalculates parent total cost."""
    ok = connector.update_amortization_purchase(
        purchase_link_id=link_id,
        cost_override=body.cost_override,
        allocation_note=body.allocation_note,
        purchase_id=body.purchase_id,
        purchase_item_id=body.purchase_item_id,
    )
    if not ok:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Purchase link not found")
    return {"status": "success"}


@router.delete("/api/amortizations/purchases/{link_id}")
def delete_amortization_purchase(link_id: int):
    """Remove a purchase link and recalculate parent total cost."""
    ok = connector.delete_amortization_purchase(link_id)
    if not ok:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Purchase link not found")
    return {"status": "success"}


# ────────────── Audit Log Endpoints ──────────────

@router.get("/api/audit-log")
def list_audit_log(limit: int = 50, offset: int = 0, operation: str = None,
                   entity_type: str = None, status: str = None):
    """List recent write audit log entries with optional filters."""
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)
        where = []
        vals = []
        if operation:
            where.append(connector._q("operation = ?"))
            vals.append(operation)
        if entity_type:
            where.append(connector._q("entity_type = ?"))
            vals.append(entity_type)
        if status:
            where.append(connector._q("status = ?"))
            vals.append(status)

        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        vals.extend([limit, offset])

        cursor.execute(connector._q(f'''
            SELECT id, timestamp, source, operation, entity_type, entity_id,
                   status, safe_mode, duration_ms, error_detail
            FROM write_audit_log {where_sql}
            ORDER BY id DESC LIMIT ? OFFSET ?
        '''), tuple(vals))
        rows = cursor.fetchall()
        return [_row_to_dict(cursor, r) for r in rows] if rows else []
    finally:
        connector.release_db(conn)


@router.get("/api/audit-log/{audit_id}")
def get_audit_log_detail(audit_id: int):
    """Get full audit log entry including payload, preview, warnings, and reverse action."""
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)
        cursor.execute(connector._q(
            'SELECT * FROM write_audit_log WHERE id = ?'
        ), (audit_id,))
        row = cursor.fetchone()
        if not row:
            return {"error": "Audit entry not found"}
        entry = _row_to_dict(cursor, row)
        # Parse JSON fields for structured output
        for field in ('payload_sent', 'response_received', 'preview_data',
                      'warnings', 'tables_synced', 'reverse_action', 'reverse_payload'):
            if entry.get(field) and isinstance(entry[field], str):
                try:
                    entry[field] = json.loads(entry[field])
                except (json.JSONDecodeError, TypeError):
                    pass
        return entry
    finally:
        connector.release_db(conn)


# ────────────── Invoice Analysis Endpoints ──────────────

@router.get("/api/analysis/status")
def get_analysis_status():
    """Current state of the analysis job + overall progress stats."""
    from api import analysis_status
    stats = connector.get_analysis_stats()
    merged = {**analysis_status, **stats}
    # last_run in memory is reset on every server restart; use DB value as fallback
    if not merged.get("last_run") and stats.get("last_run_db"):
        merged["last_run"] = stats["last_run_db"]
    return merged

@router.post("/api/analysis/run")
async def trigger_analysis(background_tasks: BackgroundTasks, batch_size: int = 10):
    """Manually trigger an analysis batch (runs in background)."""
    from api import run_analysis_job, analysis_status
    if analysis_status["running"]:
        return {"status": "already_running"}
    background_tasks.add_task(run_analysis_job, batch_size)
    return {"status": "started", "batch_size": batch_size}

@router.get("/api/analysis/matches")
def get_inventory_matches():
    """Return all pending inventory→purchase matches awaiting confirmation."""
    return connector.get_pending_matches()

class MatchConfirm(BaseModel):
    confirmed: bool
    custom_price: Optional[float] = None      # User-overridden cost for this specific purchase link
    allocation_note: Optional[str] = None     # e.g. "1/3 del pack de 3 Manfrotto 1004BAC"
    product_type: Optional[str] = None        # Override default product type (alquiler/venta/etc.)

@router.post("/api/analysis/matches/{match_id}/confirm")
def confirm_match(match_id: int, body: MatchConfirm):
    """Confirm or reject an inventory match. Confirmed ones go to amortizations.
    If custom_price is provided, it overrides the auto-detected matched_price."""
    result = connector.confirm_inventory_match(
        match_id, body.confirmed, body.custom_price,
        allocation_note=body.allocation_note or "",
        product_type=body.product_type,
    )
    if not result.get("ok"):
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=result.get("error", "Unknown error"))
    return result

@router.get("/api/analysis/categories")
def get_category_breakdown():
    """Spending breakdown by category with totals."""
    stats = connector.get_analysis_stats()
    return stats.get("by_category", [])

@router.get("/api/analysis/invoices")
def get_analyzed_invoices(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0), category: str = None, q: str = None):
    """List categorized invoices with analysis details, paginated. q= for text search."""
    return connector.get_analyzed_invoices(limit=limit, offset=offset, category=category, q=q)
