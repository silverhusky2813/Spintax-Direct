"""
stage2_templates.py
====================
Template family definitions for all campaign types.

Each template is a structured object containing:
  - template_id: stable identifier (used in tracking)
  - template_version: bump when template content changes meaningfully
                      (audit error 2.14: paths only comparable within version)
  - campaign_type: which Stage 1 type this template serves
  - subject: spintax string for the subject line
  - body: spintax string for the body
  - required_variables: list of <<VARS>> that MUST be non-empty before send
  - optional_variables: list of <<VARS>> with graceful defaults
  - notes: internal documentation

Templates use INLINE spintax: {option1|option2|option3}

Variables use ANGLE BRACKETS: <<VARIABLE_NAME>>

Order of operations (audit errors 2.3, 2.22):
  1. spin() picks one option from each {...} block
  2. substitute_variables() replaces <<VARS>>

ALL TEMPLATES are validated at module load (TEMPLATE_REGISTRY init).
Templates with validation errors will fail loudly with a TemplateValidationError.

To add a new template:
  1. Add a Template entry to TEMPLATE_REGISTRY
  2. Bump template_version if editing an existing one (preserves tracking)
  3. Run test_stage2.py to verify it parses
"""

from dataclasses import dataclass, field

from stage2_spintax_engine import (
    count_spin_space,
    validate_template,
    TemplateValidationError,
)


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class Template:
    """A subject + body template pair for one campaign type."""
    template_id: str
    template_version: int
    campaign_type: str           # "Outreach" | "FollowUp" | "Brief" | "WinBack"
    subject: str                 # spintax string
    body: str                    # spintax string
    required_variables: list[str] = field(default_factory=list)
    optional_variables: list[str] = field(default_factory=list)
    notes: str = ""

    @property
    def subject_spin_space(self) -> int:
        return count_spin_space(self.subject)

    @property
    def body_spin_space(self) -> int:
        return count_spin_space(self.body)

    @property
    def total_spin_space(self) -> int:
        return self.subject_spin_space * self.body_spin_space


# ============================================================================
# COMMON FALLBACK VALUES (audit error 2.18)
# ============================================================================

VARIABLE_FALLBACKS = {
    "FIRST_NAME": "there",        # "Hi there," when no first name known
    "PUBLISHER_NAME": "your team",
    "LAST_NAME": "",
    "SENDER_NAME": "Daniel",      # default sender
    "SENDER_SIGNATURE": "Daniel\nPremiumAds",
}

# Variables we expect to ALWAYS be provided by the system (not user-edited)
SYSTEM_VARIABLES = {
    "BRAND",
    "APP_NAME",
    "VERTICAL",
    "FLIGHT",
    "CPM_FLOOR",
    "CPM_OFFER",
    "CPM_TABLE",
}


# ============================================================================
# OUTREACH TEMPLATE (initial cold contact)
# ============================================================================

OUTREACH_V1 = Template(
    template_id="outreach_v1",
    template_version=1,
    campaign_type="Outreach",
    subject=(
        "{Confirmed media buy|Active campaign|Direct deal opportunity} "
        "for <<BRAND>> — <<APP_NAME>>"
    ),
    body=(
        "Hi <<FIRST_NAME>>,\n\n"
        "{I'm Daniel from PremiumAds — confirmed media buy I need to fill.|"
        "Daniel here from PremiumAds with an active <<BRAND>> campaign.|"
        "Daniel from PremiumAds — direct deal opportunity for <<APP_NAME>>.}\n\n"
        "{We're running|We have|Currently allocating} <<BRAND>> across <<VERTICAL>> "
        "inventory and <<APP_NAME>> {flagged as a strong match|came up as a top fit|"
        "is exactly the audience we need}.\n\n"
        "Floor CPM: <<CPM_FLOOR>>\n"
        "Offer CPM: <<CPM_OFFER>>\n"
        "Flight: <<FLIGHT>>\n\n"
        "<<CPM_TABLE>>\n\n"
        "{Allocations filling up — held a slot for <<APP_NAME>>|"
        "Budget gets locked end of week, but I held space for <<APP_NAME>>|"
        "I'm prioritizing <<APP_NAME>> on this one}, "
        "but need to confirm by end of week.\n\n"
        "{Worth a quick reply to lock it in?|"
        "Quick yes/no so I can lock the slot?|"
        "Reply to hold your spot?}\n\n"
        "Daniel\n"
        "PremiumAds"
    ),
    required_variables=[
        "BRAND", "APP_NAME", "VERTICAL", "FLIGHT",
        "CPM_FLOOR", "CPM_OFFER", "CPM_TABLE",
    ],
    optional_variables=["FIRST_NAME"],
    notes="Standard cold outreach. Identity-first, CPM-transparent, scarcity close.",
)


