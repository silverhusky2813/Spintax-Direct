"""
schema_setup_v3.py
====================
Stage 3 schema migration. Run AFTER schema_setup_v2.py.

Adds new columns to the Emails tab:
  - html_body         : HTML version of the email body
  - from_account      : Sender email (default: daniel@premiumads.net)
  - idempotency_key   : SHA-256 hash for duplicate detection
  - confirmed_at      : When the user confirmed the send (separate from queued_at)
  - last_attempt_at   : When Apps Script last tried to send (for retry tracking)
  - attempt_count     : How many send attempts have been made
  - error_message     : Last error from Apps Script if status=Failed

These columns enable:
  - HTML email rendering (Stage 3)
  - Sender account diversification (future Stage 6)
  - Idempotent retries (Stage 3 + Apps Script)
  - Retry/error visibility (Stage 4 view + future Stage 5)

Idempotent — safe to run multiple times.
"""

from datetime import datetime

import gspread

from schema_setup import get_gspread_client, get_sheet_id
from schema_setup_v2 import add_columns_if_missing


# ============================================================================
# NEW COLUMN DEFINITIONS
# ============================================================================

EMAILS_V3_NEW_COLUMNS = [
    "html_body",          # HTML-rendered email body
    "from_account",       # Sender Gmail address
    "idempotency_key",    # SHA-256 of (campaign_id, recipient_email)
    "confirmed_at",       # When user pressed Confirm in Stage 3
    "last_attempt_at",    # When Apps Script last tried to send
    "attempt_count",      # 0 = never tried, N = N retries
    "error_message",      # Populated when status=Failed
]


# ============================================================================
# MIGRATION
# ============================================================================

def run_migration_v3(verbose: bool = True):
    """Add Stage 3 columns to Emails tab. Idempotent."""
    if verbose:
        print(f"\n{'='*60}")
        print(f"Stage 3 Schema Migration — {datetime.now().isoformat()}")
        print(f"{'='*60}\n")

    gc = get_gspread_client()
    sheet_id = get_sheet_id()
    sh = gc.open_by_key(sheet_id)

    if verbose:
        print(f"Opened spreadsheet: {sh.title}")
        print(f"URL: {sh.url}\n")

    print("Adding Stage 3 columns to Emails tab")
    try:
        emails_ws = sh.worksheet("Emails")
        added = add_columns_if_missing(emails_ws, EMAILS_V3_NEW_COLUMNS)
        if not added:
            print("  ✓ Emails tab already has all Stage 3 columns")
        else:
            print(f"  ✓ Added {len(added)} new columns: {', '.join(added)}")
    except gspread.WorksheetNotFound:
        print("  ⚠ Emails tab not found — run earlier schema setups first")
        return

    if verbose:
        print(f"\n{'='*60}")
        print("Stage 3 migration complete!")
        print(f"{'='*60}\n")


if __name__ == "__main__":
    run_migration_v3()
