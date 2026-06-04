"""
Fetch layer for the research-brief generator.

Pulls plain text from PUBLIC sources only:
  - Wikipedia (action API, free, no key)
  - any public URLs the user supplies (company site, a case-study page, etc.)

Kept separate from brief_engine so the extraction logic stays offline-testable.
Every fetch degrades gracefully: on any error it returns ("", reason) instead
of raising, and gather() collects warnings rather than failing the whole run.

stdlib only (urllib) — no third-party dependency, no API key.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

_UA = {"User-Agent": "Mozilla/5.0 (compatible; OutreachBriefBot/1.0; research)"}
_TIMEOUT = 15


def _get(url: str, timeout: int = _TIMEOUT) -> tuple[str, str | None]:
    """Return (body_text, error). Never raises."""
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            enc = r.headers.get_content_charset() or "utf-8"
            return raw.decode(enc, errors="replace"), None
    except urllib.error.HTTPError as e:
        return "", f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return "", f"network error: {e.reason}"
    except Exception as e:  # noqa: BLE001 - defensive, must never propagate
        return "", f"{type(e).__name__}: {e}"


def html_to_text(html: str) -> str:
    """Crude but robust HTML -> text for keyword extraction."""
    html = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = re.sub(r"&[a-zA-Z#0-9]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_wikipedia(publisher: str) -> tuple[str, str | None, str]:
    """Return (plain_extract, error, url_used)."""
    title = urllib.parse.quote(publisher.strip().replace(" ", "_"))
    url = (
        "https://en.wikipedia.org/w/api.php?action=query&prop=extracts"
        "&explaintext=1&redirects=1&format=json&titles=" + title
    )
    body, err = _get(url)
    if err:
        return "", err, url
    try:
        data = json.loads(body)
        pages = data.get("query", {}).get("pages", {})
        extract = " ".join(p.get("extract", "") for p in pages.values()).strip()
        return extract, (None if extract else "no Wikipedia extract found"), url
    except (ValueError, KeyError, TypeError) as e:
        return "", f"parse error: {e}", url


def fetch_url(url: str) -> tuple[str, str | None]:
    """Fetch an arbitrary public URL and return (plain_text, error)."""
    body, err = _get(url)
    if err:
        return "", err
    return html_to_text(body), None


def gather(publisher: str, extra_urls: list[str] | None = None) -> tuple[dict[str, str], list[str]]:
    """
    Collect text from Wikipedia + any extra public URLs.
    Returns (sources {url: text}, warnings).
    """
    sources: dict[str, str] = {}
    warnings: list[str] = []

    wiki_text, wiki_err, wiki_url = fetch_wikipedia(publisher)
    if wiki_text:
        sources[wiki_url] = wiki_text
    else:
        warnings.append(f"Wikipedia: {wiki_err}")

    for url in (extra_urls or []):
        url = url.strip()
        if not url:
            continue
        if not url.startswith(("http://", "https://")):
            warnings.append(f"Skipped (not a URL): {url}")
            continue
        text, err = fetch_url(url)
        if text:
            sources[url] = text
        else:
            warnings.append(f"{url}: {err}")

    if not sources:
        warnings.append("No sources returned text. Paste a public URL (company site, "
                        "case study, app listing) to build a useful brief.")
    return sources, warnings
