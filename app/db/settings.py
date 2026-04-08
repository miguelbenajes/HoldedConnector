"""Runtime settings — reload_config, get_setting, save_setting.

Extracted from connector.py (L111-168) during Fase 1 refactor.
Modifies API_KEY/HEADERS in connection.py when holded_api_key changes.
"""

import os
import logging

from dotenv import load_dotenv

import app.db.connection as _conn

logger = logging.getLogger(__name__)


def reload_config():
    """Reload API_KEY and HEADERS from DB settings (fallback: .env)."""
    conn = _conn.get_db()
    try:
        cursor = _conn._cursor(conn)
        # settings table is created by schema.init_db() — if it doesn't exist yet,
        # the except block handles it gracefully (first run before init_db).
        cursor.execute("SELECT key, value FROM settings")
        rows = cursor.fetchall()
        if _conn._USE_SQLITE:
            settings = {row[0]: row[1] for row in rows}
        else:
            settings = {row["key"]: row["value"] for row in rows}

        if 'holded_api_key' in settings:
            _conn.API_KEY = settings['holded_api_key']
        else:
            load_dotenv()
            _conn.API_KEY = os.getenv("HOLDED_API_KEY")

        _conn.HEADERS["key"] = _conn.API_KEY
    except Exception:
        # DB not ready yet (first run before init_db) — keep current config
        logger.debug("reload_config failed (DB not ready?), keeping current config")
    finally:
        _conn.release_db(conn)


def get_setting(key, default=None):
    """Read a single setting from the settings table."""
    conn = _conn.get_db()
    try:
        cursor = _conn._cursor(conn)
        cursor.execute(_conn._q("SELECT value FROM settings WHERE key = ?"), (key,))
        row = cursor.fetchone()
        if row is None:
            return default
        return row[0] if _conn._USE_SQLITE else row["value"]
    finally:
        _conn.release_db(conn)


def save_setting(key, value):
    """Upsert a single setting in the settings table."""
    conn = _conn.get_db()
    try:
        cursor = _conn._cursor(conn)
        if _conn._USE_SQLITE:
            cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        else:
            cursor.execute("""
                INSERT INTO settings (key, value) VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, (key, value))
        conn.commit()
    finally:
        _conn.release_db(conn)


# Initial load — safe even if DB doesn't exist yet
try:
    reload_config()
except Exception:
    pass
