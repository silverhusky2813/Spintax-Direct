import streamlit as st
import random
import requests as _requests
from datetime import date, datetime
from spintax import spin

# Optional dependency — graceful fallback if gspread not installed yet
try:
    import gspread
    from google.oauth2.service_account import Credentials
    _GSPREAD_OK = True
except ImportError:
    _GSPREAD_OK = False

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Direct Deal Spintax Generator",
    page_icon="💼",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    [data-testid="stSidebar"] { min-width: 320px; }
    code { white-space: pre-wrap !important; }
</style>
""", unsafe_allow_html=True)

# ── Secrets / Sheet config ────────────────────────────────────────────────────
def _sheet_configured():
    try:
        _ = st.secrets["sheet_id"]
        _ = st.secrets["gcp_service_account"]
        return _GSPREAD_OK
    except Exception:
        return False

def _webapp_configured():
    try:
        return bool(st.secrets["webapp_url"])
    except Exception:
        return False

SHEET_OK  = _sheet_configured()
WEBAPP_OK = _webapp_configured()

# Sheet columns — A through J (matches existing sheet + appended cols G-J)
SHEET_COLS = [
    "Email",      # A — recipient
    "Name",       # B
    "Company",    # C
    "App_Name",   # D
    "Status",     # E — Queued / Sent / Failed
    "Approach",   # F — template label
    "Subject",    # G — generated subject line
    "Body",       # H — generated email body
    "Timestamp",  # I — when row was written
    "Sent_At",    # J — when Apps Script sent it
]

@st.cache_resource(show_spinner=False)
def _get_sheet():
    scope = ["https://spreadsheets.google.com/feeds",
             "https://www.googleapis.com/auth/drive"]
    sa_info = dict(st.secrets["gcp_service_account"])
    # Normalise private key — handles literal \n stored as text or actual newlines
    if "private_key" in sa_info:
        sa_info["private_key"] = sa_info["private_key"].replace("\\n", "\n")
    creds = Credentials.from_service_account_info(sa_info, scopes=scope)
    gc    = gspread.authorize(creds)
    # Open the "Emails" tab directly — headers are already in place
    wb    = gc.open_by_key(st.secrets["sheet_id"])
    sheet = wb.worksheet("Emails")
    # Auto-add new columns G-J if not present yet
    headers = sheet.row_values(1)
    for col_name in ["Subject", "Body", "Timestamp", "Sent_At"]:
        if col_name not in headers:
            sheet.update_cell(1, len(headers) + 1, col_name)
            headers.append(col_name)
    return sheet

def append_to_sheet(row: list) -> int:
    sheet = _get_sheet()
    sheet.append_row(row, value_input_option="USER_ENTERED")
    return sheet.row_count

def trigger_send_now(row_number: int) -> bool:
    try:
        url  = st.secrets["webapp_url"]
        resp = _requests.post(url, json={"row": row_number}, timeout=15)
        data = resp.json()
        return data.get("success", False)
    except Exception:
        return False

# ── Static data ───────────────────────────────────────────────────────────────
VERTICALS_BRANDS = {
    "FMCG":             ["Unilever", "P&G", "Nestlé", "Coca-Cola", "PepsiCo", "Colgate-Palmolive", "Reckitt"],
    "Finance":          ["HSBC", "American Express", "Visa", "Mastercard", "Fidelity", "Charles Schwab", "PayPal"],
    "Travel":           ["Booking Holdings", "Expedia", "Airbnb", "TripAdvisor", "Marriott", "Hilton", "Delta Airlines"],
    "E-commerce":       ["Amazon", "Nike", "Unilever", "P&G", "L'Oreal", "LVMH", "Shopify"],
    "Health & Fitness": ["Peloton", "Nike Health", "Johnson & Johnson", "Abbott", "Fitbit", "Whoop", "Headspace"],
    "Automotive":       ["Toyota", "BMW", "Ford", "Volkswagen", "Hyundai", "General Motors", "Tesla"],
    "Retail":           ["Walmart", "Target", "Best Buy", "H&M", "Zara", "IKEA", "Costco"],
    "Entertainment":    ["Netflix", "Disney+", "Warner Bros", "Spotify", "Apple", "Sony", "Electronic Arts"],
}

BUDGET_OPTIONS = [
    "$80,000", "$100,000", "$150,000", "$200,000",
    "$250,000", "$300,000", "$350,000", "$400,000", "Custom"
]

FLIGHT_OPTIONS = [
    "Q3 2025 (Jul 1 – Sep 30, 2025)",
    "Q4 2025 (Oct 1 – Dec 31, 2025)",
    "Q1 2026 (Jan 1 – Mar 31, 2026)",
    "Q2 2026 (Apr 1 – Jun 30, 2026)",
    "Rolling 4 weeks",
    "Rolling 8 weeks",
]

GEO_DATA = {
    "US":     {"countries": "United States",                                            "mult": 1.00},
    "Tier 1": {"countries": "UK, Canada, Australia, Germany, France",                   "mult": 0.60},
    "Tier 2": {"countries": "Brazil, Mexico, Japan, South Korea, Spain, Italy",          "mult": 0.35},
    "Tier 3": {"countries": "Indonesia, Thailand, Vietnam, Philippines, Turkey, Poland", "mult": 0.15},
    "Tier 4": {"countries": "India, Pakistan, Nigeria, Egypt, Bangladesh",               "mult": 0.08},
    "ROW":    {"countries": "Rest of World",                                             "mult": 0.06},
}

ALL_FORMATS = [
    "Instream Video", "Rewarded Video", "Interstitial",
    "Banner/Display", "App Open", "Native", "Audio Ads",
]

CPM_BASE = {
    "Instream Video": 12.0,
    "Rewarded Video": 10.0,
    "Interstitial":    8.0,
    "Banner/Display":  2.5,
    "App Open":        6.0,
    "Native":          4.0,
    "Audio Ads":       7.0,
}

# ── Spintax templates ─────────────────────────────────────────────────────────

OUTREACH_SUBJECT = (
    "{Exclusive|Confirmed|Active} {campaign opportunity|media buy|advertiser interest}"
    " — <<VERTICAL>> | <<APP_NAME>>"
)

OUTREACH_BODY = """\
Hi <<PROSPECT_NAME>>,

