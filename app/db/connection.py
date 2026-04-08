"""Database connection layer — pool, helpers, context manager, audit log.

Extracted from connector.py (L1-107, L863-878, L1912-1917, L2732-2796)
and api.py (L302-333) during Fase 1 refactor.

All DB access goes through these primitives. Other modules import from here
(or via the connector.py facade for backwards compatibility).
"""

import os
import json
import sqlite3
import logging
import threading
from contextlib import contextmanager

from dotenv import load_dotenv

# ── Load environment BEFORE any os.getenv() ─────────────────────────────────
load_dotenv()

logger = logging.getLogger(__name__)

# ── Holded API configuration ────────────────────────────────────────────────
API_KEY = os.getenv("HOLDED_API_KEY")
SAFE_MODE = os.getenv("HOLDED_SAFE_MODE", "true").lower() == "true"
BASE_URL = "https://api.holded.com/api"
HEADERS = {
    "key": API_KEY,
    "Content-Type": "application/json"
}

# ── Database backend selection ───────────────────────────────────────────────
# If DATABASE_URL is set → PostgreSQL (Supabase). Otherwise → SQLite (dev).
DATABASE_URL = os.getenv("DATABASE_URL")
_USE_SQLITE = not DATABASE_URL

if not _USE_SQLITE:
    import psycopg2
    import psycopg2.extras
    import psycopg2.pool as _pg_pool

DB_NAME = os.getenv("DB_NAME", "holded.db")  # SQLite mode only

# ── Connection pooling (PostgreSQL) — thread-safe init ───────────────────────
_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    """Return the shared connection pool, creating it on first call (thread-safe)."""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = _pg_pool.ThreadedConnectionPool(
                minconn=2, maxconn=10, dsn=DATABASE_URL, connect_timeout=10
            )
    return _pool


def get_db():
    """Return a database connection for the active backend."""
    if _USE_SQLITE:
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        return conn
    try:
        conn = _get_pool().getconn()
    except Exception as e:
        logger.error("Failed to get connection from pool: %s", e)
        raise RuntimeError("Database connection unavailable") from e
    try:
        conn.autocommit = False
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
    except Exception:
        try:
            _get_pool().putconn(conn, close=True)
        except Exception:
            pass
        conn = _get_pool().getconn()
    return conn


def release_db(conn):
    """Return a PostgreSQL connection to the pool, rolling back any uncommitted transaction."""
    if _USE_SQLITE:
        conn.close()
    else:
        try:
            conn.rollback()
        except Exception:
            pass
        _get_pool().putconn(conn)


# ── Cursor & query helpers ───────────────────────────────────────────────────

def _cursor(conn):
    """Return a dict-like cursor regardless of DB backend."""
    if _USE_SQLITE:
        return conn.cursor()
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def _q(sql):
    """Convert SQLite ? placeholders to PostgreSQL %s when needed."""
    if _USE_SQLITE:
        return sql
    return sql.replace("?", "%s")


def _num(val):
    """Sanitize a value for a NUMERIC column: empty strings → None (NULL).
    PostgreSQL rejects empty strings in NUMERIC columns; SQLite accepts them silently."""
    if val is None or val == '':
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _row_val(row, key, idx):
    """Retrieve a value from a DB row that may be a dict (PG) or tuple (SQLite)."""
    if isinstance(row, dict):
        return row.get(key)
    return row[idx]


def _fetch_one_val(cursor, key):
    """Fetch a single scalar value from a row, works for both dict and tuple cursors."""
    row = cursor.fetchone()
    if row is None:
        return None
    return row[key] if isinstance(row, dict) else row[0]


# ── Connection proxy & context manager ───────────────────────────────────────
# Unified from api.py's _CompatCursor + _ConnProxy + get_db_connection()

class _CompatCursor:
    """Cursor wrapper: auto-converts ? placeholders for PostgreSQL and returns dict-like rows."""
    def __init__(self, inner):
        self._cur = inner
    def execute(self, sql, params=None):
        sql = _q(sql)
        return self._cur.execute(sql, params) if params is not None else self._cur.execute(sql)
    def fetchone(self):     return self._cur.fetchone()
    def fetchall(self):     return self._cur.fetchall()
    def fetchmany(self, n): return self._cur.fetchmany(n)
    def close(self):        return self._cur.close()
    @property
    def description(self): return self._cur.description
    @property
    def rowcount(self):    return self._cur.rowcount


class _ConnProxy:
    """Connection proxy: .cursor() returns a _CompatCursor backed by _cursor()."""
    def __init__(self, conn):
        self._conn = conn
    def cursor(self):   return _CompatCursor(_cursor(self._conn))
    def commit(self):   self._conn.commit()
    def rollback(self): self._conn.rollback()
    def close(self):    release_db(self._conn)


@contextmanager
def db_context():
    """Context manager yielding a _ConnProxy. Auto-releases on exit."""
    conn = get_db()
    try:
        yield _ConnProxy(conn)
    finally:
        release_db(conn)


# ── Audit log ────────────────────────────────────────────────────────────────

def insert_audit_log(source, operation, entity_type, payload_sent=None,
                     preview_data=None, warnings=None, status='pending',
                     safe_mode=False, conversation_id=None):
    """Insert a new audit log entry. Returns the new row ID."""
    conn = get_db()
    try:
        cursor = _cursor(conn)
        insert_params = (source, operation, entity_type,
               json.dumps(payload_sent) if payload_sent else None,
               json.dumps(preview_data) if preview_data else None,
               json.dumps(warnings) if warnings else None,
               status, safe_mode, conversation_id)
        if _USE_SQLITE:
            cursor.execute('''
                INSERT INTO write_audit_log
                    (source, operation, entity_type, payload_sent, preview_data,
                     warnings, status, safe_mode, conversation_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', insert_params)
            audit_id = cursor.lastrowid
        else:
            cursor.execute('''
                INSERT INTO write_audit_log
                    (source, operation, entity_type, payload_sent, preview_data,
                     warnings, status, safe_mode, conversation_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', insert_params)
            row = cursor.fetchone()
            audit_id = row['id'] if row else None
        conn.commit()
        return audit_id
    except Exception as e:
        logger.error(f"Failed to insert audit log: {e}")
        conn.rollback()
        return None
    finally:
        release_db(conn)


def update_audit_log(audit_id, **kwargs):
    """Update an existing audit log entry. Accepts any column as kwarg."""
    if not audit_id:
        return
    json_fields = {'payload_sent', 'response_received', 'preview_data',
                   'warnings', 'tables_synced', 'reverse_action', 'reverse_payload'}
    conn = get_db()
    try:
        cursor = _cursor(conn)
        sets = []
        vals = []
        for k, v in kwargs.items():
            sets.append(f'{k} = {_q("?")}')
            if k in json_fields and v is not None and not isinstance(v, str):
                vals.append(json.dumps(v))
            else:
                vals.append(v)
        vals.append(audit_id)
        cursor.execute(_q(f'UPDATE write_audit_log SET {", ".join(sets)} WHERE id = ?'), vals)
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to update audit log {audit_id}: {e}")
        conn.rollback()
    finally:
        release_db(conn)
