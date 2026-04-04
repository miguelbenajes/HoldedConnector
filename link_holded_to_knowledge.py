#!/usr/bin/env python3
"""
Interactive tool to map Holded products → knowledge.product_models.
Outputs mappings to holded_product_links table in Supabase.

Usage: /usr/bin/python3 link_holded_to_knowledge.py
"""

import os
import sys
import difflib
from dotenv import load_dotenv

load_dotenv()

import connector

# ── Supabase knowledge connection ──────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    print("ERROR: Set SUPABASE_URL and SUPABASE_SERVICE_KEY in .env")
    print("  SUPABASE_URL=https://mpgfivufawurjnpyvacf.supabase.co")
    print("  SUPABASE_SERVICE_KEY=eyJ...")
    sys.exit(1)

try:
    from supabase import create_client
    sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
except ImportError:
    print("ERROR: pip install supabase")
    sys.exit(1)


def fetch_holded_products():
    """Get all products from holded-connector DB."""
    conn = connector.get_db()
    cur = connector._cursor(conn)
    cur.execute('SELECT id, name, sku, kind, price FROM products ORDER BY name')
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def fetch_knowledge_products():
    """Get all canonical products from knowledge.product_models."""
    all_products = []
    offset = 0
    batch = 1000
    while True:
        resp = sb.schema("knowledge").from_("product_models").select(
            "id, name, slug"
        ).range(offset, offset + batch - 1).execute()
        if not resp.data:
            break
        all_products.extend(resp.data)
        if len(resp.data) < batch:
            break
        offset += batch
    return all_products


def fetch_existing_links():
    """Get already-linked holded_product_ids."""
    resp = sb.schema("knowledge").from_("holded_product_links").select(
        "holded_product_id"
    ).execute()
    return {r["holded_product_id"] for r in (resp.data or [])}


def find_candidates(holded_name, kb_products, n=8):
    """Fuzzy match holded product name against knowledge product names."""
    kb_names = [p["name"] for p in kb_products]
    matches = difflib.get_close_matches(holded_name, kb_names, n=n, cutoff=0.3)
    results = []
    for m in matches:
        for p in kb_products:
            if p["name"] == m:
                ratio = difflib.SequenceMatcher(None, holded_name.lower(), m.lower()).ratio()
                results.append({**p, "score": round(ratio * 100)})
                break
    return sorted(results, key=lambda x: -x["score"])


def insert_link(model_id, holded_product_id):
    """Insert a mapping into holded_product_links."""
    sb.schema("knowledge").from_("holded_product_links").insert({
        "model_id": model_id,
        "holded_product_id": holded_product_id,
    }).execute()


def main():
    print("=" * 60)
    print("  Holded → Knowledge Product Linker")
    print("=" * 60)

    print("\nLoading Holded products...")
    holded = fetch_holded_products()
    print(f"  {len(holded)} products")

    print("Loading knowledge products...")
    kb = fetch_knowledge_products()
    print(f"  {len(kb)} canonical products")

    existing = fetch_existing_links()
    print(f"  {len(existing)} already linked\n")

    # Filter out already-linked and packs (packs are combos, not single products)
    todo = [p for p in holded if p["id"] not in existing and p["kind"] == "simple"]
    packs = [p for p in holded if p["id"] not in existing and p["kind"] == "pack"]

    print(f"  {len(todo)} simple products to link")
    print(f"  {len(packs)} packs skipped (link individually later)")
    print(f"\nCommands: number to select, [s]kip, [q]uit, [a]uto (auto-link ≥80%)")
    print("-" * 60)

    linked = 0
    skipped = 0
    auto_mode = False

    for i, prod in enumerate(todo):
        candidates = find_candidates(prod["name"], kb)

        if not candidates:
            print(f"\n[{i+1}/{len(todo)}] {prod['name']}")
            print("  No candidates found. Skipping.")
            skipped += 1
            continue

        best = candidates[0]

        # Auto mode: link if score ≥ 80%
        if auto_mode and best["score"] >= 80:
            try:
                insert_link(best["id"], prod["id"])
                linked += 1
                print(f"  AUTO: {prod['name']} → {best['name']} ({best['score']}%)")
            except Exception as e:
                print(f"  AUTO SKIP (conflict): {e}")
            continue

        # Interactive mode
        print(f"\n[{i+1}/{len(todo)}] HOLDED: {prod['name']}")
        if prod["price"]:
            print(f"  Price: €{prod['price']}/day")
        print(f"  Candidates:")
        for j, c in enumerate(candidates):
            marker = " ★" if c["score"] >= 80 else ""
            print(f"    {j+1}. [{c['score']}%] {c['name']}{marker}")

        choice = input("  → ").strip().lower()

        if choice == "q":
            print(f"\nDone. Linked: {linked}, Skipped: {skipped}")
            return
        elif choice == "s" or choice == "":
            skipped += 1
            continue
        elif choice == "a":
            auto_mode = True
            # Process current one too
            if best["score"] >= 80:
                try:
                    insert_link(best["id"], prod["id"])
                    linked += 1
                    print(f"  AUTO: → {best['name']} ({best['score']}%)")
                except Exception as e:
                    print(f"  AUTO SKIP: {e}")
            else:
                skipped += 1
            continue
        else:
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(candidates):
                    selected = candidates[idx]
                    try:
                        insert_link(selected["id"], prod["id"])
                        linked += 1
                        print(f"  ✓ Linked → {selected['name']}")
                    except Exception as e:
                        print(f"  ERROR: {e}")
                else:
                    print("  Invalid number, skipping.")
                    skipped += 1
            except ValueError:
                print("  Invalid input, skipping.")
                skipped += 1

    print(f"\n{'=' * 60}")
    print(f"  DONE. Linked: {linked}, Skipped: {skipped}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
