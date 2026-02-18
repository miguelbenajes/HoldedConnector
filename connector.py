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

DB_NAME = "holded.db"

def reload_config():
    global API_KEY, HEADERS
    conn = sqlite3.connect(DB_NAME)
    try:
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        cursor.execute("SELECT key, value FROM settings")
        rows = cursor.fetchall()
        settings = {row[0]: row[1] for row in rows}

        if 'holded_api_key' in settings:
            API_KEY = settings['holded_api_key']
        else:
            load_dotenv()
            API_KEY = os.getenv("HOLDED_API_KEY")

        HEADERS["key"] = API_KEY
    finally:
        conn.close()

def get_setting(key, default=None):
    conn = sqlite3.connect(DB_NAME)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row[0] if row else default
    finally:
        conn.close()

def save_setting(key, value):
    conn = sqlite3.connect(DB_NAME)
    try:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
    finally:
        conn.close()

# Initial load
if os.path.exists(DB_NAME):
    reload_config()

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
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS invoices (
            id TEXT PRIMARY KEY,
            contact_id TEXT,
            contact_name TEXT,
            desc TEXT,
            date INTEGER,
            amount REAL,
            status INTEGER
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS purchase_invoices (
            id TEXT PRIMARY KEY,
            contact_id TEXT,
            contact_name TEXT,
            desc TEXT,
            date INTEGER,
            amount REAL,
            status INTEGER
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS estimates (
            id TEXT PRIMARY KEY,
            contact_id TEXT,
            contact_name TEXT,
            desc TEXT,
            date INTEGER,
            amount REAL,
            status INTEGER
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY,
            name TEXT,
            desc TEXT,
            price REAL,
            stock REAL,
            sku TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS invoice_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id TEXT,
            product_id TEXT,
            name TEXT,
            sku TEXT,
            units REAL,
            price REAL,
            subtotal REAL,
            discount REAL,
            tax REAL,
            retention REAL,
            account TEXT,
            FOREIGN KEY (invoice_id) REFERENCES invoices (id),
            FOREIGN KEY (product_id) REFERENCES products (id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS estimate_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            estimate_id TEXT,
            product_id TEXT,
            name TEXT,
            sku TEXT,
            units REAL,
            price REAL,
            subtotal REAL,
            discount REAL,
            tax REAL,
            retention REAL,
            account TEXT,
            FOREIGN KEY (estimate_id) REFERENCES estimates (id),
            FOREIGN KEY (product_id) REFERENCES products (id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS purchase_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purchase_id TEXT,
            product_id TEXT,
            name TEXT,
            sku TEXT,
            units REAL,
            price REAL,
            subtotal REAL,
            discount REAL,
            tax REAL,
            retention REAL,
            account TEXT,
            FOREIGN KEY (purchase_id) REFERENCES purchase_invoices (id),
            FOREIGN KEY (product_id) REFERENCES products (id)
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

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT,
            desc TEXT,
            status TEXT,
            customer_id TEXT,
            budget REAL,
            FOREIGN KEY (customer_id) REFERENCES contacts (id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id TEXT PRIMARY KEY,
            document_id TEXT,
            amount REAL,
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

    cursor.execute('CREATE INDEX IF NOT EXISTS idx_invoices_contact ON invoices(contact_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_invoices_date ON invoices(date)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_purchases_contact ON purchase_invoices(contact_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_purchases_date ON purchase_invoices(date)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_inv_items_product ON invoice_items(product_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_inv_items_invoice ON invoice_items(invoice_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_pur_items_product ON purchase_items(product_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_pur_items_purchase ON purchase_items(purchase_id)')

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

def sync_documents(doc_type, table, items_table, fk_column):
    logger.info(f"Syncing {doc_type}s (Historical)...")
    params = {"starttmp": 1262304000, "endtmp": int(time.time())}
    data = fetch_data(f"/invoicing/v1/documents/{doc_type}", params=params)

    conn = sqlite3.connect(DB_NAME)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, num FROM ledger_accounts")
        acc_map = {row[0]: f"{row[1]} ({row[2]})" if row[2] else row[1] for row in cursor.fetchall()}

        for item in data:
            doc_id = item.get('id')
            cursor.execute(f'''
                INSERT OR REPLACE INTO {table} (id, contact_id, contact_name, desc, date, amount, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (doc_id, item.get('contact'), item.get('contactName'), item.get('desc'),
                  item.get('date'), item.get('total'), item.get('status')))

            cursor.execute(f'DELETE FROM {items_table} WHERE {fk_column} = ?', (doc_id,))
            for prod in item.get('products', []):
                retention = extract_ret(prod)
                acc_id = prod.get('accountCode') or prod.get('accountName') or prod.get('account')
                account = acc_map.get(acc_id, acc_id)

                cursor.execute(f'''
                    INSERT INTO {items_table} ({fk_column}, product_id, name, sku, units, price, subtotal, discount, tax, retention, account)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (doc_id, prod.get('productId'), prod.get('name'), prod.get('sku'),
                      prod.get('units'), prod.get('price'), prod.get('subtotal'),
                      prod.get('discount'), prod.get('tax'), retention, account))

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
    conn = sqlite3.connect(DB_NAME)
    try:
        cursor = conn.cursor()
        accounts = data if isinstance(data, list) else data.get('accounts', [])
        for item in accounts:
            cursor.execute('''
                INSERT OR REPLACE INTO ledger_accounts (id, name, num)
                VALUES (?, ?, ?)
            ''', (item.get('id'), item.get('name'), item.get('num')))
        conn.commit()
    finally:
        conn.close()
    logger.info(f"Synced {len(accounts)} ledger accounts.")

def sync_products():
    logger.info("Syncing Products (Inventory)...")
    data = fetch_data("/invoicing/v1/products")
    conn = sqlite3.connect(DB_NAME)
    try:
        cursor = conn.cursor()
        for item in data:
            cursor.execute('''
                INSERT OR REPLACE INTO products (id, name, desc, price, stock, sku)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (item.get('id'), item.get('name'), item.get('desc'), item.get('price'), item.get('stock'), item.get('sku')))
        conn.commit()
    finally:
        conn.close()
    logger.info(f"Synced {len(data)} products.")

def sync_contacts():
    logger.info("Syncing Contacts...")
    data = fetch_data("/invoicing/v1/contacts")
    conn = sqlite3.connect(DB_NAME)
    try:
        cursor = conn.cursor()
        for item in data:
            cursor.execute('''
                INSERT OR REPLACE INTO contacts (id, name, email, type, code, vat, phone, mobile)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (item.get('id'), item.get('name'), item.get('email'), item.get('type'), item.get('code'), item.get('vat'), item.get('phone'), item.get('mobile')))
        conn.commit()
    finally:
        conn.close()
    logger.info(f"Synced {len(data)} contacts.")

def sync_projects():
    logger.info("Syncing Projects...")
    data = fetch_data("/projects/v1/projects")
    conn = sqlite3.connect(DB_NAME)
    try:
        cursor = conn.cursor()
        for item in data:
            cursor.execute('''
                INSERT OR REPLACE INTO projects (id, name, desc, status, customer_id, budget)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (item.get('id'), item.get('name'), item.get('desc'), item.get('status'), item.get('customer'), item.get('budget')))
        conn.commit()
    finally:
        conn.close()
    logger.info(f"Synced {len(data)} projects.")

def sync_payments():
    logger.info("Syncing Payments...")
    data = fetch_data("/invoicing/v1/payments")
    conn = sqlite3.connect(DB_NAME)
    try:
        cursor = conn.cursor()
        for item in data:
            cursor.execute('''
                INSERT OR REPLACE INTO payments (id, document_id, amount, date, method, type)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (item.get('id'), item.get('documentId'), item.get('amount'), item.get('date'), item.get('paymentMethod'), item.get('type')))
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
