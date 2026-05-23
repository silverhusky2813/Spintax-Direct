"""
stage6_warmup.py
=================
Account warm-up ramp logic. Pure functions.

Solves audit errors:
  - 6.3: Warm-up cap only ever LOWERS the effective cap, never raises it
  - 6.4: days_active derived from activated_at (or first send)

New sending domains/accounts that blast full volume on day 1 get flagged as
spam. A warm-up ramp starts low and scales over ~4 weeks, building a positive
sending reputation gradually.

The ramp is a schedule of (day_threshold → daily_cap). The effective cap for an
account is:
    min(configured_daily_cap, warmup_cap_for_current_day)

So warm-up can only restrict, never exceed, the account's configured cap.
"""

from datetime import date, datetime, timezone
from typing import Optional

from time_utils import safe_parse_date


# ============================================================================
# WARM-UP SCHEDULE
# ============================================================================

# (min_day_inclusive, suggested_daily_cap)
# Day 1-2: 20/day, ramping to full by day 29+.
# Tune to your domain reputation appetite — conservative is safer.
WARMUP_SCHEDULE = [
    (1, 20),
    (3, 40),
    (5, 60),
    (8, 100),
    (12, 150),
    (16, 200),
    (22, 300),
    (29, None),   # None = no warm-up restriction (use configured cap)
]


def days_active(activated_at, now: Optional[datetime] = None) -> int:
    """
    How many days since the account was activated.

    Day 1 = the activation day itself. Returns 0 if no activation date
    (account hasn't started — treat as not yet warming).
    """
    if not activated_at:
        return 0

    dt = safe_parse_date(activated_at)
    if dt is None:
        return 0

    if now is None:
        now = datetime.now(timezone.utc)

    # Normalize both to dates
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    activated_date = dt.date()
    now_date = now.date()

    delta = (now_date - activated_date).days
    # Day of activation counts as day 1
    return max(0, delta + 1)


def warmup_cap_for_day(day: int) -> Optional[int]:
    """
    Return the suggested daily cap for a given warm-up day.

    Returns None when the account has graduated (day >= final threshold),
    meaning "no warm-up restriction — use the configured cap".
    """
    if day <= 0:
        return WARMUP_SCHEDULE[0][1]  # not started → most conservative cap

    applicable_cap = WARMUP_SCHEDULE[0][1]
    for threshold_day, cap in WARMUP_SCHEDULE:
        if day >= threshold_day:
            applicable_cap = cap
        else:
            break
    return applicable_cap


def effective_daily_cap(
    configured_cap: int,
    warmup_enabled: bool,
    activated_at,
    now: Optional[datetime] = None,
) -> int:
    """
    Compute the effective daily cap, applying warm-up if enabled.

    effective = min(configured_cap, warmup_cap)   [audit error 6.3]

    If warm-up disabled or graduated → just the configured cap.
    """
    if not warmup_enabled:
        return configured_cap

    day = days_active(activated_at, now)
    wcap = warmup_cap_for_day(day)

    if wcap is None:
        # Graduated — no restriction
        return configured_cap

    return min(configured_cap, wcap)


def warmup_status_label(
    warmup_enabled: bool,
    activated_at,
    configured_cap: int,
    now: Optional[datetime] = None,
) -> str:
    """Human-readable warm-up status for the dashboard."""
    if not warmup_enabled:
        return "Warm-up off"

    day = days_active(activated_at, now)
    wcap = warmup_cap_for_day(day)

    if wcap is None:
        return f"Warmed up (day {day}, full {configured_cap}/day)"

    eff = min(configured_cap, wcap)
    return f"Warming up — day {day}, {eff}/day (target {configured_cap})"


def is_warmed_up(warmup_enabled: bool, activated_at, now: Optional[datetime] = None) -> bool:
    """True if the account has graduated from warm-up."""
    if not warmup_enabled:
        return True
    day = days_active(activated_at, now)
    return warmup_cap_for_day(day) is None
