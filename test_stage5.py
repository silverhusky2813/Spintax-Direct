"""
test_stage5.py
===============
Tests for Stage 5: priority scoring + sender pool selection.

Covers audit errors:
  - 5.12: Single-account exhaustion returns None
  - 5.13: Stateless round-robin rotates on attempt_count
  - 5.16: Priority score — tier dominates, age breaks ties
  - 5.17: Empty sender_accounts falls back to default

Run with:
  python test_stage5.py
"""

from datetime import datetime, timedelta, timezone

from stage5_priority import (
    compute_priority_score,
    sort_rows_by_priority,
    tier_weight,
    to_epoch_seconds,
)
from stage5_sender_pool import (
    SenderAccount,
    _hash_to_index,
    pick_sender_account,
)


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
# Test: Tier weights
# ============================================================================

def test_tier_weights():
    print("\n--- Test: Tier weight mapping ---")

    assert_eq(tier_weight("High"), 3, "High → 3")
    assert_eq(tier_weight("high"), 3, "case-insensitive")
    assert_eq(tier_weight("Medium"), 2, "Medium → 2")
    assert_eq(tier_weight("Low"), 1, "Low → 1")
    assert_eq(tier_weight(""), 2, "empty → default Medium (2)")
    assert_eq(tier_weight("Bogus"), 2, "unknown → default Medium (2)")


# ============================================================================
# Test: Priority score — tier dominates (audit error 5.16)
# ============================================================================

def test_priority_tier_dominates():
    print("\n--- Test: Priority score — tier dominates age (audit error 5.16) ---")

    # A High-tier email queued LATE should still beat a Low-tier queued EARLY
    early = "2026-01-01T00:00:00Z"
    late = "2026-12-31T23:59:59Z"

    high_late = compute_priority_score("High", late)
    low_early = compute_priority_score("Low", early)
    medium_late = compute_priority_score("Medium", late)

    assert_true(high_late > low_early, "High (late) beats Low (early)")
    assert_true(high_late > medium_late, "High beats Medium regardless of age")
    assert_true(medium_late > low_early, "Medium (late) beats Low (early)")


def test_priority_age_breaks_ties():
    print("\n--- Test: Priority score — older wins within same tier ---")

    older = "2026-06-01T09:00:00Z"
    newer = "2026-06-01T10:00:00Z"

    high_older = compute_priority_score("High", older)
    high_newer = compute_priority_score("High", newer)

    assert_true(
        high_older > high_newer,
        "older High sorts higher than newer High (FIFO within tier)",
    )


def test_priority_score_no_collision():
    print("\n--- Test: Priority scores don't collide across tiers ---")

    # Range check: highest possible Low score should be below lowest High score
    # Low tier, queued at epoch 0 (year 1970) → max Low score
    low_max = compute_priority_score("Low", "1970-01-01T00:00:00Z")
    # High tier, queued now → near-min High score (current epoch subtracted)
    high_min = compute_priority_score("High", "2026-01-01T00:00:00Z")

    assert_true(high_min > low_max, "lowest High still beats highest realistic Low")


def test_sort_rows_by_priority():
    print("\n--- Test: sort_rows_by_priority orders correctly ---")

    rows = [
        {"recipient_email": "low@x.com", "priority_tier": "Low", "queued_at": "2026-06-01T08:00:00Z"},
        {"recipient_email": "high@x.com", "priority_tier": "High", "queued_at": "2026-06-01T12:00:00Z"},
        {"recipient_email": "med@x.com", "priority_tier": "Medium", "queued_at": "2026-06-01T09:00:00Z"},
        {"recipient_email": "high2@x.com", "priority_tier": "High", "queued_at": "2026-06-01T07:00:00Z"},
    ]

    sorted_rows = sort_rows_by_priority(rows)
    order = [r["recipient_email"] for r in sorted_rows]

    # Expected: high2 (High, older) → high (High, newer) → med (Medium) → low (Low)
    assert_eq(
        order,
        ["high2@x.com", "high@x.com", "med@x.com", "low@x.com"],
        "rows sorted High→Low, older-first within tier",
    )


def test_sort_uses_existing_score():
    print("\n--- Test: sort uses precomputed priority_score when present ---")

    rows = [
        {"recipient_email": "a@x.com", "priority_score": "100"},
        {"recipient_email": "b@x.com", "priority_score": "300"},
        {"recipient_email": "c@x.com", "priority_score": "200"},
    ]
    sorted_rows = sort_rows_by_priority(rows)
    order = [r["recipient_email"] for r in sorted_rows]
    assert_eq(order, ["b@x.com", "c@x.com", "a@x.com"], "sorted by explicit score desc")


# ============================================================================
# Test: Hash index determinism
# ============================================================================

