"""Tool executor functions for the AI agent. Each exec_* handles one tool call."""
import json
import os
import logging
import re
import time
from datetime import datetime, timedelta
import connector
from write_gateway import gateway
import reports
from app.agent.tools import _month_fmt

logger = logging.getLogger(__name__)


def _validate_sql(sql):
    """Validate SQL is read-only. Mirrors Brain's sql.ts protections."""
    # Block semicolons (prevents statement stacking)
    if ";" in sql:
        return False, "Semicolons not allowed — use a single SELECT statement"

    # Block escape sequences that could hide mutation keywords
    if re.search(r'\\x[0-9a-f]', sql, re.IGNORECASE) or "U&'" in sql.upper():
        return False, "Escape sequences not allowed"

    # Strip comments before checking keywords (prevents hiding mutations in comments)
    normalized = re.sub(r'--[^\n]*', '', sql)           # Line comments
    normalized = re.sub(r'/\*[\s\S]*?\*/', '', normalized)  # Block comments
    normalized = normalized.upper()

    # Check for mutation keywords anywhere in the query
    mutation_keywords = [
        "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE",
        "GRANT", "REVOKE", "COPY", "EXECUTE", "CALL", "ATTACH", "DETACH",
        "PRAGMA", "VACUUM", "REINDEX",
    ]
    for kw in mutation_keywords:
        if re.search(r'\b' + kw + r'\b', normalized):
            return False, f"Keyword {kw} not allowed — only SELECT queries"

    # Block dangerous PostgreSQL functions that can read filesystem or leak data
    dangerous_functions = [
        "PG_READ_FILE", "PG_READ_BINARY_FILE", "PG_WRITE_FILE",
        "LO_IMPORT", "LO_EXPORT", "LO_GET", "LO_PUT",
        "DBLINK", "PG_SHADOW", "PG_AUTHID", "PG_ROLES",
        "CURRENT_SETTING", "SET_CONFIG", "PG_RELOAD_CONF",
        "PG_TERMINATE_BACKEND", "PG_CANCEL_BACKEND",
        "PG_SLEEP",
    ]
    for fn in dangerous_functions:
        if fn in normalized:
            return False, f"Function {fn} not allowed"

    # Must start with SELECT or WITH (after stripping comments)
    trimmed = normalized.strip()
    if not trimmed.startswith("SELECT") and not trimmed.startswith("WITH"):
        return False, "Query must start with SELECT or WITH"

    return True, None


def exec_query_database(params):
    sql = params["sql"]
    # Limit query length
    if len(sql) > 2000:
        return {"error": "Query too long (max 2000 chars)"}

    valid, error_msg = _validate_sql(sql)
    if not valid:
        return {"error": error_msg}

    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)
        # Set query timeout to prevent runaway queries (PostgreSQL only)
        if not connector._USE_SQLITE:
            cursor.execute("SET statement_timeout = '10s'")
        cursor.execute(connector._q(sql))
        rows = [dict(r) for r in cursor.fetchmany(100)]
        return {"rows": rows, "count": len(rows), "explanation": params.get("explanation", "")}
    except Exception as e:
        # Don't leak full error details — log server-side, return generic message
        logger.error(f"SQL query error: {e}")
        return {"error": "Query execution failed"}
    finally:
        connector.release_db(conn)


def exec_get_contact_details(params):
    search = params["search"]
    include_history = params.get("include_history", False)
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)

        cursor.execute(connector._q("SELECT * FROM contacts WHERE id = ? OR name LIKE ? LIMIT 10"),
                       (search, f"%{search}%"))
        contacts = [dict(r) for r in cursor.fetchall()]

        if include_history and contacts:
            cids = [c["id"] for c in contacts]
            ph = ','.join(['?' if connector._USE_SQLITE else '%s'] * len(cids))
            cursor.execute(connector._q(f"""
                SELECT contact_id, 'invoices' as doc_type, COUNT(*) as cnt, COALESCE(SUM(amount),0) as total
                FROM invoices WHERE contact_id IN ({ph}) GROUP BY contact_id
                UNION ALL
                SELECT contact_id, 'purchases', COUNT(*), COALESCE(SUM(amount),0)
                FROM purchase_invoices WHERE contact_id IN ({ph}) GROUP BY contact_id
                UNION ALL
                SELECT contact_id, 'estimates', COUNT(*), COALESCE(SUM(amount),0)
                FROM estimates WHERE contact_id IN ({ph}) GROUP BY contact_id
            """), cids * 3)
            hist_map = {}
            for r in cursor.fetchall():
                r = dict(r)
                hist_map.setdefault(r["contact_id"], {})[r["doc_type"]] = {"cnt": r["cnt"], "total": r["total"]}
            for c in contacts:
                h = hist_map.get(c["id"], {})
                empty = {"cnt": 0, "total": 0}
                c["history"] = {"invoices": h.get("invoices", empty), "purchases": h.get("purchases", empty), "estimates": h.get("estimates", empty)}

        return {"contacts": contacts, "count": len(contacts)}
    finally:
        connector.release_db(conn)


