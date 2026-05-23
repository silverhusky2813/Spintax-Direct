"""
stage1_ui.py
=============
Streamlit UI for Stage 1 — Campaign Setup.

Corrections applied from audit:
  - Issue C: Uses st.form() to batch input — validation runs only on submit,
    not on every keystroke
  - Issue A: Saves draft immediately after validation passes
  - Error 5.1: Simple user identification at app start (no real auth needed)
  - Error 5.2/5.3: priority_tier and variant_strategy marked "future use"
  - Errors 2.1: No fake performance data — only shows what we have
  - Errors 4.1/4.2: Presets and history merged into single "Load from..." flow

Drop-in: call render_stage1() from your main app.py.
Returns a campaign_id (str) when the user has saved a valid campaign,
or None if they're still in setup.
"""

from datetime import date, timedelta
from typing import Optional

import streamlit as st

from stage1_dedup import check_publisher_contact_history, is_suppressed
from stage1_history import (
    apply_preset_dates,
    format_campaign_for_display,
    format_preset_for_display,
    get_brand_history_summary,
    load_campaign_history,
    load_presets,
)
from stage1_persistence import generate_campaign_id, save_campaign
from stage1_validation import (
    VALID_CAMPAIGN_TYPES,
    VALID_PRIORITY_TIERS,
    VALID_SEGMENTS,
    VALID_VARIANT_STRATEGIES,
    VALID_VERTICALS,
    validate_campaign_input,
)


# ============================================================================
# USER IDENTIFICATION (lightweight, pre-auth)
# ============================================================================

def ensure_user_identified():
    """
    Lightweight 'who are you' selector at app start.
    Sets st.session_state['user_email']. Halts execution until selected.
    """
    if st.session_state.get("user_email"):
        return

    st.title("PremiumAds Spintax Tool")
    st.info("Before we start — who are you? (used for audit trail, not auth)")

    # Common team emails — adjust to your actual team
    team_options = [
        "daniel@premiumads.net",
        "Other (enter manually)",
    ]
    choice = st.selectbox("Pick your email:", team_options)

    if choice == "Other (enter manually)":
        manual = st.text_input("Enter your email:")
        if manual and "@" in manual:
            if st.button("Continue"):
                st.session_state["user_email"] = manual.strip().lower()
                st.rerun()
    else:
        if st.button("Continue"):
            st.session_state["user_email"] = choice
            st.rerun()

    st.stop()


# ============================================================================
# LOAD-FROM SECTION
# ============================================================================

def _render_load_from_section() -> dict:
    """
    Render the "Start from..." selector at the top of the form.
    Returns a dict of pre-filled values (or empty dict if Blank).
    """
    st.subheader("Start from")

    col1, col2 = st.columns([1, 3])

    with col1:
        load_source = st.radio(
            "Source:",
            ["Blank", "Recent campaign", "Saved preset"],
            label_visibility="collapsed",
        )

    prefill = {}

    if load_source == "Recent campaign":
        recent = load_campaign_history(num_recent=8)
        if not recent:
            with col2:
                st.info("No past campaigns yet. Pick 'Blank' to start fresh.")
        else:
            options = [format_campaign_for_display(c) for c in recent]
            with col2:
                selected_label = st.selectbox("Pick recent campaign:", options)
                idx = options.index(selected_label)
                selected = recent[idx]

                # Show what we know (no fake performance metrics)
                st.caption(
                    f"📋 Loaded from previous campaign. "
                    f"Note: performance tracking activates in Stage 7."
                )

                prefill = {
                    "brand": selected.get("brand", ""),
                    "app_name": selected.get("app_name", ""),
                    "vertical": selected.get("vertical", "Gaming"),
                    "campaign_type": selected.get("campaign_type", "Outreach"),
                    "cpm_floor": float(selected.get("cpm_floor", 5.0)),
                    "cpm_offer": float(selected.get("cpm_offer", 12.0)),
                    # Use TODAY's date for new campaign, not the old date
                    "flight_start": date.today() + timedelta(days=7),
                    "flight_end": date.today() + timedelta(days=37),
                    "priority_tier": selected.get("priority_tier", "Medium"),
                    "publisher_segment": selected.get("publisher_segment", "All"),
                    "variant_strategy": selected.get("variant_strategy", "Sequential"),
                    "notes": "",  # Don't carry over notes
                }

    elif load_source == "Saved preset":
        presets = load_presets()
        if not presets:
            with col2:
                st.info("No presets configured. Run schema_setup.py to seed starter presets.")
        else:
            options = [format_preset_for_display(p) for p in presets]
            with col2:
                selected_label = st.selectbox("Pick preset:", options)
                idx = options.index(selected_label)
                selected = presets[idx]

                flight_start, flight_end = apply_preset_dates(selected)

                prefill = {
                    "brand": selected.get("brand", ""),
                    "vertical": selected.get("vertical", "Gaming"),
                    "campaign_type": selected.get("campaign_type", "Outreach"),
                    "cpm_floor": float(selected.get("cpm_floor", 5.0)),
                    "cpm_offer": float(selected.get("cpm_offer", 12.0)),
                    "flight_start": flight_start,
                    "flight_end": flight_end,
                    "notes": selected.get("notes", ""),
                }

                if selected.get("notes"):
                    st.caption(f"📌 Preset notes: {selected['notes']}")

    return prefill


