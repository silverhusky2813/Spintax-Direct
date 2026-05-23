"""
schema_setup_v6.py
====================
Stage 6 schema migration. Run AFTER schema_setup_v5.py.

Adds columns to the sender_accounts tab:
  - activated_at      : ISO date the account started sending (for warm-up day count)
  - warmup_enabled    : TRUE/FALSE — whether to apply the warm-up ramp
  - paused_reason     : why the account was auto-paused (empty if active)
  - paused_at         : ISO timestamp of the pause
  - reactivated_at    : ISO timestamp of last manual reactivation (grace window)

Creates:
  - 'account_health_log' tab — periodic health snapshots for trend visibility

Idempotent — safe to run multiple times.

Note: is_active already exists from Stage 5. Stage 6 *manages* it (auto-pause
sets it FALSE with a reason; reactivation sets it TRUE and stamps reactivated_at).
"""

from datetime import datetime

import gspread

from schema_setup import get_gspread_client, get_sheet_id, create_tab_if_missing
from schema_setup_v2 import add_columns_if_missing


# ============================================================================
# SCHEMA DEFINITIONS
# ============================================================================

SENDER_ACCOUNTS_V6_NEW_COLUMNS = [
    "activated_at",     # ISO date account started (warm-up anchor — audit 6.4)
    "warmup_enabled",   # TRUE/FALSE — apply warm-up ramp?
    "paused_reason",    # why auto-paused (audit 6.5)
    "paused_at",        # when auto-paused
    "reactivated_at",   # last manual reactivation (grace window — audit 6.7)
]

ACCOUNT_HEALTH_LOG_SCHEMA = [
    "checked_at",       # A  ISO timestamp of the health check
    "from_account",     # B
    "sends_7d",         # C  sends in trailing 7 days
    "bounces_7d",       # D  bounces in trailing 7 days
    "bounce_rate_7d",   # E  percentage
    "health_status",    # F  healthy | warning | critical | paused
    "action_taken",     # G  none | alerted | auto_paused
]


# ============================================================================
# MIGRATION
# ============================================================================

def run_migration_v6(verbose: bool = True):
    """Idempotent Stage 6 schema migration."""
    if verbose:
        print(f"\n{'='*60}")
        print(f"Stage 6 Schema Migration — {datetime.now().isoformat()}")
        print(f"{'='*60}\n")

    gc = get_gspread_client()
    sheet_id = get_sheet_id()
    sh = gc.open_by_key(sheet_id)

    if verbose:
        print(f"Opened spreadsheet: {sh.title}")
        print(f"URL: {sh.url}\n")

    print("Step 1/2: Add Stage 6 columns to sender_accounts tab")
    try:
        accounts_ws = sh.worksheet("sender_accounts")
        added = add_columns_if_missing(accounts_ws, SENDER_ACCOUNTS_V6_NEW_COLUMNS)
        if not added:
            print("  ✓ sender_accounts already has Stage 6 columns")
        else:
            # Backfill activated_at for existing accounts (use today as anchor)
            _backfill_activated_at(accounts_ws)
    except gspread.WorksheetNotFound:
        print("  ⚠ sender_accounts not found — run schema_setup_v4.py first")
        return

    print("\nStep 2/2: Create account_health_log tab")
    create_tab_if_missing(sh, "account_health_log", ACCOUNT_HEALTH_LOG_SCHEMA)

    if verbose:
        print(f"\n{'='*60}")
        print("Stage 6 migration complete!")
        print(f"{'='*60}\n")


def _backfill_activated_at(worksheet):
    """
    For existing accounts with no activated_at, set it to today.
    Existing accounts are presumed already warm, so warmup_enabled defaults FALSE.
    """
    records = worksheet.get_all_records()
    headers = worksheet.row_values(1)

    try:
        activated_col = headers.index("activated_at") + 1
        warmup_col = headers.index("warmup_enabled") + 1
    except ValueError:
        return

    today = datetime.now().date().isoformat()
    backfilled = 0
    for i, rec in enumerate(records):
        row_num = i + 2  # +1 header, +1 for 1-index
        if not str(rec.get("activated_at", "")).strip():
            worksheet.update_cell(row_num, activated_col, today)
            # Existing accounts presumed warm → warmup off by default
            worksheet.update_cell(row_num, warmup_col, "FALSE")
            backfilled += 1

    if backfilled:
        print(f"  ✓ Backfilled activated_at for {backfilled} existing account(s) "
              f"(warmup_enabled=FALSE — presumed already warm)")


if __name__ == "__main__":
    run_migration_v6()