# ============================================================================
# FOLLOW-UP TEMPLATE (referencing prior outreach)
# ============================================================================

FOLLOWUP_V1 = Template(
    template_id="followup_v1",
    template_version=1,
    campaign_type="FollowUp",
    subject=(
        "{Re: <<BRAND>> × <<APP_NAME>> — Still Available|"
        "Following Up: <<BRAND>> Deal for <<APP_NAME>>|"
        "<<BRAND>> × <<APP_NAME>> — One Last Note}"
    ),
    body=(
        "Hi <<FIRST_NAME>>,\n\n"
        "{Wanted to circle back|Quick follow-up|Just bumping this} on the "
        "<<BRAND>> opportunity for <<APP_NAME>>.\n\n"
        "{The slot is still open|Allocation is still available|"
        "I haven't locked the spot yet}, but I {can't hold it much longer|"
        "need to confirm allocations soon|am closing this batch end of day}.\n\n"
        "Quick recap:\n"
        "• Floor CPM: <<CPM_FLOOR>>\n"
        "• Offer CPM: <<CPM_OFFER>>\n"
        "• Flight: <<FLIGHT>>\n\n"
        "{Quick yes/no?|Worth a 30-sec reply?|Even a 'not now' helps me plan.}\n\n"
        "Daniel"
    ),
    required_variables=[
        "BRAND", "APP_NAME", "FLIGHT", "CPM_FLOOR", "CPM_OFFER",
    ],
    optional_variables=["FIRST_NAME"],
    notes="Short, urgency-focused. Assumes recipient has seen prior outreach.",
)


# ============================================================================
# BRIEF TEMPLATE (formal campaign proposal)
# ============================================================================

BRIEF_V1 = Template(
    template_id="brief_v1",
    template_version=1,
    campaign_type="Brief",
    subject=(
        "{Campaign Brief|Media Proposal|Partnership Brief}: "
        "<<BRAND>> × <<APP_NAME>>"
    ),
    body=(
        "Hi <<FIRST_NAME>>,\n\n"
        "{Below is the campaign brief|Sharing the brief|Full proposal below} "
        "for the <<BRAND>> × <<APP_NAME>> partnership.\n\n"
        "**Advertiser:** <<BRAND>>\n"
        "**Vertical:** <<VERTICAL>>\n"
        "**Target app:** <<APP_NAME>>\n"
        "**Flight:** <<FLIGHT>>\n\n"
        "**Inventory & Rates:**\n"
        "<<CPM_TABLE>>\n\n"
        "**Floor CPM:** <<CPM_FLOOR>>\n"
        "**Offer CPM:** <<CPM_OFFER>>\n\n"
        "{Let me know if these terms work|Happy to adjust if you need different terms|"
        "Open to discussing the structure} or set up a quick call to finalize.\n\n"
        "Daniel\n"
        "PremiumAds"
    ),
    required_variables=[
        "BRAND", "APP_NAME", "VERTICAL", "FLIGHT",
        "CPM_FLOOR", "CPM_OFFER", "CPM_TABLE",
    ],
    optional_variables=["FIRST_NAME"],
    notes="Formal brief format. Use for established publishers or post-call follow-up.",
)


# ============================================================================
# WIN-BACK TEMPLATE (re-engage cold publishers)
# ============================================================================

