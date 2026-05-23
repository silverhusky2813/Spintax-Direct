"""
test_stage3_renderer.py
=========================
Tests for body cleaner and HTML renderer.

Covers audit errors:
  - 3.9: Whitespace/orphan punctuation cleanup
  - 3.13: HTML escape user content
  - 3.14: Strict markdown table detection with graceful fallback

Run with:
  python test_stage3_renderer.py
"""

from stage3_body_cleaner import (
    clean_email_body,
    clean_subject_line,
    collapse_excess_newlines,
    fix_orphan_punctuation,
    collapse_excess_spaces,
)
from stage3_html_renderer import (
    detect_table_blocks,
    render_html_email,
    render_table_html,
)


def assert_eq(actual, expected, label):
    if actual == expected:
        print(f"  ✓ PASS: {label}")
        return True
    print(f"  ✗ FAIL: {label}")
    print(f"      Expected: {expected!r}")
    print(f"      Got:      {actual!r}")
    return False


def assert_true(cond, label):
    if cond:
        print(f"  ✓ PASS: {label}")
        return True
    print(f"  ✗ FAIL: {label}")
    return False


# ============================================================================
# Test: Body cleaner (audit error 3.9)
# ============================================================================

def test_orphan_punctuation():
    print("\n--- Test: Orphan punctuation fix (audit error 3.9) ---")

    # Common case: empty FIRST_NAME → "Hi , there"
    assert_eq(
        fix_orphan_punctuation("Hi , there"),
        "Hi, there",
        "comma after space → cleaned",
    )

    # Period orphan
    assert_eq(
        fix_orphan_punctuation("Hello ."),
        "Hello.",
        "period after space → cleaned",
    )

    # Should NOT affect punctuation already adjacent
    assert_eq(
        fix_orphan_punctuation("Hi, there."),
        "Hi, there.",
        "correct punctuation preserved",
    )


def test_excess_newlines():
    print("\n--- Test: Excess newline collapse ---")

    assert_eq(
        collapse_excess_newlines("a\n\n\n\nb"),
        "a\n\nb",
        "4 newlines → 2",
    )

    assert_eq(
        collapse_excess_newlines("a\n\nb"),
        "a\n\nb",
        "2 newlines preserved",
    )

    assert_eq(
        collapse_excess_newlines("a\nb"),
        "a\nb",
        "single newline preserved",
    )


def test_excess_spaces():
    print("\n--- Test: Excess space collapse ---")

    assert_eq(
        collapse_excess_spaces("Hi   there"),
        "Hi there",
        "3 spaces → 1",
    )

    # Don't collapse leading indentation (for now — pure-content lines)
    # Actually our regex does collapse leading spaces; let's test that's OK
    assert_eq(
        collapse_excess_spaces("Line  one\nLine two"),
        "Line one\nLine two",
        "across newlines, only within-line",
    )


def test_clean_email_body_full():
    print("\n--- Test: Full clean_email_body pipeline ---")

    # Scenario: FIRST_NAME fell back to empty, PUBLISHER_NAME fell back to empty
    messy = (
        "Hi ,\n\n"
        "Welcome to  PremiumAds.\n\n\n\n"
        "Best ,\n"
        "Daniel  \n"
    )
    expected = (
        "Hi,\n\n"
        "Welcome to PremiumAds.\n\n"
        "Best,\n"
        "Daniel"
    )
    assert_eq(clean_email_body(messy), expected, "messy body cleaned end-to-end")


def test_clean_subject():
    print("\n--- Test: Subject cleaner ---")

    assert_eq(
        clean_subject_line("Hi   there"),
        "Hi there",
        "subject excess spaces",
    )

    assert_eq(
        clean_subject_line("Hi\nthere"),
        "Hi there",
        "subject newlines → spaces",
    )

    assert_eq(
        clean_subject_line("Hi ,  there"),
        "Hi, there",
        "subject orphan punct",
    )

    # Truncation at 998
    long_subject = "x" * 1500
    cleaned = clean_subject_line(long_subject)
    assert_true(len(cleaned) <= 998, f"long subject truncated to {len(cleaned)}")
    assert_true(cleaned.endswith("..."), "truncated with ...")


# ============================================================================
# Test: HTML renderer — table detection (audit error 3.14)
# ============================================================================

def test_table_detection_valid():
    print("\n--- Test: Valid markdown table detected ---")

    body = """Hello!

| Format       | Floor    |
|--------------|----------|
| Banner       | $0.50    |
| Interstitial | $5.00    |

Thanks!"""

    blocks = detect_table_blocks(body)
    assert_eq(len(blocks), 1, "exactly 1 table found")
    assert_eq(blocks[0].headers, ["Format", "Floor"], "headers parsed")
    assert_eq(len(blocks[0].rows), 2, "2 data rows")
    assert_eq(blocks[0].rows[0], ["Banner", "$0.50"], "first row cells")


