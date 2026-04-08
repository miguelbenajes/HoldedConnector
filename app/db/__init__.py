"""app.db — Database layer for holded-connector.

Public API:
  connection: get_db, release_db, db_context, _cursor, _q, _num, _row_val,
              _fetch_one_val, insert_audit_log, update_audit_log
  schema:     init_db
  settings:   reload_config, get_setting, save_setting
"""

from app.db.connection import (
    get_db, release_db, db_context,
    _cursor, _q, _num, _row_val, _fetch_one_val,
    insert_audit_log, update_audit_log,
    _CompatCursor, _ConnProxy,
)
from app.db.schema import init_db
from app.db.settings import reload_config, get_setting, save_setting

__all__ = [
    "get_db", "release_db", "db_context",
    "_cursor", "_q", "_num", "_row_val", "_fetch_one_val",
    "insert_audit_log", "update_audit_log",
    "_CompatCursor", "_ConnProxy",
    "init_db",
    "reload_config", "get_setting", "save_setting",
]
