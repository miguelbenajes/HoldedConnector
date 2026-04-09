"""
Sync router — health check, sync control, config, and schema endpoints.

Handles Holded data sync triggers, sync status, API key configuration,
and DB schema introspection for Brain's db_schema tool.
Extracted from api.py (Fase 4 router split, Task 6).

Note: sync_status, _sync_lock, and run_sync() live in api.py (shared state).
They are imported lazily inside endpoint functions to avoid circular imports.

Endpoints:
    GET  /health
    POST /api/sync
    GET  /api/sync/status
    GET  /api/config
    POST /api/config
    GET  /api/schema
"""
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import connector
import requests
import os
import logging
from app.db.connection import db_context
from app.routers._shared import assert_valid_table, _VALID_TABLE_RE

logger = logging.getLogger(__name__)
router = APIRouter()


class ConfigUpdate(BaseModel):
    apiKey: Optional[str] = None


@router.get("/health")
def health():
    return {"status": "ok", "service": "holded-connector"}


@router.post("/api/sync")
async def sync_data(background_tasks: BackgroundTasks):
    from api import sync_status, _sync_lock, run_sync
    with _sync_lock:
        if sync_status["running"]:
            return {"status": "already_running"}
        sync_status["running"] = True
    background_tasks.add_task(run_sync)
    return {"status": "Sync started"}


@router.get("/api/sync/status")
async def get_sync_status():
    from api import sync_status
    return sync_status


@router.get("/api/config")
async def get_config():
    return {
        "hasKey": bool(connector.API_KEY),
        "apiKey": "****" if connector.API_KEY else None
    }


@router.post("/api/config")
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


@router.get("/api/schema")
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
                assert_valid_table(tname)
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
                assert_valid_table(tname)
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
