"""
test_stage2_integration.py
============================
Integration tests for stage2_variants.py with mocked Stage 1 / Sheets dependencies.

These run WITHOUT Streamlit/Sheets — they use monkeypatching to stub out
data sources. Covers the orchestration logic and audit error fixes.

Run with:
  python test_stage2_integration.py
"""

import sys
from unittest.mock import patch
from datetime import date


# ============================================================================
# MOCK FIXTURES
# ============================================================================

MOCK_CAMPAIGN_FULL = {
    "campaign_id": "test-campaign-001",
    "campaign_type": "Outreach",
    "brand": "Nike",
    "app_name": "Clash Royale",
    "vertical": "Gaming",
    "target_geo": "US",
    "cpm_floor": 5.00,
    "cpm_offer": 12.00,
    "flight_start": "2026-06-01",
    "flight_end": "2026-06-30",
    "recipient_email": "alice@example.com",
}

MOCK_CAMPAIGN_NO_GEO = {
    **MOCK_CAMPAIGN_FULL,
    "target_geo": "",  # Missing — test fallback
}

MOCK_PUBLISHER_FULL = {
    "publisher_email": "alice@example.com",
    "first_name": "Alice",
    "last_name": "Chen",
    "publisher_name": "Acme Mobile",
    "publisher_tier": "Tier1",
}

MOCK_PUBLISHER_EMPTY = {}  # Returned as fallback when publisher not found

MOCK_CPM_RATES = [
    {"vertical": "Gaming", "ad_format": "Banner",       "geo": "US", "cpm_floor": 0.50,  "cpm_ceiling": 1.50,  "notes": ""},
    {"vertical": "Gaming", "ad_format": "Interstitial", "geo": "US", "cpm_floor": 5.00,  "cpm_ceiling": 12.00, "notes": ""},
    {"vertical": "Gaming", "ad_format": "Rewarded",     "geo": "US", "cpm_floor": 15.00, "cpm_ceiling": 25.00, "notes": ""},
]


# ============================================================================
# TEST HELPERS
# ============================================================================

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


def setup_mocks():
    """
    Return list of patches to apply.
    Use:
        for p in setup_mocks(): p.start()
        try: ...test code...
        finally: for p in setup_mocks_reverse(): p.stop()
    """
    return [
        patch("stage2_variants.get_campaign", return_value=MOCK_CAMPAIGN_FULL),
        patch("stage2_publishers._load_all_publishers", return_value={
            "alice@example.com": MOCK_PUBLISHER_FULL
        }),
        patch("stage2_cpm_table._load_all_cpm_rates", return_value=MOCK_CPM_RATES),
    ]


# ============================================================================
# Test: Basic generation works end-to-end
# ============================================================================

def test_basic_generation():
    print("\n--- Test: Basic end-to-end generation ---")

    mocks = setup_mocks()
    for p in mocks: p.start()
    try:
        from stage2_variants import generate_variant

        variant = generate_variant(
            campaign_id="test-campaign-001",
            recipient_email="alice@example.com",
            regenerate_count=0,
        )

        assert_true(variant.subject, "subject is non-empty")
        assert_true(variant.body, "body is non-empty")
        assert_true("Nike" in variant.body, "brand substituted in body")
        assert_true("Clash Royale" in variant.body, "app_name substituted in body")
        assert_true("Alice" in variant.body, "first_name substituted from publisher")
        assert_eq(variant.template_id, "outreach_v1", "default template selected by campaign_type")
        assert_eq(variant.publisher_fallback_used, False, "no publisher fallback used (data exists)")
        assert_eq(variant.cpm_table_fallback_used, False, "no CPM fallback used (rates exist)")
        assert_eq(variant.missing_required_variables, [], "no missing required vars")
        assert_true(variant.is_ready_to_send, "variant marked ready to send")

    finally:
        for p in mocks: p.stop()


# ============================================================================
# Test: Determinism (audit error 2.1)
# ============================================================================

def test_determinism():
    print("\n--- Test: Determinism across calls (audit error 2.1) ---")

    mocks = setup_mocks()
    for p in mocks: p.start()
    try:
        from stage2_variants import generate_variant

        v1 = generate_variant("test-campaign-001", "alice@example.com", regenerate_count=0)
        v2 = generate_variant("test-campaign-001", "alice@example.com", regenerate_count=0)
        v3 = generate_variant("test-campaign-001", "alice@example.com", regenerate_count=1)

        assert_eq(v1.subject, v2.subject, "same regen_count → same subject")
        assert_eq(v1.body, v2.body, "same regen_count → same body")
        assert_true(v1.seed == v2.seed, "same regen_count → same seed")

        # Different regen_count usually produces different output
        # (could collide on tiny spin spaces, but template has 729 combos)
        assert_true(
            v1.subject != v3.subject or v1.body != v3.body,
            "different regen_count → different output (usually)"
        )

    finally:
        for p in mocks: p.stop()


