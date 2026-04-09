from fastapi import FastAPI, BackgroundTasks, Response, UploadFile, File, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
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

# ── Table name validation (SQL injection prevention) ─────────────────────
_VALID_TABLE_RE = _re_mod.compile(r'^[a-z_][a-z0-9_]*$')

def _assert_valid_table(name: str) -> None:
    """Raise ValueError if table name contains unexpected characters."""
    if not _VALID_TABLE_RE.match(name):
        raise ValueError(f"Invalid table name: {name!r}")
import reports
import ai_agent
from app.routers import jobs as jobs_router
from app.routers import treasury as treasury_router
from app.routers import gateway_api as gateway_api_router
from app.routers import files as files_router
from app.routers import sync as sync_router
from app.routers import amortizations as amortizations_router
from app.routers import entities as entities_router
from app.routers import dashboard as dashboard_router
from app.routers import agent_writes as agent_writes_router
from app.routers import ai as ai_router


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

# ── Dashboard endpoints — moved to app/routers/dashboard.py ──────────────────
# (registered via app.include_router(dashboard_router.router) below)

# ── Entities endpoints — moved to app/routers/entities.py ────────────────────
# (registered via app.include_router(entities_router.router) below)

# ─── AI Chat Endpoints — moved to app/routers/ai.py ─────────────────────────
# (registered via app.include_router(ai_router.router) below)

# ────────────── File Management Endpoints ──────────────
# Moved to app/routers/files.py

# ── Amortizations, audit log, analysis endpoints — moved to app/routers/amortizations.py
# (registered via app.include_router(amortizations_router.router) below)


# ── Schema Introspection ─────────────────────────────────────────────────────
# Moved to app/routers/sync.py

# ── Agent Write Endpoints — moved to app/routers/agent_writes.py ─────────────
# (registered via app.include_router(agent_writes_router.router) below)


# ── Gateway Estimate endpoint — moved to app/routers/gateway_api.py ─────────────
# (registered via app.include_router(gateway_api_router.router) below)


# ── Treasury & Payment endpoints — moved to app/routers/treasury.py ────────────
# (registered via app.include_router(treasury_router.router) below)


# Backup endpoints removed — security risk: exposed full DB and codebase.
# Use direct DB access (pg_dump) or Supabase dashboard for backups.


# ── Job Tracker Endpoints — moved to app/routers/jobs.py ──────────────────────
# (registered via app.include_router(jobs_router.router) below)


# ── Router registrations (extracted routers) ──────────────────────────────────
app.include_router(jobs_router.router)
app.include_router(treasury_router.router)
app.include_router(gateway_api_router.router)
app.include_router(files_router.router)
app.include_router(sync_router.router)
app.include_router(amortizations_router.router)
app.include_router(entities_router.router)
app.include_router(dashboard_router.router)
app.include_router(agent_writes_router.router)
app.include_router(ai_router.router)

# Serve static files (mount at the end to avoid intercepting /api)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def read_index():
    return FileResponse("static/index.html")

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    uvicorn.run(app, host="0.0.0.0", port=8000)
