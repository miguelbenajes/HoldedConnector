#!/usr/bin/env python3
"""
backfill_packs.py — One-time migration to populate pack_components,
set products.kind, and migrate pack amortizations to component amortizations.

Usage:
    /usr/bin/python3 backfill_packs.py          # dry-run (default)
    /usr/bin/python3 backfill_packs.py --apply   # actually write to DB

What it does:
1. Fetches all products from Holded API → sets `kind` (simple/pack) on each
2. Parses `packItems` → populates `pack_components` table
3. For each pack amortization:
   - Finds component products via pack_components
   - If component already has an amortization → adds cost via amortization_purchases
   - If component has NO amortization → creates one with allocated cost
   - Deletes the pack amortization after migrating
"""

import sys
import os
import logging

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import connector
from connector import get_db, _cursor, _q, _num, _USE_SQLITE

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DRY_RUN = "--apply" not in sys.argv


def main():
    if DRY_RUN:
        logger.info("=== DRY RUN MODE (pass --apply to write) ===")
    else:
        logger.info("=== APPLY MODE — changes will be written ===")

    # Ensure schema is up to date
    connector.init_db()

    # ── Step 1: Fetch products from Holded API and update kind + pack_components ─
    logger.info("Step 1: Fetching products from Holded API...")
    data = connector.fetch_data("/invoicing/v1/products")
    logger.info(f"  Fetched {len(data)} products from API")

    packs = [p for p in data if (p.get('kind') or 'simple') == 'pack']
    simples = [p for p in data if (p.get('kind') or 'simple') != 'pack']
    logger.info(f"  Breakdown: {len(simples)} simple, {len(packs)} packs")

    conn = get_db()
    try:
        cursor = _cursor(conn)

        # Update kind on all products
        pack_count = 0
        for item in data:
            kind = item.get('kind', 'simple') or 'simple'
            pid = item.get('id')
            if _USE_SQLITE:
                cursor.execute("UPDATE products SET kind=? WHERE id=?", (kind, pid))
            else:
                cursor.execute("UPDATE products SET kind=%s WHERE id=%s", (kind, pid))
            if kind == 'pack':
                pack_count += 1
        logger.info(f"  Updated kind on {len(data)} products ({pack_count} packs)")

        # Populate pack_components
        cursor.execute("DELETE FROM pack_components")
        comp_count = 0
        pack_details = {}  # pack_id → [(component_id, qty), ...]
        for item in packs:
            pack_id = item.get('id')
            pack_name = item.get('name', '?')
            components = []
            for pi in (item.get('packItems') or []):
                raw_pid = pi.get('pid', '')
                component_id = raw_pid.split('#')[0] if '#' in raw_pid else raw_pid
                qty = _num(pi.get('units', 1)) or 1
                if component_id:
                    if _USE_SQLITE:
                        cursor.execute("INSERT INTO pack_components (pack_id, component_id, quantity) VALUES (?,?,?)",
                                       (pack_id, component_id, qty))
                    else:
                        cursor.execute("INSERT INTO pack_components (pack_id, component_id, quantity) VALUES (%s,%s,%s)",
                                       (pack_id, component_id, qty))
                    comp_count += 1
                    components.append((component_id, qty))
            pack_details[pack_id] = {"name": pack_name, "components": components}
        logger.info(f"  Inserted {comp_count} pack_components rows from {len(packs)} packs")

        # ── Step 2: Find pack amortizations ──────────────────────────────────────
        logger.info("\nStep 2: Finding pack amortizations...")
        cursor.execute(_q('''
            SELECT a.id, a.product_id, a.product_name, a.purchase_price,
                   a.purchase_date, a.notes, a.product_type
            FROM amortizations a
            JOIN products p ON p.id = a.product_id
            WHERE p.kind = 'pack'
        '''))
        pack_amorts = [dict(r) for r in cursor.fetchall()]
        logger.info(f"  Found {len(pack_amorts)} pack amortizations to migrate")

        if not pack_amorts:
            logger.info("  Nothing to migrate!")
            if not DRY_RUN:
                conn.commit()
            return

        # ── Step 3: Migrate each pack amortization ───────────────────────────────
        logger.info("\nStep 3: Migrating pack amortizations to components...")
        migrated = 0
        created = 0
        cost_transfers = 0

        for pa in pack_amorts:
            pack_id = pa['product_id']
            info = pack_details.get(pack_id, {"name": pa['product_name'], "components": []})
            components = info["components"]

            if not components:
                logger.warning(f"  SKIP: Pack '{pa['product_name']}' has no components in API data")
                continue

            pa['purchase_price'] = float(pa['purchase_price'] or 0)
            logger.info(f"\n  Migrating: {pa['product_name']} (amort #{pa['id']}, cost €{pa['purchase_price']})")
            logger.info(f"    Components: {len(components)}")

            # Get component prices for proportional cost allocation
            comp_prices = {}
            for comp_id, qty in components:
                cursor.execute(_q("SELECT name, price FROM products WHERE id = ?"), (comp_id,))
                row = cursor.fetchone()
                if row:
                    row = dict(row)
                    comp_prices[comp_id] = {"name": row["name"], "price": float(row["price"] or 0), "qty": float(qty)}
                else:
                    comp_prices[comp_id] = {"name": f"Unknown ({comp_id[:8]}...)", "price": 0.0, "qty": float(qty)}

            total_value = sum(c["price"] * c["qty"] for c in comp_prices.values())

            for comp_id, qty in components:
                cp = comp_prices[comp_id]

                # Calculate this component's share of the pack purchase cost
                if total_value > 0:
                    share = (cp["price"] * cp["qty"]) / total_value
                else:
                    share = 1.0 / len(components)
                allocated_cost = round(pa['purchase_price'] * share, 2)

                logger.info(f"    → {cp['name']}: share={share:.1%}, allocated €{allocated_cost}")

                # Check if component already has an amortization
                cursor.execute(_q("SELECT id FROM amortizations WHERE product_id = ?"), (comp_id,))
                existing = cursor.fetchone()

                if existing:
                    existing_id = existing['id'] if isinstance(existing, dict) else existing[0]
                    logger.info(f"      EXISTS (amort #{existing_id}) — adding cost source")
                    if not DRY_RUN:
                        if _USE_SQLITE:
                            cursor.execute('''
                                INSERT INTO amortization_purchases
                                    (amortization_id, cost_override, allocation_note)
                                VALUES (?,?,?)
                            ''', (existing_id, allocated_cost,
                                  f"Migrated from pack '{pa['product_name']}' (share {share:.0%})"))
                        else:
                            cursor.execute('''
                                INSERT INTO amortization_purchases
                                    (amortization_id, cost_override, allocation_note)
                                VALUES (%s,%s,%s)
                            ''', (existing_id, allocated_cost,
                                  f"Migrated from pack '{pa['product_name']}' (share {share:.0%})"))
                        # Recalc purchase_price from amortization_purchases
                        connector._recalc_purchase_price(cursor, existing_id)
                    cost_transfers += 1
                else:
                    logger.info(f"      NEW — creating amortization")
                    if not DRY_RUN:
                        if _USE_SQLITE:
                            cursor.execute('''
                                INSERT INTO amortizations
                                    (product_id, product_name, purchase_price, purchase_date, notes, product_type)
                                VALUES (?,?,?,?,?,?)
                            ''', (comp_id, cp["name"], allocated_cost, pa['purchase_date'],
                                  f"From pack '{pa['product_name']}'", pa['product_type']))
                        else:
                            cursor.execute('''
                                INSERT INTO amortizations
                                    (product_id, product_name, purchase_price, purchase_date, notes, product_type)
                                VALUES (%s,%s,%s,%s,%s,%s)
                                RETURNING id
                            ''', (comp_id, cp["name"], allocated_cost, pa['purchase_date'],
                                  f"From pack '{pa['product_name']}'", pa['product_type']))
                    created += 1

            # Delete the pack amortization
            logger.info(f"    DELETE pack amortization #{pa['id']}")
            if not DRY_RUN:
                cursor.execute(_q("DELETE FROM amortization_purchases WHERE amortization_id = ?"), (pa['id'],))
                cursor.execute(_q("DELETE FROM amortizations WHERE id = ?"), (pa['id'],))
            migrated += 1

        if not DRY_RUN:
            conn.commit()

        # ── Summary ──────────────────────────────────────────────────────────────
        logger.info(f"\n{'=' * 60}")
        logger.info(f"MIGRATION {'COMPLETE' if not DRY_RUN else 'PREVIEW (dry run)'}")
        logger.info(f"  Packs populated:          {len(packs)}")
        logger.info(f"  Pack components stored:    {comp_count}")
        logger.info(f"  Pack amortizations found:  {len(pack_amorts)}")
        logger.info(f"  → Migrated to components:  {migrated}")
        logger.info(f"  → New amortizations:       {created}")
        logger.info(f"  → Cost transfers:          {cost_transfers}")
        if DRY_RUN:
            logger.info(f"\n  Run with --apply to execute these changes.")
        logger.info(f"{'=' * 60}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
