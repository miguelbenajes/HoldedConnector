import json
import sqlite3
import logging
import time
import uuid
import os
from datetime import datetime, timedelta

import anthropic
import connector
import reports

logger = logging.getLogger(__name__)

DB_NAME = "holded.db"

WRITE_TOOLS = {"create_estimate", "create_invoice", "send_document", "create_contact"}

# Pending confirmations: { state_id: { messages, tool_block, conversation_id, expires_at } }
pending_actions = {}

# Rate limiting: { ip: [timestamps] }
_rate_limits = {}
RATE_LIMIT_MAX = 10
RATE_LIMIT_WINDOW = 60

# ─── Tool Definitions ───────────────────────────────────────────────

TOOL_DEFINITIONS = [
    {
        "name": "query_database",
        "description": "Execute a read-only SQL SELECT query against the local financial database. Tables: invoices, purchase_invoices, estimates (columns: id, contact_id, contact_name, desc, date[unix epoch], amount[EUR], status[int]). Line items: invoice_items(invoice_id, product_id, name, sku, units, price, subtotal, discount, tax, retention, account), purchase_items(purchase_id, ...), estimate_items(estimate_id, ...). Also: contacts(id, name, email, type, code, vat, phone, mobile), products(id, name, desc, price, stock, sku), payments(id, document_id, amount, date, method, type), projects(id, name, desc, status, customer_id, budget), ledger_accounts(id, name, num). Status codes for invoices/purchases: 0=draft, 1=issued, 2=partial payment, 3=paid, 4=overdue, 5=cancelled. Estimates: 0=draft, 1=pending, 2=accepted, 3=rejected, 4=invoiced. Use strftime('%Y-%m', datetime(date, 'unixepoch')) for month grouping.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "A SELECT query. Only SELECT is allowed."},
                "explanation": {"type": "string", "description": "Brief explanation of what this query does."}
            },
            "required": ["sql", "explanation"]
        }
    },
    {
        "name": "get_contact_details",
        "description": "Look up a contact by name (fuzzy search) or exact ID. Returns contact info and optionally their transaction history summary (invoice/purchase counts and totals).",
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Contact name (partial match) or exact Holded ID"},
                "include_history": {"type": "boolean", "description": "Include transaction summary", "default": False}
            },
            "required": ["search"]
        }
    },
    {
        "name": "get_product_pricing",
        "description": "Look up product catalog prices and compare with actual historical sale/purchase prices. Returns catalog price, avg/min/max sale and purchase prices, and margin analysis.",
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Product name (partial match) or SKU"},
                "include_history": {"type": "boolean", "description": "Include historical price analysis", "default": True}
            },
            "required": ["search"]
        }
    },
    {
        "name": "get_financial_summary",
        "description": "Get a high-level financial summary for a time period: total income, expenses, net balance, top clients, and monthly trend.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "Start date YYYY-MM-DD (defaults to 12 months ago)"},
                "end_date": {"type": "string", "description": "End date YYYY-MM-DD (defaults to today)"}
            }
        }
    },
    {
        "name": "get_document_details",
        "description": "Get full details of a specific invoice, purchase, or estimate including all line items.",
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_type": {"type": "string", "enum": ["invoice", "purchase", "estimate"]},
                "doc_id": {"type": "string", "description": "The document ID"}
            },
            "required": ["doc_type", "doc_id"]
        }
    },
    {
        "name": "create_estimate",
        "description": "Create a new estimate/presupuesto in Holded as a draft. Requires contact_id and line items. In safe mode, simulates without writing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "string", "description": "Holded contact ID"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "units": {"type": "number"},
                            "price": {"type": "number"},
                            "tax": {"type": "number", "description": "Tax % (e.g. 21 for IVA)"},
                            "retention": {"type": "number", "description": "IRPF retention %, 0 if N/A"}
                        },
                        "required": ["name", "units", "price"]
                    }
                },
                "desc": {"type": "string", "description": "Description/notes"},
                "date": {"type": "string", "description": "Date YYYY-MM-DD, defaults to today"}
            },
            "required": ["contact_id", "items"]
        }
    },
    {
        "name": "create_invoice",
        "description": "Create a new sales invoice in Holded. Requires contact_id and line items. In safe mode, simulates without writing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "string"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "units": {"type": "number"},
                            "price": {"type": "number"},
                            "tax": {"type": "number"},
                            "retention": {"type": "number"}
                        },
                        "required": ["name", "units", "price"]
                    }
                },
                "desc": {"type": "string"}
            },
            "required": ["contact_id", "items"]
        }
    },
    {
        "name": "send_document",
        "description": "Send a document (invoice, estimate, purchase) via email through Holded's email system. The PDF is attached automatically. In safe mode, simulates without sending.",
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_type": {"type": "string", "enum": ["invoice", "estimate", "purchase"]},
                "doc_id": {"type": "string"},
                "emails": {"type": "array", "items": {"type": "string"}, "description": "Email addresses. If omitted, sends to contact's email on file."},
                "subject": {"type": "string", "description": "Email subject (optional)"},
                "body": {"type": "string", "description": "Email body text (optional)"}
            },
            "required": ["doc_type", "doc_id"]
        }
    },
    {
        "name": "generate_report",
        "description": "Generate a downloadable PDF report from analysis text. Returns a download URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "content": {"type": "string", "description": "Report text content"}
            },
            "required": ["title", "content"]
        }
    },
    {
        "name": "create_contact",
        "description": "Create a new contact (client or supplier) in Holded. In safe mode, simulates without writing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "email": {"type": "string"},
                "type": {"type": "string", "enum": ["client", "supplier", "creditor", "debtor"]},
                "vat": {"type": "string", "description": "Tax ID / NIF / CIF"},
                "phone": {"type": "string"},
                "code": {"type": "string"}
            },
            "required": ["name"]
        }
    }
]

