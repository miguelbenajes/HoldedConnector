"""Inventory matching — fuzzy-match purchase items to catalog products.

Scans purchase_items to find equipment purchases that correspond to
products in the catalog, enabling ROI tracking via amortizations.
"""

import re
import logging
from difflib import SequenceMatcher
from datetime import datetime

from app.db.connection import get_db, release_db, _cursor, _q, _fetch_one_val, _USE_SQLITE

logger = logging.getLogger(__name__)


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


def find_inventory_in_purchases() -> list:
    """Exhaustive scan of purchase_items to find inventory products.

    Matching strategy (per product, looking for best item):
      1. Exact: purchase_item.product_id == product.id  (Holded-linked)
      2. Substring: cleaned product name contained in cleaned item name
      3. Token overlap: >=75% of product name tokens found in item name
      4. Fuzzy: SequenceMatcher ratio >= 0.72 on cleaned names

    Skips products already in amortizations or inventory_matches (any status).
    Iterates by product to guarantee one best match per product.
    """
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
    """Confirm or reject a pending inventory match.
    If confirmed: upserts amortization, adds purchase link, recalculates price."""
    from app.domain.amortization import _recalc_purchase_price

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
