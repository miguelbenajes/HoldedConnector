from fastapi import FastAPI, BackgroundTasks, Response, UploadFile, File, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from typing import Optional, List
import os
import time
import logging
import json
import requests
import io
import pandas as pd
import re as _re_mod
import threading
from datetime import datetime, timedelta
import connector
from app.db.connection import db_context
from app.domain.item_builder import build_holded_items, build_holded_items_with_accounts

# ── Table name validation (SQL injection prevention) ─────────────────────
_VALID_TABLE_RE = _re_mod.compile(r'^[a-z_][a-z0-9_]*$')

def _assert_valid_table(name: str) -> None:
    """Raise ValueError if table name contains unexpected characters."""
    if not _VALID_TABLE_RE.match(name):
        raise ValueError(f"Invalid table name: {name!r}")
import reports
import ai_agent
from write_gateway import gateway, RateLimiter
from app.routers import jobs as jobs_router

# Feature flag: route /api/agent/* endpoints through Write Gateway (default: true)
# Set USE_GATEWAY_FOR_AGENT=false to rollback to direct connector calls (requires restart)
_USE_GATEWAY = os.getenv("USE_GATEWAY_FOR_AGENT", "true").lower() == "true"

# Rate limiter for approve_invoice endpoint (1/min safety gate, separate from gateway limits)
_approve_limiter = RateLimiter()


def _gw_error(result, default="Operation failed"):
    """Extract first error message from a gateway result dict."""
    errs = result.get("errors")
    return errs[0].get("msg", default) if errs else default
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

app = FastAPI()

# ── API Authentication ─────────────────────────────────────────────────
# Triple-auth middleware: Supabase cookie + Supabase JWT Bearer + legacy token.
# See auth.py for full documentation of each auth path.
import auth as auth_module

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # Public paths (health, static files, root)
    if auth_module.is_public_path(request.url.path):
        request.state.user = None
        return await call_next(request)

    # Dev mode: no auth configured → allow all
    if not auth_module.HOLDED_CONNECTOR_TOKEN and not auth_module.SUPABASE_JWT_SECRET:
        request.state.user = None
        return await call_next(request)

    # Path 1: Bearer token (legacy inter-service OR Supabase JWT)
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]

        # Legacy HOLDED_CONNECTOR_TOKEN (Brain inter-service) → full access
        # EXCEPT: /approve routes require X-Confirm-Hacienda header (SII safety)
        if auth_module.is_legacy_token(token):
            if "/approve" in request.url.path:
                if request.headers.get("x-confirm-hacienda") != "true":
                    return JSONResponse(status_code=403, content={
                        "error": "Approve requires X-Confirm-Hacienda: true header (Hacienda/SII submission)"
                    })
            request.state.user = None
            return await call_next(request)

        # Try as Supabase JWT Bearer (API clients)
        try:
            payload = auth_module.validate_supabase_jwt(token)
            user = auth_module.lookup_user(payload["sub"], connector.get_db)
            if user and user.is_active:
                if not auth_module.check_permission(user.role, request.method, request.url.path):
                    return JSONResponse(status_code=403, content={"error": "Insufficient permissions"})
                request.state.user = user
                return await call_next(request)
        except Exception as e:
            logger.warning("Auth validation failed: %s", type(e).__name__)

    # Path 2: Supabase cookie (panel users via nginx proxy)
    cookie_header = request.headers.get("cookie", "")
    jwt_from_cookie = auth_module.extract_jwt_from_cookies(cookie_header)
    if jwt_from_cookie:
        try:
            payload = auth_module.validate_supabase_jwt(jwt_from_cookie)
            user = auth_module.lookup_user(payload["sub"], connector.get_db)
            if not user:
                return JSONResponse(status_code=403, content={"error": "User not registered"})
            if not user.is_active:
                return JSONResponse(status_code=403, content={"error": "Account deactivated"})
            if not auth_module.check_permission(user.role, request.method, request.url.path):
                return JSONResponse(status_code=403, content={"error": "Insufficient permissions"})
            request.state.user = user
            return await call_next(request)
        except Exception as e:
            logger.warning("Cookie JWT validation failed: %s", e)

    return JSONResponse(status_code=401, content={"error": "Authentication required"})

# CORS: restrict to known origins in production.
# In production (DATABASE_URL set), ALLOWED_ORIGINS is required — never allow *.
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "")
if not ALLOWED_ORIGINS and connector.DATABASE_URL:
    logger.critical("ALLOWED_ORIGINS must be set in production (DATABASE_URL is set). Defaulting to coyoterent.com")
    ALLOWED_ORIGINS = "https://coyoterent.com,https://bolsa.coyoterent.com"
cors_origins = ALLOWED_ORIGINS.split(",") if ALLOWED_ORIGINS else ["*"]  # * only in local dev (SQLite)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

REPORTS_DIR = os.path.abspath("reports")
UPLOADS_DIR = os.path.abspath("uploads")

sync_status = {"running": False, "last_result": None, "last_time": None, "errors": []}
_sync_lock = threading.Lock()

# ── Invoice Analysis Job ─────────────────────────────────────────────
analysis_status = {
    "running": False,
    "last_run": None,
    "last_result": None,
    "processed": 0,
    "pending_matches": 0,
}
_analysis_lock = threading.Lock()

@app.get("/health")
def health():
    return {"status": "ok", "service": "holded-connector"}

