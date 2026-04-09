"""DB upsert logic for Holded entities. Handles SQLite vs PostgreSQL dialects.

Single-entity upsert helpers used by:
  - sync_single_document / sync_single_contact / sync_single_product (sync-back after writes)
  - sync.py bulk sync helpers for contacts and products

Each function takes a cursor — callers manage connection lifecycle.

Extracted from connector.py during Fase 5a refactor.
"""
import json
import logging
from app.db.connection import _q, _num, _USE_SQLITE, _row_val
from app.holded.client import (
    extract_ret,
    _extract_project_code,
    _extract_shooting_dates,
)

logger = logging.getLogger(__name__)


def _upsert_single_document(cursor, doc, table, items_table, fk_column):
    """Upsert a single document + its line items into local DB.

    Reuses the same field mapping and SQL patterns as sync_documents().
    `doc` is a single Holded document dict (from GET /documents/{type}/{id}).

    IMPORTANT: Invoices use 13 columns (includes payments_pending, payments_total,
    due_date). Estimates and purchases use only 10 columns (no payment fields).
    This mirrors the branching in sync_documents() at line 708.
    """
    doc_id = doc.get('id')
    if not doc_id:
        return

    # Build ledger account map for human-readable account names (like sync_documents lines 697-702)
    acc_map = {}
    try:
        cursor.execute(_q('SELECT id, name, num FROM ledger_accounts'))
        for r in cursor.fetchall():
            name = r[1] if isinstance(r, tuple) else r['name']
            num = r[2] if isinstance(r, tuple) else r.get('num')
            rid = r[0] if isinstance(r, tuple) else r['id']
            acc_map[rid] = f"{name} ({num})" if num else name
    except Exception:
        pass  # No ledger accounts loaded yet — fall through to raw ID

    is_invoice = (table == 'invoices')

    if is_invoice:
        # Invoices: 13 columns including payment fields
        vals = (
            doc_id,
            doc.get('contact'),
            doc.get('contactName'),
            doc.get('desc', ''),
            doc.get('date'),
            _num(doc.get('total')),
            doc.get('status', 0),
            _num(doc.get('paymentsPending')),
            _num(doc.get('paymentsTotal')),
            doc.get('dueDate'),
            doc.get('docNumber', ''),
            json.dumps(doc.get('tags') or []),
            doc.get('notes', '')
        )

        if _USE_SQLITE:
            cursor.execute(f'''
                INSERT OR REPLACE INTO {table}
                    (id, contact_id, contact_name, "desc", date, amount, status,
                     payments_pending, payments_total, due_date, doc_number, tags, notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', vals)
        else:
            cursor.execute(f'''
                INSERT INTO {table}
                    (id, contact_id, contact_name, "desc", date, amount, status,
                     payments_pending, payments_total, due_date, doc_number, tags, notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    contact_id=EXCLUDED.contact_id, contact_name=EXCLUDED.contact_name,
                    "desc"=EXCLUDED."desc", date=EXCLUDED.date, amount=EXCLUDED.amount,
                    status=EXCLUDED.status, payments_pending=EXCLUDED.payments_pending,
                    payments_total=EXCLUDED.payments_total, due_date=EXCLUDED.due_date,
                    doc_number=EXCLUDED.doc_number, tags=EXCLUDED.tags, notes=EXCLUDED.notes
            ''', vals)
    else:
        # Estimates & purchases: 10 columns (no payment fields)
        vals = (
            doc_id,
            doc.get('contact'),
            doc.get('contactName'),
            doc.get('desc', ''),
            doc.get('date'),
            _num(doc.get('total')),
            doc.get('status', 0),
            doc.get('docNumber', ''),
            json.dumps(doc.get('tags') or []),
            doc.get('notes', '')
        )

        if _USE_SQLITE:
            cursor.execute(f'''
                INSERT OR REPLACE INTO {table}
                    (id, contact_id, contact_name, "desc", date, amount, status,
                     doc_number, tags, notes)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            ''', vals)
        else:
            cursor.execute(f'''
                INSERT INTO {table}
                    (id, contact_id, contact_name, "desc", date, amount, status,
                     doc_number, tags, notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    contact_id=EXCLUDED.contact_id, contact_name=EXCLUDED.contact_name,
                    "desc"=EXCLUDED."desc", date=EXCLUDED.date, amount=EXCLUDED.amount,
                    status=EXCLUDED.status,
                    doc_number=EXCLUDED.doc_number, tags=EXCLUDED.tags, notes=EXCLUDED.notes
            ''', vals)

    # Items: delete + re-insert
    cursor.execute(_q(f'DELETE FROM {items_table} WHERE {fk_column} = ?'), (doc_id,))
    for prod in doc.get('products', []):
        retention = extract_ret(prod)
        acc_id = prod.get('accountCode') or prod.get('accountName') or prod.get('account')
        # Resolve ledger account ID to human-readable name (like sync_documents line 760)
        account = acc_map.get(acc_id, acc_id)
        cursor.execute(_q(f'''
            INSERT INTO {items_table}
                ({fk_column}, product_id, name, sku, units, price, subtotal,
                 discount, tax, retention, account, project_id, kind, "desc")
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        '''), (doc_id, prod.get('productId'), prod.get('name'), prod.get('sku'),
               _num(prod.get('units')), _num(prod.get('price')), _num(prod.get('subtotal')),
               _num(prod.get('discount')), _num(prod.get('tax')), _num(retention), account,
               prod.get('projectid'), prod.get('kind'), prod.get('desc')))

    # Extract project code + shooting dates from line items
    project_code = _extract_project_code(doc.get('products'))
    shooting_raw = _extract_shooting_dates(doc.get('products'))
    cursor.execute(_q(f'UPDATE {table} SET project_code = ?, shooting_dates_raw = ? WHERE id = ?'),
                   (project_code, shooting_raw, doc_id))

    # If this doc has a project code, ensure job exists
    if project_code:
        from skills.job_tracker import ensure_job
        doc_data = {
            "client_id": doc.get('contact'),
            "client_name": doc.get('contactName'),
            "shooting_dates_raw": shooting_raw,
            "estimate_id": doc_id if table == 'estimates' else None,
            "estimate_number": doc.get('docNumber') if table == 'estimates' else None,
            "invoice_id": doc_id if table == 'invoices' else None,
            "invoice_number": doc.get('docNumber') if table == 'invoices' else None,
            "doc_date": doc.get('date'),
        }
        ensure_job(project_code, doc_data, cursor)


def _upsert_single_contact(cursor, contact):
    """Upsert a single contact into local DB."""
    cid = contact.get('id')
    if not cid:
        return

    vals = (
        cid,
        contact.get('name', ''),
        contact.get('email', ''),
        contact.get('type', ''),
        contact.get('code', ''),
        contact.get('vat', ''),
        contact.get('phone', ''),
        contact.get('mobile', '')
    )

    if _USE_SQLITE:
        cursor.execute('''
            INSERT OR REPLACE INTO contacts
                (id, name, email, type, code, vat, phone, mobile)
            VALUES (?,?,?,?,?,?,?,?)
        ''', vals)
    else:
        cursor.execute('''
            INSERT INTO contacts
                (id, name, email, type, code, vat, phone, mobile)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO UPDATE SET
                name=EXCLUDED.name, email=EXCLUDED.email, type=EXCLUDED.type,
                code=EXCLUDED.code, vat=EXCLUDED.vat, phone=EXCLUDED.phone,
                mobile=EXCLUDED.mobile
        ''', vals)


def _upsert_single_product(cursor, product):
    """Upsert a single product into local DB."""
    pid = product.get('id')
    if not pid:
        return

    vals = (
        pid,
        product.get('name', ''),
        product.get('desc', ''),
        _num(product.get('price')),
        _num(product.get('stock')),
        product.get('sku', ''),
        product.get('kind', 'simple')
    )

    if _USE_SQLITE:
        cursor.execute('''
            INSERT OR REPLACE INTO products
                (id, name, "desc", price, stock, sku, kind)
            VALUES (?,?,?,?,?,?,?)
        ''', vals)
    else:
        cursor.execute('''
            INSERT INTO products
                (id, name, "desc", price, stock, sku, kind)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO UPDATE SET
                name=EXCLUDED.name, "desc"=EXCLUDED."desc", price=EXCLUDED.price,
                stock=EXCLUDED.stock, sku=EXCLUDED.sku, kind=EXCLUDED.kind
        ''', vals)
