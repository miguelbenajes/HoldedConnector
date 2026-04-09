"""Amortization tracking — ROI calculation for rental equipment.

Tracks purchase costs vs. invoice revenue for each product, including
proportional revenue attribution from pack products.
"""

import logging

from app.db.connection import get_db, release_db, _cursor, _q, _fetch_one_val, _USE_SQLITE

logger = logging.getLogger(__name__)


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
    """Return all amortizations with real-time revenue calculation from invoice_items.
    Revenue includes both direct revenue (product_id match) and pack revenue
    (attributed proportionally from packs that contain the component product).
    Also returns product_type and its fiscal rule (irpf_pct)."""
    conn = get_db()
    try:
        cursor = _cursor(conn)

        # 1. Direct revenue per amortization
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