def run_analysis_job(batch_size: int = 10):
    """
    Core analysis job:
    1. Categorize up to batch_size unanalyzed purchase invoices (rules → Claude fallback)
    2. Scan ALL purchase_items for inventory matches and save pending ones
    """
    with _analysis_lock:
        if analysis_status["running"]:
            return {"error": "Already running"}
        analysis_status["running"] = True
        analysis_status["last_run"] = datetime.now().isoformat()
    processed = 0
    errors = []

    try:
        # ── Step 1: Categorize unanalyzed invoices ───────────────────
        invoices = connector.get_unanalyzed_purchases(limit=batch_size)
        logger.info(f"Analysis job: {len(invoices)} invoices to categorize")

        for inv in invoices:
            try:
                result = connector.categorize_by_rules(
                    inv.get('desc') or '',
                    inv.get('contact_name') or '',
                    inv.get('item_names') or []
                )

                if result is None:
                    # Claude fallback for ambiguous invoices
                    result = _claude_categorize(inv)

                connector.save_purchase_analysis(
                    purchase_id=inv['id'],
                    category=result.get('category', 'Sin categoría'),
                    subcategory=result.get('subcategory', ''),
                    confidence=result.get('confidence', 'low'),
                    method=result.get('method', 'unknown'),
                    reasoning=result.get('reasoning', '')
                )
                processed += 1
            except Exception as e:
                logger.error(f"Error categorizing {inv['id']}: {e}")
                errors.append(str(e))

        # ── Step 2: Scan for inventory matches ───────────────────────
        matches = connector.find_inventory_in_purchases()
        logger.info(f"Analysis job: {len(matches)} inventory matches found")
        for m in matches:
            try:
                connector.save_inventory_match(
                    m['purchase_id'], m['purchase_item_id'], m['product_id'],
                    m['product_name'], m['matched_price'], m['matched_date'],
                    m['match_method']
                )
            except Exception as e:
                logger.warning(f"Match save error: {e}")

        pending = len(connector.get_pending_matches())
        analysis_status["processed"] = processed
        analysis_status["pending_matches"] = pending
        analysis_status["last_result"] = "success" if not errors else "partial"
        return {"processed": processed, "matches_found": len(matches), "pending": pending}

    except Exception as e:
        logger.error(f"Analysis job failed: {e}", exc_info=True)
        analysis_status["last_result"] = "error"
        return {"error": "Analysis job failed"}
    finally:
        analysis_status["running"] = False


