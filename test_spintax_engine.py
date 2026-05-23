"""
test_spintax_engine.py
========================
Unit tests for stage2_spintax_engine.py.

Tests every audit error fix:
  - 2.1: Determinism via seeding
  - 2.3 / 2.22: Spin before substitute
  - 2.7: Required variable enforcement
  - 2.8: Nesting rejected
  - 2.11: Empty value handling
  - 2.12: Variable syntax conflicts
  - 2.13: Empty options rejected
  - 2.14: Spin path tracks chosen text
  - 2.16: Spin space counting

Run with:
  python test_spintax_engine.py
"""

import sys

from stage2_spintax_engine import (
    SPINTAX_PATTERN,
    VARIABLE_PATTERN,
    TemplateValidationError,
    count_spin_space,
    derive_seed,
    render,
    spin,
    substitute_variables,
    validate_template,
)


def assert_eq(actual, expected, label):
    if actual == expected:
        print(f"  ✓ PASS: {label}")
        return True
    else:
        print(f"  ✗ FAIL: {label}")
        print(f"      Expected: {expected!r}")
        print(f"      Got:      {actual!r}")
        return False


def assert_true(condition, label):
    if condition:
        print(f"  ✓ PASS: {label}")
        return True
    else:
        print(f"  ✗ FAIL: {label}")
        return False


def assert_raises(callable_fn, exception_type, label):
    try:
        callable_fn()
        print(f"  ✗ FAIL: {label} (expected {exception_type.__name__}, got no exception)")
        return False
    except exception_type:
        print(f"  ✓ PASS: {label}")
        return True
    except Exception as e:
        print(f"  ✗ FAIL: {label} (expected {exception_type.__name__}, got {type(e).__name__}: {e})")
        return False


# ============================================================================
# Test: Determinism (audit error 2.1)
# ============================================================================

def test_determinism():
    print("\n--- Test: Determinism (audit error 2.1) ---")

    template = "Hi {there|hey|hello}, {check out|consider} this offer."
    seed = derive_seed("campaign-123", "alice@example.com", "subject")

    result1 = spin(template, seed)
    result2 = spin(template, seed)

    assert_eq(result1.text, result2.text, "same seed produces same output")
    assert_eq(result1.spin_path, result2.spin_path, "same seed produces same spin_path")

    # Different seed → different output (probabilistically)
    seed_different = derive_seed("campaign-456", "alice@example.com", "subject")
    result3 = spin(template, seed_different)

    # 9 possible outputs total (3 × 3). Two different seeds should produce
    # different outputs most of the time, but could collide. Test 5 different
    # seeds and check at least 2 unique outputs.
    outputs = set()
    for i in range(5):
        s = derive_seed("campaign", f"recipient-{i}", "subject")
        outputs.add(spin(template, s).text)

    assert_true(len(outputs) >= 2, f"different seeds produce variation ({len(outputs)} unique from 5 seeds)")


def test_seed_derivation():
    print("\n--- Test: Seed derivation ---")

    # Same components → same seed
    s1 = derive_seed("a", "b", "c")
    s2 = derive_seed("a", "b", "c")
    assert_eq(s1, s2, "same components → same seed")

    # Order matters
    s3 = derive_seed("a", "c", "b")
    assert_true(s1 != s3, "component order matters")

    # Integer types coerced to string
    s4 = derive_seed("campaign", "alice@example.com", 1)
    s5 = derive_seed("campaign", "alice@example.com", "1")
    assert_eq(s4, s5, "int and str produce same seed (coerced)")

    # Seed is non-negative 64-bit
    assert_true(0 <= s1 < 2**64, "seed in valid uint64 range")


# ============================================================================
# Test: Spin path tracks chosen text (audit error 2.14)
# ============================================================================

