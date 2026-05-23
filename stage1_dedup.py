"""
stage1_dedup.py
================
Publisher dedup checker with campaign-type-aware rules.

Corrections applied from audit:
  - Defensive date parsing (handles Apps Script date strings, not just ISO)
  - Case-insensitive matching (uses normalized brand/email)
  - Campaign-type-aware: FollowUp INVERTS the check (requires prior contact)
  - Cached gspread client (no re-auth on every call)
  - Cached Sheet reads (60s TTL) for performance
"""

import base64
import json
from datetime import datetime, timedelta
from typing import Literal

import gspread
import streamlit as st
from dateutil import parser as date_parser

from stage1_validation import normalize_brand, normalize_email, normalize_string


# ============================================================================
# DEDUP RULES — single source of truth
# ============================================================================

# Days within which "already contacted" warning fires, per campaign type
DEDUP_WINDOWS_DAYS = {
    "Outreach": 30,   # Don't re-outreach within 30 days
    "Brief": 14,      # Don't send 2 briefs within 14 days
    "WinBack": 90,    # Long gap before win-back attempts
    # FollowUp uses an INVERTED rule (see check function)
}

# For FollowUp: prior contact must be within this many days to be valid
FOLLOWUP_VALID_WINDOW_DAYS = 60


# ============================================================================
# CACHED GSPREAD CLIENT (singleton)
# ============================================================================

@st.cache_resource
def get_gspread_client():
    """Cached gspread client — created once per Streamlit session."""
    creds_b64 = st.secrets["service_account_b64"]
    creds_dict = json.loads(base64.b64decode(creds_b64))
    return gspread.service_account_from_dict(creds_dict)


@st.cache_data(ttl=60)
def _load_emails_history() -> list[dict]:
    """
    Load all rows from Emails tab. Cached for 60 seconds.

    This is called by dedup checks — we tolerate slight staleness because:
      - Users send a few emails per minute at most
      - The cost of a stale cache (occasional false-negative dedup) is
        much lower than the cost of re-loading 1000+ rows on every check
    """
    gc = get_gspread_client()
    sh = gc.open_by_key(st.secrets["sheet_id"])
    try:
        ws = sh.worksheet("Emails")
    except gspread.WorksheetNotFound:
        return []
    return ws.get_all_records()


# ============================================================================
# DEFENSIVE DATE PARSING
# ============================================================================

def safe_parse_date(date_str) -> datetime | None:
    """
    Handle multiple date formats from Sheets / Apps Script.

    Apps Script writes timestamps via `new Date()`, which produces strings like:
      "Mon Apr 22 2026 14:30:00 GMT-0700 (Pacific Daylight Time)"
    Sheets may also store as ISO strings, serial numbers, etc.

    Uses dateutil.parser as a fuzzy fallback. Returns None on failure.
    """
    if date_str is None or date_str == "":
        return None

    # Already a datetime
    if isinstance(date_str, datetime):
        return date_str

    # Excel-style serial number (days since 1899-12-30)
    if isinstance(date_str, (int, float)):
        try:
            return datetime(1899, 12, 30) + timedelta(days=float(date_str))
        except (ValueError, OverflowError):
            return None

    # String — try dateutil
    try:
        return date_parser.parse(str(date_str))
    except (ValueError, TypeError, date_parser.ParserError):
        return None


# ============================================================================
# DEDUP RESULT TYPES
# ============================================================================

DedupStatus = Literal[
    "ok",                  # Safe to send
    "duplicate",           # Already contacted recently — block/warn
    "no_prior_contact",    # FollowUp without prior Outreach — block
    "stale_contact",       # FollowUp where prior contact was too long ago
]


# ============================================================================
# MAIN DEDUP CHECK
# ============================================================================