def _claude_categorize(inv: dict) -> dict:
    """
    Use Claude to categorize a purchase invoice when rules don't match.
    Keeps prompt minimal to save tokens.
    """
    try:
        api_key = ai_agent._get_api_key()
        if not api_key:
            return {"category": "Sin categoría", "subcategory": "", "confidence": "low",
                    "method": "no_key", "reasoning": "No Claude API key configured"}

        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        item_list = ", ".join(inv.get('item_names') or []) or "—"
        prompt = (
            f"Clasifica esta factura de gasto empresarial. "
            f"Elige la carpeta contable que mejor encaje.\n"
            f"Proveedor: {inv.get('contact_name','')}\n"
            f"Descripción: {inv.get('desc','')}\n"
            f"Items: {item_list}\n"
            f"Importe: {inv.get('amount',0)}€\n\n"
            f"Carpetas contables disponibles:\n"
            f"- AIRBNB RECIBOS (recibos de alojamiento Airbnb)\n"
            f"- AMAZON (compras en Amazon de cualquier tipo)\n"
            f"- GASTOS LOCAL → subcarpeta: agua y luz | alarma | alquiler\n"
            f"- SEGUROS - SEG SOCIAL → subcarpeta: Seguro | Seg. Social\n"
            f"- SOFTWARE → subcarpeta: adobe | apple | capture one | google | holded | hostalia | spotify | Suscripción\n"
            f"- TELEFONIA E INTERNET → subcarpeta: Digi | finetwork | Telefonía\n"
            f"- TRANSPORTE → subcarpeta: dhl | gasolina | renfe | taxis | uber | vuelos | Mensajería\n"
            f"- A AMORTIZAR (equipamiento de alto valor: cámaras, ordenadores, maquinaria)\n"
            f"- VARIOS → subcarpeta: Restaurante | Formación | Varios\n\n"
            f"Responde SOLO con JSON: "
            f'{{\"category\":\"...\",\"subcategory\":\"...\",\"reasoning\":\"...\"}}'
        )
        msg = client.messages.create(
            model=ai_agent._get_model(),
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()
        # Extract JSON even if Claude adds surrounding text
        import re
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            data = json.loads(m.group())
            return {
                "category": data.get("category", "Otros"),
                "subcategory": data.get("subcategory", ""),
                "confidence": "medium",
                "method": "claude",
                "reasoning": data.get("reasoning", "")
            }
    except Exception as e:
        logger.warning(f"Claude categorization failed: {e}")

    return {"category": "Sin categoría", "subcategory": "", "confidence": "low",
            "method": "failed", "reasoning": "Categorization failed"}


# Daily scheduler: runs analysis job automatically each day
_scheduler_thread = None
_scheduler_stop = threading.Event()

def _daily_scheduler():
    """Background thread: runs analysis job once per day."""
    while not _scheduler_stop.is_set():
        now = datetime.now()
        # Run at 3:00 AM
        next_run = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        wait_secs = (next_run - now).total_seconds()
        logger.info(f"Analysis scheduler: next run in {wait_secs/3600:.1f}h at {next_run.strftime('%H:%M')}")
        if _scheduler_stop.wait(timeout=wait_secs):
            break  # Shutdown requested
        logger.info("Analysis scheduler: starting daily job")
        try:
            run_analysis_job(batch_size=10)
        except Exception as e:
            logger.error(f"Analysis scheduler: job failed: {e}", exc_info=True)


@app.on_event("startup")
def on_startup():
    """Initialize DB schema on server start (creates tables if they don't exist)."""
    connector.init_db()
    # Start daily analysis scheduler in background
    global _scheduler_thread
    _scheduler_thread = threading.Thread(target=_daily_scheduler, daemon=True)
    _scheduler_thread.start()
    logger.info("Daily analysis scheduler started")

@app.on_event("shutdown")
def on_shutdown():
    """Signal scheduler thread to stop gracefully."""
    _scheduler_stop.set()

# get_db_connection → db_context (from app.db.connection)
# _CompatCursor, _ConnProxy also live there now.
get_db_connection = db_context

def run_sync():
    sync_status["errors"] = []
    try:
        connector.init_db()
        steps = [
            ("accounts", connector.sync_accounts),
            ("contacts", connector.sync_contacts),
            ("products", connector.sync_products),
            ("invoices", connector.sync_invoices),
            ("purchases", connector.sync_purchases),
            ("estimates", connector.sync_estimates),
            ("projects", connector.sync_projects),
            ("payments", connector.sync_payments),
        ]
        for step_name, step_fn in steps:
            try:
                step_fn()
            except Exception as e:
                logger.error(f"Sync step '{step_name}' failed: {e}", exc_info=True)
                sync_status["errors"].append(f"{step_name} failed")

        # Flush pending job notes to Obsidian
        try:
            from skills.job_tracker import flush_note_queue
            count = flush_note_queue()
            if count:
                logger.info(f"[JOB_TRACKER] Flushed {count} job notes to Obsidian")
        except Exception as e:
            logger.error(f"[JOB_TRACKER] Queue flush failed: {e}")
    finally:
        sync_status["running"] = False
        sync_status["last_time"] = datetime.now().isoformat()
        sync_status["last_result"] = "error" if sync_status["errors"] else "success"

@app.post("/api/sync")
async def sync_data(background_tasks: BackgroundTasks):
    with _sync_lock:
        if sync_status["running"]:
            return {"status": "already_running"}
        sync_status["running"] = True
    background_tasks.add_task(run_sync)
    return {"status": "Sync started"}

@app.get("/api/sync/status")
async def get_sync_status():
    return sync_status

@app.get("/api/config")
async def get_config():
    return {
        "hasKey": bool(connector.API_KEY),
        "apiKey": "****" if connector.API_KEY else None
    }

class ConfigUpdate(BaseModel):
    apiKey: Optional[str] = None

@app.post("/api/config")
async def update_config(body: ConfigUpdate, request: Request):
    # POST /api/config requires auth (GET is public for SPA init check)
    if not getattr(request.state, "user", None):
        return JSONResponse(status_code=401, content={"error": "Authentication required"})
    if body.apiKey:
        url = "https://api.holded.com/api/invoicing/v1/contacts"
        headers = {"key": body.apiKey}
        try:
            response = requests.get(url, headers=headers, params={"limit": 1}, timeout=15)
            if response.status_code == 200:
                connector.save_setting("holded_api_key", body.apiKey)
                connector.reload_config()
            else:
                return {"status": "error", "message": "Invalid Holded API Key"}
        except Exception as e:
            logger.error(f"Holded config validation error: {e}")
            return {"status": "error", "message": "Could not validate API key with Holded"}

    return {"status": "success"}

@app.get("/api/reports/excel")
def download_excel_report():
    try:
        data_dict = reports.get_financial_summary_data()
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            for sheet_name, df in data_dict.items():
                df.to_excel(writer, index=False, sheet_name=sheet_name)
        output.seek(0)

        headers = {
            'Content-Disposition': 'attachment; filename="holded_connector_report.xlsx"'
        }
        return StreamingResponse(output, headers=headers, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    except Exception as e:
        logger.error(f"Excel API Error: {e}", exc_info=True)
        return {"status": "error", "message": "Failed to generate Excel report"}

@app.get("/api/reports/download/{filename}")
async def download_report_file(filename: str):
    if not filename.endswith(".pdf"):
        return {"status": "error", "message": "Invalid file type"}

    safe_name = os.path.basename(filename)
    file_path = os.path.join(REPORTS_DIR, safe_name)
    if not os.path.abspath(file_path).startswith(REPORTS_DIR):
        return {"status": "error", "message": "Invalid file path"}

    if os.path.exists(file_path):
        return FileResponse(file_path, filename=safe_name)
    return {"status": "error", "message": "File not found"}

@app.post("/api/tickets/upload")
async def upload_ticket(file: UploadFile = File(...)):
    from fastapi import HTTPException
    # Validate file type
    allowed_exts = {".jpg", ".jpeg", ".png", ".pdf", ".csv", ".xlsx", ".xls"}
    file_ext = os.path.splitext(file.filename or "")[1].lower()
    if file_ext not in allowed_exts:
        raise HTTPException(status_code=400, detail=f"File type not allowed: {file_ext}")

    os.makedirs(UPLOADS_DIR, exist_ok=True)
    safe_name = f"{int(time.time())}_{os.path.basename(file.filename or 'upload')}"
    file_path = os.path.join(UPLOADS_DIR, safe_name)
    if not os.path.abspath(file_path).startswith(UPLOADS_DIR):
        raise HTTPException(status_code=400, detail="Invalid filename")

    content = await file.read()
    if len(content) > 50 * 1024 * 1024:  # 50MB limit
        raise HTTPException(status_code=413, detail="File too large (max 50MB)")

    with open(file_path, "wb") as buffer:
        buffer.write(content)

    return {
        "status": "success",
        "filename": safe_name,
        "message": "Ticket subido correctamente. En la siguiente versión implementaremos el reconocimiento automático."
    }

@app.get("/api/summary")
def get_summary():
    with get_db_connection() as conn:
        cursor = conn.cursor()

        counts = {}
        for table in ["invoices", "purchase_invoices", "estimates", "products", "contacts"]:
            _assert_valid_table(table)
            cursor.execute(f"SELECT COUNT(*) as count FROM {table}")
            counts[table] = cursor.fetchone()["count"]

        cursor.execute("SELECT SUM(amount) as total FROM invoices")
        total_income = cursor.fetchone()["total"] or 0

        cursor.execute("SELECT SUM(amount) as total FROM purchase_invoices")
        total_expenses = cursor.fetchone()["total"] or 0

    return {
        "counts": counts,
        "totals": {
            "income": total_income,
            "expenses": total_expenses,
            "balance": total_income - total_expenses
        }
    }

@app.get("/api/stats/date-range")
def get_date_range():
    """
    Returns the earliest and latest date found across all main transactional tables.
    Used by the date picker to know the absolute min/max for 'Desde siempre'.
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT MIN(d) AS min_date, MAX(d) AS max_date FROM (
                SELECT MIN(date) AS d FROM invoices          WHERE date > 0
                UNION ALL
                SELECT MIN(date) FROM purchase_invoices      WHERE date > 0
                UNION ALL
                SELECT MIN(date) FROM estimates              WHERE date > 0
                UNION ALL
                SELECT MAX(date) FROM invoices               WHERE date > 0
                UNION ALL
                SELECT MAX(date) FROM purchase_invoices      WHERE date > 0
                UNION ALL
                SELECT MAX(date) FROM estimates              WHERE date > 0
            )
        """)
        row = dict(cursor.fetchone())
    return row


@app.get("/api/invoices/unpaid")
def get_unpaid_invoices():
    """
    Return all invoices where paymentsPending > 0 (truly unpaid).
    Holded's 'paymentsPending' field is the authoritative source — it is the
    amount still owed and is 0 only when the invoice is fully paid.
    Aging is calculated from dueDate (the real payment deadline):
      - days_overdue <= 0   → Pendiente (green,  within payment terms)
      - days_overdue 1-30   → Atención  (yellow, slightly overdue)
      - days_overdue > 30   → Vencida   (red,    significantly overdue)
    Sorted oldest due date first (most urgent at top).
    """
    import time
    now_ts = int(time.time())
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT i.id, i.contact_name, i.desc, i.date, i.amount, i.status,
                   i.payments_pending, i.payments_total,
                   i.due_date, i.doc_number,
                   c.email AS contact_email,
                   CAST((? - COALESCE(i.due_date, i.date)) / 86400 AS INTEGER) AS days_overdue
            FROM invoices i
            LEFT JOIN contacts c ON c.id = i.contact_id
            WHERE i.payments_pending > 0.01
              AND i.status != 3
            ORDER BY COALESCE(i.due_date, i.date) ASC
        """, (now_ts,))
        rows = [dict(r) for r in cursor.fetchall()]
    # Annotate each row with a human-readable aging label
    for r in rows:
        d = r['days_overdue'] or 0
        if d <= 0:
            r['aging_label'] = 'Pendiente'
        elif d <= 30:
            r['aging_label'] = 'Atención'
        else:
            r['aging_label'] = 'Vencida'
    return rows


@app.get("/api/stats/monthly")
def get_monthly_stats(start: Optional[int] = None, end: Optional[int] = None):
    if connector._USE_SQLITE:
        month_expr = "strftime('%Y-%m', datetime(date, 'unixepoch'))"
    else:
        month_expr = "TO_CHAR(TO_TIMESTAMP(date), 'YYYY-MM')"

    with get_db_connection() as conn:
        cursor = conn.cursor()

        where_clause = ""
        params = []
        if start and end:
            where_clause = "WHERE date >= ? AND date <= ?"
            params = [start, end]

        cursor.execute(f"""
            SELECT
                {month_expr} as month,
                SUM(amount) as total
            FROM invoices
            {where_clause}
            GROUP BY month
            ORDER BY month DESC
            LIMIT 12
        """, params or None)
        income = [dict(row) for row in cursor.fetchall()]
        income.reverse()

        cursor.execute(f"""
            SELECT
                {month_expr} as month,
                SUM(amount) as total
            FROM purchase_invoices
            {where_clause}
            GROUP BY month
            ORDER BY month DESC
            LIMIT 12
        """, params or None)
        expenses = [dict(row) for row in cursor.fetchall()]
        expenses.reverse()

    return {"income": income, "expenses": expenses}

@app.get("/api/stats/range")
def get_range_stats(start: int, end: int):
    with get_db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT SUM(amount) as total FROM invoices WHERE date >= ? AND date <= ?", (start, end))
        income = cursor.fetchone()["total"] or 0

        cursor.execute("SELECT SUM(amount) as total FROM purchase_invoices WHERE date >= ? AND date <= ?", (start, end))
        expenses = cursor.fetchone()["total"] or 0

    return {
        "income": income,
        "expenses": expenses,
        "range": {"start": start, "end": end}
    }

@app.get("/api/stats/top-contacts")
def get_top_contacts():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT contact_name, SUM(amount) as total
            FROM invoices
            WHERE contact_name IS NOT NULL AND contact_name != ''
            GROUP BY contact_name
            ORDER BY total DESC
            LIMIT 5
        """)
        return [dict(row) for row in cursor.fetchall()]

