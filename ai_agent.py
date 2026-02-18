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

WRITE_TOOLS = {"create_estimate", "create_invoice", "send_document", "create_contact", "update_invoice_status", "upload_file"}

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
    },
    {
        "name": "get_overdue_invoices",
        "description": "Get all overdue invoices (status=4) or invoices past their due date. Returns a list sorted by amount descending.",
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["receivable", "payable", "both"], "description": "receivable=sales invoices, payable=purchase invoices, both=all. Default: both"},
                "min_amount": {"type": "number", "description": "Minimum amount filter"}
            }
        }
    },
    {
        "name": "get_upcoming_payments",
        "description": "Get recent and upcoming payments, optionally filtered by date range. Shows payment method, amounts, and linked documents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days_ahead": {"type": "integer", "description": "Number of days to look ahead from today (default: 30)"},
                "type": {"type": "string", "enum": ["income", "expense", "both"], "description": "Filter by payment type. Default: both"}
            }
        }
    },
    {
        "name": "compare_periods",
        "description": "Compare financial performance between two time periods. Returns income, expenses, balance, and percentage changes for each period.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period1_start": {"type": "string", "description": "Start date YYYY-MM-DD for first period"},
                "period1_end": {"type": "string", "description": "End date YYYY-MM-DD for first period"},
                "period2_start": {"type": "string", "description": "Start date YYYY-MM-DD for second period"},
                "period2_end": {"type": "string", "description": "End date YYYY-MM-DD for second period"}
            },
            "required": ["period1_start", "period1_end", "period2_start", "period2_end"]
        }
    },
    {
        "name": "update_invoice_status",
        "description": "Update the status of an invoice or purchase invoice in Holded (e.g., mark as paid). In safe mode, simulates without writing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_type": {"type": "string", "enum": ["invoice", "purchase"]},
                "doc_id": {"type": "string", "description": "The document ID"},
                "status": {"type": "integer", "description": "New status: 0=draft, 1=issued, 2=partial, 3=paid, 4=overdue, 5=cancelled"}
            },
            "required": ["doc_type", "doc_id", "status"]
        }
    },
    {
        "name": "render_chart",
        "description": "Render an inline chart in the chat response. Use this when the user asks for visual data or graphs. The chart will be rendered using Chart.js in the chat panel.",
        "input_schema": {
            "type": "object",
            "properties": {
                "chart_type": {"type": "string", "enum": ["bar", "line", "doughnut", "pie"], "description": "Type of chart"},
                "title": {"type": "string", "description": "Chart title"},
                "labels": {"type": "array", "items": {"type": "string"}, "description": "Labels for each data point (x-axis or segments)"},
                "datasets": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "data": {"type": "array", "items": {"type": "number"}}
                        },
                        "required": ["label", "data"]
                    },
                    "description": "One or more data series"
                }
            },
            "required": ["chart_type", "title", "labels", "datasets"]
        }
    },
    {
        "name": "analyze_file",
        "description": "Analyze an uploaded CSV or Excel file. Returns data summary, columns, data types, and statistical analysis.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Name of the uploaded file (e.g., 'sales.csv' or '1234567890_data.xlsx')"},
                "analysis_type": {"type": "string", "enum": ["summary", "trends", "anomalies"], "description": "Type of analysis to perform", "default": "summary"}
            },
            "required": ["filename"]
        }
    },
    {
        "name": "list_files",
        "description": "List recently uploaded files or generated reports. Useful for referencing files by name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "directory": {"type": "string", "enum": ["uploads", "reports"], "description": "Which directory to list"},
                "limit": {"type": "integer", "description": "Maximum files to return (default: 20)", "default": 20}
            },
            "required": ["directory"]
        }
    },
    {
        "name": "upload_file",
        "description": "Register an uploaded file for processing. This is a write operation that requires user confirmation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Name of the file that was uploaded"},
                "description": {"type": "string", "description": "Optional description of file contents"}
            },
            "required": ["filename"]
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


def exec_get_overdue_invoices(params):
    inv_type = params.get("type", "both")
    min_amount = params.get("min_amount", 0)
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    results = []
    if inv_type in ("receivable", "both"):
        cursor.execute(
            "SELECT id, contact_name, amount, date, status FROM invoices WHERE status = 4 AND amount >= ? ORDER BY amount DESC",
            (min_amount,))
        results.extend([{**dict(r), "source": "receivable"} for r in cursor.fetchall()])

    if inv_type in ("payable", "both"):
        cursor.execute(
            "SELECT id, contact_name, amount, date, status FROM purchase_invoices WHERE status = 4 AND amount >= ? ORDER BY amount DESC",
            (min_amount,))
        results.extend([{**dict(r), "source": "payable"} for r in cursor.fetchall()])

    conn.close()
    total = sum(r["amount"] or 0 for r in results)
    return {"overdue": results, "count": len(results), "total_overdue": round(total, 2)}