def check_publisher_contact_history(
    publisher_email: str,
    brand: str,
    vertical: str,
    campaign_type: str,
) -> tuple[DedupStatus, str, list[dict]]:
    """
    Check if it's safe to send this email.

    Returns:
        (status, human_readable_message, list_of_relevant_past_emails)

    Logic:
      - FollowUp: REQUIRES prior contact within FOLLOWUP_VALID_WINDOW_DAYS days
                  for same (publisher, brand, vertical)
      - All others: BLOCKS if contacted within DEDUP_WINDOWS_DAYS[type] days
    """
    publisher_normalized = normalize_email(publisher_email)
    brand_normalized = normalize_brand(brand)
    vertical_normalized = normalize_string(vertical)

    emails = _load_emails_history()
    now = datetime.now()

    # Find all prior contacts for (publisher, brand, vertical), normalized
    prior_contacts = []
    for row in emails:
        if normalize_email(row.get("recipient_email", "")) != publisher_normalized:
            continue
        if normalize_brand(row.get("brand", "")) != brand_normalized:
            continue
        if normalize_string(row.get("vertical", "")) != vertical_normalized:
            continue

        # Only count rows that actually sent (skip Failed, Draft, Queued)
        status = normalize_string(row.get("status", ""))
        if status not in ("sent", "delivered"):
            continue

        sent_dt = safe_parse_date(row.get("sent_at"))
        if not sent_dt:
            continue

        prior_contacts.append({
            "sent_at": sent_dt,
            "campaign_id": row.get("campaign_id", ""),
            "campaign_type": row.get("campaign_type", ""),
            "subject": row.get("subject", ""),
        })

    # Sort by recency
    prior_contacts.sort(key=lambda x: x["sent_at"], reverse=True)

    # ----------------------------------------------------------------------
    # FollowUp logic: INVERTED — must have prior contact
    # ----------------------------------------------------------------------
    if campaign_type == "FollowUp":
        if not prior_contacts:
            return (
                "no_prior_contact",
                f"Cannot send FollowUp — no prior contact found for "
                f"{publisher_email} on {brand} × {vertical}. "
                f"Send an Outreach first.",
                [],
            )

        most_recent = prior_contacts[0]
        days_ago = (now - most_recent["sent_at"]).days

        if days_ago > FOLLOWUP_VALID_WINDOW_DAYS:
            return (
                "stale_contact",
                f"Last contact was {days_ago} days ago "
                f"(over {FOLLOWUP_VALID_WINDOW_DAYS}-day window). "
                f"Consider sending fresh Outreach instead.",
                prior_contacts,
            )

        return (
            "ok",
            f"Following up on contact from {days_ago} days ago. "
            f"({len(prior_contacts)} prior touch{'es' if len(prior_contacts) > 1 else ''})",
            prior_contacts,
        )

    # ----------------------------------------------------------------------
    # All other types: standard dedup window
    # ----------------------------------------------------------------------
    window_days = DEDUP_WINDOWS_DAYS.get(campaign_type, 30)
    cutoff = now - timedelta(days=window_days)

    recent_contacts = [c for c in prior_contacts if c["sent_at"] > cutoff]

    if recent_contacts:
        most_recent = recent_contacts[0]
        days_ago = (now - most_recent["sent_at"]).days
        return (
            "duplicate",
            f"Already contacted {days_ago} days ago "
            f"(within {window_days}-day {campaign_type} window). "
            f"Campaign ID: {most_recent['campaign_id']}",
            recent_contacts,
        )

    return (
        "ok",
        f"Safe to send. "
        + (f"{len(prior_contacts)} prior contact(s) outside dedup window."
           if prior_contacts else "No prior contact history."),
        prior_contacts,
    )


# ============================================================================
# SUPPRESSION LIST CHECK (Stage 7 foundation)
# ============================================================================

@st.cache_data(ttl=300)
def _load_suppression_list() -> set[str]:
    """Load suppressed emails (unsubscribed, bounced). Cached 5 min."""
    gc = get_gspread_client()
    sh = gc.open_by_key(st.secrets["sheet_id"])
    try:
        ws = sh.worksheet("Suppression")
    except gspread.WorksheetNotFound:
        return set()
    records = ws.get_all_records()
    return {normalize_email(r.get("recipient_email", "")) for r in records}


def is_suppressed(email: str) -> tuple[bool, str | None]:
    """
    Check if email is on the suppression list.
    Returns (is_suppressed, reason_if_known).
    """
    normalized = normalize_email(email)
    suppressed_set = _load_suppression_list()

    if normalized in suppressed_set:
        # Look up the reason
        gc = get_gspread_client()
        sh = gc.open_by_key(st.secrets["sheet_id"])
        try:
            ws = sh.worksheet("Suppression")
            records = ws.get_all_records()
            for r in records:
                if normalize_email(r.get("recipient_email", "")) == normalized:
                    return True, r.get("reason", "unknown")
        except gspread.WorksheetNotFound:
            pass
        return True, "unknown"

    return False, None
