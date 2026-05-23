"""
stage2_ui.py
=============
Streamlit UI for Stage 2 — Variant Generation, Review, and Edit.

Solves audit errors:
  - 2.10: Regenerate handles edited rows safely (warns, preserves on user choice)
  - 2.15: Edits tracked separately from variant_id
  - 2.16: Spin space counter shown ("3 / 729 unique variants seen")
  - 2.18: Fallback warnings surfaced prominently
  - 2.19: CPM table fallback warned
  - 2.7: "Ready to send" gate based on missing required variables

Flow:
  1. Read campaign_id from session state (from Stage 1)
  2. Generate initial variant (regenerate_count=0)
  3. Show subject + body in editable text fields
  4. "Regenerate" button increments count, generates new spin
  5. "Approve & Queue" button passes (subject, body, metadata) to Stage 4

Public API:
  render_stage2(campaign_id) → ApprovedVariant | None
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import streamlit as st

from stage1_persistence import get_campaign
from stage2_publishers import (
    get_publisher,
    guess_first_name_from_email,
    upsert_publisher,
)
from stage2_spintax_engine import count_spin_space
from stage2_templates import (
    Template,
    get_template,
    get_template_for_campaign_type,
    list_templates,
)
from stage2_variants import (
    GeneratedVariant,
    detect_edits,
    generate_variant,
)


# ============================================================================
# DATA STRUCTURE FOR APPROVED OUTPUT
# ============================================================================

@dataclass
class ApprovedVariant:
    """
    What Stage 2 hands off to Stage 4 (Queue) when the user approves.

    Carries all tracking metadata so the Emails row can be properly tagged.
    """
    campaign_id: str
    recipient_email: str
    template_id: str
    template_version: int

    subject: str   # may differ from variant.subject if user edited
    body: str      # may differ from variant.body if user edited

    spin_path_json: dict
    was_edited: bool
    subject_was_edited: bool
    body_was_edited: bool
    subject_edit_distance: int
    body_edit_distance: int

    generated_at: str
    approved_at: str


# ============================================================================
# SESSION STATE HELPERS
# ============================================================================

def _get_state_key(campaign_id: str, suffix: str) -> str:
    """Namespace session state keys per campaign to avoid collisions."""
    return f"stage2_{campaign_id}_{suffix}"


def _get_regen_count(campaign_id: str) -> int:
    """Current regenerate counter for this campaign."""
    return st.session_state.get(_get_state_key(campaign_id, "regen_count"), 0)


def _bump_regen_count(campaign_id: str):
    """Increment the regenerate counter."""
    key = _get_state_key(campaign_id, "regen_count")
    st.session_state[key] = _get_regen_count(campaign_id) + 1


def _reset_regen_count(campaign_id: str):
    key = _get_state_key(campaign_id, "regen_count")
    st.session_state[key] = 0


def _get_seen_variants_count(campaign_id: str) -> int:
    """How many unique variants the user has seen for this campaign."""
    return st.session_state.get(_get_state_key(campaign_id, "seen_count"), 0)


def _increment_seen(campaign_id: str):
    key = _get_state_key(campaign_id, "seen_count")
    st.session_state[key] = _get_seen_variants_count(campaign_id) + 1


# ============================================================================
# UI: PUBLISHER QUICK-ADD (audit error 2.18: prompt to fill missing data)
# ============================================================================

def _render_publisher_quickadd(recipient_email: str, fields_missing: list[str]):
    """
    Inline quick-add form for filling Publishers tab without leaving the flow.
    Renders when fallback was used.
    """
    with st.expander(f"➕ Add {recipient_email} to Publishers tab", expanded=False):
        st.caption(
            "Adding publisher data improves email personalization. "
            "This won't regenerate the current email — it'll apply on next generation."
        )

        # Try to guess first name from email
        guess = guess_first_name_from_email(recipient_email)

        col1, col2 = st.columns(2)
        with col1:
            first_name = st.text_input(
                "First name",
                value=guess,
                key=f"pub_add_first_{recipient_email}",
            )
            last_name = st.text_input(
                "Last name",
                key=f"pub_add_last_{recipient_email}",
            )
        with col2:
            publisher_name = st.text_input(
                "Company / Publisher name",
                key=f"pub_add_company_{recipient_email}",
            )
            tier = st.selectbox(
                "Tier",
                ["Unverified", "Tier1", "Tier2", "Tier3"],
                key=f"pub_add_tier_{recipient_email}",
            )

        notes = st.text_input("Notes (optional)", key=f"pub_add_notes_{recipient_email}")

        if st.button("Save to Publishers tab", key=f"pub_save_btn_{recipient_email}"):
            try:
                result = upsert_publisher(
                    email=recipient_email,
                    first_name=first_name,
                    last_name=last_name,
                    publisher_name=publisher_name,
                    publisher_tier=tier,
                    notes=notes,
                )
                if result == "created":
                    st.success(f"✓ Created publisher entry for {recipient_email}")
                else:
                    st.success(f"✓ Updated publisher entry for {recipient_email}")
                st.caption("Click 'Regenerate' to apply the new data.")
            except Exception as e:
                st.error(f"Failed to save: {e}")


# ============================================================================
# UI: TEMPLATE SELECTOR
# ============================================================================

def _render_template_selector(campaign: dict) -> str:
    """
    Optional template override selector. Defaults to type-matched template.
    Returns the chosen template_id.
    """
    ctype = campaign.get("campaign_type", "Outreach")
    default_template = get_template_for_campaign_type(ctype)

    # Filter templates to those matching this campaign type
    matching = [
        t for t in list_templates()
        if t.campaign_type == ctype
    ]

    if len(matching) <= 1:
        # Only one option, no need for a selector
        return default_template.template_id

    options = [(t.template_id, f"{t.template_id} (v{t.template_version}) — {t.notes[:50]}")
               for t in matching]
    ids = [o[0] for o in options]
    labels = [o[1] for o in options]

    default_idx = ids.index(default_template.template_id) if default_template.template_id in ids else 0
    chosen = st.selectbox(
        "Template (optional override):",
        labels,
        index=default_idx,
        help="Templates compatible with this campaign type",
    )
    return ids[labels.index(chosen)]


# ============================================================================
# MAIN STAGE 2 ENTRY
# ============================================================================

def render_stage2(campaign_id: str) -> Optional[ApprovedVariant]:
    """
    Render the full Stage 2 UI.

    Args:
        campaign_id: From Stage 1 — must reference a saved campaign

    Returns:
        ApprovedVariant if user clicked "Approve & Queue"
        None otherwise
    """
    st.title("Stage 2: Generate Email Variant")

    # ---- Load campaign ----
    campaign = get_campaign(campaign_id)
    if not campaign:
        st.error(f"⛔ Campaign {campaign_id} not found. Go back to Stage 1.")
        return None

    recipient_email = campaign.get("recipient_email", "")
    if not recipient_email:
        st.error(
            "⛔ Campaign has no recipient_email. "
            "This shouldn't happen after Stage 1 — check the campaign data."
        )
        return None

    # ---- Campaign summary header ----
    with st.container(border=True):
        col1, col2, col3 = st.columns(3)
        with col1:
            st.caption("Campaign")
            st.write(f"**{campaign.get('brand', '?')}** × {campaign.get('app_name', '?')}")
            st.caption(f"Type: {campaign.get('campaign_type', '?')}  •  "
                       f"Vertical: {campaign.get('vertical', '?')}")
        with col2:
            st.caption("Recipient")
            st.write(f"📧 {recipient_email}")
            # Show known publisher info inline
            pub = get_publisher(recipient_email)
            if pub:
                first = pub.get("first_name", "")
                company = pub.get("publisher_name", "")
                tier = pub.get("publisher_tier", "")
                if first or company:
                    st.caption(f"{first} ({company}) — {tier}")
            else:
                st.caption("⚠️ Not in Publishers tab")
        with col3:
            st.caption("Flight & CPM")
            st.write(f"💰 ${campaign.get('cpm_floor', 0)} → ${campaign.get('cpm_offer', 0)}")
            st.caption(f"{campaign.get('flight_start', '?')} → {campaign.get('flight_end', '?')}")

    st.divider()

    # ---- Template override (if applicable) ----
    template_id = _render_template_selector(campaign)

    # ---- Generation ----
    regen_count = _get_regen_count(campaign_id)

    try:
        variant: GeneratedVariant = generate_variant(
            campaign_id=campaign_id,
            recipient_email=recipient_email,
            regenerate_count=regen_count,
            template_id_override=template_id,
        )
    except Exception as e:
        st.error(f"⛔ Variant generation failed: {e}")
        return None

    # Count: only increment once per regen click (key matches regen_count)
    seen_key = _get_state_key(campaign_id, f"seen_regen_{regen_count}")
    if seen_key not in st.session_state:
        _increment_seen(campaign_id)
        st.session_state[seen_key] = True

    # ---- Spin space counter (audit error 2.16) ----
    template = get_template(template_id)
    total_combos = template.total_spin_space
    seen = _get_seen_variants_count(campaign_id)

    col_counter, col_regen = st.columns([3, 1])
    with col_counter:
        st.caption(
            f"🎲 Variant **{seen} of {total_combos:,}** possible combinations  "
            f"•  Seed: `{variant.seed}`  •  Regen count: {regen_count}"
        )
    with col_regen:
        if st.button("🔄 Regenerate", use_container_width=True):
            # Check if there are unsaved edits — warn before discarding
            subj_key = _get_state_key(campaign_id, "subject_edit")
            body_key = _get_state_key(campaign_id, "body_edit")

            edited_subj = st.session_state.get(subj_key, variant.subject)
            edited_body = st.session_state.get(body_key, variant.body)

            edits = detect_edits(variant, edited_subj, edited_body)
            if edits["was_edited"]:
                st.session_state[_get_state_key(campaign_id, "regen_pending")] = True
                st.rerun()
            else:
                _bump_regen_count(campaign_id)
                # Clear edits since they were identical to generated
                st.session_state.pop(subj_key, None)
                st.session_state.pop(body_key, None)
                st.rerun()

    # ---- Pending regenerate-with-edits confirmation ----
    pending_key = _get_state_key(campaign_id, "regen_pending")
    if st.session_state.get(pending_key):
        st.warning(
            "⚠️ **You have unsaved edits.** Regenerating will discard them. "
            "Are you sure?"
        )
        c1, c2, _ = st.columns([1, 1, 4])
        with c1:
            if st.button("✓ Discard edits & regenerate"):
                _bump_regen_count(campaign_id)
                st.session_state.pop(_get_state_key(campaign_id, "subject_edit"), None)
                st.session_state.pop(_get_state_key(campaign_id, "body_edit"), None)
                st.session_state[pending_key] = False
                st.rerun()
        with c2:
            if st.button("✗ Keep edits"):
                st.session_state[pending_key] = False
                st.rerun()

    # ---- Warnings (audit errors 2.18, 2.19, 2.7) ----
    if variant.warnings:
        for w in variant.warnings:
            if w.startswith("⛔"):
                st.error(w)
            else:
                st.warning(w)

    if variant.publisher_fallback_used:
        _render_publisher_quickadd(
            recipient_email,
            variant.publisher_fallback_fields,
        )

    # ---- Editable Subject ----
    st.subheader("Subject")
    subj_state_key = _get_state_key(campaign_id, "subject_edit")
    if subj_state_key not in st.session_state:
        st.session_state[subj_state_key] = variant.subject

    edited_subject = st.text_input(
        "Subject line",
        key=subj_state_key,
        label_visibility="collapsed",
    )

    # ---- Editable Body ----
    st.subheader("Body")
    body_state_key = _get_state_key(campaign_id, "body_edit")
    if body_state_key not in st.session_state:
        st.session_state[body_state_key] = variant.body

    edited_body = st.text_area(
        "Body",
        key=body_state_key,
        label_visibility="collapsed",
        height=420,
    )

    # ---- Edit tracking display ----
    edits = detect_edits(variant, edited_subject, edited_body)
    if edits["was_edited"]:
        st.caption(
            f"✏️ **Edited** — "
            f"Subject: {edits['subject_edit_distance']} chars changed  •  "
            f"Body: {edits['body_edit_distance']} chars changed"
        )

    st.divider()

    # ---- Approve / Cancel ----
    col_approve, col_cancel, col_spacer = st.columns([1, 1, 3])

    with col_approve:
        approve_disabled = not variant.is_ready_to_send
        approve_help = (
            "Cannot send: required variables missing"
            if approve_disabled
            else "Save to send queue (Stage 4)"
        )
        if st.button(
            "✓ Approve & Queue",
            type="primary",
            disabled=approve_disabled,
            help=approve_help,
            use_container_width=True,
        ):
            return ApprovedVariant(
                campaign_id=campaign_id,
                recipient_email=recipient_email,
                template_id=variant.template_id,
                template_version=variant.template_version,

                subject=edited_subject,
                body=edited_body,

                spin_path_json=variant.spin_path_json,
                was_edited=edits["was_edited"],
                subject_was_edited=edits["subject_was_edited"],
                body_was_edited=edits["body_was_edited"],
                subject_edit_distance=edits["subject_edit_distance"],
                body_edit_distance=edits["body_edit_distance"],

                generated_at=variant.generated_at,
                approved_at=datetime.now().isoformat(),
            )

    with col_cancel:
        if st.button("← Back to Stage 1", use_container_width=True):
            # Clear Stage 2 state for this campaign
            for key in list(st.session_state.keys()):
                if key.startswith(f"stage2_{campaign_id}_"):
                    del st.session_state[key]
            # Clear current campaign so Stage 1 shows again
            st.session_state.pop("current_campaign_id", None)
            st.rerun()

    return None