@app.get("/api/recent")
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

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

@app.get("/api/entities/contacts")
def get_contacts():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM contacts ORDER BY name ASC")
        return [dict(row) for row in cursor.fetchall()]

@app.get("/api/entities/contacts/{contact_id}/history")
def get_contact_history(contact_id: str):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        query = """
            SELECT 'income' as type, id, contact_name, amount, date, status FROM invoices WHERE contact_id = ?
            UNION ALL
            SELECT 'expense' as type, id, contact_name, amount, date, status FROM purchase_invoices WHERE contact_id = ?
            ORDER BY date DESC
        """
        cursor.execute(query, (contact_id, contact_id))
        return [dict(row) for row in cursor.fetchall()]

@app.get("/api/entities/products")
def get_products():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM products ORDER BY name ASC")
        return [dict(row) for row in cursor.fetchall()]

@app.get("/api/products/web")
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

@app.patch("/api/entities/products/{product_id}/web-include")
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

@app.get("/api/entities/products/{product_id}/history")
def get_product_history(product_id: str):
    with get_db_connection() as conn:
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

@app.get("/api/entities/invoices")
def get_all_invoices():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM invoices ORDER BY date DESC")
        return [dict(row) for row in cursor.fetchall()]

@app.get("/api/entities/invoices/{invoice_id}/items")
def get_invoice_items(invoice_id: str):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
        return [dict(row) for row in cursor.fetchall()]

