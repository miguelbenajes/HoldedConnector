"""
Job Tracker router — /api/jobs and /api/estimates/without-ref endpoints.

Handles job lifecycle (create, read, update, sync to Obsidian) and
retrieval of estimates without a project reference code.
Extracted from api.py (Fase 4 router split, Task 2).
"""
from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse
from typing import Optional
import connector
import re as _re_mod
import logging
from datetime import datetime
from app.db.connection import db_context

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/jobs")
def list_jobs(status: str = None, quarter: str = None, limit: int = 50):
    """List jobs, optionally filtered by status and/or quarter."""
    VALID_STATUSES = {"open", "shooting", "invoiced", "closed"}
    limit = min(max(1, limit), 200)
    conn = connector.get_db()
    try:
        cur = connector._cursor(conn)
        conditions = []
        params = []
        if status:
            if status not in VALID_STATUSES:
                return JSONResponse({"error": f"Invalid status. Must be one of: {', '.join(sorted(VALID_STATUSES))}"}, status_code=400)
            conditions.append("status = ?")
            params.append(status)
        if quarter:
            if not _re_mod.match(r'^[1-4]T_\d{4}$', quarter):
                return JSONResponse({"error": "Invalid quarter format. Expected: 1T_2026"}, status_code=400)
            conditions.append("quarter = ?")
            params.append(quarter)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT * FROM jobs {where} ORDER BY updated_at DESC LIMIT ?"
        cur.execute(connector._q(sql), (*params, limit))
        rows = cur.fetchall()
        result = []
        for r in rows:
            if not isinstance(r, dict):
                cols = [d[0] for d in cur.description]
                r = dict(zip(cols, r))
            result.append(r)
        return result
    finally:
        connector.release_db(conn)


@router.post("/api/jobs")
def create_job(request: dict):
    """Create a new job (Brain's entry point)."""
    from skills.job_tracker import ensure_job
    project_code = (request.get("project_code") or "").strip()
    if not project_code:
        return JSONResponse({"error": "project_code required"}, status_code=400)
    if len(project_code) > 50 or not _re_mod.match(r'^[A-Za-z0-9_\- ]+$', project_code):
        return JSONResponse({"error": "Invalid project_code (max 50 chars, alphanumeric/dash/underscore)"}, status_code=400)

    conn = connector.get_db()
    try:
        cur = connector._cursor(conn)
        doc_data = {
            "client_id": request.get("client_id"),
            "client_name": request.get("client_name"),
            "shooting_dates_raw": request.get("shooting_dates_raw"),
            "estimate_id": request.get("estimate_id"),
            "estimate_number": request.get("estimate_number"),
            "invoice_id": request.get("invoice_id"),
            "invoice_number": request.get("invoice_number"),
            "doc_date": request.get("doc_date"),
        }
        result = ensure_job(project_code, doc_data, cur)
        conn.commit()

        try:
            from skills.job_tracker import flush_note_queue
            flush_note_queue()
        except Exception:
            pass

        return result
    finally:
        connector.release_db(conn)


@router.get("/api/jobs/{code}")
def get_job(code: str):
    """Get job detail with expenses."""
    if not code or len(code) > 50 or not _re_mod.match(r'^[A-Za-z0-9_\- ]+$', code):
        return JSONResponse({"error": "Invalid project code"}, status_code=400)
    conn = connector.get_db()
    try:
        cur = connector._cursor(conn)
        cur.execute(connector._q("SELECT * FROM jobs WHERE project_code = ?"), (code,))
        row = cur.fetchone()
        if not row:
            return JSONResponse({"error": "Job not found"}, status_code=404)
        if not isinstance(row, dict):
            cols = [d[0] for d in cur.description]
            row = dict(zip(cols, row))

        cur.execute(connector._q("""
            SELECT date, "desc" as name, amount, doc_number
            FROM purchase_invoices WHERE project_code = ?
            ORDER BY date
        """), (code,))
        expenses = []
        for r in cur.fetchall():
            if not isinstance(r, dict):
                cols = [d[0] for d in cur.description]
                r = dict(zip(cols, r))
            expenses.append(r)

        row["expenses"] = expenses
        return row
    finally:
        connector.release_db(conn)


