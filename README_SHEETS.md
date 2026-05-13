# Google Sheets + Auto-Send Setup Guide

This guide connects the Streamlit app to your Google Sheet so generated emails are written and sent automatically — entirely through Google infrastructure (no third-party services).

---

## Architecture

```
Streamlit app
  └─ writes row → Google Sheet "Emails" tab
                        └─ Apps Script (runs inside Sheets)
                                └─ GmailApp.sendEmail() → daniel@premiumads.net sends the email
```

Everything except the Streamlit app runs inside Google. No external mail services required.

---

## Step 1 — Add columns G–J to your sheet

Your sheet currently has columns A–F (`Email, Name, Company, App_Name, Status, Approach`).

The app needs 4 more columns. The easiest way:

1. Open your sheet: https://docs.google.com/spreadsheets/d/1IbkbJfUXhS1V38WaNgemG7q9TW7FFBssXIMhPN_QQfo/edit
2. In the **Apps Script editor** (Step 2 below), run `ensureHeaders()` once — it adds the missing columns automatically.

Or add them manually: G = `Subject`, H = `Body`, I = `Timestamp`, J = `Sent_At`

---

## Step 2 — Install the Apps Script

1. In your Google Sheet, click **Extensions → Apps Script**
2. Delete everything in `Code.gs`
3. Paste the full contents of `apps_script.gs` (from this repo)
4. Click **Save** (disk icon)
5. Run `ensureHeaders` once: click the function dropdown → select `ensureHeaders` → click ▶ Run
   - Accept the permissions prompt (allow Gmail + Sheets access)

---

## Step 3 — Set up the time trigger (Queue mode)

This makes Apps Script automatically process pending emails on a schedule.

1. In Apps Script, click the **clock icon** (Triggers) in the left sidebar
2. Click **+ Add Trigger** (bottom right)
3. Configure:
   - Function: `sendQueuedEmails`
   - Event source: `Time-driven`
   - Type: `Minutes timer`
   - Interval: `Every 5 minutes` (or 10/15 to stay well within Gmail limits)
4. Click **Save** → accept permissions

Now any row with `Status = Queued` will be sent within 5 minutes.

---

## Step 4 — Deploy Web App (enables "Send Now" button)

This is optional but enables the **⚡ Send Now** button, which sends immediately instead of waiting for the trigger.

1. In Apps Script, click **Deploy → New Deployment**
2. Click the gear icon next to "Select type" → choose **Web App**
3. Configure:
   - Description: `PremiumAds Mail Sender`
   - Execute as: **Me** (daniel@premiumads.net)
   - Who has access: **Anyone** (required for Streamlit Cloud to call it)
4. Click **Deploy** → copy the Web App URL
5. Paste that URL into your Streamlit secrets as `webapp_url` (see Step 5)

> Re-deploy (Manage Deployments → Edit) every time you change the script.

---

## Step 5 — Create a Google Service Account

The Streamlit app needs API access to write to your sheet.

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a new project (or use an existing one)
3. Enable **Google Sheets API** and **Google Drive API**:
   - APIs & Services → Enable APIs → search each → Enable
4. Create a service account:
   - IAM & Admin → Service Accounts → Create Service Account
   - Name: `premiumads-sheet-writer` → Create
   - Skip optional steps → Done
5. Create a key:
   - Click your new service account → Keys tab → Add Key → Create new key → **JSON**
   - A `.json` file downloads — keep it safe, never commit to GitHub

6. Share your sheet with the service account:
   - Open your Google Sheet → Share
   - Add the service account email (looks like `name@project.iam.gserviceaccount.com`)
   - Role: **Editor**
   - Uncheck "notify people" → Share

---

## Step 6 — Configure Streamlit Secrets

### For local development

Create `.streamlit/secrets.toml` using the template in `secrets.toml.example`:
- Copy your service account JSON values into the `[gcp_service_account]` block
- Set `sheet_id` to `1IbkbJfUXhS1V38WaNgemG7q9TW7FFBssXIMhPN_QQfo`
- Set `webapp_url` to your Web App URL (or leave empty for queue mode)

Add to `.gitignore`:
```
.streamlit/secrets.toml
```

### For Streamlit Cloud

1. Go to your app on [share.streamlit.io](https://share.streamlit.io)
2. Click **⋮ → Settings → Secrets**
3. Paste the full contents of your completed `secrets.toml` (with real values)
4. Click **Save** → app restarts automatically

---

## How it works end to end

1. You fill in prospect info + generate variations in the Streamlit app
2. Click **⚡ Send Now** or **🕐 Add to Queue** under any variation
3. App writes a row to the `Emails` tab: `Status = Queued`
4. **Queue mode:** Apps Script time trigger runs every 5 min, finds `Queued` rows, sends via GmailApp, marks `Status = Sent`
5. **Send Now mode:** Streamlit calls the Web App endpoint directly → instant send → marks `Sent`

All sending goes through your Google account (`daniel@premiumads.net`) via Gmail. No external services.

---

## Gmail limits (good to know)

| Account type | Daily send limit |
|---|---|
| Personal Gmail | 500 emails/day |
| Google Workspace | 1,500 emails/day |

Apps Script enforces a 100-email/batch limit per trigger run — the script includes a 500ms pause between sends to stay safe.

---

## Troubleshooting

**Sheet not found error** — make sure the sheet tab is named exactly `Emails` (case-sensitive).

**Permission denied on Sheets** — the service account email must be added as Editor on the sheet.

**"Send Now" returns error but email was sent** — the Web App URL may have expired; re-deploy to get a new URL.

**Emails going to spam** — this is normal for cold outreach. Consider Gmail's Workspace warmup settings or send limits.

**Apps Script logs** — Apps Script editor → Executions (left sidebar) → click any run to see logs.