def exec_get_product_pricing(params):
    search = params["search"]
    include_history = params.get("include_history", True)
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)

        cursor.execute(connector._q("SELECT * FROM products WHERE id = ? OR name LIKE ? OR sku LIKE ? LIMIT 10"),
                       (search, f"%{search}%", f"%{search}%"))
        products = [dict(r) for r in cursor.fetchall()]

        if include_history and products:
            pids = [p["id"] for p in products]
            ph = ','.join(['?' if connector._USE_SQLITE else '%s'] * len(pids))

            cursor.execute(connector._q(f"""
                SELECT product_id, COUNT(*) as sales_count,
                       COALESCE(AVG(price),0) as avg_sale_price,
                       COALESCE(MIN(price),0) as min_sale_price,
                       COALESCE(MAX(price),0) as max_sale_price,
                       COALESCE(SUM(subtotal),0) as total_revenue
                FROM invoice_items WHERE product_id IN ({ph}) GROUP BY product_id
            """), pids)
            sales_map = {dict(r)["product_id"]: dict(r) for r in cursor.fetchall()}

            cursor.execute(connector._q(f"""
                SELECT product_id, COUNT(*) as purchase_count,
                       COALESCE(AVG(price),0) as avg_purchase_price,
                       COALESCE(MIN(price),0) as min_purchase_price,
                       COALESCE(MAX(price),0) as max_purchase_price,
                       COALESCE(SUM(subtotal),0) as total_cost
                FROM purchase_items WHERE product_id IN ({ph}) GROUP BY product_id
            """), pids)
            purch_map = {dict(r)["product_id"]: dict(r) for r in cursor.fetchall()}

            empty_sales = {"sales_count": 0, "avg_sale_price": 0, "min_sale_price": 0, "max_sale_price": 0, "total_revenue": 0}
            empty_purch = {"purchase_count": 0, "avg_purchase_price": 0, "min_purchase_price": 0, "max_purchase_price": 0, "total_cost": 0}
            for p in products:
                p["sales"] = sales_map.get(p["id"], empty_sales)
                p["purchases"] = purch_map.get(p["id"], empty_purch)
                avg_sale = p["sales"]["avg_sale_price"]
                avg_cost = p["purchases"]["avg_purchase_price"]
                if avg_cost > 0:
                    p["margin_pct"] = round((avg_sale - avg_cost) / avg_cost * 100, 2)
                else:
                    p["margin_pct"] = None

            for p in products:
                pack_info = connector.get_pack_info(p["id"])
                if pack_info:
                    p["kind"] = pack_info["kind"]
                    if pack_info.get("components"):
                        p["pack_components"] = pack_info["components"]
                    if pack_info.get("member_of"):
                        p["member_of_packs"] = pack_info["member_of"]

        return {"products": products, "count": len(products)}
    finally:
        connector.release_db(conn)


