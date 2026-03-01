import os
import sqlite3
import logging
import requests
from dotenv import load_dotenv
import time

logger = logging.getLogger(__name__)

# Load configuration
load_dotenv()
API_KEY = os.getenv("HOLDED_API_KEY")
SAFE_MODE = os.getenv("HOLDED_SAFE_MODE", "true").lower() == "true"
BASE_URL = "https://api.holded.com/api"
HEADERS = {
    "key": API_KEY,
    "Content-Type": "application/json"
}

# ── Database backend selection ───────────────────────────────────────────────
# If DATABASE_URL is set → use PostgreSQL (Supabase in production).
# If not set → fall back to local SQLite (dev mode, zero config required).
DATABASE_URL = os.getenv("DATABASE_URL")
_USE_SQLITE = not DATABASE_URL

if not _USE_SQLITE:
    import psycopg2
    import psycopg2.extras

DB_NAME = os.getenv("DB_NAME", "holded.db")  # used only in SQLite mode


def get_db():
    """Return a database connection for the active backend."""
    if _USE_SQLITE:
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        return conn
    return psycopg2.connect(DATABASE_URL)


def _cursor(conn):
    """Return a dict-like cursor regardless of DB backend."""
    if _USE_SQLITE:
        return conn.cursor()
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def _q(sql):
    """Convert SQLite ? placeholders to PostgreSQL %s when needed."""
    if _USE_SQLITE:
        return sql
    return sql.replace("?", "%s")

# ────────────────────────────────────────────────────────────────────────────


def reload_config():
    global API_KEY, HEADERS
    conn = get_db()
    try:
        cursor = _cursor(conn)
        cursor.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        cursor.execute("SELECT key, value FROM settings")
        rows = cursor.fetchall()
        if _USE_SQLITE:
            settings = {row[0]: row[1] for row in rows}
        else:
            settings = {row["key"]: row["value"] for row in rows}

        if 'holded_api_key' in settings:
            API_KEY = settings['holded_api_key']
        else:
            load_dotenv()
            API_KEY = os.getenv("HOLDED_API_KEY")

        HEADERS["key"] = API_KEY
    finally:
        conn.close()


def get_setting(key, default=None):
    conn = get_db()
    try:
        cursor = _cursor(conn)
        cursor.execute(_q("SELECT value FROM settings WHERE key = ?"), (key,))
        row = cursor.fetchone()
        if row is None:
            return default
        return row[0] if _USE_SQLITE else row["value"]
    finally:
        conn.close()