WINBACK_V1 = Template(
    template_id="winback_v1",
    template_version=1,
    campaign_type="WinBack",
    subject=(
        "{Long time, no chat|Checking in|It's been a minute} — "
        "<<BRAND>> opportunity for <<APP_NAME>>"
    ),
    body=(
        "Hi <<FIRST_NAME>>,\n\n"
        "{It's been a while|Long time no talk|Hope things are well} since "
        "we last connected on <<APP_NAME>>.\n\n"
        "{New opportunity|Fresh campaign|Direct deal} from <<BRAND>> hit my desk "
        "and <<APP_NAME>> {looks like a strong fit|matches the brief|is right on target}.\n\n"
        "Floor CPM: <<CPM_FLOOR>>\n"
        "Offer CPM: <<CPM_OFFER>>\n"
        "Flight: <<FLIGHT>>\n\n"
        "{Worth a quick chat?|Open to revisiting?|Even a quick reply helps me plan.}\n\n"
        "Daniel\n"
        "PremiumAds"
    ),
    required_variables=[
        "BRAND", "APP_NAME", "FLIGHT", "CPM_FLOOR", "CPM_OFFER",
    ],
    optional_variables=["FIRST_NAME"],
    notes="Warmer tone for publishers we haven't contacted in 90+ days.",
)


# ============================================================================
# REGISTRY (single source of truth for the rest of Stage 2)
# ============================================================================

TEMPLATE_REGISTRY: dict[str, Template] = {
    "outreach_v1": OUTREACH_V1,
    "followup_v1": FOLLOWUP_V1,
    "brief_v1":    BRIEF_V1,
    "winback_v1":  WINBACK_V1,
}


# Map: campaign_type → list of compatible template_ids
# Used when Stage 2 needs to pick a template based on Stage 1's campaign_type
DEFAULT_TEMPLATE_FOR_TYPE: dict[str, str] = {
    "Outreach": "outreach_v1",
    "FollowUp": "followup_v1",
    "Brief":    "brief_v1",
    "WinBack":  "winback_v1",
}


# ============================================================================
# LOAD-TIME VALIDATION (audit errors 2.12, 2.13)
# ============================================================================

def _validate_all_templates_on_load():
    """Raise TemplateValidationError if any registered template is invalid."""
    all_issues = []
    for tid, template in TEMPLATE_REGISTRY.items():
        # Validate subject
        subj_issues = validate_template(template.subject)
        if subj_issues:
            all_issues.append(
                f"Template '{tid}' SUBJECT has issues: {subj_issues}"
            )

        # Validate body
        body_issues = validate_template(template.body)
        if body_issues:
            all_issues.append(
                f"Template '{tid}' BODY has issues: {body_issues}"
            )

    if all_issues:
        raise TemplateValidationError(
            f"Template registry has {len(all_issues)} issue(s):\n" +
            "\n".join(f"  - {issue}" for issue in all_issues)
        )


# Run validation at import time — fail fast if anything is broken
_validate_all_templates_on_load()


# ============================================================================
# PUBLIC API
# ============================================================================

def get_template(template_id: str) -> Template:
    """Retrieve a template by id. Raises KeyError if not found."""
    if template_id not in TEMPLATE_REGISTRY:
        available = ", ".join(TEMPLATE_REGISTRY.keys())
        raise KeyError(
            f"Template '{template_id}' not found. Available: {available}"
        )
    return TEMPLATE_REGISTRY[template_id]


def get_template_for_campaign_type(campaign_type: str) -> Template:
    """Get the default template for a Stage 1 campaign type."""
    template_id = DEFAULT_TEMPLATE_FOR_TYPE.get(campaign_type)
    if not template_id:
        raise ValueError(
            f"No template registered for campaign_type='{campaign_type}'. "
            f"Known: {list(DEFAULT_TEMPLATE_FOR_TYPE.keys())}"
        )
    return get_template(template_id)


def list_templates() -> list[Template]:
    """Return all registered templates (for UI dropdown, etc.)."""
    return list(TEMPLATE_REGISTRY.values())
