"""
stage7_analytics_ui.py
=======================
Variant performance analytics dashboard.

Per user's choice (Q4): a dedicated analytics screen, separate from the
Stage 5 operational health dashboard.

Shows:
  - Top-line reply rate, bounce rate, response breakdown
  - Per-variant performance (template × version) with sample-size guards
  - Per-subject-choice performance (which opener wins)
  - Per-campaign performance
  - Honest caveats about sample size (audit errors 7.7, 7.15)

Read-only. All metrics from compute_* functions in stage7_engagement.

Public API:
  render_analytics() → Optional[str]  (returns nav action)
"""

from typing import Optional

import gspread
import streamlit as st

from stage1_dedup import get_gspread_client
from stage7_engagement import (
    MIN_SAMPLE_FOR_RANKING,
    compute_campaign_stats,
    compute_overall_stats,
    compute_subject_choice_stats,
    compute_variant_stats,
    identify_best_subject_choice,
    identify_best_variant,
)


# ============================================================================
# DATA LOAD
# ============================================================================

@st.cache_data(ttl=120)
def _load_emails() -> list[dict]:
    """Load all Emails rows. Cached 2 min (analytics isn't real-time-critical)."""
    gc = get_gspread_client()
    sh = gc.open_by_key(st.secrets["sheet_id"])
    try:
        ws = sh.worksheet("Emails")
    except gspread.WorksheetNotFound:
        return []
    return ws.get_all_records()


# ============================================================================
# MAIN ENTRY
# ============================================================================

