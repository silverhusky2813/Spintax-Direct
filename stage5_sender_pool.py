"""
stage5_sender_pool.py
======================
Multi-account sender selection with rate limiting (Python side).

Implements the HYBRID strategy (user's choice):
  1. Hash recipient_email → primary account (consistent per recipient)
  2. If primary is under its caps → use it
  3. If primary is exhausted → round-robin among remaining available accounts
  4. If ALL accounts exhausted → return None (caller defers the send)

Solves audit errors:
  - 5.12: Single-account edge case — returns None gracefully when exhausted
  - 5.13: Stateless round-robin via (hash + offset) % n — no stored cursor
  - 5.14: This is the ASSIGN-time logic (Stage 3). Apps Script re-validates at send.
  - 5.17: Falls back to DEFAULT account if sender_accounts tab is empty

The same cap rules are enforced on the Apps Script side at send time
(apps_script_v3.gs). Both read the sender_accounts + send_log tabs so the
rules stay in sync.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import gspread
import streamlit as st

from stage1_dedup import get_gspread_client
from stage1_validation import normalize_email
from time_utils import safe_parse_date


# ============================================================================
# FALLBACK (audit error 5.17)
# ============================================================================

DEFAULT_FROM_ACCOUNT = "daniel@premiumads.net"
DEFAULT_DAILY_CAP = 200
DEFAULT_HOURLY_CAP = 30


@dataclass
class SenderAccount:
    """One sending account's configuration + current usage."""
    from_account: str
    display_name: str
    daily_cap: int
    hourly_cap: int
    send_window_start_utc: int
    send_window_end_utc: int
    is_active: bool
    priority_order: int

    # Filled in by usage lookup
    sends_last_24h: int = 0
    sends_last_1h: int = 0

    @property
    def daily_remaining(self) -> int:
        return max(0, self.daily_cap - self.sends_last_24h)

    @property
    def hourly_remaining(self) -> int:
        return max(0, self.hourly_cap - self.sends_last_1h)

    @property
    def is_exhausted(self) -> bool:
        """True if either cap is hit."""
        return self.daily_remaining <= 0 or self.hourly_remaining <= 0

    def is_within_send_window(self, now_utc: Optional[datetime] = None) -> bool:
        """
        Check if current UTC hour is within the account's send window.
        Window of 0-24 means "always allowed".
        """
        if self.send_window_start_utc == 0 and self.send_window_end_utc == 24:
            return True
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
        hour = now_utc.hour
        start = self.send_window_start_utc
        end = self.send_window_end_utc
        if start <= end:
            return start <= hour < end
        else:
            # Window wraps midnight (e.g., 22-6)
            return hour >= start or hour < end

    @property
    def is_available(self) -> bool:
        """Active, not exhausted, and within send window."""
        return self.is_active and not self.is_exhausted and self.is_within_send_window()


# ============================================================================
# ACCOUNT LOADING
# ============================================================================

@st.cache_data(ttl=30)
def _load_sender_accounts_raw() -> list[dict]:
    """Load raw account rows. Cached 30s."""
    gc = get_gspread_client()
    sh = gc.open_by_key(st.secrets["sheet_id"])
    try:
        ws = sh.worksheet("sender_accounts")
    except gspread.WorksheetNotFound:
        return []
    return ws.get_all_records()


@st.cache_data(ttl=30)
def _load_send_log() -> list[dict]:
    """Load send log rows. Cached 30s."""
    gc = get_gspread_client()
    sh = gc.open_by_key(st.secrets["sheet_id"])
    try:
        ws = sh.worksheet("send_log")
    except gspread.WorksheetNotFound:
        return []
    return ws.get_all_records()


def _count_recent_sends(
    send_log: list[dict],
    from_account: str,
    since: datetime,
) -> int:
    """Count sends from `from_account` at or after `since`."""
    account_norm = normalize_email(from_account)
    count = 0
    for entry in send_log:
        if normalize_email(entry.get("from_account", "")) != account_norm:
            continue
        sent_dt = safe_parse_date(entry.get("sent_at"))
        if sent_dt is None:
            continue
        # Normalize to aware UTC for comparison
        if sent_dt.tzinfo is None:
            sent_dt = sent_dt.replace(tzinfo=timezone.utc)
        if sent_dt >= since:
            count += 1
    return count


