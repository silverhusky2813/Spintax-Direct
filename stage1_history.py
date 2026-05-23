"""
stage1_history.py
==================
Load past campaigns and saved presets for the "Load from..." dropdown.

Corrections applied from audit:
  - Uses cached gspread client (no re-auth per call)
  - Graceful fallback when Campaigns tab doesn't exist yet (during migration)
  - No fake performance data — only shows what we actually have
  - Presets and history merged into one "load source" concept
"""

from datetime import date, datetime, timedelta

import gspread
import streamlit as st

from stage1_dedup import get_gspread_client


# ============================================================================
# CAMPAIGN HISTORY
# ============================================================================

@st.cache_data(ttl=60)
def load_campaign_history(num_recent: int = 10) -> list[dict]:
    """
    Load most recent campaigns from the Campaigns tab.

    Returns empty list (not error) if the tab doesn't exist yet —
    this is expected before schema migration runs.
    """
    gc = get_gspread_client()
    sh = gc.open_by_key(st.secrets["sheet_id"])

    try:
        ws = sh.worksheet("Campaigns")
    except gspread.WorksheetNotFound:
        return []

    records = ws.get_all_records()
    if not records:
        return []

    # Sort by created_at, most recent first
    # Use empty string fallback so missing timestamps sort to the end
    sorted_records = sorted(
        records,
        key=lambda x: x.get("created_at", ""),
        reverse=True,
    )

    return sorted_records[:num_recent]


def format_campaign_for_display(campaign: dict) -> str:
    """Format a campaign dict as a display string for dropdowns."""
    brand = campaign.get("brand", "?")
    vertical = campaign.get("vertical", "?")
    ctype = campaign.get("campaign_type", "?")
    created = campaign.get("created_at", "")[:10]  # YYYY-MM-DD
    return f"{brand} × {vertical} ({ctype}) — {created}"


def get_brand_history_summary(brand: str) -> dict:
    """
    Aggregate stats for a brand across all past campaigns.
    Returns ONLY data we actually have — no fake metrics.

    Returns:
        {
            "total_campaigns": int,
            "last_sent_date": str | None,
            "verticals_used": list[str],
            "campaign_types_used": list[str],
        }
    """
    from stage1_validation import normalize_brand

    brand_norm = normalize_brand(brand)

    gc = get_gspread_client()
    sh = gc.open_by_key(st.secrets["sheet_id"])

    try:
        ws = sh.worksheet("Campaigns")
        campaigns = ws.get_all_records()
    except gspread.WorksheetNotFound:
        return {
            "total_campaigns": 0,
            "last_sent_date": None,
            "verticals_used": [],
            "campaign_types_used": [],
        }

    matching = [
        c for c in campaigns
        if normalize_brand(c.get("brand", "")) == brand_norm
    ]

    if not matching:
        return {
            "total_campaigns": 0,
            "last_sent_date": None,
            "verticals_used": [],
            "campaign_types_used": [],
        }

    # Last sent
    sorted_matching = sorted(
        matching,
        key=lambda x: x.get("created_at", ""),
        reverse=True,
    )
    last_sent = sorted_matching[0].get("created_at", "")[:10]

    # Unique verticals + types
    verticals = sorted({c.get("vertical", "") for c in matching if c.get("vertical")})
    types = sorted({c.get("campaign_type", "") for c in matching if c.get("campaign_type")})

    return {
        "total_campaigns": len(matching),
        "last_sent_date": last_sent,
        "verticals_used": verticals,
        "campaign_types_used": types,
    }


# ============================================================================
# PRESETS
# ============================================================================

@st.cache_data(ttl=300)
def load_presets() -> list[dict]:
    """Load all presets from the Presets tab. Cached 5 minutes."""
    gc = get_gspread_client()
    sh = gc.open_by_key(st.secrets["sheet_id"])

    try:
        ws = sh.worksheet("Presets")
    except gspread.WorksheetNotFound:
        return []

    return ws.get_all_records()


def apply_preset_dates(preset: dict, today: date = None) -> tuple[date, date]:
    """
    Calculate flight_start and flight_end from a preset, based on its date_strategy.

    Strategies:
      - relative_to_today: start = today + offset, end = start + duration
      - fixed_window: use the explicit start/end dates

    Returns (flight_start, flight_end) as date objects.
    """
    if today is None:
        today = date.today()

    strategy = preset.get("date_strategy", "relative_to_today")

    if strategy == "fixed_window":
        start_str = preset.get("flight_start", "")
        end_str = preset.get("flight_end", "")
        if not start_str or not end_str:
            # Fallback to relative if fixed dates missing
            strategy = "relative_to_today"
        else:
            try:
                start = datetime.fromisoformat(str(start_str)[:10]).date()
                end = datetime.fromisoformat(str(end_str)[:10]).date()
                return start, end
            except (ValueError, TypeError):
                strategy = "relative_to_today"

    # relative_to_today (default and fallback)
    try:
        offset = int(preset.get("flight_offset_days", 7))
    except (ValueError, TypeError):
        offset = 7

    try:
        duration = int(preset.get("flight_duration_days", 30))
    except (ValueError, TypeError):
        duration = 30

    start = today + timedelta(days=offset)
    end = start + timedelta(days=duration)
    return start, end


def format_preset_for_display(preset: dict) -> str:
    """Display string for preset dropdowns."""
    name = preset.get("preset_name", "Unnamed")
    vertical = preset.get("vertical") or "Any vertical"
    ctype = preset.get("campaign_type") or "Any type"
    return f"{name} — {vertical} / {ctype}"