# ============================================================================
# Test: Publisher fallback flow (audit error 2.18)
# ============================================================================

def test_publisher_fallback():
    print("\n--- Test: Publisher fallback when not in tab (audit error 2.18) ---")

    # Override: no publishers
    mocks = [
        patch("stage2_variants.get_campaign", return_value=MOCK_CAMPAIGN_FULL),
        patch("stage2_publishers._load_all_publishers", return_value={}),  # EMPTY
        patch("stage2_cpm_table._load_all_cpm_rates", return_value=MOCK_CPM_RATES),
    ]
    for p in mocks: p.start()
    try:
        from stage2_variants import generate_variant

        variant = generate_variant(
            campaign_id="test-campaign-001",
            recipient_email="unknown@example.com",
            regenerate_count=0,
        )

        assert_true(variant.publisher_fallback_used, "fallback flag set when no publisher data")
        assert_true(
            "FIRST_NAME" in variant.publisher_fallback_fields,
            "FIRST_NAME marked as using fallback"
        )
        assert_true(
            "there" in variant.body,  # fallback value
            f"fallback 'there' substituted: '{variant.body[:100]}...'"
        )
        # Should still be ready to send (FIRST_NAME is not required, it's optional)
        assert_true(
            variant.is_ready_to_send,
            "still ready to send (optional var has fallback)"
        )
        # Should have warnings
        assert_true(
            any("fallback" in w.lower() for w in variant.warnings),
            "warning mentions fallback"
        )

    finally:
        for p in mocks: p.stop()


# ============================================================================
# Test: CPM fallback (audit error 2.19)
# ============================================================================

def test_cpm_fallback_no_rates():
    print("\n--- Test: CPM table fallback when no rates (audit error 2.19) ---")

    # No CPM rates at all
    mocks = [
        patch("stage2_variants.get_campaign", return_value=MOCK_CAMPAIGN_FULL),
        patch("stage2_publishers._load_all_publishers", return_value={
            "alice@example.com": MOCK_PUBLISHER_FULL
        }),
        patch("stage2_cpm_table._load_all_cpm_rates", return_value=[]),  # EMPTY
    ]
    for p in mocks: p.start()
    try:
        from stage2_variants import generate_variant

        variant = generate_variant(
            campaign_id="test-campaign-001",
            recipient_email="alice@example.com",
            regenerate_count=0,
        )

        assert_true(variant.cpm_table_fallback_used, "CPM fallback flag set")
        # Fallback line should contain the floor/offer CPM
        assert_true("$5.00" in variant.body, "floor CPM appears in fallback")
        assert_true("$12.00" in variant.body, "offer CPM appears in fallback")
        assert_true(
            "rate card" in variant.body.lower(),
            "fallback mentions rate card available"
        )

    finally:
        for p in mocks: p.stop()


# ============================================================================
# Test: CPM table formatting when rates exist
# ============================================================================

def test_cpm_table_with_rates():
    print("\n--- Test: CPM table rendering when rates exist ---")

    mocks = setup_mocks()
    for p in mocks: p.start()
    try:
        from stage2_variants import generate_variant

        variant = generate_variant(
            campaign_id="test-campaign-001",
            recipient_email="alice@example.com",
            regenerate_count=0,
        )

        # Markdown table headers should appear
        assert_true("| Format" in variant.body, "table header present")
        assert_true("Banner" in variant.body, "Banner format in table")
        assert_true("Interstitial" in variant.body, "Interstitial format in table")
        assert_true("Rewarded" in variant.body, "Rewarded format in table")
        assert_true(variant.cpm_table_fallback_used == False, "no fallback flag")

    finally:
        for p in mocks: p.stop()


# ============================================================================
# Test: Edit detection (audit error 2.15)
# ============================================================================

