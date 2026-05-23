# Stage 6 — Sender Health, Warm-up & Auto-Pause

Sender-ops automation. Monitors account reputation, ramps new accounts safely,
and auto-pauses senders before bounce rates damage your deliverability.

**Scope shipped:** full suite — health scoring, warm-up ramp, both alerting and
auto-pause. Built complete (your choice: "build everything, grow into it").

**Honest note:** most of this is dormant with one sending account — auto-pause
won't fire on your only account (by design), and warm-up defaults off for
existing accounts. It activates the moment you add accounts. The value today is
the **safety net** (you'll get alerted if your account's bounce rate climbs) and
the **readiness** (add an account, flip `warmup_enabled=TRUE`, and the ramp +
health monitoring just work).

## File overview

| File | Purpose | Lines |
|---|---|---|
| `schema_setup_v6.py` | Adds warm-up + pause columns to sender_accounts; account_health_log tab | 130 |
| `stage6_warmup.py` | Warm-up ramp schedule (pure functions) | 140 |
| `stage6_health_score.py` | Bounce-rate health scoring + auto-pause decisions (pure) | 245 |
| `stage6_enforcement.py` | Applies pause/reactivate decisions to Sheets | 175 |
| `stage6_accounts_ui.py` | Account management dashboard | 235 |
| `apps_script_v5.gs` | Warm-up cap enforcement at send + scheduled health check | 870 |
| `test_stage6.py` | 43 tests for warm-up + health + all guards | 360 |

**342 tests passing across all stages.**

---

## Setup

### Step 1 — Run schema migration v6

```bash
python schema_setup_v6.py
```

Adds to sender_accounts: `activated_at`, `warmup_enabled`, `paused_reason`,
`paused_at`, `reactivated_at`. Backfills existing accounts with today's date and
`warmup_enabled=FALSE` (presumed already warm). Creates `account_health_log`.

### Step 2 — Replace Apps Script with v5

1. Paste `apps_script_v5.gs` over v4 in the Apps Script editor
2. Run `sendQueuedEmails()` once to re-authorize
3. Run `installHealthCheckTrigger()` to check account health every 6 hours
4. Existing send + reply-scan triggers keep working

### Step 3 — Wire the accounts dashboard into `app.py`

```python
from stage6_accounts_ui import render_accounts

elif view == "accounts":
    next_action = render_accounts()
    if next_action == "new_campaign":
        ...  # reset
    elif next_action == "dashboard":
        st.session_state["active_view"] = "dashboard"; st.rerun()
    elif next_action == "analytics":
        st.session_state["active_view"] = "analytics"; st.rerun()

with st.sidebar:
    if st.button("✉️ Accounts"):
        st.session_state["active_view"] = "accounts"; st.rerun()
```

---

## How health scoring works

Computed over the trailing **7 days**, per account:

