"""
stage4_queue_view.py
======================
Read-only view of the email queue. Lets the user see:
  - Recently queued emails awaiting send
  - Recently sent emails
  - Failed/bounced emails (with retry option)

Solves audit error 3.16: filters and sorting so the view doesn't get useless
as the queue grows.

Pure read functionality — does NOT trigger sends or modify rows. Retries from
this view send the user back to Stage 1/2 to regenerate the content first.
"""

from typing import Optional

import gspread
import streamlit as st

from stage1_dedup import get_gspread_client
from time_utils import format_age, format_for_display, safe_parse_date


# ============================================================================
# DATA LOAD
# ============================================================================

@st.cache_data(ttl=30)
def load_queue_rows(
    status_filter: Optional[str] = None,
    campaign_id_filter: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """
    Load rows from Emails tab with optional filters.

    Args:
        status_filter: e.g., 'Queued', 'Sent', 'Failed'. None = all.
        campaign_id_filter: only rows for this campaign. None = all.
        limit: max rows to return (most recent first).
    """
    gc = get_gspread_client()
    sh = gc.open_by_key(st.secrets["sheet_id"])

    try:
        ws = sh.worksheet("Emails")
    except gspread.WorksheetNotFound:
        return []

    records = ws.get_all_records()

    # Apply filters
    filtered = records
    if status_filter and status_filter != "All":
        filtered = [
            r for r in filtered
            if str(r.get("status", "")).lower() == status_filter.lower()
        ]
    if campaign_id_filter:
        filtered = [
            r for r in filtered
            if r.get("campaign_id") == campaign_id_filter
        ]

    # Sort by queued_at descending (most recent first)
    def sort_key(row):
        dt = safe_parse_date(row.get("queued_at", "")) or safe_parse_date(
            row.get("confirmed_at", "")
        )
        # None dates sort to the end
        return dt.timestamp() if dt else 0

    filtered.sort(key=sort_key, reverse=True)

    return filtered[:limit]


# ============================================================================
# SUMMARY STATS
# ============================================================================

def compute_queue_summary(rows: list[dict]) -> dict:
    """Aggregate counts by status."""
    summary = {
        "total": len(rows),
        "queued": 0,
        "sending": 0,
        "sent": 0,
        "failed": 0,
        "bounced": 0,
        "other": 0,
    }
    for r in rows:
        status = str(r.get("status", "")).lower()
        if status in summary:
            summary[status] += 1
        else:
            summary["other"] += 1
    return summary


# ============================================================================
# UI: ROW DISPLAY
# ============================================================================

def _status_emoji(status: str) -> str:
    """Visual indicator for status."""
    return {
        "queued":    "⏳",
        "sending":   "📤",
        "sent":      "✅",
        "delivered": "✅",
        "failed":    "❌",
        "bounced":   "⚠️",
    }.get(status.lower(), "❓")


def _render_row(row: dict, idx: int):
    """Render one queue row as a compact expandable card."""
    status = str(row.get("status", "?"))
    emoji = _status_emoji(status)
    recipient = row.get("recipient_email", "?")
    subject = row.get("subject", "(no subject)")
    brand = row.get("brand", "?")
    app = row.get("app_name", "?")
    queued_at = row.get("queued_at", "")
    queued_age = format_age(queued_at)

    header_label = (
        f"{emoji} **{status}**  •  {brand} × {app}  •  → {recipient}  •  {queued_age}"
    )

    with st.expander(header_label, expanded=False):
        col1, col2 = st.columns([3, 2])

        with col1:
            st.caption("Subject")
            st.write(subject)

            st.caption("Body preview")
            body = str(row.get("body", ""))
            preview = body[:300] + ("..." if len(body) > 300 else "")
            st.text(preview)

        with col2:
            st.caption("Send info")
            st.write(f"**From:** {row.get('from_account', '?')}")
            st.write(f"**Queued:** {format_for_display(queued_at)}")
            st.write(f"**Confirmed:** {format_for_display(row.get('confirmed_at', ''))}")
            if row.get("sent_at"):
                st.write(f"**Sent:** {format_for_display(row.get('sent_at', ''))}")
            if row.get("last_attempt_at"):
                st.write(f"**Last attempt:** {format_for_display(row.get('last_attempt_at', ''))}")

            st.caption("Tracking")
            st.write(f"**Template:** `{row.get('template_id', '?')}` v{row.get('template_version', '?')}")
            st.write(f"**Edited:** {'Yes' if row.get('was_edited') == 'TRUE' else 'No'}")
            st.write(f"**Attempts:** {row.get('attempt_count', '0')}")

            if row.get("error_message"):
                st.error(f"❌ {row.get('error_message')}")

        st.caption(f"Campaign ID: `{row.get('campaign_id', '?')}` • "
                   f"Idempotency: `{row.get('idempotency_key', '?')[:8]}...`")


# ============================================================================
# MAIN ENTRY
# ============================================================================

def render_queue_view() -> Optional[str]:
    """
    Render the queue view UI.

    Returns:
        Next-action string ('new_campaign') if user clicks a navigation button,
        otherwise None.
    """
    st.title("📋 Email Queue")

    # ---- Filter bar ----
    col1, col2, col3 = st.columns([2, 2, 1])

    with col1:
        status_filter = st.selectbox(
            "Filter by status",
            ["All", "Queued", "Sending", "Sent", "Failed", "Bounced"],
        )

    with col2:
        # Campaign filter (optional)
        campaign_filter = st.text_input(
            "Filter by campaign ID (optional)",
            value="",
            placeholder="Paste a campaign_id to filter",
        )
        campaign_filter = campaign_filter.strip() or None

    with col3:
        st.caption("Limit")
        limit = st.number_input("Limit", 10, 500, 100, label_visibility="collapsed")

    # ---- Refresh button ----
    if st.button("🔄 Refresh"):
        st.cache_data.clear()
        st.rerun()

    # ---- Load data ----
    rows = load_queue_rows(
        status_filter=status_filter,
        campaign_id_filter=campaign_filter,
        limit=int(limit),
    )

    # ---- Summary stats ----
    summary = compute_queue_summary(rows)

    cols = st.columns(6)
    cols[0].metric("Total", summary["total"])
    cols[1].metric("⏳ Queued", summary["queued"])
    cols[2].metric("📤 Sending", summary["sending"])
    cols[3].metric("✅ Sent", summary["sent"])
    cols[4].metric("❌ Failed", summary["failed"])
    cols[5].metric("⚠️ Bounced", summary["bounced"])

    st.divider()

    # ---- Empty state ----
    if not rows:
        st.info(
            "No rows match the current filters. "
            "Try changing the status filter, or queue some emails from Stage 1."
        )
        if st.button("🆕 Start new campaign"):
            return "new_campaign"
        return None

    # ---- Row list ----
    st.caption(f"Showing {len(rows)} rows (most recent first)")

    for idx, row in enumerate(rows):
        _render_row(row, idx)

    st.divider()

    # ---- Navigation ----
    col_new, col_spacer = st.columns([1, 4])
    with col_new:
        if st.button("🆕 New campaign"):
            return "new_campaign"

    return None
