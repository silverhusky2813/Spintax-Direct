"""
test_stage3_integration.py
============================
Integration tests for Stage 3 modules:
  - presend_checks: idempotency key generation, dedup logic, status mapping
  - queue_writer: row dict assembly, retry handling

Uses unittest.mock to patch Sheets I/O. Pure Python tests.

Run with:
  python test_stage3_integration.py
"""

import sys
from dataclasses import dataclass
from unittest.mock import patch, MagicMock


# ============================================================================
# MOCK FIXTURES
# ============================================================================

MOCK_CAMPAIGN = {
    "campaign_id": "test-001",
    "brand": "Nike",
    "app_name": "Clash Royale",
    "vertical": "Gaming",
    "campaign_type": "Outreach",
    "cpm_floor": 5.00,
    "cpm_offer": 12.00,
    "recipient_email": "alice@example.com",
}


@dataclass
class FakeApprovedVariant:
    """Stub for ApprovedVariant for testing without Stage 2."""
    campaign_id: str = "test-001"
    recipient_email: str = "alice@example.com"
    template_id: str = "outreach_v1"
    template_version: int = 1
    subject: str = "Test subject"
    body: str = "Test body content."
    spin_path_json: dict = None
    was_edited: bool = False
    subject_was_edited: bool = False
    body_was_edited: bool = False
    subject_edit_distance: int = 0
    body_edit_distance: int = 0
    generated_at: str = "2026-05-21T10:00:00Z"
    approved_at: str = "2026-05-21T10:05:00Z"

    def __post_init__(self):
        if self.spin_path_json is None:
            self.spin_path_json = {"subject": [], "body": []}


def assert_eq(actual, expected, label):
    if actual == expected:
        print(f"  ✓ PASS: {label}")
        return True
    print(f"  ✗ FAIL: {label}")
    print(f"      Expected: {expected!r}")
    print(f"      Got:      {actual!r}")
    return False


def assert_true(cond, label):
    if cond:
        print(f"  ✓ PASS: {label}")
        return True
    print(f"  ✗ FAIL: {label}")
    return False


# ============================================================================
# Test: Idempotency key generation
# ============================================================================

def test_idempotency_key():
    print("\n--- Test: Idempotency key generation ---")

    from stage3_presend_checks import make_idempotency_key

    # Same inputs → same key
    k1 = make_idempotency_key("c1", "alice@example.com")
    k2 = make_idempotency_key("c1", "alice@example.com")
    assert_eq(k1, k2, "same inputs → same key")

    # Case-insensitive email
    k3 = make_idempotency_key("c1", "ALICE@EXAMPLE.COM")
    assert_eq(k1, k3, "email case doesn't affect key")

    # Whitespace
    k4 = make_idempotency_key("c1", "  alice@example.com  ")
    assert_eq(k1, k4, "email whitespace doesn't affect key")

    # Different campaign → different key
    k5 = make_idempotency_key("c2", "alice@example.com")
    assert_true(k1 != k5, "different campaign → different key")

    # Different recipient → different key
    k6 = make_idempotency_key("c1", "bob@example.com")
    assert_true(k1 != k6, "different recipient → different key")

    # Key length
    assert_eq(len(k1), 16, "key is 16 hex chars (truncated SHA-256)")


# ============================================================================
# Test: Idempotency check — various existing-row scenarios
# ============================================================================

def test_idempotency_check_no_existing():
    print("\n--- Test: Idempotency check when no existing row ---")

    with patch("stage3_presend_checks._find_existing_email_row", return_value=None):
        from stage3_presend_checks import check_idempotency

        result, existing = check_idempotency("c1", "alice@example.com")
        assert_eq(result.status, "ok", "no existing row → ok")
        assert_eq(existing, None, "no existing row returned")


def test_idempotency_check_queued():
    print("\n--- Test: Idempotency check blocks if existing Queued ---")

    fake_existing = {"status": "Queued", "__row_num": 5}
    with patch("stage3_presend_checks._find_existing_email_row", return_value=fake_existing):
        from stage3_presend_checks import check_idempotency

        result, existing = check_idempotency("c1", "alice@example.com")
        assert_eq(result.status, "block", "Queued existing → block")
        assert_eq(result.can_override, False, "cannot override Queued duplicate")


def test_idempotency_check_sent():
    print("\n--- Test: Idempotency check blocks if existing Sent ---")

    fake_existing = {
        "status": "Sent",
        "__row_num": 5,
        "sent_at": "2026-05-20T15:00:00Z",
    }
    with patch("stage3_presend_checks._find_existing_email_row", return_value=fake_existing):
        from stage3_presend_checks import check_idempotency

        result, existing = check_idempotency("c1", "alice@example.com")
        assert_eq(result.status, "block", "Sent existing → block")
        assert_eq(result.can_override, False, "cannot override Sent")
        assert_true("already sent" in result.detail.lower(), "mentions already sent")