def exec_get_upcoming_payments(params):
    days = params.get("days_ahead", 30)
    ptype = params.get("type", "both")
    now = int(time.time())
    future = now + days * 86400

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    where_type = ""
    if ptype == "income":
        where_type = "AND type = 'income'"
    elif ptype == "expense":
        where_type = "AND type = 'expense'"

    cursor.execute(f"""
        SELECT id, document_id, amount, date, method, type
        FROM payments
        WHERE date >= ? AND date <= ? {where_type}
        ORDER BY date ASC
    """, (now - 30 * 86400, future))
    payments = [dict(r) for r in cursor.fetchall()]
    conn.close()

    total = sum(p["amount"] or 0 for p in payments)
    return {"payments": payments, "count": len(payments), "total": round(total, 2)}


def exec_compare_periods(params):
    def _period_stats(cursor, start_str, end_str):
        start_epoch = int(datetime.strptime(start_str, "%Y-%m-%d").timestamp())
        end_epoch = int(datetime.strptime(end_str, "%Y-%m-%d").timestamp()) + 86399

        cursor.execute("SELECT COALESCE(SUM(amount),0) as t FROM invoices WHERE date>=? AND date<=?", (start_epoch, end_epoch))
        income = cursor.fetchone()["t"]
        cursor.execute("SELECT COALESCE(SUM(amount),0) as t FROM purchase_invoices WHERE date>=? AND date<=?", (start_epoch, end_epoch))
        expenses = cursor.fetchone()["t"]
        cursor.execute("SELECT COUNT(*) as c FROM invoices WHERE date>=? AND date<=?", (start_epoch, end_epoch))
        inv_count = cursor.fetchone()["c"]
        cursor.execute("SELECT COUNT(*) as c FROM purchase_invoices WHERE date>=? AND date<=?", (start_epoch, end_epoch))
        pur_count = cursor.fetchone()["c"]

        return {
            "period": f"{start_str} to {end_str}",
            "income": round(income, 2),
            "expenses": round(expenses, 2),
            "balance": round(income - expenses, 2),
            "invoice_count": inv_count,
            "purchase_count": pur_count
        }

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    p1 = _period_stats(cursor, params["period1_start"], params["period1_end"])
    p2 = _period_stats(cursor, params["period2_start"], params["period2_end"])
    conn.close()

    def _pct(old, new):
        if old == 0:
            return None
        return round((new - old) / abs(old) * 100, 2)

    return {
        "period1": p1,
        "period2": p2,
        "changes": {
            "income_pct": _pct(p1["income"], p2["income"]),
            "expenses_pct": _pct(p1["expenses"], p2["expenses"]),
            "balance_pct": _pct(p1["balance"], p2["balance"])
        }
    }


def exec_update_invoice_status(params):
    doc_type = params["doc_type"]
    doc_id = params["doc_id"]
    status = params["status"]

    type_map = {"invoice": "invoice", "purchase": "purchase"}
    holded_type = type_map.get(doc_type)
    if not holded_type:
        return {"error": f"Unknown doc_type: {doc_type}"}

    result = connector.put_data(f"/invoicing/v1/documents/{holded_type}/{doc_id}", {"status": status})
    if result:
        is_safe = connector.SAFE_MODE
        return {"success": True, "safe_mode": is_safe,
                "message": f"Status updated to {status} (dry run)" if is_safe else f"Status updated to {status}"}
    return {"success": False, "error": "Failed to update status"}


def exec_render_chart(params):
    return {
        "chart_type": params["chart_type"],
        "title": params["title"],
        "labels": params["labels"],
        "datasets": params["datasets"]
    }

