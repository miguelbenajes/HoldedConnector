"""Unit tests for skills/job_tracker.py — date parser, sanitizers, note renderer."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills.job_tracker import parse_shooting_dates


class TestParseShootingDates:
    """Flexible date parser for Holded 'Shooting Dates:' line item descriptions."""

    def test_single_date(self):
        assert parse_shooting_dates("17/3", 2026) == ["2026-03-17"]

    def test_range_same_month(self):
        result = parse_shooting_dates("17/3-21/3", 2026)
        assert result == ["2026-03-17", "2026-03-18", "2026-03-19", "2026-03-20", "2026-03-21"]

    def test_short_range_shared_month(self):
        assert parse_shooting_dates("17-18/3", 2026) == ["2026-03-17", "2026-03-18"]

    def test_comma_separated(self):
        assert parse_shooting_dates("17/3, 19/3, 21/3", 2026) == [
            "2026-03-17", "2026-03-19", "2026-03-21"
        ]

    def test_list_with_trailing_month(self):
        assert parse_shooting_dates("17, 18, 21/3", 2026) == [
            "2026-03-17", "2026-03-18", "2026-03-21"
        ]

    def test_cross_month_range(self):
        result = parse_shooting_dates("30/3-2/4", 2026)
        assert result == ["2026-03-30", "2026-03-31", "2026-04-01", "2026-04-02"]

    def test_cross_year_range(self):
        result = parse_shooting_dates("28/12-3/1", 2026)
        assert result[0] == "2026-12-28"
        assert result[-1] == "2027-01-03"
        assert len(result) == 7

    def test_empty_string(self):
        assert parse_shooting_dates("", 2026) == []

    def test_none(self):
        assert parse_shooting_dates(None, 2026) == []

    def test_garbage_input(self):
        assert parse_shooting_dates("not a date", 2026) == []

    def test_whitespace_handling(self):
        assert parse_shooting_dates("  17/3 - 19/3  ", 2026) == [
            "2026-03-17", "2026-03-18", "2026-03-19"
        ]

    def test_deduplication(self):
        result = parse_shooting_dates("17/3, 17/3", 2026)
        assert result == ["2026-03-17"]


from skills.job_tracker import get_quarter, sanitize_for_path, sanitize_for_markdown


class TestGetQuarter:
    def test_q1(self):
        assert get_quarter("2026-02-15") == "1T_2026"
    def test_q2(self):
        assert get_quarter("2026-05-01") == "2T_2026"
    def test_q3(self):
        assert get_quarter("2026-09-30") == "3T_2026"
    def test_q4(self):
        assert get_quarter("2026-12-31") == "4T_2026"
    def test_boundary_march(self):
        assert get_quarter("2026-03-31") == "1T_2026"
    def test_boundary_april(self):
        assert get_quarter("2026-04-01") == "2T_2026"


class TestSanitizeForPath:
    def test_normal(self):
        assert sanitize_for_path("NETFLIX-260312") == "NETFLIX-260312"
    def test_spaces(self):
        assert sanitize_for_path("MY PROJECT") == "MY-PROJECT"
    def test_path_traversal(self):
        result = sanitize_for_path("../../etc/passwd")
        assert ".." not in result
        assert "/" not in result
    def test_special_chars(self):
        result = sanitize_for_path('test<>:"|?*file')
        assert "<" not in result
        assert ">" not in result
    def test_empty(self):
        assert sanitize_for_path("") == ""


class TestSanitizeForMarkdown:
    def test_normal_text(self):
        assert sanitize_for_markdown("Hello World") == "Hello World"
    def test_brackets(self):
        result = sanitize_for_markdown("[link](url)")
        assert "[" not in result or "\\[" in result
    def test_pipe(self):
        result = sanitize_for_markdown("col1 | col2")
        assert "\\|" in result


from skills.job_tracker import (
    render_job_note, ensure_job,
    _preserve_manual_content, _extract_section, _has_real_content,
    MANUAL_MARKER,
)
import connector


class TestEnsureJob:
    """Test job upsert logic. Requires DB connection."""

    def setup_method(self):
        connector.init_db()
        self.conn = connector.get_db()
        self.cur = connector._cursor(self.conn)

    def teardown_method(self):
        self.cur.execute(connector._q("DELETE FROM jobs WHERE project_code LIKE 'TEST-%'"))
        self.cur.execute(connector._q("DELETE FROM job_note_queue WHERE project_code LIKE 'TEST-%'"))
        self.conn.commit()
        connector.release_db(self.conn)

    def test_create_new_job(self):
        doc_data = {
            "client_id": "contact123",
            "client_name": "Test Client",
            "shooting_dates_raw": "17/3-21/3",
            "estimate_id": "est123",
            "estimate_number": "QUOTE-26/TEST",
            "invoice_id": None,
            "invoice_number": None,
            "doc_date": 1742169600,
        }
        result = ensure_job("TEST-260317", doc_data, self.cur)
        self.conn.commit()
        assert result["project_code"] == "TEST-260317"
        assert result["status"] == "open"
        assert result["estimate_id"] == "est123"

    def test_update_existing_job_with_invoice(self):
        doc_data = {
            "client_id": "c1", "client_name": "Client",
            "shooting_dates_raw": "1/4", "estimate_id": "est1",
            "estimate_number": "Q-1", "invoice_id": None,
            "invoice_number": None, "doc_date": 1742169600,
        }
        ensure_job("TEST-UPDATE", doc_data, self.cur)
        self.conn.commit()

        doc_data2 = {
            "client_id": "c1", "client_name": "Client",
            "shooting_dates_raw": "1/4", "estimate_id": None,
            "estimate_number": None, "invoice_id": "inv1",
            "invoice_number": "INV-1", "doc_date": 1742169600,
        }
        result = ensure_job("TEST-UPDATE", doc_data2, self.cur)
        self.conn.commit()

        assert result["estimate_id"] == "est1"  # preserved
        assert result["invoice_id"] == "inv1"    # added
        assert result["status"] == "invoiced"    # auto-transition


class TestRenderJobNote:
    def test_basic_render(self):
        job = {
            "project_code": "NETFLIX-260315",
            "client_name": "Netflix Spain",
            "client_email": "prod@netflix.com",
            "status": "open",
            "shooting_dates_raw": "15/3-18/3",
            "shooting_dates": '["2026-03-15", "2026-03-16", "2026-03-17", "2026-03-18"]',
            "created_at": "2026-03-12",
            "estimate_id": "abc123",
            "estimate_number": "QUOTE-26/0015",
            "invoice_id": None,
            "invoice_number": None,
        }
        result = render_job_note(job, expenses=[])
        assert "NETFLIX-260315" in result
        assert "Netflix Spain" in result
        assert "prod@netflix.com" in result
        assert "project_code:" in result and "NETFLIX-260315" in result
        assert "## Quote" in result
        assert "## Expenses" in result
        assert "## Invoicing Checklist" in result

    def test_with_expenses(self):
        job = {
            "project_code": "TEST-260101",
            "client_name": "Test Client",
            "client_email": "",
            "status": "shooting",
            "shooting_dates_raw": "1/1",
            "shooting_dates": '["2026-01-01"]',
            "created_at": "2026-01-01",
            "estimate_id": None,
            "estimate_number": None,
            "invoice_id": None,
            "invoice_number": None,
        }
        expenses = [
            {"date": 1704067200, "name": "Taxi", "amount": 25.0, "doc_number": "EXP-001"},
            {"date": 1704153600, "name": "Lunch", "amount": 18.5, "doc_number": "EXP-002"},
        ]
        result = render_job_note(job, expenses)
        assert "Taxi" in result
        assert "25" in result
        assert "43.5" in result or "43.50" in result

    def test_status_emoji(self):
        for status, emoji in [("open", "🟢"), ("shooting", "🎬"), ("invoiced", "📄"), ("closed", "✅")]:
            job = {
                "project_code": "T-1", "client_name": "", "client_email": "",
                "status": status, "shooting_dates_raw": "", "shooting_dates": "[]",
                "created_at": "", "estimate_id": None, "estimate_number": None,
                "invoice_id": None, "invoice_number": None,
            }
            result = render_job_note(job, [])
            assert emoji in result


class TestPreserveManualContent:
    """Verify that manual edits above and below MANUAL_MARKER survive re-sync."""

    def _make_job(self, code="TEST-1"):
        return {
            "project_code": code, "client_name": "Client", "client_email": "a@b.com",
            "status": "open", "shooting_dates_raw": "1/3", "shooting_dates": '["2026-03-01"]',
            "created_at": "2026-03-01", "estimate_id": "est1", "estimate_number": "Q-1",
            "invoice_id": None, "invoice_number": None,
        }

    def test_preserves_manual_expenses(self):
        """Manual expenses in existing note survive when DB has no expenses."""
        new_content = render_job_note(self._make_job(), expenses=[])
        assert "*No expenses yet*" in new_content

        # Simulate existing note with hand-edited expenses
        existing = new_content.replace(
            "| — | *No expenses yet* | — | — |",
            "| 25-3-2026 | taxi Shelphy | 14,75 | — |\n| — | canon selphy | — | — |"
        )
        merged = _preserve_manual_content(new_content, existing)

        assert "taxi Shelphy" in merged
        assert "canon selphy" in merged
        assert "*No expenses yet*" not in merged

    def test_db_expenses_override_manual(self):
        """When DB has real expenses, they take priority over manual ones."""
        expenses = [{"date": 1742169600, "name": "Hotel", "amount": 120.0, "doc_number": "E-1"}]
        new_content = render_job_note(self._make_job(), expenses=expenses)
        assert "Hotel" in new_content

        # Existing note had manual expenses
        old_content = render_job_note(self._make_job(), expenses=[]).replace(
            "| — | *No expenses yet* | — | — |",
            "| 25-3-2026 | taxi | 15 | — |"
        )
        merged = _preserve_manual_content(new_content, old_content)

        # DB expenses win because new_content has real content
        assert "Hotel" in merged

    def test_preserves_manual_email_notes(self):
        """Hand-edited Email Thread section survives re-sync."""
        new_content = render_job_note(self._make_job(), expenses=[])

        # Simulate user adding notes to Email Thread
        existing = new_content.replace(
            "- **Client contact:** a@b.com",
            "- **Client contact:** custom@email.com\n- Contacto alternativo: manager@company.com"
        )
        merged = _preserve_manual_content(new_content, existing)

        assert "custom@email.com" in merged
        assert "manager@company.com" in merged

    def test_preserves_below_marker(self):
        """Content below MANUAL_MARKER is always preserved."""
        new_content = render_job_note(self._make_job(), expenses=[])

        existing = new_content.replace(
            "### Items to Add\n<!-- + Product x2 @180€  or  + Transport 150€  or  free text -->",
            "### Items to Add\n+ Sony FX3 x1 @150€\n+ LED Panel x2 @80€"
        )
        merged = _preserve_manual_content(new_content, existing)

        assert "Sony FX3" in merged
        assert "LED Panel" in merged

    def test_no_existing_note(self):
        """First sync (no existing note) returns new content unchanged."""
        new_content = render_job_note(self._make_job(), expenses=[])
        merged = _preserve_manual_content(new_content, None)
        assert merged == new_content

    def test_existing_without_marker(self):
        """Old-format notes without MANUAL_MARKER still get sections preserved."""
        new_content = render_job_note(self._make_job(), expenses=[])

        # Old note without marker but with manual expenses
        old_note = """---
