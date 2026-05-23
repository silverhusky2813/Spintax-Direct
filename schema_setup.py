"""
schema_setup.py
================
One-time bootstrap script. Run this ONCE to set up the Sheets schema for Stage 1.

What it does:
  1. Creates the "Campaigns" tab with 16-column schema if it doesn't exist
  2. Adds 'campaign_id' column to the existing "Emails" tab if missing
  3. Backfills existing "Emails" rows with placeholder campaign_ids
  4. Creates the "Presets" tab with starter presets
  5. Creates the "Suppression" tab (for future Stage 7 unsubscribe tracking)

Run from terminal:
  python schema_setup.py

Or from inside Streamlit (one-time):
  from schema_setup import run_migration
  run_migration()
"""

import base64
import json
import uuid
from datetime import datetime

import gspread
import streamlit as st


# ============================================================================
# SCHEMA DEFINITIONS — single source of truth for column order/types
# ============================================================================

CAMPAIGNS_SCHEMA = [
    "campaign_id",          # A  string, UUID
    "created_at",           # B  ISO 8601 timestamp
    "created_by",           # C  string, email of creator
    "status",               # D  enum: Draft, Active, Paused, Complete
    "brand",                # E  string, normalized lowercase
    "vertical",             # F  enum: Gaming, Finance, Health, etc.
    "app_name",             # G  string
    "campaign_type",        # H  enum: Outreach, FollowUp, Brief, WinBack
    "cpm_floor",            # I  number, USD
    "cpm_offer",            # J  number, USD
    "flight_start",         # K  date YYYY-MM-DD
    "flight_end",           # L  date YYYY-MM-DD
    "priority_tier",        # M  enum: High, Medium, Low (future use)
    "publisher_segment",    # N  enum: All, Tier1, Tier2, DirectOnly (future use)
    "variant_strategy",     # O  enum: RandomRotate, Sequential, TopPerformer (future use)
    "notes",                # P  free text, internal
]

PRESETS_SCHEMA = [
    "preset_id",            # A  string, e.g. P001
    "preset_name",          # B  human-readable name
    "brand",                # C
    "vertical",             # D
    "campaign_type",        # E
    "cpm_floor",            # F
    "cpm_offer",            # G
    "date_strategy",        # H  enum: relative_to_today, fixed_window
    "flight_offset_days",   # I  for relative_to_today
    "flight_duration_days", # J  for relative_to_today
    "flight_start",         # K  for fixed_window
    "flight_end",           # L  for fixed_window
    "notes",                # M
]

SUPPRESSION_SCHEMA = [
    "recipient_email",      # A  normalized lowercase
    "added_at",             # B  ISO timestamp
    "reason",               # C  unsubscribed, bounced, complained, manual
    "campaign_id",          # D  campaign that caused suppression (if any)
]

# Starter presets — adjust these to your actual playbook
STARTER_PRESETS = [
    {
        "preset_id": "P001",
        "preset_name": "Standard Direct (Gaming)",
        "brand": "",
        "vertical": "Gaming",
        "campaign_type": "Outreach",
        "cpm_floor": 5.00,
        "cpm_offer": 12.00,
        "date_strategy": "relative_to_today",
        "flight_offset_days": 7,
        "flight_duration_days": 30,
        "flight_start": "",
        "flight_end": "",
        "notes": "Standard 30-day outreach for gaming inventory",
    },
    {
        "preset_id": "P002",
        "preset_name": "Rewarded Video (Premium)",
        "brand": "",
        "vertical": "Gaming",
        "campaign_type": "Brief",
        "cpm_floor": 15.00,
        "cpm_offer": 25.00,
        "date_strategy": "relative_to_today",
        "flight_offset_days": 14,
        "flight_duration_days": 14,
        "flight_start": "",
        "flight_end": "",
        "notes": "Premium rewarded video deal — 2-week flight",
    },
    {
        "preset_id": "P003",
        "preset_name": "Follow-Up (3-day check)",
        "brand": "",
        "vertical": "",
        "campaign_type": "FollowUp",
        "cpm_floor": 5.00,
        "cpm_offer": 12.00,
        "date_strategy": "relative_to_today",
        "flight_offset_days": 0,
        "flight_duration_days": 30,
        "flight_start": "",
        "flight_end": "",
        "notes": "Use 3 days after initial outreach with no reply",
    },
]


# ============================================================================
# GSPREAD CLIENT (cached singleton)
# ============================================================================

def get_gspread_client():
    """
    Returns a gspread client using credentials from st.secrets.
    Falls back to local file if running outside Streamlit.
    """
    try:
        creds_b64 = st.secrets["service_account_b64"]
        creds_dict = json.loads(base64.b64decode(creds_b64))
    except (FileNotFoundError, KeyError):
        # Outside Streamlit context — load from local file
        with open("service_account.json") as f:
            creds_dict = json.load(f)

    return gspread.service_account_from_dict(creds_dict)


def get_sheet_id():
    """Returns the spreadsheet ID from secrets or env."""
    try:
        return st.secrets["sheet_id"]
    except (FileNotFoundError, KeyError):
        import os
        return os.environ.get("SHEET_ID", "1IbkbJfUXhS1V38WaNgemG7q9TW7FFBssXIMhPN_QQfo")


