# PremiumAds Spintax Tool — Unified Workflow

The complete 7-stage outreach engine, wired into one runnable app.

```
streamlit run app.py
```

That's it. One entry point, one sidebar, one workflow.

---

## What this is

A consultative cold-outreach engine for mobile-monetization partnerships:
validated campaign setup → spintax-personalized variants → safety-checked
queueing → priority-ordered multi-account sending with reputation protection →
reply tracking that tells you which messaging actually wins deals.

**By the numbers:** 30 production modules, 342 passing tests, 6 schema
migrations, 1 Apps Script backend, 7 UI screens — assembled into a single app.

---

## The workflow at a glance

```
┌─────────────────── SEND FLOW (sequential) ───────────────────┐
│                                                               │
│   ① Campaign      ② Generate       ③ Confirm                  │
│      setup    ──▶    variant    ──▶   & queue                 │
│   (validate,      (spintax,        (preview, safety           │
│    dedup)          edit)            checks, write)            │
│                                                               │
└───────────────────────────────────────────────────────────────┘
                            │
                            ▼  writes row to Emails tab (status=Queued)
            ┌───────────────────────────────────┐
            │   APPS SCRIPT v5 (runs on triggers) │
            │                                     │
            │   • send  — priority order,         │
            │            sender rotation,         │
            │            rate limits, warm-up caps│
            │   • reply scan — classify, suppress,│
            │            flag engagement          │
            │   • health check — auto-pause       │
            │            unhealthy senders        │
            └───────────────────────────────────┘
                            │
        ┌───────────────────┴───────────────────┐
        ▼                                        ▼
┌─────────────── MONITORING (reachable any time) ──────────────┐
│  Queue        Health Dashboard    Analytics    Accounts      │
│  (what's      (throughput,        (variant      (sender       │
│   queued)      drain time)         reply rates)  health)      │
└───────────────────────────────────────────────────────────────┘
```

The **send flow** is sequential — you move through it for each recipient. The
**monitoring screens** are standalone, reachable any time from the sidebar.

---

## Deployment runbook

### 1. Secrets

Your Streamlit `secrets.toml` needs (you already have these from the original
tool):

```toml
sheet_id = "1IbkbJfUXhS1V38WaNgemG7q9TW7FFBssXIMhPN_QQfo"
service_account_b64 = "<base64-encoded service account JSON>"
webapp_url = "<your apps script webapp url, if used>"
```

### 2. Run all schema migrations (once)

```bash
python migrate_all.py
```

This runs all 6 migrations in order, idempotently. Creates every tab and column
the workflow needs:

- **Campaigns, Presets, Suppression** (base)
- **Publishers, cpm_rates** (Stage 2)
- **sender_accounts, send_log** (Stage 5)
- **reply_log, tracking_meta** (Stage 7)
- **account_health_log** (Stage 6)
- Plus ~25 columns added to the **Emails** tab across stages

Safe to re-run anytime.

### 3. Install the Apps Script backend

1. Open the spreadsheet → Extensions → Apps Script
2. Paste the entire contents of `apps_script_v5.gs` (replacing anything there)
3. Save
4. Run `sendQueuedEmails()` once manually → grant all permission prompts
   (Gmail send + inbox read, needed for sending and reply scanning)
5. Install the three time-based triggers (run each once from the editor):
   - `installFiveMinuteTrigger()`   → sends queued mail every 5 min
   - `installReplyScanTrigger()`    → scans for replies every 15 min
   - `installHealthCheckTrigger()`  → checks sender health every 6 hours

### 4. Run the app

```bash
streamlit run app.py
```

First screen asks who you are (audit trail). Then you're in the campaign flow.

---

## File map

### Entry points
| File | Role |
|---|---|
| `app.py` | The unified app — run this |
| `migrate_all.py` | One-command schema setup |
| `apps_script_v5.gs` | The full backend (paste into Apps Script) |

### Stage 1 — Campaign setup
`stage1_validation.py` · `stage1_dedup.py` · `stage1_history.py` ·
`stage1_persistence.py` · `stage1_ui.py`

### Stage 2 — Variant generation
`stage2_spintax_engine.py` · `stage2_templates.py` · `stage2_publishers.py` ·
`stage2_cpm_table.py` · `stage2_variants.py` · `stage2_ui.py`

### Stage 3 — Confirm & queue
`stage3_body_cleaner.py` · `stage3_html_renderer.py` ·
`stage3_presend_checks.py` · `stage3_queue_writer.py` · `stage3_ui.py`

### Stage 4 — Queue view
`stage4_queue_view.py`

### Stage 5 — Smart send
`stage5_priority.py` · `stage5_sender_pool.py` · `stage5_health.py` ·
`stage5_dashboard_ui.py`

### Stage 6 — Sender health
`stage6_warmup.py` · `stage6_health_score.py` · `stage6_enforcement.py` ·
`stage6_accounts_ui.py`

### Stage 7 — Reply tracking
`stage7_reply_classifier.py` · `stage7_subject_matcher.py` ·
`stage7_engagement.py` · `stage7_analytics_ui.py`

### Shared + schema
`time_utils.py` · `schema_setup.py` through `schema_setup_v6.py`

### Tests (8 suites, 342 tests)
`test_validation.py` · `test_spintax_engine.py` · `test_stage2_integration.py` ·
`test_stage3_renderer.py` · `test_stage3_integration.py` · `test_stage5.py` ·
`test_stage6.py` · `test_stage7.py`

---

## Daily usage

**To send outreach:**
1. Sidebar → "➕ New campaign"
2. Stage 1: enter brand, app, vertical, CPM, flight, recipient → Validate & Save
3. Stage 2: review the generated variant, regenerate or edit → Approve
4. Stage 3: check the inbox preview, clear the safety checks → Confirm & Queue
5. Click "Send to another publisher" to start the next one — this returns to
   Stage 1 with a clean slate; use its "Load from recent campaign" option to
   reuse the brand/vertical/CPM settings and just change the recipient

Apps Script sends queued mail automatically every 5 minutes.

**To monitor:**
- **Queue** — did my emails send? what failed?
- **Dashboard** — throughput, how long to clear the backlog, per-account usage
- **Analytics** — which templates/subjects get the most replies (the strategy view)
- **Accounts** — sender health; run a health check; reactivate paused accounts

---

## Running the tests

```bash
for t in test_validation test_spintax_engine test_stage2_integration \
         test_stage3_renderer test_stage3_integration test_stage5 \
         test_stage6 test_stage7; do
  python $t.py
done
```

All 342 should pass. Run these after any edit to catch regressions.

---

## The feedback loop (why this is more than a mailer)

```
   send  ──▶  measure (Analytics: which variants reply)  ──▶  refine templates
     ▲                                                              │
     └──────────────────────────────────────────────────────────────┘
```

Stage 7's analytics tell you which spintax openers and templates actually
generate publisher replies — with sample-size discipline so you don't chase
noise. That insight feeds directly into how you write the next batch of
templates in `stage2_templates.py`. Send → measure → refine → send better.

That's the consultative-outreach flywheel: every campaign teaches you something
that makes the next one land harder.
