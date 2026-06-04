"""
Outreach email linter — pure rule engine (stdlib only, no Streamlit, no network).

Catches the things that get cold-but-honest outreach killed or that signal a
careless blast:
  - fake urgency / manufactured scarcity
  - unfilled placeholders or generic greetings (no real personalization)
  - missing opt-out
  - CPM figures in the prose that contradict the rate-card table

Design notes / known limits are documented inline. The CPM checks are
deliberately conservative: when a sentence is ambiguous (e.g. names two
formats) the engine skips rather than risk a false positive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

SEVERITY_ORDER = {"error": 0, "warn": 1, "info": 2}


@dataclass
class Finding:
    rule: str
    severity: str  # "error" | "warn" | "info"
    message: str
    evidence: Optional[str] = None  # the offending snippet, if any

    def __post_init__(self):
        if self.severity not in SEVERITY_ORDER:
            raise ValueError(f"bad severity: {self.severity!r}")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_DOLLAR_RE = re.compile(r"\$\s?(\d+(?:\.\d+)?)")
FORMAT_KEYWORDS = ("banner", "interstitial", "rewarded")


def _is_table_line(line: str) -> bool:
    """A markdown table row or separator starts with a pipe after stripping."""
    return line.strip().startswith("|")


def split_table_and_prose(text: str) -> tuple[list[str], list[str]]:
    """Return (table_lines, prose_lines). Table = contiguous pipe lines."""
    table_lines, prose_lines = [], []
    for line in text.splitlines():
        if _is_table_line(line):
            table_lines.append(line)
        else:
            prose_lines.append(line)
    return table_lines, prose_lines


def _dollar_amounts(s: str) -> list[tuple[float, int]]:
    """All dollar amounts in s as (value, char_index_of_match_start)."""
    out = []
    for m in _DOLLAR_RE.finditer(s):
        out.append((float(m.group(1)), m.start()))
    return out


def parse_rate_card(table_lines: list[str]) -> dict[str, dict[str, float]]:
    """
    Parse a markdown rate-card table into {format_lower: {"floor":x,"ceiling":y}}.

    Locates the 'floor' and 'ceiling' columns by header name so column order
    doesn't matter. Rows whose first cell isn't a known format are ignored.
    Returns {} if no usable header is found.
    """
    if not table_lines:
        return {}

    def cells(line: str) -> list[str]:
        # strip leading/trailing pipe, split, trim
        parts = line.strip().strip("|").split("|")
        return [p.strip() for p in parts]

    # find header row: one that contains 'format' and at least one of floor/ceiling
    header_idx = None
    header_cells: list[str] = []
    for i, line in enumerate(table_lines):
        c = [x.lower() for x in cells(line)]
        if "format" in c and ("floor" in c or "ceiling" in c):
            header_idx = i
            header_cells = c
            break
    if header_idx is None:
        return {}

    try:
        fmt_col = header_cells.index("format")
    except ValueError:
        return {}
    floor_col = header_cells.index("floor") if "floor" in header_cells else None
    ceil_col = header_cells.index("ceiling") if "ceiling" in header_cells else None

    rate_card: dict[str, dict[str, float]] = {}
    for line in table_lines[header_idx + 1:]:
        c = cells(line)
        # skip separator rows like |---|---|
        if all(set(cell) <= set("-: ") for cell in c if cell != ""):
            continue
        if fmt_col >= len(c):
            continue
        fmt = c[fmt_col].strip().lower()
        if fmt not in FORMAT_KEYWORDS:
            continue
        entry: dict[str, float] = {}
        if floor_col is not None and floor_col < len(c):
            amts = _dollar_amounts(c[floor_col])
            if amts:
                entry["floor"] = amts[0][0]
        if ceil_col is not None and ceil_col < len(c):
            amts = _dollar_amounts(c[ceil_col])
            if amts:
                entry["ceiling"] = amts[0][0]
        if entry:
            rate_card[fmt] = entry
    return rate_card


def _sentences(prose: str) -> list[str]:
    """Rough sentence/line split good enough for proximity heuristics."""
    # split on sentence punctuation and newlines, keep non-empty
    raw = re.split(r"[.!?\n]+", prose)
    return [s.strip() for s in raw if s.strip()]


def _nearest_amount(amounts: list[tuple[float, int]], anchor_idx: int) -> Optional[float]:
    """Value of the dollar amount whose match-start is closest to anchor_idx."""
    if not amounts:
        return None
    best = min(amounts, key=lambda a: abs(a[1] - anchor_idx))
    return best[0]


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #

# Phrase -> human label. Matched case-insensitively as substrings/regex.
_URGENCY_PATTERNS: list[tuple[str, str]] = [
    (r"confirm by\b", "deadline pressure ('confirm by ...')"),
    (r"\bby (end of week|eod|friday|cob)\b", "artificial deadline"),
    (r"lock (the|this|in the)? ?slot", "scarcity ('lock the slot')"),
    (r"\bi'?m prioriti[sz]ing\b", "fake prioritization"),
    (r"\bneed to (confirm|fill)\b", "urgency ('need to confirm/fill')"),
    (r"\bquick yes/?no\b", "pressure CTA ('quick yes/no')"),
    (r"\bonly (have )?one\b", "scarcity ('only one ...')"),
    (r"\b(act now|limited time|expires|don'?t miss)\b", "hard-sell urgency"),
    (r"\bbefore (it'?s|they'?re) gone\b", "scarcity"),
]

_GENERIC_GREETINGS = [
    "hi there",
    "dear sir",
    "dear madam",
    "sir/madam",
    "to whom it may concern",
    "dear team",
    "hello team",
    "hi team",
    "dear valued partner",
    "dear partner",
    "dear publisher",
]

# Unfilled placeholder tokens.
_PLACEHOLDER_RE = re.compile(r"(\[[A-Za-z0-9 _/]+\]|<[A-Za-z0-9 _/]+>|\{[^}]*\})")

_OPTOUT_SIGNALS = [
    "won't follow up",
    "wont follow up",
    "no follow up",
    "no follow-up",
    "not a fit",
    "no hard feelings",
    "just say so",
    "tell me and i'll",
    "let me know and i'll",
    "i'll leave it",
    "leave it there",
    "not of interest",
    "unsubscribe",
    "opt out",
    "opt-out",
    "reply stop",
    "happy to stop",
]


def check_urgency(prose: str) -> list[Finding]:
    findings: list[Finding] = []
    low = prose.lower()
    seen: set[str] = set()
    for pat, label in _URGENCY_PATTERNS:
        m = re.search(pat, low)
        if m and label not in seen:
            seen.add(label)
            findings.append(
                Finding(
                    rule="fake_urgency",
                    severity="warn",
                    message=f"Manufactured urgency/scarcity: {label}.",
                    evidence=m.group(0),
                )
            )
    return findings


def check_greeting(text: str) -> list[Finding]:
    findings: list[Finding] = []
    # consider the first 3 non-empty lines as the greeting zone
    lines = [l for l in text.splitlines() if l.strip()]
    zone = " ".join(lines[:3]).lower() if lines else ""

    if not zone:
        findings.append(
            Finding("greeting", "warn", "No greeting found.", None)
        )
        return findings

    # unfilled placeholder anywhere in greeting zone
    ph = _PLACEHOLDER_RE.search(" ".join(lines[:3]))
    if ph:
        findings.append(
            Finding(
                "greeting",
                "error",
                "Unfilled placeholder in greeting — fill before sending.",
                ph.group(0),
            )
        )

    for g in _GENERIC_GREETINGS:
        if g in zone:
            findings.append(
                Finding(
                    "greeting",
                    "warn",
                    f"Generic greeting ('{g}') — use a real contact name.",
                    g,
                )
            )
            break
    return findings


def check_optout(prose: str) -> list[Finding]:
    low = prose.lower()
    if any(sig in low for sig in _OPTOUT_SIGNALS):
        return []
    return [
        Finding(
            "opt_out",
            "warn",
            "No opt-out / graceful exit found — add a line letting them decline.",
            None,
        )
    ]


def check_cpm_consistency(text: str) -> list[Finding]:
    """
    Two conservative checks against the rate-card table:
      C1 floor sanity: a prose figure labelled 'floor' must equal SOME table floor.
      C2 format bounds: in a sentence naming exactly one format, a non-floor
         dollar (offer/cpm) must sit within [floor, ceiling] for that format.
    """
    findings: list[Finding] = []
    table_lines, prose_lines = split_table_and_prose(text)
    rate_card = parse_rate_card(table_lines)
    if not rate_card:
        return findings  # nothing to check against

    table_floors = {v["floor"] for v in rate_card.values() if "floor" in v}
    prose = "\n".join(prose_lines)

    for sent in _sentences(prose):
        low = sent.lower()
        amounts = _dollar_amounts(sent)
        if not amounts:
            continue

        # --- C1: floor sanity ---
        floor_pos = low.find("floor")
        floor_claim = None
        if floor_pos != -1:
            floor_claim = _nearest_amount(amounts, floor_pos)
            if floor_claim is not None and table_floors and floor_claim not in table_floors:
                findings.append(
                    Finding(
                        "cpm_consistency",
                        "error",
                        f"Stated floor ${floor_claim:g} matches no rate-card floor "
                        f"({', '.join('$'+format(f,'g') for f in sorted(table_floors))}).",
                        sent.strip(),
                    )
                )

        # --- C2: format bounds (only when exactly one format named) ---
        named = [f for f in FORMAT_KEYWORDS if f in low]
        if len(named) == 1:
            fmt = named[0]
            bounds = rate_card.get(fmt, {})
            # offer = the dollar nearest cpm/offer/pay words, else any non-floor dollar
            anchor = -1
            for kw in ("cpm", "offer", "pay", "paying", "come in at"):
                p = low.find(kw)
                if p != -1:
                    anchor = p
                    break
            offer = _nearest_amount(amounts, anchor) if anchor != -1 else None
            # don't treat the floor figure itself as the offer
            if offer is not None and offer == floor_claim and len(amounts) > 1:
                others = [a for a in amounts if a[0] != floor_claim]
                offer = others[0][0] if others else None

            if offer is not None:
                ceil = bounds.get("ceiling")
                flr = bounds.get("floor")
                if ceil is not None and offer > ceil:
                    findings.append(
                        Finding(
                            "cpm_consistency",
                            "error",
                            f"Offer ${offer:g} exceeds {fmt} ceiling ${ceil:g}.",
                            sent.strip(),
                        )
                    )
                elif flr is not None and offer < flr:
                    findings.append(
                        Finding(
                            "cpm_consistency",
                            "warn",
                            f"Offer ${offer:g} is below {fmt} floor ${flr:g} "
                            f"— you're underbidding your own card.",
                            sent.strip(),
                        )
                    )
    return findings


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def lint(text: str) -> list[Finding]:
    """Run all rules and return findings sorted by severity (errors first)."""
    findings: list[Finding] = []
    findings += check_urgency(text)
    findings += check_greeting(text)
    findings += check_optout(text)
    findings += check_cpm_consistency(text)
    findings.sort(key=lambda f: SEVERITY_ORDER[f.severity])
    return findings