# ─── Tool Executors ──────────────────────────────────────────────────

def _validate_sql(sql):
    s = sql.strip().upper()
    if not s.startswith("SELECT"):
        return False
    dangerous = ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "ATTACH", "DETACH", "PRAGMA"]
    for kw in dangerous:
        if kw in s.split("'")[0] if "'" in s else s:
            # Check outside string literals (simple heuristic)
            parts = s.split("'")
            outside = " ".join(parts[::2])
            if kw in outside.split():
                return False
    return True


def exec_query_database(params):
    sql = params["sql"]
    if not _validate_sql(sql):
        return {"error": "Only SELECT queries are allowed."}
    try:
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(sql)
        rows = [dict(r) for r in cursor.fetchmany(100)]
        conn.close()
        return {"rows": rows, "count": len(rows), "explanation": params.get("explanation", "")}
    except Exception as e:
        return {"error": str(e)}


def exec_get_contact_details(params):
    search = params["search"]
    include_history = params.get("include_history", False)
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM contacts WHERE id = ? OR name LIKE ? LIMIT 10",
                   (search, f"%{search}%"))
    contacts = [dict(r) for r in cursor.fetchall()]

    if include_history and contacts:
        for c in contacts:
            cid = c["id"]
            cursor.execute("SELECT COUNT(*) as cnt, COALESCE(SUM(amount),0) as total FROM invoices WHERE contact_id=?", (cid,))
            inv = dict(cursor.fetchone())
            cursor.execute("SELECT COUNT(*) as cnt, COALESCE(SUM(amount),0) as total FROM purchase_invoices WHERE contact_id=?", (cid,))
            pur = dict(cursor.fetchone())
            cursor.execute("SELECT COUNT(*) as cnt, COALESCE(SUM(amount),0) as total FROM estimates WHERE contact_id=?", (cid,))
            est = dict(cursor.fetchone())
            c["history"] = {"invoices": inv, "purchases": pur, "estimates": est}

    conn.close()
    return {"contacts": contacts, "count": len(contacts)}


