"""
stage7_reply_classifier.py
============================
Classify an inbound email as: genuine reply, auto-reply, bounce, or unsubscribe.

Solves audit errors:
  - 7.3: Auto-replies/OOO/bounces shouldn't count as genuine engagement
  - 7.12: Patterns are locale-dependent and extensible

Pure functions — no Gmail I/O, no Sheets. Easily testable. The same
classification logic is mirrored in apps_script_v4.gs (JS) for the inbox scan;
this Python version is the source of truth + the test target.

Classification precedence (first match wins):
  1. bounce       — delivery failure notifications
  2. unsubscribe  — opt-out requests (important: suppress these!)
  3. auto_reply   — OOO / vacation / automatic acknowledgements
  4. genuine      — anything else (a real human reply)
"""

import re
from typing import Literal


ReplyStatus = Literal["genuine", "auto_reply", "bounce", "unsubscribe"]


# ============================================================================
# PATTERN LISTS (extensible — add locales/variants here)
# ============================================================================

# Sender addresses that indicate a bounce / delivery failure
BOUNCE_SENDER_PATTERNS = [
    "mailer-daemon@",
    "postmaster@",
    "mail-daemon@",
    "no-reply-bounces@",
    "bounce@",
]

# Subject/body substrings indicating a bounce
BOUNCE_CONTENT_PATTERNS = [
    "delivery status notification",
    "undelivered mail",
    "delivery has failed",
    "could not be delivered",
    "delivery failure",
    "returned mail",
    "mail delivery failed",
    "address not found",
    "recipient address rejected",
    "550 5.1.1",
]

# Subject/body substrings indicating an auto-reply / OOO (multi-locale)
AUTO_REPLY_PATTERNS = [
    "out of office",
    "out of the office",
    "automatic reply",
    "auto-reply",
    "autoreply",
    "away from my desk",
    "on vacation",
    "on holiday",
    "annual leave",
    "i am currently away",
    "i'm currently away",
    "currently out",
    "abwesenheit",          # German
    "abwesenheitsnotiz",
    "réponse automatique",  # French
    "absence du bureau",
    "fuori sede",           # Italian
    "respuesta automática",  # Spanish
    "ausência",             # Portuguese
    "no longer with",       # left company
    "has left the company",
]

# Subject/body substrings indicating an unsubscribe / opt-out request
UNSUBSCRIBE_PATTERNS = [
    "unsubscribe",
    "remove me",
    "take me off",
    "opt out",
    "opt-out",
    "stop emailing",
    "stop contacting",
    "do not contact",
    "don't contact",
    "no longer wish to receive",
    "please remove",
    "remove from your list",
]


# ============================================================================
# HELPERS
# ============================================================================

def _normalize(text: str) -> str:
    """Lowercase + collapse whitespace for pattern matching."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", str(text).strip().lower())


def _any_pattern_in(text: str, patterns: list[str]) -> bool:
    """True if any pattern is a substring of the normalized text."""
    norm = _normalize(text)
    return any(p in norm for p in patterns)


def _sender_matches(from_email: str, patterns: list[str]) -> bool:
    """True if the sender email starts with / contains any pattern."""
    norm = _normalize(from_email)
    return any(p in norm for p in patterns)


# ============================================================================
# MAIN CLASSIFIER
# ============================================================================

def classify_reply(
    from_email: str,
    subject: str,
    body_snippet: str = "",
) -> ReplyStatus:
    """
    Classify an inbound email.

    Args:
        from_email: sender address of the inbound message
        subject: subject line of the inbound message
        body_snippet: first chunk of the body (optional but improves accuracy)

    Returns:
        One of: 'bounce', 'unsubscribe', 'auto_reply', 'genuine'

    Precedence: bounce > unsubscribe > auto_reply > genuine.
    """
    combined = f"{subject} {body_snippet}"

    # 1. Bounce — check sender address first (most reliable), then content
    if _sender_matches(from_email, BOUNCE_SENDER_PATTERNS):
        return "bounce"
    if _any_pattern_in(combined, BOUNCE_CONTENT_PATTERNS):
        return "bounce"

    # 2. Unsubscribe — important to catch BEFORE auto_reply, since an OOO
    #    rarely contains "unsubscribe" but an opt-out is high priority
    if _any_pattern_in(combined, UNSUBSCRIBE_PATTERNS):
        return "unsubscribe"

    # 3. Auto-reply / OOO
    if _any_pattern_in(combined, AUTO_REPLY_PATTERNS):
        return "auto_reply"

    # 4. Genuine human reply
    return "genuine"


# ============================================================================
# ENGAGEMENT WEIGHT (audit error 7.14: categorical, not over-engineered)
# ============================================================================

# How each reply status contributes to engagement analytics.
# genuine = strong positive; bounce = negative (bad address); others neutral.
ENGAGEMENT_WEIGHT = {
    "genuine": 1,
    "auto_reply": 0,
    "unsubscribe": 0,   # not "engagement" — but triggers suppression separately
    "bounce": -1,       # signals a bad address
    "none": 0,          # no reply at all
}


def is_positive_engagement(reply_status: str) -> bool:
    """True only for genuine human replies."""
    return reply_status == "genuine"


def should_suppress(reply_status: str) -> bool:
    """
    True if this reply means we should add the recipient to the suppression
    list (audit: respect opt-outs; stop emailing bounced addresses).
    """
    return reply_status in ("unsubscribe", "bounce")
