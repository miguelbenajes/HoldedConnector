"""
Probe script for Holded document attachment API.

Tests the undocumented behavior of:
  POST /documents/{docType}/{documentId}/attachments  (upload)
  GET  /documents/{docType}/{documentId}/attachments  (list)
  DELETE attachment (if supported)

Run: cd services/holded-connector && python3 scripts/probe_attach_api.py

Creates a test purchase (0.01€), runs attachment tests, then cleans up.
Outputs results as JSON to stdout, human-readable summary to stderr.
"""

import io
import json
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("HOLDED_API_KEY")
BASE = "https://api.holded.com/api/invoicing/v1"
HEADERS_JSON = {"key": API_KEY, "Content-Type": "application/json"}
HEADERS_KEY_ONLY = {"key": API_KEY}  # for multipart — let requests set Content-Type

if not API_KEY:
    print("ERROR: HOLDED_API_KEY not set in .env", file=sys.stderr)
    sys.exit(1)


def log(msg):
    print(f"  {msg}", file=sys.stderr)


def api_get(path, params=None):
    url = f"{BASE}{path}"
    r = requests.get(url, headers=HEADERS_JSON, params=params, timeout=30)
    return r


def api_post_json(path, payload):
    url = f"{BASE}{path}"
    r = requests.post(url, headers=HEADERS_JSON, json=payload, timeout=30)
    return r


def api_post_multipart(path, files, data=None):
    url = f"{BASE}{path}"
    r = requests.post(url, headers=HEADERS_KEY_ONLY, files=files, data=data, timeout=60)
    return r


def api_delete(path):
    url = f"{BASE}{path}"
    r = requests.delete(url, headers=HEADERS_JSON, timeout=30)
    return r


# ── Test helpers ────────────────────────────────────────────────────────────

def make_test_jpeg(size_bytes=1024):
    """Create a minimal valid JPEG for testing."""
    # Minimal JPEG: SOI + APP0 + minimal frame + EOI
    # For size testing, pad with comment segments
    # This creates a ~1KB valid JPEG
    import struct

    buf = io.BytesIO()
    # SOI
    buf.write(b'\xff\xd8')
    # APP0 (JFIF header)
    buf.write(b'\xff\xe0')
    buf.write(struct.pack('>H', 16))  # length
    buf.write(b'JFIF\x00')
    buf.write(b'\x01\x01')  # version
    buf.write(b'\x00')  # units
    buf.write(struct.pack('>HH', 1, 1))  # density
    buf.write(b'\x00\x00')  # thumbnail

    # Pad with COM (comment) segments to reach target size
    current = buf.tell()
    remaining = size_bytes - current - 2  # -2 for EOI
    while remaining > 4:
        chunk = min(remaining - 4, 65533)  # max segment = 65535 - 2
        buf.write(b'\xff\xfe')  # COM marker
        buf.write(struct.pack('>H', chunk + 2))  # segment length includes itself
        buf.write(b'X' * chunk)
        remaining -= (chunk + 4)

    # EOI
    buf.write(b'\xff\xd9')
    return buf.getvalue()


def make_test_pdf():
    """Create a minimal valid PDF."""
    return (
        b"%PDF-1.0\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/MediaBox[0 0 3 3]/Parent 2 0 R>>endobj\n"
        b"xref\n0 4\n"
        b"0000000000 65535 f \n"
        b"0000000009 00000 n \n"
        b"0000000058 00000 n \n"
        b"0000000115 00000 n \n"
        b"trailer<</Size 4/Root 1 0 R>>\n"
        b"startxref\n183\n%%EOF\n"
    )


# ── Main probe ──────────────────────────────────────────────────────────────