- **Below 20 sends in window** → `insufficient_data`, no action (audit 6.2 —
  don't panic over 1 bounce in 2 sends)
- **Bounce rate < 3%** → healthy
- **3–5%** → warning → **alert only**
- **Above 5%** → critical → **auto-pause candidate**

### Auto-pause guards (the safety rails)

1. **Last-account guard (6.1):** never auto-pauses your last active account —
   that would halt all sends. Instead it alerts hard and tells you to fix list
   quality or add an account.

2. **Reactivation grace (6.7):** after you manually reactivate a paused account,
   a 24-hour grace window prevents the next health check from instantly
   re-pausing it (the 7d bounce rate is still high right after reactivation).

3. **Enforcement is deliberate (6.6):** the dashboard *shows* health on render
   but only *pauses* when you click "Run check & enforce" or the scheduled
   Apps Script `checkAccountHealth()` runs. No surprise pauses from passive
   page loads.

---

## How warm-up works

New sending accounts/domains that blast full volume on day 1 get flagged as
spam. The warm-up ramp scales daily caps over ~4 weeks:

| Day | Cap |
|-----|-----|
| 1–2 | 20/day |
| 3–4 | 40/day |
| 5–7 | 60/day |
| 8–11 | 100/day |
| 12–15 | 150/day |
| 16–21 | 200/day |
| 22–28 | 300/day |
| 29+ | full configured cap |

**Effective cap = min(configured cap, warm-up cap)** — warm-up only ever
*lowers*, never raises (audit 6.3). Enforced at send time in Apps Script v5 AND
reflected in the Python sender pool.

**To warm up a new account:** add the row, set `warmup_enabled=TRUE` and
`activated_at` to today. The ramp handles the rest. Existing accounts default to
warm-up OFF.

---

## Adding a second sending account (the real Stage 6 payoff)

1. In Gmail (the account running Apps Script): Settings → Accounts →
   "Send mail as" → add the new address as a verified alias. **This is required**
   for the `from` to actually work.
2. Add a row to `sender_accounts`:
   ```
   from_account:    alex@premiumads.net
   display_name:    Alex @ PremiumAds
   daily_cap:       200
   hourly_cap:      30
   is_active:       TRUE
   priority_order:  1
   warmup_enabled:  TRUE          ← ramp the new account
   activated_at:    2026-05-22    ← today
   ```
3. Done. Stage 5's rotation picks it up immediately; Stage 6 warms it up and
   monitors its health. Auto-pause now has a fallback account, so it can
   actually fire if needed.

---

## What auto-pause looks like in practice

1. `alex@premiumads.net` bounce rate climbs to 6% over 7 days (≥20 sends)
2. Scheduled `checkAccountHealth()` runs (every 6h)
3. Since there's another active account, it pauses alex: `is_active=FALSE`,
   `paused_reason="Auto-paused: bounce rate 6%..."`, `paused_at=now`
4. Stage 5 rotation stops routing to alex; sends continue via daniel
5. You see the pause in the Accounts dashboard with the reason
6. You investigate (bad list?), then click "Reactivate" → 24h grace starts
7. If bounces are still high after grace, it re-pauses

---

## Troubleshooting

**My only account got flagged critical but didn't pause**
Working as designed (audit 6.1). The last active account is never auto-paused.
Fix the underlying issue (recipient list quality) — high bounces mean bad
addresses are getting through. Check your Stage 1 email validation.

**Warm-up isn't restricting my caps**
Check `warmup_enabled=TRUE` and `activated_at` is set. Existing accounts default
to FALSE (presumed warm). Only newly-added accounts with the flag get ramped.

**Reactivated account immediately re-paused**
Shouldn't happen within 24h (grace window). If it does after grace, the bounce
rate is genuinely still critical — the list quality issue isn't resolved.

**Health check never runs automatically**
Run `installHealthCheckTrigger()` in the Apps Script editor once.

---

## Test summary

```
Stage 1 validation:       21 tests
Stage 2 spintax engine:   39 tests
Stage 2 integration:      48 tests
Stage 3 renderer:         36 tests
Stage 3 integration:      56 tests
Stage 5 priority+pool:    38 tests
Stage 6 warmup+health:    43 tests
Stage 7 reply+analytics:  61 tests
──────────────────────────────────
Total:                   342 tests passing
```

---

## The system is complete

All seven planned stages are built:

```
Stage 1  Campaign setup + validation + dedup
Stage 2  Spintax variant generation
Stage 3  Preview + safety checks + queue write
Stage 5  Smart send: priority, multi-sender rotation, rate limiting
Stage 6  Sender health, warm-up, auto-pause      ← this stage
Stage 7  Reply detection + variant analytics

Apps Script v5 ties the backend together:
  send (priority + rotation + warm-up caps)
  → reply scan (classify + suppress + flag engagement)
  → health check (auto-pause unhealthy senders)
```

Six dashboards/screens: campaign flow (1→2→3), queue view, operational health,
variant analytics, and account management.

The consultative-outreach engine you set out to build is done: validated input,
sophisticated personalization, safe high-volume sending, reputation protection,
and the closed-loop analytics that tell you which messaging actually wins deals.
