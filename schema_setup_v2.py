"""
schema_setup_v2.py
===================
Idempotent Stage 2 schema migration. Run AFTER schema_setup.py.

Adds:
  1. 'Publishers' tab — recipient metadata lookup (first_name, publisher_name)
  2. 'cpm_rates' tab — CPM data keyed by (vertical, ad_format, geo)
  3. New columns on existing 'Campaigns' tab:
     - target_geo (where the campaign runs)
  4. New columns on existing 'Emails' tab:
     - template_id
     - template_version
     - spin_path_json (the chosen text at each spin position)
     - was_edited (boolean)
     - generated_at (timestamp of variant generation)

Safe to run multiple times — checks for existing columns/tabs before adding.

Run from terminal:
  python schema_setup_v2.py

Or from inside Streamlit:
  from schema_setup_v2 import run_migration_v2
  run_migration_v2()
"""

from datetime import datetime

import gspread

from schema_setup import (
    CAMPAIGNS_SCHEMA,
    create_tab_if_missing,
    get_gspread_client,
    get_sheet_id,
)


# ============================================================================
# NEW SCHEMA DEFINITIONS
# ============================================================================

PUBLISHERS_SCHEMA = [
    "publisher_email",      # A  PRIMARY KEY — normalized lowercase
    "first_name",           # B  e.g., "Alice"
    "last_name",            # C  e.g., "Chen"
    "publisher_name",       # D  company / org name, e.g., "Acme Mobile"
    "publisher_tier",       # E  Tier1 / Tier2 / Tier3 / Unverified
    "default_geo",          # F  hint for CPM_TABLE lookup if campaign GEO missing
    "notes",                # G  free text
    "created_at",           # H  ISO timestamp
    "updated_at",           # I  ISO timestamp (touched whenever row updates)
]

CPM_RATES_SCHEMA = [
    "vertical",             # A  PRIMARY KEY part 1 (Gaming, Finance, etc.)
    "ad_format",            # B  PRIMARY KEY part 2 (Banner, Interstitial, Rewarded, Native)
    "geo",                  # C  PRIMARY KEY part 3 (US, UK, DE, Global, etc.)
    "cpm_floor",            # D  USD
    "cpm_ceiling",          # E  USD (informational)
    "updated_at",           # F  ISO timestamp
    "notes",                # G  optional
]

# New campaigns column to add (single-add at end)
CAMPAIGNS_NEW_COLUMNS = [
    "target_geo",           # Q  Primary GEO for the campaign
]

# New emails columns for variant tracking
EMAILS_NEW_COLUMNS = [
    "template_id",          # Which template was used (e.g., "outreach_v1")
    "template_version",     # Version snapshot for path comparability
    "spin_path_json",       # JSON: which text was chosen at each spin position
    "was_edited",           # Bool: did user modify the generated variant before send
    "generated_at",         # When the variant was generated (not when sent)
]


# ============================================================================
# STARTER DATA
# ============================================================================

STARTER_CPM_RATES = [
    # Gaming
    {"vertical": "Gaming", "ad_format": "Banner",       "geo": "US",     "cpm_floor": 0.50,  "cpm_ceiling": 1.50,  "notes": "Avg US gaming banner"},
    {"vertical": "Gaming", "ad_format": "Interstitial", "geo": "US",     "cpm_floor": 5.00,  "cpm_ceiling": 12.00, "notes": "Avg US gaming interstitial"},
    {"vertical": "Gaming", "ad_format": "Rewarded",     "geo": "US",     "cpm_floor": 15.00, "cpm_ceiling": 25.00, "notes": "Avg US gaming rewarded video"},
    {"vertical": "Gaming", "ad_format": "Banner",       "geo": "UK",     "cpm_floor": 0.40,  "cpm_ceiling": 1.20,  "notes": ""},
    {"vertical": "Gaming", "ad_format": "Interstitial", "geo": "UK",     "cpm_floor": 4.00,  "cpm_ceiling": 9.00,  "notes": ""},
    {"vertical": "Gaming", "ad_format": "Rewarded",     "geo": "UK",     "cpm_floor": 12.00, "cpm_ceiling": 20.00, "notes": ""},
    {"vertical": "Gaming", "ad_format": "Banner",       "geo": "Global", "cpm_floor": 0.20,  "cpm_ceiling": 0.80,  "notes": "Tier-3 GEO blend"},
    {"vertical": "Gaming", "ad_format": "Interstitial", "geo": "Global", "cpm_floor": 2.00,  "cpm_ceiling": 5.00,  "notes": "Tier-3 GEO blend"},
    {"vertical": "Gaming", "ad_format": "Rewarded",     "geo": "Global", "cpm_floor": 6.00,  "cpm_ceiling": 12.00, "notes": "Tier-3 GEO blend"},

    # Finance — usually higher CPMs
    {"vertical": "Finance", "ad_format": "Banner",       "geo": "US", "cpm_floor": 2.00,  "cpm_ceiling": 6.00,  "notes": "US finance is premium"},
    {"vertical": "Finance", "ad_format": "Interstitial", "geo": "US", "cpm_floor": 12.00, "cpm_ceiling": 30.00, "notes": ""},

    # Shopping
    {"vertical": "Shopping", "ad_format": "Banner",       "geo": "US", "cpm_floor": 1.00, "cpm_ceiling": 3.00, "notes": ""},
    {"vertical": "Shopping", "ad_format": "Interstitial", "geo": "US", "cpm_floor": 6.00, "cpm_ceiling": 15.00, "notes": ""},
]


