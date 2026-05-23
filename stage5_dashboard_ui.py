"""
stage5_dashboard_ui.py
=======================
Queue health dashboard — operational visibility into the send pipeline.

Per user's choice (Q4): a separate dashboard view.

Shows:
  - Queue depth + status breakdown
  - Oldest queued age + estimated drain time
  - Per-account usage bars (daily/hourly % of cap)
  - 24h send/failure rates
  - Next-to-send preview (top 5 by priority)
  - Recent failures with error messages

Read-only. No sends triggered here.

Public API:
  render_dashboard() → Optional[str]  (returns 'new_campaign' if user navigates)
"""

from typing import Optional

import streamlit as st

from stage5_health import (
    estimate_drain_time,
    get_account_usage,
    get_queue_health,
    get_recent_failures,
)
from stage5_priority import describe_score
from time_utils import format_age, format_for_display


# ============================================================================
# MAIN ENTRY
# ============================================================================

def render_dashboard() -> Optional[str]:
    """Render the queue health dashboard."""
    st.title("📊 Queue Health Dashboard")

    col_refresh, col_spacer = st.columns([1, 4])
    with col_refresh:
        if st.button("🔄 Refresh data"):
            st.cache_data.clear()
            st.rerun()

    # ---- Load all metrics (cached) ----
    with st.spinner("Loading queue metrics..."):
        health = get_queue_health()
        accounts = get_account_usage()
        drain = estimate_drain_time()

    # ========================================================================
    # SECTION 1: Status overview
    # ========================================================================
    st.subheader("Queue Status")

    cols = st.columns(6)
    cols[0].metric("⏳ Queued", health.queued)
    cols[1].metric("📤 Sending", health.sending)
    cols[2].metric("✅ Sent", health.sent)
    cols[3].metric("❌ Failed", health.failed)
    cols[4].metric("⚠️ Bounced", health.bounced)
    cols[5].metric("📅 Scheduled", health.scheduled)

    # Backlog health row
    col1, col2, col3 = st.columns(3)
    with col1:
        if health.oldest_queued_age_hours is not None:
            age_str = f"{health.oldest_queued_age_hours}h"
            # Flag if backlog is getting stale
            delta_color = "inverse" if health.oldest_queued_age_hours > 6 else "off"
            st.metric(
                "Oldest queued",
                age_str,
                delta=health.oldest_queued_recipient[:24] if health.oldest_queued_recipient else None,
                delta_color="off",
            )
        else:
            st.metric("Oldest queued", "—")
    with col2:
        st.metric("Est. drain time", drain or "—")
    with col3:
        st.metric(
            "24h failure rate",
            f"{health.failure_rate_24h}%",
            delta="High" if health.failure_rate_24h > 5 else "OK",
            delta_color="inverse" if health.failure_rate_24h > 5 else "normal",
        )

    # Throughput
    col4, col5 = st.columns(2)
    with col4:
        st.metric("Sent (last 24h)", health.sent_last_24h)
    with col5:
        st.metric("Sent (last 1h)", health.sent_last_1h)

    st.divider()

    # ========================================================================
    # SECTION 2: Account usage
    # ========================================================================
    st.subheader("Sender Account Usage")

    if not accounts:
        st.warning(
            "No sender accounts configured. Run schema_setup_v4.py to seed "
            "the sender_accounts tab."
        )
    else:
        for acct in accounts:
            with st.container(border=True):
                # Header row
                cols = st.columns([3, 1, 1, 1])
                with cols[0]:
                    status_icon = "🟢" if (acct.is_active and not acct.is_exhausted and acct.within_window) else "🔴"
                    st.write(f"{status_icon} **{acct.display_name}**")
                    st.caption(acct.from_account)
                with cols[1]:
                    st.caption("Active")
                    st.write("Yes" if acct.is_active else "No")
                with cols[2]:
                    st.caption("In window")
                    st.write("Yes" if acct.within_window else "No")
                with cols[3]:
                    st.caption("Status")
                    st.write("Exhausted" if acct.is_exhausted else "Available")

                # Daily usage bar
                st.caption(
                    f"Daily: {acct.sends_last_24h} / {acct.daily_cap} "
                    f"({acct.daily_pct:.0f}%)"
                )
                st.progress(min(1.0, acct.daily_pct / 100))

                # Hourly usage bar
                st.caption(
                    f"Hourly: {acct.sends_last_1h} / {acct.hourly_cap} "
                    f"({acct.hourly_pct:.0f}%)"
                )
                st.progress(min(1.0, acct.hourly_pct / 100))

    st.divider()

    # ========================================================================
    # SECTION 3: Next to send
    # ========================================================================
    st.subheader("Next to Send (top 5 by priority)")

    if not health.next_to_send:
        st.info("Queue is empty — nothing waiting to send.")
    else:
        for i, row in enumerate(health.next_to_send, 1):
            score = row.get("priority_score", "")
            score_desc = ""
            if score not in ("", None):
                try:
                    score_desc = describe_score(int(score))
                except (ValueError, TypeError):
                    pass

            col1, col2, col3 = st.columns([1, 3, 3])
            with col1:
                st.write(f"**#{i}**")
            with col2:
                st.write(f"{row.get('brand', '?')} × {row.get('app_name', '?')}")
                st.caption(f"→ {row.get('recipient_email', '?')}")
            with col3:
                st.caption("Priority")
                st.write(score_desc or row.get("priority_tier", "?"))
                st.caption(f"Queued {format_age(row.get('queued_at'))}")

    st.divider()

    # ========================================================================
    # SECTION 4: Recent failures
    # ========================================================================
    st.subheader("Recent Failures")

    failures = get_recent_failures(limit=10)
    if not failures:
        st.success("✓ No recent failures.")
    else:
        for row in failures:
            status = row.get("status", "?")
            icon = "❌" if status.lower() == "failed" else "⚠️"
            with st.expander(
                f"{icon} {row.get('recipient_email', '?')} — "
                f"{row.get('brand', '?')} — "
                f"attempt {row.get('attempt_count', '?')} — "
                f"{format_age(row.get('last_attempt_at'))}"
            ):
                st.write(f"**Status:** {status}")
                st.write(f"**From account:** {row.get('from_account', '?')}")
                st.write(f"**Last attempt:** {format_for_display(row.get('last_attempt_at', ''))}")
                st.write(f"**Attempts:** {row.get('attempt_count', '?')}")
                if row.get("error_message"):
                    st.error(f"Error: {row.get('error_message')}")
                st.caption(f"Campaign: {row.get('campaign_id', '?')}")

    st.divider()

    # ---- Navigation ----
    col_new, col_queue, _ = st.columns([1, 1, 3])
    with col_new:
        if st.button("🆕 New campaign"):
            return "new_campaign"
    with col_queue:
        if st.button("📋 Full queue"):
            return "view_queue"

    return None