{I'm reaching out because we have|Quick note — we've secured|Hope you're doing well. We have} a \
{confirmed|live|active} {direct deal|brand campaign|media buy} from <<BRAND>> {with budget \
specifically allocated for|actively targeting|looking for} <<VERTICAL>> {mobile app|in-app|app} \
{inventory|audiences|placements} {this quarter|in the coming weeks|for the current period}.

{Here's a quick snapshot|Campaign overview|What we have on the table}:

  Brand / Advertiser  :  <<BRAND>>
  {Campaign budget|Allocated spend}  :  <<BUDGET>>
  {Ad formats|Units}  :  <<FORMATS>>
  {Target markets|GEOs}  :  <<GEOS>>
  {Flight period|Duration}  :  <<FLIGHT>>

{<<APP_NAME>>|Your app} {is a strong fit for their targeting parameters|stood out as a top \
candidate for this placement|matches the audience profile they're after} — {we specifically \
shortlisted it during targeting|it came up in our inventory review|your user base aligns well \
with their ICP}.

{We handle everything through Google Ad Manager as a GCPP|As a Google Certified Publishing \
Partner, setup is clean and fast|All deals run via GAM — transparent, no surprises} — \
{no migration required|no changes to your existing stack|fully compatible with your current setup}.

{Would you be open to a quick call|Are you free for 15 minutes|Can we connect briefly} \
{this week|early next week|in the next day or two} to {go over the details|confirm availability|\
align on terms}?

{Best regards,|Cheers,|Looking forward to connecting,}
Daniel
Head of Global Partnerships | PremiumAds
premiumads.net\
"""

FOLLOWUP_SUBJECT = (
    "{Following up — |Re: }{Confirmed {campaign|deal}|{Active|Exclusive} media buy}"
    " — <<BRAND>> x <<APP_NAME>>"
)

FOLLOWUP_BODY = """\
Hi <<PROSPECT_NAME>>,

{Just wanted to follow up on|Circling back on|Checking in regarding} my {previous message|\
note from last week|earlier email} about the <<BRAND>> {campaign|media buy|direct deal}.

{The opportunity is still open|Budget is still available|The campaign is still active} — \
{the flight window is coming up|we haven't filled the inventory yet|I wanted to make sure \
this didn't get lost in the inbox}.

{Quick recap|Just to recap|Campaign snapshot}:

  Brand    :  <<BRAND>>
  Budget   :  <<BUDGET>>
  Formats  :  <<FORMATS>>
  GEOs     :  <<GEOS>>
  Flight   :  <<FLIGHT>>

{Happy to send over the full campaign brief if that helps|I can jump on a quick call if easier|\
Let me know if you'd like more details} — {no heavy lift on your end|setup is straightforward \
via GAM|we can have this live within a few days of confirmation}.

{No pressure — just didn't want you to miss out on this one.|Totally understand if timing \
isn't right — happy to reconnect next quarter.|If now isn't ideal, let me know a better time \
and I'll follow up then.}

{Best,|Cheers,|Thanks,}
Daniel
PremiumAds | Head of Global Partnerships
premiumads.net\
"""

BRIEF_BODY = """\
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  AGENCY CAMPAIGN BRIEF
  PremiumAds — Direct Deal Program
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Reference No.    :  PA-DD-{2025|2026}-{47|48|49|50|51|52|53|54}
  {Issued|Prepared|Generated}          :  <<TODAY_DATE>>
  Account Manager  :  Daniel — Head of Global Partnerships

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ADVERTISER DETAILS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Brand / Advertiser  :  <<BRAND>>
  Vertical            :  <<VERTICAL>>
  Campaign Objective  :  {Brand awareness & reach|User acquisition & installs|Retargeting & re-engagement|Conversion-focused performance}
  Agency / Desk       :  {In-house programmatic team|Independent trading desk|Agency of record|Omnicom|IPG Mediabrands|GroupM}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  PUBLISHER DETAILS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Publisher           :  <<PROSPECT_NAME>>
  App / Property      :  <<APP_NAME>>
  Placement Type      :  {Premium in-app mobile inventory|In-app mobile — programmatic direct|Mobile app — direct placement}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  CAMPAIGN PARAMETERS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Total Budget        :  <<BUDGET>>
  Flight Period       :  <<FLIGHT>>
  Target GEOs         :  <<GEOS>>

  Ad Formats          :
<<FORMATS_BULLETED>>

  Frequency Cap       :  {3 impressions / user / 24h|5 impressions / user / 24h|No cap — broad reach}
  Brand Safety        :  {GARM standard — Suitable content only|IAS / DoubleVerify verified|Publisher-declared — Premium app environment}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  CPM RATE CARD (Floor Rates, USD)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

<<CPM_TABLE>>

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  DEAL TERMS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Deal Type           :  {Programmatic Guaranteed (PG)|Preferred Deal (PD)|Programmatic Direct}
  Measurement         :  {MRC-accredited viewability standards|IAS third-party verification|DoubleVerify brand safety + viewability}
  Payment Terms       :  {Net 30|Net 45|Net 60}
  Creative Assets     :  {Provided by advertiser via VAST tag|Client-supplied creatives — VAST 4.1|PremiumAds creative studio (on request)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  NEXT STEPS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{  1. Confirm inventory availability + floor CPM rates
  2. PremiumAds issues Deal ID via Google Ad Manager
  3. Creative assets submitted (T-3 days before flight start)
  4. Test impressions + QA sign-off
  5. Campaign go-live confirmation|  1. Publisher reviews brief + confirms fit
  2. Align on CPM floor rates
  3. Exchange Deal IDs via GAM / preferred SSP
  4. Creative trafficking — VAST tags provided by advertiser
  5. Activation + first-week performance check-in}

  To proceed → Daniel | daniel@premiumads.net | premiumads.net

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  {CONFIDENTIAL — For recipient use only|PRIVATE & CONFIDENTIAL|FOR PUBLISHER USE ONLY}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\
"""

TEMPLATE_MAP = {
    "📧 Outreach Email": {
        "subject":  OUTREACH_SUBJECT,
        "body":     OUTREACH_BODY,
        "is_email": True,
    },
    "🔁 Follow-Up Email": {
        "subject":  FOLLOWUP_SUBJECT,
        "body":     FOLLOWUP_BODY,
        "is_email": True,
    },
    "📋 Agency Campaign Brief": {
        "subject":  None,
        "body":     BRIEF_BODY,
        "is_email": False,
    },
}

# ── Helper functions ──────────────────────────────────────────────────────────

def build_cpm_table(selected_formats, selected_geos, custom_geo_mult=0.20, variance=0.12):
    """Build CPM rate card. Each call applies +-variance% random noise per cell (2 d.p.)."""
    if not selected_formats or not selected_geos:
        return "  [Select formats and GEOs to generate rate card]"
    fmt_w, col_w = 22, 13
    header  = f"  {'Format':<{fmt_w}}" + "".join(f"{'CPM (' + g + ')':>{col_w}}" for g in selected_geos)
    divider = "  " + "─" * (fmt_w + col_w * len(selected_geos))
    rows    = [header, divider]
    for fmt in selected_formats:
        base = CPM_BASE.get(fmt, 5.0)
        row  = f"  {fmt:<{fmt_w}}"
        for g in selected_geos:
            mult  = GEO_DATA[g]["mult"] if g in GEO_DATA else custom_geo_mult
            noise = random.uniform(1 - variance, 1 + variance)
            cpm   = round(base * mult * noise, 2)
            row  += f"${cpm:>{col_w - 2}.2f}   "
        rows.append(row)
    return "\n".join(rows)


def substitute(template, data):
    formats_bulleted = (
        "\n".join(f"    \u2022 {f}" for f in data["formats_list"])
        if data["formats_list"] else "    \u2022 [No formats selected]"
    )
    subs = {
        "<<PROSPECT_NAME>>":    data.get("prospect_name", "[Publisher Name]"),
        "<<APP_NAME>>":         data.get("app_name",      "[App Name]"),
        "<<BRAND>>":            data.get("brand",         "[Brand]"),
        "<<BUDGET>>":           data.get("budget",        "[Budget]"),
        "<<VERTICAL>>":         data.get("vertical",      "[Vertical]"),
        "<<FORMATS>>":          data.get("formats_str",   "[Formats]"),
        "<<FORMATS_BULLETED>>": formats_bulleted,
        "<<GEOS>>":             data.get("geos_str",      "[GEOs]"),
        "<<FLIGHT>>":           data.get("flight",        "[Flight Period]"),
        # <<CPM_TABLE>> is injected per-variation in make_variations
        "<<TODAY_DATE>>":       date.today().strftime("%B %d, %Y"),
    }
    for k, v in subs.items():
        template = template.replace(k, v)
    return template


def make_variations(subject_tpl, body_tpl, data, n, fmt_list, geo_list, custom_mult):
    """Generate n unique variations; each gets a freshly spun CPM table."""
    filled_subj = substitute(subject_tpl, data) if subject_tpl else None
    filled_body = substitute(body_tpl, data)
    results = []
    for _ in range(n):
        cpm_table     = build_cpm_table(fmt_list, geo_list, custom_mult)
        body_with_cpm = filled_body.replace("<<CPM_TABLE>>", cpm_table)
        results.append({
            "subject": spin(filled_subj) if filled_subj else None,
            "body":    spin(body_with_cpm),
        })
    return results


def do_send(var, to_email, template_name, prospect_name, company, app_name, send_mode):
    """Write row to Sheet and optionally trigger immediate send via Apps Script Web App."""
    if not SHEET_OK:
        return False, "Sheet not configured — see README for secrets setup."

    now      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    subject  = var.get("subject") or ""
    body     = var.get("body") or ""
    # Column order: A=Email, B=Name, C=Company, D=App_Name,
    #               E=Status, F=Approach, G=Subject, H=Body, I=Timestamp, J=Sent_At
    row_data = [
        to_email, prospect_name, company, app_name,
        "Queued", template_name, subject, body, now, ""
    ]

    try:
        row_number = append_to_sheet(row_data)
    except Exception as e:
        return False, f"Sheet write failed: {e}"

    if send_mode == "Send Now":
        if not WEBAPP_OK:
            return True, "Queued to sheet (no webapp_url — will send on next trigger run)"
        ok = trigger_send_now(row_number)
        if ok:
            return True, f"Sent immediately via Apps Script (row {row_number})"
        else:
            return True, f"Sheet write OK, Web App trigger failed — will send on queue run (row {row_number})"

    return True, f"Queued to sheet (row {row_number}) — Apps Script will send on next trigger"


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("💼 Direct Deal")
    st.caption("Spintax Generator — PremiumAds")
    st.divider()

    st.subheader("📋 Prospect Info")
    prospect_name  = st.text_input("Publisher / Contact Name", placeholder="e.g. John Kim")
    company        = st.text_input("Company / Studio", placeholder="e.g. AppCo Ltd")
    app_name       = st.text_input("App Name", placeholder="e.g. Puzzle Adventure")
    prospect_email = st.text_input("Prospect Email", placeholder="e.g. john@appco.com")

    st.divider()
    st.subheader("🏷️ Campaign Details")

    vertical_opts   = list(VERTICALS_BRANDS.keys()) + ["Custom"]
    vertical_choice = st.selectbox("Vertical", vertical_opts)
    if vertical_choice == "Custom":
        vertical   = st.text_input("Enter vertical name", placeholder="e.g. EdTech") or "[Custom Vertical]"
        brand_opts = ["Custom"]
    else:
        vertical   = vertical_choice
        brand_opts = VERTICALS_BRANDS[vertical_choice] + ["Custom"]

    brand_choice = st.selectbox("Brand / Advertiser", brand_opts)
    if brand_choice == "Custom":
        brand = st.text_input("Enter brand name", placeholder="e.g. Coca-Cola")
    else:
        brand = brand_choice

    budget_choice = st.selectbox("Budget", BUDGET_OPTIONS)
    if budget_choice == "Custom":
        budget = st.text_input("Enter budget", placeholder="e.g. $175,000")
    else:
        budget = budget_choice

    flight = st.selectbox("Flight Period", FLIGHT_OPTIONS, index=1)

    geo_opts      = list(GEO_DATA.keys()) + ["Custom"]
    selected_geos = st.multiselect("Target GEOs", geo_opts, default=["US", "Tier 1"])
    custom_geo_mult = 0.20
    if "Custom" in selected_geos:
        custom_geo = st.text_input("Custom GEO(s)", placeholder="e.g. MENA, LATAM, SEA")
        custom_geo_mult = st.number_input(
            "Fallback CPM multiplier for custom GEO",
            min_value=0.01, max_value=1.00, value=0.20, step=0.01,
            help="Ref: Tier 1=0.60 · Tier 2=0.35 · Tier 3=0.15 · Tier 4=0.08",
        )
        selected_geos = [g for g in selected_geos if g != "Custom"]
        if custom_geo.strip():
            selected_geos.append(custom_geo.strip())

    selected_formats = st.multiselect(
        "Ad Formats", ALL_FORMATS, default=["Rewarded Video", "Interstitial"]
    )

    st.divider()
    st.subheader("⚙️ Generate")
    n_variations = st.slider("Number of variations", min_value=1, max_value=10, value=3)
    generate_btn = st.button("🎲 Generate Variations", use_container_width=True, type="primary")

    # Sheet connection status
    st.divider()
    if SHEET_OK:
        st.success("📊 Sheet connected", icon="✅")
        if WEBAPP_OK:
            st.success("⚡ Send Now enabled", icon="✅")
        else:
            st.info("🕐 Queue mode only\n(add webapp_url to enable Send Now)", icon="ℹ️")
    else:
        st.warning("Sheet not configured\nSee README → Secrets Setup", icon="⚠️")

# ── Main area ─────────────────────────────────────────────────────────────────
st.title("Direct Deal Spintax Generator")
st.caption("PremiumAds · Google Certified Publishing Partner · premiumads.net")

template_choice = st.radio("Template", list(TEMPLATE_MAP.keys()), horizontal=True)
st.divider()

# ── Generate ──────────────────────────────────────────────────────────────────
if generate_btn:
    if not brand or (brand_choice == "Custom" and not brand.strip()):
        st.warning("Please enter a brand name.")
    elif not selected_geos:
        st.warning("Please select at least one GEO.")
    elif not selected_formats:
        st.warning("Please select at least one ad format.")
    else:
        data = {
            "prospect_name": prospect_name.strip() or "[Publisher Name]",
            "app_name":      app_name.strip()      or "[App Name]",
            "brand":         brand.strip() if brand_choice == "Custom" else brand_choice,
            "budget":        budget.strip() if budget_choice == "Custom" else budget_choice,
            "vertical":      vertical,
            "formats_str":   ", ".join(selected_formats),
            "formats_list":  selected_formats,
            "geos_str":      ", ".join(selected_geos),
            "flight":        flight,
        }
        tpl = TEMPLATE_MAP[template_choice]
        st.session_state["variations"]     = make_variations(
            tpl["subject"], tpl["body"], data,
            n_variations, selected_formats, selected_geos, custom_geo_mult,
        )
        st.session_state["is_email"]       = tpl["is_email"]
        st.session_state["template_name"]  = template_choice
        st.session_state["prospect_email"] = prospect_email.strip()
        st.session_state["prospect_name"]  = prospect_name.strip() or "[Publisher Name]"
        st.session_state["company_saved"]  = company.strip()       or ""
        st.session_state["app_name_saved"] = app_name.strip()      or "[App Name]"
        st.session_state.pop("send_status", None)   # reset statuses on re-generate

# ── Display & Send ────────────────────────────────────────────────────────────
if "variations" in st.session_state and st.session_state["variations"]:
    variations    = st.session_state["variations"]
    is_email      = st.session_state["is_email"]
    template_name = st.session_state["template_name"]
    saved_email   = st.session_state.get("prospect_email", "")
    saved_name    = st.session_state.get("prospect_name",  "[Publisher Name]")
    saved_company = st.session_state.get("company_saved",  "")
    saved_app     = st.session_state.get("app_name_saved", "[App Name]")

    if "send_status" not in st.session_state:
        st.session_state["send_status"] = {}

    st.subheader(f"✅ {len(variations)} variation{'s' if len(variations) > 1 else ''} — {template_name}")

    for i, var in enumerate(variations):
        with st.expander(f"Variation {i + 1}", expanded=(i == 0)):

            # Content preview
            if is_email and var["subject"]:
                st.markdown("**Subject line:**")
                st.code(var["subject"], language="text")
                st.markdown("**Email body:**")
            st.code(var["body"], language="text")

            # Send section
            st.divider()
            st.markdown("**📤 Send this variation**")

            to_email_input = st.text_input(
                "To", value=saved_email,
                placeholder="publisher@example.com",
                key=f"to_{i}",
                label_visibility="collapsed",
            )

            status_key = f"v{i}"
            existing   = st.session_state["send_status"].get(status_key)

            if existing:
                icon = "✅" if existing["ok"] else "⚠️"
                st.markdown(f"{icon} `{existing['msg']}`")
                if st.button("↩ Re-send", key=f"resend_{i}"):
                    st.session_state["send_status"].pop(status_key, None)
                    st.rerun()
            else:
                c1, c2 = st.columns(2)
                with c1:
                    send_now = st.button(
                        "⚡ Send Now", key=f"now_{i}",
                        use_container_width=True, type="primary",
                        disabled=not SHEET_OK,
                        help="Sends immediately via Apps Script Web App" if SHEET_OK else "Configure secrets first",
                    )
                with c2:
                    queue = st.button(
                        "🕐 Add to Queue", key=f"queue_{i}",
                        use_container_width=True,
                        disabled=not SHEET_OK,
                        help="Adds to Sheet; time trigger will process it" if SHEET_OK else "Configure secrets first",
                    )

                if send_now or queue:
                    if not to_email_input.strip():
                        st.warning("Enter a recipient email first.")
                    else:
                        mode = "Send Now" if send_now else "Queued"
                        with st.spinner("Writing to Sheet…"):
                            ok, msg = do_send(
                                var, to_email_input.strip(),
                                template_name, saved_name, saved_company, saved_app, mode,
                            )
                        st.session_state["send_status"][status_key] = {"ok": ok, "msg": msg}
                        st.rerun()

else:
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.info(
            "**Getting started:**\n\n"
            "1. Fill in **Prospect Info** (including email) in the sidebar\n"
            "2. Select a **template** above\n"
            "3. Click **Generate Variations**\n"
            "4. Use **⚡ Send Now** or **🕐 Add to Queue** per variation\n\n"
            "_Each variation is a unique spin of the selected template._"
        )

st.divider()
st.caption("PremiumAds · Google Certified Publishing Partner (GCPP) · premiumads.net")
