"""Test suite for the brief generator. Run: python3 test_brief.py"""

from brief_engine import (
    detect_geos, detect_formats, detect_monetization,
    detect_titles, detect_contacts, build_brief, Brief,
)
from brief_fetch import html_to_text, _get, fetch_wikipedia

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


# Synthetic fixtures (own wording, not copied from sources) -------------------
WIKI = (
    "Mytona is a mobile game developer and publisher headquartered in Auckland, "
    "New Zealand, with offices in Singapore, Thailand, and Kazakhstan. Founded in "
    "2012, the company is best known for Seekers Notes, Cooking Diary, and Ravenhill. "
    "Its games are free-to-play and earn revenue mainly through in-app purchases. "
    "The largest share of revenue comes from the United States."
)
CASE = (
    "Mytona adopted a hybrid monetization model for Cooking Diary, adding in-app ads "
    "alongside in-app purchases. They chose rewarded and intrinsic in-game ad formats "
    "so monetization would not compromise the player experience. Contact: contact@mytona.com"
)
IAP_ONLY = (
    "PuzzleCo is a mobile studio known for Brain Bender. Its titles are free-to-play and "
    "monetize entirely through in-app purchases. Headquartered in Japan."
)


# geos ------------------------------------------------------------------------
print("\n[geos]")
g = dict(detect_geos(WIKI))
check("finds United States", "United States" in g)
check("finds New Zealand", "New Zealand" in g)
check("finds Singapore", "Singapore" in g)
check("finds Thailand", "Thailand" in g)
check("finds Kazakhstan", "Kazakhstan" in g)
# 'us' as a pronoun must NOT register as United States
check("lowercase 'us' pronoun ignored", "United States" not in dict(detect_geos("please join us today, all of us")))
check("'BUS' does not match US", "United States" not in dict(detect_geos("we took the BUS downtown")))
# sorted by count desc
ranked = detect_geos("United States United States Japan")
check("ranked by count", ranked[0][0] == "United States" and ranked[0][1] == 2)


# formats ---------------------------------------------------------------------
print("\n[formats]")
f = detect_formats(CASE)
check("finds rewarded", "rewarded" in f)
check("finds intrinsic", "intrinsic" in f)
check("finds in-game", "in-game" in f)
check("no false banner", "banner" not in f)


# monetization ----------------------------------------------------------------
print("\n[monetization]")
lab, _ = detect_monetization(CASE)
check("hybrid detected", lab == "Hybrid (IAP + ads)")
lab2, _ = detect_monetization(IAP_ONLY)
check("iap-led detected", lab2 == "IAP-led")
lab3, _ = detect_monetization("a game about gardening")
check("unknown when no signal", lab3 == "Unknown")


# titles ----------------------------------------------------------------------
print("\n[titles]")
t = detect_titles(WIKI, publisher="Mytona")
check("finds Seekers Notes", "Seekers Notes" in t)
check("finds Cooking Diary", "Cooking Diary" in t)
check("finds Ravenhill (one-word, end of list)", "Ravenhill" in t)
check("excludes geo 'New Zealand'", "New Zealand" not in t)
check("excludes publisher name", "Mytona" not in t)
check("no stopword titles", not any(x.lower() in {"the", "its", "they"} for x in t))
# prose after trigger without commas: take leading Title-case run only
t2 = detect_titles("The studio is known for Castle Quest which is a strategy game.", "")
check("trims trailing lowercase clause", "Castle Quest" in t2 and not any("which" in x.lower() for x in t2))


# contacts --------------------------------------------------------------------
print("\n[contacts]")
c = detect_contacts(CASE)
emails = [e for e, _ in c]
check("finds contact@mytona.com", "contact@mytona.com" in emails)
check("flags it generic", c and c[0][1] is True)
c2 = detect_contacts("reach maria.lopez@studio.io for partnerships")
check("non-generic not flagged generic", c2 and c2[0][1] is False)
check("dedup emails", len(detect_contacts("a@x.com a@x.com")) == 1)
check("no emails -> empty", detect_contacts("no addresses here") == [])


# build_brief integration -----------------------------------------------------
print("\n[build_brief]")
b = build_brief("Mytona", {"http://wiki": WIKI, "http://case": CASE})
check("brief is hybrid", b.monetization == "Hybrid (IAP + ads)")
check("brief has US in geos", any(g0 == "United States" for g0, _ in b.geos))
check("brief lists 2 sources", len(b.sources_used) == 2)
check("always-on contact-desk warning present",
      any("don't pitch the CEO" in w for w in b.warnings))
check("always-on staleness warning present",
      any("Verify before acting" in w for w in b.warnings))

# IAP-only triggers the 'ad buy may not fit' strategic flag
b2 = build_brief("PuzzleCo", {"http://x": IAP_ONLY})
check("IAP-only -> ad-buy-may-not-fit warning",
      any("ad-buy pitch may not fit" in w for w in b2.warnings))

# empty sources handled
b3 = build_brief("Nobody", {})
check("empty sources -> empty-brief warning",
      any("brief is empty" in w for w in b3.warnings))
check("empty brief still renders markdown", isinstance(b3.to_markdown(), str) and len(b3.to_markdown()) > 0)

# markdown render doesn't crash and includes the publisher
md = b.to_markdown()
check("markdown includes publisher header", md.startswith("# Research brief — Mytona"))


# fetch layer: pure + error path ----------------------------------------------
print("\n[fetch: pure + error handling]")
check("html_to_text strips tags",
      html_to_text("<p>Hello <b>world</b></p><script>x=1</script>") == "Hello world")
check("html_to_text collapses whitespace",
      html_to_text("a\n\n   b\t c") == "a b c")
# _get must degrade gracefully on an unreachable host (fast connection refusal)
body, err = _get("http://127.0.0.1:9/nothing", timeout=3)
check("_get returns error, no crash, on unreachable host", body == "" and err is not None)


print(f"\n==== {PASS} passed, {FAIL} failed ====")
import sys
sys.exit(1 if FAIL else 0)
