"""
stage3_body_cleaner.py
========================
Clean up email body text before send.

Solves audit error 3.9 (whitespace/orphan punctuation from empty variable
substitutions). Even with required-variable validation, edges can slip through:

  Template: "Hi <<FIRST_NAME>>, here's the offer."
  If FIRST_NAME falls back to ""  → "Hi , here's the offer."
                                       ^^^ orphan comma

We don't try to fix every edge case — just the common ones that look obviously
broken to a recipient.

Pure functions, no I/O, easily testable.
"""

import re


# ============================================================================
# REGEX PATTERNS
# ============================================================================

# Three or more consecutive newlines → collapse to two (one blank line)
EXCESS_NEWLINES = re.compile(r"\n{3,}")

# Trailing whitespace on a line (multi-line mode)
TRAILING_WS = re.compile(r"[ \t]+$", flags=re.MULTILINE)

# Multiple consecutive spaces (within a line)
EXCESS_SPACES = re.compile(r" {2,}")

# Orphan punctuation patterns — punctuation with whitespace before it
# Examples:  "Hi , there"  →  "Hi, there"
#            "Hello ."      →  "Hello."
#            "Hi ."         →  "Hi."
ORPHAN_PUNCT = re.compile(r"\s+([,.;:!?])")

# Empty parenthetical: "()" or "( )" — collapse to nothing
EMPTY_PARENS = re.compile(r"\(\s*\)")

# Double comma: ",," → ","
DOUBLE_COMMA = re.compile(r",\s*,")


# ============================================================================
# CLEANING FUNCTIONS
# ============================================================================

def collapse_excess_newlines(text: str) -> str:
    """3+ newlines → 2 newlines (preserves paragraph breaks)."""
    return EXCESS_NEWLINES.sub("\n\n", text)


def strip_trailing_whitespace(text: str) -> str:
    """Remove trailing whitespace on each line."""
    return TRAILING_WS.sub("", text)


def collapse_excess_spaces(text: str) -> str:
    """Multiple consecutive spaces → single space (within lines)."""
    # Only collapse within lines (not affecting newlines)
    lines = text.split("\n")
    cleaned_lines = [EXCESS_SPACES.sub(" ", line) for line in lines]
    return "\n".join(cleaned_lines)


def fix_orphan_punctuation(text: str) -> str:
    """
    Remove whitespace before punctuation.

    Note: this runs AFTER variable substitution, so empty <<FIRST_NAME>>
    leaving "Hi ," gets cleaned to "Hi,".
    """
    return ORPHAN_PUNCT.sub(r"\1", text)


def remove_empty_parens(text: str) -> str:
    """Remove empty () that result from optional content all being filtered."""
    return EMPTY_PARENS.sub("", text)


def collapse_double_commas(text: str) -> str:
    """',  ,' → ',' (common after empty substitution between commas)."""
    return DOUBLE_COMMA.sub(",", text)


def strip_leading_trailing(text: str) -> str:
    """Strip leading/trailing whitespace (including newlines) from full text."""
    return text.strip()


# ============================================================================
# MASTER CLEANER
# ============================================================================

def clean_email_body(text: str) -> str:
    """
    Apply all cleaning rules in order. Use this as the single entry point.

    Order matters:
      1. Strip trailing whitespace on lines first (so punctuation regex sees clean lines)
      2. Fix orphan punctuation
      3. Collapse double commas
      4. Remove empty parens
      5. Collapse multiple spaces (after orphan-punct creates them)
      6. Collapse excess newlines
      7. Strip leading/trailing
    """
    if not text:
        return ""

    text = strip_trailing_whitespace(text)
    text = fix_orphan_punctuation(text)
    text = collapse_double_commas(text)
    text = remove_empty_parens(text)
    text = collapse_excess_spaces(text)
    text = collapse_excess_newlines(text)
    text = strip_leading_trailing(text)

    return text


def clean_subject_line(text: str) -> str:
    """
    Subject-specific cleaning.

    Subjects shouldn't have newlines, multiple spaces, or orphan punctuation.
    """
    if not text:
        return ""

    # Subjects should be single-line
    text = text.replace("\n", " ").replace("\r", " ")
    text = collapse_excess_spaces(text)
    text = fix_orphan_punctuation(text)
    text = collapse_double_commas(text)
    text = strip_leading_trailing(text)

    # Cap at 998 chars (RFC 5322 hard limit on single-line headers)
    if len(text) > 998:
        text = text[:995] + "..."

    return text
