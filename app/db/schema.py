"""Database schema — CREATE TABLE statements, migrations, indexes, seed data.

Extracted from connector.py (L184-785) during Fase 1 refactor.
Called once on startup via init_db().
"""

import logging

from app.db.connection import get_db, release_db, _cursor, _USE_SQLITE

logger = logging.getLogger(__name__)


def init_db():
    """Initialize all database tables, run column migrations, seed data."""
    conn = get_db()
    try:
        _init_db_inner(conn)
    finally:
        release_db(conn)


def _init_db_inner(conn):
    cursor = _cursor(conn)

    # Dialect-specific type tokens
    if _USE_SQLITE:
        _serial = "INTEGER PRIMARY KEY AUTOINCREMENT"
        _real   = "REAL"
        _now    = "datetime('now')"
    else:
        _serial = "SERIAL PRIMARY KEY"
        _real   = "NUMERIC"
        _now    = "NOW()"

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS invoices (
            id TEXT PRIMARY KEY,
            contact_id TEXT,
            contact_name TEXT,
            "desc" TEXT,
            date INTEGER,
            amount {_real},
            status INTEGER,
            payments_pending {_real} DEFAULT 0,
            payments_total {_real} DEFAULT 0,
            due_date INTEGER,
            doc_number TEXT,
            tags TEXT,
            notes TEXT,
            project_code TEXT
        )
    ''')

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS purchase_invoices (
            id TEXT PRIMARY KEY,
            contact_id TEXT,
            contact_name TEXT,
            "desc" TEXT,
            date INTEGER,
            amount {_real},
            status INTEGER,
            doc_number TEXT,
            tags TEXT,
            notes TEXT,
            project_code TEXT
        )
    ''')

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS estimates (
            id TEXT PRIMARY KEY,
            contact_id TEXT,
            contact_name TEXT,
            "desc" TEXT,
            date INTEGER,
            amount {_real},
            status INTEGER,
            doc_number TEXT,
            tags TEXT,
            notes TEXT,
            project_code TEXT
        )
    ''')

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY,
            name TEXT,
            "desc" TEXT,
            price {_real},
            stock {_real},
            sku TEXT,
            kind TEXT DEFAULT 'simple',
            web_include INTEGER NOT NULL DEFAULT 1
        )
    ''')

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS pack_components (
            pack_id TEXT NOT NULL,
            component_id TEXT NOT NULL,
            quantity {_real} NOT NULL DEFAULT 1,
            UNIQUE(pack_id, component_id)
        )
    ''')

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS invoice_items (
            id {_serial},
            invoice_id TEXT,
            product_id TEXT,
            name TEXT,
            sku TEXT,
            units {_real},
            price {_real},
            subtotal {_real},
            discount {_real},
            tax {_real},
            retention {_real},
            account TEXT,
            project_id TEXT,
            kind TEXT,
            "desc" TEXT
        )
    ''')

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS estimate_items (
            id {_serial},
            estimate_id TEXT,
            product_id TEXT,
            name TEXT,
            sku TEXT,
            units {_real},
            price {_real},
            subtotal {_real},
            discount {_real},
            tax {_real},
            retention {_real},
            account TEXT,
            project_id TEXT,
            kind TEXT,
            "desc" TEXT
        )
    ''')

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS purchase_items (
            id {_serial},
            purchase_id TEXT,
            product_id TEXT,
            name TEXT,
            sku TEXT,
            units {_real},
            price {_real},
            subtotal {_real},
            discount {_real},
            tax {_real},
            retention {_real},
            account TEXT,
            project_id TEXT,
            kind TEXT,
            "desc" TEXT
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

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT,
            "desc" TEXT,
            status TEXT,
            customer_id TEXT,
            budget {_real}
        )
    ''')

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS payments (
            id TEXT PRIMARY KEY,
            document_id TEXT,
            amount {_real},
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

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS amortizations (
            id {_serial},
            product_id TEXT NOT NULL,
            product_name TEXT NOT NULL,
            purchase_price {_real} NOT NULL,
            purchase_date TEXT NOT NULL,
            notes TEXT,
            product_type TEXT NOT NULL DEFAULT 'alquiler',
            created_at TEXT DEFAULT ({_now}),
            updated_at TEXT DEFAULT ({_now}),
            UNIQUE(product_id)
        )
    ''')

    # Fiscal rules per product type — drives IRPF logic across the whole app
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS product_type_rules (
            type_key TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            irpf_pct {_real} NOT NULL DEFAULT 0,
            is_expense INTEGER NOT NULL DEFAULT 0,
            description TEXT
        )
    ''')

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS purchase_analysis (
            id {_serial},
            purchase_id TEXT NOT NULL UNIQUE,
            category TEXT,
            subcategory TEXT,
            confidence TEXT,
            method TEXT,
            reasoning TEXT,
            analyzed_at TEXT DEFAULT ({_now})
        )
    ''')

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS inventory_matches (
            id {_serial},
            purchase_id TEXT NOT NULL,
            purchase_item_id INTEGER,
            product_id TEXT NOT NULL,
            product_name TEXT NOT NULL,
            matched_price {_real} NOT NULL,
            matched_date TEXT NOT NULL,
            match_method TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT ({_now}),
            UNIQUE(purchase_id, product_id)
        )
    ''')

    # amortization_purchases: many-to-many between amortizations and purchase sources
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS amortization_purchases (
            id {_serial},
            amortization_id INTEGER NOT NULL,
            purchase_id TEXT,
            purchase_item_id INTEGER,
            cost_override {_real} NOT NULL,
            allocation_note TEXT,
            created_at TEXT DEFAULT ({_now})
        )
    ''')

    # ── AI tables ────────────────────────────────────────────────────────────
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ai_history (
            id TEXT PRIMARY KEY,
            role TEXT,
            content TEXT,
            timestamp TEXT,
            conversation_id TEXT,
            tool_calls TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ai_favorites (
            id TEXT PRIMARY KEY,
            query TEXT,
            label TEXT,
            created_at TEXT
        )
    ''')

    # ── Sync log table (used by n8n flows) ───────────────────────────────────
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS sync_logs (
            id {_serial},
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT,
            duration_seconds {_real},
            counts TEXT
        )
    ''')

    # ── Write Audit Log ──────────────────────────────────────────────
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS write_audit_log (
            id              {_serial},
            timestamp       TEXT DEFAULT ({_now}),
            source          TEXT NOT NULL,
            operation       TEXT NOT NULL,
            entity_type     TEXT NOT NULL,
            entity_id       TEXT,
            payload_sent    TEXT,
            response_received TEXT,
            preview_data    TEXT,
            warnings        TEXT,
            status          TEXT NOT NULL DEFAULT 'pending',
            tables_synced   TEXT,
            reverse_action  TEXT,
            reverse_payload TEXT,
            user_confirmed  BOOLEAN,
            error_detail    TEXT,
            safe_mode       BOOLEAN DEFAULT FALSE,
            conversation_id TEXT,
            checksum        TEXT,
            duration_ms     INTEGER
        )
    ''')

    # ── Contact table expansion (country, address, discount) ──────────
    for col, defn in [
        ("country", "TEXT DEFAULT ''"),
        ("address", "TEXT DEFAULT ''"),
        ("city", "TEXT DEFAULT ''"),
        ("province", "TEXT DEFAULT ''"),
        ("postal_code", "TEXT DEFAULT ''"),
        ("trade_name", "TEXT DEFAULT ''"),
        ("discount", "REAL DEFAULT 0"),
    ]:
        try:
            if _USE_SQLITE:
                cursor.execute(f"ALTER TABLE contacts ADD COLUMN {col} {defn}")
            else:
                cursor.execute("SAVEPOINT sp_contact_col")
                cursor.execute(f"ALTER TABLE contacts ADD COLUMN {col} {defn}")
                cursor.execute("RELEASE SAVEPOINT sp_contact_col")
        except Exception:
            if not _USE_SQLITE:
                try:
                    cursor.execute("ROLLBACK TO SAVEPOINT sp_contact_col")
                except Exception:
                    pass

    # ── Job Tracker tables ────────────────────────────────────────────────────
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS jobs (
            project_code TEXT PRIMARY KEY,
            client_id TEXT,
            client_name TEXT,
            client_email TEXT,
            status TEXT DEFAULT 'open',
            shooting_dates_raw TEXT,
            shooting_dates TEXT,
            quarter TEXT,
            estimate_id TEXT,
            estimate_number TEXT,
            invoice_id TEXT,
            invoice_number TEXT,
            note_path TEXT,
            pdf_hash TEXT,
            created_at TEXT DEFAULT ({_now}),
            updated_at TEXT DEFAULT ({_now})
        )
    ''')

    # Job tracker automation columns
    for col, col_type in [
        ("notes_hash", "TEXT"),
        ("invoice_draft_created_at", "TEXT"),
        ("last_alerts", "TEXT"),
    ]:
        try:
            if _USE_SQLITE:
                cursor.execute(f'ALTER TABLE jobs ADD COLUMN {col} {col_type}')
            else:
                cursor.execute("SAVEPOINT sp_add_col")
                cursor.execute(f'ALTER TABLE jobs ADD COLUMN {col} {col_type}')
                cursor.execute("RELEASE SAVEPOINT sp_add_col")
        except Exception:
            if not _USE_SQLITE:
                cursor.execute("ROLLBACK TO SAVEPOINT sp_add_col")
            pass  # Column already exists

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS job_note_queue (
            id {_serial},
            project_code TEXT NOT NULL,
            action TEXT NOT NULL,
            retry_count INTEGER DEFAULT 0,
            last_error TEXT,
            created_at TEXT DEFAULT ({_now}),
            processed_at TEXT
        )
    ''')

    cursor.execute(f'''CREATE TABLE IF NOT EXISTS job_note_actions (
        id {_serial},
        project_code TEXT NOT NULL,
        action_type TEXT NOT NULL,
        details TEXT,
        confirmed_by TEXT,
        result TEXT,
        created_at TEXT DEFAULT ({_now})
    )''')

    # Audit log indexes (PG only)
    if not _USE_SQLITE:
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON write_audit_log(timestamp)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_audit_entity ON write_audit_log(entity_type, entity_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_audit_operation ON write_audit_log(operation)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_audit_status ON write_audit_log(status)')

        try:
            cursor.execute("""CREATE INDEX IF NOT EXISTS idx_job_queue_pending
                ON job_note_queue (created_at) WHERE processed_at IS NULL AND retry_count < 5""")
        except Exception:
            pass

    # ── Column migrations (SQLite only — PG creates all columns upfront) ──────
    if _USE_SQLITE:
        for col, definition in [
            ("payments_pending", "REAL DEFAULT 0"),
            ("payments_total",   "REAL DEFAULT 0"),
            ("due_date",         "INTEGER"),
            ("doc_number",       "TEXT"),
        ]:
            try:
                cursor.execute(f"ALTER TABLE invoices ADD COLUMN {col} {definition}")
            except Exception:
                pass  # column already exists

        for tbl in ("purchase_invoices", "estimates"):
            try:
                cursor.execute(f"ALTER TABLE {tbl} ADD COLUMN doc_number TEXT")
            except Exception:
                pass

        for tbl, col, defn in [
            ("invoices",          "tags",       "TEXT"),
            ("invoices",          "notes",      "TEXT"),
            ("purchase_invoices", "tags",       "TEXT"),
            ("purchase_invoices", "notes",      "TEXT"),
            ("estimates",         "tags",       "TEXT"),
            ("estimates",         "notes",      "TEXT"),
            ("invoice_items",     "project_id", "TEXT"),
            ("invoice_items",     "kind",       "TEXT"),
            ("purchase_items",    "project_id", "TEXT"),
            ("purchase_items",    "kind",       "TEXT"),
            ("estimate_items",    "project_id", "TEXT"),
            ("estimate_items",    "kind",       "TEXT"),
            ("invoice_items",     '"desc"',     "TEXT"),
            ("purchase_items",    '"desc"',     "TEXT"),
            ("estimate_items",    '"desc"',     "TEXT"),
            ("invoices",          "project_code", "TEXT"),
            ("purchase_invoices", "project_code", "TEXT"),
            ("estimates",         "project_code", "TEXT"),
            ("invoices",          "shooting_dates_raw", "TEXT"),
            ("purchase_invoices", "shooting_dates_raw", "TEXT"),
            ("estimates",         "shooting_dates_raw", "TEXT"),
        ]:
            try:
                cursor.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {defn}")
            except Exception:
                pass

        try:
            cursor.execute("ALTER TABLE amortizations ADD COLUMN product_type TEXT NOT NULL DEFAULT 'alquiler'")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE products ADD COLUMN kind TEXT DEFAULT 'simple'")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE products ADD COLUMN web_include INTEGER NOT NULL DEFAULT 1")
        except Exception:
            pass
    else:
        # PostgreSQL: ADD COLUMN IF NOT EXISTS (PG 9.6+)
        for stmt in [
            "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS payments_pending NUMERIC DEFAULT 0",
            "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS payments_total NUMERIC DEFAULT 0",
            "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS due_date INTEGER",
            "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS doc_number TEXT",
            "ALTER TABLE purchase_invoices ADD COLUMN IF NOT EXISTS doc_number TEXT",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS doc_number TEXT",
            "ALTER TABLE amortizations ADD COLUMN IF NOT EXISTS product_type TEXT NOT NULL DEFAULT 'alquiler'",
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS kind TEXT DEFAULT 'simple'",
            "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS tags TEXT",
            "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS notes TEXT",
            "ALTER TABLE purchase_invoices ADD COLUMN IF NOT EXISTS tags TEXT",
            "ALTER TABLE purchase_invoices ADD COLUMN IF NOT EXISTS notes TEXT",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS tags TEXT",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS notes TEXT",
            "ALTER TABLE invoice_items ADD COLUMN IF NOT EXISTS project_id TEXT",
            "ALTER TABLE invoice_items ADD COLUMN IF NOT EXISTS kind TEXT",
            "ALTER TABLE purchase_items ADD COLUMN IF NOT EXISTS project_id TEXT",
            "ALTER TABLE purchase_items ADD COLUMN IF NOT EXISTS kind TEXT",
            "ALTER TABLE estimate_items ADD COLUMN IF NOT EXISTS project_id TEXT",
            "ALTER TABLE estimate_items ADD COLUMN IF NOT EXISTS kind TEXT",
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS web_include INTEGER NOT NULL DEFAULT 1",
            'ALTER TABLE invoice_items ADD COLUMN IF NOT EXISTS "desc" TEXT',
            'ALTER TABLE purchase_items ADD COLUMN IF NOT EXISTS "desc" TEXT',
            'ALTER TABLE estimate_items ADD COLUMN IF NOT EXISTS "desc" TEXT',
            "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS project_code TEXT",
            "ALTER TABLE purchase_invoices ADD COLUMN IF NOT EXISTS project_code TEXT",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS project_code TEXT",
            "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS shooting_dates_raw TEXT",
            "ALTER TABLE purchase_invoices ADD COLUMN IF NOT EXISTS shooting_dates_raw TEXT",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS shooting_dates_raw TEXT",
        ]:
            try:
                cursor.execute(stmt)
            except Exception:
                pass

    # ── Seed product_type_rules (never overwrites user edits) ────────────────
    default_rules = [
        ('alquiler', 'Alquiler',        19.0, 0, 'Equipamiento cedido en uso. IRPF retención 19%.'),
        ('venta',    'Venta',            0.0, 0, 'Venta de producto. Sin retención IRPF. Match 1-to-1 compra→venta.'),
        ('servicio', 'Servicio / Fee',  15.0, 0, 'Honorarios profesionales. IRPF retención 15%.'),
        ('gasto',    'Gasto',            0.0, 1, 'Gasto deducible / suplido. No genera ingreso directo.'),
    ]
    for rule in default_rules:
        if _USE_SQLITE:
            cursor.execute(
                "INSERT OR IGNORE INTO product_type_rules "
                "(type_key, label, irpf_pct, is_expense, description) VALUES (?,?,?,?,?)",
                rule
            )
        else:
            cursor.execute(
                "INSERT INTO product_type_rules "
                "(type_key, label, irpf_pct, is_expense, description) VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT (type_key) DO NOTHING",
                rule
            )

    # ── Data migration: populate amortization_purchases from legacy notes ─────
    if _USE_SQLITE:
        cursor.execute('''
            INSERT OR IGNORE INTO amortization_purchases (amortization_id, purchase_id, cost_override, allocation_note)
            SELECT
                a.id,
                CASE
                    WHEN a.notes LIKE 'Auto-detected from purchase %'
                    THEN TRIM(SUBSTR(a.notes, 27, INSTR(SUBSTR(a.notes,27),' ')-1))
                    ELSE NULL
                END AS purchase_id,
                a.purchase_price,
                'Migrado automáticamente'
            FROM amortizations a
            WHERE a.id NOT IN (SELECT DISTINCT amortization_id FROM amortization_purchases)
              AND a.purchase_price > 0
        ''')

    # ── Indexes ───────────────────────────────────────────────────────────────
    for idx_sql in [
        'CREATE INDEX IF NOT EXISTS idx_invoices_contact ON invoices(contact_id)',
        'CREATE INDEX IF NOT EXISTS idx_invoices_date ON invoices(date)',
        'CREATE INDEX IF NOT EXISTS idx_purchases_contact ON purchase_invoices(contact_id)',
        'CREATE INDEX IF NOT EXISTS idx_purchases_date ON purchase_invoices(date)',
        'CREATE INDEX IF NOT EXISTS idx_inv_items_product ON invoice_items(product_id)',
        'CREATE INDEX IF NOT EXISTS idx_inv_items_invoice ON invoice_items(invoice_id)',
        'CREATE INDEX IF NOT EXISTS idx_pur_items_product ON purchase_items(product_id)',
        'CREATE INDEX IF NOT EXISTS idx_pur_items_purchase ON purchase_items(purchase_id)',
        'CREATE INDEX IF NOT EXISTS idx_pack_components_pack ON pack_components(pack_id)',
        'CREATE INDEX IF NOT EXISTS idx_pack_components_comp ON pack_components(component_id)',
        'CREATE INDEX IF NOT EXISTS idx_ai_history_conv ON ai_history(conversation_id, timestamp)',
        'CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status)',
        'CREATE INDEX IF NOT EXISTS idx_invoices_payments_pending ON invoices(payments_pending)',
        'CREATE INDEX IF NOT EXISTS idx_ai_history_conv_role ON ai_history(conversation_id, role, timestamp)',
        # Added in Fase 1 refactor
        'CREATE INDEX IF NOT EXISTS idx_invoices_project_code ON invoices(project_code)',
        'CREATE INDEX IF NOT EXISTS idx_est_items_estimate ON estimate_items(estimate_id)',
    ]:
        cursor.execute(idx_sql)

    conn.commit()