def main():
    results = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tests": {},
        "summary": {},
    }

    # ── Step 1: Create test purchase ────────────────────────────────────────
    print("\n=== HOLDED ATTACHMENT API PROBE ===\n", file=sys.stderr)
    log("Step 1: Creating test purchase (0.01€)...")

    purchase_payload = {
        "contactName": "TEST_PROBE_CLEANUP",
        "date": int(time.time()),
        "desc": "Probe test — safe to delete",
        "items": [
            {"name": "Probe test item", "units": 1, "subtotal": 0.01, "tax": 0}
        ],
    }

    r = api_post_json("/documents/purchase", purchase_payload)
    if r.status_code not in (200, 201):
        log(f"FATAL: Cannot create test purchase: {r.status_code} {r.text[:200]}")
        results["tests"]["create_purchase"] = {
            "status": "FAILED",
            "http_status": r.status_code,
            "body": r.text[:500],
        }
        print(json.dumps(results, indent=2))
        sys.exit(1)

    create_resp = r.json()
    purchase_id = create_resp.get("id")
    log(f"  Created purchase: {purchase_id}")
    results["tests"]["create_purchase"] = {
        "status": "OK",
        "http_status": r.status_code,
        "response_keys": list(create_resp.keys()),
        "id": purchase_id,
    }

    if not purchase_id:
        log("FATAL: No purchase ID in response")
        print(json.dumps(results, indent=2))
        sys.exit(1)

    # Small delay to let Holded process
    time.sleep(1)

    # ── Step 2: Test JPEG attachment ────────────────────────────────────────
    log("Step 2: Attaching small JPEG (1KB)...")

    test_jpeg = make_test_jpeg(1024)
    files = {"file": ("test_probe.jpg", test_jpeg, "image/jpeg")}

    # Try the documented endpoint path
    attach_paths = [
        f"/documents/purchase/{purchase_id}/attachments",
        f"/documents/purchase/{purchase_id}/attach",
    ]

    attach_result = None
    working_path = None

    for path in attach_paths:
        log(f"  Trying: POST {path}")
        r = api_post_multipart(path, files={"file": ("test_probe.jpg", test_jpeg, "image/jpeg")})
        log(f"    → {r.status_code}")
        try:
            body = r.json()
        except Exception:
            body = r.text[:500]

        results["tests"][f"attach_jpeg_{path.split('/')[-1]}"] = {
            "path": path,
            "http_status": r.status_code,
            "response_headers": dict(r.headers),
            "body": body,
        }

        if r.status_code in (200, 201):
            attach_result = body
            working_path = path
            log(f"  ✓ SUCCESS on {path}")
            break
        else:
            log(f"  ✗ Failed: {r.status_code} — {str(body)[:200]}")

    if not working_path:
        log("WARNING: Neither attachment path worked. Trying with 'attachment' field name...")
        # Try alternative field names
        for field_name in ["attachment", "document", "upload"]:
            r = api_post_multipart(
                attach_paths[0],
                files={field_name: ("test_probe.jpg", test_jpeg, "image/jpeg")},
            )
            log(f"  Field '{field_name}': {r.status_code}")
            results["tests"][f"attach_field_{field_name}"] = {
                "http_status": r.status_code,
                "body": r.text[:500],
            }
            if r.status_code in (200, 201):
                working_path = attach_paths[0]
                attach_result = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:500]
                log(f"  ✓ SUCCESS with field name '{field_name}'")
                results["summary"]["field_name"] = field_name
                break

    if working_path:
        results["summary"]["working_endpoint"] = working_path
        results["summary"]["attach_works"] = True
    else:
        results["summary"]["attach_works"] = False
        log("\n⚠️  ATTACHMENT ENDPOINT NOT WORKING — cannot proceed with further tests")

    # ── Step 3: List attachments ────────────────────────────────────────────
    if working_path:
        log("Step 3: Listing attachments...")
        list_path = working_path  # same path, GET instead of POST
        r = api_get(list_path)
        log(f"  GET {list_path}: {r.status_code}")
        try:
            body = r.json()
        except Exception:
            body = r.text[:500]

        results["tests"]["list_attachments"] = {
            "http_status": r.status_code,
            "body": body,
        }

        # Also try without the 's'
        alt_list = list_path.rstrip("s") if list_path.endswith("ments") else list_path + "s"
        r2 = api_get(alt_list)
        log(f"  GET {alt_list}: {r2.status_code}")

    # ── Step 4: Test PDF attachment ─────────────────────────────────────────
    if working_path:
        log("Step 4: Attaching PDF...")
        test_pdf = make_test_pdf()
        field = results["summary"].get("field_name", "file")
        r = api_post_multipart(
            working_path,
            files={field: ("test_probe.pdf", test_pdf, "application/pdf")},
        )
        log(f"  → {r.status_code}")
        try:
            body = r.json()
        except Exception:
            body = r.text[:500] or "(empty body)"
        results["tests"]["attach_pdf"] = {
            "http_status": r.status_code,
            "body": body,
        }

    # ── Step 5: Test PNG attachment ─────────────────────────────────────────
    if working_path:
        log("Step 5: Attaching PNG (minimal 1x1)...")
        # Minimal 1x1 PNG
        png_data = (
            b'\x89PNG\r\n\x1a\n'
            b'\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde'
            b'\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N'
            b'\x00\x00\x00\x00IEND\xaeB`\x82'
        )
        field = results["summary"].get("field_name", "file")
        r = api_post_multipart(
            working_path,
            files={field: ("test_probe.png", png_data, "image/png")},
        )
        log(f"  → {r.status_code}")
        try:
            body = r.json()
        except Exception:
            body = r.text[:500] or "(empty body)"
        results["tests"]["attach_png"] = {
            "http_status": r.status_code,
            "body": body,
        }

    # ── Step 6: Test size limits ────────────────────────────────────────────
    if working_path:
        log("Step 6: Testing size limits...")
        field = results["summary"].get("field_name", "file")
        sizes_mb = [1, 5, 10]
        for size_mb in sizes_mb:
            log(f"  Testing {size_mb}MB...")
            big_jpeg = make_test_jpeg(size_mb * 1024 * 1024)
            r = api_post_multipart(
                working_path,
                files={field: (f"test_{size_mb}mb.jpg", big_jpeg, "image/jpeg")},
            )
            log(f"    → {r.status_code}")
            results["tests"][f"size_{size_mb}mb"] = {
                "http_status": r.status_code,
                "success": r.status_code in (200, 201),
            }
            if r.status_code not in (200, 201):
                results["summary"]["max_size_mb"] = sizes_mb[sizes_mb.index(size_mb) - 1] if size_mb > 1 else 0
                log(f"  Size limit found: {size_mb}MB fails")
                break
            time.sleep(0.5)  # be gentle with rate limits
        else:
            results["summary"]["max_size_mb"] = f">={sizes_mb[-1]}"

    # ── Step 7: Test other doc types ────────────────────────────────────────
    log("Step 7: Testing attachment on invoice docType (using existing if any)...")
    # Fetch a recent invoice to test
    r = api_get("/documents/invoice", params={"limit": 1})
    if r.status_code == 200:
        invoices = r.json()
        if isinstance(invoices, list) and len(invoices) > 0:
            test_invoice_id = invoices[0].get("id")
            log(f"  Using invoice {test_invoice_id}")
            # DON'T actually attach to a real invoice — just test GET attachments
            list_endpoint = working_path.replace(f"purchase/{purchase_id}", f"invoice/{test_invoice_id}") if working_path else None
            if list_endpoint:
                r2 = api_get(list_endpoint)
                log(f"  GET attachments on invoice: {r2.status_code}")
                try:
                    body2 = r2.json()
                except Exception:
                    body2 = r2.text[:200] or "(empty body)"
                results["tests"]["list_invoice_attachments"] = {
                    "http_status": r2.status_code,
                    "body": body2,
                }

    # ── Step 8: Test delete attachment ──────────────────────────────────────
    if working_path and attach_result:
        log("Step 8: Testing delete attachment...")
        # Try to find attachment ID from attach_result
        att_id = None
        if isinstance(attach_result, dict):
            att_id = attach_result.get("id") or attach_result.get("attachmentId") or attach_result.get("_id")

        if att_id:
            # Try DELETE on the attachment
            delete_paths = [
                f"{working_path}/{att_id}",
                f"/documents/purchase/{purchase_id}/attachments/{att_id}",
            ]
            for dp in delete_paths:
                r = api_delete(dp)
                log(f"  DELETE {dp}: {r.status_code}")
                results["tests"]["delete_attachment"] = {
                    "path": dp,
                    "http_status": r.status_code,
                    "body": r.text[:500],
                }
                if r.status_code in (200, 204):
                    results["summary"]["delete_works"] = True
                    break
            else:
                results["summary"]["delete_works"] = False
        else:
            log("  No attachment ID found in response — cannot test delete")
            results["summary"]["delete_works"] = "unknown (no ID in attach response)"

    # ── Cleanup: Delete test purchase ───────────────────────────────────────
    log("\nCleanup: Deleting test purchase...")
    r = api_delete(f"/documents/purchase/{purchase_id}")
    log(f"  → {r.status_code}")
    results["tests"]["cleanup_delete"] = {
        "http_status": r.status_code,
    }

    # Also try to delete the test contact that was auto-created
    log("Cleanup: Searching for test contact...")
    r = api_get("/contacts", params={"limit": 500})
    if r.status_code == 200:
        contacts = r.json()
        if isinstance(contacts, list):
            test_contacts = [c for c in contacts if c.get("name") == "TEST_PROBE_CLEANUP"]
            for tc in test_contacts:
                log(f"  Deleting test contact {tc['id']}...")
                api_delete(f"/contacts/{tc['id']}")

    # ── Summary ─────────────────────────────────────────────────────────────
    results["summary"].setdefault("field_name", "file")
    results["summary"].setdefault("attach_works", False)
    results["summary"].setdefault("delete_works", False)

    print("\n=== PROBE RESULTS ===\n", file=sys.stderr)
    for key, val in results["summary"].items():
        log(f"  {key}: {val}")

    print(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()
