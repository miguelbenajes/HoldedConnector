#!/usr/bin/env python3
"""
migrate_amortizations.py
------------------------
Migrates amortizations + amortization_purchases from local SQLite (holded.db)
to Supabase (PostgreSQL) via connector.py.

Run once:
    /usr/bin/python3 migrate_amortizations.py

Requirements:
    - DATABASE_URL must be set in .env (pointing to Supabase)
    - holded.db must exist in the same directory
"""

import os
import sys
import sqlite3

# Load .env manually (no external deps needed)
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())

import connector  # noqa: E402 — must load after .env

SQLITE_PATH = os.path.join(os.path.dirname(__file__), "holded.db")


def migrate():
    if not os.path.exists(SQLITE_PATH):
        print(f"ERROR: SQLite database not found at {SQLITE_PATH}")
        sys.exit(1)

    if not os.getenv("DATABASE_URL"):
        print("ERROR: DATABASE_URL is not set — Supabase not configured")
        sys.exit(1)

    print(f"Source : SQLite  → {SQLITE_PATH}")
    print(f"Target : Supabase → {os.getenv('DATABASE_URL')[:60]}...")
    print()

    # ── 1. Read from SQLite ──────────────────────────────────────────────────
    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    sqlite_conn.row_factory = sqlite3.Row
    sc = sqlite_conn.cursor()

    sc.execute("SELECT * FROM amortizations ORDER BY id")
    amortizations = sc.fetchall()

    sc.execute("SELECT * FROM amortization_purchases ORDER BY id")
    purchases = sc.fetchall()

    sqlite_conn.close()

    print(f"Found in SQLite: {len(amortizations)} amortizations, {len(purchases)} purchase links")

    if not amortizations:
        print("Nothing to migrate.")
        return

    # ── 2. Connect to Supabase ───────────────────────────────────────────────
    pg_conn = connector.get_db()
    cur = connector._cursor(pg_conn)

    amort_migrated = 0
    purchase_migrated = 0
    amort_skipped = 0
    purchase_skipped = 0

    # Maps old SQLite amortization.id → new PostgreSQL amortization.id
    id_map = {}

    # ── 3. Migrate amortizations ─────────────────────────────────────────────
    print("\nMigrating amortizations...")
    for row in amortizations:
        old_id = row["id"]
        product_id   = row["product_id"]
        product_name = row["product_name"]
        purchase_price = row["purchase_price"]
        purchase_date  = row["purchase_date"]
        notes          = row["notes"]
        product_type   = row["product_type"] or "alquiler"
        created_at     = row["created_at"]

        # INSERT … ON CONFLICT (product_id) DO NOTHING, then SELECT to get id
        cur.execute(
            """
            INSERT INTO amortizations
                (product_id, product_name, purchase_price, purchase_date,
                 notes, product_type, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (product_id) DO NOTHING
            RETURNING id
            """,
            (
                product_id,
                product_name,
                connector._num(purchase_price),
                purchase_date,
                notes,
                product_type,
                created_at or "NOW()",
                created_at or "NOW()",
            ),
        )
        result = cur.fetchone()

        if result:
            new_id = result["id"] if hasattr(result, "keys") else result[0]
            id_map[old_id] = new_id
            amort_migrated += 1
            print(f"  ✓ [{old_id}→{new_id}] {product_name[:50]}")
        else:
            # Row already existed — fetch existing id for purchase link mapping
            cur.execute(
                "SELECT id FROM amortizations WHERE product_id = %s",
                (product_id,),
            )
            existing = cur.fetchone()
            if existing:
                new_id = existing["id"] if hasattr(existing, "keys") else existing[0]
                id_map[old_id] = new_id
            amort_skipped += 1
            print(f"  ~ [{old_id}→{id_map.get(old_id,'?')}] SKIPPED (already exists): {product_name[:50]}")

    pg_conn.commit()

    # ── 4. Migrate amortization_purchases ────────────────────────────────────
    print("\nMigrating purchase links...")
    for row in purchases:
        old_amort_id = row["amortization_id"]
        new_amort_id = id_map.get(old_amort_id)

        if new_amort_id is None:
            print(f"  ! Skipping purchase link id={row['id']} — parent amortization id={old_amort_id} not mapped")
            purchase_skipped += 1
            continue

        purchase_id      = row["purchase_id"]
        # purchase_item_id is SQLite-specific SERIAL — not portable to PostgreSQL
        cost_override    = row["cost_override"]
        allocation_note  = row["allocation_note"]
        created_at       = row["created_at"]

        cur.execute(
            """
            INSERT INTO amortization_purchases
                (amortization_id, purchase_id, purchase_item_id,
                 cost_override, allocation_note, created_at)
            VALUES (%s, %s, NULL, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                new_amort_id,
                purchase_id,
                connector._num(cost_override),
                allocation_note,
                created_at or "NOW()",
            ),
        )
        affected = cur.rowcount
        if affected:
            purchase_migrated += 1
            print(f"  ✓ amortization_id={new_amort_id} ← purchase {purchase_id or '(manual)'} cost={cost_override}")
        else:
            purchase_skipped += 1
            print(f"  ~ SKIPPED (already exists): amortization_id={new_amort_id} purchase={purchase_id}")

    pg_conn.commit()

    # ── 5. Summary ───────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"Amortizations : {amort_migrated} migrated, {amort_skipped} skipped")
    print(f"Purchase links: {purchase_migrated} migrated, {purchase_skipped} skipped")

    # Verify final counts in PostgreSQL
    cur.execute("SELECT COUNT(*) AS c FROM amortizations")
    pg_amort = connector._fetch_one_val(cur, "c")
    cur.execute("SELECT COUNT(*) AS c FROM amortization_purchases")
    pg_purch = connector._fetch_one_val(cur, "c")

    print(f"\nSupabase now has: {pg_amort} amortizations, {pg_purch} purchase links")
    pg_conn.close()
    print("\nDone ✓")


if __name__ == "__main__":
    migrate()