def test_idempotency_check_failed_offers_retry():
    """Audit error 3.15: Failed status should allow retry."""
    print("\n--- Test: Idempotency check offers retry for Failed (audit error 3.15) ---")

    fake_existing = {
        "status": "Failed",
        "__row_num": 5,
        "error_message": "Network timeout",
    }
    with patch("stage3_presend_checks._find_existing_email_row", return_value=fake_existing):
        from stage3_presend_checks import check_idempotency

        result, existing = check_idempotency("c1", "alice@example.com")
        assert_eq(result.status, "warn", "Failed existing → warn (retry)")
        assert_eq(result.can_override, True, "can override Failed (retry)")
        assert_true(existing is not None, "existing row returned for retry")
        assert_true("retry" in result.detail.lower(), "mentions retry")


def test_idempotency_check_bounced_offers_retry():
    print("\n--- Test: Idempotency check offers retry for Bounced ---")

    fake_existing = {"status": "Bounced", "__row_num": 5}
    with patch("stage3_presend_checks._find_existing_email_row", return_value=fake_existing):
        from stage3_presend_checks import check_idempotency

        result, existing = check_idempotency("c1", "alice@example.com")
        assert_eq(result.status, "warn", "Bounced existing → warn (retry)")


# ============================================================================
# Test: Aggregate status logic
# ============================================================================

def test_aggregate_status():
    print("\n--- Test: aggregate_status logic ---")

    from stage3_presend_checks import CheckResult, aggregate_status

    ok = CheckResult(status="ok", title="t", detail="d")
    warn = CheckResult(status="warn", title="t", detail="d")
    block = CheckResult(status="block", title="t", detail="d")

    assert_eq(aggregate_status([ok, ok, ok]), "ok", "all ok → ok")
    assert_eq(aggregate_status([ok, warn, ok]), "warn", "any warn → warn")
    assert_eq(aggregate_status([ok, warn, block]), "block", "any block → block")
    assert_eq(aggregate_status([block]), "block", "single block → block")
    assert_eq(aggregate_status([]), "ok", "empty list → ok")


# ============================================================================
# Test: Queue writer row dict assembly
# ============================================================================

def test_queue_writer_row_dict_new():
    print("\n--- Test: Queue writer row dict (new insert) ---")

    from stage3_queue_writer import _build_row_dict

    approved = FakeApprovedVariant()
    row_dict = _build_row_dict(
        campaign=MOCK_CAMPAIGN,
        approved=approved,
        html_body="<p>HTML version</p>",
        from_account="daniel@premiumads.net",
        existing_row=None,
    )

    assert_eq(row_dict["campaign_id"], "test-001", "campaign_id set")
    assert_eq(row_dict["recipient_email"], "alice@example.com", "recipient normalized")
    assert_eq(row_dict["brand"], "Nike", "brand denormalized")
    assert_eq(row_dict["status"], "Queued", "status = Queued")
    assert_eq(row_dict["attempt_count"], "0", "attempt_count = 0 for new")
    assert_eq(row_dict["from_account"], "daniel@premiumads.net", "from_account set")
    assert_eq(row_dict["html_body"], "<p>HTML version</p>", "html_body set")
    assert_eq(row_dict["error_message"], "", "error_message blank")
    assert_eq(row_dict["sent_at"], "", "sent_at blank")
    assert_true(row_dict["idempotency_key"], "idempotency_key set")
    assert_true(row_dict["queued_at"], "queued_at set")
    assert_true(row_dict["confirmed_at"], "confirmed_at set")
    assert_eq(row_dict["was_edited"], "FALSE", "was_edited = FALSE (string)")


def test_queue_writer_row_dict_retry():
    """Audit error 3.15: retry preserves queued_at, increments attempt_count."""
    print("\n--- Test: Queue writer row dict (retry of Failed row) ---")

    from stage3_queue_writer import _build_row_dict

    existing_row = {
        "queued_at": "2026-05-20T10:00:00Z",
        "attempt_count": "1",
        "status": "Failed",
        "error_message": "Old error",
        "__row_num": 5,
    }

    approved = FakeApprovedVariant()
    row_dict = _build_row_dict(
        campaign=MOCK_CAMPAIGN,
        approved=approved,
        html_body="<p>HTML</p>",
        from_account="daniel@premiumads.net",
        existing_row=existing_row,
    )

    assert_eq(row_dict["queued_at"], "2026-05-20T10:00:00Z", "queued_at preserved from original")
    assert_eq(row_dict["attempt_count"], "2", "attempt_count incremented (1 → 2)")
    assert_eq(row_dict["status"], "Queued", "status reset to Queued on retry")
    assert_eq(row_dict["error_message"], "", "old error cleared on retry")


