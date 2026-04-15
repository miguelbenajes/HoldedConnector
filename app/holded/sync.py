"""Bulk sync engine: pull all entities from Holded API and upsert to local DB.

Each sync_* function fetches all records from Holded and writes them to the
local database (SQLite or PostgreSQL). sync_documents is the core function
handling invoices, purchases, and estimates — including status derivation,
line items, project codes, and job creation.

Extracted from connector.py during Fase 5a refactor.
"""
import json
import time
import logging
import requests
from app.db.connection import get_db, release_db, _cursor, _q, _num, _row_val, _fetch_one_val, _USE_SQLITE
from app.holded.client import (
    fetch_data,
    extract_ret,
    _extract_project_code,
    _extract_shooting_dates,
    PROYECTO_PRODUCT_ID,
    PROYECTO_PRODUCT_NAME,
    SHOOTING_DATES_PRODUCT_ID,
    SHOOTING_DATES_PRODUCT_NAME,
)
from app.holded.upsert import _upsert_single_document, _upsert_single_contact, _upsert_single_product

logger = logging.getLogger(__name__)


def sync_documents(doc_type, table, items_table, fk_column):
    logger.info(f"Syncing {doc_type}s (Historical)...")
    params = {"starttmp": 1262304000, "endtmp": int(time.time())}
    data = fetch_data(f"/invoicing/v1/documents/{doc_type}", params=params)

    _changed_project_codes = set()
    conn = get_db()
    try:
        cursor = _cursor(conn)
        cursor.execute("SELECT id, name, num FROM ledger_accounts")
        rows = cursor.fetchall()
        if _USE_SQLITE:
            acc_map = {row[0]: f"{row[1]} ({row[2]})" if row[2] else row[1] for row in rows}
        else:
            acc_map = {r["id"]: f"{r['name']} ({r['num']})" if r["num"] else r["name"] for r in rows}

        for item in data:
            doc_id = item.get('id')
            tags   = json.dumps(item.get('tags') or [])
            notes  = item.get('notes') or ''

            # --- Holded API status derivation (invoices only) --------------------
            # Holded API status field is unreliable for invoices:
            #   API 0 = draft (but also returned for approved invoices — buggy)
            #   API 1 = approved (but doesn't distinguish paid/unpaid/overdue)
            #   API 3 = cancelled (Anulado)
            # We derive the real status from multiple fields:
            #   0 = draft, 1 = pending, 3 = paid, 4 = overdue, 5 = cancelled
            # Estimates/purchases use different status codes — pass through as-is.
            api_status = item.get('status')
            if table == 'invoices':
                pending = float(_num(item.get('paymentsPending', 0)) or 0)
                due_ts  = item.get('dueDate')

                if api_status == 3:
                    raw_status = 5  # cancelled (Anulado)
                elif api_status == 0 and not item.get('approvedAt'):
                    raw_status = 0  # truly draft
                else:
                    # Approved invoice — derive payment status
                    if abs(pending) < 0.01:
                        raw_status = 3  # paid (Pagado)
                    elif due_ts and isinstance(due_ts, (int, float)) and due_ts < time.time():
                        raw_status = 4  # overdue (Vencido)
                    else:
                        raw_status = 1  # pending (Pendiente)
            else:
                raw_status = api_status
            # -------------------------------------------------------------------

            if table == 'invoices':
                vals = (doc_id, item.get('contact'), item.get('contactName'), item.get('desc'),
                        item.get('date'), _num(item.get('total')), raw_status,
                        _num(item.get('paymentsPending', 0)), _num(item.get('paymentsTotal', 0)),
                        item.get('dueDate'), item.get('docNumber'), tags, notes)
                if _USE_SQLITE:
                    cursor.execute('''
                        INSERT OR REPLACE INTO invoices
                            (id, contact_id, contact_name, "desc", date, amount, status,
                             payments_pending, payments_total, due_date, doc_number, tags, notes)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ''', vals)
                else:
                    cursor.execute('''
                        INSERT INTO invoices
                            (id, contact_id, contact_name, "desc", date, amount, status,
                             payments_pending, payments_total, due_date, doc_number, tags, notes)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (id) DO UPDATE SET
                            contact_id=EXCLUDED.contact_id, contact_name=EXCLUDED.contact_name,
                            "desc"=EXCLUDED."desc", date=EXCLUDED.date, amount=EXCLUDED.amount,
                            status=EXCLUDED.status, payments_pending=EXCLUDED.payments_pending,
                            payments_total=EXCLUDED.payments_total, due_date=EXCLUDED.due_date,
                            doc_number=EXCLUDED.doc_number, tags=EXCLUDED.tags, notes=EXCLUDED.notes
                    ''', vals)
            else:
                vals = (doc_id, item.get('contact'), item.get('contactName'), item.get('desc'),
                        item.get('date'), _num(item.get('total')), raw_status,
                        item.get('docNumber'), tags, notes)
                if _USE_SQLITE:
                    cursor.execute(f'''
                        INSERT OR REPLACE INTO {table}
                            (id, contact_id, contact_name, "desc", date, amount, status, doc_number, tags, notes)
                        VALUES (?,?,?,?,?,?,?,?,?,?)
                    ''', vals)
                else:
                    cursor.execute(f'''
                        INSERT INTO {table}
                            (id, contact_id, contact_name, "desc", date, amount, status, doc_number, tags, notes)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (id) DO UPDATE SET
                            contact_id=EXCLUDED.contact_id, contact_name=EXCLUDED.contact_name,
                            "desc"=EXCLUDED."desc", date=EXCLUDED.date, amount=EXCLUDED.amount,
                            status=EXCLUDED.status, doc_number=EXCLUDED.doc_number,
                            tags=EXCLUDED.tags, notes=EXCLUDED.notes
                    ''', vals)

            # Items: delete + re-insert (simpler than upsert for SERIAL-keyed rows)
            cursor.execute(_q(f'DELETE FROM {items_table} WHERE {fk_column} = ?'), (doc_id,))
            for prod in item.get('products', []):
                retention = extract_ret(prod)
                acc_id = prod.get('accountCode') or prod.get('accountName') or prod.get('account')
                account = acc_map.get(acc_id, acc_id)
                cursor.execute(_q(f'''
                    INSERT INTO {items_table}
                        ({fk_column}, product_id, name, sku, units, price, subtotal,
                         discount, tax, retention, account, project_id, kind, "desc")
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                '''), (doc_id, prod.get('productId'), prod.get('name'), prod.get('sku'),
                       _num(prod.get('units')), _num(prod.get('price')), _num(prod.get('subtotal')),
                       _num(prod.get('discount')), _num(prod.get('tax')), _num(retention), account,
                       prod.get('projectid'), prod.get('kind'), prod.get('desc')))

            # Extract project code + shooting dates from line items
            project_code = _extract_project_code(item.get('products'))
            shooting_raw = _extract_shooting_dates(item.get('products'))
            cursor.execute(_q(f'UPDATE {table} SET project_code = ?, shooting_dates_raw = ? WHERE id = ?'),
                           (project_code, shooting_raw, doc_id))

            # If this doc has a project code, ensure job exists
            if project_code:
                from skills.job_tracker import ensure_job
                doc_data = {
                    "client_id": item.get('contact'),
                    "client_name": item.get('contactName'),
                    "shooting_dates_raw": shooting_raw,
                    "estimate_id": doc_id if table == 'estimates' else None,
                    "estimate_number": item.get('docNumber') if table == 'estimates' else None,
                    "invoice_id": doc_id if table == 'invoices' else None,
                    "invoice_number": item.get('docNumber') if table == 'invoices' else None,
                    "doc_date": item.get('date'),
                }
                ensure_job(project_code, doc_data, cursor)
                _changed_project_codes.add(project_code)

        conn.commit()
    finally:
        release_db(conn)

    # Notify Brain for each changed job — non-blocking, after DB transaction is closed
    if _changed_project_codes:
        from skills.job_tracker import BRAIN_API_URL, BRAIN_INTERNAL_KEY
        for _pc in _changed_project_codes:
            try:
                requests.post(
                    f"{BRAIN_API_URL}/internal/job-review",
                    json={"project_code": _pc, "trigger": "holded_sync"},
                    headers={"x-api-key": BRAIN_INTERNAL_KEY},
                    timeout=5,
                )
            except Exception:
                pass  # Non-critical — cron will pick it up

    logger.info(f"Synced {len(data)} {doc_type}s and their line items.")