@router.patch("/api/jobs/{code}")
def update_job(code: str, request: dict):
    """Update job fields (status, shooting_dates, invoice_id, etc.)."""
    if not code or len(code) > 50 or not _re_mod.match(r'^[A-Za-z0-9_\- ]+$', code):
        return JSONResponse({"error": "Invalid project code"}, status_code=400)
    VALID_STATUSES = {"open", "shooting", "invoiced", "closed"}
    ALLOWED_FIELDS = {"status", "shooting_dates_raw", "invoice_id", "invoice_number", "note_path", "notes_hash"}

    # Validate status if provided
    if "status" in request and request["status"] not in VALID_STATUSES:
        return JSONResponse({"error": f"Invalid status. Must be one of: {', '.join(sorted(VALID_STATUSES))}"}, status_code=400)

    conn = connector.get_db()
    try:
        cur = connector._cursor(conn)

        cur.execute(connector._q("SELECT project_code FROM jobs WHERE project_code = ?"), (code,))
        if not cur.fetchone():
            return JSONResponse({"error": "Job not found"}, status_code=404)

        updates = {k: v for k, v in request.items() if k in ALLOWED_FIELDS}
        if not updates:
            return JSONResponse({"error": "No valid fields to update"}, status_code=400)

        # Build SET clause with ? placeholders, then convert once via _q()
        set_parts = [f'"{k}" = ?' for k in updates]
        values = list(updates.values())

        set_clause = ", ".join(set_parts)
        values.append(datetime.now().isoformat())
        values.append(code)
        cur.execute(connector._q(f"UPDATE jobs SET {set_clause}, updated_at = ? WHERE project_code = ?"),
                    tuple(values))
        conn.commit()

        cur.execute(connector._q("SELECT * FROM jobs WHERE project_code = ?"), (code,))
        row = cur.fetchone()
        if not isinstance(row, dict):
            cols = [d[0] for d in cur.description]
            row = dict(zip(cols, row))
        return row
    finally:
        connector.release_db(conn)


@router.post("/api/jobs/{code}/sync-note")
def sync_job_note(code: str):
    """Force re-render and push job note to Obsidian."""
    if not code or len(code) > 50:
        return JSONResponse({"error": "Invalid code"}, status_code=400)
    from skills.job_tracker import sync_job_to_obsidian
    success = sync_job_to_obsidian(code)
    if success:
        return {"success": True, "message": f"Note synced for {code}"}
    return JSONResponse({"error": "Sync failed"}, status_code=502)


@router.get("/api/estimates/without-ref")
def get_estimates_without_ref(since: str = "2026-03-25", limit: int = 50):
    """List estimates created after cutoff that have no project_code (REF).
    Used by Brain's presupuesto audit in the job-review pipeline."""
    limit = min(max(1, limit), 200)
    try:
        cutoff_ts = int(datetime.strptime(since, "%Y-%m-%d").timestamp())
    except ValueError:
        return JSONResponse({"error": "Invalid date format. Expected YYYY-MM-DD"}, status_code=400)

    conn = connector.get_db()
    try:
        cur = connector._cursor(conn)
        cur.execute(connector._q("""
            SELECT id, doc_number, contact_id, date, amount, tags
            FROM estimates
            WHERE project_code IS NULL
              AND date >= ?
            ORDER BY date DESC
            LIMIT ?
        """), (cutoff_ts, limit))
        rows = []
        for r in cur.fetchall():
            row = r if isinstance(r, dict) else dict(zip([d[0] for d in cur.description], r))
            rows.append(row)

        # Enrich with contact names
        for row in rows:
            if row.get("contact_id"):
                cur.execute(connector._q(
                    "SELECT name FROM contacts WHERE id = ?"
                ), (row["contact_id"],))
                contact = cur.fetchone()
                if contact:
                    row["client_name"] = (contact["name"] if isinstance(contact, dict) else contact[0]) or ""
        return rows
    finally:
        connector.release_db(conn)


@router.post("/api/jobs/flush-queue")
def flush_job_queue():
    """Process all pending Obsidian note queue items."""
    from skills.job_tracker import flush_note_queue
    count = flush_note_queue()
    return {"success": True, "processed": count}