def exec_get_product_pricing(params):
    search = params["search"]
    include_history = params.get("include_history", True)
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM products WHERE id = ? OR name LIKE ? OR sku LIKE ? LIMIT 10",
                   (search, f"%{search}%", f"%{search}%"))
    products = [dict(r) for r in cursor.fetchall()]

    if include_history and products:
        for p in products:
            pid = p["id"]
            cursor.execute("""
                SELECT COUNT(*) as sales_count,
                       COALESCE(AVG(price),0) as avg_sale_price,
                       COALESCE(MIN(price),0) as min_sale_price,
                       COALESCE(MAX(price),0) as max_sale_price,
                       COALESCE(SUM(subtotal),0) as total_revenue
                FROM invoice_items WHERE product_id=?
            """, (pid,))
            p["sales"] = dict(cursor.fetchone())

            cursor.execute("""
                SELECT COUNT(*) as purchase_count,
                       COALESCE(AVG(price),0) as avg_purchase_price,
                       COALESCE(MIN(price),0) as min_purchase_price,
                       COALESCE(MAX(price),0) as max_purchase_price,
                       COALESCE(SUM(subtotal),0) as total_cost
                FROM purchase_items WHERE product_id=?
            """, (pid,))
            p["purchases"] = dict(cursor.fetchone())

            avg_sale = p["sales"]["avg_sale_price"]
            avg_cost = p["purchases"]["avg_purchase_price"]
            if avg_cost > 0:
                p["margin_pct"] = round((avg_sale - avg_cost) / avg_cost * 100, 2)
            else:
                p["margin_pct"] = None

    conn.close()
    return {"products": products, "count": len(products)}


def exec_get_financial_summary(params):
    now = datetime.now()
    start_str = params.get("start_date")
    end_str = params.get("end_date")

    if start_str:
        start_dt = datetime.strptime(start_str, "%Y-%m-%d")
    else:
        start_dt = now - timedelta(days=365)
    if end_str:
        end_dt = datetime.strptime(end_str, "%Y-%m-%d")
    else:
        end_dt = now

    start_epoch = int(start_dt.timestamp())
    end_epoch = int(end_dt.timestamp())

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT COALESCE(SUM(amount),0) as total FROM invoices WHERE date>=? AND date<=?",
                   (start_epoch, end_epoch))
    income = cursor.fetchone()["total"]

    cursor.execute("SELECT COALESCE(SUM(amount),0) as total FROM purchase_invoices WHERE date>=? AND date<=?",
                   (start_epoch, end_epoch))
    expenses = cursor.fetchone()["total"]

    cursor.execute("""
        SELECT contact_name, SUM(amount) as total FROM invoices
        WHERE date>=? AND date<=? AND contact_name IS NOT NULL AND contact_name != ''
        GROUP BY contact_name ORDER BY total DESC LIMIT 5
    """, (start_epoch, end_epoch))
    top_clients = [dict(r) for r in cursor.fetchall()]

    cursor.execute("""
        SELECT strftime('%Y-%m', datetime(date, 'unixepoch')) as month, SUM(amount) as total
        FROM invoices WHERE date>=? AND date<=?
        GROUP BY month ORDER BY month
    """, (start_epoch, end_epoch))
    monthly_income = [dict(r) for r in cursor.fetchall()]

    cursor.execute("""
        SELECT strftime('%Y-%m', datetime(date, 'unixepoch')) as month, SUM(amount) as total
        FROM purchase_invoices WHERE date>=? AND date<=?
        GROUP BY month ORDER BY month
    """, (start_epoch, end_epoch))
    monthly_expenses = [dict(r) for r in cursor.fetchall()]

    conn.close()
    return {
        "period": {"start": start_str or start_dt.strftime("%Y-%m-%d"), "end": end_str or end_dt.strftime("%Y-%m-%d")},
        "income": round(income, 2),
        "expenses": round(expenses, 2),
        "balance": round(income - expenses, 2),
        "top_clients": top_clients,
        "monthly_income": monthly_income,
        "monthly_expenses": monthly_expenses
    }


