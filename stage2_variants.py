"""
stage2_variants.py
===================
Orchestrator that ties together templates, spintax engine, publishers,
and CPM table to produce a complete generated email variant.

This is the module Stage 2 UI calls to do the actual work.

Solves audit errors:
  - 2.15: Edits don't lose variant_id (we track was_edited separately)
  - 2.19/2.20: CPM table fallback with surface warning
  - 2.18: Publisher fallback surfaced
  - 2.7: Required vars validated before "ready to send"

Public API:
  generate_variant(campaign_id, regenerate_count=0) → GeneratedVariant
  build_email_values(campaign, publisher, regenerate_count) → dict
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from stage1_persistence import get_campaign
from stage2_cpm_table import build_cpm_table
from stage2_publishers import get_publisher_with_fallback
from stage2_spintax_engine import (
    SpinResult,
    SubstitutionResult,
    derive_seed,
    render,
)
from stage2_templates import (
    SYSTEM_VARIABLES,
    Template,
    get_template_for_campaign_type,
)


# ============================================================================
# DATA STRUCTURE
# ============================================================================

@dataclass
class GeneratedVariant:
    """
    Complete output of variant generation for one campaign + recipient.

    Carries everything Stage 4 (Queue) needs to record + send the email.
    """
    # Identity & traceability
    campaign_id: str
    template_id: str
    template_version: int
    seed: int
    regenerate_count: int

    # Output content
    subject: str
    body: str

    # Tracking data (audit errors 2.14, 2.15)
    subject_spin_path: list[tuple[int, str]] = field(default_factory=list)
    body_spin_path: list[tuple[int, str]] = field(default_factory=list)

    # Quality flags (audit errors 2.18, 2.19, 2.7)
    publisher_fallback_used: bool = False
    publisher_fallback_fields: list[str] = field(default_factory=list)
    cpm_table_fallback_used: bool = False
    missing_required_variables: list[str] = field(default_factory=list)

    # Metadata for the Emails row
    generated_at: str = ""
    recipient_email: str = ""

    @property
    def is_ready_to_send(self) -> bool:
        """True if no critical issues blocking send."""
        return not self.missing_required_variables

    @property
    def spin_path_json(self) -> dict:
        """JSON-serializable form for storage in the Emails tab."""
        return {
            "subject": [{"pos": p, "text": t} for p, t in self.subject_spin_path],
            "body": [{"pos": p, "text": t} for p, t in self.body_spin_path],
        }

    @property
    def warnings(self) -> list[str]:
        """All UI-relevant warnings about this variant."""
        warnings = []
        if self.publisher_fallback_used:
            warnings.append(
                f"Using fallback values for: {', '.join(self.publisher_fallback_fields)}. "
                f"Consider adding {self.recipient_email} to Publishers tab."
            )
        if self.cpm_table_fallback_used:
            warnings.append(
                "No detailed CPM rates available — using simple inline CPM line. "
                "Add rates to cpm_rates tab for richer table."
            )
        if self.missing_required_variables:
            warnings.append(
                f"⛔ Required variables missing: {', '.join(self.missing_required_variables)}. "
                f"Email NOT ready to send."
            )
        return warnings


# ============================================================================
# FLIGHT FORMATTER
# ============================================================================

def _format_flight(flight_start, flight_end) -> str:
    """Format flight dates as human-readable string for the FLIGHT variable.

    Output: "June 1 – June 30, 2026"
    """
    if not flight_start or not flight_end:
        return ""

    # Coerce strings to dates
    if isinstance(flight_start, str):
        try:
            flight_start = datetime.fromisoformat(flight_start[:10]).date()
        except ValueError:
            return f"{flight_start} – {flight_end}"
    if isinstance(flight_end, str):
        try:
            flight_end = datetime.fromisoformat(flight_end[:10]).date()
        except ValueError:
            return f"{flight_start} – {flight_end}"

    if isinstance(flight_start, datetime):
        flight_start = flight_start.date()
    if isinstance(flight_end, datetime):
        flight_end = flight_end.date()

    # Same year: "June 1 – June 30, 2026"
    # Different year: "Dec 20, 2026 – Jan 15, 2027"
    if flight_start.year == flight_end.year:
        return (
            f"{flight_start.strftime('%B %-d')} – "
            f"{flight_end.strftime('%B %-d, %Y')}"
        )
    else:
        return (
            f"{flight_start.strftime('%b %-d, %Y')} – "
            f"{flight_end.strftime('%b %-d, %Y')}"
        )


# ============================================================================
# VALUE ASSEMBLY (the inputs to substitute_variables)
# ============================================================================

def build_email_values(
    campaign: dict,
    recipient_email: str,
) -> tuple[dict, bool, list[str], bool]:
    """
    Assemble the dict of values for variable substitution.

    Returns:
        (values_dict, publisher_fallback_used, publisher_fallback_fields,
         cpm_table_fallback_used)

    Reads:
        - Campaign data (Stage 1)
        - Publishers tab (with fallback)
        - cpm_rates tab (with fallback)
    """
    # Publisher lookup
    publisher_data, fb_used, fb_fields = get_publisher_with_fallback(recipient_email)

    # CPM table
    vertical = campaign.get("vertical", "")
    geo = campaign.get("target_geo", "") or "Global"
    cpm_floor = float(campaign.get("cpm_floor", 0) or 0)
    cpm_offer = float(campaign.get("cpm_offer", 0) or 0)

    cpm_table, cpm_fallback = build_cpm_table(
        vertical=vertical,
        geo=geo,
        fallback_floor=cpm_floor,
        fallback_offer=cpm_offer,
    )

    # Flight string
    flight = _format_flight(
        campaign.get("flight_start"),
        campaign.get("flight_end"),
    )

    # CPM number formatting (audit error 2.11: clean output)
    def fmt_cpm(val) -> str:
        try:
            return f"${float(val):.2f}"
        except (ValueError, TypeError):
            return "—"

    values = {
        # System variables (always provided)
        "BRAND": campaign.get("brand", ""),
        "APP_NAME": campaign.get("app_name", ""),
        "VERTICAL": campaign.get("vertical", ""),
        "FLIGHT": flight,
        "CPM_FLOOR": fmt_cpm(cpm_floor),
        "CPM_OFFER": fmt_cpm(cpm_offer),
        "CPM_TABLE": cpm_table,

        # Publisher variables (may use fallback)
        "FIRST_NAME": publisher_data.get("first_name", ""),
        "LAST_NAME": publisher_data.get("last_name", ""),
        "PUBLISHER_NAME": publisher_data.get("publisher_name", ""),

        # Sender (TODO: make configurable later)
        "SENDER_NAME": "Daniel",
        "SENDER_SIGNATURE": "Daniel\nPremiumAds",
    }

    return values, fb_used, fb_fields, cpm_fallback


# ============================================================================
# CORE GENERATION
# ============================================================================

def generate_variant(
    campaign_id: str,
    recipient_email: str,
    regenerate_count: int = 0,
    template_id_override: Optional[str] = None,
) -> GeneratedVariant:
    """
    Generate a fully-rendered email variant for the given campaign.

    Determinism (audit error 2.1):
      Seed = hash(campaign_id, recipient_email, regenerate_count)
      Same inputs → same output. Increment regenerate_count for new variant.

    Args:
        campaign_id: From Stage 1's save_campaign() output
        recipient_email: Where this email will be sent
        regenerate_count: 0 = first generation, N = Nth regenerate click
        template_id_override: Force a specific template (default: use the one
                              mapped to campaign's campaign_type)

    Returns:
        GeneratedVariant ready for review/edit/send.
    """
    # ---- Load campaign ----
    campaign = get_campaign(campaign_id)
    if not campaign:
        raise ValueError(f"Campaign {campaign_id} not found")

    # ---- Pick template ----
    if template_id_override:
        from stage2_templates import get_template
        template: Template = get_template(template_id_override)
    else:
        ctype = campaign.get("campaign_type", "Outreach")
        template = get_template_for_campaign_type(ctype)

    # ---- Assemble values ----
    values, fb_used, fb_fields, cpm_fb = build_email_values(
        campaign, recipient_email,
    )

    # ---- Derive seeds (separate for subject vs body so they vary independently) ----
    subject_seed = derive_seed(
        campaign_id, recipient_email, "subject",
        template.template_id, template.template_version,
        regenerate_count,
    )
    body_seed = derive_seed(
        campaign_id, recipient_email, "body",
        template.template_id, template.template_version,
        regenerate_count,
    )

    # ---- Render subject ----
    # We use strict=False so we get back the variant even if vars are missing,
    # with the missing list. UI surfaces this as a warning.
    subject_final, subject_spin, subject_sub = render(
        template.subject,
        subject_seed,
        values,
        required=template.required_variables,
        strict=False,
    )

    # ---- Render body ----
    body_final, body_spin, body_sub = render(
        template.body,
        body_seed,
        values,
        required=template.required_variables,
        strict=False,
    )

    # Aggregate missing required variables across subject + body
    missing = set(subject_sub.missing_variables) | set(body_sub.missing_variables)
    # Filter to only REQUIRED ones (the rest are tolerable empties)
    required_set = {v.upper() for v in template.required_variables}
    missing_required = sorted([v for v in missing if v in required_set])

    return GeneratedVariant(
        campaign_id=campaign_id,
        template_id=template.template_id,
        template_version=template.template_version,
        seed=subject_seed,  # store subject seed as the canonical one
        regenerate_count=regenerate_count,

        subject=subject_final,
        body=body_final,

        subject_spin_path=subject_spin.spin_path,
        body_spin_path=body_spin.spin_path,

        publisher_fallback_used=fb_used,
        publisher_fallback_fields=fb_fields,
        cpm_table_fallback_used=cpm_fb,
        missing_required_variables=missing_required,

        generated_at=datetime.now().isoformat(),
        recipient_email=recipient_email,
    )


# ============================================================================
# EDIT TRACKING (audit error 2.15)
# ============================================================================

def compute_edit_distance(original: str, edited: str) -> int:
    """
    Simple character-level edit distance for tracking how much a user edited.

    Used to populate `was_edited` and (optionally) a percent-changed metric.
    NOT Levenshtein — that's overkill. We just want to know "did this change at all"
    and "roughly how much" for analytics.
    """
    if original == edited:
        return 0

    # Char count difference + content difference
    return abs(len(original) - len(edited)) + sum(
        1 for a, b in zip(original, edited) if a != b
    )


def detect_edits(
    variant: GeneratedVariant,
    final_subject: str,
    final_body: str,
) -> dict:
    """
    Compare original variant output to (potentially edited) final content.

    Returns dict with:
        was_edited: bool
        subject_was_edited: bool
        body_was_edited: bool
        subject_edit_distance: int
        body_edit_distance: int
    """
    subj_dist = compute_edit_distance(variant.subject, final_subject)
    body_dist = compute_edit_distance(variant.body, final_body)

    return {
        "was_edited": (subj_dist > 0 or body_dist > 0),
        "subject_was_edited": subj_dist > 0,
        "body_was_edited": body_dist > 0,
        "subject_edit_distance": subj_dist,
        "body_edit_distance": body_dist,
    }
