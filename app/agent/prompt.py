"""System prompt builder for the AI agent."""
import os
import time
import logging
import connector
from app.agent.tools import get_tools_for_role

logger = logging.getLogger(__name__)


def load_skills():
    """Load skills from the skills/ directory."""
    try:
        # Assuming skills dir is in the same directory as the project root
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        skills_dir = os.path.join(base_dir, "skills")

        if not os.path.exists(skills_dir):
            return ""

        skills_text = []
        for item in sorted(os.listdir(skills_dir)):
            skill_path = os.path.join(skills_dir, item)
            if os.path.isdir(skill_path):
                skill_file = os.path.join(skill_path, "SKILL.md")
                if os.path.exists(skill_file):
                    try:
                        with open(skill_file, "r", encoding="utf-8") as f:
                            content = f.read()
                            # Simply append the content of the skill file
                            skills_text.append(f"--- SKILL: {item} ---\n{content}\n")
                    except Exception as e:
                        logger.error(f"Error loading skill {item}: {e}")

        if not skills_text:
            return ""

        return "\n\nENABLED SKILLS (follow these specific instructions):\n" + "\n".join(skills_text)
    except Exception as e:
        logger.error(f"Error in load_skills: {e}")
        return ""

_system_prompt_cache = {"prompt": None, "built_at": 0}
_SYSTEM_PROMPT_TTL = 300  # 5 minutes

def build_system_prompt():
    now = time.time()
    if _system_prompt_cache["prompt"] and now - _system_prompt_cache["built_at"] < _SYSTEM_PROMPT_TTL:
        return _system_prompt_cache["prompt"]

    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)

        stats = {}
        for table in ["invoices", "purchase_invoices", "estimates", "products", "contacts"]:
            cursor.execute(f"SELECT COUNT(*) as c FROM {table}")
            stats[table] = cursor.fetchone()["c"]

        cursor.execute("SELECT COALESCE(SUM(amount),0) as t FROM invoices")
        total_income = cursor.fetchone()["t"]
        cursor.execute("SELECT COALESCE(SUM(amount),0) as t FROM purchase_invoices")
        total_expenses = cursor.fetchone()["t"]
    finally:
        connector.release_db(conn)

    safe_status = "ON (dry run - writes are simulated)" if connector.SAFE_MODE else "OFF (writes execute against Holded)"

    skills_content = load_skills()

    prompt = f"""You are the financial assistant for this company's Holded accounting system.
You help analyze financial data, check prices, create estimates/invoices, generate reports, and send documents.

DATABASE (PostgreSQL via query_database tool — use standard SQL with %s placeholders or hardcoded values):
- invoices (id, contact_id, contact_name, "desc", date[epoch], amount[EUR], status 0-5)
- invoice_items (invoice_id, product_id, name, sku, units, price, subtotal, discount, tax, retention, account)
- purchase_invoices (same as invoices), purchase_items (purchase_id, ...)
- estimates (same as invoices), estimate_items (estimate_id, ...)
- contacts (id, name, email, type, code, vat, phone, mobile)
- products (id, name, "desc", price, stock, sku)
- payments (id, document_id, amount, date, method, type)
- projects (id, name, "desc", status, customer_id, budget)
- ledger_accounts (id, name, num)

Status codes - invoices/purchases: 0=draft, 1=issued, 2=partial, 3=paid, 4=overdue, 5=cancelled. Estimates: 0=draft, 1=pending, 2=accepted, 3=rejected, 4=invoiced.
Dates are Unix epoch. In SQL: to_timestamp(date) for conversion, to_char(to_timestamp(date), 'YYYY-MM') for month grouping. Note: "desc" is a reserved word — always quote it.

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
- Use compare_periods for period-over-period analysis (e.g., this month vs last month).
- For convert_estimate_to_invoice: if the user refers to an estimate by contact name, date, or "the last one" instead of by ID, first use query_database to find the estimate ID (e.g., SELECT id, doc_number, contact_name, amount FROM estimates WHERE contact_name ILIKE '%name%' ORDER BY date DESC LIMIT 5), then use convert_estimate_to_invoice with the resolved ID.

{skills_content}"""

    _system_prompt_cache["prompt"] = prompt
    _system_prompt_cache["built_at"] = time.time()
    return prompt