def test_spin_path_text():
    print("\n--- Test: Spin path stores TEXT not indices (audit error 2.14) ---")

    template = "Hi {there|hey|hello}, you should {act now|jump in}."
    result = spin(template, seed=42)

    # spin_path is list of (position, chosen_text) tuples
    assert_true(len(result.spin_path) == 2, "spin_path has one entry per spintax block")

    for position, chosen_text in result.spin_path:
        assert_true(
            chosen_text in ["there", "hey", "hello", "act now", "jump in"],
            f"chosen text '{chosen_text}' is one of the options"
        )
        assert_true(
            isinstance(position, int) and position >= 0,
            "position is non-negative int"
        )


# ============================================================================
# Test: Variable substitution (audit error 2.11)
# ============================================================================

def test_substitution_basic():
    print("\n--- Test: Variable substitution basic ---")

    text = "Hi <<FIRST_NAME>>, the <<BRAND>> campaign is live."
    result = substitute_variables(text, {"first_name": "Alice", "brand": "Nike"})

    assert_eq(result.text, "Hi Alice, the Nike campaign is live.", "substitution works (case-insensitive keys)")
    assert_eq(result.missing_variables, [], "no missing variables")
    assert_eq(set(result.variables_used.keys()), {"FIRST_NAME", "BRAND"}, "variables_used tracked")


def test_substitution_missing_value():
    print("\n--- Test: Substitution with missing values (audit error 2.11) ---")

    text = "Hi <<FIRST_NAME>>, the <<BRAND>> campaign is live."

    # Missing FIRST_NAME, not required
    result = substitute_variables(text, {"brand": "Nike"}, strict=False)
    assert_eq(result.text, "Hi , the Nike campaign is live.", "missing var → empty string (leaves gap)")
    assert_true("FIRST_NAME" in result.missing_variables, "missing var logged")

    # Missing FIRST_NAME, required + strict → error
    assert_raises(
        lambda: substitute_variables(text, {"brand": "Nike"}, required=["FIRST_NAME"], strict=True),
        ValueError,
        "strict mode raises on missing required var",
    )

    # Missing FIRST_NAME, required + non-strict → no error, but flagged
    result = substitute_variables(
        text,
        {"brand": "Nike"},
        required=["FIRST_NAME"],
        strict=False,
    )
    assert_true("FIRST_NAME" in result.missing_variables, "non-strict mode flags missing required var")


# ============================================================================
# Test: Spin THEN substitute order (audit errors 2.3, 2.22)
# ============================================================================

def test_render_order():
    print("\n--- Test: Render does spin THEN substitute (audit errors 2.3, 2.22) ---")

    template = "{Hi|Hey} <<FIRST_NAME>>, check {<<BRAND>>|out <<BRAND>>}'s offer."
    seed = derive_seed("test", "alice@example.com")

    # Even with variable containing potentially weird chars
    final, spin_result, sub_result = render(
        template,
        seed,
        {"first_name": "Alice", "brand": "Ben & Jerry's"},
    )

    # The brand should appear in the output (not get mangled by spintax)
    assert_true("Ben & Jerry's" in final, f"brand with special chars preserved: '{final}'")

    # Verify spin happened before substitute
    # (spin_result.text should still contain <<FIRST_NAME>> and <<BRAND>>)
    assert_true(
        "<<FIRST_NAME>>" in spin_result.text or "<<BRAND>>" in spin_result.text,
        "spin_result still contains variable placeholders (spin ran first)"
    )


# ============================================================================
# Test: Template validation (audit errors 2.8, 2.12, 2.13)
# ============================================================================

def test_validation_empty_options():
    print("\n--- Test: Empty options rejected (audit error 2.13) ---")

    # Empty option in middle
    issues = validate_template("Hi {there||hey}")
    assert_true(len(issues) > 0, "empty middle option flagged")

    # Trailing empty option (intentional optional marker) — allowed
    issues = validate_template("Hi {there|hey|}")
    assert_eq(issues, [], "trailing empty option allowed (optional marker)")

    # Whitespace-only option
    issues = validate_template("Hi {there| |hey}")
    assert_true(len(issues) > 0, "whitespace-only option flagged")


