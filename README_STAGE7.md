# Stage 7 — Reply Tracking & Variant Analytics

Closes the loop. Sending well is useless if you don't know what worked. Stage 7
detects replies, classifies them, and ties engagement back to the variant
metadata captured since Stage 2 — so you finally know *which messaging gets deals*.

**Scope shipped:** reply detection only (7C+7D+7E). Open/click tracking (7A/7B)
deliberately skipped — they need a web server, and post-Apple-MPP they're mostly
noise anyway. Replies are the metric that closes deals.

## File overview

| File | Purpose | Lines |
|---|---|---|
| `schema_setup_v5.py` | Adds reply columns to Emails; reply_log + tracking_meta tabs | 110 |
| `stage7_reply_classifier.py` | Classify inbound: genuine/auto/bounce/unsubscribe | 180 |
| `stage7_subject_matcher.py` | Match replies to sent rows (thread ID + subject fallback) | 165 |
| `stage7_engagement.py` | Per-variant aggregation with sample-size guards | 290 |
| `stage7_analytics_ui.py` | Variant performance dashboard | 280 |
| `apps_script_v4.gs` | Thread ID capture at send + inbox reply scan | 620 |
| `test_stage7.py` | 61 tests for classifier, matcher, engagement | 430 |

**299 tests passing across all stages.**

---

## Setup

### Step 1 — Run schema migration v5

```bash
python schema_setup_v5.py
```

Adds to Emails: `thread_id`, `reply_status`, `replied_at`, `reply_snippet`.
Creates `reply_log` tab (idempotency) and `tracking_meta` tab (scan watermark).

### Step 2 — Replace Apps Script with v4

1. Paste `apps_script_v4.gs` over v3 in the Apps Script editor
2. Run `sendQueuedEmails()` once manually — **it now needs Gmail read scope**
   (for the reply scan), so you'll get a new authorization prompt. Accept it.
3. Run `installReplyScanTrigger()` to scan for replies every 15 minutes
4. Your existing 5-minute send trigger keeps working

**Important:** v4 changes the send path from `GmailApp.sendEmail()` to
`createDraft().send()` so it can capture the thread ID. Functionally identical
send, but now every sent row records its Gmail `thread_id` — which makes reply
matching reliable.

### Step 3 — Wire analytics into `app.py`

```python
from stage7_analytics_ui import render_analytics

elif view == "analytics":
    next_action = render_analytics()
    if next_action == "new_campaign":
        # reset to stage 1
        ...
    elif next_action == "dashboard":
        st.session_state["active_view"] = "dashboard"
        st.rerun()

# Sidebar:
with st.sidebar:
    if st.button("📈 Analytics"):
        st.session_state["active_view"] = "analytics"
        st.rerun()
```

---

## How reply detection works

**At send time** (Apps Script v4): every email is sent via draft-then-send,
capturing the Gmail `thread_id` onto the Emails row.

**Every 15 minutes** (the scan trigger): `scanReplies()` runs:

1. Searches the inbox (only threads newer than the last scan — watermark in
   `tracking_meta`, audit error 7.10)
2. For each inbound message not already processed (idempotency via `reply_log`,
   audit error 7.17):
   - Skips messages we sent ourselves
   - Matches to a sent row — **thread ID first, subject+recipient fallback**
     (audit error 7.4). When multiple sends share a thread, matches the most
     recent (audit error 7.13)
   - Classifies: genuine / auto_reply / bounce / unsubscribe (audit error 7.3)
   - Writes `reply_status`, `replied_at`, `reply_snippet` to the matched row
   - If unsubscribe or bounce → adds to the Suppression tab automatically

---

## Reply classification

Precedence (first match wins): **bounce > unsubscribe > auto_reply > genuine.**

| Status | Detected by | Effect |
|---|---|---|
| `bounce` | mailer-daemon/postmaster sender, or delivery-failure content | Suppressed; counts negatively |
| `unsubscribe` | "unsubscribe", "remove me", "do not contact"… | Suppressed; respects opt-out |
| `auto_reply` | "out of office", "automatic reply", multi-locale OOO | Neutral; not counted as engagement |
| `genuine` | anything else from a human | **The signal that matters** |

Patterns live at the top of both `stage7_reply_classifier.py` (Python, source of
truth + tests) and `apps_script_v4.gs` (JS, mirrored for the live scan). To add
a locale or variant, edit both lists.

The classifier is deliberately extensible — auto-reply patterns cover English,
German, French, Italian, Spanish, Portuguese out of the box.

---

## The analytics dashboard

