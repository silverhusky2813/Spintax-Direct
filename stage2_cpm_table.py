"""
stage2_cpm_table.py
====================
Build the <<CPM_TABLE>> string from cpm_rates Sheets tab.

Solves audit errors:
  - 2.19: Graceful fallback when no rates exist for a vertical/geo
  - 2.20/2.21: Reads target_geo from campaign, defaults to "Global" if missing

The CPM_TABLE substitution renders inline in the email body as either:
  - A markdown table when rates are available
  - A fallback line "Floor CPM: $X / Offer CPM: $Y (full rate card on request)"
    when no detailed rates exist for the (vertical, geo) combination

Public API:
  build_cpm_table(vertical, geo, fallback_floor, fallback_offer) → str
"""

from typing import Optional

import gspread
import streamlit as st

from stage1_dedup import get_gspread_client
from stage1_validation import normalize_string


# ============================================================================
# CACHED READS
# ============================================================================

@st.cache_data(ttl=300)
def _load_all_cpm_rates() -> list[dict]:
    """Load all rows from cpm_rates tab. Cached 5 min (rates change rarely)."""
    gc = get_gspread_client()
    sh = gc.open_by_key(st.secrets["sheet_id"])

    try:
        ws = sh.worksheet("cpm_rates")
    except gspread.WorksheetNotFound:
        return []

    return ws.get_all_records()


# ============================================================================
# RATE LOOKUP
# ============================================================================

def _filter_rates(
    vertical: str,
    geo: str,
    rates: Optional[list[dict]] = None,
) -> list[dict]:
    """
    Find rate rows matching the given (vertical, geo).
    Falls back to "Global" geo if no exact match.
    """
    if rates is None:
        rates = _load_all_cpm_rates()

    vertical_norm = normalize_string(vertical)
    geo_norm = normalize_string(geo) if geo else "global"

    # First pass: exact vertical + geo match
    exact = [
        r for r in rates
        if normalize_string(r.get("vertical")) == vertical_norm
        and normalize_string(r.get("geo")) == geo_norm
    ]
    if exact:
        return exact

    # Fallback: same vertical, "Global" geo
    if geo_norm != "global":
        global_match = [
            r for r in rates
            if normalize_string(r.get("vertical")) == vertical_norm
            and normalize_string(r.get("geo")) == "global"
        ]
        if global_match:
            return global_match

    # No rates for this vertical at all
    return []


# ============================================================================
# TABLE FORMATTING
# ============================================================================

def _format_cpm(value) -> str:
    """Format a CPM value as $X.XX. Handles strings and numbers."""
    try:
        n = float(value)
        return f"${n:.2f}"
    except (ValueError, TypeError):
        return "—"


def _format_table_markdown(rows: list[dict]) -> str:
    """
    Render rate rows as a markdown table (renders well in most email clients).

    Output example:
        | Format       | Floor   | Ceiling |
        |--------------|---------|---------|
        | Banner       | $0.50   | $1.50   |
        | Interstitial | $5.00   | $12.00  |
        | Rewarded     | $15.00  | $25.00  |
    """
    if not rows:
        return ""

    # Sort by ad_format for consistent presentation
    format_order = ["Banner", "Native", "Interstitial", "Rewarded"]
    rows_sorted = sorted(
        rows,
        key=lambda r: format_order.index(r.get("ad_format", ""))
        if r.get("ad_format") in format_order
        else 99,
    )

    lines = [
        "| Format       | Floor    | Ceiling  |",
        "|--------------|----------|----------|",
    ]
    for r in rows_sorted:
        fmt = str(r.get("ad_format", "—"))[:12].ljust(12)
        floor = _format_cpm(r.get("cpm_floor")).ljust(8)
        ceiling = _format_cpm(r.get("cpm_ceiling")).ljust(8)
        lines.append(f"| {fmt} | {floor} | {ceiling} |")

    return "\n".join(lines)


# ============================================================================
# PUBLIC API
# ============================================================================

def build_cpm_table(
    vertical: str,
    geo: str,
    fallback_floor: float = 0.0,
    fallback_offer: float = 0.0,
) -> tuple[str, bool]:
    """
    Build the CPM_TABLE string for substitution into an email template.

    Args:
        vertical: Campaign vertical (Gaming, Finance, etc.)
        geo: Target GEO (US, UK, Global, etc.)
        fallback_floor: cpm_floor from Stage 1 campaign (used if no rates)
        fallback_offer: cpm_offer from Stage 1 campaign (used if no rates)

    Returns:
        (cpm_table_string, used_fallback)

        cpm_table_string: The text to substitute for <<CPM_TABLE>>
        used_fallback: True if we couldn't find rates and fell back to
                       the simple format. UI should warn.
    """
    rates = _filter_rates(vertical, geo)

    if rates:
        return _format_table_markdown(rates), False

    # Fallback: simple inline line using Stage 1 CPM numbers
    fallback_line = (
        f"Floor CPM: {_format_cpm(fallback_floor)}  |  "
        f"Offer CPM: {_format_cpm(fallback_offer)}  "
        f"(full rate card available on request)"
    )

    return fallback_line, True


def get_available_geos_for_vertical(vertical: str) -> list[str]:
    """Return list of GEOs for which we have rates in this vertical."""
    rates = _load_all_cpm_rates()
    vertical_norm = normalize_string(vertical)
    geos = {
        r.get("geo") for r in rates
        if normalize_string(r.get("vertical")) == vertical_norm
    }
    return sorted([g for g in geos if g])


def get_cpm_coverage_summary() -> dict[str, dict[str, int]]:
    """
    Return matrix of (vertical → geo → format_count).
    Used by an admin UI to see where rate data is missing.

    Example return:
        {
            "Gaming":   {"US": 3, "UK": 3, "Global": 3},
            "Finance":  {"US": 2},
            "Shopping": {"US": 2},
        }
    """
    rates = _load_all_cpm_rates()
    matrix: dict[str, dict[str, int]] = {}

    for r in rates:
        v = r.get("vertical")
        g = r.get("geo")
        if not v or not g:
            continue
        matrix.setdefault(v, {})
        matrix[v][g] = matrix[v].get(g, 0) + 1

    return matrix