def load_accounts_with_usage() -> list[SenderAccount]:
    """
    Load all sender accounts with their current usage filled in.

    Falls back to a single default account if the tab is empty (audit 5.17).
    """
    raw_accounts = _load_sender_accounts_raw()
    send_log = _load_send_log()

    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)
    cutoff_1h = now - timedelta(hours=1)

    # Fallback: empty tab → synthesize a default account
    if not raw_accounts:
        default = SenderAccount(
            from_account=DEFAULT_FROM_ACCOUNT,
            display_name="Daniel @ PremiumAds (default)",
            daily_cap=DEFAULT_DAILY_CAP,
            hourly_cap=DEFAULT_HOURLY_CAP,
            send_window_start_utc=0,
            send_window_end_utc=24,
            is_active=True,
            priority_order=0,
        )
        default.sends_last_24h = _count_recent_sends(send_log, DEFAULT_FROM_ACCOUNT, cutoff_24h)
        default.sends_last_1h = _count_recent_sends(send_log, DEFAULT_FROM_ACCOUNT, cutoff_1h)
        return [default]

    accounts = []
    for r in raw_accounts:
        try:
            acct = SenderAccount(
                from_account=str(r.get("from_account", "")).strip(),
                display_name=str(r.get("display_name", "")),
                daily_cap=int(r.get("daily_cap", DEFAULT_DAILY_CAP) or DEFAULT_DAILY_CAP),
                hourly_cap=int(r.get("hourly_cap", DEFAULT_HOURLY_CAP) or DEFAULT_HOURLY_CAP),
                send_window_start_utc=int(r.get("send_window_start_utc", 0) or 0),
                send_window_end_utc=int(r.get("send_window_end_utc", 24) or 24),
                is_active=str(r.get("is_active", "TRUE")).strip().upper() == "TRUE",
                priority_order=int(r.get("priority_order", 0) or 0),
            )
        except (ValueError, TypeError):
            # Skip malformed rows rather than crashing the whole pool
            continue

        if not acct.from_account:
            continue

        acct.sends_last_24h = _count_recent_sends(send_log, acct.from_account, cutoff_24h)
        acct.sends_last_1h = _count_recent_sends(send_log, acct.from_account, cutoff_1h)
        accounts.append(acct)

    # If all rows were malformed, fall back to default
    if not accounts:
        default = SenderAccount(
            from_account=DEFAULT_FROM_ACCOUNT,
            display_name="Daniel @ PremiumAds (default fallback)",
            daily_cap=DEFAULT_DAILY_CAP,
            hourly_cap=DEFAULT_HOURLY_CAP,
            send_window_start_utc=0,
            send_window_end_utc=24,
            is_active=True,
            priority_order=0,
        )
        return [default]

    return accounts


# ============================================================================
# HYBRID SELECTION (audit errors 5.12, 5.13)
# ============================================================================

def _hash_to_index(recipient_email: str, n: int, offset: int = 0) -> int:
    """
    Deterministically map an email to an index in [0, n).

    The offset enables stateless round-robin (audit error 5.13): incrementing
    offset rotates the choice without stored cursor state.
    """
    if n <= 0:
        return 0
    normalized = normalize_email(recipient_email)
    digest = hashlib.sha256(normalized.encode("utf-8")).digest()
    base = int.from_bytes(digest[:8], byteorder="little", signed=False)
    return (base + offset) % n


def pick_sender_account(
    recipient_email: str,
    attempt_count: int = 0,
    accounts: Optional[list[SenderAccount]] = None,
) -> Optional[SenderAccount]:
    """
    Pick a sending account using the hybrid strategy.

    Args:
        recipient_email: drives the hash for primary selection
        attempt_count: used as round-robin offset on retries (audit 5.13)
        accounts: pre-loaded accounts (for testing); loads fresh if None

    Returns:
        SenderAccount to use, or None if ALL accounts are exhausted/unavailable
        (caller should defer the send — audit error 5.12).

    Strategy:
        1. Primary = hash(recipient) among ALL active accounts (stable per recipient)
        2. If primary available → use it
        3. Else round-robin among AVAILABLE accounts, offset by attempt_count
        4. Else None
    """
    if accounts is None:
        accounts = load_accounts_with_usage()

    # Only consider active accounts for primary hashing (stable ordering)
    active = [a for a in accounts if a.is_active]
    if not active:
        return None

    # Sort by priority_order for deterministic indexing
    active.sort(key=lambda a: (a.priority_order, a.from_account))

    # --- Step 1 & 2: hash-based primary ---
    primary_idx = _hash_to_index(recipient_email, len(active))
    primary = active[primary_idx]
    if primary.is_available:
        return primary

    # --- Step 3: round-robin among AVAILABLE accounts ---
    available = [a for a in active if a.is_available]
    if not available:
        # Audit error 5.12: all exhausted → defer
        return None

    available.sort(key=lambda a: (a.priority_order, a.from_account))
    rr_idx = _hash_to_index(recipient_email, len(available), offset=attempt_count)
    return available[rr_idx]


# ============================================================================
# CONVENIENCE: just the account string (for queue writer)
# ============================================================================

def pick_sender_email(
    recipient_email: str,
    attempt_count: int = 0,
) -> Optional[str]:
    """
    Return just the from_account string for the chosen sender, or None if
    all accounts exhausted.
    """
    account = pick_sender_account(recipient_email, attempt_count)
    return account.from_account if account else None