def test_hash_index_deterministic():
    print("\n--- Test: Hash-to-index is deterministic ---")

    i1 = _hash_to_index("alice@example.com", 3)
    i2 = _hash_to_index("alice@example.com", 3)
    assert_eq(i1, i2, "same email + n → same index")

    # In range
    assert_true(0 <= i1 < 3, "index in [0, n)")

    # Case-insensitive
    i3 = _hash_to_index("ALICE@EXAMPLE.COM", 3)
    assert_eq(i1, i3, "email case doesn't change index")

    # Offset rotates
    i_off = _hash_to_index("alice@example.com", 3, offset=1)
    assert_eq(i_off, (i1 + 1) % 3, "offset rotates index by 1")


def test_hash_index_n_zero():
    print("\n--- Test: Hash index handles n=0 ---")
    assert_eq(_hash_to_index("x@y.com", 0), 0, "n=0 → 0 (no crash)")


# ============================================================================
# Test: Sender selection — single account (audit error 5.12)
# ============================================================================

def _make_account(email, daily_cap=200, hourly_cap=30, sends_24h=0, sends_1h=0,
                  active=True, priority=0):
    acct = SenderAccount(
        from_account=email,
        display_name=email,
        daily_cap=daily_cap,
        hourly_cap=hourly_cap,
        send_window_start_utc=0,
        send_window_end_utc=24,
        is_active=active,
        priority_order=priority,
    )
    acct.sends_last_24h = sends_24h
    acct.sends_last_1h = sends_1h
    return acct


def test_single_account_available():
    print("\n--- Test: Single account, available → picked ---")

    accounts = [_make_account("daniel@premiumads.net")]
    result = pick_sender_account("alice@example.com", accounts=accounts)
    assert_true(result is not None, "account returned")
    assert_eq(result.from_account, "daniel@premiumads.net", "the only account picked")


def test_single_account_exhausted_daily(): 
    print("\n--- Test: Single account, daily cap hit → None (audit error 5.12) ---")

    accounts = [_make_account("daniel@premiumads.net", daily_cap=200, sends_24h=200)]
    result = pick_sender_account("alice@example.com", accounts=accounts)
    assert_eq(result, None, "exhausted single account → None (defer)")


def test_single_account_exhausted_hourly():
    print("\n--- Test: Single account, hourly cap hit → None ---")

    accounts = [_make_account("daniel@premiumads.net", hourly_cap=30, sends_1h=30)]
    result = pick_sender_account("alice@example.com", accounts=accounts)
    assert_eq(result, None, "hourly-exhausted account → None")


def test_inactive_account_skipped():
    print("\n--- Test: Inactive account → None ---")

    accounts = [_make_account("daniel@premiumads.net", active=False)]
    result = pick_sender_account("alice@example.com", accounts=accounts)
    assert_eq(result, None, "inactive account not picked")


# ============================================================================
# Test: Sender selection — multi-account hybrid (audit error 5.13)
# ============================================================================

def test_multi_account_hash_consistency():
    print("\n--- Test: Multi-account, hash gives consistent primary ---")

    accounts = [
        _make_account("a@premiumads.net", priority=0),
        _make_account("b@premiumads.net", priority=1),
        _make_account("c@premiumads.net", priority=2),
    ]

    # Same recipient always gets same account (when all available)
    r1 = pick_sender_account("alice@example.com", accounts=accounts)
    r2 = pick_sender_account("alice@example.com", accounts=accounts)
    assert_eq(r1.from_account, r2.from_account, "same recipient → same sender (consistent)")


def test_multi_account_distributes():
    print("\n--- Test: Multi-account distributes across recipients ---")

    accounts = [
        _make_account("a@premiumads.net", priority=0),
        _make_account("b@premiumads.net", priority=1),
        _make_account("c@premiumads.net", priority=2),
    ]

    # Different recipients should spread across accounts
    senders = set()
    for i in range(20):
        r = pick_sender_account(f"user{i}@example.com", accounts=accounts)
        senders.add(r.from_account)

    assert_true(len(senders) >= 2, f"20 recipients spread across {len(senders)} accounts")


def test_multi_account_fallback_when_primary_exhausted():
    print("\n--- Test: Primary exhausted → round-robin fallback (audit error 5.13) ---")

    # Make primary (whichever alice hashes to) exhausted, others available
    accounts = [
        _make_account("a@premiumads.net", priority=0),
        _make_account("b@premiumads.net", priority=1),
        _make_account("c@premiumads.net", priority=2),
    ]

    # Find alice's primary
    primary = pick_sender_account("alice@example.com", accounts=accounts)
    primary_email = primary.from_account

    # Exhaust the primary
    for a in accounts:
        if a.from_account == primary_email:
            a.sends_last_24h = a.daily_cap

    # Now alice should get a DIFFERENT account
    fallback = pick_sender_account("alice@example.com", accounts=accounts)
    assert_true(fallback is not None, "fallback account returned")
    assert_true(
        fallback.from_account != primary_email,
        f"fallback differs from exhausted primary ({fallback.from_account} != {primary_email})",
    )


