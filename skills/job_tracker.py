"""
Job Tracker Skill — Obsidian Job Dossier System
================================================
Creates and maintains an Obsidian note per project (identified by project_code).
Each note aggregates: quote PDF, shooting dates, expenses, client email, invoicing checklist.

Architecture:
  connector.py (sync) → jobs table (source of truth) → render note → Brain API → Obsidian vault

Path convention:
  ACCOUNTS/SEGUIMIENTO_TRABAJOS/{year}/{quarter}/{code}.md
  ACCOUNTS/SEGUIMIENTO_TRABAJOS/{year}/{quarter}/attachments/{code}_{doc_number}.pdf
"""
import os
import re
import json
import logging
import hashlib
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────
BRAIN_API_URL = os.getenv("BRAIN_API_URL", "http://localhost:3100")
BRAIN_INTERNAL_KEY = os.getenv("BRAIN_INTERNAL_KEY", "")

# ── Date Parser ────────────────────────────────────────────────────────────────

def parse_shooting_dates(raw, reference_year):
    """Parse flexible shooting date strings into sorted ISO date list.

    Supported formats:
      17/3           → single date
      17/3-21/3      → range (same or cross month)
      17-18/3        → short range (shared month)
      17/3, 19/3     → comma-separated
      17, 18, 21/3   → list with trailing month
      28/12-3/1      → cross-year range

    Args:
        raw: date string from Holded line item description
        reference_year: year from the parent document date

    Returns:
        Sorted list of ISO date strings ["2026-03-17", ...]. Empty list on failure.
    """
    if not raw or not isinstance(raw, str):
        return []

    raw = raw.strip()
    if not raw:
        return []

    try:
        dates = set()
        segments = [s.strip() for s in raw.split(",")]

        # First pass: find the rightmost explicit month (for inheriting)
        last_month = None
        for seg in reversed(segments):
            m = re.search(r'(\d{1,2})/(\d{1,2})', seg)
            if m:
                last_month = int(m.group(2))
                break

        if last_month is None:
            return []

        for seg in segments:
            if not seg:
                continue

            # Check for range (contains - that's not inside a d/m pattern)
            range_match = re.match(
                r'^(\d{1,2}(?:/\d{1,2})?)\s*-\s*(\d{1,2}(?:/\d{1,2})?)$', seg
            )
            if range_match:
                start_str, end_str = range_match.group(1), range_match.group(2)
                start_date = _parse_single(start_str, last_month, reference_year)
                end_date = _parse_single(end_str, last_month, reference_year)
                if start_date and end_date:
                    # Cross-year: if end < start, bump end year
                    if end_date < start_date:
                        end_date = end_date.replace(year=end_date.year + 1)
                    current = start_date
                    while current <= end_date:
                        dates.add(current)
                        current += timedelta(days=1)
                    # Update last_month from this segment
                    if '/' in end_str:
                        last_month = int(end_str.split('/')[1])
                    elif '/' in start_str:
                        last_month = int(start_str.split('/')[1])
            else:
                # Single date
                d = _parse_single(seg.strip(), last_month, reference_year)
                if d:
                    dates.add(d)

        return [d.isoformat() for d in sorted(dates)]

    except Exception as e:
        logger.warning(f"Failed to parse shooting dates '{raw}': {e}")
        return []


def _parse_single(s, default_month, year):
    """Parse a single date token like '17/3' or '17' into a date object."""
    s = s.strip()
    if not s:
        return None

    if '/' in s:
        parts = s.split('/')
        try:
            day, month = int(parts[0]), int(parts[1])
            return date(year, month, day)
        except (ValueError, IndexError):
            return None
    else:
        try:
            day = int(s)
            return date(year, default_month, day)
        except (ValueError, TypeError):
            return None
