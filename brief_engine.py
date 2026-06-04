"""
Research-brief engine — pure extraction (stdlib only, no network, no Streamlit).

Given raw text scraped from PUBLIC pages, extract the signals a partnerships
lead actually needs before writing one personalized outreach email:

  - monetization model (IAP / ads / hybrid)   <- decides if an ad-buy even fits
  - geo weighting                              <- where their audience over-indexes
  - ad formats they run                        <- which placements to pitch
  - candidate game titles                      <- something specific to anchor on
  - contact emails found on the page           <- starting point, not a target list

Design mirrors linter_rules.py: pure functions, dataclass output, fully
offline-testable. The engine NEVER invents data — anything not found is
reported as missing, not guessed. Title and contact extraction are deliberately
conservative; the human verifies.

This module produces a research brief. It does NOT write or send email.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# --------------------------------------------------------------------------- #
# Geo detection
# --------------------------------------------------------------------------- #
# Each entry: canonical -> list of (pattern, case_sensitive)
_GEO_PATTERNS: dict[str, list[tuple[str, bool]]] = {
    "United States": [(r"\bUnited States\b", False), (r"\bU\.S\.A?\.?\b", True), (r"\bUSA?\b", True)],
    "United Kingdom": [(r"\bUnited Kingdom\b", False), (r"\bUK\b", True)],
    "Canada": [(r"\bCanada\b", False)],
    "Germany": [(r"\bGermany\b", False)],
    "France": [(r"\bFrance\b", False)],
    "Japan": [(r"\bJapan\b", False)],
    "South Korea": [(r"\bSouth Korea\b", False), (r"\bKorea\b", False)],
    "China": [(r"\bChina\b", False)],
    "Russia": [(r"\bRussia\b", False)],
    "Singapore": [(r"\bSingapore\b", False)],
    "New Zealand": [(r"\bNew Zealand\b", False)],
    "Thailand": [(r"\bThailand\b", False)],
    "Kazakhstan": [(r"\bKazakhstan\b", False)],
    "Australia": [(r"\bAustralia\b", False)],
    "Brazil": [(r"\bBrazil\b", False)],
    "India": [(r"\bIndia\b", False)],
    "Western Europe": [(r"\bWestern Europe\b", False)],
    "CIS": [(r"\bCIS\b", True)],
}

# canonical geo names lowercased, for title filtering
_GEO_NAMES_LOWER = {g.lower() for g in _GEO_PATTERNS}


def detect_geos(text: str) -> list[tuple[str, int]]:
    """Return [(geo, mention_count), ...] sorted by count desc, then name."""
    counts: dict[str, int] = {}
    for canonical, pats in _GEO_PATTERNS.items():
        total = 0
        for pat, cs in pats:
            flags = 0 if cs else re.IGNORECASE
            total += len(re.findall(pat, text, flags))
        if total:
            counts[canonical] = total
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


# --------------------------------------------------------------------------- #
# Ad formats
# --------------------------------------------------------------------------- #
_FORMAT_PATTERNS = {
    "banner": r"\bbanner\b",
    "interstitial": r"\binterstitial\b",
    "rewarded": r"\brewarded\b",
    "intrinsic": r"\bintrinsic\b",
    "playable": r"\bplayable\b",
    "native": r"\bnative\b",
    "in-game": r"\bin-?game\b",
}


def detect_formats(text: str) -> list[str]:
    found = [name for name, pat in _FORMAT_PATTERNS.items()
             if re.search(pat, text, re.IGNORECASE)]
    return sorted(found)


# --------------------------------------------------------------------------- #
# Monetization model
# --------------------------------------------------------------------------- #
_IAP_SIGNALS = [
    "in-app purchase", "in-app purchases", "in app purchase",
    "iap", "in-app revenue", "in-app store",
]
_ADS_SIGNALS = [
    "in-app ads", "in-game ads", "in-game advertising", "in-game advertisement",
    "ad monetization", "ad monetisation", "ad revenue", "rewarded",
    "interstitial", "hybrid monetization", "hybrid monetisation",
    "ads alongside", "advertising solution",
]


def _matched(signals: list[str], low: str) -> list[str]:
    out = []
    for s in signals:
        if s in low and s not in out:
            out.append(s)
    return out


def detect_monetization(text: str) -> tuple[str, list[str]]:
    """Return (label, evidence_phrases). Label in
    {Hybrid (IAP + ads), Ads, IAP-led, Unknown}."""
    low = text.lower()
    iap = _matched(_IAP_SIGNALS, low)
    ads = _matched(_ADS_SIGNALS, low)
    if iap and ads:
        label = "Hybrid (IAP + ads)"
    elif ads:
        label = "Ads"
    elif iap:
        label = "IAP-led"
    else:
        label = "Unknown"
    return label, (iap + ads)


# --------------------------------------------------------------------------- #
# Candidate titles (trigger-anchored, conservative)
# --------------------------------------------------------------------------- #
_TITLE_TRIGGERS = [
    "known for", "best known for", "such as", "including", "games like",
    "titles include", "title include", "creator of", "developers of",
    "developer of", "published", "famous for", "portfolio includes",
    "games include",
]
# split a list segment into chunks on comma / 'and' / '&'
_LIST_SPLIT = re.compile(r"\s*,\s*|\s+and\s+|\s*&\s*", re.IGNORECASE)
_SENT_END = re.compile(r"[.\n;]")
# words that should never be treated as (the start of) a title
_TITLE_STOP = {"the", "its", "their", "they", "it", "this", "these", "a", "an",
               "and", "or", "for", "with", "in", "on", "of", "to"}


def _leading_titlecase_run(chunk: str) -> str:
    """Take the leading run of capitalized tokens (allowing &, :, digits)."""
    tokens = chunk.split()
    run = []
    for tok in tokens:
        core = tok.strip(":,.")
        if not core:
            break
        if core in ("&", ":"):
            run.append(core)
            continue
        if core[0].isupper() or core[0].isdigit():
            run.append(core)
        else:
            break
    return " ".join(run).strip(" :&")


def detect_titles(text: str, publisher: str = "") -> list[str]:
    low = text.lower()
    pub_low = publisher.strip().lower()
    titles: list[str] = []
    seen: set[str] = set()

    for trig in _TITLE_TRIGGERS:
        start = 0
        while True:
            i = low.find(trig, start)
            if i == -1:
                break
            start = i + len(trig)
            # segment from end of trigger to next sentence terminator
            seg = text[start:]
            m = _SENT_END.search(seg)
            seg = seg[: m.start()] if m else seg
            for chunk in _LIST_SPLIT.split(seg):
                chunk = chunk.strip()
                # a "comma + and" list leaves chunks like "and Ravenhill"
                chunk = re.sub(r"^(and|&)\s+", "", chunk, flags=re.IGNORECASE)
                if not chunk:
                    continue
                title = _leading_titlecase_run(chunk)
                if not title:
                    continue
                tl = title.lower()
                if (
                    len(title) < 2
                    or len(title) > 40
                    or tl in _TITLE_STOP
                    or tl in _GEO_NAMES_LOWER
                    or tl == pub_low
                    or tl in seen
                ):
                    continue
                seen.add(tl)
                titles.append(title)
    return titles


# --------------------------------------------------------------------------- #
# Contacts
# --------------------------------------------------------------------------- #
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_GENERIC_LOCALPARTS = {"info", "contact", "support", "hello", "press",
                       "admin", "sales", "help", "team", "office"}


def detect_contacts(text: str) -> list[tuple[str, bool]]:
    """Return [(email, is_generic), ...] deduped, order preserved."""
    out: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for m in _EMAIL_RE.finditer(text):
        email = m.group(0).rstrip(".")
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        local = key.split("@", 1)[0]
        out.append((email, local in _GENERIC_LOCALPARTS))
    return out


# --------------------------------------------------------------------------- #
# Brief assembly
# --------------------------------------------------------------------------- #
@dataclass
class Brief:
    publisher: str
    titles: list[str] = field(default_factory=list)
    monetization: str = "Unknown"
    monetization_evidence: list[str] = field(default_factory=list)
    geos: list[tuple[str, int]] = field(default_factory=list)
    formats: list[str] = field(default_factory=list)
    contacts: list[tuple[str, bool]] = field(default_factory=list)
    sources_used: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [f"# Research brief — {self.publisher}", ""]
        lines.append("## Candidate titles (verify)")
        lines.append(", ".join(self.titles) if self.titles else "_none detected_")
        lines.append("")
        lines.append("## Monetization")
        lines.append(self.monetization)
        if self.monetization_evidence:
            lines.append(f"_evidence: {', '.join(self.monetization_evidence)}_")
        lines.append("")
        lines.append("## Geo weighting (by mention count)")
        if self.geos:
            for g, c in self.geos:
                lines.append(f"- {g} ({c})")
        else:
            lines.append("_none detected_")
        lines.append("")
        lines.append("## Ad formats referenced")
        lines.append(", ".join(self.formats) if self.formats else "_none detected_")
        lines.append("")
        lines.append("## Contact emails found (public pages)")
        if self.contacts:
            for e, generic in self.contacts:
                tag = " — generic, find the real owner" if generic else ""
                lines.append(f"- {e}{tag}")
        else:
            lines.append("_none found_")
        lines.append("")
        lines.append("## Sources used")
        for s in self.sources_used:
            lines.append(f"- {s}")
        if not self.sources_used:
            lines.append("_none_")
        lines.append("")
        if self.warnings:
            lines.append("## ⚠️ Verify before acting")
            for w in self.warnings:
                lines.append(f"- {w}")
        return "\n".join(lines)


def build_brief(publisher: str, sources: dict[str, str]) -> Brief:
    """sources: {url: extracted_plain_text}. Combines all, extracts, warns."""
    corpus = "\n".join(sources.values())
    label, mon_ev = detect_monetization(corpus)
    geos = detect_geos(corpus)
    formats = detect_formats(corpus)
    titles = detect_titles(corpus, publisher)
    contacts = detect_contacts(corpus)
    used = [u for u, t in sources.items() if t and t.strip()]

    warnings: list[str] = []
    if not used:
        warnings.append("No sources returned text — brief is empty. Add public URLs manually.")
    if label == "Unknown":
        warnings.append("Monetization model not detected — verify manually before pitching.")
    if label == "IAP-led" and not formats:
        warnings.append("Looks IAP-only with no ad formats found — an ad-buy pitch may not fit. "
                        "Confirm they run ads at all before reaching out.")
    if not geos:
        warnings.append("No geo signal found — don't claim a geo fit you can't support.")
    if not titles:
        warnings.append("No candidate titles detected — add a specific title manually to anchor the email.")
    # always-on guidance
    warnings.append("Contacts above are scraped from public pages and are usually generic. "
                    "Find the actual ad-monetization / platform-partnerships owner; don't pitch the CEO or info@.")
    warnings.append("Extracted from public sources — may be partial or stale. Verify before acting.")

    return Brief(
        publisher=publisher,
        titles=titles,
        monetization=label,
        monetization_evidence=mon_ev,
        geos=geos,
        formats=formats,
        contacts=contacts,
        sources_used=used,
        warnings=warnings,
    )