def sync_invoices():
    sync_documents("invoice", "invoices", "invoice_items", "invoice_id")

def sync_purchases():
    sync_documents("purchase", "purchase_invoices", "purchase_items", "purchase_id")

def sync_estimates():
    sync_documents("estimate", "estimates", "estimate_items", "estimate_id")


def sync_accounts():
    logger.info("Syncing Ledger Accounts (Chart of Accounts)...")
    data = fetch_data("/accounting/v1/chartofaccounts")
    conn = get_db()
    try:
        cursor = _cursor(conn)
        accounts = data if isinstance(data, list) else data.get('accounts', [])
        for item in accounts:
            vals = (item.get('id'), item.get('name'), item.get('num'))
            if _USE_SQLITE:
                cursor.execute("INSERT OR REPLACE INTO ledger_accounts (id, name, num) VALUES (?,?,?)", vals)
            else:
                cursor.execute("""
                    INSERT INTO ledger_accounts (id, name, num) VALUES (%s,%s,%s)
                    ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, num=EXCLUDED.num
                """, vals)
        conn.commit()
    finally:
        release_db(conn)
    logger.info(f"Synced {len(accounts)} ledger accounts.")


def sync_products():
    logger.info("Syncing Products (Inventory)...")
    data = fetch_data("/invoicing/v1/products")
    conn = get_db()
    try:
        cursor = _cursor(conn)
        pack_rows = []  # collect (pack_id, component_id, qty) for batch insert
        for item in data:
            kind = item.get('kind', 'simple') or 'simple'
            vals = (item.get('id'), item.get('name'), item.get('desc'),
                    _num(item.get('price')), _num(item.get('stock')), item.get('sku'), kind)
            if _USE_SQLITE:
                cursor.execute('INSERT OR REPLACE INTO products (id, name, "desc", price, stock, sku, kind) VALUES (?,?,?,?,?,?,?)', vals)
            else:
                cursor.execute("""
                    INSERT INTO products (id, name, "desc", price, stock, sku, kind) VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO UPDATE SET
                        name=EXCLUDED.name, "desc"=EXCLUDED."desc", price=EXCLUDED.price,
                        stock=EXCLUDED.stock, sku=EXCLUDED.sku, kind=EXCLUDED.kind
                """, vals)

            # Collect pack composition for batch insert
            if kind == 'pack':
                pack_id = item.get('id')
                for pi in (item.get('packItems') or []):
                    raw_pid = pi.get('pid', '')
                    component_id = raw_pid.split('#')[0] if '#' in raw_pid else raw_pid
                    if component_id:
                        pack_rows.append((pack_id, component_id, _num(pi.get('units', 1)) or 1))

        # Refresh pack_components: DELETE all + re-INSERT (small table, composition may change)
        cursor.execute("DELETE FROM pack_components")
        for row in pack_rows:
            if _USE_SQLITE:
                cursor.execute("INSERT INTO pack_components (pack_id, component_id, quantity) VALUES (?,?,?)", row)
            else:
                cursor.execute("INSERT INTO pack_components (pack_id, component_id, quantity) VALUES (%s,%s,%s)", row)
        conn.commit()
    finally:
        release_db(conn)
    logger.info(f"Synced {len(data)} products ({len(pack_rows)} pack components).")