def test_table_detection_invalid_inconsistent_cells():
    print("\n--- Test: Invalid table (inconsistent cells) rejected ---")

    body = """| A | B |
|---|---|
| 1 | 2 |
| only one cell |"""

    blocks = detect_table_blocks(body)
    assert_eq(len(blocks), 0, "inconsistent table rejected")


def test_table_detection_missing_separator():
    print("\n--- Test: Invalid table (missing separator) rejected ---")

    body = """| A | B |
| 1 | 2 |"""

    blocks = detect_table_blocks(body)
    assert_eq(len(blocks), 0, "table without separator rejected")


def test_table_detection_no_data_rows():
    print("\n--- Test: Invalid table (no data rows) rejected ---")

    body = """| A | B |
|---|---|"""

    blocks = detect_table_blocks(body)
    assert_eq(len(blocks), 0, "table with only header+separator rejected")


def test_table_detection_multiple_tables():
    print("\n--- Test: Multiple tables in body ---")

    body = """First:

| A | B |
|---|---|
| 1 | 2 |

Second:

| X | Y |
|---|---|
| 3 | 4 |"""

    blocks = detect_table_blocks(body)
    assert_eq(len(blocks), 2, "two tables detected")


# ============================================================================
# Test: HTML rendering — XSS safety (audit error 3.13)
# ============================================================================

def test_html_escape_user_content():
    print("\n--- Test: User content HTML-escaped (audit error 3.13) ---")

    malicious_body = "Hi <script>alert('xss')</script> there!"
    html_out = render_html_email(malicious_body)

    # The literal <script> should be escaped
    assert_true(
        "&lt;script&gt;" in html_out,
        "<script> escaped to &lt;script&gt;",
    )
    assert_true(
        "<script>" not in html_out,
        "no literal <script> in output",
    )


def test_html_escape_in_table_cells():
    print("\n--- Test: Table cells HTML-escaped ---")

    body = """| Name | Value |
|------|-------|
| <evil> | safe |"""

    html_out = render_html_email(body)
    assert_true(
        "&lt;evil&gt;" in html_out,
        "<evil> in cell escaped",
    )


def test_html_renders_paragraph_breaks():
    print("\n--- Test: Paragraph breaks render as <p> tags ---")

    body = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
    html_out = render_html_email(body)

    # Count <p> tags
    p_count = html_out.count("<p ")
    assert_eq(p_count, 3, "3 paragraphs → 3 <p> tags")


def test_html_inline_newlines_become_br():
    print("\n--- Test: Inline newlines render as <br> ---")

    body = "Line one\nLine two\n\nNew paragraph"
    html_out = render_html_email(body)

    # Should have <br> within the first paragraph (between lines 1 and 2)
    # and a separate <p> for "New paragraph"
    assert_true("<br>" in html_out, "<br> present for inline newlines")
    assert_eq(html_out.count("<p "), 2, "2 paragraphs (blank line separator)")


def test_html_full_email_with_table():
    print("\n--- Test: Full email with embedded table ---")

    body = """Hi Alice,

Here are the rates:

| Format       | Floor    | Ceiling  |
|--------------|----------|----------|
| Banner       | $0.50    | $1.50    |
| Interstitial | $5.00    | $12.00   |

Let me know what you think.

Daniel"""

    html_out = render_html_email(body)

    # Has table
    assert_true("<table" in html_out, "table tag present")
    assert_true("<thead>" in html_out, "thead present")
    assert_true("<tbody>" in html_out, "tbody present")

    # Has paragraphs around it
    assert_true("Hi Alice" in html_out, "first paragraph rendered")
    assert_true("Let me know" in html_out, "post-table paragraph rendered")
    assert_true("Daniel" in html_out, "signature rendered")


def test_html_empty_body():
    print("\n--- Test: Empty body returns empty string ---")

    assert_eq(render_html_email(""), "", "empty input → empty output")
    assert_eq(render_html_email(None), "", "None input → empty output")


# ============================================================================
# RUNNER
# ============================================================================

def run_all():
    print("=" * 60)
    print("Stage 3 Renderer/Cleaner Test Suite")
    print("=" * 60)

    test_orphan_punctuation()
    test_excess_newlines()
    test_excess_spaces()
    test_clean_email_body_full()
    test_clean_subject()
    test_table_detection_valid()
    test_table_detection_invalid_inconsistent_cells()
    test_table_detection_missing_separator()
    test_table_detection_no_data_rows()
    test_table_detection_multiple_tables()
    test_html_escape_user_content()
    test_html_escape_in_table_cells()
    test_html_renders_paragraph_breaks()
    test_html_inline_newlines_become_br()
    test_html_full_email_with_table()
    test_html_empty_body()

    print("\n" + "=" * 60)
    print("Test suite complete.")
    print("=" * 60)


if __name__ == "__main__":
    run_all()
