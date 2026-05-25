"""
setup_gate.py
==============
First-run setup helper. Makes deployment one-click:

On first load, checks whether the Google Sheet has been initialized (looks for
the Campaigns tab + recipient_email column). If not, shows a single "Set up
Google Sheet" button that runs all migrations — so the user never has to touch
a terminal.

Once set up, this is a no-op (one cheap cached check per session).
"""

import streamlit as st


@st.cache_data(ttl=300)
def _schema_is_ready() -> bool:
    """
    Cheap check: does the Sheet have the Campaigns tab WITH recipient_email?
    Cached 5 min so we don't hit Sheets on every rerun.
    """
    try:
        from schema_setup import get_gspread_client, get_sheet_id
        gc = get_gspread_client()
        sh = gc.open_by_key(get_sheet_id())
        ws = sh.worksheet("Campaigns")
        headers = ws.row_values(1)
        # The bugfix column is the marker for "fully migrated"
        return "recipient_email" in headers
    except Exception:
        return False


def ensure_schema_ready():
    """
    Gate the app behind schema setup. If the Sheet isn't initialized, show a
    one-click setup screen and halt. Returns normally once ready.
    """
    if _schema_is_ready():
        return

    st.title("📧 PremiumAds Spintax Tool")
    st.subheader("First-time setup")
    st.info(
        "Your Google Sheet needs to be initialized before first use. This "
        "creates all the tabs and columns the tool requires. It's safe to run "
        "more than once — it only adds what's missing."
    )

    # Verify secrets are present before offering the button
    secrets_ok = True
    missing = []
    for key in ("sheet_id", "service_account_b64"):
        try:
            _ = st.secrets[key]
        except Exception:
            secrets_ok = False
            missing.append(key)

    if not secrets_ok:
        st.error(
            "⚠️ Missing secrets: "
            + ", ".join(f"`{m}`" for m in missing)
            + ". Add them in App → Settings → Secrets, then reload."
        )
        st.stop()

    if st.button("🚀 Set up Google Sheet", type="primary"):
        with st.spinner("Creating tabs and columns... (this takes ~30 seconds)"):
            try:
                from migrate_all import run_all_migrations
                run_all_migrations(verbose=False)
            except Exception as e:
                st.error(f"Setup failed: {type(e).__name__}: {e}")
                st.stop()
        st.cache_data.clear()
        st.success("✓ Setup complete! Reloading...")
        st.rerun()

    st.caption(
        "This runs all schema migrations (Campaigns, Publishers, sender "
        "accounts, reply tracking, health logs, and the recipient_email fix)."
    )
    st.stop()