def exec_get_document_details(params):
    doc_type = params["doc_type"]
    doc_id = params["doc_id"]

    table_map = {
        "invoice": ("invoices", "invoice_items", "invoice_id"),
        "purchase": ("purchase_invoices", "purchase_items", "purchase_id"),
        "estimate": ("estimates", "estimate_items", "estimate_id")
    }
    if doc_type not in table_map:
        return {"error": f"Unknown doc_type: {doc_type}"}

    table, items_table, fk = table_map[doc_type]
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute(f"SELECT * FROM {table} WHERE id=?", (doc_id,))
    doc = cursor.fetchone()
    if not doc:
        conn.close()
        return {"error": f"Document {doc_id} not found"}

    cursor.execute(f"SELECT * FROM {items_table} WHERE {fk}=?", (doc_id,))
    items = [dict(r) for r in cursor.fetchall()]
    conn.close()

    return {"document": dict(doc), "items": items}


def exec_create_estimate(params):
    contact_id = params["contact_id"]
    items = params["items"]
    desc = params.get("desc", "")

    products = []
    for item in items:
        p = {"name": item["name"], "units": item["units"], "subtotal": item["price"]}
        if "tax" in item:
            p["tax"] = item["tax"]
        if "retention" in item:
            p["retention"] = item["retention"]
        products.append(p)

    payload = {"contact": contact_id, "desc": desc, "products": products}
    result = connector.post_data("/invoicing/v1/documents/estimate", payload)
    if result:
        is_safe = connector.SAFE_MODE
        return {"success": True, "id": result.get("id", "SAFE_MODE"), "safe_mode": is_safe,
                "message": "Estimate created (dry run)" if is_safe else "Estimate created successfully"}
    return {"success": False, "error": "Failed to create estimate"}


def exec_create_invoice(params):
    contact_id = params["contact_id"]
    items = params["items"]
    desc = params.get("desc", "")

    products = []
    for item in items:
        p = {"name": item["name"], "units": item["units"], "subtotal": item["price"]}
        if "tax" in item:
            p["tax"] = item["tax"]
        if "retention" in item:
            p["retention"] = item["retention"]
        products.append(p)

    payload = {"contact": contact_id, "desc": desc, "products": products}
    result = connector.create_invoice(payload)
    if result:
        is_safe = connector.SAFE_MODE
        return {"success": True, "id": result, "safe_mode": is_safe,
                "message": "Invoice created (dry run)" if is_safe else "Invoice created successfully"}
    return {"success": False, "error": "Failed to create invoice"}


def exec_send_document(params):
    doc_type = params["doc_type"]
    doc_id = params["doc_id"]
    payload = {}
    if "emails" in params:
        payload["emails"] = params["emails"]
    if "subject" in params:
        payload["subject"] = params["subject"]
    if "body" in params:
        payload["body"] = params["body"]

    result = connector.post_data(f"/invoicing/v1/documents/{doc_type}/{doc_id}/send", payload)
    if result:
        is_safe = connector.SAFE_MODE
        return {"success": True, "safe_mode": is_safe,
                "message": "Document sent (dry run)" if is_safe else "Document sent successfully"}
    return {"success": False, "error": "Failed to send document"}


def exec_generate_report(params):
    title = params["title"]
    content = params["content"]
    filename = f"ai_report_{int(time.time())}.pdf"
    filepath = reports.generate_pdf_report(f"{title}\n\n{content}", filename)
    download_url = f"/api/reports/download/{os.path.basename(filepath)}"
    return {"success": True, "download_url": download_url, "filename": os.path.basename(filepath)}


def exec_create_contact(params):
    payload = {"name": params["name"]}
    for key in ["email", "type", "vat", "phone", "code"]:
        if key in params:
            payload[key] = params[key]

    result = connector.create_contact(payload)
    if result:
        is_safe = connector.SAFE_MODE
        return {"success": True, "id": result, "safe_mode": is_safe,
                "message": "Contact created (dry run)" if is_safe else "Contact created successfully"}
    return {"success": False, "error": "Failed to create contact"}


