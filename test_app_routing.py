"""
test_app_routing.py
=====================
Integration tests for the unified app.py router.

These cover the CONSOLIDATION SEAM — the routing + state-transition logic that
ties the 7 stages together. The per-stage unit tests (342 of them) test stage
internals; none test how app.py wires them. This suite fills that gap.

This is the layer where Bug 1 (broken send_another loop) lived undetected.

Approach: stub streamlit + the render_* functions, drive _route() with
controlled return values, assert the resulting session_state transitions.

Run with:
  python test_app_routing.py
"""

import sys
from unittest.mock import MagicMock


# ============================================================================
# STREAMLIT STUB
# ============================================================================
# We replace streamlit with a fake that records rerun() calls and gives us a
# real dict for session_state, so we can assert transitions.

class RerunSignal(Exception):
    """Mimics st.rerun() halting execution by raising."""


class FakeStreamlit:
    def __init__(self):
        self.session_state = {}
        self.rerun_count = 0
        self.secrets = {"sheet_id": "x", "service_account_b64": "x"}

    def rerun(self):
        self.rerun_count += 1
        raise RerunSignal()

    # Everything else is a no-op
    def __getattr__(self, name):
        return MagicMock()


# Install the stub BEFORE importing app
fake_st = FakeStreamlit()
sys.modules["streamlit"] = fake_st

# Stub the stage UI modules so importing app doesn't pull the whole tree
for mod in [
    "stage1_ui", "stage2_ui", "stage3_ui", "stage4_queue_view",
    "stage5_dashboard_ui", "stage6_accounts_ui", "stage7_analytics_ui",
]:
    sys.modules[mod] = MagicMock()

import app  # noqa: E402  (import after stubbing)


# ============================================================================
# TEST HELPERS
# ============================================================================

def reset_state():
    fake_st.session_state.clear()
    fake_st.rerun_count = 0


def drive_route():
    """Run _route() once, swallowing the RerunSignal so we can inspect state."""
    try:
        app._route()
    except RerunSignal:
        pass


def assert_eq(actual, expected, label):
    if actual == expected:
        print(f"  ✓ PASS: {label}")
        return True
    print(f"  ✗ FAIL: {label}")
    print(f"      Expected: {expected!r}")
    print(f"      Got:      {actual!r}")
    return False


def assert_true(cond, label):
    if cond:
        print(f"  ✓ PASS: {label}")
        return True
    print(f"  ✗ FAIL: {label}")
    return False


# ============================================================================
# Test: default view
# ============================================================================

def test_default_view():
    print("\n--- Test: default view is stage1 ---")
    reset_state()
    assert_eq(app._get_view(), "stage1", "no active_view → stage1")


# ============================================================================
# Test: Stage 1 → Stage 2 transition
# ============================================================================

def test_stage1_to_stage2():
    print("\n--- Test: Stage 1 returns campaign_id → advance to stage2 ---")
    reset_state()
    fake_st.session_state["active_view"] = "stage1"
    app.render_stage1 = MagicMock(return_value="campaign-123")

    drive_route()

    assert_eq(
        fake_st.session_state.get("current_campaign_id"),
        "campaign-123",
        "campaign_id stored",
    )
    assert_eq(fake_st.session_state.get("active_view"), "stage2", "advanced to stage2")


def test_stage1_no_advance_without_id():
    print("\n--- Test: Stage 1 returns None → stay on stage1 ---")
    reset_state()
    fake_st.session_state["active_view"] = "stage1"
    app.render_stage1 = MagicMock(return_value=None)

    drive_route()

    assert_true("current_campaign_id" not in fake_st.session_state, "no campaign stored")
    assert_eq(fake_st.session_state.get("active_view"), "stage1", "stayed on stage1")


# ============================================================================
# Test: Stage 2 guards against missing campaign
# ============================================================================

def test_stage2_missing_campaign_redirects():
    print("\n--- Test: Stage 2 with no campaign_id → redirect to stage1 ---")
    reset_state()
    fake_st.session_state["active_view"] = "stage2"
    # no current_campaign_id
    app.render_stage2 = MagicMock(return_value=None)

    drive_route()

    assert_eq(fake_st.session_state.get("active_view"), "stage1", "redirected to stage1")
    # CRITICAL: render_stage2 must NOT have been called with None
    app.render_stage2.assert_not_called()
    print("  ✓ PASS: render_stage2 NOT called when campaign_id missing")


