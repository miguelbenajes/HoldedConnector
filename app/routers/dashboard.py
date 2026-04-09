"""
Dashboard router — summary stats, date range, unpaid invoices, and top contacts.

Covers: summary counts/totals, date-range picker bounds, unpaid invoice aging,
monthly trends, date-range stats, and top contacts by revenue.
6 endpoints extracted from api.py (Fase 4 router split).
"""
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from typing import Optional
import connector
import logging
from app.db.connection import db_context
from app.routers._shared import assert_valid_table

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/summary")
def get_summary():
    with db_context() as conn:
        cursor = conn.cursor()

        counts = {}
        for table in ["invoices", "purchase_invoices", "estimates", "products", "contacts"]:
            assert_valid_table(table)
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

@router.get("/api/stats/date-range")
def get_date_range():
    """
    Returns the earliest and latest date found across all main transactional tables.
    Used by the date picker to know the absolute min/max for 'Desde siempre'.
    """
    with db_context() as conn:
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


@router.get("/api/invoices/unpaid")
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
    with db_context() as conn:
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


@router.get("/api/stats/monthly")
def get_monthly_stats(start: Optional[int] = None, end: Optional[int] = None):
    if connector._USE_SQLITE:
        month_expr = "strftime('%Y-%m', datetime(date, 'unixepoch'))"
    else:
        month_expr = "TO_CHAR(TO_TIMESTAMP(date), 'YYYY-MM')"

    with db_context() as conn:
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

@router.get("/api/stats/range")
def get_range_stats(start: int, end: int):
    with db_context() as conn:
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

@router.get("/api/stats/top-contacts")
def get_top_contacts():
    with db_context() as conn:
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
