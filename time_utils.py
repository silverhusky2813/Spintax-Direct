"""
time_utils.py
==============
Shared timestamp handling for Python ↔ Apps Script interoperability.

The problem (audit error 3.8):
  Apps Script's `new Date()` produces strings like:
    "Mon Apr 22 2026 14:30:00 GMT-0700 (Pacific Daylight Time)"
  Python's `datetime.isoformat()` produces:
    "2026-04-22T14:30:00.123456"

Mixing these in the same Sheets column causes sort failures and parse errors.

The fix:
  - All Python writes use ISO 8601 (this module's `now_iso()`)
  - The Apps Script equivalent (apps_script_v2.gs) writes ISO via
    Utilities.formatDate(d, 'UTC', "yyyy-MM-dd'T'HH:mm:ssXXX")
  - Both sides parse defensively via `safe_parse_date()`

If you're seeing date-related bugs anywhere in the pipeline, check that
the writer uses one of these functions.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional, Union

from dateutil import parser as date_parser


# ============================================================================
# WRITING (Python side)
# ============================================================================

def now_iso() -> str:
    """
    Current timestamp in ISO 8601 UTC format.

    Use this EVERYWHERE you need to write a timestamp to Sheets from Python.

    Example: '2026-05-21T14:30:00+00:00'
    """
    return datetime.now(timezone.utc).isoformat()


def now_iso_naive() -> str:
    """
    ISO 8601 without timezone info — for fields where timezone is implicit.
    Less precise than now_iso() but matches existing rows that lack TZ data.

    Example: '2026-05-21T14:30:00.123456'
    """
    return datetime.now().isoformat()


# ============================================================================
# READING (parses ANY format we might encounter)
# ============================================================================

def safe_parse_date(value: Union[str, datetime, int, float, None]) -> Optional[datetime]:
    """
    Defensive date parser. Handles:
      - ISO 8601 strings (Python-written)
      - Apps Script Date string format ('Mon Apr 22 2026 14:30:00 GMT-0700')
      - Sheets serial number (days since 1899-12-30)
      - Pre-existing datetime objects (passthrough)
      - None / empty → returns None (not an error)

    Returns:
        datetime object (naive or aware depending on input) or None.

    Used everywhere we read timestamps from Sheets to avoid format-dependent
    parsing bugs (audit error 3.1 originally).
    """
    if value is None or value == "":
        return None

    if isinstance(value, datetime):
        return value

    # Sheets stores some dates as serial numbers (days since epoch)
    if isinstance(value, (int, float)):
        try:
            return datetime(1899, 12, 30) + timedelta(days=float(value))
        except (ValueError, OverflowError):
            return None

    # String — strip Apps Script's trailing parenthetical TZ name first
    # ("Mon Apr 22 2026 14:30:00 GMT-0700 (Pacific Daylight Time)")
    s = str(value).strip()
    # Remove trailing "(...)" if present — dateutil chokes on TZ name annotations
    import re
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s)

    # Try dateutil's fuzzy parser
    try:
        return date_parser.parse(s)
    except (ValueError, TypeError, date_parser.ParserError):
        return None


# ============================================================================
# DISPLAY FORMATTING (for UI)
# ============================================================================

def format_for_display(value, fallback: str = "—") -> str:
    """
    Format a timestamp for human display in UI.
    Returns fallback if parsing fails.
    """
    dt = safe_parse_date(value)
    if not dt:
        return fallback
    return dt.strftime("%Y-%m-%d %H:%M")


def format_age(value) -> str:
    """
    Human-readable relative age.

    Examples: '2 min ago', '3 hours ago', '5 days ago', 'just now'
    """
    dt = safe_parse_date(value)
    if not dt:
        return "—"

    # Normalize to naive for comparison
    if dt.tzinfo:
        dt = dt.replace(tzinfo=None)

    delta = datetime.now() - dt
    seconds = delta.total_seconds()

    if seconds < 60:
        return "just now"
    if seconds < 3600:
        mins = int(seconds / 60)
        return f"{mins} min ago"
    if seconds < 86400:
        hours = int(seconds / 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"

    days = int(seconds / 86400)
    return f"{days} day{'s' if days != 1 else ''} ago"
