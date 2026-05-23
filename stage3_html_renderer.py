"""
stage3_html_renderer.py
=========================
Convert plain-text email body (with embedded markdown CPM table) → safe HTML.

Solves audit errors:
  - 3.1: Gmail doesn't render markdown — we convert to HTML <table>
  - 3.13: User edits could contain HTML/scripts — we html-escape everything
          EXCEPT the recognized markdown table block
  - 3.14: Fragile table detection — strict pattern match, graceful fallback

Strategy:
  1. Split body into segments by blank lines (paragraphs)
  2. For each segment, detect if it's a markdown table (strict rules)
  3. Convert markdown tables → HTML tables with inline CSS
  4. HTML-escape all non-table content
  5. Wrap newlines in non-table content with <br>
  6. Assemble into a clean HTML document with email-safe styling

This is plain HTML email, NOT a web page. Inline CSS only. No external
resources. Tested against Gmail rendering (markdown tables don't survive,
HTML tables do).
"""

import html
import re
from dataclasses import dataclass
from typing import Optional


# ============================================================================
# TABLE DETECTION
# ============================================================================

# Strict markdown table line: starts with `|`, ends with `|`, has at least 2 cells
TABLE_LINE_PATTERN = re.compile(r"^\s*\|.+\|\s*$")

# Separator line: |---|---| (the second row of a markdown table)
TABLE_SEPARATOR_PATTERN = re.compile(r"^\s*\|[\s\-:|]+\|\s*$")


@dataclass
class TableBlock:
    """A detected markdown table within the body."""
    start_line: int        # Line index where the table starts
    end_line: int          # Line index AFTER the last table line (exclusive)
    headers: list[str]
    rows: list[list[str]]


def _parse_table_row(line: str) -> list[str]:
    """Parse a single markdown table row → list of cell values."""
    # Strip outer pipes and split on internal pipes
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


def detect_table_blocks(body: str) -> list[TableBlock]:
    """
    Find all valid markdown table blocks in `body`.

    A valid block:
      - Has at least 3 lines: header, separator, 1+ data row
      - All lines match TABLE_LINE_PATTERN
      - Second line matches TABLE_SEPARATOR_PATTERN
      - All rows have the same number of cells

    Invalid blocks are NOT returned — caller treats them as regular text.
    This makes the renderer resilient to user-edited broken tables.
    """
    lines = body.split("\n")
    blocks: list[TableBlock] = []

    i = 0
    while i < len(lines):
        if not TABLE_LINE_PATTERN.match(lines[i]):
            i += 1
            continue

        # Found a potential table start
        block_start = i
        # Need at least 3 lines for a valid table
        if i + 2 >= len(lines):
            i += 1
            continue

        # Line i+1 must be the separator
        if not TABLE_SEPARATOR_PATTERN.match(lines[i + 1]):
            i += 1
            continue

        # Walk forward to find the end of the table
        block_end = i + 2
        while block_end < len(lines) and TABLE_LINE_PATTERN.match(lines[block_end]):
            block_end += 1

        # Need at least one data row
        if block_end <= i + 2:
            i += 1
            continue

        # Parse cells
        headers = _parse_table_row(lines[i])
        rows = [_parse_table_row(lines[r]) for r in range(i + 2, block_end)]

        # Validate cell count consistency
        if not all(len(row) == len(headers) for row in rows):
            i += 1
            continue

        blocks.append(TableBlock(
            start_line=block_start,
            end_line=block_end,
            headers=headers,
            rows=rows,
        ))
        i = block_end

    return blocks


# ============================================================================
# HTML BUILDING BLOCKS (inline CSS for email compatibility)
# ============================================================================

# Email clients vary wildly in CSS support. Inline styles only.
# Style choices follow "lowest common denominator" of Gmail web, iOS Mail,
# Outlook, and Apple Mail.

STYLE_BODY = (
    "font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, "
    "Arial, sans-serif; font-size: 14px; line-height: 1.5; color: #1a1a1a; "
    "max-width: 600px; margin: 0;"
)

STYLE_PARA = "margin: 0 0 16px 0;"

STYLE_TABLE = (
    "border-collapse: collapse; margin: 16px 0; font-size: 13px; "
    "border: 1px solid #d0d0d0;"
)

STYLE_TH = (
    "background-color: #f5f5f5; padding: 8px 12px; text-align: left; "
    "border: 1px solid #d0d0d0; font-weight: 600;"
)

