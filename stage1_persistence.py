"""
stage1_persistence.py
======================
Write campaign data to the Campaigns tab with idempotency.

Solves:
  - Issue A (state safety): saves draft immediately after validation so it
    survives page refresh / Streamlit reruns
  - Issue B (schema): uses CAMPAIGNS_SCHEMA from schema_setup.py as single source

Idempotency:
  - Same campaign_id = UPDATE existing row
  - New campaign_id = INSERT new row
  - Prevents accidental duplicates from double-clicks or page reloads
"""

import uuid
from datetime import datetime
from typing import Optional

import gspread
import streamlit as st

from schema_setup import CAMPAIGNS_SCHEMA
from stage1_dedup import get_gspread_client
from stage1_validation import normalize_campaign_input


def generate_campaign_id() -> str:
    """Generate a new UUID4 for a campaign."""
    return str(uuid.uuid4())


def _row_dict_to_schema_list(campaign_data: dict) -> list:
    """Convert a campaign dict into a list ordered by CAMPAIGNS_SCHEMA."""
    return [campaign_data.get(col, "") for col in CAMPAIGNS_SCHEMA]


def save_campaign(
    campaign_data: dict,
    status: str = "Draft",
    created_by: Optional[str] = None,
) -> str:
    """
    Idempotent save: same campaign_id = UPDATE, new = INSERT.

    Args:
        campaign_data: validated and normalized campaign dict
        status: 'Draft' | 'Active' | 'Paused' | 'Complete'
        created_by: email of user creating it

    Returns:
        campaign_id of saved campaign
    """
    # Ensure we have a campaign_id
    if not campaign_data.get("campaign_id"):
        campaign_data["campaign_id"] = generate_campaign_id()

    # Set metadata
    campaign_data["status"] = status
    if "created_at" not in campaign_data or not campaign_data["created_at"]:
        campaign_data["created_at"] = datetime.now().isoformat()
    if created_by:
        campaign_data["created_by"] = created_by

    # Normalize before storage
    campaign_data = normalize_campaign_input(campaign_data)

    gc = get_gspread_client()
    sh = gc.open_by_key(st.secrets["sheet_id"])
    ws = sh.worksheet("Campaigns")

    # Look for existing row with this campaign_id (column A)
    campaign_id = campaign_data["campaign_id"]
    existing_cells = ws.findall(campaign_id, in_column=1)

    row_values = _row_dict_to_schema_list(campaign_data)

    if existing_cells:
        # UPDATE existing row
        row_num = existing_cells[0].row
        # Calculate range A{n}:{last_col}{n}
        last_col_letter = chr(ord("A") + len(CAMPAIGNS_SCHEMA) - 1)
        range_str = f"A{row_num}:{last_col_letter}{row_num}"
        ws.update(range_str, [row_values])
    else:
        # INSERT new row
        ws.append_row(row_values)

    # Invalidate the campaign history cache so the new entry shows up
    st.cache_data.clear()

    return campaign_id


def get_campaign(campaign_id: str) -> Optional[dict]:
    """Retrieve a campaign by ID. Returns None if not found."""
    gc = get_gspread_client()
    sh = gc.open_by_key(st.secrets["sheet_id"])
    ws = sh.worksheet("Campaigns")

    records = ws.get_all_records()
    for r in records:
        if r.get("campaign_id") == campaign_id:
            return r
    return None


def update_campaign_status(campaign_id: str, new_status: str) -> bool:
    """
    Update only the status field of a campaign.
    Returns True if successful.
    """
    valid_statuses = ["Draft", "Active", "Paused", "Complete"]
    if new_status not in valid_statuses:
        raise ValueError(f"Status must be one of: {valid_statuses}")

    gc = get_gspread_client()
    sh = gc.open_by_key(st.secrets["sheet_id"])
    ws = sh.worksheet("Campaigns")

    cells = ws.findall(campaign_id, in_column=1)
    if not cells:
        return False

    # Status is column D (index 4)
    status_col_index = CAMPAIGNS_SCHEMA.index("status") + 1
    row_num = cells[0].row
    ws.update_cell(row_num, status_col_index, new_status)

    st.cache_data.clear()
    return True
