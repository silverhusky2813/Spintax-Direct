"""
test_stage6.py
===============
Tests for Stage 6: warm-up ramp + account health scoring.

Covers audit errors:
  - 6.1: Never auto-pause the last active account
  - 6.2: Bounce rate window + minimum-volume guard
  - 6.3: Warm-up cap only lowers, never raises
  - 6.4: days_active from activation date
  - 6.7: Reactivation grace prevents instant re-pause

Run with:
  python test_stage6.py
"""

from datetime import datetime, timedelta, timezone

from stage6_warmup import (
    days_active,
    effective_daily_cap,
    is_warmed_up,
    warmup_cap_for_day,
)
from stage6_health_score import (
    BOUNCE_RATE_CRITICAL,
    BOUNCE_RATE_WARNING,
    MIN_SENDS_FOR_HEALTH,
    REACTIVATION_GRACE_HOURS,
    assess_account_health,
    assess_all_accounts,
    in_reactivation_grace,
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


NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


# ============================================================================
# Test: days_active (audit error 6.4)
# ============================================================================

def test_days_active():
    print("\n--- Test: days_active computation (audit error 6.4) ---")

    # Activated today → day 1
    today = NOW.date().isoformat()
    assert_eq(days_active(today, NOW), 1, "activated today → day 1")

    # Activated 10 days ago → day 11
    ten_ago = (NOW.date() - timedelta(days=10)).isoformat()
    assert_eq(days_active(ten_ago, NOW), 11, "10 days ago → day 11")

    # No activation date → 0
    assert_eq(days_active("", NOW), 0, "no date → 0")
    assert_eq(days_active(None, NOW), 0, "None → 0")


# ============================================================================
# Test: warm-up cap schedule (audit error 6.3)
# ============================================================================

def test_warmup_cap_schedule():
    print("\n--- Test: Warm-up cap schedule ---")

    assert_eq(warmup_cap_for_day(1), 20, "day 1 → 20")
    assert_eq(warmup_cap_for_day(2), 20, "day 2 → 20")
    assert_eq(warmup_cap_for_day(3), 40, "day 3 → 40")
    assert_eq(warmup_cap_for_day(8), 100, "day 8 → 100")
    assert_eq(warmup_cap_for_day(16), 200, "day 16 → 200")
    assert_eq(warmup_cap_for_day(29), None, "day 29 → graduated (None)")
    assert_eq(warmup_cap_for_day(100), None, "day 100 → graduated (None)")


def test_effective_cap_lowers_only():
    print("\n--- Test: Warm-up cap only LOWERS (audit error 6.3) ---")

    today = NOW.date().isoformat()

    # Day 1, configured 200 → warm-up restricts to 20
    eff = effective_daily_cap(200, warmup_enabled=True, activated_at=today, now=NOW)
    assert_eq(eff, 20, "day 1 warm-up restricts 200 → 20")

    # Day 1, configured 10 (lower than warm-up cap) → stays 10 (min wins)
    eff = effective_daily_cap(10, warmup_enabled=True, activated_at=today, now=NOW)
    assert_eq(eff, 10, "configured 10 < warmup 20 → 10 (min, never raises)")

    # Warm-up disabled → configured cap regardless
    eff = effective_daily_cap(200, warmup_enabled=False, activated_at=today, now=NOW)
    assert_eq(eff, 200, "warm-up off → full configured cap")

    # Graduated (day 30) → configured cap
    old = (NOW.date() - timedelta(days=29)).isoformat()
    eff = effective_daily_cap(200, warmup_enabled=True, activated_at=old, now=NOW)
    assert_eq(eff, 200, "graduated → full configured cap")


def test_is_warmed_up():
    print("\n--- Test: is_warmed_up ---")

    today = NOW.date().isoformat()
    old = (NOW.date() - timedelta(days=35)).isoformat()

    assert_true(not is_warmed_up(True, today, NOW), "day 1 with warmup → not warmed")
    assert_true(is_warmed_up(True, old, NOW), "day 36 → warmed")
    assert_true(is_warmed_up(False, today, NOW), "warmup disabled → always warmed")


# ============================================================================
# Test: Health — insufficient volume (audit error 6.2)
# ============================================================================

def _email_row(from_account="a@x.com", status="sent", reply_status="none",
               sent_at=None):
    if sent_at is None:
        sent_at = NOW.isoformat()
    return {
        "from_account": from_account,
        "status": status,
        "reply_status": reply_status,
        "sent_at": sent_at,
        "last_attempt_at": sent_at,
    }


def test_health_insufficient_volume():
    print("\n--- Test: Insufficient volume → no action (audit error 6.2) ---")

    # 2 sends, 1 bounce = 50% but below MIN_SENDS_FOR_HEALTH
    rows = [
        _email_row(status="sent", reply_status="bounce"),
        _email_row(status="sent", reply_status="none"),
    ]
    account = {"from_account": "a@x.com", "is_active": "TRUE"}
    health = assess_account_health(account, rows, active_account_count=2, now=NOW)

    assert_eq(health.status, "insufficient_data", "2 sends → insufficient_data")
    assert_eq(health.recommended_action, "none", "no action on tiny sample")


def test_health_healthy():
    print("\n--- Test: Healthy account ---")

    # 30 sends, 0 bounces
    rows = [_email_row() for _ in range(30)]
    account = {"from_account": "a@x.com", "is_active": "TRUE"}
    health = assess_account_health(account, rows, active_account_count=2, now=NOW)

    assert_eq(health.status, "healthy", "0% bounce → healthy")
    assert_eq(health.recommended_action, "none", "healthy → no action")
    assert_eq(health.bounce_rate, 0.0, "bounce rate 0")


def test_health_warning():
    print("\n--- Test: Warning band → alert ---")

    # 30 sends, ~4% bounce (between 3% warning and 5% critical)
    rows = [_email_row(reply_status="bounce") for _ in range(4)]  # wait, need denominator
    # 50 sends total, 2 bounces = 4%
    rows = [_email_row(reply_status="bounce") for _ in range(2)] + \
           [_email_row(reply_status="none") for _ in range(48)]
    account = {"from_account": "a@x.com", "is_active": "TRUE"}
    health = assess_account_health(account, rows, active_account_count=2, now=NOW)

    assert_eq(health.bounce_rate, 4.0, "bounce rate 4%")
    assert_eq(health.status, "warning", "4% → warning")
    assert_eq(health.recommended_action, "alert", "warning → alert")


def test_health_critical_autopause():
    print("\n--- Test: Critical → auto_pause (with other accounts) ---")

    # 50 sends, 4 bounces = 8% (above 5% critical)
    rows = [_email_row(reply_status="bounce") for _ in range(4)] + \
           [_email_row(reply_status="none") for _ in range(46)]
    account = {"from_account": "a@x.com", "is_active": "TRUE"}
    # 2 active accounts → safe to pause one
    health = assess_account_health(account, rows, active_account_count=2, now=NOW)

    assert_eq(health.status, "critical", "8% → critical")
    assert_eq(health.recommended_action, "auto_pause", "critical + others active → auto_pause")


def test_health_last_account_not_paused():
    print("\n--- Test: Last active account NEVER auto-paused (audit error 6.1) ---")

    # Critical bounce rate
    rows = [_email_row(reply_status="bounce") for _ in range(5)] + \
           [_email_row(reply_status="none") for _ in range(45)]
    account = {"from_account": "a@x.com", "is_active": "TRUE"}
    # Only 1 active account
    health = assess_account_health(account, rows, active_account_count=1, now=NOW)

    assert_eq(health.status, "critical", "still flagged critical")
    assert_eq(
        health.recommended_action,
        "blocked_last_account",
        "last account → blocked_last_account, NOT auto_pause",
    )
    assert_true("last active account" in health.reason.lower(), "reason explains the block")


def test_health_reactivation_grace():
    print("\n--- Test: Reactivation grace prevents re-pause (audit error 6.7) ---")

    # Critical bounce rate
    rows = [_email_row(reply_status="bounce") for _ in range(5)] + \
           [_email_row(reply_status="none") for _ in range(45)]
    # Reactivated 1 hour ago — within grace
    recent_reactivation = (NOW - timedelta(hours=1)).isoformat()
    account = {
        "from_account": "a@x.com",
        "is_active": "TRUE",
        "reactivated_at": recent_reactivation,
    }
    health = assess_account_health(account, rows, active_account_count=2, now=NOW)

    assert_eq(health.status, "critical", "still critical")
    assert_eq(
        health.recommended_action,
        "in_grace",
        "within grace window → in_grace, not auto_pause",
    )


def test_health_grace_expired():
    print("\n--- Test: Expired grace allows re-pause ---")

    rows = [_email_row(reply_status="bounce") for _ in range(5)] + \
           [_email_row(reply_status="none") for _ in range(45)]
    # Reactivated 48 hours ago — grace (24h) expired
    old_reactivation = (NOW - timedelta(hours=48)).isoformat()
    account = {
        "from_account": "a@x.com",
        "is_active": "TRUE",
        "reactivated_at": old_reactivation,
    }
    health = assess_account_health(account, rows, active_account_count=2, now=NOW)

    assert_eq(health.recommended_action, "auto_pause", "expired grace → auto_pause resumes")


def test_in_reactivation_grace():
    print("\n--- Test: in_reactivation_grace helper ---")

    recent = (NOW - timedelta(hours=1)).isoformat()
    old = (NOW - timedelta(hours=48)).isoformat()

    assert_true(in_reactivation_grace(recent, NOW), "1h ago → in grace")
    assert_true(not in_reactivation_grace(old, NOW), "48h ago → grace expired")
    assert_true(not in_reactivation_grace("", NOW), "no date → not in grace")
    assert_true(not in_reactivation_grace(None, NOW), "None → not in grace")


def test_window_excludes_old_sends():
    print("\n--- Test: Health window excludes old sends (audit error 6.2) ---")

    # Recent: 30 clean sends; Old (10 days ago): 10 bounces
    recent_rows = [_email_row(reply_status="none") for _ in range(30)]
    old_ts = (NOW - timedelta(days=10)).isoformat()
    old_rows = [_email_row(reply_status="bounce", sent_at=old_ts) for _ in range(10)]
    rows = recent_rows + old_rows

    account = {"from_account": "a@x.com", "is_active": "TRUE"}
    health = assess_account_health(account, rows, active_account_count=2, now=NOW)

    # Old bounces (10d ago) are outside the 7d window → not counted
    assert_eq(health.sends_window, 30, "only recent 30 sends in window")
    assert_eq(health.bounces_window, 0, "old bounces excluded from window")
    assert_eq(health.status, "healthy", "healthy when old bounces excluded")


def test_assess_all_accounts():
    print("\n--- Test: assess_all_accounts computes active count ---")

    rows = [_email_row(from_account="a@x.com") for _ in range(30)]
    accounts = [
        {"from_account": "a@x.com", "is_active": "TRUE"},
        {"from_account": "b@x.com", "is_active": "TRUE"},
        {"from_account": "c@x.com", "is_active": "FALSE"},  # inactive
    ]
    results = assess_all_accounts(accounts, rows, now=NOW)
    assert_eq(len(results), 3, "assessed all 3 accounts")
    # 'a' should be healthy with its 30 clean sends
    a_health = next(r for r in results if r.from_account == "a@x.com")
    assert_eq(a_health.status, "healthy", "account a healthy")


# ============================================================================
# RUNNER
# ============================================================================

def run_all():
    print("=" * 60)
    print("Stage 6 Test Suite — Warm-up & Health Scoring")
    print("=" * 60)

    test_days_active()
    test_warmup_cap_schedule()
    test_effective_cap_lowers_only()
    test_is_warmed_up()
    test_health_insufficient_volume()
    test_health_healthy()
    test_health_warning()
    test_health_critical_autopause()
    test_health_last_account_not_paused()
    test_health_reactivation_grace()
    test_health_grace_expired()
    test_in_reactivation_grace()
    test_window_excludes_old_sends()
    test_assess_all_accounts()

    print("\n" + "=" * 60)
    print("Test suite complete.")
    print("=" * 60)


if __name__ == "__main__":
    run_all()