def test_stage2_to_stage3():
    print("\n--- Test: Stage 2 returns approved → advance to stage3 ---")
    reset_state()
    fake_st.session_state["active_view"] = "stage2"
    fake_st.session_state["current_campaign_id"] = "c1"
    fake_approved = MagicMock()
    app.render_stage2 = MagicMock(return_value=fake_approved)

    drive_route()

    assert_eq(
        fake_st.session_state.get("current_approved"),
        fake_approved,
        "approved variant stored",
    )
    assert_eq(fake_st.session_state.get("active_view"), "stage3", "advanced to stage3")


# ============================================================================
# Test: Stage 3 guards
# ============================================================================

def test_stage3_missing_approved_redirects():
    print("\n--- Test: Stage 3 with no approved → redirect to stage2 ---")
    reset_state()
    fake_st.session_state["active_view"] = "stage3"
    app.render_stage3 = MagicMock(return_value=None)

    drive_route()

    assert_eq(fake_st.session_state.get("active_view"), "stage2", "redirected to stage2")
    app.render_stage3.assert_not_called()
    print("  ✓ PASS: render_stage3 NOT called when approved missing")


# ============================================================================
# Test: THE BUG — send_another must NOT loop to stage2 with same recipient
# ============================================================================

def test_send_another_goes_to_stage1_not_stage2():
    print("\n--- Test: 'send another' routes to STAGE 1 (Bug 1 fix) ---")
    reset_state()
    fake_st.session_state["active_view"] = "stage3"
    fake_st.session_state["current_campaign_id"] = "c1"
    fake_st.session_state["current_approved"] = MagicMock()
    fake_st.session_state["stage3_write_done_c1_alice@x.com"] = MagicMock()
    app.render_stage3 = MagicMock(return_value="send_another")

    drive_route()

    # The critical assertion: send_another goes to stage1, NOT stage2.
    # Stage 2 reads recipient from the campaign, so looping there would
    # re-target the same recipient (then get idempotency-blocked).
    assert_eq(
        fake_st.session_state.get("active_view"),
        "stage1",
        "send_another → stage1 (not the dead-end stage2 loop)",
    )
    # Campaign id must be cleared so Stage 1 starts a fresh pair
    assert_true(
        "current_campaign_id" not in fake_st.session_state,
        "campaign_id cleared for fresh (campaign, recipient) entry",
    )
    # The old recipient's write-done flag must be cleared
    assert_true(
        "stage3_write_done_c1_alice@x.com" not in fake_st.session_state,
        "stale write-done flag cleared",
    )


def test_send_another_preserves_user_identity():
    print("\n--- Test: 'send another' preserves user identity ---")
    reset_state()
    fake_st.session_state["active_view"] = "stage3"
    fake_st.session_state["current_campaign_id"] = "c1"
    fake_st.session_state["current_approved"] = MagicMock()
    fake_st.session_state["user_email"] = "daniel@premiumads.net"
    app.render_stage3 = MagicMock(return_value="send_another")

    drive_route()

    assert_eq(
        fake_st.session_state.get("user_email"),
        "daniel@premiumads.net",
        "user identity survives send_another (no re-prompt)",
    )


# ============================================================================
# Test: Stage 3 other actions
# ============================================================================

def test_stage3_new_campaign_full_reset():
    print("\n--- Test: 'new_campaign' clears everything but identity ---")
    reset_state()
    fake_st.session_state["active_view"] = "stage3"
    fake_st.session_state["current_campaign_id"] = "c1"
    fake_st.session_state["current_approved"] = MagicMock()
    fake_st.session_state["user_email"] = "daniel@premiumads.net"
    fake_st.session_state["stage2_c1_regen_count"] = 3
    app.render_stage3 = MagicMock(return_value="new_campaign")

    drive_route()

    assert_true("current_campaign_id" not in fake_st.session_state, "campaign cleared")
    assert_true("current_approved" not in fake_st.session_state, "approved cleared")
    assert_true("stage2_c1_regen_count" not in fake_st.session_state, "stage2 keys cleared")
    assert_eq(
        fake_st.session_state.get("user_email"),
        "daniel@premiumads.net",
        "identity preserved on new_campaign",
    )


