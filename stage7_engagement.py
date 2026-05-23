"""
stage7_engagement.py
=====================
Aggregate reply outcomes into per-variant performance metrics.

Solves audit errors:
  - 7.7 / 7.15: Sample-size guards — don't rank variants on tiny data; always
                show the denominator (sent count) alongside reply rate
  - 7.16: Group by (template_id, template_version) — spin paths only comparable
          within the same version
  - 7.14: Reply-only data → categorical outcomes, not an over-engineered score

Pure functions operating on lists of Emails rows. No I/O here (the UI layer
loads the rows and passes them in), which keeps this fully testable.
"""

from dataclasses import dataclass, field
from typing import Optional

from stage7_reply_classifier import is_positive_engagement


# ============================================================================
# CONFIG
# ============================================================================

# Don't declare a variant a "winner" / "loser" below this many sends.
# Below this, we show data but mark it "insufficient sample".
MIN_SAMPLE_FOR_RANKING = 20


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class VariantStats:
    """Performance stats for one variant group."""
    template_id: str
    template_version: str

    sent: int = 0
    genuine_replies: int = 0
    auto_replies: int = 0
    bounces: int = 0
    unsubscribes: int = 0
    no_response: int = 0

    @property
    def reply_rate(self) -> float:
        """Genuine reply rate as a percentage of sent."""
        if self.sent == 0:
            return 0.0
        return round(100.0 * self.genuine_replies / self.sent, 1)

    @property
    def bounce_rate(self) -> float:
        if self.sent == 0:
            return 0.0
        return round(100.0 * self.bounces / self.sent, 1)

    @property
    def has_sufficient_sample(self) -> bool:
        return self.sent >= MIN_SAMPLE_FOR_RANKING

    @property
    def label(self) -> str:
        return f"{self.template_id} v{self.template_version}"


@dataclass
class SubjectVariantStats:
    """
    Performance for a specific subject spin choice.
    Used to answer: "which subject opener gets the most replies?"
    """
    template_id: str
    template_version: str
    subject_choice: str   # the chosen text at the subject's first spin position

    sent: int = 0
    genuine_replies: int = 0

    @property
    def reply_rate(self) -> float:
        if self.sent == 0:
            return 0.0
        return round(100.0 * self.genuine_replies / self.sent, 1)

    @property
    def has_sufficient_sample(self) -> bool:
        return self.sent >= MIN_SAMPLE_FOR_RANKING


@dataclass
class CampaignStats:
    """Performance rolled up by campaign."""
    campaign_id: str
    brand: str = ""
    sent: int = 0
    genuine_replies: int = 0

    @property
    def reply_rate(self) -> float:
        if self.sent == 0:
            return 0.0
        return round(100.0 * self.genuine_replies / self.sent, 1)


@dataclass
class OverallStats:
    """Top-line numbers across everything in scope."""
    total_sent: int = 0
    genuine_replies: int = 0
    auto_replies: int = 0
    bounces: int = 0
    unsubscribes: int = 0
    no_response: int = 0

    @property
    def reply_rate(self) -> float:
        if self.total_sent == 0:
            return 0.0
        return round(100.0 * self.genuine_replies / self.total_sent, 1)

    @property
    def bounce_rate(self) -> float:
        if self.total_sent == 0:
            return 0.0
        return round(100.0 * self.bounces / self.total_sent, 1)


# ============================================================================
# HELPERS
# ============================================================================

def _is_sent(row: dict) -> bool:
    """True if the row was actually sent (not Queued/Failed/Draft)."""
    return str(row.get("status", "")).strip().lower() in ("sent", "delivered")


def _reply_status(row: dict) -> str:
    """Normalized reply_status, defaulting to 'none'."""
    val = str(row.get("reply_status", "")).strip().lower()
    return val if val else "none"


def _first_subject_choice(row: dict) -> Optional[str]:
    """
    Extract the chosen text at the subject's first spin position from
    spin_path_json. Returns None if unavailable.
    """
    import json
    raw = row.get("spin_path_json", "")
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        subject_path = data.get("subject", [])
        if subject_path:
            return subject_path[0].get("text")
    except (json.JSONDecodeError, AttributeError, IndexError, TypeError):
        return None
    return None


# ============================================================================
# AGGREGATIONS
# ============================================================================