def test_multi_account_all_exhausted():
    print("\n--- Test: All accounts exhausted → None ---")

    accounts = [
        _make_account("a@premiumads.net", daily_cap=200, sends_24h=200, priority=0),
        _make_account("b@premiumads.net", daily_cap=200, sends_24h=200, priority=1),
    ]
    result = pick_sender_account("alice@example.com", accounts=accounts)
    assert_eq(result, None, "all exhausted → None (defer)")


def test_attempt_count_rotates_fallback():
    print("\n--- Test: attempt_count rotates the fallback choice ---")

    accounts = [
        _make_account("a@premiumads.net", priority=0),
        _make_account("b@premiumads.net", priority=1),
        _make_account("c@premiumads.net", priority=2),
        _make_account("d@premiumads.net", priority=3),
    ]

    # Exhaust alice's primary so we're in fallback mode
    primary = pick_sender_account("alice@example.com", accounts=accounts)
    for a in accounts:
        if a.from_account == primary.from_account:
            a.sends_last_24h = a.daily_cap

    # Different attempt counts should be able to produce different fallbacks
    picks = set()
    for attempt in range(6):
        r = pick_sender_account("alice@example.com", attempt_count=attempt, accounts=accounts)
        if r:
            picks.add(r.from_account)

    assert_true(len(picks) >= 2, f"attempt_count rotation hit {len(picks)} distinct accounts")


# ============================================================================
# Test: Send window
# ============================================================================

def test_send_window_always():
    print("\n--- Test: Send window 0-24 = always allowed ---")
    acct = _make_account("a@x.com")
    acct.send_window_start_utc = 0
    acct.send_window_end_utc = 24
    assert_true(acct.is_within_send_window(), "0-24 window always open")


def test_send_window_restricted():
    print("\n--- Test: Restricted send window ---")
    acct = _make_account("a@x.com")
    acct.send_window_start_utc = 13
    acct.send_window_end_utc = 23

    # 10:00 UTC — outside window
    morning = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    assert_true(not acct.is_within_send_window(morning), "10:00 outside 13-23 window")

    # 15:00 UTC — inside window
    afternoon = datetime(2026, 6, 1, 15, 0, tzinfo=timezone.utc)
    assert_true(acct.is_within_send_window(afternoon), "15:00 inside 13-23 window")


def test_send_window_wraps_midnight():
    print("\n--- Test: Send window wrapping midnight (22-6) ---")
    acct = _make_account("a@x.com")
    acct.send_window_start_utc = 22
    acct.send_window_end_utc = 6

    late = datetime(2026, 6, 1, 23, 0, tzinfo=timezone.utc)
    assert_true(acct.is_within_send_window(late), "23:00 inside wrapping 22-6")

    early = datetime(2026, 6, 1, 3, 0, tzinfo=timezone.utc)
    assert_true(acct.is_within_send_window(early), "03:00 inside wrapping 22-6")

    midday = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    assert_true(not acct.is_within_send_window(midday), "12:00 outside wrapping 22-6")


# ============================================================================
# Test: account availability composite
# ============================================================================

def test_account_availability():
    print("\n--- Test: is_available composite logic ---")

    ok = _make_account("ok@x.com")
    assert_true(ok.is_available, "active + capacity + window → available")

    exhausted = _make_account("ex@x.com", daily_cap=10, sends_24h=10)
    assert_true(not exhausted.is_available, "exhausted → not available")

    inactive = _make_account("in@x.com", active=False)
    assert_true(not inactive.is_available, "inactive → not available")


# ============================================================================
# RUNNER
# ============================================================================

def run_all():
    print("=" * 60)
    print("Stage 5 Test Suite — Priority & Sender Pool")
    print("=" * 60)

    test_tier_weights()
    test_priority_tier_dominates()
    test_priority_age_breaks_ties()
    test_priority_score_no_collision()
    test_sort_rows_by_priority()
    test_sort_uses_existing_score()
    test_hash_index_deterministic()
    test_hash_index_n_zero()
    test_single_account_available()
    test_single_account_exhausted_daily()
    test_single_account_exhausted_hourly()
    test_inactive_account_skipped()
    test_multi_account_hash_consistency()
    test_multi_account_distributes()
    test_multi_account_fallback_when_primary_exhausted()
    test_multi_account_all_exhausted()
    test_attempt_count_rotates_fallback()
    test_send_window_always()
    test_send_window_restricted()
    test_send_window_wraps_midnight()
    test_account_availability()

    print("\n" + "=" * 60)
    print("Test suite complete.")
    print("=" * 60)


if __name__ == "__main__":
    run_all()
