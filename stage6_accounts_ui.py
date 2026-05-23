"""
stage6_accounts_ui.py
======================
Account management dashboard — sender health, warm-up status, pause controls.

Shows:
  - Each account's health (bounce rate, status), warm-up progress
  - Auto-pause status with reason + manual reactivate button
  - A "Run health check" button that enforces (pauses critical accounts)
  - Health trend from account_health_log

Per the audit (6.6): health SCORING displays read-only on render; ENFORCEMENT
(actual pauses) only happens when the user clicks "Run health check & enforce"
or the scheduled Apps Script runs it.

Public API:
  render_accounts() → Optional[str]  (nav action)
"""

from typing import Optional

import gspread
import streamlit as st

from stage1_dedup import get_gspread_client
from stage6_enforcement import reactivate_account, run_health_check
from stage6_warmup import warmup_status_label
from time_utils import format_age, format_for_display


# ============================================================================
# DATA
# ============================================================================

@st.cache_data(ttl=30)
def _load_accounts() -> list[dict]:
    gc = get_gspread_client()
    sh = gc.open_by_key(st.secrets["sheet_id"])
    try:
        ws = sh.worksheet("sender_accounts")
    except gspread.WorksheetNotFound:
        return []
    return ws.get_all_records()


# ============================================================================
# MAIN
# ============================================================================

def render_accounts() -> Optional[str]:
    """Render the account management + health dashboard."""
    st.title("✉️ Sender Accounts & Health")
    st.caption(
        "Monitor sender reputation, warm-up progress, and auto-pause unhealthy "
        "accounts before they damage deliverability."
    )

    # ---- Action bar ----
    col1, col2, col3 = st.columns([2, 2, 2])
    with col1:
        if st.button("🔄 Refresh"):
            st.cache_data.clear()
            st.rerun()
    with col2:
        run_check = st.button("🩺 Run health check (dry run)")
    with col3:
        run_enforce = st.button("⚠️ Run check & enforce pauses", type="primary")

    # ---- Run health check if requested ----
    health_results = None
    if run_check or run_enforce:
        with st.spinner("Assessing account health..."):
            health_results = run_health_check(enforce=run_enforce)
        if run_enforce:
            paused = [r for r in health_results if r["action_taken"] == "auto_paused"]
            if paused:
                st.error(
                    f"🛑 Auto-paused {len(paused)} account(s): "
                    + ", ".join(r["from_account"] for r in paused)
                )
            else:
                st.success("✓ Health check complete — no accounts needed pausing.")
            st.cache_data.clear()

    # ---- Load accounts ----
    accounts = _load_accounts()

    if not accounts:
        st.warning("No sender accounts configured. Run schema_setup_v4.py.")
        return None

    # Build a lookup of health results by account (if we ran a check)
    health_by_account = {}
    if health_results:
        health_by_account = {r["from_account"]: r for r in health_results}

    # ---- Account cards ----
    st.divider()
    st.subheader("Accounts")

    for acct in accounts:
        email = str(acct.get("from_account", ""))
        is_active = str(acct.get("is_active", "TRUE")).strip().upper() == "TRUE"
        warmup_enabled = str(acct.get("warmup_enabled", "FALSE")).strip().upper() == "TRUE"
        activated_at = acct.get("activated_at", "")
        configured_cap = int(acct.get("daily_cap", 200) or 200)

        with st.container(border=True):
            # Header
            cols = st.columns([3, 2, 2])
            with cols[0]:
                icon = "🟢" if is_active else "⏸️"
                st.write(f"{icon} **{acct.get('display_name', email)}**")
                st.caption(email)
            with cols[1]:
                st.caption("Status")
                if is_active:
                    st.write("Active")
                else:
                    st.write("**Paused**")
            with cols[2]:
                st.caption("Daily cap")
                st.write(f"{configured_cap}/day")

            # Warm-up status
            wlabel = warmup_status_label(warmup_enabled, activated_at, configured_cap)
            st.caption(f"🌡️ {wlabel}")

            # Pause info (if paused)
            if not is_active:
                reason = acct.get("paused_reason", "")
                paused_at = acct.get("paused_at", "")
                if reason:
                    st.error(f"Paused {format_age(paused_at)}: {reason}")
                # Reactivate button
                if st.button(f"▶️ Reactivate {email}", key=f"react_{email}"):
                    if reactivate_account(email):
                        st.success(f"✓ Reactivated {email} (24h grace window started)")
                        st.cache_data.clear()
                        st.rerun()
                    else:
                        st.error("Reactivation failed.")

            # Health result (if we just ran a check)
            if email in health_by_account:
                h = health_by_account[email]
                status_color = {
                    "healthy": "🟢",
                    "warning": "🟡",
                    "critical": "🔴",
                    "insufficient_data": "⚪",
                }.get(h["status"], "⚪")

                hcols = st.columns([2, 2, 2])
                with hcols[0]:
                    st.caption("Health")
                    st.write(f"{status_color} {h['status']}")
                with hcols[1]:
                    st.caption("Bounce rate (7d)")
                    st.write(f"{h['bounce_rate']}%")
                with hcols[2]:
                    st.caption("Volume (7d)")
                    st.write(f"{h['sends_window']} sent")

                if h["status"] in ("warning", "critical"):
                    st.warning(h["reason"])
                elif h["status"] == "insufficient_data":
                    st.caption(h["reason"])

    # ---- Reputation guidance ----
    st.divider()
    with st.expander("📖 How health & warm-up work"):
        st.markdown(
            """
**Health scoring** looks at bounce rate over the trailing 7 days, but only once
an account has at least 20 sends in that window (avoids panicking over tiny
samples).

- **Below 3%** → healthy
- **3–5%** → warning (alert only)
- **Above 5%** → critical (auto-pause candidate)

**Auto-pause guards:**
- Never pauses your *last* active account (that would halt all sends)
- A 24-hour grace window after manual reactivation prevents instant re-pause

**Warm-up** ramps new accounts from 20/day up to full capacity over ~4 weeks.
The effective cap is always `min(configured cap, warm-up cap)` — warm-up only
restricts, never raises. Existing accounts default to warm-up OFF (presumed
already warm).

**To add an account:** add a row to the `sender_accounts` tab. Set
`warmup_enabled=TRUE` and `activated_at` to today for a fresh domain.
            """
        )

    # ---- Navigation ----
    st.divider()
    col_new, col_dash, col_analytics = st.columns([1, 1, 1])
    with col_new:
        if st.button("🆕 New campaign"):
            return "new_campaign"
    with col_dash:
        if st.button("📊 Health dashboard"):
            return "dashboard"
    with col_analytics:
        if st.button("📈 Analytics"):
            return "analytics"

    return None