@app.get("/api/entities/purchases")
def get_all_purchases():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM purchase_invoices ORDER BY date DESC")
        return [dict(row) for row in cursor.fetchall()]

@app.get("/api/entities/purchases/{purchase_id}/items")
def get_purchase_items(purchase_id: str):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM purchase_items WHERE purchase_id = ?", (purchase_id,))
        return [dict(row) for row in cursor.fetchall()]

@app.get("/api/entities/estimates")
def get_all_estimates():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM estimates ORDER BY date DESC")
        return [dict(row) for row in cursor.fetchall()]

@app.get("/api/entities/estimates/{estimate_id}/items")
def get_estimate_items(estimate_id: str):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM estimate_items WHERE estimate_id = ?", (estimate_id,))
        return [dict(row) for row in cursor.fetchall()]

@app.get("/api/entities/estimates/{estimate_id}/items/fresh")
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

@app.get("/api/entities/{doc_type}/{doc_id}/pdf")
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

# ─── AI Chat Endpoints ───────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str = Field(..., max_length=8000)
    conversation_id: Optional[str] = Field(None, max_length=100)

class ConfirmRequest(BaseModel):
    pending_state_id: str = Field(..., max_length=100)
    confirmed: bool

@app.post("/api/ai/chat")
async def ai_chat(body: ChatRequest, request: Request):
    if not ai_agent.check_rate_limit():
        return {"type": "error", "content": "Rate limit exceeded. Please wait a moment."}
    user_role = getattr(getattr(request.state, "user", None), "role", "admin")
    result = ai_agent.chat(body.message, body.conversation_id, user_role=user_role)
    return result

@app.post("/api/ai/chat/stream")
async def ai_chat_stream(body: ChatRequest, request: Request):
    if not ai_agent.check_rate_limit():
        async def error_gen():
            yield f"event: error\ndata: {json.dumps({'content': 'Rate limit exceeded.'})}\n\n"
        return StreamingResponse(error_gen(), media_type="text/event-stream")

    user_role = getattr(getattr(request.state, "user", None), "role", "admin")

    def sse_generator():
        for event in ai_agent.chat_stream(body.message, body.conversation_id, user_role=user_role):
            evt = event.get("event", "message")
            data = event.get("data", "{}")
            yield f"event: {evt}\ndata: {data}\n\n"

    return StreamingResponse(sse_generator(), media_type="text/event-stream")

@app.post("/api/ai/confirm")
async def ai_confirm(body: ConfirmRequest):
    result = ai_agent.confirm_action(body.pending_state_id, body.confirmed)
    return result

@app.get("/api/ai/history")
async def ai_history(conversation_id: Optional[str] = None):
    return ai_agent.get_history(conversation_id)

@app.delete("/api/ai/history")
async def ai_clear_history(conversation_id: Optional[str] = None):
    ai_agent.clear_history(conversation_id)
    return {"status": "success"}

@app.get("/api/ai/conversations")
async def ai_conversations():
    return ai_agent.get_conversations()

@app.get("/api/ai/favorites")
async def ai_favorites():
    return ai_agent.get_favorites()

class FavoriteRequest(BaseModel):
    query: str
    label: Optional[str] = None

@app.post("/api/ai/favorites")
async def ai_add_favorite(body: FavoriteRequest):
    fav_id = ai_agent.add_favorite(body.query, body.label)
    return {"status": "success", "id": fav_id}

@app.delete("/api/ai/favorites/{fav_id}")
async def ai_remove_favorite(fav_id: int):
    ai_agent.remove_favorite(fav_id)
    return {"status": "success"}

@app.get("/api/ai/config")
async def ai_config():
    has_key = bool(ai_agent._get_api_key())
    return {"hasKey": has_key, "model": ai_agent._get_model(), "safeMode": connector.SAFE_MODE}

class AIConfigUpdate(BaseModel):
    claudeApiKey: Optional[str] = Field(None, max_length=200)

@app.post("/api/ai/config")
async def ai_config_update(body: AIConfigUpdate):
    if body.claudeApiKey:
        key = body.claudeApiKey.strip()
        if not key.startswith("sk-ant-"):
            return {"status": "error", "message": "Invalid API key format"}
        connector.save_setting("claude_api_key", key)
    return {"status": "success"}

# ────────────── File Management Endpoints ──────────────

@app.get("/api/files/config")
async def get_file_config():
    """Get current uploads and reports directory configuration."""
    return {
        "uploads_dir": connector.get_uploads_dir(),
        "reports_dir": connector.get_reports_dir()
    }

class DirectoryConfig(BaseModel):
    uploads_dir: Optional[str] = None
    reports_dir: Optional[str] = None

@app.post("/api/files/config")
async def set_file_config(body: DirectoryConfig):
    """Update uploads/reports directory paths."""
    results = {}

    if body.uploads_dir:
        result = connector.set_uploads_dir(body.uploads_dir)
        results["uploads"] = result

    if body.reports_dir:
        result = connector.set_reports_dir(body.reports_dir)
        results["reports"] = result

    return results if results else {"error": "No paths provided"}

