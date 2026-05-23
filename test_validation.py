"""
test_validation.py
==================
Quick sanity tests for stage1_validation.py — run before deploying.

Usage:
  python test_validation.py

These tests catch the exact logic errors that the audit identified, so we
can verify they're actually fixed.
"""

from datetime import date, timedelta
from stage1_validation import (
    validate_campaign_input,
    validate_email,
    validate_cpm,
    validate_flight_dates,
    normalize_brand,
    normalize_email,
)


def assert_valid(data, label):
    is_valid, errors = validate_campaign_input(data)
    if not is_valid:
        print(f"  ✗ FAIL: {label}")
        for e in errors:
            print(f"      - {e}")
        return False
    print(f"  ✓ PASS: {label}")
    return True


def assert_invalid(data, label, expected_error_substring=None):
    is_valid, errors = validate_campaign_input(data)
    if is_valid:
        print(f"  ✗ FAIL: {label} (expected errors, got none)")
        return False

    if expected_error_substring:
        found = any(expected_error_substring.lower() in e.lower() for e in errors)
        if not found:
            print(f"  ✗ FAIL: {label}")
            print(f"      Expected error containing: '{expected_error_substring}'")
            print(f"      Got: {errors}")
            return False

    print(f"  ✓ PASS: {label}")
    return True


def base_valid_campaign():
    """A baseline valid campaign — tests modify one field at a time."""
    today = date.today()
    return {
        "brand": "Nike",
        "app_name": "Clash Royale",
        "vertical": "Gaming",
        "campaign_type": "Outreach",
        "cpm_floor": 5.00,
        "cpm_offer": 12.00,
        "flight_start": today + timedelta(days=7),
        "flight_end": today + timedelta(days=37),
        "recipient_email": "publisher@example.com",
        "priority_tier": "Medium",
        "publisher_segment": "All",
        "variant_strategy": "Sequential",
    }


def test_baseline():
    print("\n--- Test: baseline valid campaign ---")
    assert_valid(base_valid_campaign(), "baseline valid campaign")


def test_cpm_corrections():
    """Audit error 1.1: CPM should be USD, range $0.10–$50.00"""
    print("\n--- Test: CPM corrections (audit error 1.1) ---")

    # Was previously broken: 12.00 would be rejected as "> $1.00"
    data = base_valid_campaign()
    data["cpm_floor"] = 5.00
    data["cpm_offer"] = 12.00
    assert_valid(data, "$5/$12 CPM is valid (was broken in original)")

    # Lower bound
    data["cpm_floor"] = 0.05
    data["cpm_offer"] = 1.00
    assert_invalid(data, "$0.05 CPM rejected (below $0.10 floor)", "cpm floor")

    # Upper bound
    data = base_valid_campaign()
    data["cpm_offer"] = 75.00
    assert_invalid(data, "$75 CPM rejected (above $50 ceiling)", "cpm offer")

    # Offer < floor
    data = base_valid_campaign()
    data["cpm_floor"] = 20.00
    data["cpm_offer"] = 10.00
    assert_invalid(data, "Offer < floor rejected", "offer cpm")


def test_flight_dates_by_type():
    """Audit error 1.2: future-start rule applies ONLY to Outreach"""
    print("\n--- Test: Flight date rules by campaign type (audit error 1.2) ---")

    today = date.today()
    past_start = today - timedelta(days=30)
    past_end = today - timedelta(days=5)

    # Outreach with past start → should fail
    data = base_valid_campaign()
    data["campaign_type"] = "Outreach"
    data["flight_start"] = past_start
    data["flight_end"] = past_end
    assert_invalid(data, "Outreach with past start rejected", "future start")

    # FollowUp with past flight → should pass
    data = base_valid_campaign()
    data["campaign_type"] = "FollowUp"
    data["flight_start"] = past_start
    data["flight_end"] = past_end
    assert_valid(data, "FollowUp with past flight allowed (was broken in original)")

    # Brief with past flight → should pass
    data = base_valid_campaign()
    data["campaign_type"] = "Brief"
    data["flight_start"] = past_start
    data["flight_end"] = past_end
    assert_valid(data, "Brief with past flight allowed")


