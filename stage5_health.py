"""
stage5_health.py
=================
Aggregate queue health metrics for the dashboard.

Solves audit error 5.10: pull all rows once, compute everything in memory,
cache with short TTL. No repeated Sheets calls per metric.

Provides:
  - get_queue_health() → QueueHealth dataclass
  - get_account_usage() → list of AccountUsage
  - get_recent_failures() → list of failed rows with error details
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import gspread
import streamlit as st

from stage1_dedup import get_gspread_client
from stage5_priority import sort_rows_by_priority
from stage5_sender_pool import load_accounts_with_usage, SenderAccount
from time_utils import safe_parse_date


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class QueueHealth:
    """Snapshot of overall queue health."""
    total_rows: int = 0
    queued: int = 0
    sending: int = 0
    sent: int = 0
    failed: int = 0
    bounced: int = 0
    scheduled: int = 0

    oldest_queued_age_hours: Optional[float] = None
    oldest_queued_recipient: str = ""

    sent_last_24h: int = 0
    sent_last_1h: int = 0
    failed_last_24h: int = 0

    failure_rate_24h: float = 0.0  # failed / (sent + failed) over 24h

    next_to_send: list[dict] = field(default_factory=list)  # top 5 by priority


@dataclass
class AccountUsage:
    """Per-account usage snapshot for the dashboard."""
    from_account: str
    display_name: str
    daily_cap: int
    hourly_cap: int
    sends_last_24h: int
    sends_last_1h: int
    is_active: bool
    is_exhausted: bool
    within_window: bool

    @property
    def daily_pct(self) -> float:
        if self.daily_cap <= 0:
            return 0.0
        return min(100.0, 100.0 * self.sends_last_24h / self.daily_cap)

    @property
    def hourly_pct(self) -> float:
        if self.hourly_cap <= 0:
            return 0.0
        return min(100.0, 100.0 * self.sends_last_1h / self.hourly_cap)


# ============================================================================
# DATA LOADING
# ============================================================================

@st.cache_data(ttl=60)
def _load_emails() -> list[dict]:
    """Load all Emails rows. Cached 60s."""
    gc = get_gspread_client()
    sh = gc.open_by_key(st.secrets["sheet_id"])
    try:
        ws = sh.worksheet("Emails")
    except gspread.WorksheetNotFound:
        return []
    return ws.get_all_records()


# ============================================================================
# QUEUE HEALTH
# ============================================================================

def get_queue_health() -> QueueHealth:
    """Compute the full queue health snapshot from a single Emails read."""
    rows = _load_emails()
    health = QueueHealth(total_rows=len(rows))

    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)
    cutoff_1h = now - timedelta(hours=1)

    queued_rows = []
    oldest_queued_dt: Optional[datetime] = None

    for row in rows:
        status = str(row.get("status", "")).strip().lower()

        # Status counts
        if status == "queued":
            health.queued += 1
            queued_rows.append(row)
            qdt = safe_parse_date(row.get("queued_at"))
            if qdt:
                if qdt.tzinfo is None:
                    qdt = qdt.replace(tzinfo=timezone.utc)
                if oldest_queued_dt is None or qdt < oldest_queued_dt:
                    oldest_queued_dt = qdt
                    health.oldest_queued_recipient = row.get("recipient_email", "")
        elif status == "sending":
            health.sending += 1
        elif status in ("sent", "delivered"):
            health.sent += 1
        elif status == "failed":
            health.failed += 1
        elif status == "bounced":
            health.bounced += 1
        elif status == "scheduled":
            health.scheduled += 1

        # Time-windowed counts (based on sent_at / last_attempt_at)
        sent_dt = safe_parse_date(row.get("sent_at"))
        if sent_dt:
            if sent_dt.tzinfo is None:
                sent_dt = sent_dt.replace(tzinfo=timezone.utc)
            if sent_dt >= cutoff_24h and status in ("sent", "delivered"):
                health.sent_last_24h += 1
            if sent_dt >= cutoff_1h and status in ("sent", "delivered"):
                health.sent_last_1h += 1

        if status == "failed":
            attempt_dt = safe_parse_date(row.get("last_attempt_at"))
            if attempt_dt:
                if attempt_dt.tzinfo is None:
                    attempt_dt = attempt_dt.replace(tzinfo=timezone.utc)
                if attempt_dt >= cutoff_24h:
                    health.failed_last_24h += 1

    # Oldest queued age
    if oldest_queued_dt:
        age = now - oldest_queued_dt
        health.oldest_queued_age_hours = round(age.total_seconds() / 3600, 1)

    # Failure rate over 24h
    total_attempts_24h = health.sent_last_24h + health.failed_last_24h
    if total_attempts_24h > 0:
        health.failure_rate_24h = round(
            100.0 * health.failed_last_24h / total_attempts_24h, 1
        )

    # Next to send — top 5 queued by priority
    sorted_queued = sort_rows_by_priority(queued_rows)
    health.next_to_send = sorted_queued[:5]

    return health


# ============================================================================
# ACCOUNT USAGE
# ============================================================================

def get_account_usage() -> list[AccountUsage]:
    """Per-account usage for the dashboard."""
    accounts: list[SenderAccount] = load_accounts_with_usage()

    usages = []
    for acct in accounts:
        usages.append(AccountUsage(
            from_account=acct.from_account,
            display_name=acct.display_name,
            daily_cap=acct.daily_cap,
            hourly_cap=acct.hourly_cap,
            sends_last_24h=acct.sends_last_24h,
            sends_last_1h=acct.sends_last_1h,
            is_active=acct.is_active,
            is_exhausted=acct.is_exhausted,
            within_window=acct.is_within_send_window(),
        ))

    return usages


# ============================================================================
# RECENT FAILURES
# ============================================================================

def get_recent_failures(limit: int = 20) -> list[dict]:
    """
    Return recent Failed/Bounced rows with error details, newest first.
    """
    rows = _load_emails()

    failures = [
        r for r in rows
        if str(r.get("status", "")).strip().lower() in ("failed", "bounced")
    ]

    def sort_key(r):
        dt = safe_parse_date(r.get("last_attempt_at")) or safe_parse_date(
            r.get("queued_at")
        )
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp() if dt else 0

    failures.sort(key=sort_key, reverse=True)
    return failures[:limit]


# ============================================================================
# CAPACITY FORECAST
# ============================================================================

def estimate_drain_time() -> Optional[str]:
    """
    Rough estimate of how long to clear the current Queued backlog,
    given total available hourly capacity across all active accounts.

    Returns a human string like "~3 hours" or None if no capacity / no queue.
    """
    health = get_queue_health()
    if health.queued == 0:
        return "Queue empty"

    accounts = load_accounts_with_usage()
    total_hourly_capacity = sum(
        a.hourly_remaining for a in accounts if a.is_active and a.is_within_send_window()
    )

    if total_hourly_capacity <= 0:
        return "No capacity right now (caps hit or outside window)"

    hours = health.queued / total_hourly_capacity
    if hours < 1:
        return f"~{int(hours * 60)} min"
    return f"~{round(hours, 1)} hours"
