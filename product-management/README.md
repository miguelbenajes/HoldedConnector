# Product Management Suite

Self-contained toolset for managing product classification, linking, and import workflows.

## Overview

This folder contains scripts and utilities for:
- **Classifying** unmatched products into categories (Real Products, Services, Expenses, Administrative)
- **Linking** products to existing inventory
- **Generating** import files for manual entry and Holded bulk import
- **Learning** classification patterns for future automation

---

## Workflow

### Phase 1: Identify Unmatched Products
```bash
# From project root:
python3 inventory_matcher.py
# Output: products_to_import.xlsx (MATCHED + NOT_MATCHED sheets)
```

### Phase 2: Classify Unmatched Items
```bash
cd product-management
python3 classify_products.py
# Output: products_classified.xlsx (Real/Service/Expense/Admin)
```

### Phase 3: Generate Import File
```bash
python3 generate_products_for_import.py
# Output: products_for_import.xlsx (5 sheets: Real/Expenses/Services/Linked/Reference)
```

### Phase 4: Review & Apply Corrections
Edit `products_for_import.xlsx` to:
- Fill in missing data (SKU, Price, Category, etc.)
- Mark decisions for Administrative items
- Mark any reclassifications

Then apply:
```bash
python3 apply_product_corrections.py
# Updates: products_for_import.xlsx with corrected classifications
# Creates: Amortizations in Supabase for linked products
```

---

## Files

### Scripts

| Script | Purpose | Input | Output |
|--------|---------|-------|--------|
| `classify_products.py` | Categorize 307 NOT_MATCHED products | `products_to_import.xlsx` | `products_classified.xlsx` |
| `match_expenses_to_inventory.py` | Fuzzy match expenses to existing products | `products_classified.xlsx` | `products_final_review.xlsx` |
| `generate_products_for_import.py` | Generate master Excel for import | `products_classified.xlsx` | `products_for_import.xlsx` |
| `apply_product_corrections.py` | Apply user decisions & link to inventory | `products_for_import.xlsx` | Updated `products_for_import.xlsx` + Supabase updates |

### Data Files

| File | Purpose | Status |
|------|---------|--------|
| `products_classified.xlsx` | Classification by category (Real/Service/Expense/Admin) | Output from Phase 2 |
| `products_final_review.xlsx` | Expenses matched to inventory (0 matches) | Output from Phase 2 |
| `products_for_import.xlsx` | Master file with all corrections applied | Ready for Holded import |
| `product_mappings.yaml` | Learned classification rules & patterns | Reference for automation |

### Reference

| File | Purpose |
|------|---------|
| `product_mappings.yaml` | Classification rules, keywords, linking patterns, automation opportunities |

---

## Classification Categories

### Real Products (178)
Physical inventory items: equipment, materials, tools, hardware.
- **Fields**: SKU, Category, Price, Stock, Description
- **Action**: Create in Holded inventory

### Expenses (67)
Operating costs: taxi, parking, food, transport.
- **Fields**: Project ID (optional), Amount, Date, Vendor
- **Action**: Create in Holded as 'expense' type

### Services/Fees (42)
Time-based labor/consulting.
- **Fields**: Hours, Date, Rate (€/hr), Total
- **Action**: Create in Holded as 'fee' type

### Linked to Inventory (1)
Items matched to existing products.
- **Example**: ALQ MACBOOK → Macbook Pro Max M3 (amortization created)
- **Action**: Already in Supabase; ready to track ROI

---

## Learning File: product_mappings.yaml

Documents patterns learned from user decisions to enable smarter automation:

```yaml
classification_rules:
  real_products:
    keywords: ["adapter", "camera", "lens", "battery", ...]
  services:
    keywords: ["session", "consultation", "labor", ...]
  expenses:
    keywords: ["taxi", "parking", "petrol", "dinner", ...]

product_links:
  "ALQ MACBOOK":
    maps_to: "Macbook Pro Max M3"
    reasoning: "Equipment rental tracking"

automation_opportunities:
  - pattern: "ALQ .+"
    action: "Match to existing inventory"
    confidence: "Medium"
  - pattern: "Dinner|Taxi|Parking"
    action: "Classify as Expense"
    confidence: "High"
```

---

## Quick Start

1. **Place in root after running inventory_matcher.py:**
   ```bash
   cd product-management
   python3 classify_products.py
   ```

2. **Review classification results:**
   - Open `products_classified.xlsx`
   - Check categorization in each sheet

3. **Generate import file:**
   ```bash
   python3 generate_products_for_import.py
   ```

4. **Edit in Excel:**
   - Fill in missing fields (SKU, Price, Category, etc.)
   - Review any items marked for decision
   - Save

5. **Apply corrections:**
   ```bash
   python3 apply_product_corrections.py
   ```

6. **Create in Holded:**
   - Use the corrected `products_for_import.xlsx`
   - Import each sheet (Real Products, Expenses, Services) to Holded

---

## Notes

- All scripts use `connector.py` for database access (Supabase or SQLite)
- Product mappings are learned and stored in `product_mappings.yaml`
- OT (Overtime) detection is built in for service entries
- String trimming matters: `'text '` ≠ `'text'` (handled in fuzzy matching)
- ON CONFLICT handles duplicates gracefully in bulk inserts

---

## Related Files (Root)

- `inventory_matcher.py` — Phase 1 (identify unmatched products)
- `link_matched_products.py` — Link matched products to invoice_items
- `migrate_amortizations.py` — Migrate amortizations SQLite → Supabase
- `connector.py` — Database abstraction layer (required for all scripts)
