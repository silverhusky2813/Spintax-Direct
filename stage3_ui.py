"""
stage3_ui.py
==============
Combined Stage 3 UI: preview + pre-send checks + queue write.

Per user's choice (Q2), this is a single screen — not split into Stage 3 +
Stage 4. The full flow:

  1. Show inbox preview (HTML rendered)
  2. Show plain-text fallback in expander
  3. Run pre-send checks (suppression fresh, dedup fresh, idempotency)
  4. Display check results
  5. Show "Confirm & Queue" button (disabled if any check blocks)
  6. On confirm → write_to_queue → show post-confirmation actions

Solves audit errors:
  - 3.5: Post-write workflow has three branches (send another / new / view queue)
  - 3.12: Preview prominent, can't miss it
  - 3.18: "Send another to same campaign" preserves campaign_id

Public API:
  render_stage3(approved_variant) → str | None
    Returns 'next_action' string indicating what user wants to do next:
      'new_campaign', 'send_another', 'view_queue', or None (still in flow)
"""

from dataclasses import dataclass
from typing import Optional

import streamlit as st

from stage1_persistence import get_campaign
from stage3_body_cleaner import clean_email_body, clean_subject_line
from stage3_html_renderer import make_inbox_preview_html, render_html_email
from stage3_presend_checks import (
    CheckResult,
    aggregate_status,
    run_all_presend_checks,
)
from stage3_queue_writer import WriteResult, write_to_queue
from time_utils import format_age


# ============================================================================
# DEFAULTS
# ============================================================================

DEFAULT_FROM_ACCOUNT = "daniel@premiumads.net"


# ============================================================================
# UI HELPERS
# ============================================================================

def _render_check_result(result: CheckResult):
    """Render one CheckResult in the UI with appropriate styling."""
    if result.status == "ok":
        st.success(f"✓ **{result.title}** — {result.detail}")
    elif result.status == "warn":
        st.warning(f"⚠️ **{result.title}** — {result.detail}")
    elif result.status == "block":
        st.error(f"⛔ **{result.title}** — {result.detail}")


def _render_post_confirm_actions(write_result: WriteResult, campaign_id: str) -> Optional[str]:
    """
    Show the post-confirm action buttons. Returns the chosen next action.
    """
    st.success(
        f"✅ **{write_result.action.title()}!** {write_result.message}\n\n"
        f"Idempotency key: `{write_result.idempotency_key}`"
    )

    st.divider()
    st.subheader("What next?")

    col1, col2, col3 = st.columns(3)

    with col1:
        if st.button("➕ Send another to this campaign", use_container_width=True):
            return "send_another"

    with col2:
        if st.button("🆕 Start new campaign", use_container_width=True):
            return "new_campaign"

    with col3:
        if st.button("📋 View queue", use_container_width=True):
            return "view_queue"

    return None


# ============================================================================
# MAIN ENTRY
# ============================================================================

