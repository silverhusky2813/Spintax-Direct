# Direct Deal Spintax Generator

PremiumAds — Programmatic Direct Deal outreach tool.  
Generates unique email and campaign brief variations using spintax templates.

## Features

- **3 templates**: Outreach Email · Follow-Up Email · Agency Campaign Brief
- **Spintax engine**: Every generation produces a unique variation
- **Smart CPM rate card**: Auto-calculates floor rates by format × GEO tier
- **8 verticals** with real brand names pre-loaded
- **No API key required** — fully static, no AI calls

---

## Deploy to Streamlit Cloud

1. **Push to GitHub**
   ```
   git init
   git add .
   git commit -m "initial commit"
   git remote add origin https://github.com/YOUR_USERNAME/direct-deal-spintax.git
   git push -u origin main
   ```

2. **Deploy**
   - Go to [share.streamlit.io](https://share.streamlit.io)
   - Click **New app**
   - Select your repo, branch `main`, main file `app.py`
   - Click **Deploy**

That's it. No secrets or environment variables needed.

---

## Run locally

```bash
pip install streamlit
streamlit run app.py
```

---

## File structure

```
├── app.py           # Main Streamlit app + templates
├── spintax.py       # Spin engine
├── requirements.txt
└── README.md
```

---

## Templates

| Template | Output |
|---|---|
| 📧 Outreach Email | Subject line + body |
| 🔁 Follow-Up Email | Subject line + body |
| 📋 Agency Campaign Brief | Formatted deal document |

## GEO Tier CPM Multipliers

| Tier | Markets | Multiplier |
|---|---|---|
| US | United States | ×1.00 |
| Tier 1 | UK, Canada, AU, DE, FR | ×0.60 |
| Tier 2 | BR, MX, JP, KR, ES, IT | ×0.35 |
| Tier 3 | ID, TH, VN, PH, TR, PL | ×0.15 |
| Tier 4 | IN, PK, NG, EG, BD | ×0.08 |
| ROW | Rest of World | ×0.06 |