A dedicated screen (separate from Stage 5's operational health view):

- **Overall** — sent, genuine reply rate, bounce rate, full response breakdown
- **Top performers** — best variant + best subject opener (only when sample is
  sufficient — audit errors 7.7, 7.15)
- **Per-variant** — every template×version with reply rate, bounce rate, sample
  flag. Variants below 20 sends shown but flagged "low sample" and not ranked
- **Subject openers** — which subject-line spin choice drives replies (the
  spintax optimization payoff)
- **Per-campaign** — reply rate by campaign

### The sample-size discipline (why this matters)

"Variant A got 2 replies, Variant B got 1" means nothing at n=3. The dashboard
**refuses to declare winners below 20 sends per variant** (`MIN_SAMPLE_FOR_RANKING`).
Low-sample variants still show their numbers — flagged with ⚠️ and an asterisk —
but they're sorted to the bottom and excluded from "best performer" callouts.

This keeps you from chasing noise. As a data-driven operator, you'll appreciate
that the tool won't let you over-interpret 3 data points.

### Variant comparison is version-aware

Variants are grouped by `(template_id, template_version)`. A spin path from
`outreach_v1` version 1 is **not** compared against version 2 — because editing
the template shifts what each spin choice means (audit error 7.16). When you
revise a template, bump its version and the analytics correctly treat it as a
fresh variant.

---

## What feeds the analytics

Every sent row already carries (since Stage 2/3):
- `template_id` + `template_version` — which template
- `spin_path_json` — the exact spin choices made
- `was_edited` — whether the user hand-edited

Stage 7 adds `reply_status`. Combined, you can answer:
- Which template gets the most replies?
- Which subject opener wins?
- Do hand-edited emails outperform untouched variants? (data's there for a
  future cut)
- Which campaigns/brands resonate?

---

## Troubleshooting

**No replies showing up**
- Confirm `scanReplies()` ran (Apps Script → Executions log)
- Confirm the reply scan trigger is installed (`installReplyScanTrigger()`)
- Replies to emails sent BEFORE the v4 retrofit have no `thread_id` — they'll
  match via subject fallback only, which requires the reply subject to contain
  the original (after stripping Re:/Fwd:)

**Genuine replies misclassified as auto_reply**
- Check the AUTO_REPLY_PATTERNS list — a phrase in the reply matched. Remove
  over-broad patterns from both the .py and .gs files

**A reply matched the wrong sent email**
- If the recipient got multiple campaigns, the scan matches the most recent send
  in the thread. If thread IDs are missing (pre-v4), subject matching can be
  ambiguous — the v4 retrofit fixes this going forward

**Unsubscribes not being suppressed**
- Check the Suppression tab is named exactly "Suppression"
- Check `scanReplies()` has run since the unsubscribe arrived
- The classifier must tag it `unsubscribe` — verify the reply contained an
  opt-out phrase

**Analytics dashboard slow**
- Cached 2 min. First load after expiry pulls all Emails rows (~2-3s at 5k rows)

---

## Privacy note

Reply tracking reads your own Gmail inbox — no third-party tracking, no pixels,
no recipient-side surveillance. This is the privacy-cleanest form of engagement
tracking. (It's also why we skipped opens/clicks: those require embedding
trackers in the recipient's view, which carries GDPR/CAN-SPAM disclosure
considerations for cold B2B outreach — audit error 7.8.)

---

## Test summary

```
Stage 1 validation:      21 tests
Stage 2 spintax engine:  39 tests
Stage 2 integration:     48 tests
Stage 3 renderer:        36 tests
Stage 3 integration:     56 tests
Stage 5 priority+pool:   38 tests
Stage 7 reply+analytics: 61 tests
─────────────────────────────────
Total:                  299 tests passing
```

---

## The full pipeline is now complete

```
Stage 1 (campaign setup)
    ↓
Stage 2 (generate variant + edit)
    ↓
Stage 3 (preview + safety checks + queue)
    ↓
Apps Script v4 (priority send, rotation, rate limiting → captures thread_id)
    ↓
Reply scan (every 15 min → classifies + suppresses + flags engagement)
    ↓
Stage 5 dashboard (operational health)  +  Stage 7 analytics (what worked)
    ↓
[feedback loop: best variants inform the next campaign's templates]
```

The loop closes. Stage 7's analytics tell you which spintax variants and subject
openers actually get replies — which directly informs how you write the next
batch of templates. That's the consultative-outreach flywheel: send → measure →
refine → send better.

Remaining roadmap item: **Stage 6** (sender-ops automation — alias health
scoring, domain warm-up, bounce-triggered account pausing). The infrastructure
is all in place (`sender_accounts`, `send_log`, bounce classification); Stage 6
would add the monitoring/automation layer. But with one sending account today,
it's not urgent — revisit when you scale past 2-3 accounts.