@app.post("/api/files/upload")
async def upload_file(file: UploadFile = File(...)):
    """Upload a file for AI analysis (CSV/Excel only)."""
    from fastapi import HTTPException

    try:
        uploads_dir = connector.get_uploads_dir()
        logger.info(f"Upload directory: {uploads_dir}")

        # Create directory if it doesn't exist
        os.makedirs(uploads_dir, exist_ok=True)
        logger.info(f"Upload directory created/verified: {uploads_dir}")

        # Validate file type
        allowed_exts = {".csv", ".xlsx", ".xls"}
        file_ext = os.path.splitext(file.filename or "")[1].lower()

        if file_ext not in allowed_exts:
            raise HTTPException(status_code=400, detail=f"File type not allowed: {file_ext}. Only CSV/Excel allowed.")

        # Validate file size (max 50MB)
        try:
            content = await file.read()
            if len(content) > 50 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="File too large (max 50MB)")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"File read error: {str(e)}")
            raise HTTPException(status_code=400, detail="File read error")

        # Save file with timestamp prefix (unique names) + path traversal guard
        safe_name = f"{int(time.time())}_{os.path.basename(file.filename or 'upload')}"
        filepath = os.path.join(uploads_dir, safe_name)
        if not os.path.abspath(filepath).startswith(os.path.abspath(uploads_dir)):
            raise HTTPException(status_code=400, detail="Invalid filename")
        logger.info(f"Saving file to: {filepath}")

        with open(filepath, "wb") as f:
            f.write(content)

        logger.info(f"File uploaded successfully: {filepath}")
        return {
            "success": True,
            "filename": safe_name,
            "original_name": file.filename,
            "size": len(content)
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Upload error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Upload failed")

@app.get("/api/files/list")
async def list_files(directory: str = "uploads", limit: int = Query(20, ge=1, le=200)):
    """List files in uploads or reports directory."""
    try:
        if directory == "uploads":
            files = connector.list_uploaded_files(limit)
        elif directory == "reports":
            reports_dir = connector.get_reports_dir()
            os.makedirs(reports_dir, exist_ok=True)
            files = []
            for f in os.listdir(reports_dir)[:limit]:
                fpath = os.path.join(reports_dir, f)
                if os.path.isfile(fpath):
                    files.append({
                        "name": f,
                        "size": os.path.getsize(fpath),
                        "type": f.split(".")[-1] if "." in f else "unknown"
                    })
            files = sorted(files, key=lambda x: x["name"], reverse=True)
        else:
            return {"error": "Invalid directory (must be 'uploads' or 'reports')"}

        return {"files": files, "count": len(files)}
    except Exception as e:
        logger.error(f"Error listing files: {e}")
        return {"error": "Failed to list files"}

# ────────────── Amortizations Endpoints ──────────────

VALID_PRODUCT_TYPES = {"alquiler", "venta", "servicio", "gasto"}

@app.get("/api/products/{product_id}/pack-info")
def get_product_pack_info(product_id: str):
    """Return pack composition (if pack) or packs containing this product (if component)."""
    info = connector.get_pack_info(product_id)
    if info is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Product not found")
    return info


@app.get("/api/amortizations")
def get_amortizations():
    """Return all amortization entries with calculated revenue/profit/ROI + fiscal type."""
    return connector.get_amortizations()

@app.get("/api/amortizations/summary")
def get_amortizations_summary():
    """Global summary: total invested, recovered, global ROI."""
    return connector.get_amortization_summary()

@app.get("/api/amortizations/types")
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

@app.post("/api/amortizations")
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

@app.put("/api/amortizations/{amort_id}")
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

@app.delete("/api/amortizations/{amort_id}")
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

@app.get("/api/purchases/search")
def search_purchases(q: str = "", limit: int = Query(20, ge=1, le=200)):
    """
    Search purchase invoices by supplier name or description.
    Returns matching invoices with their line items for the picker UI.
    """
    with get_db_connection() as conn:
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

@app.get("/api/amortizations/{amort_id}/purchases")
def get_amortization_purchases(amort_id: int):
    """Return all purchase links (cost sources) for one amortization."""
    return connector.get_amortization_purchases(amort_id)


class PurchaseLinkCreate(BaseModel):
    cost_override: float
    allocation_note: Optional[str] = ""
    purchase_id: Optional[str] = None       # purchase_invoices.id
    purchase_item_id: Optional[int] = None  # purchase_items.id


@app.post("/api/amortizations/{amort_id}/purchases")
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


@app.put("/api/amortizations/purchases/{link_id}")
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


@app.delete("/api/amortizations/purchases/{link_id}")
def delete_amortization_purchase(link_id: int):
    """Remove a purchase link and recalculate parent total cost."""
    ok = connector.delete_amortization_purchase(link_id)
    if not ok:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Purchase link not found")
    return {"status": "success"}


# ────────────── Audit Log Endpoints ──────────────

@app.get("/api/audit-log")
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
        from write_validators import _row_to_dict
        return [_row_to_dict(cursor, r) for r in rows] if rows else []
    finally:
        connector.release_db(conn)


@app.get("/api/audit-log/{audit_id}")
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
        from write_validators import _row_to_dict
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

@app.get("/api/analysis/status")
def get_analysis_status():
    """Current state of the analysis job + overall progress stats."""
    stats = connector.get_analysis_stats()
    merged = {**analysis_status, **stats}
    # last_run in memory is reset on every server restart; use DB value as fallback
    if not merged.get("last_run") and stats.get("last_run_db"):
        merged["last_run"] = stats["last_run_db"]
    return merged

@app.post("/api/analysis/run")
async def trigger_analysis(background_tasks: BackgroundTasks, batch_size: int = 10):
    """Manually trigger an analysis batch (runs in background)."""
    if analysis_status["running"]:
        return {"status": "already_running"}
    background_tasks.add_task(run_analysis_job, batch_size)
    return {"status": "started", "batch_size": batch_size}

@app.get("/api/analysis/matches")
def get_inventory_matches():
    """Return all pending inventory→purchase matches awaiting confirmation."""
    return connector.get_pending_matches()

class MatchConfirm(BaseModel):
    confirmed: bool
    custom_price: Optional[float] = None      # User-overridden cost for this specific purchase link
    allocation_note: Optional[str] = None     # e.g. "1/3 del pack de 3 Manfrotto 1004BAC"
    product_type: Optional[str] = None        # Override default product type (alquiler/venta/etc.)

@app.post("/api/analysis/matches/{match_id}/confirm")
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

@app.get("/api/analysis/categories")
def get_category_breakdown():
    """Spending breakdown by category with totals."""
    stats = connector.get_analysis_stats()
    return stats.get("by_category", [])

@app.get("/api/analysis/invoices")
def get_analyzed_invoices(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0), category: str = None, q: str = None):
    """List categorized invoices with analysis details, paginated. q= for text search."""
    return connector.get_analyzed_invoices(limit=limit, offset=offset, category=category, q=q)


