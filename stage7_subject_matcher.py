"""
stage7_subject_matcher.py
==========================
Match an inbound reply to the original sent email row.

Solves audit errors:
  - 7.4: Robust matching — thread ID primary, subject+recipient fallback
  - 7.11: Normalize "Re:" / "RE:" / "Re: Re:" prefixes before comparison
  - 7.13: When multiple sent rows match, pick the MOST RECENT sent email

Pure functions. The Apps Script scan (v4) does the Gmail reading and calls
logic mirroring this; this Python version is the source of truth + test target.

Matching strategy:
  1. If reply has a thread_id AND a sent row has the same thread_id → match (best)
  2. Else normalize subjects, match on (core_subject + sender==recipient) within
     a recency window → match the most recently sent candidate
"""

import re
from datetime import datetime, timezone
from typing import Optional

from stage1_validation import normalize_email
from time_utils import safe_parse_date


# ============================================================================
# SUBJECT NORMALIZATION (audit error 7.11)
# ============================================================================

# Matches one or more reply/forward prefixes at the start: "Re:", "RE:",
# "Fwd:", "FW:", "Re: Re:", "回复:" etc. Repeated and case-insensitive.
REPLY_PREFIX_PATTERN = re.compile(
    r"^(\s*(re|fwd|fw|aw|wg|rv|sv|antw)\s*:\s*)+",
    flags=re.IGNORECASE,
)


def normalize_subject(subject: str) -> str:
    """
    Strip reply/forward prefixes and normalize whitespace+case for comparison.

    Examples:
        "Re: Confirmed media buy"        → "confirmed media buy"
        "RE: RE: Confirmed media buy"    → "confirmed media buy"
        "Fwd: Re: Confirmed media buy"   → "confirmed media buy"
        "Confirmed media buy"            → "confirmed media buy"
    """
    if not subject:
        return ""
    # Repeatedly strip prefixes (handles "Re: Fwd: Re:")
    s = str(subject)
    prev = None
    while prev != s:
        prev = s
        s = REPLY_PREFIX_PATTERN.sub("", s, count=1)
    # Normalize whitespace + case
    return re.sub(r"\s+", " ", s.strip().lower())


# ============================================================================
# THREAD ID MATCHING (primary)
# ============================================================================

def match_by_thread_id(
    reply_thread_id: str,
    sent_rows: list[dict],
) -> Optional[dict]:
    """
    Find the sent row whose thread_id matches the reply's thread.
    If multiple rows share the thread, return the most recently sent one
    (audit error 7.13).
    """
    if not reply_thread_id:
        return None

    candidates = [
        r for r in sent_rows
        if str(r.get("thread_id", "")).strip() == str(reply_thread_id).strip()
        and str(r.get("thread_id", "")).strip() != ""
    ]
    if not candidates:
        return None

    return _most_recently_sent(candidates)


# ============================================================================
# SUBJECT + RECIPIENT MATCHING (fallback)
# ============================================================================

def match_by_subject(
    reply_from_email: str,
    reply_subject: str,
    sent_rows: list[dict],
) -> Optional[dict]:
    """
    Fallback match: the reply's sender must equal a sent row's recipient,
    AND the normalized subjects must match. Return the most recently sent.

    This catches replies where thread_id wasn't captured (e.g., emails sent
    before the v4 Apps Script retrofit).
    """
    reply_sender_norm = normalize_email(reply_from_email)
    reply_subject_norm = normalize_subject(reply_subject)

    if not reply_sender_norm or not reply_subject_norm:
        return None

    candidates = []
    for r in sent_rows:
        # The reply's sender should be the row's recipient
        if normalize_email(r.get("recipient_email", "")) != reply_sender_norm:
            continue
        # Subjects must match after normalization
        if normalize_subject(r.get("subject", "")) != reply_subject_norm:
            continue
        candidates.append(r)

    if not candidates:
        return None

    return _most_recently_sent(candidates)


# ============================================================================
# COMBINED MATCH (thread primary, subject fallback)
# ============================================================================

def match_reply_to_sent(
    reply_thread_id: str,
    reply_from_email: str,
    reply_subject: str,
    sent_rows: list[dict],
) -> Optional[dict]:
    """
    Full matching pipeline (user's choice: both strategies).

    1. Try thread ID match (most reliable)
    2. Fall back to subject + recipient match

    Returns the matched sent row dict, or None if no match.
    """
    # Primary: thread ID
    match = match_by_thread_id(reply_thread_id, sent_rows)
    if match:
        return match

    # Fallback: subject + recipient
    return match_by_subject(reply_from_email, reply_subject, sent_rows)


# ============================================================================
# HELPERS
# ============================================================================

def _most_recently_sent(rows: list[dict]) -> dict:
    """
    From a list of candidate sent rows, return the one with the latest
    sent_at (audit error 7.13: a reply addresses the most recent send).
    """
    def sent_epoch(row: dict) -> float:
        dt = safe_parse_date(row.get("sent_at"))
        if dt is None:
            # Fall back to queued_at, then 0
            dt = safe_parse_date(row.get("queued_at"))
        if dt is None:
            return 0.0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()

    return max(rows, key=sent_epoch)
