"""
stage6_health_score.py
=======================
Account health scoring + auto-pause decision logic. Pure functions.

Solves audit errors:
  - 6.1: Never recommend pausing the LAST active account
  - 6.2: Bounce rate over rolling 7d window + minimum-volume guard
  - 6.5: Pause decisions carry a human-readable reason
  - 6.7: Reactivation grace window prevents instant re-pause

This module DECIDES; it does not write to Sheets. The caller (dashboard button
or Apps Script) applies the decisions. Keeping decisions pure makes them testable.

Health tiers:
  healthy   — bounce rate below warning threshold (or insufficient volume)
  warning   — bounce rate at/above warning, below critical → alert only
  critical  — bounce rate at/above critical → auto-pause candidate
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from stage1_validation import normalize_email
from time_utils import safe_parse_date


# ============================================================================
# THRESHOLDS
# ============================================================================

BOUNCE_RATE_WARNING = 3.0    # % — alert
BOUNCE_RATE_CRITICAL = 5.0   # % — auto-pause candidate

# Don't act on bounce rate until the account has at least this many sends
# in the window (audit error 6.2 — avoids 1/2 = 50% panic).
MIN_SENDS_FOR_HEALTH = 20

# Rolling window for health computation
HEALTH_WINDOW_DAYS = 7

# After manual reactivation, don't auto-pause again for this many hours
# (audit error 6.7 — grace window).
REACTIVATION_GRACE_HOURS = 24


HealthStatus = Literal["healthy", "warning", "critical", "insufficient_data"]
HealthAction = Literal["none", "alert", "auto_pause", "blocked_last_account", "in_grace"]


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class AccountHealth:
    """Health assessment for one account."""
    from_account: str
    sends_window: int = 0
    bounces_window: int = 0
    status: HealthStatus = "insufficient_data"
    recommended_action: HealthAction = "none"
    reason: str = ""

    @property
    def bounce_rate(self) -> float:
        if self.sends_window == 0:
            return 0.0
        return round(100.0 * self.bounces_window / self.sends_window, 1)


# ============================================================================
# WINDOW COUNTING
# ============================================================================

def _count_in_window(
    emails_rows: list[dict],
    from_account: str,
    window_days: int,
    now: datetime,
) -> tuple[int, int]:
    """
    Count (sends, bounces) for an account within the trailing window.

    A "send" = a row with status sent/delivered/bounced whose from_account
    matches and whose sent_at (or last_attempt_at for bounces) is in window.
    A "bounce" = those with reply_status == 'bounce' OR status == 'Bounced'.
    """
    account_norm = normalize_email(from_account)
    cutoff = now - timedelta(days=window_days)

    sends = 0
    bounces = 0

    for row in emails_rows:
        if normalize_email(row.get("from_account", "")) != account_norm:
            continue

        status = str(row.get("status", "")).strip().lower()
        reply_status = str(row.get("reply_status", "")).strip().lower()

        # Determine the relevant timestamp
        ts = safe_parse_date(row.get("sent_at")) or safe_parse_date(row.get("last_attempt_at"))
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < cutoff:
            continue

        # Count as a send if it was actually attempted-out
        if status in ("sent", "delivered", "bounced"):
            sends += 1

        # Count bounces (either hard-bounce status, or reply classified bounce)
        if status == "bounced" or reply_status == "bounce":
            bounces += 1

    return sends, bounces


# ============================================================================
# REACTIVATION GRACE (audit error 6.7)
# ============================================================================

def in_reactivation_grace(reactivated_at, now: Optional[datetime] = None) -> bool:
    """True if the account was reactivated within the grace window."""
    if not reactivated_at:
        return False
    dt = safe_parse_date(reactivated_at)
    if dt is None:
        return False
    if now is None:
        now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt) < timedelta(hours=REACTIVATION_GRACE_HOURS)


# ============================================================================
# HEALTH ASSESSMENT
# ============================================================================

def assess_account_health(
    account: dict,
    emails_rows: list[dict],
    active_account_count: int,
    now: Optional[datetime] = None,
) -> AccountHealth:
    """
    Assess one account's health and recommend an action.

    Args:
        account: a sender_accounts row dict (needs from_account, reactivated_at)
        emails_rows: all Emails rows (for counting sends/bounces)
        active_account_count: how many accounts are currently active (audit 6.1)
        now: override for testing

    Returns:
        AccountHealth with status, recommended_action, and reason.

    Decision logic:
        - insufficient volume → healthy (insufficient_data), action none
        - bounce < warning → healthy, none
        - warning <= bounce < critical → warning, alert
        - bounce >= critical → critical
            - if in grace window → in_grace (don't pause yet)
            - elif this is the last active account → blocked_last_account (alert hard)
            - else → auto_pause
    """
    if now is None:
        now = datetime.now(timezone.utc)

    from_account = str(account.get("from_account", "")).strip()
    sends, bounces = _count_in_window(emails_rows, from_account, HEALTH_WINDOW_DAYS, now)

    health = AccountHealth(
        from_account=from_account,
        sends_window=sends,
        bounces_window=bounces,
    )

    # Insufficient volume — can't judge (audit error 6.2)
    if sends < MIN_SENDS_FOR_HEALTH:
        health.status = "insufficient_data"
        health.recommended_action = "none"
        health.reason = (
            f"Only {sends} sends in {HEALTH_WINDOW_DAYS}d "
            f"(need {MIN_SENDS_FOR_HEALTH} to assess)"
        )
        return health

    rate = health.bounce_rate

    # Healthy
    if rate < BOUNCE_RATE_WARNING:
        health.status = "healthy"
        health.recommended_action = "none"
        health.reason = f"Bounce rate {rate}% — healthy"
        return health

    # Warning band
    if rate < BOUNCE_RATE_CRITICAL:
        health.status = "warning"
        health.recommended_action = "alert"
        health.reason = (
            f"Bounce rate {rate}% — above {BOUNCE_RATE_WARNING}% warning "
            f"({bounces}/{sends})"
        )
        return health

    # Critical band
    health.status = "critical"
    critical_reason = (
        f"Bounce rate {rate}% — above {BOUNCE_RATE_CRITICAL}% critical "
        f"({bounces}/{sends})"
    )

    # Grace window guard (audit error 6.7)
    if in_reactivation_grace(account.get("reactivated_at"), now):
        health.recommended_action = "in_grace"
        health.reason = (
            critical_reason +
            f" — but in {REACTIVATION_GRACE_HOURS}h reactivation grace, not pausing"
        )
        return health

    # Last-account guard (audit error 6.1)
    if active_account_count <= 1:
        health.recommended_action = "blocked_last_account"
        health.reason = (
            critical_reason +
            " — NOT auto-pausing: this is the last active account. "
            "Manual intervention needed (fix list quality or add an account)."
        )
        return health

    # Safe to auto-pause
    health.recommended_action = "auto_pause"
    health.reason = critical_reason + " — auto-pausing"
    return health


def assess_all_accounts(
    accounts: list[dict],
    emails_rows: list[dict],
    now: Optional[datetime] = None,
) -> list[AccountHealth]:
    """Assess every account. active_account_count computed from is_active flags."""
    active_count = sum(
        1 for a in accounts
        if str(a.get("is_active", "TRUE")).strip().upper() == "TRUE"
    )
    return [
        assess_account_health(acct, emails_rows, active_count, now)
        for acct in accounts
    ]
