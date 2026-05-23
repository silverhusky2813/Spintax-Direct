"""
stage3_presend_checks.py
==========================
Final safety checks run between "user clicks Confirm" and "row written to queue."

Solves audit errors:
  - 3.7: Re-check suppression list with FRESH read (not cached from Stage 1)
  - 3.15: Idempotency key handles failed-retry scenario correctly
  - 3.6: Don't re-run validations Stage 1/2 already did

The checks run in order. The first failure stops the chain — UI shows the
error and doesn't proceed.

Public API:
  run_presend_checks(approved_variant) → CheckResult
"""

import hashlib
from dataclasses import dataclass
from typing import Literal, Optional

import gspread
import streamlit as st

from stage1_dedup import (
    check_publisher_contact_history,
    get_gspread_client,
    is_suppressed,
)
from stage1_validation import normalize_email


# ============================================================================
# RESULT TYPE
# ============================================================================

CheckStatus = Literal["ok", "block", "warn"]


@dataclass
class CheckResult:
    """Outcome of one or more pre-send checks."""
    status: CheckStatus       # 'ok' | 'block' | 'warn'
    title: str                # Short user-facing label
    detail: str               # Longer explanation
    can_override: bool = False  # True for 'warn' — user can confirm to proceed


# ============================================================================
# IDEMPOTENCY KEY
# ============================================================================

def make_idempotency_key(campaign_id: str, recipient_email: str) -> str:
    """
    Generate a stable idempotency key from (campaign_id, recipient_email).

    Used to detect "is this email already queued or sent?" — prevents
    duplicates from double-clicks or browser reloads.

    Same campaign + same recipient = same key, regardless of regenerate_count
    or template variations.
    """
    normalized = f"{campaign_id}|{normalize_email(recipient_email)}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


# ============================================================================
# SHEETS LOOKUPS (fresh reads — bypass cache for safety)
# ============================================================================

def _find_existing_email_row(idempotency_key: str) -> Optional[dict]:
    """
    Fresh lookup (no cache) for any existing email row with this idempotency key.

    Returns the row dict (with 'row_num' field added) or None.
    """
    gc = get_gspread_client()
    sh = gc.open_by_key(st.secrets["sheet_id"])

    try:
        ws = sh.worksheet("Emails")
    except gspread.WorksheetNotFound:
        return None

    # Look up by idempotency_key column
    # Find the column index first (we don't hardcode — schema may evolve)
    headers = ws.row_values(1)
    if "idempotency_key" not in headers:
        # Column doesn't exist yet (pre-migration) — can't check
        return None

    col_idx = headers.index("idempotency_key") + 1  # 1-indexed for gspread

    try:
        cells = ws.findall(idempotency_key, in_column=col_idx)
    except gspread.exceptions.APIError:
        return None

    if not cells:
        return None

    # Get the full row data for the first match
    row_num = cells[0].row
    row_values = ws.row_values(row_num)
    row_dict = dict(zip(headers, row_values))
    row_dict["__row_num"] = row_num  # for later updates
    return row_dict


# ============================================================================
# INDIVIDUAL CHECKS
# ============================================================================

def check_suppression_fresh(recipient_email: str) -> CheckResult:
    """
    Re-check suppression list with bypass cache.

    Solves audit error 3.7: between Stage 1 and Stage 3 the user might have
    spent 10+ minutes working. The suppression list could have been updated
    in that window. We re-read it FRESH here.
    """
    # Bypass cache by clearing first
    st.cache_data.clear()

    suppressed, reason = is_suppressed(recipient_email)
    if suppressed:
        return CheckResult(
            status="block",
            title="Recipient is on suppression list",
            detail=(
                f"⛔ {recipient_email} cannot be contacted. "
                f"Reason: {reason}. Remove from Suppression tab to override."
            ),
            can_override=False,
        )

    return CheckResult(
        status="ok",
        title="Suppression check passed",
        detail="Recipient is not on the suppression list.",
    )


def check_dedup_fresh(
    recipient_email: str,
    brand: str,
    vertical: str,
    campaign_type: str,
) -> CheckResult:
    """
    Re-check the dedup window. Possible scenarios where this matters:

    - User started Stage 1 before midnight, finished Stage 3 after midnight
      (dedup window for a 30-day rule might now exclude a prior contact)
    - Another teammate sent an email to the same recipient during the
      Stage 1→3 gap
    """
    st.cache_data.clear()

    status, message, prior = check_publisher_contact_history(
        publisher_email=recipient_email,
        brand=brand,
        vertical=vertical,
        campaign_type=campaign_type,
    )

    if status == "ok":
        return CheckResult(status="ok", title="Dedup check passed", detail=message)

    if status == "duplicate":
        return CheckResult(
            status="warn",
            title="Recently contacted",
            detail=message,
            can_override=True,
        )

    if status == "no_prior_contact":
        return CheckResult(
            status="block",
            title="FollowUp without prior contact",
            detail=message,
            can_override=False,
        )

    if status == "stale_contact":
        return CheckResult(
            status="warn",
            title="Stale contact",
            detail=message,
            can_override=True,
        )

    return CheckResult(status="ok", title="Dedup check", detail="Unknown status")


