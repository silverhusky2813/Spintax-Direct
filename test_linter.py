"""Test suite for linter_rules. Run: python3 test_linter.py"""

from linter_rules import (
    lint,
    parse_rate_card,
    split_table_and_prose,
    check_urgency,
    check_greeting,
    check_optout,
    check_cpm_consistency,
)

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def rules_hit(findings):
    return {f.rule for f in findings}


RATE_CARD_TABLE = """
| Format       | Floor    | Ceiling  |
|--------------|----------|----------|
| Banner       | $0.20    | $0.80    |
| Interstitial | $2.00    | $5.00    |
| Rewarded     | $6.00    | $12.00   |
""".strip()


# --------------------------------------------------------------------------- #
print("\n[rate card parsing]")
rc = parse_rate_card(split_table_and_prose(RATE_CARD_TABLE)[0])
check("parses 3 formats", set(rc) == {"banner", "interstitial", "rewarded"})
check("rewarded floor 6", rc["rewarded"]["floor"] == 6.0)
check("rewarded ceiling 12", rc["rewarded"]["ceiling"] == 12.0)
check("banner floor 0.2", rc["banner"]["floor"] == 0.20)

# column order swapped should still work (header-driven)
swapped = """
| Ceiling | Format | Floor |
|---|---|---|
| $0.80 | Banner | $0.20 |
| $12.00 | Rewarded | $6.00 |
""".strip()
rc2 = parse_rate_card(split_table_and_prose(swapped)[0])
check("swapped columns: rewarded floor 6", rc2.get("rewarded", {}).get("floor") == 6.0)
check("swapped columns: rewarded ceiling 12", rc2.get("rewarded", {}).get("ceiling") == 12.0)

# no table -> empty dict, no crash
check("empty table -> {}", parse_rate_card([]) == {})


# --------------------------------------------------------------------------- #
print("\n[the original bug: $5 headline floor vs $6 rewarded table floor]")
buggy = f"""Hi Scurvycatt,
I'm Daniel from PremiumAds.
Floor CPM: $5.00
Offer CPM: $12.00
{RATE_CARD_TABLE}
Quick yes/no so I can lock the slot?
Daniel"""
f = check_cpm_consistency(buggy)
check("flags the $5 floor mismatch", any("$5" in x.message and x.rule == "cpm_consistency" for x in f))


# --------------------------------------------------------------------------- #
print("\n[cpm: valid offer within bounds is NOT flagged]")
valid = f"""Hi Maria,
We'll pay $12 CPM on rewarded against a $6 floor.
{RATE_CARD_TABLE}
If it's not a fit, just say so.
Maria"""
f = check_cpm_consistency(valid)
check("no cpm findings on a clean email", f == [])


# --------------------------------------------------------------------------- #
print("\n[cpm: offer above ceiling is flagged]")
over = f"""Hi Sam,
We'll pay $20 CPM on rewarded against a $6 floor.
{RATE_CARD_TABLE}
no hard feelings if not.
Sam"""
f = check_cpm_consistency(over)
check("flags $20 over rewarded ceiling", any("exceeds rewarded ceiling" in x.message for x in f))


# --------------------------------------------------------------------------- #
print("\n[cpm: underbid below own floor is flagged]")
under = f"""Hi Sam,
We'll pay $3 CPM on rewarded.
{RATE_CARD_TABLE}
not a fit? no worries.
Sam"""
f = check_cpm_consistency(under)
check("flags $3 below rewarded floor", any("below rewarded floor" in x.message for x in f))


# --------------------------------------------------------------------------- #
print("\n[cpm: ambiguous sentence naming two formats is skipped, no false positive]")
ambig = f"""Hi Sam,
Rewarded and interstitial both look good at $4.
{RATE_CARD_TABLE}
just say so if not.
Sam"""
f = check_cpm_consistency(ambig)
# $4 is below rewarded floor(6) and within interstitial(2-5); two formats -> skip
check("two-format sentence not flagged", all(x.rule != "cpm_consistency" for x in f))


# --------------------------------------------------------------------------- #
print("\n[urgency]")
check("confirm by flagged", any(x.rule == "fake_urgency" for x in check_urgency("please confirm by Friday")))
check("lock the slot flagged", any(x.rule == "fake_urgency" for x in check_urgency("so I can lock the slot")))
check("prioritizing flagged", any(x.rule == "fake_urgency" for x in check_urgency("I'm prioritizing Santiago")))
check("clean text not flagged", check_urgency("Happy to share our rate card if useful.") == [])
# dedupe: 'confirm by end of week' shouldn't double-count the same label
multi = check_urgency("confirm by end of week, confirm by end of week")
labels = [x.message for x in multi]
check("urgency labels deduped", len(labels) == len(set(labels)))


# --------------------------------------------------------------------------- #
print("\n[greeting]")
check("unfilled [Name] -> error",
      any(x.rule == "greeting" and x.severity == "error" for x in check_greeting("Hi [Name],\nbody")))
check("spin braces -> error",
      any(x.severity == "error" for x in check_greeting("{Hi|Hey} there,\nbody")))
check("generic 'hi there' -> warn",
      any(x.rule == "greeting" and x.severity == "warn" for x in check_greeting("Hi there,\nbody")))
check("real name -> no greeting finding",
      check_greeting("Hi Maria,\nbody text here") == [])


# --------------------------------------------------------------------------- #
print("\n[opt-out]")
check("missing opt-out flagged", any(x.rule == "opt_out" for x in check_optout("Buy now. Thanks.")))
check("present opt-out not flagged", check_optout("If it's not a fit, just say so.") == [])


# --------------------------------------------------------------------------- #
print("\n[integration: a clean, well-formed email]")
clean = f"""Hi Maria,
I'm Daniel from PremiumAds. We buy rewarded inventory and your US traffic fits.
We'll pay $12 CPM on rewarded against a $6 floor.
{RATE_CARD_TABLE}
If it's not the right desk, point me along — and if it's not of interest, just say so.
Daniel"""
f = lint(clean)
check("clean email has zero findings", f == [])

clean_findings = lint(clean)
print(f"      (clean email findings: {[ (x.rule,x.severity) for x in clean_findings ]})")


# --------------------------------------------------------------------------- #
print("\n[integration: the original spammy email trips multiple rules]")
f = lint(buggy)
hit = rules_hit(f)
check("buggy email flags urgency", "fake_urgency" in hit)
check("buggy email flags cpm", "cpm_consistency" in hit)
check("errors sorted first", f[0].severity == "error" if f else False)


# --------------------------------------------------------------------------- #
print(f"\n==== {PASS} passed, {FAIL} failed ====")
import sys
sys.exit(1 if FAIL else 0)
