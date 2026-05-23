"""
stage2_spintax_engine.py
=========================
Pure functions for spintax processing. No I/O, no Streamlit, no Sheets.

The engine does two things:
  1. SPIN: Replace {a|b|c} blocks with one chosen option, deterministically
  2. SUBSTITUTE: Replace <<VARIABLE>> placeholders with values from a dict

Order is critical (audit error 2.3, 2.22): always SPIN FIRST, THEN SUBSTITUTE.
Reason: variable values may contain `{`, `|`, `}` (rare but possible in brand
names like "Ben & Jerry's") — spinning first ensures these aren't mis-parsed.

Determinism (audit error 2.1):
  Same (template, seed) → same output, every time.
  This means a user can regenerate consistently, and we can replay any
  past email exactly by storing (template_id, template_version, seed).

Spin path tracking (audit error 2.14):
  We store the CHOSEN TEXT at each spin position, not just indices.
  Storing indices breaks if templates change (option order shifts).
  Storing text is unambiguous forever.

Engine guarantees:
  - Spintax syntax: flat only, no nesting (audit error 2.8)
  - Empty options rejected at template-load time (audit error 2.13)
  - Required variables validated before substitution (audit error 2.7)
"""

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any


# ============================================================================
# REGEX PATTERNS
# ============================================================================

# Matches {a|b|c} — non-greedy, no nesting allowed
SPINTAX_PATTERN = re.compile(r"\{([^{}]+)\}")

# Matches <<VARIABLE_NAME>> — uppercase letters, digits, underscores
VARIABLE_PATTERN = re.compile(r"<<([A-Z][A-Z0-9_]*)>>")


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class SpinResult:
    """
    Result of spinning a template (before variable substitution).

    Attributes:
        text: The spun string with variables NOT yet substituted
        spin_path: List of (position, chosen_text) tuples — used for tracking
        seed_used: The integer seed that produced this spin
    """
    text: str
    spin_path: list[tuple[int, str]] = field(default_factory=list)
    seed_used: int = 0


@dataclass
class SubstitutionResult:
    """
    Result of substituting variables into spun text.

    Attributes:
        text: Final text with all variables replaced
        variables_used: Dict of variable name → value that was substituted
        missing_variables: List of <<VARS>> that were not in the values dict
                           (substituted as empty string with a warning)
    """
    text: str
    variables_used: dict[str, str] = field(default_factory=dict)
    missing_variables: list[str] = field(default_factory=list)


# ============================================================================
# SEED DERIVATION
# ============================================================================

def derive_seed(*components: str) -> int:
    """
    Derive a deterministic integer seed from string components.

    Example:
        derive_seed(campaign_id, recipient_email, "subject", regenerate_count)

    Same inputs → same seed → same spin output. Always.
    Uses SHA-256 truncated to 64 bits for distribution + collision resistance.
    """
    combined = "|".join(str(c) for c in components)
    digest = hashlib.sha256(combined.encode("utf-8")).digest()
    # Take first 8 bytes as little-endian unsigned int
    return int.from_bytes(digest[:8], byteorder="little", signed=False)


# ============================================================================
# TEMPLATE VALIDATION (audit errors 2.12, 2.13)
# ============================================================================

class TemplateValidationError(ValueError):
    """Raised when a template has invalid spintax or variable syntax."""


def validate_template(template: str) -> list[str]:
    """
    Validate a template at load time. Returns list of issues (empty = valid).

    Catches:
      - Empty spintax options: `{a||b}` or `{ |b}` — sometimes intentional, often typo
      - Unbalanced braces: `{a|b` or `a|b}`
      - Nested spintax: `{a|{b|c}}` (not supported)
      - Spintax inside variables: `<<{V1|V2}>>` (not supported)
    """
    issues = []

    # Check for unbalanced braces
    open_count = template.count("{")
    close_count = template.count("}")
    if open_count != close_count:
        issues.append(
            f"Unbalanced braces: {open_count} '{{' vs {close_count} '}}'"
        )

    # Check for nested spintax (a { inside an existing spintax block)
    # Find all { positions, check none come before the matching }
    depth = 0
    for i, char in enumerate(template):
        if char == "{":
            depth += 1
            if depth > 1:
                issues.append(
                    f"Nested spintax not supported at position {i}: "
                    f"'{template[max(0, i-10):i+10]}'"
                )
                break
        elif char == "}":
            depth -= 1
            if depth < 0:
                issues.append(f"Unmatched '}}' at position {i}")
                break

    # Check for empty options in spintax blocks
    for match in SPINTAX_PATTERN.finditer(template):
        block_content = match.group(1)
        options = block_content.split("|")

        if len(options) < 2:
            issues.append(
                f"Spintax block '{{{block_content}}}' has only 1 option "
                f"(needs at least 2)"
            )

        for i, opt in enumerate(options):
            # Allow trailing empty option as "optional" marker: {a|b|}
            is_trailing = (i == len(options) - 1)
            if not opt.strip() and not is_trailing:
                issues.append(
                    f"Empty option at index {i} in '{{{block_content}}}' "
                    f"(use trailing '|' for optional: '{{a|b|}}')"
                )

    # Check for spintax inside variable markers
    for match in VARIABLE_PATTERN.finditer(template):
        var_name = match.group(1)
        if "{" in var_name or "|" in var_name or "}" in var_name:
            issues.append(
                f"Spintax not allowed inside variable: <<{var_name}>>"
            )

    return issues