def render_stage3(approved) -> Optional[str]:
    """
    Render the full Stage 3 flow.

    Args:
        approved: ApprovedVariant from stage2_ui

    Returns:
        Next-action string ('send_another' | 'new_campaign' | 'view_queue')
        if the user pressed a post-confirm button, otherwise None (still on screen).
    """
    st.title("Stage 3: Preview, Confirm & Queue")

    # Load campaign for context
    campaign = get_campaign(approved.campaign_id)
    if not campaign:
        st.error(f"⛔ Campaign {approved.campaign_id} not found.")
        return None

    # ---- Apply final cleaning (audit error 3.9) ----
    cleaned_subject = clean_subject_line(approved.subject)
    cleaned_body = clean_email_body(approved.body)
    html_body = render_html_email(cleaned_body)

    # ---- Top: campaign + recipient summary ----
    with st.container(border=True):
        col1, col2, col3 = st.columns(3)
        with col1:
            st.caption("Campaign")
            st.write(f"**{campaign.get('brand', '?')}** × {campaign.get('app_name', '?')}")
            st.caption(f"{campaign.get('campaign_type', '?')} • {campaign.get('vertical', '?')}")
        with col2:
            st.caption("Recipient")
            st.write(f"📧 {approved.recipient_email}")
            st.caption(f"Variant: {approved.template_id} v{approved.template_version}")
        with col3:
            st.caption("Editing status")
            if approved.was_edited:
                st.write(f"✏️ Edited")
                st.caption(
                    f"Subject: {approved.subject_edit_distance} chars • "
                    f"Body: {approved.body_edit_distance} chars"
                )
            else:
                st.write("Original variant")
                st.caption("No edits made")

    st.divider()

    # ---- Inbox preview (audit error 3.12: prominent) ----
    st.subheader("📨 Inbox Preview")
    st.caption("This is exactly how it will appear in the recipient's inbox.")

    from_account = st.session_state.get("from_account", DEFAULT_FROM_ACCOUNT)
    preview_html = make_inbox_preview_html(
        subject=cleaned_subject,
        html_body=html_body,
        from_account=from_account,
        to_email=approved.recipient_email,
    )
    st.markdown(preview_html, unsafe_allow_html=True)

    # ---- Plain text fallback view (expander) ----
    with st.expander("View plain-text version (fallback for non-HTML clients)"):
        st.text(f"Subject: {cleaned_subject}")
        st.divider()
        st.text(cleaned_body)

    # ---- Raw HTML view (expander, for debugging) ----
    with st.expander("View raw HTML (for debugging)"):
        st.code(html_body, language="html")

    st.divider()

    # ---- Pre-send checks (audit errors 3.7, 3.15) ----
    st.subheader("🛡️ Pre-send Safety Checks")

    with st.spinner("Running fresh suppression + dedup + idempotency checks..."):
        check_results, existing_row = run_all_presend_checks(
            campaign_id=approved.campaign_id,
            recipient_email=approved.recipient_email,
            brand=campaign.get("brand", ""),
            vertical=campaign.get("vertical", ""),
            campaign_type=campaign.get("campaign_type", ""),
        )

    for r in check_results:
        _render_check_result(r)

    overall_status = aggregate_status(check_results)

    st.divider()

    # ---- Confirmation gate ----
    # Persistent flag so we don't lose write result on rerun
    write_done_key = f"stage3_write_done_{approved.campaign_id}_{approved.recipient_email}"

    if st.session_state.get(write_done_key):
        # Already confirmed — show post-confirm actions
        write_result: WriteResult = st.session_state[write_done_key]
        return _render_post_confirm_actions(write_result, approved.campaign_id)

    # Not yet confirmed
    if overall_status == "block":
        st.error(
            "🛑 **Cannot proceed** — one or more blocking issues above. "
            "Resolve them or go back to edit the campaign."
        )
        if st.button("← Back to Stage 2"):
            return "back_to_stage2"
        return None

    # Sender selector (future: dropdown of multiple sending accounts)
    st.subheader("✉️ Sender Account")
    from_account = st.text_input(
        "From",
        value=DEFAULT_FROM_ACCOUNT,
        help="The Gmail account that will send this email",
    )
    st.session_state["from_account"] = from_account

    # If status is 'warn', require explicit override
    override_confirmed = True
    if overall_status == "warn":
        st.warning(
            "⚠️ **Warnings above.** Check the box below to acknowledge and proceed."
        )
        override_confirmed = st.checkbox(
            "I've reviewed the warnings and want to send anyway",
            key=f"override_{approved.campaign_id}",
        )

    # ---- Disable button after first click (audit error 3.4) ----
    pending_key = f"stage3_pending_{approved.campaign_id}_{approved.recipient_email}"

    col_confirm, col_back, _ = st.columns([2, 2, 3])

    with col_confirm:
        confirm_disabled = (
            not override_confirmed
            or st.session_state.get(pending_key, False)
        )
        if st.button(
            "✅ Confirm & Queue",
            type="primary",
            disabled=confirm_disabled,
            use_container_width=True,
        ):
            # Lock against double-click
            st.session_state[pending_key] = True

            with st.spinner("Writing to queue..."):
                write_result = write_to_queue(
                    approved=approved,
                    html_body=html_body,
                    from_account=from_account,
                    existing_row=existing_row,
                )

            # Persist result
            st.session_state[write_done_key] = write_result
            st.session_state[pending_key] = False  # release lock

            # Rerun to show post-confirm UI
            st.rerun()

    with col_back:
        if st.button("← Back to Stage 2", use_container_width=True):
            return "back_to_stage2"

    return None
