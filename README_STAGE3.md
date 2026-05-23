# Stages 3 & 4 — Confirm, Queue, View

Per your answer to Q2, Stage 3 and Stage 4 are merged UX-wise into a single
"Confirm & Queue" flow. The "Stage 4" name lives on as the **read-only Queue
view** — a separate screen for browsing queued/sent emails.

## What these stages do

**Stage 3** (combined preview/confirm/queue):
- Render the variant as a polished HTML email (with embedded CPM table)
- Show inbox preview + plain-text fallback
- Run fresh pre-send safety checks (suppression, dedup, idempotency)
- Write the row to the Emails tab idempotently
- Offer post-confirm navigation (send another / new campaign / view queue)

**Stage 4 (Queue View)**:
- Read-only browser of all queued/sent/failed emails
- Filter by status, campaign ID
- See variant tracking metadata
- Visual indicators for retries and failures

## File overview

| File | Purpose | Lines |
|---|---|---|
| `time_utils.py` | Shared Python ↔ Apps Script timestamp parsing | 109 |
| `schema_setup_v3.py` | Adds Stage 3 columns to Emails tab | 81 |
| `stage3_body_cleaner.py` | Whitespace + orphan punctuation cleanup | 138 |
| `stage3_html_renderer.py` | Plain text + markdown table → safe HTML email | 274 |
| `stage3_presend_checks.py` | Fresh suppression/dedup/idempotency checks | 287 |
| `stage3_queue_writer.py` | Idempotent insert/update to Emails tab | 233 |
| `stage3_ui.py` | Combined preview + confirm + queue Streamlit UI | 232 |
| `stage4_queue_view.py` | Read-only queue browser with filters | 219 |
| `apps_script_v2.gs` | Backward-compatible Apps Script for new schema | 246 |
| `test_stage3_renderer.py` | 36 tests for cleaner + HTML renderer | 246 |
| `test_stage3_integration.py` | 56 tests for queue logic + time_utils | 390 |

**200 tests passing across all stages.**

---

## Setup

### Step 1 — Run schema migration v3

```bash
python schema_setup_v3.py
```

Adds 7 new columns to the existing Emails tab:
`html_body`, `from_account`, `idempotency_key`, `confirmed_at`,
`last_attempt_at`, `attempt_count`, `error_message`.

Idempotent — safe to re-run.

### Step 2 — Replace Apps Script

1. Open the spreadsheet → Extensions → Apps Script
2. Paste the contents of `apps_script_v2.gs` (replaces existing code)
3. Save
4. Run `sendQueuedEmails()` manually once to grant Gmail permissions
5. Optional: run `installFiveMinuteTrigger()` to auto-send every 5 min

**Backward-compatible** — works with both old rows (missing the new columns)
and new rows. Existing Failed rows can be reset with `resetFailedRowsToQueued()`.

### Step 3 — Wire into `app.py`

The full multi-stage flow now looks like:

```python
import streamlit as st
from stage1_ui import render_stage1
from stage2_ui import render_stage2
from stage3_ui import render_stage3
from stage4_queue_view import render_queue_view

# Pick which screen to show based on session state
view = st.session_state.get("active_view", "stage1")

if view == "queue":
    next_action = render_queue_view()
    if next_action == "new_campaign":
        st.session_state.pop("active_view", None)
        st.session_state.pop("current_campaign_id", None)
        st.session_state.pop("current_approved", None)
        st.rerun()

elif view == "stage3":
    approved = st.session_state.get("current_approved")
    if not approved:
        st.session_state["active_view"] = "stage1"
        st.rerun()

    next_action = render_stage3(approved)
    if next_action == "send_another":
        # Keep campaign_id, clear recipient-level state
        st.session_state.pop("current_approved", None)
        st.session_state["active_view"] = "stage2"
        st.rerun()
    elif next_action == "new_campaign":
        for k in ["current_campaign_id", "current_approved", "active_view"]:
            st.session_state.pop(k, None)
        st.rerun()
    elif next_action == "view_queue":
        st.session_state["active_view"] = "queue"
        st.rerun()
    elif next_action == "back_to_stage2":
        st.session_state["active_view"] = "stage2"
        st.rerun()

elif view == "stage2":
    campaign_id = st.session_state.get("current_campaign_id")
    if not campaign_id:
        st.session_state["active_view"] = "stage1"
        st.rerun()

    approved = render_stage2(campaign_id)
    if approved:
        st.session_state["current_approved"] = approved
        st.session_state["active_view"] = "stage3"
        st.rerun()

else:  # stage1 (default)
    campaign_id = render_stage1()
    if campaign_id:
        st.session_state["current_campaign_id"] = campaign_id
        st.session_state["active_view"] = "stage2"
        st.rerun()

# Always-available "View Queue" button in sidebar
with st.sidebar:
    if st.button("📋 View Queue"):
        st.session_state["active_view"] = "queue"
        st.rerun()
```

---

## How the HTML email works

The Stage 2 plain-text body contains a markdown table for CPMs:

```
| Format       | Floor    | Ceiling  |
|--------------|----------|----------|
| Banner       | $0.50    | $1.50    |
```

Gmail does **not** render markdown. So Stage 3:

1. Detects markdown table blocks (strict: header + separator + data rows)
2. Converts them to `<table>` with inline CSS
3. HTML-escapes all other text (XSS-safe — audit error 3.13)
4. Wraps paragraphs in `<p>`, line breaks in `<br>`

