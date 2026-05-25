"""
app.py
=======
PremiumAds Spintax Tool — unified entry point.

Wires all stages into one workflow with sidebar navigation and session-state
routing. This is the single file you run:

    streamlit run app.py

────────────────────────────────────────────────────────────────────────────
THE WORKFLOW (linear send flow + standalone monitoring screens)
────────────────────────────────────────────────────────────────────────────

  SEND FLOW (sequential):
    Stage 1  Campaign setup ──▶ Stage 2  Generate variant ──▶ Stage 3  Confirm & queue

  MONITORING (standalone, reachable any time via sidebar):
    Queue        — all queued/sent/failed rows (Stage 4 view)
    Dashboard    — operational health: throughput, drain time (Stage 5)
    Analytics    — variant reply-rate performance (Stage 7)
    Accounts     — sender health, warm-up, auto-pause (Stage 6)

Routing is driven by st.session_state["active_view"]. Each stage returns a
"next action" string; the router translates that into a view transition.
────────────────────────────────────────────────────────────────────────────
"""

import streamlit as st

# ---- Stage UIs ----
from stage1_ui import render_stage1, ensure_user_identified
from stage2_ui import render_stage2
from stage3_ui import render_stage3
from stage4_queue_view import render_queue_view
from stage5_dashboard_ui import render_dashboard
from stage6_accounts_ui import render_accounts
from stage7_analytics_ui import render_analytics
from setup_gate import ensure_schema_ready


# ============================================================================
# PAGE CONFIG
# ============================================================================

st.set_page_config(
    page_title="PremiumAds Spintax Tool",
    page_icon="📧",
    layout="wide",
)


# ============================================================================
# SESSION STATE HELPERS
# ============================================================================

def _get_view() -> str:
    """Current active view. Defaults to the start of the send flow."""
    return st.session_state.get("active_view", "stage1")


def _go(view: str):
    """Transition to a view and rerun."""
    st.session_state["active_view"] = view
    st.rerun()


def _reset_to_new_campaign():
    """Clear all flow state and return to Stage 1 fresh."""
    for key in [
        "active_view", "current_campaign_id", "current_approved",
    ]:
        st.session_state.pop(key, None)
    # Clear any per-campaign stage2/stage3 keys
    for key in list(st.session_state.keys()):
        if key.startswith("stage2_") or key.startswith("stage3_"):
            st.session_state.pop(key, None)
    st.rerun()


def _clear_recipient_state():
    """
    Clear recipient-specific state but KEEP the campaign — used for
    'back to stage 2' (re-pick a variant for the SAME recipient).
    """
    st.session_state.pop("current_approved", None)
    for key in list(st.session_state.keys()):
        if key.startswith("stage2_") or key.startswith("stage3_"):
            st.session_state.pop(key, None)


def _clear_send_flow_state():
    """
    Clear campaign + recipient + variant state, returning to a clean Stage 1,
    but PRESERVE user identity (don't re-prompt 'who are you'). Used by
    'send another to this campaign' — each send is a new (campaign, recipient)
    pair, and Stage 1's 'Load from recent' makes reusing settings fast.
    """
    for key in ["active_view", "current_campaign_id", "current_approved"]:
        st.session_state.pop(key, None)
    for key in list(st.session_state.keys()):
        if key.startswith("stage2_") or key.startswith("stage3_"):
            st.session_state.pop(key, None)


# ============================================================================
# SIDEBAR NAVIGATION
# ============================================================================

def _render_sidebar():
    with st.sidebar:
        st.title("📧 PremiumAds")
        st.caption("Spintax outreach engine")

        # User identity (set by ensure_user_identified)
        user = st.session_state.get("user_email")
        if user:
            st.caption(f"👤 {user}")

        st.divider()

        # --- Send flow ---
        st.caption("**SEND FLOW**")
        if st.button("➕ New campaign", use_container_width=True):
            _reset_to_new_campaign()

        # Show current position in the flow
        view = _get_view()
        flow_label = {
            "stage1": "① Campaign setup",
            "stage2": "② Generate variant",
            "stage3": "③ Confirm & queue",
        }.get(view)
        if flow_label:
            st.caption(f"Current: {flow_label}")

        st.divider()

        # --- Monitoring ---
        st.caption("**MONITORING**")
        if st.button("📋 Queue", use_container_width=True):
            _go("queue")
        if st.button("📊 Health dashboard", use_container_width=True):
            _go("dashboard")
        if st.button("📈 Analytics", use_container_width=True):
            _go("analytics")
        if st.button("✉️ Accounts", use_container_width=True):
            _go("accounts")

        st.divider()
        st.caption("v1.0 · 7-stage pipeline")


# ============================================================================
# VIEW ROUTER
# ============================================================================

def _route():
    view = _get_view()

    # ---- SEND FLOW ----

    if view == "stage1":
        campaign_id = render_stage1()
        if campaign_id:
            st.session_state["current_campaign_id"] = campaign_id
            _go("stage2")

    elif view == "stage2":
        campaign_id = st.session_state.get("current_campaign_id")
        if not campaign_id:
            _go("stage1")
            return  # _go raises rerun, but be explicit — never call render with None
        approved = render_stage2(campaign_id)
        if approved:
            st.session_state["current_approved"] = approved
            _go("stage3")

    elif view == "stage3":
        approved = st.session_state.get("current_approved")
        if not approved:
            _go("stage2")
            return  # never call render_stage3(None)
        action = render_stage3(approved)
        if action == "send_another":
            # BUG FIX: "send another" must return to Stage 1, not Stage 2.
            # The recipient_email lives on the campaign row (set in Stage 1);
            # Stage 2 reads it from there and has no recipient input. Looping
            # to Stage 2 would re-target the SAME recipient (then get blocked by
            # the idempotency check). Stage 1 is where a new recipient is
            # entered — and its "Load from recent campaign" feature makes
            # reusing the brand/vertical/CPM fast. We clear the campaign id so
            # Stage 1 starts a fresh (campaign, recipient) pair.
            _clear_send_flow_state()
            _go("stage1")
        elif action == "new_campaign":
            _reset_to_new_campaign()
        elif action == "view_queue":
            _go("queue")
        elif action == "back_to_stage2":
            # Genuine "go back and re-pick a variant for the SAME recipient"
            _clear_recipient_state()
            _go("stage2")

    # ---- MONITORING SCREENS ----

    elif view == "queue":
        action = render_queue_view()
        if action == "new_campaign":
            _reset_to_new_campaign()

    elif view == "dashboard":
        action = render_dashboard()
        if action == "new_campaign":
            _reset_to_new_campaign()
        elif action == "view_queue":
            _go("queue")

    elif view == "analytics":
        action = render_analytics()
        if action == "new_campaign":
            _reset_to_new_campaign()
        elif action == "dashboard":
            _go("dashboard")

    elif view == "accounts":
        action = render_accounts()
        if action == "new_campaign":
            _reset_to_new_campaign()
        elif action == "dashboard":
            _go("dashboard")
        elif action == "analytics":
            _go("analytics")

    else:
        # Unknown view — reset
        _go("stage1")


# ============================================================================
# MAIN
# ============================================================================

def main():
    # First-run gate: if the Sheet isn't initialized, show a one-click setup
    # screen and halt. No-op once the schema is ready.
    ensure_schema_ready()

    # Identify the user once (used for audit trail across the app).
    # ensure_user_identified() halts with st.stop() until a user is chosen.
    ensure_user_identified()

    _render_sidebar()
    _route()


if __name__ == "__main__":
    main()
