"""
stage1_validation.py
====================
Pure validation functions for campaign input.

Design principles:
  - All functions are pure (no I/O, no side effects) → easily unit-testable
  - Return (is_valid: bool, errors: list[str]) tuples
  - Errors are user-facing strings — avoid jargon
  - Normalization happens HERE, at input time, so downstream code never
    has to worry about case sensitivity / whitespace

Corrections applied from audit:
  - CPM units are USD (not cents), range $0.10–$50.00
  - Flight-start "future only" rule applies ONLY to Outreach type
  - Email validation added (RFC 5322 + role-based + typo detection)
  - Flight duration capped at 180 days
  - All string fields normalized (lowercase, stripped) for comparison
"""

import re
from datetime import date, datetime, timedelta
from typing import Any


# ============================================================================
# CONSTANTS — single source of truth for enums and ranges
# ============================================================================

VALID_VERTICALS = [
    "Gaming",
    "Finance",
    "Health",
    "Shopping",
    "Entertainment",
    "Utility",
    "Social",
    "Education",
    "Travel",
    "Other",
]

VALID_CAMPAIGN_TYPES = ["Outreach", "FollowUp", "Brief", "WinBack"]

VALID_PRIORITY_TIERS = ["High", "Medium", "Low"]

VALID_SEGMENTS = ["All", "Tier1", "Tier2", "DirectOnly"]

VALID_VARIANT_STRATEGIES = ["RandomRotate", "Sequential", "TopPerformer"]

CPM_MIN_USD = 0.10
CPM_MAX_USD = 50.00

FLIGHT_MAX_DURATION_DAYS = 180

# Role-based email prefixes that typically have low deliverability
ROLE_EMAIL_PREFIXES = [
    "admin@",
    "info@",
    "support@",
    "noreply@",
    "no-reply@",
    "donotreply@",
    "do-not-reply@",
    "mailer-daemon@",
    "postmaster@",
    "abuse@",
]

# Common TLD typos
SUSPICIOUS_TLDS = [
    ".con",   # .com typo
    ".cmo",   # .com typo
    ".con",
    ".ocm",
    ".nte",   # .net typo
    ".ogr",   # .org typo
]


# ============================================================================
# NORMALIZATION HELPERS
# ============================================================================

def normalize_string(s: Any) -> str:
    """Lowercase + strip whitespace. Returns empty string on None/empty."""
    if s is None:
        return ""
    return str(s).strip().lower()


def normalize_email(email: Any) -> str:
    """Normalize email: lowercase, strip, handle plus-addressing."""
    if not email:
        return ""
    email = str(email).strip().lower()
    return email


def normalize_brand(brand: Any) -> str:
    """
    Normalize brand name for comparison:
      - Strip whitespace
      - Lowercase
      - Remove common suffixes (Inc, LLC, Corp) so 'Nike Inc' == 'Nike'
    """
    if not brand:
        return ""
    s = str(brand).strip().lower()
    for suffix in [" inc", " inc.", " llc", " corp", " corp.", " ltd", " ltd."]:
        if s.endswith(suffix):
            s = s[: -len(suffix)].strip()
    return s


# ============================================================================
# EMAIL VALIDATION
# ============================================================================

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")


def validate_email(email: str) -> tuple[bool, str | None]:
    """
    Returns (is_valid, error_message_or_None).

    Checks:
      1. RFC 5322 basic format
      2. Suspicious TLD typos
      3. Role-based prefixes (warns, doesn't block)
    """
    if not email:
        return False, "Email is required"

    email = email.strip().lower()

    if not EMAIL_REGEX.match(email):
        return False, "Invalid email format"

    # Check for suspicious TLDs
    for bad_tld in SUSPICIOUS_TLDS:
        if email.endswith(bad_tld):
            return False, f"Suspicious TLD '{bad_tld}' — likely typo of .com/.net/.org"

    # Role-based check — warn but allow
    for prefix in ROLE_EMAIL_PREFIXES:
        if email.startswith(prefix):
            return False, f"Role-based email ({prefix}*) — low deliverability, use a personal address"

    return True, None


# ============================================================================
# FIELD-LEVEL VALIDATORS
# ============================================================================

def validate_brand(brand: Any) -> list[str]:
    errors = []
    if not brand or not str(brand).strip():
        errors.append("Brand name is required")
    elif len(str(brand)) > 50:
        errors.append("Brand name max 50 characters")
    return errors


def validate_app_name(app: Any) -> list[str]:
    errors = []
    if not app or not str(app).strip():
        errors.append("App name is required")
    elif len(str(app)) > 100:
        errors.append("App name max 100 characters")
    return errors


def validate_vertical(vertical: Any) -> list[str]:
    if vertical not in VALID_VERTICALS:
        return [f"Vertical must be one of: {', '.join(VALID_VERTICALS)}"]
    return []


def validate_campaign_type(ctype: Any) -> list[str]:
    if ctype not in VALID_CAMPAIGN_TYPES:
        return [f"Campaign type must be one of: {', '.join(VALID_CAMPAIGN_TYPES)}"]
    return []


