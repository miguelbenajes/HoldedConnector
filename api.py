"""Holded Connector — FastAPI application factory.

Entry point for the API server. Creates and configures the FastAPI app:
middleware (auth, CORS), routers, background schedulers, and static files.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os
import logging
import re as _re_mod

import connector
from app.db.connection import db_context
from app.background.analysis import (  # noqa: F401 — re-exported for legacy `from api import`
    start_scheduler, stop_scheduler,
    analysis_status, run_analysis_job,
)
from app.background.sync_runner import (  # noqa: F401 — re-exported for legacy `from api import`
    sync_status, _sync_lock, run_sync,
)

# ── Table name validation (SQL injection prevention) ─────────────────────
_VALID_TABLE_RE = _re_mod.compile(r'^[a-z_][a-z0-9_]*$')

def _assert_valid_table(name: str) -> None:
    """Raise ValueError if table name contains unexpected characters."""
    if not _VALID_TABLE_RE.match(name):
        raise ValueError(f"Invalid table name: {name!r}")

# ── Router imports ───────────────────────────────────────────────────────
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

# ── Application factory ─────────────────────────────────────────────────

app = FastAPI()

# ── Authentication middleware ────────────────────────────────────────────
import auth as auth_module
from fastapi import Request
from fastapi.responses import JSONResponse

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

# ── CORS ─────────────────────────────────────────────────────────────────
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

# ── Lifecycle ────────────────────────────────────────────────────────────

@app.on_event("startup")
def on_startup():
    """Initialize DB schema and start background schedulers."""
    connector.init_db()
    start_scheduler()

@app.on_event("shutdown")
def on_shutdown():
    """Signal background threads to stop gracefully."""
    stop_scheduler()

# ── Legacy alias ─────────────────────────────────────────────────────────
get_db_connection = db_context

# ── Router registrations ─────────────────────────────────────────────────
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

# ── Static files ─────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def read_index():
    return FileResponse("static/index.html")

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    uvicorn.run(app, host="0.0.0.0", port=8000)
