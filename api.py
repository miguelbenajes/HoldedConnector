from fastapi import FastAPI, BackgroundTasks, Response, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from typing import Optional
from contextlib import contextmanager
import sqlite3
import os
import time
import logging
import json
import requests
import io
import pandas as pd
from datetime import datetime
import connector
import reports
import ai_agent
from pydantic import BaseModel

logger = logging.getLogger(__name__)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8000", "http://127.0.0.1:8000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_NAME = "holded.db"
REPORTS_DIR = os.path.abspath("reports")
UPLOADS_DIR = os.path.abspath("uploads")

sync_status = {"running": False, "last_result": None, "last_time": None, "errors": []}

@contextmanager
def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def run_sync():
    sync_status["running"] = True
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
                logger.error(f"Sync step '{step_name}' failed: {e}")
                sync_status["errors"].append(f"{step_name}: {str(e)}")
    finally:
        sync_status["running"] = False
        sync_status["last_time"] = datetime.now().isoformat()
        sync_status["last_result"] = "error" if sync_status["errors"] else "success"

@app.post("/api/sync")
async def sync_data(background_tasks: BackgroundTasks):
    if sync_status["running"]:
        return {"status": "already_running"}
    background_tasks.add_task(run_sync)
    return {"status": "Sync started"}

@app.get("/api/sync/status")
async def get_sync_status():
    return sync_status

@app.get("/api/config")
async def get_config():
    return {
        "hasKey": bool(connector.API_KEY),
        "apiKey": connector.API_KEY[:4] + "*" * 10 if connector.API_KEY else None
    }

class ConfigUpdate(BaseModel):
    apiKey: Optional[str] = None

@app.post("/api/config")
async def update_config(body: ConfigUpdate):
    if body.apiKey:
        url = "https://api.holded.com/api/invoicing/v1/contacts"
        headers = {"key": body.apiKey}
        try:
            response = requests.get(url, headers=headers, params={"limit": 1})
            if response.status_code == 200:
                connector.save_setting("holded_api_key", body.apiKey)
                connector.reload_config()
            else:
                return {"status": "error", "message": "Invalid Holded API Key"}
        except Exception as e:
            return {"status": "error", "message": f"Holded Error: {str(e)}"}

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
        logger.error(f"Excel API Error: {e}")
        return {"status": "error", "message": str(e)}

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
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    safe_name = f"{int(time.time())}_{os.path.basename(file.filename)}"
    file_path = os.path.join(UPLOADS_DIR, safe_name)
    if not os.path.abspath(file_path).startswith(UPLOADS_DIR):
        return {"status": "error", "message": "Invalid filename"}

    with open(file_path, "wb") as buffer:
        buffer.write(await file.read())

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

@app.get("/api/stats/monthly")
def get_monthly_stats(start: Optional[int] = None, end: Optional[int] = None):
    with get_db_connection() as conn:
        cursor = conn.cursor()

        where_clause = ""
        params = []
        if start and end:
            where_clause = "WHERE date >= ? AND date <= ?"
            params = [start, end]

        cursor.execute(f"""
            SELECT
                strftime('%Y-%m', datetime(date, 'unixepoch')) as month,
                SUM(amount) as total
            FROM invoices
            {where_clause}
            GROUP BY month
            ORDER BY month DESC
            LIMIT 12
        """, params)
        income = [dict(row) for row in cursor.fetchall()]
        income.reverse()

        cursor.execute(f"""
            SELECT
                strftime('%Y-%m', datetime(date, 'unixepoch')) as month,
                SUM(amount) as total
            FROM purchase_invoices
            {where_clause}
            GROUP BY month
            ORDER BY month DESC
            LIMIT 12
        """, params)
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

@app.get("/api/entities/products/{product_id}/history")
def get_product_history(product_id: str):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        query = """
            SELECT 'income' as type, i.id as doc_id, i.date, it.units, it.price, it.subtotal
            FROM invoice_items it
            JOIN invoices i ON it.invoice_id = i.id
            WHERE it.product_id = ?
            UNION ALL
            SELECT 'expense' as type, p.id as doc_id, p.date, pit.units, pit.price, pit.subtotal
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

@app.get("/api/entities/{doc_type}/{doc_id}/pdf")
def get_document_pdf(doc_type: str, doc_id: str):
    type_map = {
        "invoices": "invoice",
        "purchases": "purchase",
        "estimates": "estimate"
    }
    holded_type = type_map.get(doc_type, doc_type)

    url = f"https://api.holded.com/api/invoicing/v1/documents/{holded_type}/{doc_id}/pdf"
    headers = {"key": connector.API_KEY}

    response = requests.get(url, headers=headers)

    if response.status_code == 200:
        try:
            json_data = response.json()
            if isinstance(json_data, dict) and "data" in json_data:
                import base64
                pdf_bytes = base64.b64decode(json_data["data"])
                return Response(content=pdf_bytes, media_type="application/pdf")
        except Exception:
            pass
        return Response(content=response.content, media_type="application/pdf")
    else:
        return Response(content=f"Error fetching PDF: {response.status_code}", status_code=response.status_code)

# ─── AI Chat Endpoints ───────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None

class ConfirmRequest(BaseModel):
    pending_state_id: str
    confirmed: bool

@app.post("/api/ai/chat")
async def ai_chat(body: ChatRequest):
    if not ai_agent.check_rate_limit():
        return {"type": "error", "content": "Rate limit exceeded. Please wait a moment."}
    result = ai_agent.chat(body.message, body.conversation_id)
    return result

@app.post("/api/ai/chat/stream")
async def ai_chat_stream(body: ChatRequest):
    if not ai_agent.check_rate_limit():
        async def error_gen():
            yield f"event: error\ndata: {json.dumps({'content': 'Rate limit exceeded.'})}\n\n"
        return StreamingResponse(error_gen(), media_type="text/event-stream")

    def sse_generator():
        for event in ai_agent.chat_stream(body.message, body.conversation_id):
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
    claudeApiKey: Optional[str] = None

@app.post("/api/ai/config")
async def ai_config_update(body: AIConfigUpdate):
    if body.claudeApiKey:
        connector.save_setting("claude_api_key", body.claudeApiKey)
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
    uploads_dir = connector.get_uploads_dir()
    os.makedirs(uploads_dir, exist_ok=True)

    # Validate file type
    allowed_exts = {".csv", ".xlsx", ".xls"}
    file_ext = os.path.splitext(file.filename)[1].lower()

    if file_ext not in allowed_exts:
        return {"error": f"File type not allowed: {file_ext}. Only CSV/Excel allowed."}

    # Validate file size (max 50MB)
    try:
        content = await file.read()
        if len(content) > 50 * 1024 * 1024:
            return {"error": "File too large (max 50MB)"}
    except Exception as e:
        return {"error": f"File read error: {str(e)}"}

    # Save file with timestamp prefix (unique names)
    safe_name = f"{int(time.time())}_{os.path.basename(file.filename)}"
    filepath = os.path.join(uploads_dir, safe_name)

    try:
        with open(filepath, "wb") as f:
            f.write(content)

        return {
            "success": True,
            "filename": safe_name,
            "original_name": file.filename,
            "size": len(content)
        }
    except Exception as e:
        return {"error": f"Upload failed: {str(e)}"}

@app.get("/api/files/list")
async def list_files(directory: str = "uploads", limit: int = 20):
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
        return {"error": f"Error listing files: {str(e)}"}

# Serve static files (mount at the end to avoid intercepting /api)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def read_index():
    return FileResponse("static/index.html")

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    uvicorn.run(app, host="0.0.0.0", port=8000)