# ── Schema Introspection ─────────────────────────────────────────────────────
# Used by Brain's db_schema tool to understand the Holded DB structure.

@app.get("/api/schema")
def get_holded_schema():
    """Return table names, columns, types, and row counts for the Holded DB."""
    tables = []
    conn = connector.get_db()
    cur = connector._cursor(conn)
    try:
        if connector._USE_SQLITE:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            table_names = [r["name"] if isinstance(r, dict) else r[0] for r in cur.fetchall()]
            for tname in table_names:
                if tname.startswith("sqlite_") or not _VALID_TABLE_RE.match(tname): continue
                _assert_valid_table(tname)
                cur.execute(f"PRAGMA table_info({tname})")
                cols = [{"name": r["name"] if isinstance(r, dict) else r[1],
                         "type": r["type"] if isinstance(r, dict) else r[2]}
                        for r in cur.fetchall()]
                cur.execute(f"SELECT count(*) as c FROM {tname}")
                row = cur.fetchone()
                count = row["c"] if isinstance(row, dict) else row[0]
                tables.append({"table_name": tname, "row_count": count, "columns": cols})
        else:
            cur.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                ORDER BY table_name
            """)
            table_names = [r["table_name"] if isinstance(r, dict) else r[0] for r in cur.fetchall()]
            for tname in table_names:
                if not _VALID_TABLE_RE.match(tname): continue
                _assert_valid_table(tname)
                cur.execute("""
                    SELECT column_name, data_type, is_nullable
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = %s
                    ORDER BY ordinal_position
                """, (tname,))
                cols = [{"name": r["column_name"] if isinstance(r, dict) else r[0],
                         "type": r["data_type"] if isinstance(r, dict) else r[1],
                         "nullable": (r["is_nullable"] if isinstance(r, dict) else r[2]) == "YES"}
                        for r in cur.fetchall()]
                cur.execute(f"SELECT count(*) as c FROM {tname}")
                row = cur.fetchone()
                count = row["c"] if isinstance(row, dict) else row[0]
                tables.append({"table_name": tname, "row_count": count, "columns": cols})
    finally:
        connector.release_db(conn)
    return tables


# ── Agent Write Endpoints ────────────────────────────────────────────────────
# Used by the agent-runner accounts agent. SAFE_MODE is enforced by connector.py.

class CreateDocumentBody(BaseModel):
    contact_id: str = Field(..., max_length=100)
    desc: Optional[str] = Field("", max_length=500)
    items: list = Field(..., max_length=100)  # [{name, units, price, tax?}]

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

@app.post("/api/agent/invoice")
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
    params = {"contact_id": body.contact_id, "desc": body.desc, "items": body.items}
    result = gateway.execute("create_invoice", params, source="rest_api", skip_confirm=True)
    if result.get("success"):
        return {"success": True, "id": result.get("entity_id", ""), "safe_mode": result.get("safe_mode", False)}
    return {"success": False, "error": _gw_error(result, "Failed to create invoice"), "safe_mode": connector.SAFE_MODE}

# ACCOUNT_IDS and _resolve_account moved to app.domain.item_builder

@app.post("/api/agent/estimate")
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
    params = {"contact_id": body.contact_id, "desc": body.desc, "items": body.items}
    result = gateway.execute("create_estimate", params, source="rest_api", skip_confirm=True)
    if result.get("success"):
        return {"success": True, "id": result.get("entity_id", ""), "safe_mode": result.get("safe_mode", False)}
    return {"success": False, "error": _gw_error(result, "Failed to create estimate"), "safe_mode": connector.SAFE_MODE}

@app.put("/api/agent/estimate/{estimate_id}")
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


@app.post("/api/agent/contact")
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


@app.put("/api/agent/invoice/{invoice_id}/approve")
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



@app.get("/api/agent/contact/{contact_id}")
def agent_get_contact(contact_id: str):
    """Get full contact details + check for missing required fields."""
    if not _re_mod.match(r'^[a-zA-Z0-9]+$', contact_id):
        return {"success": False, "error": "Invalid contact ID"}
    with get_db_connection() as conn:
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


@app.put("/api/agent/contact/{contact_id}")
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


@app.put("/api/agent/invoice/{invoice_id}/status")
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

@app.post("/api/agent/send/{doc_type}/{doc_id}")
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


class ConvertEstimateBody(BaseModel):
    estimate_id: str = Field(..., min_length=24, max_length=24, pattern=r'^[a-f0-9]{24}$')


@app.post("/api/agent/convert-estimate")
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


# ── Gateway Estimate endpoint (Gaffer SP3 Quote Engine) ──────────────────────

class GatewayEstimateItem(BaseModel):
    name: str
    units: float = 1
    subtotal: float = 0
    tax: float = 21
    desc: Optional[str] = None

class GatewayEstimateBody(BaseModel):
    contact_id: str = Field(..., description="Holded contact ID")
    items: List[GatewayEstimateItem] = Field(..., min_length=1)
    desc: Optional[str] = None
    date: Optional[str] = None
    notes: Optional[str] = None

@app.post("/api/gateway/estimate")
def gateway_create_estimate(request: Request, body: GatewayEstimateBody):
    """
    REST wrapper around Write Gateway's create_estimate operation.
    For service-to-service calls (Gaffer → Holded).
    Auth: BRAIN_INTERNAL_KEY Bearer token.
    """
    from fastapi import HTTPException

    # Auth check
    auth_header = request.headers.get("authorization", "")
    expected_key = os.environ.get("BRAIN_INTERNAL_KEY", "")
    if not expected_key or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    import hmac as _hmac
    if not _hmac.compare_digest(auth_header[7:], expected_key):
        raise HTTPException(status_code=401, detail="Invalid key")

    # Remap 'subtotal' → 'price' so _build_holded_payload picks it up correctly
    gateway_items = []
    for item in body.items:
        d = item.model_dump(exclude_none=True)
        if "subtotal" in d:
            d["price"] = d.pop("subtotal")
        gateway_items.append(d)

    params = {
        "contact_id": body.contact_id,
        "items": gateway_items,
    }
    if body.desc:
        params["desc"] = body.desc
    # Holded requires Unix timestamp; default to now if not provided
    params["date"] = body.date if body.date else str(int(time.time()))
    if body.notes:
        params["notes"] = body.notes

    result = gateway.execute("create_estimate", params, source="gaffer", skip_confirm=True)

    if result.get("success"):
        return {
            "success": True,
            "entity_id": result.get("entity_id", ""),
            "doc_number": result.get("doc_number", ""),
        }
    else:
        raise HTTPException(
            status_code=500,
            detail=result.get("error") or result.get("errors", "Unknown error"),
        )


# ── Treasury & Payment endpoints ─────────────────────────────────────────────

@app.get("/api/treasury")
def get_treasury_accounts():
    """Fetch bank/treasury accounts from Holded API."""
    try:
        url = f"{connector.BASE_URL}/invoicing/v1/treasury"
        response = requests.get(url, headers=connector.HEADERS, timeout=15)
        if response.status_code == 200:
            accounts = response.json()
            if not isinstance(accounts, list):
                return JSONResponse(status_code=502, content={"error": "Unexpected response format from Holded"})
            # Return only safe fields
            return [
                {
                    "id": a.get("id", ""),
                    "name": a.get("name", ""),
                    "type": a.get("type", ""),
                    "iban": a.get("iban", ""),
                    "bankname": a.get("bankname", ""),
                }
                for a in accounts
            ]
        return JSONResponse(
            status_code=response.status_code,
            content={"error": f"Holded API returned {response.status_code}"}
        )
    except requests.exceptions.RequestException as e:
        logger.error(f"Treasury fetch failed: {e}")
        return JSONResponse(status_code=502, content={"error": "Failed to reach Holded API"})


class PayDocumentBody(BaseModel):
    date: int = Field(..., description="Payment date as Unix timestamp")
    amount: float = Field(..., gt=0, le=999999.99, description="Payment amount in EUR")
    treasury: str = Field(..., min_length=1, max_length=64, description="Treasury/bank account ID from Holded")
    desc: str = Field("", max_length=500, description="Payment description")


@app.post("/api/documents/{doc_type}/{doc_id}/pay")
def pay_document(doc_type: str, doc_id: str, body: PayDocumentBody):
    """Register a payment against an invoice/purchase in Holded."""
    allowed_types = {"invoice", "purchase"}
    if doc_type not in allowed_types:
        return JSONResponse(status_code=400, content={"error": "doc_type must be 'invoice' or 'purchase'"})
    if not _re_mod.match(r'^[a-f0-9]{24}$', doc_id):
        return JSONResponse(status_code=400, content={"error": "Invalid document ID format"})

    # Validate date is reasonable (2020–01–01 to ~2 years ahead)
    max_ts = int(time.time()) + (2 * 365 * 86400)
    if body.date < 1577836800 or body.date > max_ts:
        return JSONResponse(status_code=400, content={"error": "Payment date out of valid range"})

    payload = {
        "date": body.date,
        "amount": body.amount,
        "treasury": body.treasury,
        "desc": body.desc,
    }
    result = connector.post_data(f"/invoicing/v1/documents/{doc_type}/{doc_id}/pay", payload)
    if result and not result.get("error"):
        return {"success": True, "result": result, "safe_mode": connector.SAFE_MODE}
    detail = result.get("detail", "Unknown error") if result else "No response"
    return JSONResponse(status_code=502, content={"success": False, "error": detail})


# Backup endpoints removed — security risk: exposed full DB and codebase.
# Use direct DB access (pg_dump) or Supabase dashboard for backups.


# ── Job Tracker Endpoints — moved to app/routers/jobs.py ──────────────────────
# (registered via app.include_router(jobs_router.router) below)


# ── Router registrations (extracted routers) ──────────────────────────────────
app.include_router(jobs_router.router)

# Serve static files (mount at the end to avoid intercepting /api)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def read_index():
    return FileResponse("static/index.html")

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    uvicorn.run(app, host="0.0.0.0", port=8000)