def exec_get_financial_summary(params):
    now = datetime.now()
    start_str = params.get("start_date")
    end_str = params.get("end_date")

    try:
        start_dt = datetime.strptime(start_str, "%Y-%m-%d") if start_str else now - timedelta(days=365)
        end_dt = datetime.strptime(end_str, "%Y-%m-%d") if end_str else now
    except ValueError:
        return {"error": "Invalid date format. Use YYYY-MM-DD."}
    if start_dt > end_dt:
        return {"error": "Start date must be before end date."}

    start_epoch = int(start_dt.timestamp())
    end_epoch = int(end_dt.timestamp())

    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)

        cursor.execute(connector._q("SELECT COALESCE(SUM(amount),0) as total FROM invoices WHERE date>=? AND date<=?"),
                       (start_epoch, end_epoch))
        income = cursor.fetchone()["total"]

        cursor.execute(connector._q("SELECT COALESCE(SUM(amount),0) as total FROM purchase_invoices WHERE date>=? AND date<=?"),
                       (start_epoch, end_epoch))
        expenses = cursor.fetchone()["total"]

        cursor.execute(connector._q("""
            SELECT contact_name, SUM(amount) as total FROM invoices
            WHERE date>=? AND date<=? AND contact_name IS NOT NULL AND contact_name != ''
            GROUP BY contact_name ORDER BY total DESC LIMIT 5
        """), (start_epoch, end_epoch))
        top_clients = [dict(r) for r in cursor.fetchall()]

        mf = _month_fmt()
        cursor.execute(connector._q(f"""
            SELECT {mf} as month, SUM(amount) as total
            FROM invoices WHERE date>=? AND date<=?
            GROUP BY month ORDER BY month
        """), (start_epoch, end_epoch))
        monthly_income = [dict(r) for r in cursor.fetchall()]

        cursor.execute(connector._q(f"""
            SELECT {mf} as month, SUM(amount) as total
            FROM purchase_invoices WHERE date>=? AND date<=?
            GROUP BY month ORDER BY month
        """), (start_epoch, end_epoch))
        monthly_expenses = [dict(r) for r in cursor.fetchall()]

        return {
            "period": {"start": start_str or start_dt.strftime("%Y-%m-%d"), "end": end_str or end_dt.strftime("%Y-%m-%d")},
            "income": round(income, 2),
            "expenses": round(expenses, 2),
            "balance": round(income - expenses, 2),
            "top_clients": top_clients,
            "monthly_income": monthly_income,
            "monthly_expenses": monthly_expenses
        }
    finally:
        connector.release_db(conn)


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
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)

        cursor.execute(connector._q(f"SELECT * FROM {table} WHERE id=?"), (doc_id,))
        doc = cursor.fetchone()
        if not doc:
            return {"error": f"Document {doc_id} not found"}

        cursor.execute(connector._q(f"SELECT * FROM {items_table} WHERE {fk}=?"), (doc_id,))
        items = [dict(r) for r in cursor.fetchall()]

        return {"document": dict(doc), "items": items}
    finally:
        connector.release_db(conn)


def exec_create_estimate(params):
    """Create estimate via WriteGateway."""
    result = gateway.execute("create_estimate", params, source="ai_agent", skip_confirm=True)
    if result.get("success"):
        return {
            "success": True,
            "id": result.get("entity_id", ""),
            "doc_number": result.get("doc_number", ""),
            "safe_mode": result.get("safe_mode", False),
            "message": "Estimate created (dry run)" if result.get("safe_mode") else "Estimate created successfully",
        }
    return {"success": False, "error": result.get("error") or result.get("errors", "Unknown error")}


def exec_create_invoice(params):
    """Create invoice via WriteGateway."""
    result = gateway.execute("create_invoice", params, source="ai_agent", skip_confirm=True)
    if result.get("success"):
        return {
            "success": True,
            "id": result.get("entity_id", ""),
            "doc_number": result.get("doc_number", ""),
            "safe_mode": result.get("safe_mode", False),
            "message": "Invoice created (dry run)" if result.get("safe_mode") else "Invoice created successfully",
        }
    return {"success": False, "error": result.get("error") or result.get("errors", "Unknown error")}


def exec_send_document(params):
    """Send document via WriteGateway."""
    result = gateway.execute("send_document", params, source="ai_agent", skip_confirm=True)
    if result.get("success"):
        return {
            "success": True,
            "safe_mode": result.get("safe_mode", False),
            "message": "Document sent (dry run)" if result.get("safe_mode") else "Document sent successfully",
        }
    return {"success": False, "error": result.get("error") or result.get("errors", "Unknown error")}


def exec_generate_report(params):
    title = params["title"]
    content = params["content"]
    filename = f"ai_report_{int(time.time())}.pdf"
    filepath = reports.generate_pdf_report(f"{title}\n\n{content}", filename)
    download_url = f"/api/reports/download/{os.path.basename(filepath)}"
    return {"success": True, "download_url": download_url, "filename": os.path.basename(filepath)}


