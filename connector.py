"""Holded Connector — thin facade + business logic.

DB primitives (get_db, _cursor, _q, etc.) live in app.db.connection.
Schema (init_db) lives in app.db.schema.
Settings (reload_config, get/save_setting) live in app.db.settings.

This module re-exports everything for backwards compatibility:
    import connector
    connector.get_db()      # works
    connector.API_KEY       # works (via __getattr__ for mutables)
    connector.init_db()     # works
"""

import os
import json
import logging
import requests
import time

# ── DB layer imports (canonical location: app.db.*) ──────────────────────────
import app.db.connection as _conn
import app.db.schema as _schema
import app.db.settings as _settings

logger = logging.getLogger(__name__)

# ── Function re-exports (safe — functions look up globals at call time) ──────
get_db = _conn.get_db
release_db = _conn.release_db
_cursor = _conn._cursor
_q = _conn._q
_num = _conn._num
_row_val = _conn._row_val
_fetch_one_val = _conn._fetch_one_val
insert_audit_log = _conn.insert_audit_log
update_audit_log = _conn.update_audit_log
db_context = _conn.db_context

init_db = _schema.init_db

reload_config = _settings.reload_config
get_setting = _settings.get_setting
save_setting = _settings.save_setting

# ── Shared-reference globals (used by business functions in this file) ───────
# HEADERS is a dict — shared reference, mutations from reload_config propagate.
# BASE_URL, DB_NAME, _USE_SQLITE, SAFE_MODE are immutable after init — snapshots are safe.
# API_KEY can change via reload_config → accessed through __getattr__ by external callers.
HEADERS = _conn.HEADERS
BASE_URL = _conn.BASE_URL
DB_NAME = _conn.DB_NAME
_USE_SQLITE = _conn._USE_SQLITE
SAFE_MODE = _conn.SAFE_MODE

# ── Module-level __getattr__ for mutable globals (external callers) ──────────
# When other modules do `connector.API_KEY`, this resolves to the canonical
# value in connection.py, even after reload_config() changes it.
# _USE_SQLITE and SAFE_MODE are module-level snapshots (never change), so they
# don't need __getattr__. API_KEY and DATABASE_URL can change via reload_config.
_MUTABLE_CONN_ATTRS = ('API_KEY', 'DATABASE_URL', '_pool')

def __getattr__(name):
    """Proxy mutable globals to their canonical module."""
    if name in _MUTABLE_CONN_ATTRS:
        return getattr(_conn, name)
    raise AttributeError(f"module 'connector' has no attribute {name!r}")


# ── Holded-specific constants (used by sync functions below) ─────────────────
PROYECTO_PRODUCT_ID = os.getenv("HOLDED_PROYECTO_PRODUCT_ID", "69b2b35f75ae381d8f05c133")
PROYECTO_PRODUCT_NAME = "proyect ref:"
SHOOTING_DATES_PRODUCT_ID = os.getenv("HOLDED_SHOOTING_DATES_PRODUCT_ID", "69b2cfcd0df77ff4010e4ac8")
SHOOTING_DATES_PRODUCT_NAME = "shooting dates:"


# ═══════════════════════════════════════════════════════════════════════════════
# Business logic (Holded API sync, data access, amortizations, analysis)
# ═══════════════════════════════════════════════════════════════════════════════

def extract_ret(prod):
    r = prod.get('retention')
    if r is not None and r != 0:
        return r
    taxes = prod.get('taxes')
    if isinstance(taxes, str): taxes = taxes.split(',')
    if isinstance(taxes, list):
        for t in taxes:
            st = str(t)
            if '_ret_' in st:
                try: return float(st.split('_ret_')[-1])
                except (ValueError, IndexError): pass
    return 0

def fetch_data(endpoint, params=None):
    if params is None: params = {}
    all_data = []
    page = 1
    max_retries = 5
    retry_delay = 5

    if 'limit' not in params:
        params['limit'] = 500

    while True:
        params['page'] = page
        url = f"{BASE_URL}{endpoint}"
        logger.info(f"Fetching {endpoint} (Page {page}, Limit {params['limit']})...")

        current_data = None
        for attempt in range(max_retries):
            try:
                response = requests.get(url, headers=HEADERS, params=params, timeout=40)

                if response.status_code == 200:
                    current_data = response.json()
                    break
                elif response.status_code == 429:
                    logger.warning(f"Rate limit hit (429) for {endpoint}. Waiting {retry_delay}s... (Attempt {attempt+1})")
                    time.sleep(retry_delay * (attempt + 1))
                    continue
                else:
                    logger.error(f"Error fetching {endpoint} (Page {page}): {response.status_code} - {response.text}")
                    if response.status_code >= 500:
                        time.sleep(2 * (attempt + 1))
                        continue
                    else:
                        break
            except Exception as e:
                logger.error(f"Exception fetching {endpoint} (Page {page}, Attempt {attempt+1}): {e}")
                time.sleep(2 * (attempt + 1))

        if current_data is None:
            logger.warning(f"Failed to fetch {endpoint} page {page} after retries. Returning partial data.")
            break

        if isinstance(current_data, list):
            all_data.extend(current_data)
            logger.info(f"Received {len(current_data)} items. Total: {len(all_data)}")
            if len(current_data) < params['limit']:
                break
            page += 1
        else:
            return current_data

    return all_data

def _extract_project_code(products):
    """Scan line items for the 'Proyect REF:' product and return its desc as project code.
    Returns the project code string, or None if not found.
    Detection: by productId (reliable) or by name (fallback, case-insensitive)."""
    for prod in (products or []):
        pid = prod.get('productId')
        name = (prod.get('name') or '').strip().lower()
        if pid == PROYECTO_PRODUCT_ID or name == PROYECTO_PRODUCT_NAME:
            return (prod.get('desc') or '').strip() or None
    return None


def _extract_shooting_dates(products):
    """Scan line items for 'Shooting Dates:' product and return its desc.
    Detection: by productId (reliable) or by name (fallback, case-insensitive)."""
    for prod in (products or []):
        pid = prod.get('productId')
        name = (prod.get('name') or '').strip().lower()
        if pid == SHOOTING_DATES_PRODUCT_ID or name == SHOOTING_DATES_PRODUCT_NAME:
            return (prod.get('desc') or '').strip() or None
    return None


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
                    addr.get('country', ''), addr.get('address', ''), addr.get('city', ''),
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


# ---------------------------------------------------------------------------
# Single-entity sync helpers — used by Write Gateway for sync-back after writes
# ---------------------------------------------------------------------------

