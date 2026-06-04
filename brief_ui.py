"""
Streamlit view: Research Brief Generator.

Thin UI over brief_fetch.gather() + brief_engine.build_brief(). All extraction
logic lives in the tested engine. Pulls from public sources only and never
writes or sends outreach — it produces a brief you read, then write the email
yourself, then check with the linter.

Wire render_brief() into your sidebar router alongside render_linter().
"""

import streamlit as st

from brief_fetch import gather
from brief_engine import build_brief


def render_brief():
    st.header("🔎 Research Brief")
    st.caption(
        "Enter a publisher and (optionally) public URLs — company site, a case "
        "study, an app listing. Pulls monetization model, geo weighting, ad "
        "formats, candidate titles, and any public contact emails into one brief. "
        "Free (Wikipedia API + the URLs you paste); never invents data."
    )

    publisher = st.text_input("Publisher name", placeholder="e.g. Mytona")
    urls_raw = st.text_area(
        "Public URLs to mine (optional, one per line)",
        height=110,
        placeholder="https://www.example.com/\nhttps://www.example.com/games\nhttps://partner.example.io/case-study",
    )

    if not st.button("Generate brief", type="primary"):
        st.info("Wikipedia is always checked. Adding the company site or a case-study "
                "URL makes the brief far richer.")
        return

    if not publisher or not publisher.strip():
        st.warning("Enter a publisher name first.")
        return

    extra = [u for u in urls_raw.splitlines() if u.strip()]

    with st.spinner("Fetching public sources…"):
        sources, fetch_warnings = gather(publisher.strip(), extra)
        brief = build_brief(publisher.strip(), sources)

    # surface fetch problems (rate limits, blocked scrapers, bad URLs)
    for w in fetch_warnings:
        st.warning(w)

    # --- headline signals ---
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Monetization")
        st.write(f"**{brief.monetization}**")
        if brief.monetization_evidence:
            st.caption("evidence: " + ", ".join(brief.monetization_evidence))
    with c2:
        st.subheader("Ad formats")
        st.write(", ".join(brief.formats) if brief.formats else "_none detected_")

    st.subheader("Geo weighting")
    if brief.geos:
        for g, n in brief.geos:
            st.write(f"- {g} ({n})")
    else:
        st.write("_none detected_")

    st.subheader("Candidate titles (verify)")
    st.write(", ".join(brief.titles) if brief.titles else "_none detected_")

    st.subheader("Contact emails found")
    if brief.contacts:
        for e, generic in brief.contacts:
            st.write(f"- {e}" + ("  ·  _generic, find the real owner_" if generic else ""))
    else:
        st.write("_none found_")

    if brief.warnings:
        st.divider()
        st.markdown("**⚠️ Verify before acting**")
        for w in brief.warnings:
            st.markdown(f"- {w}")

    # --- copyable markdown brief ---
    st.divider()
    st.caption("Copy this brief, write your email from it, then run it through the Linter.")
    st.code(brief.to_markdown(), language="markdown")


if __name__ == "__main__":
    render_brief()