def test_stage3_back_to_stage2_keeps_campaign():
    print("\n--- Test: 'back_to_stage2' keeps campaign, clears recipient state ---")
    reset_state()
    fake_st.session_state["active_view"] = "stage3"
    fake_st.session_state["current_campaign_id"] = "c1"
    fake_st.session_state["current_approved"] = MagicMock()
    app.render_stage3 = MagicMock(return_value="back_to_stage2")

    drive_route()

    assert_eq(fake_st.session_state.get("active_view"), "stage2", "went to stage2")
    assert_eq(
        fake_st.session_state.get("current_campaign_id"),
        "c1",
        "campaign PRESERVED (same recipient, re-pick variant)",
    )
    assert_true("current_approved" not in fake_st.session_state, "approved cleared")


def test_stage3_view_queue():
    print("\n--- Test: 'view_queue' navigates to queue ---")
    reset_state()
    fake_st.session_state["active_view"] = "stage3"
    fake_st.session_state["current_approved"] = MagicMock()
    app.render_stage3 = MagicMock(return_value="view_queue")

    drive_route()

    assert_eq(fake_st.session_state.get("active_view"), "queue", "navigated to queue")


# ============================================================================
# Test: monitoring screen navigation
# ============================================================================

def test_dashboard_to_queue():
    print("\n--- Test: dashboard 'view_queue' action ---")
    reset_state()
    fake_st.session_state["active_view"] = "dashboard"
    app.render_dashboard = MagicMock(return_value="view_queue")

    drive_route()

    assert_eq(fake_st.session_state.get("active_view"), "queue", "dashboard → queue")


def test_analytics_to_dashboard():
    print("\n--- Test: analytics 'dashboard' action ---")
    reset_state()
    fake_st.session_state["active_view"] = "analytics"
    app.render_analytics = MagicMock(return_value="dashboard")

    drive_route()

    assert_eq(fake_st.session_state.get("active_view"), "dashboard", "analytics → dashboard")


def test_accounts_to_analytics():
    print("\n--- Test: accounts 'analytics' action ---")
    reset_state()
    fake_st.session_state["active_view"] = "accounts"
    app.render_accounts = MagicMock(return_value="analytics")

    drive_route()

    assert_eq(fake_st.session_state.get("active_view"), "analytics", "accounts → analytics")


def test_unknown_view_resets():
    print("\n--- Test: unknown view → reset to stage1 ---")
    reset_state()
    fake_st.session_state["active_view"] = "bogus_view"

    drive_route()

    assert_eq(fake_st.session_state.get("active_view"), "stage1", "unknown view → stage1")


def test_monitoring_no_action_stays_put():
    print("\n--- Test: monitoring screen with no action stays on screen ---")
    reset_state()
    fake_st.session_state["active_view"] = "queue"
    app.render_queue_view = MagicMock(return_value=None)

    drive_route()

    assert_eq(fake_st.session_state.get("active_view"), "queue", "no action → stay on queue")


# ============================================================================
# RUNNER
# ============================================================================

def run_all():
    print("=" * 60)
    print("App Router Integration Test Suite")
    print("(the consolidation seam — untested before this)")
    print("=" * 60)

    test_default_view()
    test_stage1_to_stage2()
    test_stage1_no_advance_without_id()
    test_stage2_missing_campaign_redirects()
    test_stage2_to_stage3()
    test_stage3_missing_approved_redirects()
    test_send_another_goes_to_stage1_not_stage2()
    test_send_another_preserves_user_identity()
    test_stage3_new_campaign_full_reset()
    test_stage3_back_to_stage2_keeps_campaign()
    test_stage3_view_queue()
    test_dashboard_to_queue()
    test_analytics_to_dashboard()
    test_accounts_to_analytics()
    test_unknown_view_resets()
    test_monitoring_no_action_stays_put()

    print("\n" + "=" * 60)
    print("Router test suite complete.")
    print("=" * 60)


if __name__ == "__main__":
    run_all()
