# Stage 5 — Smart Queue: Priority, Multi-Sender, Rate Limiting

The queue gets smart. This is the difference between manual 50/week sending and
automated 500+/week without burning sender reputation.

**Scope shipped:** 5A (priority ordering), 5B (multi-sender rotation), 5C (rate
limiting). Deferred: 5D (smart backoff — partial done in Apps Script), 5E
(business-hours by recipient timezone — account-level windows shipped instead).

## File overview

| File | Purpose | Lines |
|---|---|---|
| `schema_setup_v4.py` | Adds sender_accounts + send_log tabs, priority_score + next_retry_at columns | 180 |
| `stage5_priority.py` | Priority score computation (tier + age → sortable int) | 165 |
| `stage5_sender_pool.py` | Hybrid account selection + rate-limit checks (Python) | 290 |
| `stage5_health.py` | Queue health metric aggregation | 250 |
| `stage5_dashboard_ui.py` | Health dashboard Streamlit view | 240 |
| `apps_script_v3.gs` | Priority sort + sender re-validation + rate limiting + lock | 430 |
| `test_stage5.py` | 38 tests for priority + sender pool + windows | 340 |

Plus a retrofit to `stage3_queue_writer.py`: assigns `priority_score` and
auto-picks a sender at queue time.

**238 tests passing across all stages.**

---

## Setup

### Step 1 — Run schema migration v4

```bash
python schema_setup_v4.py
```

Creates:
- `sender_accounts` tab — seeded with `daniel@premiumads.net` (200/day, 30/hour caps)
- `send_log` tab — empty, fills as emails send
- Two new Emails columns: `priority_score`, `next_retry_at`

### Step 2 — Replace Apps Script with v3

1. Extensions → Apps Script → paste `apps_script_v3.gs` (replaces v2)
2. Save
3. Run `sendQueuedEmails()` once manually to re-authorize
4. The 5-minute trigger from v2 still works; or re-run `installFiveMinuteTrigger()`

v3 is backward-compatible — works with rows that lack the new columns.

### Step 3 — Wire dashboard into `app.py`

```python
from stage5_dashboard_ui import render_dashboard

# Add to your view router:
elif view == "dashboard":
    next_action = render_dashboard()
    if next_action == "new_campaign":
        # reset to stage 1
        ...
    elif next_action == "view_queue":
        st.session_state["active_view"] = "queue"
        st.rerun()

# Sidebar button:
with st.sidebar:
    if st.button("📊 Dashboard"):
        st.session_state["active_view"] = "dashboard"
        st.rerun()
```

---

## How priority ordering works (5A)

Every queued row gets a `priority_score` — a single integer computed at queue
time from the campaign's `priority_tier` and the `queued_at` timestamp:

```
score = tier_weight × 1e12 − queued_at_epoch_seconds
```

- **Tier dominates:** High (3) always outranks Medium (2) always outranks Low (1),
  regardless of age. The 1e12 multiplier guarantees this.
- **Age breaks ties:** within a tier, older rows (smaller epoch) get a larger
  score → sent first (FIFO within tier).

Apps Script reads all Queued rows, sorts by `priority_score` descending, and
sends highest-first. Computing the score once at queue time (not at send time)
keeps the Apps Script fast even with thousands of rows (audit error 5.1).

---

## How multi-sender works (5B)

**Hybrid strategy** (your choice):

1. **Hash** the recipient email → a primary account. Same recipient always maps
   to the same sender (better deliverability — consistent From address).
2. If primary is **available** (active, under caps, in send window) → use it.
3. If primary is **exhausted** → round-robin among remaining available accounts,
   offset by `attempt_count` so retries rotate (audit error 5.13).
4. If **all** accounts exhausted → defer the send (audit error 5.12).

**Two-phase assignment** (audit error 5.14):
- **Queue time** (Python): `stage3_queue_writer` picks a preferred account, writes
  it to `from_account`.
- **Send time** (Apps Script): re-validates the preferred account's caps. If it's
  now exhausted (email sat in queue while account filled up), reassigns via
  round-robin. The `from_account` column is updated to reflect what actually sent.

**Single-account today:** With just `daniel@premiumads.net`, hash and round-robin
both always pick it. The rate limiter still applies (200/day, 30/hour). When you
add accounts, rotation activates automatically — no code change, just add rows to
the `sender_accounts` tab.

---

## How rate limiting works (5C)

Each account has `daily_cap` and `hourly_cap` (rolling windows, not calendar days
— audit error 5.3). The `send_log` tab records every send with a timestamp.