def test_validation_nesting():
    print("\n--- Test: Nested spintax rejected (audit error 2.8) ---")

    issues = validate_template("Hi {there|{a|b}}")
    assert_true(len(issues) > 0, "nested spintax flagged")

    # spin() should also raise
    assert_raises(
        lambda: spin("Hi {there|{a|b}}", seed=42),
        TemplateValidationError,
        "spin() raises on nested template",
    )


def test_validation_unbalanced():
    print("\n--- Test: Unbalanced braces rejected ---")

    issues = validate_template("Hi {there|hey")
    assert_true(len(issues) > 0, "missing closing brace flagged")

    issues = validate_template("Hi there|hey}")
    assert_true(len(issues) > 0, "missing opening brace flagged")


def test_validation_singleton_block():
    print("\n--- Test: Single-option spintax flagged ---")

    issues = validate_template("Hi {there} hey")
    assert_true(len(issues) > 0, "spintax with only 1 option flagged")


def test_validation_clean_template():
    print("\n--- Test: Valid template passes ---")

    issues = validate_template("Hi {there|hey|hello} <<NAME>>, check out <<BRAND>>'s offer!")
    assert_eq(issues, [], "clean template has no issues")


# ============================================================================
# Test: Spin space counting (audit error 2.16)
# ============================================================================

def test_spin_space():
    print("\n--- Test: Spin space counting (audit error 2.16) ---")

    assert_eq(count_spin_space("No spintax here"), 1, "no spintax = 1 combo")
    assert_eq(count_spin_space("{a|b}"), 2, "{a|b} = 2 combos")
    assert_eq(count_spin_space("{a|b} {c|d|e}"), 6, "{a|b} {c|d|e} = 6 combos")
    assert_eq(count_spin_space("{a|b|c} {x|y|z} {1|2|3}"), 27, "3x3x3 = 27 combos")


# ============================================================================
# Test: Full pipeline
# ============================================================================

def test_full_pipeline():
    print("\n--- Test: Full render pipeline ---")

    template = """Hi {there|hey} <<FIRST_NAME>>,

{Confirmed media buy|Active campaign} for <<BRAND>> needs <<VERTICAL>> inventory.
Flight: <<FLIGHT>>
CPM floor: <<CPM_FLOOR>>

Worth a quick reply?

Daniel"""

    seed = derive_seed("campaign-001", "alice@example.com", "body")
    values = {
        "first_name": "Alice",
        "brand": "Nike",
        "vertical": "Gaming",
        "flight": "June 1 – June 30",
        "cpm_floor": "$5.00",
    }
    required = ["FIRST_NAME", "BRAND", "VERTICAL", "FLIGHT", "CPM_FLOOR"]

    final, spin_result, sub_result = render(template, seed, values, required=required)

    assert_true("Alice" in final, "first name substituted")
    assert_true("Nike" in final, "brand substituted")
    assert_true("Gaming" in final, "vertical substituted")
    assert_eq(sub_result.missing_variables, [], "no missing variables")
    assert_true(len(spin_result.spin_path) == 2, "2 spintax blocks recorded")


# ============================================================================
# RUNNER
# ============================================================================

def run_all():
    print("=" * 60)
    print("Spintax Engine Test Suite")
    print("=" * 60)

    test_determinism()
    test_seed_derivation()
    test_spin_path_text()
    test_substitution_basic()
    test_substitution_missing_value()
    test_render_order()
    test_validation_empty_options()
    test_validation_nesting()
    test_validation_unbalanced()
    test_validation_singleton_block()
    test_validation_clean_template()
    test_spin_space()
    test_full_pipeline()

    print("\n" + "=" * 60)
    print("Test suite complete.")
    print("=" * 60)


if __name__ == "__main__":
    run_all()
