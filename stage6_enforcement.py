"""
stage6_enforcement.py
======================
Apply health decisions to the sender_accounts tab (the WRITE layer).

Solves audit errors:
  - 6.5: Pauses carry a reason and timestamp; reactivation is explicit + visible
  - 6.6: Enforcement is a deliberate action (button / scheduled), not per-render

This module reads health decisions from stage6_health_score and writes the
results: pausing accounts (is_active=FALSE + reason), logging health snapshots,
and handling manual reactivation (sets reactivated_at for the grace window).

The pure decision logic lives in stage6_health_score; this module does I/O.
"""

from datetime import datetime, timezone
from typing import Optional

import gspread
import streamlit as st

from stage1_dedup import get_gspread_client
from stage1_validation import normalize_email
from stage6_health_score import AccountHealth, assess_all_accounts
from time_utils import now_iso


# ============================================================================
# DATA LOADING
# ============================================================================

def _load_accounts_raw() -> list[dict]:
    """Fresh read of sender_accounts (no cache — enforcement needs current state)."""
    gc = get_gspread_client()
    sh = gc.open_by_key(st.secrets["sheet_id"])
    try:
        ws = sh.worksheet("sender_accounts")
    except gspread.WorksheetNotFound:
        return []
    return ws.get_all_records()


def _load_emails() -> list[dict]:
    """Fresh read of Emails for health computation."""
    gc = get_gspread_client()
    sh = gc.open_by_key(st.secrets["sheet_id"])
    try:
        ws = sh.worksheet("Emails")
    except gspread.WorksheetNotFound:
        return []
    return ws.get_all_records()


# ============================================================================
# WRITE: PAUSE / REACTIVATE
# ============================================================================

def _find_account_row(ws, from_account: str) -> Optional[int]:
    """Return the 1-indexed row number for an account, or None."""
    cells = ws.findall(from_account, in_column=1)
    return cells[0].row if cells else None


def pause_account(from_account: str, reason: str) -> bool:
    """
    Set is_active=FALSE with a reason + timestamp (audit error 6.5).
    Returns True on success.
    """
    gc = get_gspread_client()
    sh = gc.open_by_key(st.secrets["sheet_id"])
    ws = sh.worksheet("sender_accounts")

    headers = ws.row_values(1)
    row_num = _find_account_row(ws, from_account)
    if not row_num:
        return False

    updates = {
        "is_active": "FALSE",
        "paused_reason": reason,
        "paused_at": now_iso(),
    }
    for col, val in updates.items():
        if col in headers:
            ws.update_cell(row_num, headers.index(col) + 1, val)

    st.cache_data.clear()
    return True


def reactivate_account(from_account: str) -> bool:
    """
    Manually reactivate a paused account.

    Sets is_active=TRUE, clears the pause reason, and stamps reactivated_at
    to start the grace window (audit error 6.7) so health checks don't
    instantly re-pause it.
    """
    gc = get_gspread_client()
    sh = gc.open_by_key(st.secrets["sheet_id"])
    ws = sh.worksheet("sender_accounts")

    headers = ws.row_values(1)
    row_num = _find_account_row(ws, from_account)
    if not row_num:
        return False

    updates = {
        "is_active": "TRUE",
        "paused_reason": "",
        "paused_at": "",
        "reactivated_at": now_iso(),
    }
    for col, val in updates.items():
        if col in headers:
            ws.update_cell(row_num, headers.index(col) + 1, val)

    st.cache_data.clear()
    return True


# ============================================================================
# HEALTH LOG
# ============================================================================

def _log_health_snapshots(snapshots: list[tuple]) -> None:
    """Append health-check rows to account_health_log."""
    if not snapshots:
        return
    gc = get_gspread_client()
    sh = gc.open_by_key(st.secrets["sheet_id"])
    try:
        ws = sh.worksheet("account_health_log")
    except gspread.WorksheetNotFound:
        return
    ws.append_rows(snapshots)


# ============================================================================
# MAIN: RUN HEALTH CHECK + ENFORCE
# ============================================================================

def run_health_check(enforce: bool = False) -> list[dict]:
    """
    Run a full health check across all accounts.

    Args:
        enforce: if True, actually pause accounts recommended for auto_pause
                 and log snapshots. If False, just assess + return (dry run).

    Returns:
        A list of result dicts (one per account) summarizing assessment and
        any action taken. Suitable for displaying in the dashboard.

    Audit error 6.6: enforcement only happens when explicitly requested
    (a button click or a scheduled Apps Script call), never on passive render.
    """
    accounts = _load_accounts_raw()
    emails = _load_emails()
    now = datetime.now(timezone.utc)

    assessments: list[AccountHealth] = assess_all_accounts(accounts, emails, now)

    results = []
    log_snapshots = []

    for health in assessments:
        action_taken = "none"

        if enforce:
            if health.recommended_action == "auto_pause":
                if pause_account(health.from_account, health.reason):
                    action_taken = "auto_paused"
            elif health.recommended_action in ("alert", "blocked_last_account"):
                action_taken = "alerted"

            # Record a snapshot for trend history
            log_snapshots.append([
                now_iso(),
                health.from_account,
                health.sends_window,
                health.bounces_window,
                health.bounce_rate,
                health.status,
                action_taken,
            ])

        results.append({
            "from_account": health.from_account,
            "status": health.status,
            "bounce_rate": health.bounce_rate,
            "sends_window": health.sends_window,
            "bounces_window": health.bounces_window,
            "recommended_action": health.recommended_action,
            "action_taken": action_taken,
            "reason": health.reason,
        })

    if enforce and log_snapshots:
        _log_health_snapshots(log_snapshots)

    return results