def test_email_validation():
    """Audit error 1.3: recipient email validation was missing entirely"""
    print("\n--- Test: Email validation (audit error 1.3) ---")

    # Valid emails
    assert validate_email("publisher@example.com")[0]
    assert validate_email("first.last+tag@sub.example.co.uk")[0]
    print("  ✓ PASS: standard emails accepted")

    # Invalid format
    assert not validate_email("not-an-email")[0]
    assert not validate_email("@example.com")[0]
    assert not validate_email("test@")[0]
    print("  ✓ PASS: malformed emails rejected")

    # Typo TLDs
    assert not validate_email("test@example.con")[0]
    assert not validate_email("test@example.cmo")[0]
    print("  ✓ PASS: .con/.cmo typos caught")

    # Role-based
    is_valid, err = validate_email("admin@example.com")
    assert not is_valid and "role" in err.lower()
    is_valid, err = validate_email("noreply@example.com")
    assert not is_valid and "role" in err.lower()
    print("  ✓ PASS: role-based emails rejected")

    # Full campaign with bad email
    data = base_valid_campaign()
    data["recipient_email"] = "not-an-email"
    assert_invalid(data, "Campaign with malformed email rejected", "invalid email format")

    data = base_valid_campaign()
    data["recipient_email"] = ""
    assert_invalid(data, "Campaign with empty email rejected", "email")


def test_flight_duration_cap():
    """Audit error 1.4: 180-day cap (not 365)"""
    print("\n--- Test: Flight duration cap (audit error 1.4) ---")

    today = date.today()

    # 90 days OK
    data = base_valid_campaign()
    data["flight_start"] = today + timedelta(days=1)
    data["flight_end"] = today + timedelta(days=91)
    assert_valid(data, "90-day flight allowed")

    # 200 days rejected
    data["flight_end"] = today + timedelta(days=201)
    assert_invalid(data, "200-day flight rejected", "max 180")


def test_vertical_enum():
    print("\n--- Test: Vertical enum validation ---")

    data = base_valid_campaign()
    data["vertical"] = "Gaming"
    assert_valid(data, "Gaming is valid vertical")

    data["vertical"] = "NotARealVertical"
    assert_invalid(data, "Unknown vertical rejected", "vertical must be")

    data["vertical"] = ""
    assert_invalid(data, "Empty vertical rejected", "vertical")


def test_brand_normalization():
    """Audit error 3.2: brand matching must be case-insensitive"""
    print("\n--- Test: Brand normalization (audit error 3.2) ---")

    assert normalize_brand("Nike") == "nike"
    assert normalize_brand("NIKE") == "nike"
    assert normalize_brand(" Nike ") == "nike"
    assert normalize_brand("Nike Inc") == "nike"
    assert normalize_brand("Nike LLC") == "nike"
    assert normalize_brand("Adidas Corp.") == "adidas"
    print("  ✓ PASS: brand normalization handles case, whitespace, suffixes")


def test_email_normalization():
    print("\n--- Test: Email normalization ---")

    assert normalize_email("Test@Example.COM") == "test@example.com"
    assert normalize_email("  test@example.com  ") == "test@example.com"
    print("  ✓ PASS: email normalization works")


def run_all():
    print("=" * 60)
    print("Stage 1 Validation — Logic Audit Test Suite")
    print("=" * 60)

    test_baseline()
    test_cpm_corrections()
    test_flight_dates_by_type()
    test_email_validation()
    test_flight_duration_cap()
    test_vertical_enum()
    test_brand_normalization()
    test_email_normalization()

    print("\n" + "=" * 60)
    print("All tests complete.")
    print("=" * 60)


if __name__ == "__main__":
    run_all()
