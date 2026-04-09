"""Purchase analysis — rule-based and AI-assisted categorization of expenses.

Categories mirror the folder structure in:
~/Documents/MIGUEL/WORK/CONTABILIDAD/MODELO Soportadas

Add new rules to CATEGORY_RULES when you create new supplier folders.
"""

import logging

from app.db.connection import get_db, release_db, _cursor, _q, _num, _row_val, _fetch_one_val, _USE_SQLITE

logger = logging.getLogger(__name__)


# Category keywords for rule-based classification.
# Each entry: (category, subcategory, [keywords_to_match_in_desc_or_supplier])
# Keywords are matched case-insensitively against: purchase desc + contact_name + item names
CATEGORY_RULES = [
    # ── AIRBNB RECIBOS ──────────────────────────────────────
    ("AIRBNB RECIBOS",       "Airbnb",         ["airbnb"]),

    # ── AMAZON ──────────────────────────────────────────────
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
    ("SOFTWARE",             "Suscripción",    ["microsoft", "dropbox", "notion", "slack",
                                                "fastspring", "github", "figma", "canva",
                                                "cloudflare", "digitalocean", "vercel",
                                                "suscripcion", "subscription"]),

    # ── TELEFONIA E INTERNET ────────────────────────────────
    ("TELEFONIA E INTERNET", "Digi",           ["digi spain", "digi telecom"]),
    ("TELEFONIA E INTERNET", "finetwork",      ["finetwork"]),
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
    ("TRANSPORTE",           "Mensajería",     ["correos", "fedex", "ups", "mrw",
                                                "seur", "nacex", "envio paquete"]),

    # ── A AMORTIZAR ─────────────────────────────────────────
    ("A AMORTIZAR",          "Equipamiento",   ["fnac", "mediamarkt", "pccomponentes",
                                                "bambulab", "bambu lab", "anker",
                                                "llumm", "fotopro", "photospecialist",
                                                "kamera express", "leroy merlin", "leroy",
                                                "bauhaus", "ikea"]),

    # ── VARIOS ──────────────────────────────────────────────
    ("VARIOS",               "Restaurante",    ["restaurante", "cafeteria", "comida",
                                                "cena", "almuerzo", "glovo", "just eat",
                                                "uber eats"]),
    ("VARIOS",               "Formación",      ["formacion", "curso", "training",
                                                "udemy", "coursera", "master"]),
    ("VARIOS",               "Varios",         ["varios", "material oficina", "papeleria"]),
]


def categorize_by_rules(desc: str, contact_name: str, item_names: list):
    """Try to categorize a purchase invoice using keyword rules.
    Returns dict with category/subcategory/confidence='high' or None if no match."""
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