# ============================================================================
# COLUMN ADDITION (idempotent)
# ============================================================================

def add_columns_if_missing(worksheet, new_columns: list[str]) -> list[str]:
    """
    Add columns to the end of a worksheet if they don't already exist.

    Returns the list of columns that were actually added (empty if all
    already existed).
    """
    existing_headers = worksheet.row_values(1)
    added = []

    for col_name in new_columns:
        if col_name in existing_headers:
            continue

        # Append to the end
        next_col_index = len(existing_headers) + 1
        # Convert column index to letter(s)
        col_letter = _col_index_to_letter(next_col_index)

        # Ensure the sheet has enough columns
        if worksheet.col_count < next_col_index:
            worksheet.add_cols(next_col_index - worksheet.col_count)

        worksheet.update_cell(1, next_col_index, col_name)
        existing_headers.append(col_name)  # for next iteration
        added.append(col_name)
        print(f"  ✓ Added column '{col_name}' to '{worksheet.title}' at {col_letter}1")

    return added


def _col_index_to_letter(index: int) -> str:
    """1 → 'A', 26 → 'Z', 27 → 'AA', etc."""
    result = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


# ============================================================================
# SEEDING
# ============================================================================

def seed_starter_cpm_rates(spreadsheet):
    """Insert starter CPM rates if the tab is empty (beyond headers)."""
    ws = spreadsheet.worksheet("cpm_rates")
    rows = ws.get_all_values()

    if len(rows) > 1:
        print(f"  ✓ cpm_rates tab already has {len(rows) - 1} entries — skipping seed")
        return

    now = datetime.now().isoformat()
    rows_to_insert = []
    for rate in STARTER_CPM_RATES:
        rows_to_insert.append([
            rate["vertical"],
            rate["ad_format"],
            rate["geo"],
            rate["cpm_floor"],
            rate["cpm_ceiling"],
            now,
            rate.get("notes", ""),
        ])

    ws.append_rows(rows_to_insert)
    print(f"  ✓ Seeded {len(rows_to_insert)} starter CPM rates")


# ============================================================================
# MAIN MIGRATION
# ============================================================================

def run_migration_v2(verbose: bool = True):
    """Idempotent Stage 2 schema migration. Safe to run multiple times."""
    if verbose:
        print(f"\n{'='*60}")
        print(f"Stage 2 Schema Migration — {datetime.now().isoformat()}")
        print(f"{'='*60}\n")

    gc = get_gspread_client()
    sheet_id = get_sheet_id()
    sh = gc.open_by_key(sheet_id)

    if verbose:
        print(f"Opened spreadsheet: {sh.title}")
        print(f"URL: {sh.url}\n")

    # Step 1: Publishers tab
    print("Step 1/4: Create Publishers tab")
    create_tab_if_missing(sh, "Publishers", PUBLISHERS_SCHEMA)

    # Step 2: cpm_rates tab + seed data
    print("\nStep 2/4: Create cpm_rates tab")
    create_tab_if_missing(sh, "cpm_rates", CPM_RATES_SCHEMA)
    seed_starter_cpm_rates(sh)

    # Step 3: Add target_geo column to Campaigns tab
    print("\nStep 3/4: Add new columns to Campaigns tab")
    try:
        campaigns_ws = sh.worksheet("Campaigns")
        added = add_columns_if_missing(campaigns_ws, CAMPAIGNS_NEW_COLUMNS)
        if not added:
            print(f"  ✓ Campaigns tab already has all Stage 2 columns")
    except gspread.WorksheetNotFound:
        print("  ⚠ Campaigns tab not found — run schema_setup.py first!")
        return

    # Step 4: Add variant tracking columns to Emails tab
    print("\nStep 4/4: Add variant tracking columns to Emails tab")
    try:
        emails_ws = sh.worksheet("Emails")
        added = add_columns_if_missing(emails_ws, EMAILS_NEW_COLUMNS)
        if not added:
            print(f"  ✓ Emails tab already has all Stage 2 columns")
    except gspread.WorksheetNotFound:
        print("  ⚠ Emails tab not found — initial setup required first")
        return

    if verbose:
        print(f"\n{'='*60}")
        print("Stage 2 migration complete!")
        print(f"{'='*60}\n")


if __name__ == "__main__":
    run_migration_v2()
