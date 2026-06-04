"""
Streamlit view: Outreach Personalization Linter.

Thin UI over linter_rules.lint() — all logic lives in the tested engine.
Drop this into the spintax-direct repo and wire render_linter() into your
sidebar router (e.g. under the ADMIN / tooling section).
"""

import streamlit as st

from linter_rules import lint, SEVERITY_ORDER

_BADGE = {
    "error": ("🔴", "Error"),
    "warn": ("🟡", "Warning"),
    "info": ("🔵", "Info"),
}

_SAMPLE = """Hi [Name],

I'm Daniel from PremiumAds. We buy rewarded inventory and your US traffic fits.
We'll pay $12 CPM on rewarded against a $5 floor.

| Format       | Floor    | Ceiling  |
|--------------|----------|----------|
| Banner       | $0.20    | $0.80    |
| Interstitial | $2.00    | $5.00    |
| Rewarded     | $6.00    | $12.00   |

Confirm by Friday so I can lock the slot?

Daniel"""


def render_linter():
    st.header("✅ Outreach Linter")
    st.caption(
        "Paste a draft. Flags fake urgency, unfilled placeholders / generic "
        "greetings, missing opt-out, and CPM figures that contradict the "
        "rate-card table. Fully offline — no API calls, no token cost."
    )

    if st.button("Load sample (with deliberate problems)"):
        st.session_state["linter_text"] = _SAMPLE

    text = st.text_area(
        "Email draft",
        key="linter_text",
        height=320,
        placeholder="Paste the full email, including the rate-card table…",
    )

    if not text or not text.strip():
        st.info("Paste a draft above to lint it.")
        return

    findings = lint(text)

    if not findings:
        st.success("No issues found. Greeting personalized, no fake urgency, "
                   "opt-out present, CPMs consistent with the rate card.")
        return

    counts = {"error": 0, "warn": 0, "info": 0}
    for f in findings:
        counts[f.severity] += 1

    c1, c2, c3 = st.columns(3)
    c1.metric("🔴 Errors", counts["error"])
    c2.metric("🟡 Warnings", counts["warn"])
    c3.metric("🔵 Info", counts["info"])

    if counts["error"]:
        st.error("Has blocking issues — fix the errors before sending.")
    elif counts["warn"]:
        st.warning("Sendable, but the warnings are worth a look.")

    st.divider()

    for f in findings:
        icon, label = _BADGE[f.severity]
        st.markdown(f"**{icon} {label} · `{f.rule}`** — {f.message}")
        if f.evidence:
            st.code(f.evidence, language=None)


# Allow standalone run for quick manual testing: `streamlit run linter_ui.py`
if __name__ == "__main__":
    render_linter()