TOOL_EXECUTORS = {
    "query_database": exec_query_database,
    "get_contact_details": exec_get_contact_details,
    "get_product_pricing": exec_get_product_pricing,
    "get_financial_summary": exec_get_financial_summary,
    "get_document_details": exec_get_document_details,
    "create_estimate": exec_create_estimate,
    "create_invoice": exec_create_invoice,
    "send_document": exec_send_document,
    "generate_report": exec_generate_report,
    "create_contact": exec_create_contact,
}

# ─── System Prompt Builder ───────────────────────────────────────────

def build_system_prompt():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    stats = {}
    for table in ["invoices", "purchase_invoices", "estimates", "products", "contacts"]:
        cursor.execute(f"SELECT COUNT(*) as c FROM {table}")
        stats[table] = cursor.fetchone()["c"]

    cursor.execute("SELECT COALESCE(SUM(amount),0) as t FROM invoices")
    total_income = cursor.fetchone()["t"]
    cursor.execute("SELECT COALESCE(SUM(amount),0) as t FROM purchase_invoices")
    total_expenses = cursor.fetchone()["t"]
    conn.close()

    safe_status = "ON (dry run - writes are simulated)" if connector.SAFE_MODE else "OFF (writes execute against Holded)"

    return f"""You are the financial assistant for this company's Holded accounting system.
You help analyze financial data, check prices, create estimates/invoices, generate reports, and send documents.

DATABASE (SQLite, read via query_database tool):
- invoices (id, contact_id, contact_name, desc, date[epoch], amount[EUR], status 0-5)
- invoice_items (invoice_id, product_id, name, sku, units, price, subtotal, discount, tax, retention, account)
- purchase_invoices (same as invoices), purchase_items (purchase_id, ...)
- estimates (same as invoices), estimate_items (estimate_id, ...)
- contacts (id, name, email, type, code, vat, phone, mobile)
- products (id, name, desc, price, stock, sku)
- payments (id, document_id, amount, date, method, type)
- projects (id, name, desc, status, customer_id, budget)
- ledger_accounts (id, name, num)

Status codes - invoices/purchases: 0=draft, 1=issued, 2=partial, 3=paid, 4=overdue, 5=cancelled. Estimates: 0=draft, 1=pending, 2=accepted, 3=rejected, 4=invoiced.
Dates are Unix epoch. Convert with: datetime(date, 'unixepoch'). Group by month: strftime('%Y-%m', datetime(date, 'unixepoch')).

CURRENT DATA: {stats['invoices']} invoices, {stats['purchase_invoices']} purchases, {stats['estimates']} estimates, {stats['contacts']} contacts, {stats['products']} products.
Total income: {total_income:,.2f} EUR | Total expenses: {total_expenses:,.2f} EUR | Balance: {total_income - total_expenses:,.2f} EUR

SAFE MODE: {safe_status}

RULES:
- Always query the database to verify data before creating documents.
- For write operations, clearly describe what will be created before executing.
- Match the user's language (Spanish if they write in Spanish).
- Be concise but thorough in financial analysis.
- When showing amounts, use EUR format with 2 decimals."""

# ─── Conversation History ────────────────────────────────────────────

