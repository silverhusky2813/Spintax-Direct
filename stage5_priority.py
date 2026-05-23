"""
stage5_priority.py
===================
Compute a single sortable priority score for queue ordering.

Solves audit errors:
  - 5.1: Single numeric column means Apps Script sorts cheaply (no in-memory sort of dicts)
  - 5.16: Composite score avoids collisions — tier dominates, age breaks ties

The score is computed at QUEUE TIME (Stage 3) and stored in the priority_score
column. Apps Script just sorts descending by this number — highest first.

Score formula:
    score = tier_weight * TIER_MULTIPLIER - queued_at_epoch_seconds

Why this works:
  - tier_weight: High=3, Medium=2, Low=1. Multiplied by a large constant so
    tier ALWAYS dominates age.
  - Subtracting queued_at_epoch means OLDER rows (smaller epoch) get a LARGER
    score → sorted first within the same tier (FIFO within tier).

Example:
  High tier queued at epoch 1_700_000_000 → 3*1e12 - 1.7e9 = 2_999_998_300_000_000
  High tier queued at epoch 1_700_000_060 → 3*1e12 - 1.7e9 = 2_999_998_299_999_940
  (older High sorts higher — correct)

  Medium tier (any age) → ~2e12, always below any High tier (~3e12). Correct.
"""

from datetime import datetime, timezone
from typing import Union


# ============================================================================
# CONSTANTS
# ============================================================================

TIER_WEIGHTS = {
    "high": 3,
    "medium": 2,
    "low": 1,
}

DEFAULT_TIER_WEIGHT = 2  # Treat unknown/empty as Medium

# Large multiplier so tier dominates age. Epoch seconds are ~1.7e9, so a
# multiplier of 1e12 guarantees one tier-step outweighs ~31,000 years of age.
TIER_MULTIPLIER = 1_000_000_000_000  # 1e12


# ============================================================================
# CORE
# ============================================================================

def tier_weight(priority_tier: str) -> int:
    """Map a priority tier string to its numeric weight (case-insensitive)."""
    if not priority_tier:
        return DEFAULT_TIER_WEIGHT
    return TIER_WEIGHTS.get(str(priority_tier).strip().lower(), DEFAULT_TIER_WEIGHT)


def to_epoch_seconds(ts: Union[str, datetime, None]) -> int:
    """
    Convert a timestamp to integer epoch seconds (UTC).

    Falls back to current time if unparseable — a row with a broken timestamp
    should sort as if just-queued (low priority within its tier), not crash.
    """
    from time_utils import safe_parse_date

    if ts is None or ts == "":
        dt = datetime.now(timezone.utc)
    else:
        dt = safe_parse_date(ts)
        if dt is None:
            dt = datetime.now(timezone.utc)

    # Normalize to UTC epoch
    if dt.tzinfo is None:
        # Assume naive timestamps are UTC
        dt = dt.replace(tzinfo=timezone.utc)

    return int(dt.timestamp())


def compute_priority_score(
    priority_tier: str,
    queued_at: Union[str, datetime],
) -> int:
    """
    Compute the sortable priority score.

    Higher score = sent sooner. Apps Script sorts descending by this.

    Args:
        priority_tier: 'High' | 'Medium' | 'Low' (from Stage 1 campaign)
        queued_at: ISO timestamp when the row was queued

    Returns:
        Integer score. Larger = higher priority.
    """
    weight = tier_weight(priority_tier)
    epoch = to_epoch_seconds(queued_at)

    # Tier dominates; older (smaller epoch) → larger score within tier
    return weight * TIER_MULTIPLIER - epoch


def describe_score(score: int) -> str:
    """
    Reverse-engineer a human-readable description of a priority score.
    Useful for debugging / the dashboard.
    """
    # Determine tier by integer division
    tier_num = score // TIER_MULTIPLIER
    # Account for the subtracted epoch possibly crossing a tier boundary —
    # but since epoch (~1.7e9) << TIER_MULTIPLIER (1e12), the division is safe.
    tier_name = {3: "High", 2: "Medium", 1: "Low"}.get(tier_num, "Unknown")

    # Recover approximate epoch
    epoch = tier_num * TIER_MULTIPLIER - score
    try:
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
        age_str = dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, OverflowError, OSError):
        age_str = "unknown time"

    return f"{tier_name} tier, queued {age_str}"


# ============================================================================
# SORTING HELPER (for Python-side queue preview)
# ============================================================================

def sort_rows_by_priority(rows: list[dict]) -> list[dict]:
    """
    Sort a list of Emails rows by priority_score descending (highest first).

    Used by the dashboard to preview send order. Apps Script does its own
    sort at send time, but this lets the UI show "next to send" accurately.

    Rows missing priority_score are computed on the fly from
    priority_tier + queued_at (graceful for pre-Stage-5 rows).
    """
    def get_score(row: dict) -> int:
        existing = row.get("priority_score", "")
        if existing not in ("", None):
            try:
                return int(existing)
            except (ValueError, TypeError):
                pass
        # Compute on the fly for rows that predate priority_score
        return compute_priority_score(
            row.get("priority_tier", ""),
            row.get("queued_at", ""),
        )

    return sorted(rows, key=get_score, reverse=True)