def test_edit_detection():
    print("\n--- Test: Edit detection (audit error 2.15) ---")

    mocks = setup_mocks()
    for p in mocks: p.start()
    try:
        from stage2_variants import generate_variant, detect_edits

        variant = generate_variant(
            campaign_id="test-campaign-001",
            recipient_email="alice@example.com",
            regenerate_count=0,
        )

        # Unchanged → not edited
        edits = detect_edits(variant, variant.subject, variant.body)
        assert_eq(edits["was_edited"], False, "unchanged variant not flagged as edited")
        assert_eq(edits["subject_edit_distance"], 0, "unchanged subject distance = 0")
        assert_eq(edits["body_edit_distance"], 0, "unchanged body distance = 0")

        # Changed subject only
        edits = detect_edits(variant, variant.subject + " EDIT", variant.body)
        assert_eq(edits["was_edited"], True, "edited subject flagged")
        assert_eq(edits["subject_was_edited"], True, "subject_was_edited true")
        assert_eq(edits["body_was_edited"], False, "body not edited")
        assert_true(edits["subject_edit_distance"] > 0, "subject distance > 0")

    finally:
        for p in mocks: p.stop()


# ============================================================================
# Test: Spin path stored as JSON-serializable structure
# ============================================================================

def test_spin_path_json():
    print("\n--- Test: spin_path_json is JSON-serializable ---")

    mocks = setup_mocks()
    for p in mocks: p.start()
    try:
        from stage2_variants import generate_variant
        import json

        variant = generate_variant(
            campaign_id="test-campaign-001",
            recipient_email="alice@example.com",
            regenerate_count=0,
        )

        json_str = json.dumps(variant.spin_path_json)
        parsed = json.loads(json_str)

        assert_true("subject" in parsed, "spin_path_json has subject key")
        assert_true("body" in parsed, "spin_path_json has body key")
        assert_true(
            all("pos" in item and "text" in item for item in parsed["subject"]),
            "spin path entries have pos and text"
        )

    finally:
        for p in mocks: p.stop()


# ============================================================================
# Test: Different campaign types use correct templates
# ============================================================================

def test_template_selection_by_campaign_type():
    print("\n--- Test: Template auto-selection by campaign_type ---")

    type_to_expected_template = {
        "Outreach": "outreach_v1",
        "FollowUp": "followup_v1",
        "Brief":    "brief_v1",
        "WinBack":  "winback_v1",
    }

    for ctype, expected_tid in type_to_expected_template.items():
        campaign = {**MOCK_CAMPAIGN_FULL, "campaign_type": ctype}
        mocks = [
            patch("stage2_variants.get_campaign", return_value=campaign),
            patch("stage2_publishers._load_all_publishers", return_value={
                "alice@example.com": MOCK_PUBLISHER_FULL
            }),
            patch("stage2_cpm_table._load_all_cpm_rates", return_value=MOCK_CPM_RATES),
        ]
        for p in mocks: p.start()
        try:
            from stage2_variants import generate_variant

            variant = generate_variant(
                campaign_id="test-campaign-001",
                recipient_email="alice@example.com",
                regenerate_count=0,
            )
            assert_eq(variant.template_id, expected_tid, f"{ctype} → {expected_tid}")
        finally:
            for p in mocks: p.stop()


# ============================================================================
# Test: Flight formatting
# ============================================================================

def test_flight_formatting():
    print("\n--- Test: Flight date formatting ---")

    from stage2_variants import _format_flight

    # Same year
    result = _format_flight(date(2026, 6, 1), date(2026, 6, 30))
    assert_true("June 1" in result, f"June 1 in result: '{result}'")
    assert_true("June 30" in result, f"June 30 in result: '{result}'")
    assert_true("2026" in result, f"2026 in result: '{result}'")

    # Different year
    result = _format_flight(date(2026, 12, 20), date(2027, 1, 15))
    assert_true("2026" in result and "2027" in result, f"both years in result: '{result}'")

    # String input
    result = _format_flight("2026-06-01", "2026-06-30")
    assert_true("June" in result, f"string input parsed: '{result}'")

    # Missing
    result = _format_flight(None, None)
    assert_eq(result, "", "missing flight returns empty string")


# ============================================================================
# RUNNER
# ============================================================================

def run_all():
    print("=" * 60)
    print("Stage 2 Integration Test Suite")
    print("=" * 60)

    test_basic_generation()
    test_determinism()
    test_publisher_fallback()
    test_cpm_fallback_no_rates()
    test_cpm_table_with_rates()
    test_edit_detection()
    test_spin_path_json()
    test_template_selection_by_campaign_type()
    test_flight_formatting()

    print("\n" + "=" * 60)
    print("Stage 2 integration tests complete.")
    print("=" * 60)


if __name__ == "__main__":
    run_all()
