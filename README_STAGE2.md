# Stage 2 — Variant Generation & Review

Drop-in integration for the second stage of the PremiumAds Spintax Tool.
Builds on top of Stage 1 — assumes you've already run `schema_setup.py` and
have a working campaign-setup flow.

## What this stage does

Takes a saved `campaign_id` from Stage 1 → produces one fully-rendered email
(subject + body) with all variables substituted and CPM table built. User can
regenerate for fresh spins, edit inline, and approve to push to Stage 4 (Queue).

## File overview

| File | Purpose |
|---|---|
| `schema_setup_v2.py` | Adds Publishers + cpm_rates tabs, plus new columns on Campaigns/Emails |
| `stage2_spintax_engine.py` | Pure spintax engine: spin, substitute, validate, count |
| `stage2_templates.py` | All template definitions (subject + body) |
| `stage2_publishers.py` | Lookup with fallback for FIRST_NAME / PUBLISHER_NAME |
| `stage2_cpm_table.py` | Build CPM_TABLE string from cpm_rates with fallback |
| `stage2_variants.py` | Orchestrator: generates GeneratedVariant from inputs |
| `stage2_ui.py` | Streamlit UI: generate, regenerate, edit, approve |
| `test_spintax_engine.py` | 38 unit tests for engine determinism + edge cases |
| `test_stage2_integration.py` | 47 integration tests with mocked dependencies |

---

## Setup

### Step 1 — Run schema migration

```bash
python schema_setup_v2.py
```

Creates new tabs and adds new columns. Idempotent — safe to re-run. Adds:
- `Publishers` tab (recipient metadata, 9 columns)
- `cpm_rates` tab (CPM data keyed by vertical/format/geo, 7 columns)
- New column on `Campaigns`: `target_geo`
- New columns on `Emails`: `template_id`, `template_version`, `spin_path_json`, `was_edited`, `generated_at`

Also seeds the `cpm_rates` tab with 13 starter rates across Gaming/Finance/Shopping
verticals. Edit them in the Sheet to match your actual rate card.

### Step 2 — Update Stage 1 form to capture target_geo

In `stage1_ui.py`, add a GEO field somewhere in the form. Suggested location:
near the CPM section, since GEO drives which rate card applies.

```python
target_geo = st.selectbox(
    "Target GEO *",
    ["US", "UK", "DE", "FR", "IN", "BR", "MX", "JP", "KR", "AU", "Global"],
    index=0,
)
```

Add `target_geo` to `campaign_data` before calling `validate_campaign_input()`.
The validator already permits empty values for non-required fields.

### Step 3 — Wire Stage 2 into `app.py`

```python
from stage1_ui import render_stage1
from stage2_ui import render_stage2

# ... existing user auth setup ...

# Multi-stage flow
campaign_id = st.session_state.get("current_campaign_id")

if not campaign_id:
    # No active campaign — show Stage 1
    campaign_id = render_stage1()
    if not campaign_id:
        st.stop()

# Active campaign → show Stage 2
approved = render_stage2(campaign_id)

if approved:
    # Hand off to Stage 4 (Queue)
    # NEW columns to write into Emails tab:
    #   - approved.template_id
    #   - approved.template_version
    #   - approved.spin_path_json (as JSON string)
    #   - approved.was_edited
    #   - approved.subject
    #   - approved.body
    #   - approved.generated_at
    write_to_queue(approved)
```

---

## Adding new templates

Edit `stage2_templates.py`. Three things to know:

1. **Spintax syntax:** `{option1|option2|option3}` — flat only, no nesting.
   Templates are validated at module load — broken templates fail loudly.

2. **Variables:** `<<VARIABLE_NAME>>` — uppercase only. The full list:
   - System (always provided): `BRAND`, `APP_NAME`, `VERTICAL`, `FLIGHT`,
     `CPM_FLOOR`, `CPM_OFFER`, `CPM_TABLE`
   - Publisher (may use fallback): `FIRST_NAME`, `LAST_NAME`, `PUBLISHER_NAME`
   - Sender: `SENDER_NAME`, `SENDER_SIGNATURE`

3. **Bump `template_version`** when editing existing templates. The version
   gets stored alongside `spin_path_json` so future analytics can compare
   variants within a version but not across versions. (Audit error 2.14)

After editing, run `python test_spintax_engine.py` to verify the templates
parse correctly.

---

## Adding new publishers

Two options:

**From the UI:** when generating a variant for an unknown email, the UI shows
a "➕ Add to Publishers tab" expander with a quick-add form. Most ergonomic.

**Directly in Sheets:** edit the `Publishers` tab. Schema:
```
publisher_email, first_name, last_name, publisher_name, publisher_tier,
default_geo, notes, created_at, updated_at
```

Cache TTL is 30 seconds — your changes appear in the next generation.

---

## Adding new CPM rates

Edit the `cpm_rates` tab directly. Schema:
```
vertical, ad_format, geo, cpm_floor, cpm_ceiling, updated_at, notes
```

Lookup logic:
1. Exact match on `(vertical, ad_format, geo)` wins
2. Falls back to `(vertical, ad_format, "Global")` if no exact GEO match
3. Falls back to inline `Floor CPM: $X / Offer CPM: $Y` line if no rates at all

Cache TTL is 5 minutes.

---

## How determinism works

The engine seeds its RNG with `hash(campaign_id, recipient_email, regenerate_count, template_id, template_version)`. Same inputs → same output, every time.

**Implications:**
- Same campaign + same recipient + same regenerate_count = identical email
- Bump `regenerate_count` → fresh spin
- Edit the template (bump `template_version`) → fresh spin (old variants archived)
- Anyone can reproduce any past email exactly from `(campaign_id, recipient_email, regenerate_count, template_id, template_version)`

This is what makes future Stage 7 performance analytics work — variants are
identifiable and reproducible.

---

## What the Emails tab row looks like after Stage 2 approval

```
campaign_id:       d3c8a-...  (from Stage 1)
recipient_email:   alice@example.com
brand:             Nike
vertical:          Gaming
campaign_type:     Outreach
template_id:       outreach_v1
template_version:  1
subject:           Confirmed media buy for Nike — Clash Royale
body:              Hi Alice, ...
status:            Queued
queued_at:         2026-05-21T...
generated_at:      2026-05-21T...
spin_path_json:    {"subject":[{"pos":0,"text":"Confirmed media buy"}], "body":[...]}
was_edited:        false
```

Stage 4 (Queue write) and Stage 7 (tracking pivot) both read these fields.

---

## Cache invalidation behavior

| Cache | TTL | Cleared by |
|---|---|---|
| Publishers | 30s | Calling `upsert_publisher()` |
| cpm_rates | 5min | (no programmatic clear; rates change rarely) |
| Campaigns | 60s | Calling `save_campaign()` (Stage 1) |
| Emails history | 60s | Calling `save_campaign()` (Stage 1) |

If you edit a Sheets tab directly and the UI doesn't update, wait for the TTL
or restart the app.

---

## Troubleshooting

**"Campaign not found" error in Stage 2**
The `campaign_id` in session state doesn't match any row in the Campaigns tab.
Check that Stage 1's `save_campaign()` is actually writing the row before
returning the ID.

**Publisher fallback flag always fires even when publisher exists**
Should be fixed — but if you see it again, check that the email in
the recipient field matches the email in the Publishers tab exactly
(case-insensitive). Run `normalize_email()` on both to compare.

**CPM_TABLE shows fallback line even though rates exist**
Check:
1. Campaign has `target_geo` populated
2. cpm_rates tab has a row matching `(vertical, geo)` — case-insensitive
3. If geo isn't in the table, add a row with `geo="Global"` as a wildcard

**Templates fail to load (TemplateValidationError at import)**
A template in `stage2_templates.py` has invalid spintax syntax. The error
message points to the specific template_id and the issue. Common causes:
- Unbalanced braces: `{a|b` (missing `}`)
- Empty option: `{a||b}` (use trailing `|` instead: `{a|b|}`)
- Nested spintax: `{a|{b|c}}` (not supported)

**Same variant generated every time**
That's by design — determinism. Click "Regenerate" to bump the count
and produce a different spin. The spin space counter shows how many unique
variants are possible.

---

## What's next (Stage 4)

The Queue stage receives `ApprovedVariant` from Stage 2 and writes a row to the
Emails tab. Stage 4 needs to:

1. Write all columns including the new variant tracking fields
2. Generate an idempotency key (campaign_id + recipient_email + regenerate_count
   could serve as a natural key)
3. Set initial status to "Queued"
4. Record `queued_at` timestamp

The Apps Script `sendQueuedEmails()` function then picks up Queued rows on its
time-based trigger.