def sync_contacts():
    logger.info("Syncing Contacts...")
    data = fetch_data("/invoicing/v1/contacts")
    conn = get_db()
    try:
        cursor = _cursor(conn)
        for item in data:
            addr = item.get('billAddress') or {}
            vals = (item.get('id'), item.get('name'), item.get('email'), item.get('type'),
                    item.get('code'), item.get('vat'), item.get('phone'), item.get('mobile'),
                    addr.get('countryCode', '') or addr.get('country', ''),
                    addr.get('address', ''), addr.get('city', ''),
                    addr.get('province', ''), addr.get('postalCode', ''),
                    item.get('tradeName', ''), item.get('discount', 0))
            if _USE_SQLITE:
                cursor.execute("""
                    INSERT OR REPLACE INTO contacts (id, name, email, type, code, vat, phone, mobile,
                        country, address, city, province, postal_code, trade_name, discount)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, vals)
            else:
                cursor.execute("""
                    INSERT INTO contacts (id, name, email, type, code, vat, phone, mobile,
                        country, address, city, province, postal_code, trade_name, discount)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO UPDATE SET
                        name=EXCLUDED.name, email=EXCLUDED.email, type=EXCLUDED.type,
                        code=EXCLUDED.code, vat=EXCLUDED.vat, phone=EXCLUDED.phone,
                        mobile=EXCLUDED.mobile, country=EXCLUDED.country, address=EXCLUDED.address,
                        city=EXCLUDED.city, province=EXCLUDED.province, postal_code=EXCLUDED.postal_code,
                        trade_name=EXCLUDED.trade_name, discount=EXCLUDED.discount
                """, vals)
        conn.commit()
    finally:
        release_db(conn)
    logger.info(f"Synced {len(data)} contacts.")


def sync_projects():
    logger.info("Syncing Projects...")
    data = fetch_data("/projects/v1/projects")
    conn = get_db()
    try:
        cursor = _cursor(conn)
        for item in data:
            vals = (item.get('id'), item.get('name'), item.get('desc'),
                    item.get('status'), item.get('customer'), _num(item.get('budget')))
            if _USE_SQLITE:
                cursor.execute("""
                    INSERT OR REPLACE INTO projects (id, name, "desc", status, customer_id, budget)
                    VALUES (?,?,?,?,?,?)
                """, vals)
            else:
                cursor.execute("""
                    INSERT INTO projects (id, name, "desc", status, customer_id, budget)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO UPDATE SET
                        name=EXCLUDED.name, "desc"=EXCLUDED."desc", status=EXCLUDED.status,
                        customer_id=EXCLUDED.customer_id, budget=EXCLUDED.budget
                """, vals)
        conn.commit()
    finally:
        release_db(conn)
    logger.info(f"Synced {len(data)} projects.")


def sync_payments():
    logger.info("Syncing Payments...")
    data = fetch_data("/invoicing/v1/payments")
    conn = get_db()
    try:
        cursor = _cursor(conn)
        for item in data:
            vals = (item.get('id'), item.get('documentId'), _num(item.get('amount')),
                    item.get('date'), item.get('paymentMethod'), item.get('type'))
            if _USE_SQLITE:
                cursor.execute("""
                    INSERT OR REPLACE INTO payments (id, document_id, amount, date, method, type)
                    VALUES (?,?,?,?,?,?)
                """, vals)
            else:
                cursor.execute("""
                    INSERT INTO payments (id, document_id, amount, date, method, type)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO UPDATE SET
                        document_id=EXCLUDED.document_id, amount=EXCLUDED.amount,
                        date=EXCLUDED.date, method=EXCLUDED.method, type=EXCLUDED.type
                """, vals)
        conn.commit()
    finally:
        release_db(conn)
    logger.info(f"Synced {len(data)} payments.")
