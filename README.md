# PremiumAds Spintax Tool

Consultative cold-outreach engine for mobile-monetization partnerships.
Spintax-personalized emails → safety-checked queueing → priority multi-account
sending with reputation protection → reply tracking that shows which messaging
wins deals.

**One app. One command to run.**

```
streamlit run app.py
```

---

## Deploy in 3 steps

### 1. Push this folder to GitHub
Put every file in this folder into a repo (the `.gitignore` keeps your secrets
out). On Streamlit Community Cloud → "New app" → point it at the repo, main
file = `app.py`.

### 2. Add your secrets
In the deployed app → **Settings → Secrets**, paste:

```toml
sheet_id = "1IbkbJfUXhS1V38WaNgemG7q9TW7FFBssXIMhPN_QQfo"
service_account_b64 = "<base64 of your service account JSON>"
webapp_url = ""
```

(See `.streamlit/secrets.toml.example` for how to generate the base64.)
Make sure your service account email has **edit access** to the Sheet.

### 3. Open the app and click "Set up Google Sheet"
On first load the app detects an uninitialized Sheet and shows a single setup
button. Click it once — it creates every tab and column (~30 seconds). Done.

That's the whole deployment. No terminal, no manual migrations.

---

## What you get

**Send flow** (sidebar): New campaign → generate variant → confirm & queue.
**Monitoring** (sidebar): Queue · Health Dashboard · Analytics · Accounts.

Sending, reply-scanning, and health checks run automatically via the Apps
Script backend (see below).

---

## The Apps Script backend (one-time, enables auto-send)

The Streamlit app queues emails; a Google Apps Script actually sends them and
scans for replies. To turn it on:

1. Open your Sheet → Extensions → Apps Script
2. Paste all of `apps_script_v5.gs`, save
3. Run `sendQueuedEmails()` once → grant the Gmail permissions
4. Install the three triggers (run each once in the editor):
   - `installFiveMinuteTrigger()` — send queued mail every 5 min
   - `installReplyScanTrigger()` — scan replies every 15 min
   - `installHealthCheckTrigger()` — check sender health every 6 h

Without this, emails queue but don't send. With it, the pipeline runs itself.

---

## Local development

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then fill it in
streamlit run app.py
```

---

## Run the tests

```bash
for t in test_validation test_spintax_engine test_stage2_integration \
         test_stage3_renderer test_stage3_integration test_stage5 \
         test_stage6 test_stage7 test_app_routing; do python $t.py; done
```

374 tests across 9 suites. Run after any edit.

---

## File map

| File | Role |
|---|---|
| `app.py` | The app — run this. Sidebar + router tie all stages together. |
| `setup_gate.py` | First-run one-click Sheet initialization. |
| `migrate_all.py` | Runs all schema migrations (called by the setup button). |
| `apps_script_v5.gs` | The backend — paste into Apps Script for auto-send. |
| `requirements.txt` | Python dependencies. |
| `.streamlit/` | Config + secrets template. |
| `stage1_*` … `stage7_*` | The seven pipeline stages. |
| `schema_setup*.py` | Schema definitions + migrations (v1–v7). |
| `time_utils.py` | Shared timestamp handling. |
| `test_*.py` | 9 test suites, 374 tests. |

For full architecture and per-stage detail, see `README_FULL.md`.