# ============================================================================
# SPIN (audit error 2.3: spin FIRST, then substitute)
# ============================================================================

def spin(template: str, seed: int) -> SpinResult:
    """
    Replace all {a|b|c} blocks in `template` with one chosen option.

    Uses a deterministic PRNG seeded by `seed`. The spin_path records the
    chosen TEXT at each position (not index — audit error 2.14).

    Args:
        template: Source text with {a|b|c} spintax blocks
        seed: Integer seed (derive via derive_seed())

    Returns:
        SpinResult with .text, .spin_path, .seed_used

    Raises:
        TemplateValidationError if template fails validation
    """
    # Validate first — fail fast on bad templates
    issues = validate_template(template)
    if issues:
        raise TemplateValidationError(
            f"Template has {len(issues)} issue(s): {'; '.join(issues)}"
        )

    # Use Python's random module with our seed for reproducibility
    import random
    rng = random.Random(seed)

    spin_path: list[tuple[int, str]] = []

    def replace_block(match: re.Match) -> str:
        block_content = match.group(1)
        options = block_content.split("|")
        chosen = rng.choice(options)
        # Record position (start of match in original template) and chosen text
        spin_path.append((match.start(), chosen))
        return chosen

    # Iteratively replace until no more spintax blocks
    # (Multiple passes shouldn't be needed since we forbid nesting, but safe)
    result_text = SPINTAX_PATTERN.sub(replace_block, template)

    # Defensive: verify no spintax remains
    if "{" in result_text or "}" in result_text:
        # Could be literal braces in copy — only flag if pattern matches
        if SPINTAX_PATTERN.search(result_text):
            raise TemplateValidationError(
                "Spintax remained after spin — possible nested or malformed template"
            )

    return SpinResult(
        text=result_text,
        spin_path=spin_path,
        seed_used=seed,
    )


# ============================================================================
# SUBSTITUTE (audit error 2.11: handle empty values, audit error 2.7: required vars)
# ============================================================================

def substitute_variables(
    text: str,
    values: dict[str, Any],
    required: list[str] | None = None,
    strict: bool = False,
) -> SubstitutionResult:
    """
    Replace <<VARIABLE>> placeholders with values from `values` dict.

    Args:
        text: Source text with <<VAR>> placeholders
        values: Dict of variable_name → value (case-insensitive keys)
        required: List of variable names that MUST be present and non-empty.
                  If any are missing/empty, raises ValueError (unless strict=False
                  in which case they're added to missing_variables and replaced
                  with empty string).
        strict: If True, raise on missing required vars. If False, replace
                with empty string and log.

    Returns:
        SubstitutionResult with .text, .variables_used, .missing_variables

    Note on empty-value handling (audit error 2.11):
        If a variable substitutes to empty string, this can leave orphan
        whitespace/punctuation. We DO NOT auto-clean — that's the caller's
        responsibility (e.g., refuse to send emails with empty required vars).
    """
    # Normalize keys to uppercase for case-insensitive lookup
    values_upper = {k.upper(): str(v) if v is not None else "" for k, v in values.items()}

    required = required or []
    required_upper = [r.upper() for r in required]

    variables_used: dict[str, str] = {}
    missing_variables: list[str] = []

    # Find all <<VAR>> references
    used_vars = set(VARIABLE_PATTERN.findall(text))

    # Check required vars present and non-empty
    for req in required_upper:
        val = values_upper.get(req, "")
        if not val or not str(val).strip():
            missing_variables.append(req)

    if missing_variables and strict:
        raise ValueError(
            f"Required variables missing or empty: {missing_variables}"
        )

    # Perform substitution
    def replace_var(match: re.Match) -> str:
        var_name = match.group(1).upper()
        if var_name in values_upper:
            val = values_upper[var_name]
            variables_used[var_name] = val
            return val
        else:
            if var_name not in missing_variables:
                missing_variables.append(var_name)
            return ""  # leave a gap; caller decides what to do

    result_text = VARIABLE_PATTERN.sub(replace_var, text)

    return SubstitutionResult(
        text=result_text,
        variables_used=variables_used,
        missing_variables=missing_variables,
    )


# ============================================================================
# CONVENIENCE: SPIN + SUBSTITUTE IN ONE CALL
# ============================================================================

def render(
    template: str,
    seed: int,
    values: dict[str, Any],
    required: list[str] | None = None,
    strict: bool = False,
) -> tuple[str, SpinResult, SubstitutionResult]:
    """
    Full pipeline: spin THEN substitute.

    Returns:
        (final_text, spin_result, substitution_result)
    """
    spin_result = spin(template, seed)
    sub_result = substitute_variables(
        spin_result.text,
        values,
        required=required,
        strict=strict,
    )
    return sub_result.text, spin_result, sub_result


# ============================================================================
# SPIN SPACE CALCULATION (audit error 2.16: show user how many combos exist)
# ============================================================================

def count_spin_space(template: str) -> int:
    """
    Count the total number of unique outputs `template` can produce.

    Used by UI to show "You've seen 5 / 81 possible variants."
    """
    total = 1
    for match in SPINTAX_PATTERN.finditer(template):
        block_content = match.group(1)
        options = block_content.split("|")
        total *= len(options)
    return total