def compute_overall_stats(rows: list[dict]) -> OverallStats:
    """Top-line stats across all sent rows."""
    stats = OverallStats()
    for row in rows:
        if not _is_sent(row):
            continue
        stats.total_sent += 1
        status = _reply_status(row)
        if status == "genuine":
            stats.genuine_replies += 1
        elif status == "auto_reply":
            stats.auto_replies += 1
        elif status == "bounce":
            stats.bounces += 1
        elif status == "unsubscribe":
            stats.unsubscribes += 1
        else:
            stats.no_response += 1
    return stats


def compute_variant_stats(rows: list[dict]) -> list[VariantStats]:
    """
    Group sent rows by (template_id, template_version) and compute stats.
    Sorted by reply_rate descending, but only among those with sufficient
    sample (insufficient ones sorted to the end — audit error 7.7).
    """
    groups: dict[tuple, VariantStats] = {}

    for row in rows:
        if not _is_sent(row):
            continue
        tid = str(row.get("template_id", "unknown"))
        tver = str(row.get("template_version", "?"))
        key = (tid, tver)

        if key not in groups:
            groups[key] = VariantStats(template_id=tid, template_version=tver)

        vs = groups[key]
        vs.sent += 1
        status = _reply_status(row)
        if status == "genuine":
            vs.genuine_replies += 1
        elif status == "auto_reply":
            vs.auto_replies += 1
        elif status == "bounce":
            vs.bounces += 1
        elif status == "unsubscribe":
            vs.unsubscribes += 1
        else:
            vs.no_response += 1

    result = list(groups.values())
    # Sort: sufficient-sample first (by reply_rate desc), then insufficient
    result.sort(
        key=lambda v: (v.has_sufficient_sample, v.reply_rate),
        reverse=True,
    )
    return result


def compute_subject_choice_stats(rows: list[dict]) -> list[SubjectVariantStats]:
    """
    Group by (template_id, template_version, first subject spin choice).
    Answers "which subject opener performs best" — the spintax optimization win.

    Only compares within the same template version (audit error 7.16).
    """
    groups: dict[tuple, SubjectVariantStats] = {}

    for row in rows:
        if not _is_sent(row):
            continue
        choice = _first_subject_choice(row)
        if choice is None:
            continue  # can't attribute without a spin choice

        tid = str(row.get("template_id", "unknown"))
        tver = str(row.get("template_version", "?"))
        key = (tid, tver, choice)

        if key not in groups:
            groups[key] = SubjectVariantStats(
                template_id=tid, template_version=tver, subject_choice=choice,
            )

        sv = groups[key]
        sv.sent += 1
        if is_positive_engagement(_reply_status(row)):
            sv.genuine_replies += 1

    result = list(groups.values())
    result.sort(
        key=lambda v: (v.has_sufficient_sample, v.reply_rate),
        reverse=True,
    )
    return result


def compute_campaign_stats(rows: list[dict]) -> list[CampaignStats]:
    """Group by campaign_id. Sorted by reply_rate descending."""
    groups: dict[str, CampaignStats] = {}

    for row in rows:
        if not _is_sent(row):
            continue
        cid = str(row.get("campaign_id", "unknown"))
        if cid not in groups:
            groups[cid] = CampaignStats(
                campaign_id=cid,
                brand=str(row.get("brand", "")),
            )
        cs = groups[cid]
        cs.sent += 1
        if is_positive_engagement(_reply_status(row)):
            cs.genuine_replies += 1

    result = list(groups.values())
    result.sort(key=lambda c: c.reply_rate, reverse=True)
    return result


# ============================================================================
# WINNER DETECTION (with sample guards — audit error 7.7)
# ============================================================================

def identify_best_variant(
    variant_stats: list[VariantStats],
) -> Optional[VariantStats]:
    """
    Return the best-performing variant ONLY IF it has a sufficient sample.
    Returns None if no variant has enough data to call a winner.
    """
    eligible = [v for v in variant_stats if v.has_sufficient_sample]
    if not eligible:
        return None
    return max(eligible, key=lambda v: v.reply_rate)


def identify_best_subject_choice(
    subject_stats: list[SubjectVariantStats],
) -> Optional[SubjectVariantStats]:
    """Best subject opener with sufficient sample, or None."""
    eligible = [s for s in subject_stats if s.has_sufficient_sample]
    if not eligible:
        return None
    return max(eligible, key=lambda s: s.reply_rate)