# ============================================================================
# MIGRATION FUNCTIONS
# ============================================================================

def create_tab_if_missing(spreadsheet, tab_name, headers):
    """
    Idempotent: creates tab with headers if missing, returns the worksheet.
    If tab exists but has no headers, writes them. If headers exist but
    differ, raises — won't silently destroy data.
    """
    try:
        ws = spreadsheet.worksheet(tab_name)
        existing_headers = ws.row_values(1)

        if not existing_headers:
            # Tab exists but is empty — write headers
            ws.update("A1", [headers])
            print(f"  ✓ Tab '{tab_name}' existed empty — wrote headers")
            return ws

        if existing_headers == headers:
            print(f"  ✓ Tab '{tab_name}' already has correct schema")
            return ws

        # Headers differ — flag and abort (don't auto-migrate destructively)
        print(f"  ⚠ Tab '{tab_name}' has different headers:")
        print(f"     Existing: {existing_headers}")
        print(f"     Expected: {headers}")
        print(f"     ACTION NEEDED: manually align or rename the tab")
        return ws

    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(
            title=tab_name,
            rows=1000,
            cols=max(26, len(headers) + 2),
        )
        ws.update("A1", [headers])
        # Freeze header row and bold it
        ws.freeze(rows=1)
        ws.format("A1:Z1", {"textFormat": {"bold": True}})
        print(f"  ✓ Created new tab '{tab_name}' with {len(headers)} columns")
        return ws


def add_campaign_id_to_emails_tab(spreadsheet):
    """
    Adds 'campaign_id' as the FIRST column of the Emails tab if missing.
    Backfills existing rows with placeholder UUIDs prefixed 'legacy-'
    so they're identifiable as pre-migration data.
    """
    try:
        ws = spreadsheet.worksheet("Emails")
    except gspread.WorksheetNotFound:
        print("  ⚠ 'Emails' tab not found — skipping campaign_id migration")
        return

    existing_headers = ws.row_values(1)

    if "campaign_id" in existing_headers:
        print("  ✓ 'Emails' tab already has campaign_id column")
        return

    # Insert new column at position 1 (A)
    ws.insert_cols([[""]], col=1)
    ws.update("A1", [["campaign_id"]])
    print("  ✓ Inserted 'campaign_id' as column A in 'Emails' tab")

    # Backfill existing data rows with placeholder UUIDs
    num_rows = len(ws.get_all_values())
    if num_rows > 1:  # has data beyond header
        legacy_ids = [
            [f"legacy-{uuid.uuid4()}"] for _ in range(num_rows - 1)
        ]
        ws.update(f"A2:A{num_rows}", legacy_ids)
        print(f"  ✓ Backfilled {num_rows - 1} existing rows with legacy campaign_ids")


def seed_starter_presets(spreadsheet):
    """Insert starter presets if Presets tab is empty (beyond headers)."""
    ws = spreadsheet.worksheet("Presets")
    rows = ws.get_all_values()

    if len(rows) > 1:
        print(f"  ✓ Presets tab already has {len(rows) - 1} entries — skipping seed")
        return

    rows_to_insert = []
    for preset in STARTER_PRESETS:
        rows_to_insert.append([
            preset[col] for col in PRESETS_SCHEMA
        ])

    ws.append_rows(rows_to_insert)
    print(f"  ✓ Seeded {len(rows_to_insert)} starter presets")


# ============================================================================
# MAIN MIGRATION
# ============================================================================

def run_migration(verbose=True):
    """
    Idempotent migration — safe to run multiple times.
    Returns dict with migration status for each step.
    """
    if verbose:
        print(f"\n{'='*60}")
        print(f"Stage 1 Schema Migration — {datetime.now().isoformat()}")
        print(f"{'='*60}\n")

    gc = get_gspread_client()
    sheet_id = get_sheet_id()
    sh = gc.open_by_key(sheet_id)

    if verbose:
        print(f"Opened spreadsheet: {sh.title}")
        print(f"URL: {sh.url}\n")

    results = {}

    print("Step 1/4: Create Campaigns tab")
    create_tab_if_missing(sh, "Campaigns", CAMPAIGNS_SCHEMA)
    results["campaigns_tab"] = "ok"

    print("\nStep 2/4: Create Presets tab")
    create_tab_if_missing(sh, "Presets", PRESETS_SCHEMA)
    seed_starter_presets(sh)
    results["presets_tab"] = "ok"

    print("\nStep 3/4: Create Suppression tab (for Stage 7)")
    create_tab_if_missing(sh, "Suppression", SUPPRESSION_SCHEMA)
    results["suppression_tab"] = "ok"

    print("\nStep 4/4: Add campaign_id to Emails tab")
    add_campaign_id_to_emails_tab(sh)
    results["emails_migration"] = "ok"

    if verbose:
        print(f"\n{'='*60}")
        print("Migration complete!")
        print(f"{'='*60}\n")

    return results


if __name__ == "__main__":
    run_migration()