def _upsert_single_document(cursor, doc, table, items_table, fk_column):
    """Upsert a single document + its line items into local DB.

    Reuses the same field mapping and SQL patterns as sync_documents().
    `doc` is a single Holded document dict (from GET /documents/{type}/{id}).

    IMPORTANT: Invoices use 13 columns (includes payments_pending, payments_total,
    due_date). Estimates and purchases use only 10 columns (no payment fields).
    This mirrors the branching in sync_documents() at line 708.
    """
    doc_id = doc.get('id')
    if not doc_id:
        return

    # Build ledger account map for human-readable account names (like sync_documents lines 697-702)
    acc_map = {}
    try:
        cursor.execute(_q('SELECT id, name, num FROM ledger_accounts'))
        for r in cursor.fetchall():
            name = r[1] if isinstance(r, tuple) else r['name']
            num = r[2] if isinstance(r, tuple) else r.get('num')
            rid = r[0] if isinstance(r, tuple) else r['id']
            acc_map[rid] = f"{name} ({num})" if num else name
    except Exception:
        pass  # No ledger accounts loaded yet — fall through to raw ID

    is_invoice = (table == 'invoices')

    if is_invoice:
        # Invoices: 13 columns including payment fields
        vals = (
            doc_id,
            doc.get('contact'),
            doc.get('contactName'),
            doc.get('desc', ''),
            doc.get('date'),
            _num(doc.get('total')),
            doc.get('status', 0),
            _num(doc.get('paymentsPending')),
            _num(doc.get('paymentsTotal')),
            doc.get('dueDate'),
            doc.get('docNumber', ''),
            json.dumps(doc.get('tags') or []),
            doc.get('notes', '')
        )

        if _USE_SQLITE:
            cursor.execute(f'''
                INSERT OR REPLACE INTO {table}
                    (id, contact_id, contact_name, "desc", date, amount, status,
                     payments_pending, payments_total, due_date, doc_number, tags, notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', vals)
        else:
            cursor.execute(f'''
                INSERT INTO {table}
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
        # Estimates & purchases: 10 columns (no payment fields)
        vals = (
            doc_id,
            doc.get('contact'),
            doc.get('contactName'),
            doc.get('desc', ''),
            doc.get('date'),
            _num(doc.get('total')),
            doc.get('status', 0),
            doc.get('docNumber', ''),
            json.dumps(doc.get('tags') or []),
            doc.get('notes', '')
        )

        if _USE_SQLITE:
            cursor.execute(f'''
                INSERT OR REPLACE INTO {table}
                    (id, contact_id, contact_name, "desc", date, amount, status,
                     doc_number, tags, notes)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            ''', vals)
        else:
            cursor.execute(f'''
                INSERT INTO {table}
                    (id, contact_id, contact_name, "desc", date, amount, status,
                     doc_number, tags, notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    contact_id=EXCLUDED.contact_id, contact_name=EXCLUDED.contact_name,
                    "desc"=EXCLUDED."desc", date=EXCLUDED.date, amount=EXCLUDED.amount,
                    status=EXCLUDED.status,
                    doc_number=EXCLUDED.doc_number, tags=EXCLUDED.tags, notes=EXCLUDED.notes
            ''', vals)

    # Items: delete + re-insert
    cursor.execute(_q(f'DELETE FROM {items_table} WHERE {fk_column} = ?'), (doc_id,))
    for prod in doc.get('products', []):
        retention = extract_ret(prod)
        acc_id = prod.get('accountCode') or prod.get('accountName') or prod.get('account')
        # Resolve ledger account ID to human-readable name (like sync_documents line 760)
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
    project_code = _extract_project_code(doc.get('products'))
    shooting_raw = _extract_shooting_dates(doc.get('products'))
    cursor.execute(_q(f'UPDATE {table} SET project_code = ?, shooting_dates_raw = ? WHERE id = ?'),
                   (project_code, shooting_raw, doc_id))

    # If this doc has a project code, ensure job exists
    if project_code:
        from skills.job_tracker import ensure_job
        doc_data = {
            "client_id": doc.get('contact'),
            "client_name": doc.get('contactName'),
            "shooting_dates_raw": shooting_raw,
            "estimate_id": doc_id if table == 'estimates' else None,
            "estimate_number": doc.get('docNumber') if table == 'estimates' else None,
            "invoice_id": doc_id if table == 'invoices' else None,
            "invoice_number": doc.get('docNumber') if table == 'invoices' else None,
            "doc_date": doc.get('date'),
        }
        ensure_job(project_code, doc_data, cursor)


def _upsert_single_contact(cursor, contact):
    """Upsert a single contact into local DB."""
    cid = contact.get('id')
    if not cid:
        return

    vals = (
        cid,
        contact.get('name', ''),
        contact.get('email', ''),
        contact.get('type', ''),
        contact.get('code', ''),
        contact.get('vat', ''),
        contact.get('phone', ''),
        contact.get('mobile', '')
    )

    if _USE_SQLITE:
        cursor.execute('''
            INSERT OR REPLACE INTO contacts
                (id, name, email, type, code, vat, phone, mobile)
            VALUES (?,?,?,?,?,?,?,?)
        ''', vals)
    else:
        cursor.execute('''
            INSERT INTO contacts
                (id, name, email, type, code, vat, phone, mobile)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO UPDATE SET
                name=EXCLUDED.name, email=EXCLUDED.email, type=EXCLUDED.type,
                code=EXCLUDED.code, vat=EXCLUDED.vat, phone=EXCLUDED.phone,
                mobile=EXCLUDED.mobile
        ''', vals)


def _upsert_single_product(cursor, product):
    """Upsert a single product into local DB."""
    pid = product.get('id')
    if not pid:
        return

    vals = (
        pid,
        product.get('name', ''),
        product.get('desc', ''),
        _num(product.get('price')),
        _num(product.get('stock')),
        product.get('sku', ''),
        product.get('kind', 'simple')
    )

    if _USE_SQLITE:
        cursor.execute('''
            INSERT OR REPLACE INTO products
                (id, name, "desc", price, stock, sku, kind)
            VALUES (?,?,?,?,?,?,?)
        ''', vals)
    else:
        cursor.execute('''
            INSERT INTO products
                (id, name, "desc", price, stock, sku, kind)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO UPDATE SET
                name=EXCLUDED.name, "desc"=EXCLUDED."desc", price=EXCLUDED.price,
                stock=EXCLUDED.stock, sku=EXCLUDED.sku, kind=EXCLUDED.kind
        ''', vals)


def sync_single_document(doc_type, doc_id):
    """Fetch one document from Holded and upsert into local DB. Returns tables updated."""
    table_map = {
        "invoice":  ("invoices", "invoice_items", "invoice_id"),
        "estimate": ("estimates", "estimate_items", "estimate_id"),
        "purchase": ("purchase_invoices", "purchase_items", "purchase_id"),
    }
    if doc_type not in table_map:
        logger.error(f"Unknown doc_type for sync: {doc_type}")
        return []

    table, items_table, fk_col = table_map[doc_type]
    data = fetch_data(f"/invoicing/v1/documents/{doc_type}/{doc_id}")
    if not data:
        logger.warning(f"Sync-back: no data returned for {doc_type}/{doc_id}")
        return []

    conn = get_db()
    _notify_project_code = None
    _success = False
    try:
        cursor = _cursor(conn)
        _upsert_single_document(cursor, data, table, items_table, fk_col)
        conn.commit()
        _notify_project_code = _extract_project_code(data.get('products'))
        _success = True
    except Exception as e:
        logger.error(f"Sync-back failed for {doc_type}/{doc_id}: {e}")
        conn.rollback()
    finally:
        release_db(conn)

    if not _success:
        return []

    # Notify Brain — non-blocking, after DB transaction is closed
    if _notify_project_code:
        from skills.job_tracker import BRAIN_API_URL, BRAIN_INTERNAL_KEY
        try:
            requests.post(
                f"{BRAIN_API_URL}/internal/job-review",
                json={"project_code": _notify_project_code, "trigger": "holded_sync"},
                headers={"x-api-key": BRAIN_INTERNAL_KEY},
                timeout=5,
            )
        except Exception:
            pass  # Non-critical — cron will pick it up

    return [table, items_table]


def sync_single_contact(contact_id):
    """Fetch one contact from Holded and upsert into local DB."""
    data = fetch_data(f"/invoicing/v1/contacts/{contact_id}")
    if not data:
        return []
    conn = get_db()
    try:
        cursor = _cursor(conn)
        _upsert_single_contact(cursor, data)
        conn.commit()
        return ["contacts"]
    except Exception as e:
        logger.error(f"Sync-back failed for contact/{contact_id}: {e}")
        conn.rollback()
        return []
    finally:
        release_db(conn)


def sync_single_product(product_id):
    """Fetch one product from Holded and upsert into local DB."""
    data = fetch_data(f"/invoicing/v1/products/{product_id}")
    if not data:
        return []
    conn = get_db()
    try:
        cursor = _cursor(conn)
        _upsert_single_product(cursor, data)
        conn.commit()
        return ["products"]
    except Exception as e:
        logger.error(f"Sync-back failed for product/{product_id}: {e}")
        conn.rollback()
        return []
    finally:
        release_db(conn)


def post_data(endpoint, payload):
    """POST to Holded API. Returns dict with 'error' key on failure."""
    if SAFE_MODE:
        logger.info(f"[SAFE MODE] Intercepted POST to {endpoint}")
        logger.debug(f"[SAFE MODE] Payload: {payload}")
        return {"status": 1, "id": "SAFE_MODE_ID_TEST", "info": "Dry run successful", "dry_run": True}

    url = f"{BASE_URL}{endpoint}"
    try:
        response = requests.post(url, headers=HEADERS, json=payload, timeout=30)
        if response.status_code in (200, 201):
            return response.json()
        else:
            logger.error(f"Error posting to {endpoint}: {response.status_code} - {response.text}")
            return {"error": True, "status_code": response.status_code, "detail": response.text}
    except requests.exceptions.Timeout:
        logger.error(f"Timeout posting to {endpoint}")
        return {"error": True, "status_code": 0, "detail": "Request timed out"}
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error posting to {endpoint}: {e}")
        return {"error": True, "status_code": 0, "detail": str(e)}

def put_data(endpoint, payload):
    """PUT to Holded API. Returns dict with 'error' key on failure."""
    if SAFE_MODE:
        logger.info(f"[SAFE MODE] Intercepted PUT to {endpoint}")
        logger.debug(f"[SAFE MODE] Payload: {payload}")
        return {"status": 1, "info": "Dry run successful", "dry_run": True}

    url = f"{BASE_URL}{endpoint}"
    try:
        response = requests.put(url, headers=HEADERS, json=payload, timeout=30)
        if response.status_code in (200, 201):
            return response.json()
        else:
            logger.error(f"Error putting to {endpoint}: {response.status_code} - {response.text}")
            return {"error": True, "status_code": response.status_code, "detail": response.text}
    except requests.exceptions.Timeout:
        logger.error(f"Timeout putting to {endpoint}")
        return {"error": True, "status_code": 0, "detail": "Request timed out"}
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error putting to {endpoint}: {e}")
        return {"error": True, "status_code": 0, "detail": str(e)}

def delete_data(endpoint):
    """DELETE on Holded API. Returns dict with 'error' key on failure."""
    if SAFE_MODE:
        logger.info(f"[SAFE MODE] Intercepted DELETE to {endpoint}")
        return {"status": 1, "info": "Dry run successful (delete)", "dry_run": True}

    url = f"{BASE_URL}{endpoint}"
    try:
        response = requests.delete(url, headers=HEADERS, timeout=30)
        if response.status_code in (200, 201, 204):
            try:
                return response.json()
            except ValueError:
                return {"status": 1, "info": "Deleted"}
        else:
            logger.error(f"Error deleting {endpoint}: {response.status_code} - {response.text}")
            return {"error": True, "status_code": response.status_code, "detail": response.text}
    except requests.exceptions.Timeout:
        logger.error(f"Timeout deleting {endpoint}")
        return {"error": True, "status_code": 0, "detail": "Request timed out"}
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error deleting {endpoint}: {e}")
        return {"error": True, "status_code": 0, "detail": str(e)}

def create_invoice(invoice_data):
    logger.info(f"Creating invoice for contact {invoice_data.get('contactId')}...")
    result = post_data("/invoicing/v1/documents/invoice", invoice_data)
    if result and not result.get("error") and result.get('status') == 1:
        logger.info(f"Invoice created successfully: {result.get('id')}")
        return result.get('id')
    if result and result.get("error"):
        return result
    return None

def update_estimate(estimate_id, estimate_data):
    logger.info(f"Updating estimate {estimate_id}...")
    result = put_data(f"/invoicing/v1/documents/estimate/{estimate_id}", estimate_data)
    if result and result.get('status') == 1:
        logger.info(f"Estimate {estimate_id} updated successfully.")
        return True
    return False

def fetch_estimate_fresh(estimate_id):
    """Read estimate items directly from Holded API (not local DB).
    Used by Brain executor and verifier to avoid stale cache."""
    endpoint = f"/invoicing/v1/documents/estimate/{estimate_id}"
    max_retries = 3
    for attempt in range(max_retries):
        try:
            url = f"{BASE_URL}{endpoint}"
            response = requests.get(url, headers=HEADERS, timeout=15)
            if response.status_code == 200:
                data = response.json()
                raw_items = data.get("products") or []
                items = []
                for it in raw_items:
                    items.append({
                        "name": it.get("name", ""),
                        "units": _num(it.get("units")) or 1,
                        "subtotal": _num(it.get("price")) or _num(it.get("subtotal")) or 0,
                        "price": _num(it.get("price")) or _num(it.get("subtotal")) or 0,
                        "desc": it.get("desc", ""),
                        "productId": it.get("productId", ""),
                        "product_id": it.get("productId", ""),
                    })
                logger.info(f"Fresh read estimate {estimate_id}: {len(items)} items from Holded API")
                return items
            elif response.status_code == 429:
                wait = 2 * (attempt + 1)
                logger.warning(f"Rate limit (429) reading estimate {estimate_id}, waiting {wait}s (attempt {attempt+1})")
                time.sleep(wait)
                continue
            else:
                logger.error(f"Holded API error reading estimate {estimate_id}: {response.status_code} - {response.text[:200]}")
                raise Exception(f"Holded API error: HTTP {response.status_code}")
        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            raise Exception(f"Holded API timeout reading estimate {estimate_id}")
        except requests.exceptions.RequestException as e:
            raise Exception(f"Network error reading estimate {estimate_id}: {e}")
    raise Exception(f"Failed to read estimate {estimate_id} after {max_retries} retries")

def create_contact(contact_data):
    logger.info(f"Creating contact {contact_data.get('name')}...")
    result = post_data("/invoicing/v1/contacts", contact_data)
    if result and not result.get("error") and result.get('status') == 1:
        logger.info(f"Contact created successfully: {result.get('id')}")
        return result.get('id')
    if result and result.get("error"):
        return result
    return None



def holded_put(endpoint, data):
    """PUT request to Holded API."""
    try:
        response = requests.put(f"{BASE_URL}{endpoint}", headers=HEADERS, json=data)
        if response.status_code == 200:
            return response.json()
        logger.error(f"Error putting to {endpoint}: {response.status_code} - {response.text[:200]}")
        return {"status": 0, "info": response.text[:200]}
    except Exception as e:
        logger.error(f"Exception putting to {endpoint}: {e}")
        return None

def create_estimate(estimate_data):
    logger.info(f"Creating estimate for contact {estimate_data.get('contactId')}...")
    logger.info(f"[ESTIMATE PAYLOAD] {json.dumps(estimate_data)}")
    result = post_data("/invoicing/v1/documents/estimate", estimate_data)
    logger.info(f"[ESTIMATE RESULT] {result}")
    if result and result.get('status') == 1:
        logger.info(f"Estimate created successfully: {result.get('id')}")
        return result.get('id')
    return result.get('id') if result else None

def send_document(doc_type, doc_id, send_data=None):
    logger.info(f"Sending {doc_type} {doc_id}...")
    payload = send_data or {}
    result = post_data(f"/invoicing/v1/documents/{doc_type}/{doc_id}/send", payload)
    if result:
        logger.info(f"Document {doc_id} sent successfully.")
        return True
    return False

def create_product(product_data):
    logger.info(f"Creating product {product_data.get('name')}...")
    result = post_data("/invoicing/v1/products", product_data)
    if result and result.get('status') == 1:
        logger.info(f"Product created successfully: {result.get('id')}")
        return result.get('id')
    return None

# ============= File Management Functions =============

def get_uploads_dir():
    """
    Return uploads directory path.
    Priority: settings table > env var > default
    Note: Directory may not exist yet; calling code should create it.
    """
    saved = get_setting("uploads_dir")
    if saved:
        return saved

    env_path = os.getenv("UPLOADS_DIR", "")
    if env_path:
        return env_path

    return os.path.abspath("uploads")

def get_reports_dir():
    """
    Return reports directory path.
    Priority: settings table > env var > default
    Note: Directory may not exist yet; calling code should create it.
    """
    saved = get_setting("reports_dir")
    if saved:
        return saved

    env_path = os.getenv("REPORTS_DIR", "")
    if env_path:
        return env_path

    return os.path.abspath("reports")

def set_uploads_dir(path):
    """
    Validate and save uploads directory path.
    Returns: {"success": True, "path": path} or {"error": "..."}
    """
    if not path:
        return {"error": "Path cannot be empty"}

    if not os.path.isabs(path):
        return {"error": "Path must be absolute (e.g., /home/user/uploads)"}

    if not os.path.exists(path):
        try:
            os.makedirs(path, exist_ok=True)
        except Exception as e:
            return {"error": f"Cannot create directory: {str(e)}"}

    if not os.path.isdir(path):
        return {"error": "Path is not a directory"}

    if not os.access(path, os.W_OK):
        return {"error": "Directory is not writable"}

    save_setting("uploads_dir", path)
    logger.info(f"Uploads directory set to: {path}")
    return {"success": True, "path": path}

def set_reports_dir(path):
    """
    Validate and save reports directory path.
    Returns: {"success": True, "path": path} or {"error": "..."}
    """
    if not path:
        return {"error": "Path cannot be empty"}

    if not os.path.isabs(path):
        return {"error": "Path must be absolute (e.g., /home/user/reports)"}

    if not os.path.exists(path):
        try:
            os.makedirs(path, exist_ok=True)
        except Exception as e:
            return {"error": f"Cannot create directory: {str(e)}"}

    if not os.path.isdir(path):
        return {"error": "Path is not a directory"}

    if not os.access(path, os.W_OK):
        return {"error": "Directory is not writable"}

    save_setting("reports_dir", path)
    logger.info(f"Reports directory set to: {path}")
    return {"success": True, "path": path}

def list_uploaded_files(limit=50):
    """
    List uploaded files with metadata.
    Returns list of dicts with: name, size, uploaded_at, type
    """
    uploads_dir = get_uploads_dir()
    os.makedirs(uploads_dir, exist_ok=True)

    files = []
    try:
        for f in os.listdir(uploads_dir)[:limit]:
            fpath = os.path.join(uploads_dir, f)
            if os.path.isfile(fpath):
                files.append({
                    "name": f,
                    "size": os.path.getsize(fpath),
                    "uploaded_at": os.path.getmtime(fpath),
                    "type": f.split(".")[-1] if "." in f else "unknown"
                })
    except Exception as e:
        logger.error(f"Error listing uploaded files: {str(e)}")

    return sorted(files, key=lambda x: x["uploaded_at"], reverse=True)

# ============= Purchase Analysis Functions =============

# Category keywords for rule-based classification.
# Each entry: (category, subcategory, [keywords_to_match_in_desc_or_supplier])
# Keywords are matched case-insensitively against: purchase desc + contact_name + item names
# ─── Taxonomía contable: espeja exactamente la carpeta
# 'MODELO Soportadas' del gestor de Miguel.
# category    → carpeta principal
# subcategory → subcarpeta (o proveedor concreto)
# keywords    → palabras buscadas en: desc + contact_name + item_names (lowercase)
CATEGORY_RULES = [

    # ── AIRBNB RECIBOS ──────────────────────────────────────
    ("AIRBNB RECIBOS",       "Airbnb",         ["airbnb"]),

    # ── AMAZON ──────────────────────────────────────────────
    # Amazon lleva a Claude para que determine el artículo concreto
    ("AMAZON",               "Amazon",         ["amazon eu", "amazon.es", "amazon services"]),

    # ── GASTOS LOCAL ────────────────────────────────────────
    ("GASTOS LOCAL",         "agua y luz",     ["iberdrola", "endesa", "naturgy", "fenosa",
                                                "aguas de", "canal isabel", "agua potable",
                                                "suministro electr", "luz "]),
    ("GASTOS LOCAL",         "alarma",         ["tyco", "securitas", "prosegur", "alarma",
                                                "conexion a cra", "cra "]),
    ("GASTOS LOCAL",         "alquiler",       ["alquiler", "arrendamiento", "renta local",
                                                "contrato arrendamiento"]),

    # ── SEGUROS - SEG SOCIAL ────────────────────────────────
    ("SEGUROS - SEG SOCIAL", "Seguro",         ["seguro", "axa", "mapfre", "mutua",
                                                "allianz", "zurich", "prima seguro"]),
    ("SEGUROS - SEG SOCIAL", "Seg. Social",    ["seguridad social", "autonomo", "cuota autonomo",
                                                "reta ", "tesoreria general"]),

    # ── SOFTWARE ────────────────────────────────────────────
    ("SOFTWARE",             "adobe",          ["adobe"]),
    ("SOFTWARE",             "apple",          ["apple distribution", "apple services",
                                                "itunes", "app store", "icloud"]),
    ("SOFTWARE",             "capture one",    ["capture one", "phase one"]),
    ("SOFTWARE",             "google",         ["google cloud", "google workspace",
                                                "google ireland", "google llc"]),
    ("SOFTWARE",             "holded",         ["holded"]),
    ("SOFTWARE",             "hostalia",       ["hostalia", "hosting", "dominio", "cpanel"]),
    ("SOFTWARE",             "spotify",        ["spotify"]),
    # Software genérico (no tiene subcarpeta propia aún)
    ("SOFTWARE",             "Suscripción",    ["microsoft", "dropbox", "notion", "slack",
                                                "fastspring", "github", "figma", "canva",
                                                "cloudflare", "digitalocean", "vercel",
                                                "suscripcion", "subscription"]),

    # ── TELEFONIA E INTERNET ────────────────────────────────
    ("TELEFONIA E INTERNET", "Digi",           ["digi spain", "digi telecom"]),
    ("TELEFONIA E INTERNET", "finetwork",      ["finetwork"]),
    # Otras operadoras sin subcarpeta propia
    ("TELEFONIA E INTERNET", "Telefonía",      ["movistar", "vodafone", "orange", "yoigo",
                                                "telefonica", "wewi mobile", "movil",
                                                "tarifa datos", "factura telefono"]),

    # ── TRANSPORTE ──────────────────────────────────────────
    ("TRANSPORTE",           "dhl",            ["dhl"]),
    ("TRANSPORTE",           "gasolina",       ["gasolina", "diesel", "combustible",
                                                "repsol", "bp ", "cepsa", "shell",
                                                "estacion de servicio"]),
    ("TRANSPORTE",           "renfe",          ["renfe", "ave ", "ouigo", "cercanias"]),
    ("TRANSPORTE",           "taxis",          ["taxi", "mytaxi", "cabify", "bolt",
                                                "free now", "cabapp"]),
    ("TRANSPORTE",           "uber",           ["uber"]),
    ("TRANSPORTE",           "vuelos",         ["vueling", "iberia", "ryanair", "transavia",
                                                "air europa", "wizzair", "easyjet",
                                                "vuelo", "billete aereo", "aena"]),
    # Mensajería/paquetería sin subcarpeta propia
    ("TRANSPORTE",           "Mensajería",     ["correos", "fedex", "ups", "mrw",
                                                "seur", "nacex", "envio paquete"]),

    # ── A AMORTIZAR ─────────────────────────────────────────
    # Equipamiento de alto valor que se amortiza
    ("A AMORTIZAR",          "Equipamiento",   ["fnac", "mediamarkt", "pccomponentes",
                                                "bambulab", "bambu lab", "anker",
                                                "llumm", "fotopro", "photospecialist",
                                                "kamera express", "leroy merlin", "leroy",
                                                "bauhaus", "ikea"]),

    # ── VARIOS ──────────────────────────────────────────────
    # Cajón de sastre — lo que no encaje en ninguna carpeta
    ("VARIOS",               "Restaurante",    ["restaurante", "cafeteria", "comida",
                                                "cena", "almuerzo", "glovo", "just eat",
                                                "uber eats"]),
    ("VARIOS",               "Formación",      ["formacion", "curso", "training",
                                                "udemy", "coursera", "master"]),
    ("VARIOS",               "Varios",         ["varios", "material oficina", "papeleria"]),
]

def categorize_by_rules(desc: str, contact_name: str, item_names: list):
    """
    Try to categorize a purchase invoice using keyword rules.
    Returns dict with category/subcategory/confidence='high' or None if no match.

    Categories mirror the folder structure in:
    ~/Documents/MIGUEL/WORK/CONTABILIDAD/MODELO Soportadas
    Add new rules to CATEGORY_RULES when you create new supplier folders.
    """
    text = " ".join(filter(None, [desc, contact_name] + item_names)).lower()
    for category, subcategory, keywords in CATEGORY_RULES:
        if any(kw in text for kw in keywords):
            matched_kw = next(kw for kw in keywords if kw in text)
            return {
                "category": category,
                "subcategory": subcategory,
                "confidence": "high",
                "method": "rules",
                "reasoning": f"Keyword match: '{matched_kw}'"
            }
    return None


def get_unanalyzed_purchases(limit: int = 10) -> list:
    """Return up to `limit` purchase invoices not yet in purchase_analysis."""
    # Aggregate item names — SQLite uses GROUP_CONCAT, PostgreSQL uses STRING_AGG
    agg = "GROUP_CONCAT(pit.name, '||')" if _USE_SQLITE else "STRING_AGG(pit.name, '||')"
    conn = get_db()
    try:
        cursor = _cursor(conn)
        cursor.execute(_q(f'''
            SELECT pi.id, pi.contact_name, pi."desc", pi.date, pi.amount,
                   {agg} AS item_names
            FROM purchase_invoices pi
            LEFT JOIN purchase_items pit ON pit.purchase_id = pi.id
            LEFT JOIN purchase_analysis pa ON pa.purchase_id = pi.id
            WHERE pa.id IS NULL
            GROUP BY pi.id, pi.contact_name, pi."desc", pi.date, pi.amount
            ORDER BY pi.date DESC
            LIMIT ?
        '''), (limit,))
        rows = cursor.fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d['item_names'] = [n for n in (d.get('item_names') or '').split('||') if n]
            result.append(d)
        return result
    finally:
        release_db(conn)


def save_purchase_analysis(purchase_id: str, category: str, subcategory: str,
                           confidence: str, method: str, reasoning: str):
    """Persist the analysis result for a purchase invoice."""
    conn = get_db()
    try:
        cursor = _cursor(conn)
        vals = (purchase_id, category, subcategory, confidence, method, reasoning)
        if _USE_SQLITE:
            cursor.execute('''
                INSERT OR REPLACE INTO purchase_analysis
                    (purchase_id, category, subcategory, confidence, method, reasoning)
                VALUES (?,?,?,?,?,?)
            ''', vals)
        else:
            cursor.execute('''
                INSERT INTO purchase_analysis
                    (purchase_id, category, subcategory, confidence, method, reasoning)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (purchase_id) DO UPDATE SET
                    category=EXCLUDED.category, subcategory=EXCLUDED.subcategory,
                    confidence=EXCLUDED.confidence, method=EXCLUDED.method,
                    reasoning=EXCLUDED.reasoning
            ''', vals)
        conn.commit()
    finally:
        release_db(conn)


def get_analyzed_invoices(limit: int = 50, offset: int = 0, category: str = None, q: str = None) -> list:
    """List categorized purchase invoices with their analysis, newest first.
    Optional q= for full-text search across contact_name, desc, category, subcategory."""
    conn = get_db()
    try:
        cursor = _cursor(conn)
        ph = '%s' if not _USE_SQLITE else '?'
        where = "WHERE pa.id IS NOT NULL"
        params = []
        if category:
            where += f" AND pa.category = {ph}"
            params.append(category)
        if q:
            like = f"%{q}%"
            where += f' AND (pi.contact_name LIKE {ph} OR pi."desc" LIKE {ph} OR pa.category LIKE {ph} OR pa.subcategory LIKE {ph})'
            params += [like, like, like, like]
        cursor.execute(f'''
            SELECT pi.id, pi.contact_name, pi."desc", pi.amount, pi.date, pi.status,
                   pa.category, pa.subcategory, pa.confidence, pa.method, pa.reasoning
            FROM purchase_invoices pi
            JOIN purchase_analysis pa ON pi.id = pa.purchase_id
            {where}
            ORDER BY pi.date DESC
            LIMIT {ph} OFFSET {ph}
        ''', params + [limit, offset])
        return [dict(r) for r in cursor.fetchall()]
    finally:
        release_db(conn)


def get_analysis_stats() -> dict:
    """Summary of analysis progress."""
    conn = get_db()
    try:
        cursor = _cursor(conn)
        cursor.execute("SELECT COUNT(*) AS total FROM purchase_invoices")
        total = _fetch_one_val(cursor, "total") or 0
        cursor.execute("SELECT COUNT(*) AS analyzed FROM purchase_analysis")
        analyzed = _fetch_one_val(cursor, "analyzed") or 0
        cursor.execute('''
            SELECT category, COUNT(*) AS count, SUM(pi.amount) AS total_amount
            FROM purchase_analysis pa
            JOIN purchase_invoices pi ON pi.id = pa.purchase_id
            GROUP BY category ORDER BY total_amount DESC
        ''')
        by_category = [dict(r) for r in cursor.fetchall()]
        cursor.execute("SELECT MAX(analyzed_at) AS last_run FROM purchase_analysis")
        last_run = _fetch_one_val(cursor, "last_run")
        return {
            "total": total,
            "analyzed": analyzed,
            "pending": total - analyzed,
            "pct": round(analyzed / total * 100, 1) if total > 0 else 0,
            "by_category": by_category,
            "last_run_db": last_run,
        }
    finally:
        release_db(conn)


# ─── Inventory Matcher ───────────────────────────────────────────────

def find_inventory_in_purchases() -> list:
    """
    Exhaustive scan of purchase_items to find inventory products.

    Matching strategy (per product, looking for best item):
      1. Exact: purchase_item.product_id == product.id  (Holded-linked)
      2. Substring: cleaned product name contained in cleaned item name
      3. Token overlap: >=75% of product name tokens found in item name
      4. Fuzzy: SequenceMatcher ratio >= 0.72 on cleaned names

    Cleaning:
      - Strips leading Amazon ASIN codes (e.g. "B08GTYFC37 - Product...")
      - Strips quantity prefixes from product names ("2x Profoto..." -> "Profoto...")
      - Lowercases both sides before comparison

    Skips products already in amortizations or inventory_matches (any status).
    Iterates by product to guarantee one best match per product.
    """
    import re
    from difflib import SequenceMatcher
    from datetime import datetime

    # Supplier-level descriptions that never contain product info
    _NOISE_PATTERNS = re.compile(
        r'factura|invoice|billete|taxi|taxim|airbnb|vueling|iberia|ryanair|'
        r'adobe|holded|confirmaci|recibo|receipt|flight|booking|resumen de|'
        r'contrato|cuota|tarifa|orden de pago|seguro|seg\. social',
        re.IGNORECASE
    )

    def _clean_item(name):
        if not name:
            return ''
        # Remove Amazon ASIN prefix: "B08GTYFC37 - Real name..."
        name = re.sub(r'^[A-Z0-9]{10}\s*[-]\s*', '', name, flags=re.IGNORECASE)
        # Remove article numbers like "001.00 - " or "93483136 - "
        name = re.sub(r'^\d+[\.\d]*\s*[-]\s*', '', name)
        return name.strip().lower()

    def _clean_product(name):
        # Strip quantity prefix: "2x Profoto Air Remote" -> "profoto air remote"
        name = re.sub(r'^\d+x\s+', '', name, flags=re.IGNORECASE)
        return name.strip().lower()

    def _score(pname_clean, iname_clean):
        if not pname_clean or not iname_clean:
            return 0.0
        # Substring containment (most reliable for partial descriptions)
        if len(pname_clean) >= 6 and pname_clean in iname_clean:
            return 0.96
        if len(iname_clean) >= 6 and iname_clean in pname_clean:
            return 0.93
        # Token overlap: how many product tokens (>=4 chars) appear in item
        tokens = [t for t in pname_clean.split() if len(t) >= 4]
        if tokens:
            overlap = sum(1 for t in tokens if t in iname_clean) / len(tokens)
            if overlap >= 0.75:
                return 0.85 + overlap * 0.1
        # Full fuzzy ratio trimmed to avoid length bias
        return SequenceMatcher(
            None, pname_clean, iname_clean[:len(pname_clean) + 25]
        ).ratio()

    conn = get_db()
    try:
        cursor = _cursor(conn)

        cursor.execute("SELECT id, name FROM products")
        products = [dict(r) for r in cursor.fetchall()]

        # Load ALL purchase items with a real price
        cursor.execute('''
            SELECT pit.id AS item_id, pit.purchase_id, pit.product_id,
                   pit.name AS item_name, pit.price, pit.units,
                   pi.date, pi.contact_name
            FROM purchase_items pit
            JOIN purchase_invoices pi ON pi.id = pit.purchase_id
            WHERE pit.price IS NOT NULL AND pit.price > 0
        ''')
        all_items = [dict(r) for r in cursor.fetchall()]

        # Pre-filter noise and pre-clean names
        useful_items = []
        for item in all_items:
            raw = item['item_name'] or ''
            if _NOISE_PATTERNS.search(raw):
                continue
            cleaned = _clean_item(raw)
            if len(cleaned) < 5:
                continue
            item['_cleaned'] = cleaned
            useful_items.append(item)

        # Products already handled — skip them
        cursor.execute("SELECT product_id FROM amortizations")
        already_amort = set(r['product_id'] if isinstance(r, dict) else r[0] for r in cursor.fetchall())
        cursor.execute("SELECT product_id FROM inventory_matches")
        already_matched = set(r['product_id'] if isinstance(r, dict) else r[0] for r in cursor.fetchall())
        skip_ids = already_amort | already_matched

        matches = []
        for prod in products:
            if prod['id'] in skip_ids:
                continue

            pname_clean = _clean_product(prod['name'])
            if len(pname_clean) < 4:
                continue

            best_score = 0.0
            best_item = None
            best_method = None

            for item in useful_items:
                # Strategy 1: exact Holded product_id link
                if item['product_id'] and item['product_id'] == prod['id']:
                    best_score = 1.0
                    best_item = item
                    best_method = 'exact_id'
                    break

                sc = _score(pname_clean, item['_cleaned'])
                if sc > best_score:
                    best_score = sc
                    best_item = item
                    best_method = f'fuzzy_{int(sc * 100)}pct'

            if best_score >= 0.72 and best_item:
                date_str = ''
                if best_item['date']:
                    date_str = datetime.fromtimestamp(best_item['date']).strftime('%Y-%m-%d')
                matches.append({
                    'purchase_id':          best_item['purchase_id'],
                    'purchase_item_id':     best_item['item_id'],
                    'product_id':           prod['id'],
                    'product_name':         prod['name'],
                    'item_name_in_invoice': best_item['item_name'],
                    'matched_price':        round(float(best_item['price'] or 0), 2),
                    'matched_date':         date_str,
                    'match_method':         best_method,
                    'supplier':             best_item['contact_name'],
                })

        return matches
    finally:
        release_db(conn)


def save_inventory_match(purchase_id, purchase_item_id, product_id,
                         product_name, matched_price, matched_date, match_method):
    """Persist a pending inventory match for user review."""
    conn = get_db()
    try:
        cursor = _cursor(conn)
        vals = (purchase_id, purchase_item_id, product_id, product_name,
                matched_price, matched_date, match_method)
        if _USE_SQLITE:
            cursor.execute('''
                INSERT OR IGNORE INTO inventory_matches
                    (purchase_id, purchase_item_id, product_id, product_name,
                     matched_price, matched_date, match_method, status)
                VALUES (?,?,?,?,?,?,?,'pending')
            ''', vals)
            new_id = cursor.lastrowid
        else:
            cursor.execute('''
                INSERT INTO inventory_matches
                    (purchase_id, purchase_item_id, product_id, product_name,
                     matched_price, matched_date, match_method, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'pending')
                ON CONFLICT (purchase_id, product_id) DO NOTHING
                RETURNING id
            ''', vals)
            row = cursor.fetchone()
            new_id = row['id'] if row else None
        conn.commit()
        return new_id
    finally:
        release_db(conn)


def get_pending_matches() -> list:
    """Return all inventory matches awaiting user confirmation."""
    # epoch → readable date differs by dialect
    date_col = ("datetime(pi.date, 'unixepoch') AS invoice_date_full"
                if _USE_SQLITE else
                "to_timestamp(pi.date)::text AS invoice_date_full")
    conn = get_db()
    try:
        cursor = _cursor(conn)
        cursor.execute(f'''
            SELECT im.*,
                   pi.contact_name AS supplier,
                   {date_col},
                   pit.name AS item_name_found
            FROM inventory_matches im
            JOIN purchase_invoices pi ON pi.id = im.purchase_id
            LEFT JOIN purchase_items pit ON pit.id = im.purchase_item_id
            WHERE im.status = 'pending'
            ORDER BY im.matched_price DESC
        ''')
        return [dict(r) for r in cursor.fetchall()]
    finally:
        release_db(conn)


def confirm_inventory_match(match_id: int, confirmed: bool, custom_price=None,
                            allocation_note="", product_type=None):
    """
    Confirm or reject a pending inventory match.
    If confirmed:
      - Upsert into amortizations (creates or skips if product_id already there)
      - Add a new row to amortization_purchases with cost_override
      - Recalculate amortizations.purchase_price = SUM of all purchase links
    custom_price: overrides auto-detected price for this specific purchase link
    allocation_note: e.g. "1/3 del pack" — stored on the purchase link
    """
    conn = get_db()
    try:
        cursor = _cursor(conn)
        cursor.execute(_q("SELECT * FROM inventory_matches WHERE id=?"), (match_id,))
        match = cursor.fetchone()
        if not match:
            return {"ok": False, "error": "Match not found"}

        if not confirmed:
            cursor.execute(_q("UPDATE inventory_matches SET status='rejected' WHERE id=?"), (match_id,))
            conn.commit()
            return {"ok": True, "action": "rejected"}

        # Mark match as confirmed
        cursor.execute(_q("UPDATE inventory_matches SET status='confirmed' WHERE id=?"), (match_id,))

        final_price = float(custom_price) if custom_price and float(custom_price) > 0 else match['matched_price']
        ptype = product_type or 'alquiler'

        # Upsert amortization — INSERT OR IGNORE so existing products are not overwritten
        amort_vals = (match['product_id'], match['product_name'],
                      match['matched_date'],
                      f"Vinculado desde inventory_matches via {match['match_method']}",
                      ptype)
        if _USE_SQLITE:
            cursor.execute('''
                INSERT OR IGNORE INTO amortizations
                    (product_id, product_name, purchase_price, purchase_date, notes, product_type)
                VALUES (?,?,0,?,?,?)
            ''', amort_vals)
        else:
            cursor.execute('''
                INSERT INTO amortizations
                    (product_id, product_name, purchase_price, purchase_date, notes, product_type)
                VALUES (%s,%s,0,%s,%s,%s)
                ON CONFLICT (product_id) DO NOTHING
            ''', amort_vals)

        # Retrieve the amortization id (whether just created or pre-existing)
        cursor.execute(_q("SELECT id FROM amortizations WHERE product_id=?"), (match['product_id'],))
        amort_row = cursor.fetchone()
        if not amort_row:
            conn.rollback()
            return {"ok": False, "error": "Could not create or find amortization"}
        amort_id = amort_row['id'] if isinstance(amort_row, dict) else amort_row[0]

        # Add the purchase link
        note = allocation_note or f"Auto desde match #{match_id} ({match['match_method']})"
        cursor.execute(_q('''
            INSERT INTO amortization_purchases
                (amortization_id, purchase_id, purchase_item_id, cost_override, allocation_note)
            VALUES (?,?,?,?,?)
        '''), (amort_id, match['purchase_id'], match['purchase_item_id'], final_price, note))

        # Recalculate purchase_price
        _recalc_purchase_price(cursor, amort_id)

        conn.commit()
        return {"ok": True, "action": "confirmed",
                "amortization_id": amort_id, "price_used": final_price}
    finally:
        release_db(conn)


# ============= Amortization Functions =============

def _recalc_purchase_price(cursor, amortization_id):
    """Recompute amortizations.purchase_price = SUM(cost_override) from amortization_purchases."""
    cursor.execute(
        _q("SELECT COALESCE(SUM(cost_override), 0) AS total FROM amortization_purchases WHERE amortization_id=?"),
        (amortization_id,)
    )
    total = _fetch_one_val(cursor, "total") or 0
    now_expr = "datetime('now')" if _USE_SQLITE else "NOW()"
    cursor.execute(
        _q(f"UPDATE amortizations SET purchase_price=?, updated_at={now_expr} WHERE id=?"),
        (total, amortization_id)
    )


def get_amortization_purchases(amortization_id):
    """Return all purchase links for one amortization, with purchase invoice details."""
    conn = get_db()
    try:
        cursor = _cursor(conn)
        cursor.execute(_q('''
            SELECT
                ap.id,
                ap.amortization_id,
                ap.purchase_id,
                ap.purchase_item_id,
                ap.cost_override,
                ap.allocation_note,
                ap.created_at,
                pi.contact_name  AS supplier,
                pi."desc"        AS invoice_desc,
                pi.date          AS invoice_date,
                pi."desc"        AS doc_number,
                pit.name         AS item_name,
                pit.price        AS item_unit_price,
                pit.units        AS item_units
            FROM amortization_purchases ap
            LEFT JOIN purchase_invoices pi  ON pi.id  = ap.purchase_id
            LEFT JOIN purchase_items    pit ON pit.id = ap.purchase_item_id
            WHERE ap.amortization_id = ?
            ORDER BY ap.created_at
        '''), (amortization_id,))
        return [dict(r) for r in cursor.fetchall()]
    finally:
        release_db(conn)


def add_amortization_purchase(amortization_id, cost_override, allocation_note="",
                               purchase_id=None, purchase_item_id=None):
    """Link a purchase invoice/item to an amortization with a specific cost."""
    conn = get_db()
    try:
        cursor = _cursor(conn)
        vals = (amortization_id, purchase_id, purchase_item_id,
                float(cost_override), allocation_note)
        if _USE_SQLITE:
            cursor.execute('''
                INSERT INTO amortization_purchases
                    (amortization_id, purchase_id, purchase_item_id, cost_override, allocation_note)
                VALUES (?,?,?,?,?)
            ''', vals)
            new_id = cursor.lastrowid
        else:
            cursor.execute('''
                INSERT INTO amortization_purchases
                    (amortization_id, purchase_id, purchase_item_id, cost_override, allocation_note)
                VALUES (%s,%s,%s,%s,%s)
                RETURNING id
            ''', vals)
            row = cursor.fetchone()
            new_id = row['id'] if row else None
        _recalc_purchase_price(cursor, amortization_id)
        conn.commit()
        return new_id
    finally:
        release_db(conn)


def update_amortization_purchase(purchase_link_id, cost_override=None, allocation_note=None,
                                  purchase_id=None, purchase_item_id=None):
    """Edit a purchase link (cost or note). Recalculates parent purchase_price."""
    conn = get_db()
    try:
        cursor = _cursor(conn)
        ph = '%s' if not _USE_SQLITE else '?'
        fields, values = [], []
        if cost_override is not None:
            fields.append(f"cost_override={ph}"); values.append(float(cost_override))
        if allocation_note is not None:
            fields.append(f"allocation_note={ph}"); values.append(allocation_note)
        if purchase_id is not None:
            fields.append(f"purchase_id={ph}"); values.append(purchase_id)
        if purchase_item_id is not None:
            fields.append(f"purchase_item_id={ph}"); values.append(purchase_item_id)
        if not fields:
            return False
        values.append(purchase_link_id)
        cursor.execute(f"UPDATE amortization_purchases SET {', '.join(fields)} WHERE id={ph}", values)
        # Get parent amortization_id to recalc
        cursor.execute(_q("SELECT amortization_id FROM amortization_purchases WHERE id=?"), (purchase_link_id,))
        row = cursor.fetchone()
        if row:
            parent_id = row['amortization_id'] if isinstance(row, dict) else row[0]
            _recalc_purchase_price(cursor, parent_id)
        conn.commit()
        return cursor.rowcount > 0
    finally:
        release_db(conn)


def delete_amortization_purchase(purchase_link_id):
    """Remove a purchase link and recalculate parent purchase_price."""
    conn = get_db()
    try:
        cursor = _cursor(conn)
        cursor.execute(_q("SELECT amortization_id FROM amortization_purchases WHERE id=?"), (purchase_link_id,))
        row = cursor.fetchone()
        if not row:
            return False
        amort_id = row['amortization_id'] if isinstance(row, dict) else row[0]
        cursor.execute(_q("DELETE FROM amortization_purchases WHERE id=?"), (purchase_link_id,))
        _recalc_purchase_price(cursor, amort_id)
        conn.commit()
        return True
    finally:
        release_db(conn)


def get_product_type_rules() -> list:
    """Return all product type fiscal rules."""
    conn = get_db()
    try:
        cursor = _cursor(conn)
        cursor.execute("SELECT * FROM product_type_rules ORDER BY is_expense, irpf_pct DESC")
        return [dict(r) for r in cursor.fetchall()]
    finally:
        release_db(conn)


def get_pack_info(product_id):
    """Return pack composition (if pack) or packs containing this product (if component).
    Returns dict with 'kind', 'components' (if pack), and 'member_of' (if component)."""
    conn = get_db()
    try:
        cursor = _cursor(conn)
        cursor.execute(_q("SELECT id, name, kind, price FROM products WHERE id = ?"), (product_id,))
        prod = cursor.fetchone()
        if not prod:
            return None
        prod = dict(prod)
        result = {"product_id": product_id, "name": prod["name"], "kind": prod["kind"] or "simple"}

        if prod["kind"] == "pack":
            cursor.execute(_q('''
                SELECT pc.component_id, pc.quantity, p.name, p.price
                FROM pack_components pc
                LEFT JOIN products p ON p.id = pc.component_id
                WHERE pc.pack_id = ?
            '''), (product_id,))
            result["components"] = [dict(r) for r in cursor.fetchall()]
        else:
            cursor.execute(_q('''
                SELECT pc.pack_id, pc.quantity, p.name, p.price
                FROM pack_components pc
                LEFT JOIN products p ON p.id = pc.pack_id
                WHERE pc.component_id = ?
            '''), (product_id,))
            result["member_of"] = [dict(r) for r in cursor.fetchall()]

        return result
    finally:
        release_db(conn)


def get_amortizations():
    """
    Return all amortizations with real-time revenue calculation from invoice_items.
    Revenue includes both direct revenue (product_id match) and pack revenue
    (attributed proportionally from packs that contain the component product).
    Also returns product_type and its fiscal rule (irpf_pct).
    """
    conn = get_db()
    try:
        cursor = _cursor(conn)

        # 1. Direct revenue per amortization (unchanged logic)
        cursor.execute('''
            SELECT
                a.id,
                a.product_id,
                a.product_name,
                a.purchase_price,
                a.purchase_date,
                a.notes,
                a.product_type,
                a.created_at,
                COALESCE(SUM(
                    CASE
                        WHEN ii.subtotal IS NOT NULL AND ii.subtotal > 0
                        THEN ii.subtotal
                        ELSE COALESCE(ii.units, 0) * COALESCE(ii.price, 0)
                    END
                ), 0.0) AS direct_revenue,
                ptr.label        AS type_label,
                ptr.irpf_pct     AS irpf_pct,
                ptr.is_expense   AS is_expense
            FROM amortizations a
            LEFT JOIN invoice_items ii  ON ii.product_id  = a.product_id
            LEFT JOIN product_type_rules ptr ON ptr.type_key = a.product_type
            GROUP BY a.id, a.product_id, a.product_name, a.purchase_price,
                     a.purchase_date, a.notes, a.product_type, a.created_at,
                     ptr.label, ptr.irpf_pct, ptr.is_expense
            ORDER BY a.purchase_date DESC
        ''')
        rows = [dict(r) for r in cursor.fetchall()]

        # 2. Load pack_components with component prices for proportional attribution
        cursor.execute('''
            SELECT pc.pack_id, pc.component_id, pc.quantity, COALESCE(p.price, 0) AS component_price
            FROM pack_components pc
            LEFT JOIN products p ON p.id = pc.component_id
        ''')
        comp_rows = cursor.fetchall()

        # Build lookups: component_id → list of packs, pack_id → list of components
        comp_to_packs = {}   # component_id → [pack_id, ...]
        pack_contents = {}   # pack_id → [(component_id, qty, price), ...]
        for cr in comp_rows:
            cr = dict(cr)
            pack_id = cr['pack_id']
            comp_id = cr['component_id']
            qty = float(cr['quantity'] or 1)
            price = float(cr['component_price'] or 0)
            comp_to_packs.setdefault(comp_id, []).append(pack_id)
            pack_contents.setdefault(pack_id, []).append((comp_id, qty, price))

        # Collect all amortized product_ids that appear in some pack
        amort_product_ids = {r['product_id'] for r in rows}
        relevant_pack_ids = set()
        for pid in amort_product_ids:
            for pack_id in comp_to_packs.get(pid, []):
                relevant_pack_ids.add(pack_id)

        # 3. Get pack invoice revenue for relevant packs
        pack_revenue_map = {}  # pack_id → total_invoiced
        pack_count_map = {}    # pack_id → number of invoice lines
        if relevant_pack_ids:
            placeholders = ','.join(['?' if _USE_SQLITE else '%s'] * len(relevant_pack_ids))
            pack_ids_list = list(relevant_pack_ids)
            cursor.execute(f'''
                SELECT product_id,
                       COALESCE(SUM(
                           CASE WHEN subtotal IS NOT NULL AND subtotal > 0
                                THEN subtotal
                                ELSE COALESCE(units, 0) * COALESCE(price, 0)
                           END
                       ), 0) AS pack_total,
                       COUNT(*) AS line_count
                FROM invoice_items
                WHERE product_id IN ({placeholders})
                GROUP BY product_id
            ''', pack_ids_list)
            for pr in cursor.fetchall():
                pr = dict(pr)
                pack_revenue_map[pr['product_id']] = float(pr['pack_total'])
                pack_count_map[pr['product_id']] = pr['line_count']

        # 4. Attribute pack revenue proportionally to each component
        result = []
        for d in rows:
            pack_revenue = 0.0
            pack_count = 0
            component_packs = comp_to_packs.get(d['product_id'], [])
            for pack_id in component_packs:
                total_pack_invoiced = pack_revenue_map.get(pack_id, 0)
                if total_pack_invoiced <= 0:
                    continue
                # Calculate this component's share of the pack
                components = pack_contents.get(pack_id, [])
                total_pack_value = sum(c_price * c_qty for _, c_qty, c_price in components)
                my_entry = next((c for c in components if c[0] == d['product_id']), None)
                if my_entry and total_pack_value > 0:
                    my_value = my_entry[2] * my_entry[1]  # price * qty
                    share = my_value / total_pack_value
                elif components:
                    share = 1.0 / len(components)  # equal split fallback
                else:
                    share = 1.0
                pack_revenue += total_pack_invoiced * share
                pack_count += pack_count_map.get(pack_id, 0)

            d['direct_revenue'] = float(d.pop('direct_revenue', 0))
            d['pack_revenue'] = round(pack_revenue, 2)
            d['pack_count'] = pack_count
            d['total_revenue'] = round(d['direct_revenue'] + pack_revenue, 2)
            d['purchase_price'] = float(d['purchase_price'] or 0)
            d['profit'] = d['total_revenue'] - d['purchase_price']
            d['roi_pct'] = round((d['profit'] / d['purchase_price'] * 100), 2) if d['purchase_price'] > 0 else 0
            d['status'] = 'AMORTIZADO' if d['profit'] >= 0 else 'EN CURSO'
            result.append(d)
        return result
    finally:
        release_db(conn)


def add_amortization(product_id, product_name, purchase_price, purchase_date,
                     notes="", product_type="alquiler"):
    """Add a product to amortization tracking.
    Returns new id, None if duplicate, or raises ValueError if product is a pack."""
    conn = get_db()
    try:
        cursor = _cursor(conn)
        # Guard: reject pack products — track components instead
        cursor.execute(_q("SELECT kind FROM products WHERE id = ?"), (product_id,))
        prod_row = cursor.fetchone()
        if prod_row:
            kind = prod_row['kind'] if isinstance(prod_row, dict) else prod_row[0]
            if kind == 'pack':
                raise ValueError(
                    f"'{product_name}' is a pack product. "
                    "Track its component products instead for accurate ROI."
                )

        vals = (product_id, product_name, purchase_price, purchase_date, notes, product_type)
        if _USE_SQLITE:
            cursor.execute('''
                INSERT INTO amortizations
                    (product_id, product_name, purchase_price, purchase_date, notes, product_type)
                VALUES (?,?,?,?,?,?)
            ''', vals)
            new_id = cursor.lastrowid
        else:
            cursor.execute('''
                INSERT INTO amortizations
                    (product_id, product_name, purchase_price, purchase_date, notes, product_type)
                VALUES (%s,%s,%s,%s,%s,%s)
                RETURNING id
            ''', vals)
            row = cursor.fetchone()
            new_id = row['id'] if row else None
        conn.commit()
        return new_id
    except ValueError:
        conn.rollback()
        raise  # re-raise pack error for API to handle
    except Exception:
        conn.rollback()
        return None  # likely duplicate product_id (UNIQUE constraint)
    finally:
        release_db(conn)


def update_amortization(amort_id, purchase_price=None, purchase_date=None,
                        notes=None, product_type=None):
    """Update fields for an amortization entry. Pass only the fields to change."""
    conn = get_db()
    try:
        cursor = _cursor(conn)
        ph = '%s' if not _USE_SQLITE else '?'
        fields, values = [], []
        if purchase_price is not None:
            fields.append(f"purchase_price={ph}"); values.append(purchase_price)
        if purchase_date is not None:
            fields.append(f"purchase_date={ph}"); values.append(purchase_date)
        if notes is not None:
            fields.append(f"notes={ph}"); values.append(notes)
        if product_type is not None:
            fields.append(f"product_type={ph}"); values.append(product_type)
        if not fields:
            return False
        now_expr = "datetime('now')" if _USE_SQLITE else "NOW()"
        fields.append(f"updated_at={now_expr}")
        values.append(amort_id)
        cursor.execute(f"UPDATE amortizations SET {', '.join(fields)} WHERE id={ph}", values)
        conn.commit()
        return cursor.rowcount > 0
    finally:
        release_db(conn)


def delete_amortization(amort_id):
    """Remove a product from amortization tracking."""
    conn = get_db()
    try:
        cursor = _cursor(conn)
        cursor.execute(_q("DELETE FROM amortizations WHERE id=?"), (amort_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        release_db(conn)

def get_amortization_summary():
    """Global summary: total invested, total recovered, global profit, counts."""
    rows = get_amortizations()
    total_invested = sum(r['purchase_price'] for r in rows)
    total_revenue = sum(r['total_revenue'] for r in rows)
    total_profit = sum(r['profit'] for r in rows)
    amortized_count = sum(1 for r in rows if r['status'] == 'AMORTIZADO')
    return {
        "total_invested": round(total_invested, 2),
        "total_revenue": round(total_revenue, 2),
        "total_profit": round(total_profit, 2),
        "global_roi_pct": round((total_profit / total_invested * 100), 2) if total_invested > 0 else 0,
        "total_products": len(rows),
        "amortized_count": amortized_count,
        "in_progress_count": len(rows) - amortized_count
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    if not _conn.API_KEY:
        logger.error("HOLDED_API_KEY not found in .env file.")
    else:
        if _conn.SAFE_MODE:
            logger.warning("SAFE MODE (DRY RUN) ACTIVE - No data will be modified in Holded")

        init_db()
        sync_contacts()
        sync_products()
        sync_invoices()
        sync_estimates()
        sync_purchases()
        sync_projects()
        sync_payments()

        # Flush pending job notes to Obsidian
        try:
            from skills.job_tracker import flush_note_queue
            count = flush_note_queue()
            if count:
                logger.info(f"[JOB_TRACKER] Flushed {count} job notes to Obsidian")
        except Exception as e:
            logger.error(f"[JOB_TRACKER] Queue flush failed: {e}")

        logger.info("Synchronization complete!")