Result: a polished email with a real HTML table when viewed in any modern
email client. The plain-text body is preserved as a fallback for old clients.

**What recipients see** (Gmail web/iOS/Outlook):
- Clean paragraphs with proper spacing
- Styled CPM table with bordered cells
- All content in a clean sans-serif font

**What recipients see** (plain-text-only clients, ~2% of inboxes):
- Same content as plain text, with markdown table syntax visible
- Still readable, just not pretty

---

## Idempotency logic

The key is a 16-char SHA-256 hash of `(campaign_id, recipient_email)`. Same
key = same logical email. **Different regenerate counts don't change the key.**

| Existing row status | New action |
|---|---|
| (no existing row) | INSERT new row |
| Queued / Sending | BLOCK — already in queue |
| Sent / Delivered | BLOCK — already sent |
| Failed / Bounced | OFFER RETRY — UPDATE in place, attempt_count++ |

This means double-clicking Confirm is safe (the second click sees the Queued
row and blocks). Browser back-and-forth doesn't create duplicates. Failed
emails can be retried without creating new rows.

---

## Pre-send checks — what fires and when

| Check | Cache behavior | Outcome |
|---|---|---|
| Suppression | **Fresh read** (bypass cache) | Block if recipient unsubscribed |
| Dedup | **Fresh read** (bypass cache) | Warn if recently contacted, block FollowUp without prior |
| Idempotency | Direct Sheets lookup | Block dup, warn for retry, OK otherwise |

The reason for fresh reads: between Stage 1 and Stage 3, anything could have
changed (different user, different tab, time passed). Re-checking is a few
hundred ms — worth it.

---

## Field-by-field schema (what gets written)

```
campaign_id        ← from Stage 1
recipient_email    ← from Stage 2 (normalized)
idempotency_key    ← SHA-256 of (campaign_id, email)
brand, vertical, app_name, campaign_type  ← denormalized from campaign

template_id, template_version       ← from Stage 2 variant tracking
spin_path_json                      ← JSON-serialized spin choices
was_edited                          ← "TRUE" or "FALSE" string
generated_at                        ← Stage 2 timestamp

subject, body         ← post-cleaner (orphan punct fixed)
html_body             ← Stage 3 HTML renderer output

from_account          ← Sender (default: daniel@premiumads.net)

status                ← "Queued" on insert, Apps Script updates
queued_at             ← First time we wrote this row (preserved on retry)
confirmed_at          ← When user clicked Confirm (updates on retry)
attempt_count         ← 0 on insert, ++on each Apps Script attempt
error_message         ← Populated by Apps Script if send fails
sent_at               ← Set by Apps Script on success
last_attempt_at       ← Set by Apps Script on each attempt
```

---

## Troubleshooting

**Apps Script doesn't pick up rows**
- Check the `status` column says exactly "Queued" (case-sensitive)
- Verify the script has Gmail permissions (run `sendQueuedEmails()` manually first)
- Check Apps Script logs (View → Logs) for parse errors

**"Cannot send: column missing" in Apps Script logs**
- Run `schema_setup_v3.py` to add the new columns
- Old rows that pre-date the schema will still send fine — the new columns
  default to empty for them

**Emails arrive without HTML formatting**
- Check the `html_body` column has content for that row
- If empty, the variant was generated with an older Stage 3 (or schema_setup_v3
  hadn't been run yet)
- Apps Script falls back to plain-text-only if `html_body` is empty — no crash

**Failed rows keep retrying forever**
- Apps Script caps at 3 attempts (`MAX_ATTEMPTS` in the script)
- After 3 failures, row gets marked "Bounced" and stops being picked up
- To manually retry: clear the `attempt_count` and reset status to "Queued"

**Queue view is slow with many rows**
- Default limit is 100 rows — increase via the UI control if needed
- Sheets API is the bottleneck; 500+ rows = ~3 second load time

**Markdown table alignment looks broken in plain-text fallback**
The body cleaner collapses runs of spaces, which removes the visual alignment
in markdown tables. The HTML render is unaffected (the table converts to
`<table>` first). If alignment-preserved plain text matters for your use case,
exempt table-row lines from `collapse_excess_spaces` in stage3_body_cleaner.py.

---

## Test summary

```
Stage 1 validation:     21 tests
Stage 2 spintax engine: 39 tests
Stage 2 integration:    48 tests
Stage 3 renderer:       36 tests
Stage 3 integration:    56 tests
────────────────────────────────
Total:                 200 tests passing
```

Run any suite individually:
```bash
python test_validation.py
python test_spintax_engine.py
python test_stage2_integration.py
python test_stage3_renderer.py
python test_stage3_integration.py
```

---

## What's next

The send pipeline is now complete end-to-end:

```
Stage 1 (campaign setup)
    ↓
Stage 2 (generate variant)
    ↓
Stage 3 (preview + confirm + queue)
    ↓
Apps Script (auto-send every 5 min)
    ↓
Stage 4 view (monitor results)
```

Future stages (referenced in the original decomposition):

- **Stage 5** — Smarter queue: priority sorting, sender rotation, rate limiting beyond MAX_SENDS_PER_RUN
- **Stage 6** — Multi-sender setup: alias rotation, sender health monitoring
- **Stage 7** — Tracking: open pixel, link redirects, reply detection, engagement scoring

The schema is ready for all three. `from_account` enables Stage 6.
`template_id` / `spin_path_json` enable Stage 7's variant analysis.
