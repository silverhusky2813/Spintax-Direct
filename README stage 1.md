# Stage 1 — Drop-In Integration Guide

This bundle adds a validated, dedup-aware campaign setup stage to your existing
PremiumAds Spintax Tool. Each file is independent and testable.

## File overview

| File | Purpose | When to touch |
|---|---|---|
| `schema_setup.py` | Bootstrap script — creates Sheets tabs | Run **once**, then forget |
| `stage1_validation.py` | Pure validation functions | Edit to add new rules |
| `stage1_dedup.py` | Publisher contact history checks | Edit dedup windows |
| `stage1_history.py` | Campaign history + presets loader | Edit to surface new context |
| `stage1_persistence.py` | Idempotent save to Campaigns tab | Rarely touch |
| `stage1_ui.py` | Streamlit form UI | Edit to change layout |
| `test_validation.py` | Unit tests for validation logic | Run before deploying |

---

## Step 1 — Run the schema migration (ONCE)

This creates the `Campaigns`, `Presets`, and `Suppression` tabs in your Sheet,
and adds a `campaign_id` column to your existing `Emails` tab.

**Option A: Run locally**
```bash
cd /path/to/stage1
export SHEET_ID="1IbkbJfUXhS1V38WaNgemG7q9TW7FFBssXIMhPN_QQfo"
# place your service_account.json in this directory
python schema_setup.py
```

**Option B: Run from inside Streamlit (one-time)**
Add this temporarily at the top of `app.py`:
```python
from schema_setup import run_migration
if st.button("Run schema migration"):
    run_migration()
```
Click the button once, then remove it.

**Verify** by opening your Sheet — you should see new tabs `Campaigns`, `Presets`,
and `Suppression`, plus a new column A in `Emails` named `campaign_id`.

---

## Step 2 — Wire Stage 1 into your `app.py`

At the top of your existing `app.py`, add:

```python
from stage1_ui import render_stage1
```

Then somewhere near the top of your main flow (before the existing spintax UI),
add:

```python
# === STAGE 1: Campaign Setup ===
campaign_id = render_stage1()

if not campaign_id:
    # User hasn't completed Stage 1 yet — halt here
    st.stop()

# campaign_id now exists in st.session_state["current_campaign_id"]
# Use it as the foreign key for all downstream stages
```

Now every downstream stage (spintax generation, send queue) should attach this
`campaign_id` to the rows it writes.

---

## Step 3 — Pass `campaign_id` through to existing stages

In your existing `do_send()` (or wherever rows get written to the `Emails` tab):

```python
def do_send(i):
    # ... existing code ...

    row_data = {
        "campaign_id": st.session_state.get("current_campaign_id", ""),  # NEW
        "recipient_email": ...,
        "subject": ...,
        "body": ...,
        "brand": ...,
        "vertical": ...,
        "campaign_type": ...,
        "status": "Queued",
        "queued_at": datetime.now().isoformat(),
        # ... other fields ...
    }
```

This is what makes the dedup check work — past sent emails need a known
brand/vertical/campaign_type to compare against.

---

## Step 4 — Update Apps Script to write `sent_at` and `status` correctly

In your `apps_script.gs`, the `sendQueuedEmails()` function should update the
row after sending. Make sure it writes:

```javascript
// After successful GmailApp.sendEmail(...)
sheet.getRange(rowNum, statusCol).setValue("sent");
sheet.getRange(rowNum, sentAtCol).setValue(new Date().toISOString());
//                                                  ^^^^^^^^^^^^^^^^
// Use ISO format — dateutil.parser handles it, but ISO is most reliable
```

If your Apps Script currently writes raw `new Date()` (which becomes
"Mon Apr 22 2026..."), the dedup code WILL still parse it (we use
`dateutil.parser` defensively), but ISO is cleaner.

---

## Step 5 — Run the test suite

Before deploying any changes:

```bash
cd stage1
python test_validation.py
```

You should see 21 PASS lines. If any FAIL, the validation logic has a
regression — fix before pushing.

---

## How the dedup logic flows

```
User clicks "Validate & Save Draft"
            │
            ▼
   validate_campaign_input()  ──FAIL──> Show errors, halt
            │ PASS
            ▼
      is_suppressed()  ──TRUE──> Block (unsubscribed/bounced)
            │ FALSE
            ▼
  check_publisher_contact_history()
            │
            ├── status="ok"            → Show success, save
            ├── status="duplicate"     → Show warning, require confirm
            ├── status="stale_contact" → Show warning, require confirm
            └── status="no_prior_contact" → Block (FollowUp w/o prior)
            │
            ▼
       save_campaign()  → Returns campaign_id
            │
            ▼
   campaign_id → st.session_state → downstream stages
```

---

## What's deliberately deferred to later stages

| Field | Activates in | Reason |
|---|---|---|
| `priority_tier` | Stage 5 | Apps Script queue must sort by priority |
| `publisher_segment` | Future | Requires publisher tier registry first |
| `variant_strategy` | Stage 2 | Spintax generator must read this field |
| Open/click metrics | Stage 7 | Tracking pixel + redirect URLs needed |

These fields are captured NOW so when later stages activate, no backfill is
needed. The UI labels them "⏳ future use" for transparency.

---

## Cache invalidation gotchas

The dedup and history modules use `@st.cache_data` for performance:

- `_load_emails_history` — 60s TTL
- `load_campaign_history` — 60s TTL
- `_load_suppression_list` — 5min TTL
- `load_presets` — 5min TTL

`save_campaign()` calls `st.cache_data.clear()` after writing, which forces a
refresh on the next read. This is intentional — without it, a newly saved
campaign wouldn't show up in the "Recent campaigns" list until the TTL expired.

If you ever see "I saved a campaign but it's not showing up", check that
`st.cache_data.clear()` is still being called at the end of `save_campaign()`.

---

## Adding new presets

Two options:

**Quick:** edit the `STARTER_PRESETS` list in `schema_setup.py`, delete the
`Presets` tab in Sheets, re-run `run_migration()`.

**Persistent:** add rows directly to the `Presets` tab in the Sheet, following
the `PRESETS_SCHEMA` column order. The UI will pick them up after the 5-minute
cache expires (or after `save_campaign()` clears the cache).

---

## Troubleshooting

**"Worksheet not found" errors during dedup**
Run `schema_setup.py` first. The dedup code degrades gracefully (returns "ok"
when tabs don't exist), but you'll get inaccurate dedup until tabs exist.

**Dedup not catching duplicates**
Three common causes:
1. Brand strings stored unnormalized in Sheets (run a one-time normalize over
   the `Emails` tab brand column)
2. `sent_at` field empty on past rows (Apps Script wasn't writing it)
3. `status` field not set to "sent" or "delivered"

**Form keeps resetting fields after errors**
This is Streamlit form behavior — fields stay on submit only if validation
passes. To preserve values across failed submissions, replace `clear_on_submit=False`
with explicit `st.session_state` storage per field.

---

## Next: Stage 2

Once Stage 1 is live and tested, Stage 2 (spintax generation) should:
1. Read `variant_strategy` from the saved campaign
2. Tag each generated variant with the campaign_id
3. Log which subject+body combo was selected per send

This sets up the tracking foundation for Stage 7 (performance metrics).