def save_setting(key, value):
    conn = get_db()
    try:
        cursor = _cursor(conn)
        if _USE_SQLITE:
            cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        else:
            cursor.execute("""
                INSERT INTO settings (key, value) VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, (key, value))
        conn.commit()
    finally:
        conn.close()


# Initial load — always safe: SQLite file may not exist yet, PG may not be reachable
try:
    reload_config()
except Exception:
    pass  # DB not ready yet (first run before init_db)

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
                except: pass
    return 0

def init_db():
    conn = get_db()
    cursor = _cursor(conn)

    # Dialect-specific type tokens
    if _USE_SQLITE:
        _serial  = "INTEGER PRIMARY KEY AUTOINCREMENT"
        _real    = "REAL"
        _now     = "datetime('now')"
    else:
        _serial  = "SERIAL PRIMARY KEY"
        _real    = "NUMERIC"
        _now     = "NOW()"

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS invoices (
            id TEXT PRIMARY KEY,
            contact_id TEXT,
            contact_name TEXT,
            "desc" TEXT,
            date INTEGER,
            amount {_real},
            status INTEGER,
            payments_pending {_real} DEFAULT 0,
            payments_total {_real} DEFAULT 0,
            due_date INTEGER,
            doc_number TEXT
        )
    ''')

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS purchase_invoices (
            id TEXT PRIMARY KEY,
            contact_id TEXT,
            contact_name TEXT,
            "desc" TEXT,
            date INTEGER,
            amount {_real},
            status INTEGER,
            doc_number TEXT
        )
    ''')

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS estimates (
            id TEXT PRIMARY KEY,
            contact_id TEXT,
            contact_name TEXT,
            "desc" TEXT,
            date INTEGER,
            amount {_real},
            status INTEGER,
            doc_number TEXT
        )
    ''')

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY,
            name TEXT,
            "desc" TEXT,
            price {_real},
            stock {_real},
            sku TEXT
        )
    ''')

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS invoice_items (
            id {_serial},
            invoice_id TEXT,
            product_id TEXT,
            name TEXT,
            sku TEXT,
            units {_real},
            price {_real},
            subtotal {_real},
            discount {_real},
            tax {_real},
            retention {_real},
            account TEXT
        )
    ''')

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS estimate_items (
            id {_serial},
            estimate_id TEXT,
            product_id TEXT,
            name TEXT,
            sku TEXT,
            units {_real},
            price {_real},
            subtotal {_real},
            discount {_real},
            tax {_real},
            retention {_real},
            account TEXT
        )
    ''')

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS purchase_items (
            id {_serial},
            purchase_id TEXT,
            product_id TEXT,
            name TEXT,
            sku TEXT,
            units {_real},
            price {_real},
            subtotal {_real},
            discount {_real},
            tax {_real},
            retention {_real},
            account TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ledger_accounts (
            id TEXT PRIMARY KEY,
            name TEXT,
            num TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS contacts (
            id TEXT PRIMARY KEY,
            name TEXT,
            email TEXT,
            type TEXT,
            code TEXT,
            vat TEXT,
            phone TEXT,
            mobile TEXT
        )
    ''')

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT,
            "desc" TEXT,
            status TEXT,
            customer_id TEXT,
            budget {_real}
        )
    ''')

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS payments (
            id TEXT PRIMARY KEY,
            document_id TEXT,
            amount {_real},
            date INTEGER,
            method TEXT,
            type TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS amortizations (
            id {_serial},
            product_id TEXT NOT NULL,
            product_name TEXT NOT NULL,
            purchase_price {_real} NOT NULL,
            purchase_date TEXT NOT NULL,
            notes TEXT,
            product_type TEXT NOT NULL DEFAULT 'alquiler',
            created_at TEXT DEFAULT ({_now}),
            updated_at TEXT DEFAULT ({_now}),
            UNIQUE(product_id)
        )
    ''')

    # Fiscal rules per product type — drives IRPF logic across the whole app
    # irpf_pct: retention applied on invoices (15% services, 19% rentals, 0% sales/expenses)
    # is_expense: True for purchase-side concepts (gastos / suplidos)
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS product_type_rules (
            type_key TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            irpf_pct {_real} NOT NULL DEFAULT 0,
            is_expense INTEGER NOT NULL DEFAULT 0,
            description TEXT
        )
    ''')

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS purchase_analysis (
            id {_serial},
            purchase_id TEXT NOT NULL UNIQUE,
            category TEXT,
            subcategory TEXT,
            confidence TEXT,
            method TEXT,
            reasoning TEXT,
            analyzed_at TEXT DEFAULT ({_now})
        )
    ''')

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS inventory_matches (
            id {_serial},
            purchase_id TEXT NOT NULL,
            purchase_item_id INTEGER,
            product_id TEXT NOT NULL,
            product_name TEXT NOT NULL,
            matched_price {_real} NOT NULL,
            matched_date TEXT NOT NULL,
            match_method TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT ({_now}),
            UNIQUE(purchase_id, product_id)
        )
    ''')

    # ── amortization_purchases: many-to-many between amortizations and purchase sources
    # Each row represents one purchase invoice (or item) linked to one amortization product.
    # cost_override is the actual cost assigned to this product from that purchase —
    # it can differ from the invoice price (pack splits, kit allocations, etc.)
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS amortization_purchases (
            id {_serial},
            amortization_id INTEGER NOT NULL,
            purchase_id TEXT,
            purchase_item_id INTEGER,
            cost_override {_real} NOT NULL,
            allocation_note TEXT,
            created_at TEXT DEFAULT ({_now})
        )
    ''')

    # ── AI tables ────────────────────────────────────────────────────────────
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ai_history (
            id TEXT PRIMARY KEY,
            role TEXT,
            content TEXT,
            timestamp TEXT,
            conversation_id TEXT,
            tool_calls TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ai_favorites (
            id TEXT PRIMARY KEY,
            query TEXT,
            label TEXT,
            created_at TEXT
        )
    ''')

    # ── Sync log table (used by n8n flows) ───────────────────────────────────
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS sync_logs (
            id {_serial},
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT,
            duration_seconds {_real},
            counts TEXT
        )
    ''')

    # ── Column migrations (SQLite only — PG creates all columns upfront) ──────
    if _USE_SQLITE:
        for col, definition in [
            ("payments_pending", "REAL DEFAULT 0"),
            ("payments_total",   "REAL DEFAULT 0"),
            ("due_date",         "INTEGER"),
            ("doc_number",       "TEXT"),
        ]:
            try:
                cursor.execute(f"ALTER TABLE invoices ADD COLUMN {col} {definition}")
            except Exception:
                pass  # column already exists

        for tbl in ("purchase_invoices", "estimates"):
            try:
                cursor.execute(f"ALTER TABLE {tbl} ADD COLUMN doc_number TEXT")
            except Exception:
                pass

        try:
            cursor.execute("ALTER TABLE amortizations ADD COLUMN product_type TEXT NOT NULL DEFAULT 'alquiler'")
        except Exception:
            pass
    else:
        # PostgreSQL: ADD COLUMN IF NOT EXISTS (PG 9.6+)
        for stmt in [
            "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS payments_pending NUMERIC DEFAULT 0",
            "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS payments_total NUMERIC DEFAULT 0",
            "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS due_date INTEGER",
            "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS doc_number TEXT",
            "ALTER TABLE purchase_invoices ADD COLUMN IF NOT EXISTS doc_number TEXT",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS doc_number TEXT",
            "ALTER TABLE amortizations ADD COLUMN IF NOT EXISTS product_type TEXT NOT NULL DEFAULT 'alquiler'",
        ]:
            try:
                cursor.execute(stmt)
            except Exception:
                pass

    # ── Seed product_type_rules (never overwrites user edits) ────────────────
    default_rules = [
        ('alquiler', 'Alquiler',        19.0, 0, 'Equipamiento cedido en uso. IRPF retención 19%.'),
        ('venta',    'Venta',            0.0, 0, 'Venta de producto. Sin retención IRPF. Match 1-to-1 compra→venta.'),
        ('servicio', 'Servicio / Fee',  15.0, 0, 'Honorarios profesionales. IRPF retención 15%.'),
        ('gasto',    'Gasto',            0.0, 1, 'Gasto deducible / suplido. No genera ingreso directo.'),
    ]
    for rule in default_rules:
        if _USE_SQLITE:
            cursor.execute(
                "INSERT OR IGNORE INTO product_type_rules "
                "(type_key, label, irpf_pct, is_expense, description) VALUES (?,?,?,?,?)",
                rule
            )
        else:
            cursor.execute(
                "INSERT INTO product_type_rules "
                "(type_key, label, irpf_pct, is_expense, description) VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT (type_key) DO NOTHING",
                rule
            )

    # ── Data migration: populate amortization_purchases from legacy notes ─────
    # Only runs in SQLite mode (Supabase starts fresh, no legacy data to migrate)
    if _USE_SQLITE:
        cursor.execute('''
            INSERT OR IGNORE INTO amortization_purchases (amortization_id, purchase_id, cost_override, allocation_note)
            SELECT
                a.id,
                CASE
                    WHEN a.notes LIKE 'Auto-detected from purchase %'
                    THEN TRIM(SUBSTR(a.notes, 27, INSTR(SUBSTR(a.notes,27),' ')-1))
                    ELSE NULL
                END AS purchase_id,
                a.purchase_price,
                'Migrado automáticamente'
            FROM amortizations a
            WHERE a.id NOT IN (SELECT DISTINCT amortization_id FROM amortization_purchases)
              AND a.purchase_price > 0
        ''')

    # ── Indexes ───────────────────────────────────────────────────────────────
    for idx_sql in [
        'CREATE INDEX IF NOT EXISTS idx_invoices_contact ON invoices(contact_id)',
        'CREATE INDEX IF NOT EXISTS idx_invoices_date ON invoices(date)',
        'CREATE INDEX IF NOT EXISTS idx_purchases_contact ON purchase_invoices(contact_id)',
        'CREATE INDEX IF NOT EXISTS idx_purchases_date ON purchase_invoices(date)',
        'CREATE INDEX IF NOT EXISTS idx_inv_items_product ON invoice_items(product_id)',
        'CREATE INDEX IF NOT EXISTS idx_inv_items_invoice ON invoice_items(invoice_id)',
        'CREATE INDEX IF NOT EXISTS idx_pur_items_product ON purchase_items(product_id)',
        'CREATE INDEX IF NOT EXISTS idx_pur_items_purchase ON purchase_items(purchase_id)',
        'CREATE INDEX IF NOT EXISTS idx_ai_history_conv ON ai_history(conversation_id, timestamp)',
    ]:
        cursor.execute(idx_sql)

    conn.commit()
    conn.close()

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

def _row_val(row, key, idx):
    """Retrieve a value from a DB row that may be a dict (PG) or tuple (SQLite)."""
    if isinstance(row, dict):
        return row.get(key)
    return row[idx]


def _num(val):
    """Sanitize a value for a NUMERIC column: empty strings → None (NULL).
    PostgreSQL rejects empty strings in NUMERIC columns; SQLite accepts them silently."""
    if val is None or val == '':
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def sync_documents(doc_type, table, items_table, fk_column):
    logger.info(f"Syncing {doc_type}s (Historical)...")
    params = {"starttmp": 1262304000, "endtmp": int(time.time())}
    data = fetch_data(f"/invoicing/v1/documents/{doc_type}", params=params)

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
            if table == 'invoices':
                vals = (doc_id, item.get('contact'), item.get('contactName'), item.get('desc'),
                        item.get('date'), _num(item.get('total')), item.get('status'),
                        _num(item.get('paymentsPending', 0)), _num(item.get('paymentsTotal', 0)),
                        item.get('dueDate'), item.get('docNumber'))
                if _USE_SQLITE:
                    cursor.execute('''
                        INSERT OR REPLACE INTO invoices
                            (id, contact_id, contact_name, "desc", date, amount, status,
                             payments_pending, payments_total, due_date, doc_number)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ''', vals)
                else:
                    cursor.execute('''
                        INSERT INTO invoices
                            (id, contact_id, contact_name, "desc", date, amount, status,
                             payments_pending, payments_total, due_date, doc_number)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (id) DO UPDATE SET
                            contact_id=EXCLUDED.contact_id, contact_name=EXCLUDED.contact_name,
                            "desc"=EXCLUDED."desc", date=EXCLUDED.date, amount=EXCLUDED.amount,
                            status=EXCLUDED.status, payments_pending=EXCLUDED.payments_pending,
                            payments_total=EXCLUDED.payments_total, due_date=EXCLUDED.due_date,
                            doc_number=EXCLUDED.doc_number
                    ''', vals)
            else:
                vals = (doc_id, item.get('contact'), item.get('contactName'), item.get('desc'),
                        item.get('date'), _num(item.get('total')), item.get('status'), item.get('docNumber'))
                if _USE_SQLITE:
                    cursor.execute(f'''
                        INSERT OR REPLACE INTO {table}
                            (id, contact_id, contact_name, "desc", date, amount, status, doc_number)
                        VALUES (?,?,?,?,?,?,?,?)
                    ''', vals)
                else:
                    cursor.execute(f'''
                        INSERT INTO {table}
                            (id, contact_id, contact_name, "desc", date, amount, status, doc_number)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (id) DO UPDATE SET
                            contact_id=EXCLUDED.contact_id, contact_name=EXCLUDED.contact_name,
                            "desc"=EXCLUDED."desc", date=EXCLUDED.date, amount=EXCLUDED.amount,
                            status=EXCLUDED.status, doc_number=EXCLUDED.doc_number
                    ''', vals)

            # Items: delete + re-insert (simpler than upsert for SERIAL-keyed rows)
            cursor.execute(_q(f'DELETE FROM {items_table} WHERE {fk_column} = ?'), (doc_id,))
            for prod in item.get('products', []):
                retention = extract_ret(prod)
                acc_id = prod.get('accountCode') or prod.get('accountName') or prod.get('account')
                account = acc_map.get(acc_id, acc_id)
                cursor.execute(_q(f'''
                    INSERT INTO {items_table}
                        ({fk_column}, product_id, name, sku, units, price, subtotal, discount, tax, retention, account)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                '''), (doc_id, prod.get('productId'), prod.get('name'), prod.get('sku'),
                       _num(prod.get('units')), _num(prod.get('price')), _num(prod.get('subtotal')),
                       _num(prod.get('discount')), _num(prod.get('tax')), _num(retention), account))

        conn.commit()
    finally:
        conn.close()
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
        conn.close()
    logger.info(f"Synced {len(accounts)} ledger accounts.")


def sync_products():
    logger.info("Syncing Products (Inventory)...")
    data = fetch_data("/invoicing/v1/products")
    conn = get_db()
    try:
        cursor = _cursor(conn)
        for item in data:
            vals = (item.get('id'), item.get('name'), item.get('desc'),
                    _num(item.get('price')), _num(item.get('stock')), item.get('sku'))
            if _USE_SQLITE:
                cursor.execute('INSERT OR REPLACE INTO products (id, name, "desc", price, stock, sku) VALUES (?,?,?,?,?,?)', vals)
            else:
                cursor.execute("""
                    INSERT INTO products (id, name, "desc", price, stock, sku) VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO UPDATE SET
                        name=EXCLUDED.name, "desc"=EXCLUDED."desc", price=EXCLUDED.price,
                        stock=EXCLUDED.stock, sku=EXCLUDED.sku
                """, vals)
        conn.commit()
    finally:
        conn.close()
    logger.info(f"Synced {len(data)} products.")


def sync_contacts():
    logger.info("Syncing Contacts...")
    data = fetch_data("/invoicing/v1/contacts")
    conn = get_db()
    try:
        cursor = _cursor(conn)
        for item in data:
            vals = (item.get('id'), item.get('name'), item.get('email'), item.get('type'),
                    item.get('code'), item.get('vat'), item.get('phone'), item.get('mobile'))
            if _USE_SQLITE:
                cursor.execute("""
                    INSERT OR REPLACE INTO contacts (id, name, email, type, code, vat, phone, mobile)
                    VALUES (?,?,?,?,?,?,?,?)
                """, vals)
            else:
                cursor.execute("""
                    INSERT INTO contacts (id, name, email, type, code, vat, phone, mobile)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO UPDATE SET
                        name=EXCLUDED.name, email=EXCLUDED.email, type=EXCLUDED.type,
                        code=EXCLUDED.code, vat=EXCLUDED.vat, phone=EXCLUDED.phone,
                        mobile=EXCLUDED.mobile
                """, vals)
        conn.commit()
    finally:
        conn.close()
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
        conn.close()
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
        conn.close()
    logger.info(f"Synced {len(data)} payments.")

def post_data(endpoint, payload):
    if SAFE_MODE:
        logger.info(f"[SAFE MODE] Intercepted POST to {endpoint}")
        logger.debug(f"[SAFE MODE] Payload: {payload}")
        return {"status": 1, "id": "SAFE_MODE_ID_TEST", "info": "Dry run successful"}

    url = f"{BASE_URL}{endpoint}"
    response = requests.post(url, headers=HEADERS, json=payload)
    if response.status_code == 200:
        return response.json()
    else:
        logger.error(f"Error posting to {endpoint}: {response.status_code} - {response.text}")
        return None

def put_data(endpoint, payload):
    if SAFE_MODE:
        logger.info(f"[SAFE MODE] Intercepted PUT to {endpoint}")
        logger.debug(f"[SAFE MODE] Payload: {payload}")
        return {"status": 1, "info": "Dry run successful"}

    url = f"{BASE_URL}{endpoint}"
    response = requests.put(url, headers=HEADERS, json=payload)
    if response.status_code == 200:
        return response.json()
    else:
        logger.error(f"Error putting to {endpoint}: {response.status_code} - {response.text}")
        return None

def create_invoice(invoice_data):
    logger.info(f"Creating invoice for contact {invoice_data.get('contact')}...")
    result = post_data("/invoicing/v1/documents/invoice", invoice_data)
    if result and result.get('status') == 1:
        logger.info(f"Invoice created successfully: {result.get('id')}")
        return result.get('id')
    return None

def update_estimate(estimate_id, estimate_data):
    logger.info(f"Updating estimate {estimate_id}...")
    result = put_data(f"/invoicing/v1/documents/estimate/{estimate_id}", estimate_data)
    if result and result.get('status') == 1:
        logger.info(f"Estimate {estimate_id} updated successfully.")
        return True
    return False

def create_contact(contact_data):
    logger.info(f"Creating contact {contact_data.get('name')}...")
    result = post_data("/invoicing/v1/contacts", contact_data)
    if result and result.get('status') == 1:
        logger.info(f"Contact created successfully: {result.get('id')}")
        return result.get('id')
    return None

def create_estimate(estimate_data):
    logger.info(f"Creating estimate for contact {estimate_data.get('contact')}...")
    result = post_data("/invoicing/v1/documents/estimate", estimate_data)
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


def _fetch_one_val(cursor, key):
    """Fetch a single scalar value from a row, works for both dict and tuple cursors."""
    row = cursor.fetchone()
    if row is None:
        return None
    return row[key] if isinstance(row, dict) else row[0]


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
        conn.close()


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
        conn.close()


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
        conn.close()


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
        conn.close()


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
        conn.close()


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
        conn.close()


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
        conn.close()


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
        conn.close()


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
        conn.close()


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
        conn.close()


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
        conn.close()


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
        conn.close()


def get_product_type_rules() -> list:
    """Return all product type fiscal rules."""
    conn = get_db()
    try:
        cursor = _cursor(conn)
        cursor.execute("SELECT * FROM product_type_rules ORDER BY is_expense, irpf_pct DESC")
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def get_amortizations():
    """
    Return all amortizations with real-time revenue calculation from invoice_items.
    Revenue = SUM of (units * price) per invoice_item where product_id matches.
    Also returns product_type and its fiscal rule (irpf_pct).
    """
    conn = get_db()
    try:
        cursor = _cursor(conn)
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
                ), 0.0) AS total_revenue,
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
        rows = cursor.fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d['profit'] = d['total_revenue'] - d['purchase_price']
            d['roi_pct'] = round((d['profit'] / d['purchase_price'] * 100), 2) if d['purchase_price'] > 0 else 0
            d['status'] = 'AMORTIZADO' if d['profit'] >= 0 else 'EN CURSO'
            result.append(d)
        return result
    finally:
        conn.close()


def add_amortization(product_id, product_name, purchase_price, purchase_date,
                     notes="", product_type="alquiler"):
    """Add a product to amortization tracking. Returns new id or None if duplicate."""
    conn = get_db()
    try:
        cursor = _cursor(conn)
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
    except Exception:
        conn.rollback()
        return None  # likely duplicate product_id (UNIQUE constraint)
    finally:
        conn.close()


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
        conn.close()


def delete_amortization(amort_id):
    """Remove a product from amortization tracking."""
    conn = get_db()
    try:
        cursor = _cursor(conn)
        cursor.execute(_q("DELETE FROM amortizations WHERE id=?"), (amort_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()

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
    if not API_KEY:
        logger.error("HOLDED_API_KEY not found in .env file.")
    else:
        if SAFE_MODE:
            logger.warning("SAFE MODE (DRY RUN) ACTIVE - No data will be modified in Holded")

        init_db()
        sync_contacts()
        sync_products()
        sync_invoices()
        sync_estimates()
        sync_purchases()
        sync_projects()
        sync_payments()
        logger.info("Synchronization complete!")