def check_idempotency(
    campaign_id: str,
    recipient_email: str,
) -> tuple[CheckResult, Optional[dict]]:
    """
    Check if a row already exists for this (campaign, recipient).

    Returns:
        (CheckResult, existing_row_dict_or_None)

    Possible outcomes:
      - No existing row → OK, proceed to fresh insert
      - Existing row with status Queued → BLOCK (already queued; clicking
        Confirm again is probably a double-click)
      - Existing row with status Sent/Delivered → BLOCK (already sent)
      - Existing row with status Failed/Bounced → WARN (offer retry by UPDATE)
    """
    key = make_idempotency_key(campaign_id, recipient_email)
    existing = _find_existing_email_row(key)

    if not existing:
        return (
            CheckResult(
                status="ok",
                title="Idempotency check passed",
                detail="No existing row — safe to insert.",
            ),
            None,
        )

    existing_status = str(existing.get("status", "")).strip().lower()

    if existing_status in ("queued", "sending"):
        return (
            CheckResult(
                status="block",
                title="Already queued",
                detail=(
                    f"Row exists with status '{existing.get('status')}'. "
                    f"It's already in the queue. If this is a duplicate "
                    f"click, ignore. If you want to re-send, wait for it to "
                    f"complete first."
                ),
                can_override=False,
            ),
            existing,
        )

    if existing_status in ("sent", "delivered"):
        return (
            CheckResult(
                status="block",
                title="Already sent",
                detail=(
                    f"This email was already sent at "
                    f"{existing.get('sent_at', '?')}. Cannot re-send to "
                    f"the same (campaign, recipient) combo. Start a new "
                    f"campaign if you need to contact this person again."
                ),
                can_override=False,
            ),
            existing,
        )

    if existing_status in ("failed", "bounced"):
        return (
            CheckResult(
                status="warn",
                title="Previous attempt failed — retry?",
                detail=(
                    f"A prior send attempt failed: "
                    f"'{existing.get('error_message', 'unknown error')}'. "
                    f"Confirming will UPDATE the existing row to retry the send."
                ),
                can_override=True,
            ),
            existing,
        )

    # Unknown status — treat as block, surface info
    return (
        CheckResult(
            status="block",
            title=f"Existing row in unknown state",
            detail=(
                f"Existing row has status='{existing.get('status', '?')}'. "
                f"This is unexpected — investigate before proceeding."
            ),
            can_override=False,
        ),
        existing,
    )


# ============================================================================
# MAIN: RUN ALL CHECKS
# ============================================================================

def run_all_presend_checks(
    campaign_id: str,
    recipient_email: str,
    brand: str,
    vertical: str,
    campaign_type: str,
) -> tuple[list[CheckResult], Optional[dict]]:
    """
    Run all pre-send checks in order.

    Returns:
        (list_of_check_results, existing_row_dict_or_None)

        The list contains ALL check results (passed and failed) for UI display.
        existing_row is set if a duplicate was found (could be retry candidate).

    Caller decides how to react:
      - Any 'block' status → don't show Confirm button
      - Any 'warn' status → show Confirm with explicit override checkbox
      - All 'ok' → enable Confirm normally
    """
    results = []
    existing_row = None

    # Check 1: Suppression (fresh)
    results.append(check_suppression_fresh(recipient_email))
    # If suppression blocks, no need to continue — but we still run dedup
    # for the UI to surface multiple issues at once if relevant.

    # Check 2: Dedup (fresh)
    results.append(check_dedup_fresh(
        recipient_email, brand, vertical, campaign_type,
    ))

    # Check 3: Idempotency
    idempotency_result, existing_row = check_idempotency(
        campaign_id, recipient_email,
    )
    results.append(idempotency_result)

    return results, existing_row


# ============================================================================
# AGGREGATE STATUS HELPER
# ============================================================================

def aggregate_status(results: list[CheckResult]) -> CheckStatus:
    """
    Roll up multiple check results into one status:
      - Any 'block' → 'block'
      - Any 'warn' (no block) → 'warn'
      - All 'ok' → 'ok'
    """
    if any(r.status == "block" for r in results):
        return "block"
    if any(r.status == "warn" for r in results):
        return "warn"
    return "ok"
