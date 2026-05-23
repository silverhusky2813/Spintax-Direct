"""
stage2_publishers.py
=====================
Publisher metadata lookup with graceful fallback.

Solves audit errors:
  - 2.17: Reads Publishers data fresh, not from Stage 1 cache
  - 2.18: Surfaces fallback usage visibly (UI gets a warning flag)

Provides:
  - get_publisher(email): returns metadata dict or {} if not found
  - get_publisher_with_fallback(email): returns metadata WITH fallbacks applied
                                         AND a flag indicating fallback was used
  - upsert_publisher(email, **fields): create or update a publisher row

The "hybrid" approach from user's Stage 2 question:
  1. Try Publishers tab lookup
  2. If missing/empty: use VARIABLE_FALLBACKS from stage2_templates
  3. Flag that fallback was used so UI can show a warning

Caching: short TTL (30s) — needs to be fresh but not re-fetched per keystroke.
"""

from datetime import datetime
from typing import Optional

import gspread
import streamlit as st

from stage1_dedup import get_gspread_client
from stage1_validation import normalize_email
from stage2_templates import VARIABLE_FALLBACKS


# Variables that come from the Publishers tab (NOT system/sender variables).
# Only these trigger the publisher_fallback_used flag when empty.
PUBLISHER_VARIABLES = ["FIRST_NAME", "LAST_NAME", "PUBLISHER_NAME"]


# ============================================================================
# CACHED READS
# ============================================================================

@st.cache_data(ttl=30)
def _load_all_publishers() -> dict[str, dict]:
    """
    Load all publishers into a dict keyed by normalized email.
    Cached 30s. Returns empty dict if tab doesn't exist.
    """
    gc = get_gspread_client()
    sh = gc.open_by_key(st.secrets["sheet_id"])

    try:
        ws = sh.worksheet("Publishers")
    except gspread.WorksheetNotFound:
        return {}

    records = ws.get_all_records()

    publishers_by_email = {}
    for r in records:
        email = normalize_email(r.get("publisher_email", ""))
        if email:
            publishers_by_email[email] = r

    return publishers_by_email


# ============================================================================
# LOOKUP API
# ============================================================================

def get_publisher(email: str) -> Optional[dict]:
    """
    Look up publisher metadata by email.

    Returns:
        Dict with publisher fields if found, None if not found.
    """
    normalized = normalize_email(email)
    if not normalized:
        return None

    publishers = _load_all_publishers()
    return publishers.get(normalized)


def get_publisher_with_fallback(email: str) -> tuple[dict, bool, list[str]]:
    """
    Get publisher metadata with fallback values applied for missing fields.

    Returns:
        (publisher_data: dict, fallback_was_used: bool, fields_using_fallback: list)

    Example return when publisher exists:
        ({"first_name": "Alice", "publisher_name": "Acme", ...}, False, [])

    Example return when publisher missing:
        ({"first_name": "there", "publisher_name": "your team", ...}, True,
         ["FIRST_NAME", "PUBLISHER_NAME"])

    The UI should show a warning when fallback_was_used=True.
    """
    publisher = get_publisher(email)
    fallback_used = False
    fallback_fields: list[str] = []

    # Build the result dict
    if publisher is None:
        # Publisher not in tab at all — use all fallbacks
        result = {}
        for var_name in PUBLISHER_VARIABLES:
            field_name = var_name.lower()  # FIRST_NAME → first_name
            result[field_name] = VARIABLE_FALLBACKS.get(var_name, "")
            fallback_fields.append(var_name)
            fallback_used = True
    else:
        # Publisher exists — check each PUBLISHER field, fall back if empty
        result = dict(publisher)  # copy
        for var_name in PUBLISHER_VARIABLES:
            field_name = var_name.lower()
            if field_name not in result or not str(result.get(field_name, "")).strip():
                result[field_name] = VARIABLE_FALLBACKS.get(var_name, "")
                fallback_fields.append(var_name)
                fallback_used = True

    return result, fallback_used, fallback_fields


# ============================================================================
# UPSERT API (write/update)
# ============================================================================

def upsert_publisher(
    email: str,
    first_name: str = "",
    last_name: str = "",
    publisher_name: str = "",
    publisher_tier: str = "Unverified",
    default_geo: str = "",
    notes: str = "",
) -> str:
    """
    Insert a new publisher row OR update an existing one (matched by email).

    Returns:
        'created' if a new row was added, 'updated' if existing row was modified.
    """
    from schema_setup_v2 import PUBLISHERS_SCHEMA

    normalized_email = normalize_email(email)
    if not normalized_email:
        raise ValueError("Email is required and must be valid")

    gc = get_gspread_client()
    sh = gc.open_by_key(st.secrets["sheet_id"])
    ws = sh.worksheet("Publishers")

    now = datetime.now().isoformat()

    # Look for existing row by email (column A)
    existing_cells = ws.findall(normalized_email, in_column=1)

    new_row = {
        "publisher_email": normalized_email,
        "first_name": first_name.strip(),
        "last_name": last_name.strip(),
        "publisher_name": publisher_name.strip(),
        "publisher_tier": publisher_tier,
        "default_geo": default_geo.strip().upper(),
        "notes": notes.strip(),
        "created_at": now,    # may be overwritten below for updates
        "updated_at": now,
    }

    if existing_cells:
        # UPDATE — preserve created_at from existing row
        row_num = existing_cells[0].row
        existing_values = ws.row_values(row_num)
        # Find created_at column index (case-insensitive header match)
        headers = ws.row_values(1)
        try:
            created_idx = headers.index("created_at")
            if created_idx < len(existing_values):
                new_row["created_at"] = existing_values[created_idx]
        except ValueError:
            pass

        row_values = [new_row.get(col, "") for col in PUBLISHERS_SCHEMA]
        last_col_letter = chr(ord("A") + len(PUBLISHERS_SCHEMA) - 1)
        ws.update(f"A{row_num}:{last_col_letter}{row_num}", [row_values])

        # Clear the cache so next read picks up the update
        st.cache_data.clear()
        return "updated"
    else:
        # INSERT
        row_values = [new_row.get(col, "") for col in PUBLISHERS_SCHEMA]
        ws.append_row(row_values)
        st.cache_data.clear()
        return "created"


# ============================================================================
# UTILITY: extract first name from email when nothing else available
# ============================================================================

def guess_first_name_from_email(email: str) -> str:
    """
    Best-effort first-name extraction from email local-part.

    Examples:
        alice.chen@example.com → "Alice"
        a.chen@example.com → "" (too short to guess)
        alice@example.com → "Alice"
        admin@example.com → "" (role-based, skip)
        first.last@example.com → "First" (might be wrong)

    Returns empty string if extraction isn't confident.
    NOT used automatically — only as a UI suggestion to seed the Publishers tab.
    """
    if not email or "@" not in email:
        return ""

    local = email.split("@")[0].lower()

    # Role-based prefixes — skip
    role_prefixes = ["admin", "info", "support", "noreply", "no-reply",
                     "donotreply", "team", "hello", "contact", "sales"]
    if local in role_prefixes or any(local.startswith(p + ".") for p in role_prefixes):
        return ""

    # Split on common separators
    for sep in [".", "_", "-", "+"]:
        if sep in local:
            first = local.split(sep)[0]
            break
    else:
        first = local

    # Too short = probably not a real first name
    if len(first) < 3:
        return ""

    return first.capitalize()
