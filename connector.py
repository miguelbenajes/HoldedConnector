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


# ── Holded-specific constants + HTTP client (canonical: app.holded.client) ───
from app.holded.client import (
    fetch_data,
    post_data,
    put_data,
    delete_data,
    holded_put,
    extract_ret,
    _extract_project_code,
    _extract_shooting_dates,
    PROYECTO_PRODUCT_ID,
    PROYECTO_PRODUCT_NAME,
    SHOOTING_DATES_PRODUCT_ID,
    SHOOTING_DATES_PRODUCT_NAME,
)

# ═══════════════════════════════════════════════════════════════════════════════
# Business logic (Holded API sync, data access, amortizations, analysis)
# ═══════════════════════════════════════════════════════════════════════════════

# ── Bulk sync functions (canonical: app.holded.sync) ────────────────────────
from app.holded.sync import (
    sync_documents,
    sync_invoices,
    sync_purchases,
    sync_estimates,
    sync_accounts,
    sync_products,
    sync_contacts,
    sync_projects,
    sync_payments,
)

# ---------------------------------------------------------------------------
# Single-entity sync helpers — canonical: app.holded.sync_single
# ---------------------------------------------------------------------------
from app.holded.sync_single import sync_single_document, sync_single_contact, sync_single_product


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
                    item = {
                        "name": it.get("name", ""),
                        "units": _num(it.get("units")) or 1,
                        "subtotal": _num(it.get("price")) or _num(it.get("subtotal")) or 0,
                        "price": _num(it.get("price")) or _num(it.get("subtotal")) or 0,
                        "desc": it.get("desc", ""),
                        "productId": it.get("productId", ""),
                        "product_id": it.get("productId", ""),
                    }
                    if it.get("tax") is not None:
                        item["tax"] = it["tax"]
                    if it.get("taxes"):
                        item["taxes"] = it["taxes"]
                    if it.get("retention"):
                        item["retention"] = it["retention"]
                    if it.get("discount"):
                        item["discount"] = it["discount"]
                    if it.get("account"):
                        item["account"] = it["account"]
                    items.append(item)
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
