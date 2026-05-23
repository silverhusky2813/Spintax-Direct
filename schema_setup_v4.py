"""
schema_setup_v4.py
====================
Stage 5 schema migration. Run AFTER schema_setup_v3.py.

Adds:
  1. 'sender_accounts' tab — sending account config (caps, windows, active flag)
  2. 'send_log' tab — rolling record of sends per account (for rate limiting)
  3. New columns on Emails tab:
     - priority_score   : single sortable integer (tier + age) — audit error 5.1, 5.16
     - next_retry_at    : ISO timestamp; Apps Script skips rows until this passes

These enable:
  - Priority-aware queue ordering (5A)
  - Multi-account sender rotation (5B)
  - Per-account rate limiting (5C)

Idempotent — safe to run multiple times.
"""

from datetime import datetime

import gspread

from schema_setup import get_gspread_client, get_sheet_id, create_tab_if_missing
from schema_setup_v2 import add_columns_if_missing


# ============================================================================
# SCHEMA DEFINITIONS
# ============================================================================

SENDER_ACCOUNTS_SCHEMA = [
    "from_account",          # A  PRIMARY KEY — Gmail address
    "display_name",          # B  Sender display name (e.g., "Daniel @ PremiumAds")
    "daily_cap",             # C  Max sends per rolling 24h
    "hourly_cap",            # D  Max sends per rolling 60min
    "send_window_start_utc", # E  Hour 0-23; sends allowed from this hour
    "send_window_end_utc",   # F  Hour 0-23; sends allowed until this hour
    "is_active",             # G  TRUE/FALSE — inactive accounts skipped
    "priority_order",        # H  Lower = preferred in round-robin (0,1,2...)
    "notes",                 # I  free text
]

SEND_LOG_SCHEMA = [
    "sent_at",               # A  ISO timestamp of the send
    "from_account",          # B  which account sent it
    "recipient_email",       # C  who received it
    "campaign_id",           # D  for cross-reference
    "idempotency_key",       # E  links back to the Emails row
]

EMAILS_V4_NEW_COLUMNS = [
    "priority_score",        # Sortable integer for queue ordering
    "next_retry_at",         # ISO timestamp; skip row until now >= this
]

# ============================================================================
# STARTER DATA — the one account the user has now
# ============================================================================

STARTER_SENDER_ACCOUNTS = [
    {
        "from_account": "daniel@premiumads.net",
        "display_name": "Daniel @ PremiumAds",
        "daily_cap": 200,
        "hourly_cap": 30,
        "send_window_start_utc": 0,    # 0 = no window restriction (24h)
        "send_window_end_utc": 24,     # 24 = no window restriction
        "is_active": "TRUE",
        "priority_order": 0,
        "notes": "Primary sending account",
    },
    # To add more accounts later, add rows here (or directly in the Sheet):
    # {
    #     "from_account": "alex@premiumads.net",
    #     "display_name": "Alex @ PremiumAds",
    #     "daily_cap": 200, "hourly_cap": 30,
    #     "send_window_start_utc": 0, "send_window_end_utc": 24,
    #     "is_active": "TRUE", "priority_order": 1, "notes": "",
    # },
]


# ============================================================================
# SEEDING
# ============================================================================

def seed_starter_accounts(spreadsheet):
    """Insert the starter account if the tab is empty."""
    ws = spreadsheet.worksheet("sender_accounts")
    rows = ws.get_all_values()

    if len(rows) > 1:
        print(f"  ✓ sender_accounts already has {len(rows) - 1} entries — skipping seed")
        return

    rows_to_insert = []
    for acct in STARTER_SENDER_ACCOUNTS:
        rows_to_insert.append([acct.get(col, "") for col in SENDER_ACCOUNTS_SCHEMA])

    ws.append_rows(rows_to_insert)
    print(f"  ✓ Seeded {len(rows_to_insert)} starter sender account(s)")


# ============================================================================
# MAIN MIGRATION
# ============================================================================

def run_migration_v4(verbose: bool = True):
    """Idempotent Stage 5 schema migration."""
    if verbose:
        print(f"\n{'='*60}")
        print(f"Stage 5 Schema Migration — {datetime.now().isoformat()}")
        print(f"{'='*60}\n")

    gc = get_gspread_client()
    sheet_id = get_sheet_id()
    sh = gc.open_by_key(sheet_id)

    if verbose:
        print(f"Opened spreadsheet: {sh.title}")
        print(f"URL: {sh.url}\n")

    print("Step 1/3: Create sender_accounts tab")
    create_tab_if_missing(sh, "sender_accounts", SENDER_ACCOUNTS_SCHEMA)
    seed_starter_accounts(sh)

    print("\nStep 2/3: Create send_log tab")
    create_tab_if_missing(sh, "send_log", SEND_LOG_SCHEMA)

    print("\nStep 3/3: Add Stage 5 columns to Emails tab")
    try:
        emails_ws = sh.worksheet("Emails")
        added = add_columns_if_missing(emails_ws, EMAILS_V4_NEW_COLUMNS)
        if not added:
            print("  ✓ Emails tab already has Stage 5 columns")
    except gspread.WorksheetNotFound:
        print("  ⚠ Emails tab not found — run earlier schema setups first")
        return

    if verbose:
        print(f"\n{'='*60}")
        print("Stage 5 migration complete!")
        print(f"{'='*60}\n")


if __name__ == "__main__":
    run_migration_v4()