def exec_create_contact(params):
    """Create contact via WriteGateway."""
    result = gateway.execute("create_contact", params, source="ai_agent", skip_confirm=True)
    if result.get("success"):
        return {
            "success": True,
            "id": result.get("entity_id", ""),
            "safe_mode": result.get("safe_mode", False),
            "message": "Contact created (dry run)" if result.get("safe_mode") else "Contact created successfully",
        }
    return {"success": False, "error": result.get("error") or result.get("errors", "Unknown error")}


def exec_get_overdue_invoices(params):
    inv_type = params.get("type", "both")
    min_amount = params.get("min_amount", 0)
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)

        results = []
        if inv_type in ("receivable", "both"):
            cursor.execute(
                connector._q("SELECT id, contact_name, amount, date, status FROM invoices WHERE status = 4 AND amount >= ? ORDER BY amount DESC"),
                (min_amount,))
            results.extend([{**dict(r), "source": "receivable"} for r in cursor.fetchall()])

        if inv_type in ("payable", "both"):
            cursor.execute(
                connector._q("SELECT id, contact_name, amount, date, status FROM purchase_invoices WHERE status = 4 AND amount >= ? ORDER BY amount DESC"),
                (min_amount,))
            results.extend([{**dict(r), "source": "payable"} for r in cursor.fetchall()])

        total = sum(r["amount"] or 0 for r in results)
        return {"overdue": results, "count": len(results), "total_overdue": round(total, 2)}
    finally:
        connector.release_db(conn)


def exec_get_upcoming_payments(params):
    days = params.get("days_ahead", 30)
    ptype = params.get("type", "both")
    now = int(time.time())
    future = now + days * 86400

    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)

        where_type = ""
        if ptype == "income":
            where_type = "AND type = 'income'"
        elif ptype == "expense":
            where_type = "AND type = 'expense'"

        cursor.execute(connector._q(f"""
            SELECT id, document_id, amount, date, method, type
            FROM payments
            WHERE date >= ? AND date <= ? {where_type}
            ORDER BY date ASC
        """), (now - 30 * 86400, future))
        payments = [dict(r) for r in cursor.fetchall()]

        total = sum(p["amount"] or 0 for p in payments)
        return {"payments": payments, "count": len(payments), "total": round(total, 2)}
    finally:
        connector.release_db(conn)


def exec_compare_periods(params):
    def _period_stats(cursor, start_str, end_str):
        start_epoch = int(datetime.strptime(start_str, "%Y-%m-%d").timestamp())
        end_epoch = int(datetime.strptime(end_str, "%Y-%m-%d").timestamp()) + 86399

        cursor.execute(connector._q("SELECT COALESCE(SUM(amount),0) as t FROM invoices WHERE date>=? AND date<=?"), (start_epoch, end_epoch))
        income = cursor.fetchone()["t"]
        cursor.execute(connector._q("SELECT COALESCE(SUM(amount),0) as t FROM purchase_invoices WHERE date>=? AND date<=?"), (start_epoch, end_epoch))
        expenses = cursor.fetchone()["t"]
        cursor.execute(connector._q("SELECT COUNT(*) as c FROM invoices WHERE date>=? AND date<=?"), (start_epoch, end_epoch))
        inv_count = cursor.fetchone()["c"]
        cursor.execute(connector._q("SELECT COUNT(*) as c FROM purchase_invoices WHERE date>=? AND date<=?"), (start_epoch, end_epoch))
        pur_count = cursor.fetchone()["c"]

        return {
            "period": f"{start_str} to {end_str}",
            "income": round(income, 2),
            "expenses": round(expenses, 2),
            "balance": round(income - expenses, 2),
            "invoice_count": inv_count,
            "purchase_count": pur_count
        }

    try:
        for key in ["period1_start", "period1_end", "period2_start", "period2_end"]:
            datetime.strptime(params[key], "%Y-%m-%d")
    except (ValueError, KeyError):
        return {"error": "Invalid date format. Use YYYY-MM-DD for all period dates."}

    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)

        p1 = _period_stats(cursor, params["period1_start"], params["period1_end"])
        p2 = _period_stats(cursor, params["period2_start"], params["period2_end"])
    finally:
        connector.release_db(conn)

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
    """Update document status via WriteGateway."""
    result = gateway.execute("update_document_status", params, source="ai_agent", skip_confirm=True)
    if result.get("success"):
        return {
            "success": True,
            "safe_mode": result.get("safe_mode", False),
            "message": "Status updated (dry run)" if result.get("safe_mode") else "Status updated successfully",
        }
    return {"success": False, "error": result.get("error") or result.get("errors", "Unknown error")}


