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
from app.routers import treasury as treasury_router
from app.routers import gateway_api as gateway_api_router
from app.routers import files as files_router
from app.routers import sync as sync_router
from app.routers import amortizations as amortizations_router
from app.routers import entities as entities_router
from app.routers import dashboard as dashboard_router

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
# Moved to app/routers/files.py

# ── Amortizations, audit log, analysis endpoints — moved to app/routers/amortizations.py
# (registered via app.include_router(amortizations_router.router) below)


# ── Schema Introspection ─────────────────────────────────────────────────────
# Moved to app/routers/sync.py

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

# Serve static files (mount at the end to avoid intercepting /api)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def read_index():
    return FileResponse("static/index.html")

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    uvicorn.run(app, host="0.0.0.0", port=8000)