def validate_cpm(cpm_floor: Any, cpm_offer: Any) -> list[str]:
    """Validate both CPM values together — they're related."""
    errors = []

    # Floor checks
    try:
        floor = float(cpm_floor)
    except (TypeError, ValueError):
        return ["CPM floor must be a number"]

    try:
        offer = float(cpm_offer)
    except (TypeError, ValueError):
        return ["CPM offer must be a number"]

    if not (CPM_MIN_USD <= floor <= CPM_MAX_USD):
        errors.append(f"CPM floor must be ${CPM_MIN_USD:.2f}–${CPM_MAX_USD:.2f}")

    if not (CPM_MIN_USD <= offer <= CPM_MAX_USD):
        errors.append(f"CPM offer must be ${CPM_MIN_USD:.2f}–${CPM_MAX_USD:.2f}")

    if offer < floor:
        errors.append(f"Offer CPM (${offer:.2f}) must be >= floor CPM (${floor:.2f})")

    return errors


def validate_flight_dates(
    flight_start: Any,
    flight_end: Any,
    campaign_type: str,
) -> list[str]:
    """
    Validate flight dates with campaign-type-aware rules.

    - All types: end > start, duration <= 180 days
    - Outreach only: start >= today (others can reference past flights)
    """
    errors = []

    # Coerce to date if datetime
    if isinstance(flight_start, datetime):
        flight_start = flight_start.date()
    if isinstance(flight_end, datetime):
        flight_end = flight_end.date()

    if not isinstance(flight_start, date):
        return ["Flight start date is required"]
    if not isinstance(flight_end, date):
        return ["Flight end date is required"]

    today = datetime.now().date()

    # End must be after start
    if flight_end <= flight_start:
        errors.append("Flight end date must be after start date")
        return errors  # other checks moot

    # Duration cap
    duration = (flight_end - flight_start).days
    if duration > FLIGHT_MAX_DURATION_DAYS:
        errors.append(
            f"Flight duration is {duration} days — max {FLIGHT_MAX_DURATION_DAYS} days "
            f"(likely a data entry error)"
        )

    # Outreach-specific: must not start in the past
    if campaign_type == "Outreach" and flight_start < today:
        errors.append(
            f"Outreach campaigns must have a future start date "
            f"(today is {today}, you entered {flight_start})"
        )

    return errors


# ============================================================================
# MAIN VALIDATION ENTRYPOINT
# ============================================================================

def validate_campaign_input(data: dict) -> tuple[bool, list[str]]:
    """
    Validate all campaign fields. Returns (is_valid, errors).

    Expected keys in `data`:
      - brand: str
      - app_name: str
      - vertical: str (must be in VALID_VERTICALS)
      - campaign_type: str (must be in VALID_CAMPAIGN_TYPES)
      - cpm_floor: float
      - cpm_offer: float
      - flight_start: date
      - flight_end: date
      - recipient_email: str
      - priority_tier: str (optional, in VALID_PRIORITY_TIERS)
      - publisher_segment: str (optional, in VALID_SEGMENTS)
      - variant_strategy: str (optional, in VALID_VARIANT_STRATEGIES)

    Note: 'notes' and metadata like created_at/created_by are NOT validated
    here — they're set by the system, not the user.
    """
    errors: list[str] = []

    errors.extend(validate_brand(data.get("brand")))
    errors.extend(validate_app_name(data.get("app_name")))
    errors.extend(validate_vertical(data.get("vertical")))
    errors.extend(validate_campaign_type(data.get("campaign_type")))
    errors.extend(validate_cpm(data.get("cpm_floor"), data.get("cpm_offer")))
    errors.extend(
        validate_flight_dates(
            data.get("flight_start"),
            data.get("flight_end"),
            data.get("campaign_type", ""),
        )
    )

    # Recipient email — critical for deliverability
    is_valid_email, email_err = validate_email(data.get("recipient_email", ""))
    if not is_valid_email:
        errors.append(f"Recipient email: {email_err}")

    # Optional enums (only validate if provided)
    if data.get("priority_tier") and data["priority_tier"] not in VALID_PRIORITY_TIERS:
        errors.append(f"Priority tier must be one of: {', '.join(VALID_PRIORITY_TIERS)}")

    if data.get("publisher_segment") and data["publisher_segment"] not in VALID_SEGMENTS:
        errors.append(f"Segment must be one of: {', '.join(VALID_SEGMENTS)}")

    if data.get("variant_strategy") and data["variant_strategy"] not in VALID_VARIANT_STRATEGIES:
        errors.append(f"Variant strategy must be one of: {', '.join(VALID_VARIANT_STRATEGIES)}")

    return len(errors) == 0, errors


def normalize_campaign_input(data: dict) -> dict:
    """
    Return a copy of `data` with string fields normalized for storage.
    Call this AFTER validation succeeds, BEFORE writing to Sheets.

    This ensures all data in Sheets is comparable downstream (dedup, history).
    """
    normalized = data.copy()

    if "brand" in normalized:
        # Store the display version too — normalized is just for comparison
        normalized["brand_normalized"] = normalize_brand(normalized["brand"])
        normalized["brand"] = str(normalized["brand"]).strip()

    if "app_name" in normalized:
        normalized["app_name"] = str(normalized["app_name"]).strip()

    if "recipient_email" in normalized:
        normalized["recipient_email"] = normalize_email(normalized["recipient_email"])

    # Convert dates to ISO strings for Sheets storage
    for date_field in ("flight_start", "flight_end"):
        val = normalized.get(date_field)
        if isinstance(val, (date, datetime)):
            normalized[date_field] = val.isoformat()[:10]  # YYYY-MM-DD

    return normalized
