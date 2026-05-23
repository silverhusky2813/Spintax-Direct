"""
schema_setup_v5.py
====================
Stage 7 schema migration. Run AFTER schema_setup_v4.py.

Adds:
  1. New columns on Emails tab:
     - thread_id        : Gmail thread ID, captured at send time (audit 7.9)
     - reply_status     : none | genuine | auto_reply | bounce | unsubscribe
     - replied_at       : ISO timestamp of the matched reply
     - reply_snippet    : first ~200 chars of the reply (for context)
  2. 'reply_log' tab — every processed inbound reply (idempotency, audit 7.17)
  3. Script-level watermark stored in a 'tracking_meta' tab (audit 7.10)

Idempotent — safe to run multiple times.
"""

from datetime import datetime

import gspread

from schema_setup import get_gspread_client, get_sheet_id, create_tab_if_missing
from schema_setup_v2 import add_columns_if_missing


# ============================================================================
# SCHEMA DEFINITIONS
# ============================================================================

EMAILS_V5_NEW_COLUMNS = [
    "thread_id",        # Gmail thread ID (captured at send via draft-then-send)
    "reply_status",     # none | genuine | auto_reply | bounce | unsubscribe
    "replied_at",       # ISO timestamp of matched reply
    "reply_snippet",    # first ~200 chars of reply body for quick context
]

REPLY_LOG_SCHEMA = [
    "reply_message_id",  # A  PRIMARY KEY — Gmail message ID of the reply (dedup)
    "thread_id",         # B  thread it belongs to
    "matched_idempotency_key",  # C  which Emails row it matched
    "reply_status",      # D  classification result
    "from_email",        # E  who sent the reply
    "subject",           # F  reply subject
    "received_at",       # G  ISO timestamp
    "processed_at",      # H  when our scan logged it
]

TRACKING_META_SCHEMA = [
    "key",               # A  e.g. 'last_reply_scan_at'
    "value",             # B  the stored value (ISO timestamp, etc.)
    "updated_at",        # C
]


# ============================================================================
# MIGRATION
# ============================================================================

def run_migration_v5(verbose: bool = True):
    """Idempotent Stage 7 schema migration."""
    if verbose:
        print(f"\n{'='*60}")
        print(f"Stage 7 Schema Migration — {datetime.now().isoformat()}")
        print(f"{'='*60}\n")

    gc = get_gspread_client()
    sheet_id = get_sheet_id()
    sh = gc.open_by_key(sheet_id)

    if verbose:
        print(f"Opened spreadsheet: {sh.title}")
        print(f"URL: {sh.url}\n")

    print("Step 1/3: Add reply-tracking columns to Emails tab")
    try:
        emails_ws = sh.worksheet("Emails")
        added = add_columns_if_missing(emails_ws, EMAILS_V5_NEW_COLUMNS)
        if not added:
            print("  ✓ Emails tab already has Stage 7 columns")
    except gspread.WorksheetNotFound:
        print("  ⚠ Emails tab not found — run earlier schema setups first")
        return

    print("\nStep 2/3: Create reply_log tab")
    create_tab_if_missing(sh, "reply_log", REPLY_LOG_SCHEMA)

    print("\nStep 3/3: Create tracking_meta tab")
    create_tab_if_missing(sh, "tracking_meta", TRACKING_META_SCHEMA)

    if verbose:
        print(f"\n{'='*60}")
        print("Stage 7 migration complete!")
        print(f"{'='*60}\n")


if __name__ == "__main__":
    run_migration_v5()