project_code: TEST-1
---

# TEST-1

## Expenses & Tickets

| Date | Concept | Amount | Source |
|------|---------|--------|--------|
| 25-3-2026 | taxi | 14,75 | — |

**Total expenses:** €14.75

## Email Thread
- **Client contact:** custom@email.com

## Notes

Some important notes here
"""
        merged = _preserve_manual_content(new_content, old_note)

        # Expenses should be preserved (old note had real content, new has default)
        assert "taxi" in merged
        assert "14,75" in merged


class TestExtractSection:
    def test_extract_expenses(self):
        content = "## Quote\nstuff\n## Expenses & Tickets\n| a | b |\n\n## Email Thread\nmore"
        section = _extract_section(content, "## Expenses & Tickets", ["## Email Thread"])
        assert "| a | b |" in section
        assert "more" not in section

    def test_missing_section(self):
        content = "## Quote\nstuff\n## Email Thread\nmore"
        section = _extract_section(content, "## Expenses & Tickets", ["## Email Thread"])
        assert section is None


class TestHasRealContent:
    def test_empty_markers(self):
        assert not _has_real_content("| — | *No expenses yet* | — | — |", ["*No expenses yet*"])

    def test_real_content(self):
        assert _has_real_content("| 25-3 | taxi | 15 | — |", ["*No expenses yet*"])

    def test_none_input(self):
        assert not _has_real_content(None, ["*No expenses yet*"])
