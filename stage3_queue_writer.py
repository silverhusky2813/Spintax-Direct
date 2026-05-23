"""
stage3_queue_writer.py
========================
Write the approved variant to the Emails tab. Idempotent.

Solves audit errors:
  - 3.4: Double-click protection via idempotency key
  - 3.15: Retry handling — UPDATE existing failed row, INSERT new row otherwise
  - 3.11: Writes by header name (resilient to column reorder)

The Emails tab schema (all columns we write):
  campaign_id, recipient_email, brand, vertical, app_name, campaign_type,
  template_id, template_version, spin_path_json, was_edited,
  subject, body, html_body, from_account,
  idempotency_key, status, queued_at, confirmed_at, generated_at,
  attempt_count, error_message, sent_at, last_attempt_at

(Schema evolves — we write the columns that exist on the tab; gracefully
skip columns we don't have data for; new columns get blanks.)

Public API:
  write_to_queue(approved, html_body, from_account, ...) → WriteResult
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional

import gspread
import streamlit as st

from stage1_dedup import get_gspread_client
from stage1_persistence import get_campaign
from stage1_validation import normalize_email
from stage3_presend_checks import make_idempotency_key
from time_utils import now_iso

import json


# ============================================================================
# RESULT TYPE
# ============================================================================

WriteAction = Literal["inserted", "updated", "duplicate", "error"]


@dataclass
class WriteResult:
    """Outcome of write_to_queue."""
    action: WriteAction
    idempotency_key: str
    row_num: Optional[int] = None
    message: str = ""
    error: Optional[str] = None


# ============================================================================
# COLUMN MAPPING (by header — solves audit error 3.11)
# ============================================================================

def _get_emails_headers() -> list[str]:
    """Get current headers from the Emails tab (no caching — fresh)."""
    gc = get_gspread_client()
    sh = gc.open_by_key(st.secrets["sheet_id"])
    ws = sh.worksheet("Emails")
    return ws.row_values(1)


def _build_row_dict(
    campaign: dict,
    approved: object,  # ApprovedVariant from stage2_ui
    html_body: str,
    from_account: str,
    existing_row: Optional[dict] = None,
) -> dict:
    """
    Assemble the full row dict for write. Pulls from campaign + approved variant.

    For retries (existing_row is not None), preserves the original queued_at
    and increments attempt_count.
    """
    idempotency_key = make_idempotency_key(approved.campaign_id, approved.recipient_email)

    # If this is a retry, preserve some fields; if new, set them fresh
    if existing_row:
        original_queued_at = existing_row.get("queued_at", now_iso())
        attempt_count = int(existing_row.get("attempt_count", 0) or 0) + 1
    else:
        original_queued_at = now_iso()
        attempt_count = 0

    return {
        # Identity
        "campaign_id":      approved.campaign_id,
        "recipient_email":  normalize_email(approved.recipient_email),
        "idempotency_key":  idempotency_key,

        # Campaign context (denormalized for Apps Script convenience)
        "brand":            campaign.get("brand", ""),
        "vertical":         campaign.get("vertical", ""),
        "app_name":         campaign.get("app_name", ""),
        "campaign_type":    campaign.get("campaign_type", ""),

        # Variant tracking
        "template_id":      approved.template_id,
        "template_version": approved.template_version,
        "spin_path_json":   json.dumps(approved.spin_path_json),
        "was_edited":       "TRUE" if approved.was_edited else "FALSE",
        "generated_at":     approved.generated_at,

        # Content
        "subject":          approved.subject,
        "body":             approved.body,
        "html_body":        html_body,

        # Sender
        "from_account":     from_account,

        # Send lifecycle
        "status":           "Queued",
        "queued_at":        original_queued_at,
        "confirmed_at":     now_iso(),
        "attempt_count":    str(attempt_count),

        # Stage 5: priority + retry scheduling
        "priority_score":   str(_compute_priority_score_for_row(campaign, original_queued_at)),
        "next_retry_at":    "",  # empty = eligible immediately

        # Cleared on retry
        "error_message":    "",
        "sent_at":          "",
        "last_attempt_at":  "",
    }


def _compute_priority_score_for_row(campaign: dict, queued_at: str) -> int:
    """
    Compute the Stage 5 priority score for a row at queue time.
    Imported lazily so Stage 3 doesn't hard-depend on Stage 5 being present.
    """
    try:
        from stage5_priority import compute_priority_score
        return compute_priority_score(
            campaign.get("priority_tier", "Medium"),
            queued_at,
        )
    except ImportError:
        # Stage 5 not installed — default neutral score (FIFO by insertion)
        return 0


def _dict_to_row_list(row_dict: dict, headers: list[str]) -> list[str]:
    """
    Convert a dict to a list of values in the order of `headers`.
    Missing keys get blank strings. Extra keys ignored.
    """
    return [str(row_dict.get(h, "")) for h in headers]


# ============================================================================
# MAIN WRITE FUNCTION
# ============================================================================

def write_to_queue(
    approved: object,  # ApprovedVariant
    html_body: str,
    from_account: Optional[str] = None,
    existing_row: Optional[dict] = None,
) -> WriteResult:
    """
    Write an approved variant to the Emails tab.

    Args:
        approved: ApprovedVariant from stage2_ui.render_stage2()
        html_body: HTML version of the email (from stage3_html_renderer)
        from_account: Sender Gmail address. If None, auto-picks via the
                      Stage 5 sender pool (hybrid hash/round-robin). Apps
                      Script re-validates at send time (audit error 5.14).
        existing_row: If retry (from presend_checks), the existing row dict.
                      Should be provided ONLY when caller has explicitly
                      confirmed the retry intent.

    Returns:
        WriteResult with action, key, and optional row_num.

    Behavior:
        - If existing_row provided AND its status is Failed/Bounced:
          UPDATE in place, increment attempt_count
        - Otherwise: INSERT new row
    """
    try:
        gc = get_gspread_client()
        sh = gc.open_by_key(st.secrets["sheet_id"])
        ws = sh.worksheet("Emails")

        # Load campaign for denormalized fields
        campaign = get_campaign(approved.campaign_id)
        if not campaign:
            return WriteResult(
                action="error",
                idempotency_key="",
                error=f"Campaign {approved.campaign_id} not found",
            )

        # Auto-pick sender if not explicitly provided (audit error 5.14:
        # this is the ASSIGN-time choice; Apps Script re-validates at send).
        if from_account is None:
            from_account = _auto_pick_sender(
                approved.recipient_email,
                attempt_count=int(existing_row.get("attempt_count", 0) or 0) if existing_row else 0,
            )

        # Build row data
        row_dict = _build_row_dict(
            campaign=campaign,
            approved=approved,
            html_body=html_body,
            from_account=from_account,
            existing_row=existing_row,
        )

        headers = _get_emails_headers()
        row_values = _dict_to_row_list(row_dict, headers)

        if existing_row:
            # UPDATE existing row (retry path)
            row_num = existing_row.get("__row_num")
            if not row_num:
                return WriteResult(
                    action="error",
                    idempotency_key=row_dict["idempotency_key"],
                    error="Existing row found but row number unknown",
                )

            # Build range A{n}:{last_col}{n}
            last_col = _col_index_to_letter(len(headers))
            range_str = f"A{row_num}:{last_col}{row_num}"
            ws.update(range_str, [row_values])

            return WriteResult(
                action="updated",
                idempotency_key=row_dict["idempotency_key"],
                row_num=row_num,
                message=(
                    f"Updated existing row {row_num} for retry "
                    f"(attempt #{row_dict['attempt_count']})"
                ),
            )

        # INSERT new row
        ws.append_row(row_values)

        # Find the row we just appended (so we can return its number)
        # gspread's append_row doesn't return the row number, so we look it up
        cells = ws.findall(row_dict["idempotency_key"])
        row_num = cells[-1].row if cells else None

        return WriteResult(
            action="inserted",
            idempotency_key=row_dict["idempotency_key"],
            row_num=row_num,
            message=f"Queued for send (row {row_num})",
        )

    except gspread.exceptions.APIError as e:
        return WriteResult(
            action="error",
            idempotency_key="",
            error=f"Sheets API error: {e}",
        )
    except Exception as e:
        return WriteResult(
            action="error",
            idempotency_key="",
            error=f"Unexpected error: {type(e).__name__}: {e}",
        )


def _col_index_to_letter(index: int) -> str:
    """1 → 'A', 26 → 'Z', 27 → 'AA', etc."""
    result = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _auto_pick_sender(recipient_email: str, attempt_count: int = 0) -> str:
    """
    Pick a sender via the Stage 5 pool. Falls back to the default account
    if Stage 5 isn't installed or all accounts are exhausted (the email
    still gets queued; Apps Script will re-validate / defer at send time).
    """
    try:
        from stage5_sender_pool import pick_sender_email, DEFAULT_FROM_ACCOUNT
        picked = pick_sender_email(recipient_email, attempt_count=attempt_count)
        # If all exhausted (None), still assign default — the row stays Queued
        # and Apps Script re-checks caps at send time before actually sending.
        return picked or DEFAULT_FROM_ACCOUNT
    except ImportError:
        return "daniel@premiumads.net"
