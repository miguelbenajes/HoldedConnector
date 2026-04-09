"""Shared utilities for API routers."""
import re

_VALID_TABLE_RE = re.compile(r'^[a-z_][a-z0-9_]*$')


def assert_valid_table(name: str) -> None:
    """Raise ValueError if table name contains unexpected characters."""
    if not _VALID_TABLE_RE.match(name):
        raise ValueError(f"Invalid table name: {name!r}")