STYLE_TD = "padding: 8px 12px; border: 1px solid #d0d0d0;"


def render_table_html(block: TableBlock) -> str:
    """Render a TableBlock as inline-styled HTML."""
    lines = [f'<table style="{STYLE_TABLE}">']

    # Headers
    lines.append("<thead><tr>")
    for h in block.headers:
        lines.append(f'<th style="{STYLE_TH}">{html.escape(h)}</th>')
    lines.append("</tr></thead>")

    # Rows
    lines.append("<tbody>")
    for row in block.rows:
        lines.append("<tr>")
        for cell in row:
            lines.append(f'<td style="{STYLE_TD}">{html.escape(cell)}</td>')
        lines.append("</tr>")
    lines.append("</tbody>")

    lines.append("</table>")
    return "".join(lines)


def render_paragraph_html(text: str) -> str:
    """
    Render a paragraph of plain text as escaped HTML with <br> for newlines.

    All HTML special chars escaped — no XSS from user-edited body content
    (audit error 3.13).
    """
    escaped = html.escape(text)
    # Preserve single newlines within a paragraph as <br>
    escaped = escaped.replace("\n", "<br>\n")
    return f'<p style="{STYLE_PARA}">{escaped}</p>'


# ============================================================================
# MAIN RENDERER
# ============================================================================

def render_html_email(plain_text_body: str) -> str:
    """
    Convert a plain-text email body (with embedded markdown CPM table) → HTML.

    Returns a complete HTML email body wrapped in a <div> with email-safe styling.
    Does NOT include <html>/<body> tags — that's a job for the email send layer
    (Apps Script's GmailApp.sendEmail handles the wrapping).

    Args:
        plain_text_body: The cleaned plain-text body (post-spintax, post-cleaning)

    Returns:
        HTML string ready for use as Gmail's htmlBody parameter.
    """
    if not plain_text_body:
        return ""

    lines = plain_text_body.split("\n")
    tables = detect_table_blocks(plain_text_body)

    # Build a set of line indices that are inside a table
    table_lines = set()
    table_blocks_by_start: dict[int, TableBlock] = {}
    for block in tables:
        table_blocks_by_start[block.start_line] = block
        for li in range(block.start_line, block.end_line):
            table_lines.add(li)

    # Walk through lines, splitting into paragraphs (blank-line separated)
    # and tables. Render each appropriately.
    html_parts: list[str] = [f'<div style="{STYLE_BODY}">']

    i = 0
    while i < len(lines):
        # Table starts here?
        if i in table_blocks_by_start:
            block = table_blocks_by_start[i]
            html_parts.append(render_table_html(block))
            i = block.end_line
            continue

        # Skip blank lines (paragraph separators — we use <p> spacing instead)
        if not lines[i].strip():
            i += 1
            continue

        # Gather consecutive non-table, non-blank lines into a paragraph
        para_lines: list[str] = []
        while i < len(lines) and lines[i].strip() and i not in table_lines:
            para_lines.append(lines[i])
            i += 1

        if para_lines:
            paragraph_text = "\n".join(para_lines)
            html_parts.append(render_paragraph_html(paragraph_text))

    html_parts.append("</div>")
    return "".join(html_parts)


# ============================================================================
# PREVIEW HELPER (for Stage 3 UI)
# ============================================================================

def make_inbox_preview_html(subject: str, html_body: str, from_account: str,
                             to_email: str) -> str:
    """
    Wrap the HTML body in a fake inbox preview frame for the UI.

    This is NOT what gets sent — it's how the UI shows the user "this is what
    your recipient will see." The frame mimics Gmail's preview pane.
    """
    escaped_subject = html.escape(subject)
    escaped_from = html.escape(from_account)
    escaped_to = html.escape(to_email)

    return f"""
    <div style="border: 1px solid #d0d0d0; border-radius: 8px;
                background: white; max-width: 700px;">
      <div style="background: #f5f5f5; padding: 12px 16px;
                  border-bottom: 1px solid #d0d0d0; border-radius: 8px 8px 0 0;">
        <div style="font-weight: 600; font-size: 15px;
                    color: #1a1a1a; margin-bottom: 4px;">{escaped_subject}</div>
        <div style="font-size: 12px; color: #5f6368;">
          From: <strong>{escaped_from}</strong> • To: {escaped_to}
        </div>
      </div>
      <div style="padding: 20px;">
        {html_body}
      </div>
    </div>
    """