def exec_convert_estimate_to_invoice(params):
    """Convert estimate to invoice via WriteGateway."""
    result = gateway.execute("convert_estimate_to_invoice", params, source="ai_agent", skip_confirm=True)
    if result.get("success"):
        return {
            "success": True,
            "safe_mode": result.get("safe_mode", False),
            "invoice_id": result.get("entity_id", ""),
            "doc_number": result.get("doc_number", ""),
            "estimate_id": result.get("estimate_id", ""),
            "message": "Converted (dry run)" if result.get("safe_mode") else
                       f"Invoice {result.get('doc_number', '')} created as borrador. Estimate marked as invoiced.",
        }
    return {"success": False, "error": result.get("errors") or "Unknown error"}


def exec_render_chart(params):
    return {
        "chart_type": params["chart_type"],
        "title": params["title"],
        "labels": params["labels"],
        "datasets": params["datasets"]
    }

def exec_get_amortization_status(params):
    """Return amortization data for all tracked rental products, with optional name filter."""
    rows = connector.get_amortizations()
    name_filter = (params.get("product_name") or "").strip().lower()
    if name_filter:
        rows = [r for r in rows if name_filter in r["product_name"].lower()]

    summary = connector.get_amortization_summary()

    if not rows:
        return {"message": "No amortization records found.", "summary": summary}

    return {
        "products": rows,
        "summary": summary,
        "count": len(rows)
    }


def exec_analyze_file(params):
    """Analyze an uploaded CSV or Excel file."""
    import pandas as pd

    filename = params.get("filename", "")
    analysis_type = params.get("analysis_type", "summary")

    # Path traversal prevention
    safe_name = os.path.basename(filename)
    uploads_dir = os.path.abspath(connector.get_uploads_dir())
    filepath = os.path.join(uploads_dir, safe_name)

    # Validate file is within uploads dir (append sep to prevent prefix attacks)
    if not os.path.abspath(filepath).startswith(uploads_dir + os.sep):
        return {"error": "Invalid filename"}

    if not os.path.exists(filepath):
        return {"error": f"File not found: {safe_name}"}

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
                except Exception:
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
        logger.error(f"File parsing error: {e}")
        return {"error": "Failed to parse file"}

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
        logger.error(f"Error listing files: {e}")
        return {"error": "Failed to list files"}

def exec_upload_file(params):
    """Register uploaded file (write operation - requires confirmation)."""
    filename = params.get("filename", "")
    description = params.get("description", "")

    # Path traversal prevention
    safe_name = os.path.basename(filename)
    uploads_dir = os.path.abspath(connector.get_uploads_dir())
    filepath = os.path.join(uploads_dir, safe_name)

    if not os.path.abspath(filepath).startswith(uploads_dir + os.sep):
        return {"error": "Invalid filename"}

    if not os.path.exists(filepath):
        return {"error": f"File not found in uploads: {safe_name}"}

    conn = connector.get_db()
    try:
        # Record in ai_history for audit trail
        cursor = connector._cursor(conn)
        cursor.execute(connector._q("""
            INSERT INTO ai_history (role, content, conversation_id, tool_calls)
            VALUES (?, ?, ?, ?)
        """), ("system", f"File uploaded: {filename}", "default", json.dumps({
            "tool": "upload_file",
            "description": description,
            "filename": filename,
            "size": os.path.getsize(filepath)
        })))
        conn.commit()

        return {
            "success": True,
            "filename": filename,
            "size": os.path.getsize(filepath),
            "message": f"File '{filename}' registered for processing"
        }
    except Exception as e:
        logger.error(f"Failed to register file: {e}")
        return {"error": "Failed to register file"}
    finally:
        connector.release_db(conn)


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
    "get_amortization_status": exec_get_amortization_status,
    "convert_estimate_to_invoice": exec_convert_estimate_to_invoice,
}
