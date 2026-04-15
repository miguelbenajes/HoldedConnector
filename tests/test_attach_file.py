"""Tests for file attachment operations.

Unit tests for validation, hashing, sanitization (no API calls).
Integration tests (marked with @pytest.mark.integration) hit real Holded API.
Run integration: pytest tests/test_attach_file.py -m integration -v
"""
import os
import time
import pytest

from app.holded.write_wrappers import (
    attach_file_to_document,
    compute_file_hash,
    _validate_file_magic,
    _sanitize_filename,
    create_purchase,
    ALLOWED_ATTACH_TYPES,
    MAX_ATTACH_SIZE,
)
from app.holded.client import delete_data, fetch_data


# ── Test data ──────────────────────────────────────────────────────────────

TINY_JPEG = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9'
TINY_PNG = (
    b'\x89PNG\r\n\x1a\n'
    b'\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde'
    b'\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N'
    b'\x00\x00\x00\x00IEND\xaeB\x60\x82'
)
TINY_PDF = b'%PDF-1.0\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n'
FAKE_FILE = b'\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09'


# ── Unit tests (no API calls) ─────────────────────────────────────────────

class TestFileValidation:
    """Test file type and magic byte validation."""

    def test_jpeg_magic_valid(self):
        assert _validate_file_magic(TINY_JPEG, "image/jpeg") is True

    def test_png_magic_valid(self):
        assert _validate_file_magic(TINY_PNG, "image/png") is True

    def test_pdf_magic_valid(self):
        assert _validate_file_magic(TINY_PDF, "application/pdf") is True

    def test_wrong_magic_jpeg(self):
        assert _validate_file_magic(TINY_PNG, "image/jpeg") is False

    def test_wrong_magic_pdf(self):
        assert _validate_file_magic(TINY_JPEG, "application/pdf") is False

    def test_random_bytes(self):
        assert _validate_file_magic(FAKE_FILE, "image/jpeg") is False
        assert _validate_file_magic(FAKE_FILE, "image/png") is False
        assert _validate_file_magic(FAKE_FILE, "application/pdf") is False

    def test_empty_file(self):
        assert _validate_file_magic(b'', "image/jpeg") is False

    def test_too_short(self):
        assert _validate_file_magic(b'\xff\xd8', "image/jpeg") is False

    def test_unsupported_type(self):
        assert _validate_file_magic(b'some content', "text/plain") is False


class TestFilenameSanitization:
    """Test filename sanitization."""

    def test_normal_filename(self):
        assert _sanitize_filename("ticket_taxi.jpg") == "ticket_taxi.jpg"

    def test_path_traversal(self):
        assert _sanitize_filename("../../etc/passwd") == "passwd"

    def test_spaces_and_special(self):
        result = _sanitize_filename("my ticket (1).pdf")
        assert ".." not in result
        assert "/" not in result

    def test_unicode(self):
        result = _sanitize_filename("factura_pérez.pdf")
        assert result.endswith(".pdf")

    def test_very_long_filename(self):
        long_name = "a" * 200 + ".jpg"
        result = _sanitize_filename(long_name)
        assert len(result) <= 100

    def test_empty_filename(self):
        assert _sanitize_filename("") == "attachment"

    def test_only_special_chars(self):
        result = _sanitize_filename("()()")
        assert result  # should not be empty


class TestFileHash:
    """Test file hash computation."""

    def test_deterministic(self):
        h1 = compute_file_hash(TINY_JPEG)
        h2 = compute_file_hash(TINY_JPEG)
        assert h1 == h2

    def test_different_content(self):
        h1 = compute_file_hash(TINY_JPEG)
        h2 = compute_file_hash(TINY_PNG)
        assert h1 != h2

    def test_sha256_format(self):
        h = compute_file_hash(TINY_JPEG)
        assert len(h) == 64
        assert all(c in '0123456789abcdef' for c in h)


class TestAttachValidation:
    """Test attach_file_to_document input validation (no API call for invalid inputs)."""

    def test_invalid_doc_type(self):
        result = attach_file_to_document(
            "invalid_type", "aabbccdd11223344aabbccdd",
            "test.jpg", TINY_JPEG, "image/jpeg"
        )
        assert result.get("error") is True
        assert "Invalid doc_type" in result.get("detail", "")

    def test_invalid_content_type(self):
        result = attach_file_to_document(
            "purchase", "aabbccdd11223344aabbccdd",
            "test.txt", b"hello world", "text/plain"
        )
        assert result.get("error") is True

    def test_wrong_magic_bytes(self):
        result = attach_file_to_document(
            "purchase", "aabbccdd11223344aabbccdd",
            "fake.jpg", TINY_PNG, "image/jpeg"
        )
        assert result.get("error") is True

    def test_file_too_large(self):
        big = b'\xff\xd8\xff\xe0' + b'\x00' * (MAX_ATTACH_SIZE + 1)
        result = attach_file_to_document(
            "purchase", "aabbccdd11223344aabbccdd",
            "huge.jpg", big, "image/jpeg"
        )
        assert result.get("error") is True

    def test_empty_file(self):
        result = attach_file_to_document(
            "purchase", "aabbccdd11223344aabbccdd",
            "empty.jpg", b'', "image/jpeg"
        )
        assert result.get("error") is True


# ── Integration tests (real Holded API) ────────────────────────────────────

@pytest.mark.integration
class TestAttachFileReal:
    """Integration tests against real Holded API.

    Run: pytest tests/test_attach_file.py -m integration -v
    Creates a test purchase, attaches files, then cleans up.
    """

    @pytest.fixture(autouse=True)
    def setup_purchase(self):
        """Create a test purchase for attachment tests, cleanup after."""
        result = create_purchase({
            "contactName": "TEST_ATTACH_CLEANUP",
            "date": int(time.time()),
            "items": [{"name": "Attach test", "units": 1, "subtotal": 0.01, "tax": 0}],
        })
        assert isinstance(result, str) and len(result) == 24
        self.purchase_id = result
        time.sleep(0.5)

        yield

        # Cleanup
        delete_data(f"/invoicing/v1/documents/purchase/{self.purchase_id}")
        contacts = fetch_data("/invoicing/v1/contacts")
        for c in contacts:
            if c.get("name") == "TEST_ATTACH_CLEANUP":
                delete_data(f"/invoicing/v1/contacts/{c['id']}")

    def test_attach_jpeg(self):
        result = attach_file_to_document(
            "purchase", self.purchase_id,
            "ticket.jpg", TINY_JPEG, "image/jpeg"
        )
        assert not result.get("error"), f"Attach failed: {result}"
        assert result.get("file_hash")
        assert result.get("filename") == "ticket.jpg"

    def test_attach_pdf(self):
        result = attach_file_to_document(
            "purchase", self.purchase_id,
            "factura.pdf", TINY_PDF, "application/pdf"
        )
        assert not result.get("error"), f"Attach failed: {result}"

    def test_attach_png(self):
        result = attach_file_to_document(
            "purchase", self.purchase_id,
            "photo.png", TINY_PNG, "image/png"
        )
        assert not result.get("error"), f"Attach failed: {result}"