def _ensure_ai_history_schema():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""CREATE TABLE IF NOT EXISTS ai_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        role TEXT,
        content TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        conversation_id TEXT DEFAULT 'default',
        tool_calls TEXT
    )""")
    # Add columns if they don't exist (migration for existing tables)
    try:
        cursor.execute("ALTER TABLE ai_history ADD COLUMN conversation_id TEXT DEFAULT 'default'")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE ai_history ADD COLUMN tool_calls TEXT")
    except sqlite3.OperationalError:
        pass
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ai_history_conv ON ai_history(conversation_id, timestamp)")
    conn.commit()
    conn.close()


def load_history(conversation_id, limit=20):
    _ensure_ai_history_schema()
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT role, content FROM ai_history WHERE conversation_id=? ORDER BY timestamp DESC LIMIT ?",
        (conversation_id, limit)
    )
    rows = cursor.fetchall()
    conn.close()
    rows.reverse()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def save_history(conversation_id, role, content, tool_calls=None):
    _ensure_ai_history_schema()
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO ai_history (role, content, conversation_id, tool_calls) VALUES (?, ?, ?, ?)",
        (role, content, conversation_id, json.dumps(tool_calls) if tool_calls else None)
    )
    conn.commit()
    conn.close()


def get_history(conversation_id=None):
    _ensure_ai_history_schema()
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if conversation_id:
        cursor.execute(
            "SELECT role, content, timestamp FROM ai_history WHERE conversation_id=? ORDER BY timestamp ASC",
            (conversation_id,)
        )
    else:
        cursor.execute("SELECT role, content, timestamp FROM ai_history ORDER BY timestamp DESC LIMIT 50")
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def clear_history(conversation_id=None):
    _ensure_ai_history_schema()
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    if conversation_id:
        cursor.execute("DELETE FROM ai_history WHERE conversation_id=?", (conversation_id,))
    else:
        cursor.execute("DELETE FROM ai_history")
    conn.commit()
    conn.close()

# ─── Rate Limiting ───────────────────────────────────────────────────

def check_rate_limit(ip="local"):
    now = time.time()
    if ip not in _rate_limits:
        _rate_limits[ip] = []
    _rate_limits[ip] = [t for t in _rate_limits[ip] if now - t < RATE_LIMIT_WINDOW]
    if len(_rate_limits[ip]) >= RATE_LIMIT_MAX:
        return False
    _rate_limits[ip].append(now)
    return True

# ─── Cleanup expired pending actions ────────────────────────────────

def _cleanup_pending():
    now = time.time()
    expired = [k for k, v in pending_actions.items() if v["expires_at"] < now]
    for k in expired:
        del pending_actions[k]

# ─── Get Claude API Key ─────────────────────────────────────────────

def _get_api_key():
    key = connector.get_setting("claude_api_key")
    if key:
        return key
    return os.getenv("ANTHROPIC_API_KEY", "")

def _get_model():
    model = connector.get_setting("ai_model")
    if model and model.startswith("claude"):
        return model
    return "claude-sonnet-4-20250514"

# ─── Main Chat Function ─────────────────────────────────────────────

def chat(user_message, conversation_id=None):
    if not conversation_id:
        conversation_id = str(uuid.uuid4())

    api_key = _get_api_key()
    if not api_key:
        return {
            "type": "error",
            "content": "Claude API key not configured. Add it in Settings or set ANTHROPIC_API_KEY env var.",
            "conversation_id": conversation_id
        }

    client = anthropic.Anthropic(api_key=api_key)
    model = _get_model()

    # Load history and build messages
    history = load_history(conversation_id, limit=20)
    system_prompt = build_system_prompt()
    messages = history + [{"role": "user", "content": user_message}]

    tool_calls_summary = []

    try:
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=system_prompt,
            tools=TOOL_DEFINITIONS,
            messages=messages
        )

        # Agent loop: handle tool_use
        max_iterations = 10
        iteration = 0
        while response.stop_reason == "tool_use" and iteration < max_iterations:
            iteration += 1
            tool_results = []

            for block in response.content:
                if block.type == "tool_use":
                    tool_name = block.name
                    tool_input = block.input
                    logger.info(f"Agent calling tool: {tool_name}")

                    # If write tool, pause for confirmation
                    if tool_name in WRITE_TOOLS:
                        state_id = str(uuid.uuid4())
                        _cleanup_pending()

                        # Build description for user
                        desc = _describe_write_action(tool_name, tool_input)

                        pending_actions[state_id] = {
                            "messages": messages + [{"role": "assistant", "content": [b.model_dump() for b in response.content]}],
                            "tool_block": block.model_dump(),
                            "conversation_id": conversation_id,
                            "expires_at": time.time() + 300,
                            "model": model,
                            "system_prompt": system_prompt,
                            "user_message": user_message
                        }

                        return {
                            "type": "confirmation_needed",
                            "action": {
                                "tool": tool_name,
                                "description": desc,
                                "details": tool_input
                            },
                            "pending_state_id": state_id,
                            "conversation_id": conversation_id
                        }

                    # Execute read/utility tool
                    executor = TOOL_EXECUTORS.get(tool_name)
                    if executor:
                        result = executor(tool_input)
                    else:
                        result = {"error": f"Unknown tool: {tool_name}"}

                    tool_calls_summary.append({"tool": tool_name, "description": tool_input.get("explanation", tool_name)})
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, default=str)
                    })

            # Continue the conversation with tool results
            messages.append({"role": "assistant", "content": [b.model_dump() for b in response.content]})
            messages.append({"role": "user", "content": tool_results})

            response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=system_prompt,
                tools=TOOL_DEFINITIONS,
                messages=messages
            )

        # Extract final text
        final_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                final_text += block.text

        # Save to history
        save_history(conversation_id, "user", user_message)
        save_history(conversation_id, "assistant", final_text, tool_calls_summary or None)

        return {
            "type": "message",
            "content": final_text,
            "conversation_id": conversation_id,
            "tool_calls_summary": tool_calls_summary
        }

    except anthropic.APIError as e:
        logger.error(f"Claude API error: {e}")
        return {"type": "error", "content": f"AI service error: {str(e)}", "conversation_id": conversation_id}
    except Exception as e:
        logger.error(f"Agent error: {e}", exc_info=True)
        return {"type": "error", "content": f"An error occurred: {str(e)}", "conversation_id": conversation_id}


def confirm_action(state_id, confirmed):
    _cleanup_pending()
    state = pending_actions.pop(state_id, None)
    if not state:
        return {"type": "error", "content": "This action has expired. Please try again."}

    conversation_id = state["conversation_id"]
    tool_block = state["tool_block"]
    tool_name = tool_block["name"]
    tool_input = tool_block["input"]

    if not confirmed:
        save_history(conversation_id, "user", state["user_message"])
        save_history(conversation_id, "assistant", f"Action cancelled: {tool_name}")
        return {"type": "message", "content": "Action cancelled.", "conversation_id": conversation_id}

    # Execute the write tool
    executor = TOOL_EXECUTORS.get(tool_name)
    if not executor:
        return {"type": "error", "content": f"Unknown tool: {tool_name}"}

    result = executor(tool_input)

    # Resume the agent loop with the tool result
    messages = state["messages"]
    messages.append({"role": "user", "content": [{
        "type": "tool_result",
        "tool_use_id": tool_block["id"],
        "content": json.dumps(result, default=str)
    }]})

    api_key = _get_api_key()
    client = anthropic.Anthropic(api_key=api_key)

    try:
        response = client.messages.create(
            model=state["model"],
            max_tokens=4096,
            system=state["system_prompt"],
            tools=TOOL_DEFINITIONS,
            messages=messages
        )

        final_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                final_text += block.text

        save_history(conversation_id, "user", state["user_message"])
        save_history(conversation_id, "assistant", final_text, [{"tool": tool_name, "confirmed": True}])

        return {
            "type": "message",
            "content": final_text,
            "conversation_id": conversation_id,
            "tool_calls_summary": [{"tool": tool_name, "description": "Confirmed and executed"}]
        }
    except Exception as e:
        logger.error(f"Agent error after confirmation: {e}", exc_info=True)
        return {"type": "error", "content": f"Error: {str(e)}", "conversation_id": conversation_id}


def _describe_write_action(tool_name, tool_input):
    if tool_name == "create_estimate":
        items = tool_input.get("items", [])
        total = sum(i.get("units", 1) * i.get("price", 0) for i in items)
        return f"Create an estimate with {len(items)} items (subtotal: {total:,.2f} EUR)"
    elif tool_name == "create_invoice":
        items = tool_input.get("items", [])
        total = sum(i.get("units", 1) * i.get("price", 0) for i in items)
        return f"Create an invoice with {len(items)} items (subtotal: {total:,.2f} EUR)"
    elif tool_name == "send_document":
        emails = tool_input.get("emails", ["contact's email on file"])
        return f"Send {tool_input['doc_type']} {tool_input['doc_id']} to {', '.join(emails)}"
    elif tool_name == "create_contact":
        return f"Create contact: {tool_input.get('name', 'Unknown')}"
    return f"Execute {tool_name}"