# ============================================================================
# MAIN STAGE 1 FORM
# ============================================================================

def render_stage1() -> Optional[str]:
    """
    Render the complete Stage 1 UI.

    Returns:
        campaign_id (str) if a valid campaign was saved this run,
        None if the user is still in setup or just loaded the page.
    """
    ensure_user_identified()

    st.title("Stage 1: Campaign Setup")
    st.caption(f"Logged in as: {st.session_state['user_email']}")

    # ----- Pre-form: load-from selector -----
    # NOTE: this is OUTSIDE st.form() so it can react immediately to selection.
    # The actual campaign form below will use these as defaults.
    prefill = _render_load_from_section()

    st.divider()

    # ----- Brand history sidebar (read-only context) -----
    # Show this when a brand has been entered previously in session state
    brand_for_context = prefill.get("brand", "") or st.session_state.get("last_brand", "")
    if brand_for_context:
        with st.expander(f"📊 What we know about {brand_for_context}", expanded=False):
            try:
                summary = get_brand_history_summary(brand_for_context)
                if summary["total_campaigns"] == 0:
                    st.write("First time campaigning for this brand.")
                else:
                    st.write(f"**Total past campaigns:** {summary['total_campaigns']}")
                    st.write(f"**Last sent:** {summary['last_sent_date']}")
                    st.write(f"**Verticals used:** {', '.join(summary['verticals_used'])}")
                    st.write(f"**Campaign types used:** {', '.join(summary['campaign_types_used'])}")
                    st.caption("⏳ Open/click/reply metrics will appear here after Stage 7.")
            except Exception as e:
                st.caption(f"(Could not load brand history: {e})")

    # ========================================================================
    # THE FORM — batched input, validates only on submit
    # ========================================================================
    with st.form("stage1_campaign_form", clear_on_submit=False):
        st.subheader("Target")
        col1, col2 = st.columns(2)
        with col1:
            brand = st.text_input(
                "Brand *",
                value=prefill.get("brand", ""),
                max_chars=50,
                help="The advertiser brand (e.g., Nike, Adidas)",
            )
            app_name = st.text_input(
                "App name *",
                value=prefill.get("app_name", ""),
                max_chars=100,
                help="The publisher's app (e.g., Clash Royale)",
            )

        with col2:
            vertical_default = prefill.get("vertical", "Gaming")
            if vertical_default not in VALID_VERTICALS:
                vertical_default = "Gaming"
            vertical = st.selectbox(
                "Vertical *",
                VALID_VERTICALS,
                index=VALID_VERTICALS.index(vertical_default),
            )

            ctype_default = prefill.get("campaign_type", "Outreach")
            if ctype_default not in VALID_CAMPAIGN_TYPES:
                ctype_default = "Outreach"
            campaign_type = st.selectbox(
                "Campaign type *",
                VALID_CAMPAIGN_TYPES,
                index=VALID_CAMPAIGN_TYPES.index(ctype_default),
            )

        st.subheader("Recipient")
        recipient_email = st.text_input(
            "Publisher email *",
            value="",
            help="Where this email will be sent",
        )

        st.subheader("CPM Economics")
        col3, col4 = st.columns(2)
        with col3:
            cpm_floor = st.number_input(
                "CPM floor (USD) *",
                min_value=0.10,
                max_value=50.00,
                value=prefill.get("cpm_floor", 5.00),
                step=0.50,
                format="%.2f",
                help="Minimum CPM you'll accept",
            )
        with col4:
            cpm_offer = st.number_input(
                "CPM offer (USD) *",
                min_value=0.10,
                max_value=50.00,
                value=prefill.get("cpm_offer", 12.00),
                step=0.50,
                format="%.2f",
                help="What you'll pay (must be >= floor)",
            )

        st.subheader("Flight Dates")
        col5, col6 = st.columns(2)
        with col5:
            flight_start = st.date_input(
                "Flight start *",
                value=prefill.get("flight_start", date.today() + timedelta(days=7)),
            )
        with col6:
            flight_end = st.date_input(
                "Flight end *",
                value=prefill.get("flight_end", date.today() + timedelta(days=37)),
            )

        st.subheader("Strategy (future use)")
        st.caption(
            "These fields will activate after Stage 2 (variant routing) and "
            "Stage 5 (priority-aware queue) upgrades. Set them now for future use."
        )
        col7, col8, col9 = st.columns(3)
        with col7:
            priority_tier = st.selectbox(
                "Priority",
                VALID_PRIORITY_TIERS,
                index=VALID_PRIORITY_TIERS.index(prefill.get("priority_tier", "Medium")),
                help="⏳ Will affect send order (Stage 5)",
            )
        with col8:
            publisher_segment = st.selectbox(
                "Segment",
                VALID_SEGMENTS,
                index=VALID_SEGMENTS.index(prefill.get("publisher_segment", "All")),
                help="⏳ Will filter recipients (future)",
            )
        with col9:
            variant_strategy = st.selectbox(
                "Variant strategy",
                VALID_VARIANT_STRATEGIES,
                index=VALID_VARIANT_STRATEGIES.index(prefill.get("variant_strategy", "Sequential")),
                help="⏳ Will control A/B testing (Stage 2)",
            )

        notes = st.text_area(
            "Internal notes (optional)",
            value=prefill.get("notes", ""),
            placeholder="e.g., 'VIP publisher relationship', 'Testing new subject', 'Time-sensitive'",
            max_chars=500,
            height=80,
        )

        col_submit_1, col_submit_2 = st.columns([1, 4])
        with col_submit_1:
            submitted = st.form_submit_button("Validate & Save Draft", type="primary")

    # ========================================================================
    # PROCESS SUBMISSION — only runs when form is submitted
    # ========================================================================
    if not submitted:
        return None

    campaign_data = {
        "brand": brand,
        "app_name": app_name,
        "vertical": vertical,
        "campaign_type": campaign_type,
        "cpm_floor": cpm_floor,
        "cpm_offer": cpm_offer,
        "flight_start": flight_start,
        "flight_end": flight_end,
        "recipient_email": recipient_email,
        "priority_tier": priority_tier,
        "publisher_segment": publisher_segment,
        "variant_strategy": variant_strategy,
        "notes": notes,
    }

    # ---- Step 1: Validate ----
    is_valid, errors = validate_campaign_input(campaign_data)
    if not is_valid:
        st.error("**Validation failed — please fix:**")
        for e in errors:
            st.write(f"  • {e}")
        return None

    # ---- Step 2: Check suppression list ----
    suppressed, reason = is_suppressed(recipient_email)
    if suppressed:
        st.error(
            f"⛔ **{recipient_email} is on the suppression list** "
            f"(reason: {reason}). Cannot send."
        )
        return None

    # ---- Step 3: Dedup check (campaign-type-aware) ----
    status, message, prior_contacts = check_publisher_contact_history(
        publisher_email=recipient_email,
        brand=brand,
        vertical=vertical,
        campaign_type=campaign_type,
    )

    if status == "duplicate":
        st.warning(f"⚠️ **Duplicate warning:** {message}")
        with st.expander("View prior contacts"):
            for pc in prior_contacts[:5]:
                st.write(
                    f"  • {pc['sent_at']} — {pc['campaign_type']} — "
                    f"Campaign: {pc['campaign_id']}"
                )
        confirm = st.checkbox("Send anyway (I have a good reason)", key="confirm_dup")
        if not confirm:
            return None

    elif status == "no_prior_contact":
        st.error(f"⛔ **{message}**")
        return None

    elif status == "stale_contact":
        st.warning(f"⚠️ **Stale contact:** {message}")
        confirm = st.checkbox("Send FollowUp anyway", key="confirm_stale")
        if not confirm:
            return None

    else:  # ok
        st.success(f"✓ {message}")

    # ---- Step 4: Save draft ----
    try:
        campaign_id = save_campaign(
            campaign_data,
            status="Draft",
            created_by=st.session_state["user_email"],
        )
        st.success(f"✓ Campaign saved. ID: `{campaign_id}`")
        st.session_state["current_campaign_id"] = campaign_id
        st.session_state["last_brand"] = brand
        st.info("**Next:** Proceed to Stage 2 to generate email variants.")
        return campaign_id
    except Exception as e:
        st.error(f"Failed to save campaign: {e}")
        return None