def test_queue_writer_was_edited_format():
    print("\n--- Test: was_edited serialization (TRUE/FALSE strings) ---")

    from stage3_queue_writer import _build_row_dict

    edited_approved = FakeApprovedVariant(was_edited=True, subject_edit_distance=5)
    row_dict = _build_row_dict(
        campaign=MOCK_CAMPAIGN,
        approved=edited_approved,
        html_body="",
        from_account="x@y.com",
        existing_row=None,
    )
    assert_eq(row_dict["was_edited"], "TRUE", "was_edited=True → 'TRUE'")

    unedited = FakeApprovedVariant(was_edited=False)
    row_dict = _build_row_dict(
        campaign=MOCK_CAMPAIGN,
        approved=unedited,
        html_body="",
        from_account="x@y.com",
        existing_row=None,
    )
    assert_eq(row_dict["was_edited"], "FALSE", "was_edited=False → 'FALSE'")


def test_queue_writer_spin_path_json_serialization():
    print("\n--- Test: spin_path_json serialized as JSON string ---")

    from stage3_queue_writer import _build_row_dict
    import json

    approved = FakeApprovedVariant(spin_path_json={
        "subject": [{"pos": 5, "text": "Hi"}],
        "body": [{"pos": 10, "text": "Hey"}],
    })
    row_dict = _build_row_dict(
        campaign=MOCK_CAMPAIGN,
        approved=approved,
        html_body="",
        from_account="x@y.com",
        existing_row=None,
    )

    # Should be JSON-serializable string
    parsed = json.loads(row_dict["spin_path_json"])
    assert_eq(len(parsed["subject"]), 1, "subject spin path serialized")
    assert_eq(parsed["body"][0]["text"], "Hey", "body spin path content preserved")


# ============================================================================
# Test: Column letter conversion
# ============================================================================

def test_col_letter_conversion():
    print("\n--- Test: Column index to letter conversion ---")

    from stage3_queue_writer import _col_index_to_letter

    assert_eq(_col_index_to_letter(1), "A", "1 → A")
    assert_eq(_col_index_to_letter(26), "Z", "26 → Z")
    assert_eq(_col_index_to_letter(27), "AA", "27 → AA")
    assert_eq(_col_index_to_letter(52), "AZ", "52 → AZ")
    assert_eq(_col_index_to_letter(53), "BA", "53 → BA")


# ============================================================================
# Test: time_utils
# ============================================================================

def test_time_utils():
    print("\n--- Test: time_utils functions ---")

    from time_utils import now_iso, safe_parse_date, format_age

    # now_iso produces a parseable string
    ts = now_iso()
    parsed = safe_parse_date(ts)
    assert_true(parsed is not None, f"now_iso() returns parseable: {ts}")

    # safe_parse_date handles None / empty
    assert_eq(safe_parse_date(None), None, "None → None")
    assert_eq(safe_parse_date(""), None, "empty → None")

    # safe_parse_date handles Apps Script Date format
    apps_script_date = "Mon Apr 22 2026 14:30:00 GMT-0700 (Pacific Daylight Time)"
    parsed = safe_parse_date(apps_script_date)
    assert_true(parsed is not None, f"Apps Script Date parsed: {parsed}")
    assert_eq(parsed.year, 2026, "year extracted correctly")

    # format_age
    assert_eq(format_age(None), "—", "None age → dash")
    assert_eq(format_age(""), "—", "empty age → dash")


# ============================================================================
# RUNNER
# ============================================================================

def run_all():
    print("=" * 60)
    print("Stage 3 Integration Test Suite")
    print("=" * 60)

    test_idempotency_key()
    test_idempotency_check_no_existing()
    test_idempotency_check_queued()
    test_idempotency_check_sent()
    test_idempotency_check_failed_offers_retry()
    test_idempotency_check_bounced_offers_retry()
    test_aggregate_status()
    test_queue_writer_row_dict_new()
    test_queue_writer_row_dict_retry()
    test_queue_writer_was_edited_format()
    test_queue_writer_spin_path_json_serialization()
    test_col_letter_conversion()
    test_time_utils()

    print("\n" + "=" * 60)
    print("Test suite complete.")
    print("=" * 60)


if __name__ == "__main__":
    run_all()