def exec_analyze_file(params):
    """Analyze an uploaded CSV or Excel file."""
    import pandas as pd

    filename = params.get("filename", "")
    analysis_type = params.get("analysis_type", "summary")

    # Path traversal prevention
    safe_name = os.path.basename(filename)
    uploads_dir = connector.get_uploads_dir()
    filepath = os.path.join(uploads_dir, safe_name)

    # Validate file exists and is within uploads dir
    if not os.path.abspath(filepath).startswith(uploads_dir):
        return {"error": f"Invalid filename: {filename}"}

    if not os.path.exists(filepath):
        return {"error": f"File not found: {filename}"}

    # Parse file
    try:
        if filename.endswith((".csv", ".xlsx", ".xls")):
            if filename.endswith(".csv"):
                df = pd.read_csv(filepath)
            else:
                df = pd.read_excel(filepath)

            analysis = {
                "filename": filename,
                "rows": df.shape[0],
                "columns": df.shape[1],
                "column_names": list(df.columns),
                "dtypes": {col: str(dtype) for col, dtype in df.dtypes.items()}
            }

            if analysis_type in ["summary", "trends"]:
                try:
                    analysis["summary"] = df.describe().to_dict()
                except:
                    pass

            if analysis_type == "anomalies":
                # Simple anomaly detection for numeric columns
                numeric_cols = df.select_dtypes(include=['number']).columns
                anomalies = {}
                for col in numeric_cols:
                    q1 = df[col].quantile(0.25)
                    q3 = df[col].quantile(0.75)
                    iqr = q3 - q1
                    lower = q1 - 1.5 * iqr
                    upper = q3 + 1.5 * iqr
                    count = len(df[(df[col] < lower) | (df[col] > upper)])
                    if count > 0:
                        anomalies[col] = count
                analysis["anomalies"] = anomalies

            return {"success": True, "analysis": analysis}
        else:
            return {"error": "Unsupported file type (only CSV/Excel)"}
    except Exception as e:
        return {"error": f"File parsing error: {str(e)}"}

def exec_list_files(params):
    """List files in uploads or reports directory."""
    directory = params.get("directory", "uploads")
    limit = params.get("limit", 20)

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

        return {"success": True, "files": files, "count": len(files)}
    except Exception as e:
        return {"error": f"Error listing files: {str(e)}"}

def exec_upload_file(params):
    """Register uploaded file (write operation - requires confirmation)."""
    filename = params.get("filename", "")
    description = params.get("description", "")

    # Path traversal prevention
    safe_name = os.path.basename(filename)
    uploads_dir = connector.get_uploads_dir()
    filepath = os.path.join(uploads_dir, safe_name)

    if not os.path.exists(filepath):
        return {"error": f"File not found in uploads: {filename}"}

    try:
        # Record in ai_history for audit trail
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO ai_history (role, content, conversation_id, tool_calls)
            VALUES (?, ?, ?, ?)
        """, ("system", f"File uploaded: {filename}", "default", json.dumps({
            "tool": "upload_file",
            "description": description,
            "filename": filename,
            "size": os.path.getsize(filepath)
        })))
        conn.commit()
        conn.close()

        return {
            "success": True,
            "filename": filename,
            "size": os.path.getsize(filepath),
            "message": f"File '{filename}' registered for processing"
        }
    except Exception as e:
        return {"error": f"Failed to register file: {str(e)}"}


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
    "get_overdue_invoices": exec_get_overdue_invoices,
    "get_upcoming_payments": exec_get_upcoming_payments,
    "compare_periods": exec_compare_periods,
    "update_invoice_status": exec_update_invoice_status,
    "render_chart": exec_render_chart,
    "analyze_file": exec_analyze_file,
    "list_files": exec_list_files,
    "upload_file": exec_upload_file,
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
- When showing amounts, use EUR format with 2 decimals.
- Use render_chart to show inline charts when the user asks for visual data, trends, or comparisons.
- Use get_overdue_invoices to find overdue/unpaid invoices.
- Use compare_periods for period-over-period analysis (e.g., this month vs last month)."""

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
    charts = []

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

                    if tool_name == "render_chart":
                        charts.append(result)
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

        result = {
            "type": "message",
            "content": final_text,
            "conversation_id": conversation_id,
            "tool_calls_summary": tool_calls_summary
        }
        if charts:
            result["charts"] = charts
        return result

    except anthropic.APIError as e:
        logger.error(f"Claude API error: {e}")
        return {"type": "error", "content": f"AI service error: {str(e)}", "conversation_id": conversation_id}
    except Exception as e:
        logger.error(f"Agent error: {e}", exc_info=True)
        return {"type": "error", "content": f"An error occurred: {str(e)}", "conversation_id": conversation_id}