Before each send, both Python (assign) and Apps Script (send) count the account's
sends in the last 24h and last 1h. If either cap is hit, the account is "exhausted"
and skipped.

When all accounts are exhausted, the row's `next_retry_at` is set to +1 hour and
the row stays Queued. Apps Script skips it until the time passes.

**send_log auto-trims** beyond 10,000 rows (audit error 5.9) so the tab doesn't
grow unbounded.

---

## Adding sending accounts

Edit the `sender_accounts` tab directly, or add to `STARTER_SENDER_ACCOUNTS` in
`schema_setup_v4.py` and re-run. Schema:

```
from_account          : alex@premiumads.net  (must be a verified Gmail alias!)
display_name          : Alex @ PremiumAds
daily_cap             : 200
hourly_cap            : 30
send_window_start_utc : 0   (0-24 = always allowed)
send_window_end_utc   : 24
is_active             : TRUE
priority_order        : 1   (lower = preferred in round-robin)
notes                 :
```

**Critical:** For the `from` address to actually work, the account must be a
verified **send-as alias** in the Gmail account running the Apps Script. If it's
not an alias, Apps Script silently falls back to the script owner's address (the
send still succeeds, just from the default account). Set up aliases in Gmail
Settings → Accounts → "Send mail as" before relying on rotation.

---

## The dashboard

A read-only operational view showing:

- **Queue status** — counts by Queued/Sending/Sent/Failed/Bounced/Scheduled
- **Backlog health** — oldest queued age, estimated drain time, 24h failure rate
- **Per-account usage** — daily/hourly progress bars, active/window/exhausted status
- **Next to send** — top 5 queued rows by priority, with human-readable score
- **Recent failures** — expandable error details for debugging

All metrics computed from a single Emails read + single send_log read, cached 60s
(audit error 5.10).

---

## Failure handling (partial 5D)

Apps Script v3 classifies failures (audit error 5.4):

- **Permanent** (no such user, address rejected, mailbox unavailable, 550 5.1.1):
  → marked Bounced immediately, no retry.
- **Transient** (network, quota, anything unrecognized):
  → exponential backoff via `next_retry_at` (5 min → 10 min → 20 min), retried
  up to 3 attempts, then Bounced.

To add more permanent-error patterns, edit `PERMANENT_ERROR_PATTERNS` at the top
of `apps_script_v3.gs`.

---

## Concurrency safety (audit error 5.8)

Apps Script v3 acquires a document lock at the start of each run. If a previous
run is still going (slow Gmail, large batch), the new run exits immediately rather
than double-processing rows. The lock releases when the run finishes.

---

## Troubleshooting

**Priority order seems wrong**
- Check `priority_score` is populated on rows (run schema_setup_v4.py, re-queue)
- Old rows pre-Stage-5 have empty priority_score; the dashboard computes it on the
  fly, but Apps Script treats empty as 0 (lowest). Re-queue old rows if order matters.

**All emails still send from daniel@**
- Expected with one account. Add more to sender_accounts to activate rotation.
- If you added accounts but they're not used: check they're verified aliases in
  the sending Gmail account (see "Adding sending accounts" above).

**Emails stuck in Queued, never sending**
- Check the dashboard — is the account exhausted (daily/hourly cap hit)?
- Check `next_retry_at` — is it set to a future time (backoff or deferral)?
- Check Apps Script logs for "Another run is in progress" (lock contention) or
  "No capacity right now"

**send_log growing huge**
- It auto-trims at 10,000 rows. To change, edit `SEND_LOG_MAX_ROWS` in the script.

**Dashboard slow**
- Caches for 60s. With 5,000+ Emails rows, the first load after cache expiry takes
  ~2-3s (Sheets API). Click Refresh sparingly.

---

## Test summary

```
Stage 1 validation:     21 tests
Stage 2 spintax engine: 39 tests
Stage 2 integration:    48 tests
Stage 3 renderer:       36 tests
Stage 3 integration:    56 tests
Stage 5 priority+pool:  38 tests
────────────────────────────────
Total:                 238 tests passing
```

---

## What's next

The remaining roadmap items:

- **Stage 6** — Full multi-sender ops: alias auto-detection, sender health scoring,
  domain warm-up schedules, bounce-rate-triggered account pausing. (The
  infrastructure — sender_accounts tab, rotation logic — is already here. Stage 6
  adds the monitoring and automation layer on top.)
- **Stage 7** — Tracking: open pixels, link-click redirects, reply detection,
  per-variant engagement scoring. (`template_id`, `spin_path_json`, `was_edited`
  are already captured for this.)

Both build cleanly on the current schema. No retrofits needed.