def render_analytics() -> Optional[str]:
    """Render the variant analytics dashboard."""
    st.title("📈 Variant Analytics")
    st.caption(
        "Which messaging actually gets replies. Reply rate is the metric that "
        "matters — opens/clicks aren't tracked (they're mostly noise now)."
    )

    col_refresh, _ = st.columns([1, 4])
    with col_refresh:
        if st.button("🔄 Refresh data"):
            st.cache_data.clear()
            st.rerun()

    with st.spinner("Crunching engagement data..."):
        rows = _load_emails()
        overall = compute_overall_stats(rows)
        variants = compute_variant_stats(rows)
        subject_choices = compute_subject_choice_stats(rows)
        campaigns = compute_campaign_stats(rows)

    # ---- Empty state ----
    if overall.total_sent == 0:
        st.info(
            "No sent emails yet. Once emails send and replies come in (the "
            "reply scan runs via Apps Script), analytics will appear here."
        )
        if st.button("🆕 New campaign"):
            return "new_campaign"
        return None

    # ========================================================================
    # SECTION 1: Top-line
    # ========================================================================
    st.subheader("Overall Performance")

    cols = st.columns(4)
    cols[0].metric("Sent", overall.total_sent)
    cols[1].metric(
        "Genuine reply rate",
        f"{overall.reply_rate}%",
        delta=f"{overall.genuine_replies} replies",
        delta_color="normal",
    )
    cols[2].metric(
        "Bounce rate",
        f"{overall.bounce_rate}%",
        delta=f"{overall.bounces} bounces",
        delta_color="inverse" if overall.bounce_rate > 3 else "off",
    )
    cols[3].metric("Auto-replies", overall.auto_replies)

    # Response breakdown
    st.caption("Response breakdown")
    bcols = st.columns(5)
    bcols[0].metric("✅ Genuine", overall.genuine_replies)
    bcols[1].metric("🤖 Auto", overall.auto_replies)
    bcols[2].metric("⚠️ Bounce", overall.bounces)
    bcols[3].metric("🚫 Unsub", overall.unsubscribes)
    bcols[4].metric("🔇 No reply", overall.no_response)

    # Bounce rate warning
    if overall.bounce_rate > 3:
        st.warning(
            f"⚠️ Bounce rate is {overall.bounce_rate}% — above the 3% threshold. "
            f"High bounce rates hurt sender reputation. Review your recipient list quality."
        )

    st.divider()

    # ========================================================================
    # SECTION 2: Best performer callout (with sample guard)
    # ========================================================================
    best_variant = identify_best_variant(variants)
    best_subject = identify_best_subject_choice(subject_choices)

    if best_variant or best_subject:
        st.subheader("🏆 Top Performers")
        if best_variant:
            st.success(
                f"**Best variant:** {best_variant.label} — "
                f"{best_variant.reply_rate}% reply rate "
                f"({best_variant.genuine_replies}/{best_variant.sent} sent)"
            )
        if best_subject:
            st.success(
                f"**Best subject opener:** \"{best_subject.subject_choice}\" — "
                f"{best_subject.reply_rate}% reply rate "
                f"({best_subject.genuine_replies}/{best_subject.sent} sent)"
            )
        st.divider()
    else:
        st.info(
            f"📊 Not enough data to declare winners yet. Need at least "
            f"{MIN_SAMPLE_FOR_RANKING} sends per variant before ranking "
            f"(avoids chasing noise on tiny samples)."
        )
        st.divider()

    # ========================================================================
    # SECTION 3: Per-variant table
    # ========================================================================
    st.subheader("Variant Performance")
    st.caption(
        f"Grouped by template + version. Variants with < {MIN_SAMPLE_FOR_RANKING} "
        f"sends are shown but not ranked (insufficient sample)."
    )

    if not variants:
        st.write("No variant data.")
    else:
        for v in variants:
            with st.container(border=True):
                cols = st.columns([3, 1, 1, 1, 1])
                with cols[0]:
                    sample_flag = "" if v.has_sufficient_sample else " ⚠️ low sample"
                    st.write(f"**{v.label}**{sample_flag}")
                    st.caption(
                        f"{v.genuine_replies} genuine · {v.auto_replies} auto · "
                        f"{v.bounces} bounce · {v.no_response} no-reply"
                    )
                with cols[1]:
                    st.caption("Sent")
                    st.write(str(v.sent))
                with cols[2]:
                    st.caption("Reply rate")
                    if v.has_sufficient_sample:
                        st.write(f"**{v.reply_rate}%**")
                    else:
                        st.write(f"{v.reply_rate}%*")
                with cols[3]:
                    st.caption("Bounce")
                    st.write(f"{v.bounce_rate}%")
                with cols[4]:
                    st.caption("Genuine")
                    st.write(str(v.genuine_replies))

                # Visual reply-rate bar (only meaningful with sample)
                if v.has_sufficient_sample:
                    st.progress(min(1.0, v.reply_rate / 100))

        st.caption("* reply rate shown but sample too small to rank reliably")

    st.divider()

    # ========================================================================
    # SECTION 4: Subject opener performance
    # ========================================================================
    st.subheader("Subject Opener Performance")
    st.caption("Which subject-line spin choice drives the most replies.")

    if not subject_choices:
        st.write(
            "No subject-choice data yet. (Requires spin_path_json — emails sent "
            "from Stage 2 onward carry this.)"
        )
    else:
        for s in subject_choices:
            cols = st.columns([4, 1, 1])
            with cols[0]:
                flag = "" if s.has_sufficient_sample else " ⚠️"
                st.write(f"\"{s.subject_choice}\"{flag}")
                st.caption(f"{s.label}")
            with cols[1]:
                st.caption("Sent")
                st.write(str(s.sent))
            with cols[2]:
                st.caption("Reply %")
                st.write(f"{s.reply_rate}%" if s.has_sufficient_sample else f"{s.reply_rate}%*")

    st.divider()

    # ========================================================================
    # SECTION 5: Per-campaign performance
    # ========================================================================
    st.subheader("Campaign Performance")

    if not campaigns:
        st.write("No campaign data.")
    else:
        for c in campaigns[:20]:  # cap display
            cols = st.columns([3, 1, 1, 1])
            with cols[0]:
                st.write(f"**{c.brand or '(no brand)'}**")
                st.caption(f"`{c.campaign_id[:16]}...`")
            with cols[1]:
                st.caption("Sent")
                st.write(str(c.sent))
            with cols[2]:
                st.caption("Replies")
                st.write(str(c.genuine_replies))
            with cols[3]:
                st.caption("Rate")
                st.write(f"{c.reply_rate}%")

    st.divider()

    # ---- Navigation ----
    col_new, col_dash, _ = st.columns([1, 1, 3])
    with col_new:
        if st.button("🆕 New campaign"):
            return "new_campaign"
    with col_dash:
        if st.button("📊 Health dashboard"):
            return "dashboard"

    return None