def chat_stream(user_message, conversation_id=None):
    """Generator that yields SSE events for streaming responses."""
    if not conversation_id:
        conversation_id = str(uuid.uuid4())

    api_key = _get_api_key()
    if not api_key:
        yield {"event": "error", "data": json.dumps({"content": "Claude API key not configured.", "conversation_id": conversation_id})}
        return

    client = anthropic.Anthropic(api_key=api_key)
    model = _get_model()

    history = load_history(conversation_id, limit=20)
    system_prompt = build_system_prompt()
    messages = history + [{"role": "user", "content": user_message}]
    tool_calls_summary = []
    charts = []

    try:
        max_iterations = 10
        iteration = 0
        needs_tool_loop = True

        while needs_tool_loop and iteration < max_iterations:
            needs_tool_loop = False
            iteration += 1

            # Use streaming for the final text response, non-streaming for tool loops
            response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=system_prompt,
                tools=TOOL_DEFINITIONS,
                messages=messages
            )

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        tool_name = block.name
                        tool_input = block.input
                        logger.info(f"[stream] Agent calling tool: {tool_name}")
                        yield {"event": "tool_start", "data": json.dumps({"tool": tool_name})}

                        if tool_name in WRITE_TOOLS:
                            state_id = str(uuid.uuid4())
                            _cleanup_pending()
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
                            yield {"event": "confirmation_needed", "data": json.dumps({
                                "action": {"tool": tool_name, "description": desc, "details": tool_input},
                                "pending_state_id": state_id,
                                "conversation_id": conversation_id
                            })}
                            return

                        executor = TOOL_EXECUTORS.get(tool_name)
                        if executor:
                            result = executor(tool_input)
                        else:
                            result = {"error": f"Unknown tool: {tool_name}"}

                        if tool_name == "render_chart":
                            charts.append(result)
                        tool_calls_summary.append({"tool": tool_name, "description": tool_input.get("explanation", tool_name)})
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result, default=str)
                        })

                messages.append({"role": "assistant", "content": [b.model_dump() for b in response.content]})
                messages.append({"role": "user", "content": tool_results})
                needs_tool_loop = True
                continue

            # Final response - stream it
            # We already have the full response, extract text
            final_text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    final_text += block.text

            # Send meta info first
            if tool_calls_summary:
                yield {"event": "tools_used", "data": json.dumps(tool_calls_summary)}
            if charts:
                yield {"event": "charts", "data": json.dumps(charts)}

            # Stream text in chunks
            chunk_size = 20
            for i in range(0, len(final_text), chunk_size):
                chunk = final_text[i:i+chunk_size]
                yield {"event": "text_delta", "data": json.dumps({"text": chunk})}

            yield {"event": "done", "data": json.dumps({"conversation_id": conversation_id})}

            save_history(conversation_id, "user", user_message)
            save_history(conversation_id, "assistant", final_text, tool_calls_summary or None)

    except anthropic.APIError as e:
        logger.error(f"[stream] Claude API error: {e}")
        yield {"event": "error", "data": json.dumps({"content": f"AI service error: {str(e)}"})}
    except Exception as e:
        logger.error(f"[stream] Agent error: {e}", exc_info=True)
        yield {"event": "error", "data": json.dumps({"content": f"Error: {str(e)}"})}


# ─── Favorites ──────────────────────────────────────────────────────

def _ensure_favorites_schema():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""CREATE TABLE IF NOT EXISTS ai_favorites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        query TEXT NOT NULL,
        label TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()
    conn.close()

def get_favorites():
    _ensure_favorites_schema()
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT id, query, label, created_at FROM ai_favorites ORDER BY created_at DESC")
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows

def add_favorite(query, label=None):
    _ensure_favorites_schema()
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO ai_favorites (query, label) VALUES (?, ?)", (query, label or query[:50]))
    conn.commit()
    fav_id = cursor.lastrowid
    conn.close()
    return fav_id

def remove_favorite(fav_id):
    _ensure_favorites_schema()
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM ai_favorites WHERE id = ?", (fav_id,))
    conn.commit()
    conn.close()

def get_conversations():
    _ensure_ai_history_schema()
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT conversation_id, MIN(timestamp) as started, MAX(timestamp) as last_msg,
               COUNT(*) as msg_count,
               (SELECT content FROM ai_history h2 WHERE h2.conversation_id = ai_history.conversation_id AND h2.role = 'user' ORDER BY h2.timestamp ASC LIMIT 1) as first_message
        FROM ai_history
        WHERE conversation_id IS NOT NULL AND conversation_id != 'default'
        GROUP BY conversation_id
        ORDER BY last_msg DESC
        LIMIT 20
    """)
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


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
    elif tool_name == "update_invoice_status":
        status_labels = {0: "draft", 1: "issued", 2: "partial", 3: "paid", 4: "overdue", 5: "cancelled"}
        st = status_labels.get(tool_input.get("status"), str(tool_input.get("status")))
        return f"Update {tool_input.get('doc_type')} {tool_input.get('doc_id')} status to {st}"
    return f"Execute {tool_name}"
