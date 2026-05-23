"""
test_stage7.py
===============
Tests for Stage 7: reply classifier, subject matcher, engagement scoring.

Covers audit errors:
  - 7.3 / 7.12: Classification of genuine/auto/bounce/unsubscribe
  - 7.11: Subject prefix normalization
  - 7.13: Most-recent-send matching when multiple candidates
  - 7.7 / 7.15: Sample-size guards in variant ranking
  - 7.16: Variant grouping by template version

Run with:
  python test_stage7.py
"""

import json

from stage7_reply_classifier import (
    classify_reply,
    is_positive_engagement,
    should_suppress,
)
from stage7_subject_matcher import (
    normalize_subject,
    match_by_thread_id,
    match_by_subject,
    match_reply_to_sent,
)
from stage7_engagement import (
    MIN_SAMPLE_FOR_RANKING,
    compute_overall_stats,
    compute_variant_stats,
    compute_subject_choice_stats,
    compute_campaign_stats,
    identify_best_variant,
    identify_best_subject_choice,
)


def assert_eq(actual, expected, label):
    if actual == expected:
        print(f"  ✓ PASS: {label}")
        return True
    print(f"  ✗ FAIL: {label}")
    print(f"      Expected: {expected!r}")
    print(f"      Got:      {actual!r}")
    return False


def assert_true(cond, label):
    if cond:
        print(f"  ✓ PASS: {label}")
        return True
    print(f"  ✗ FAIL: {label}")
    return False


# ============================================================================
# Test: Reply classification (audit errors 7.3, 7.12)
# ============================================================================

def test_classify_genuine():
    print("\n--- Test: Genuine reply classification ---")

    assert_eq(
        classify_reply("alice@acme.com", "Re: Nike deal", "Sounds great, let's talk."),
        "genuine",
        "normal reply → genuine",
    )
    assert_eq(
        classify_reply("bob@pub.com", "Re: Confirmed media buy", "What are your CPMs for rewarded?"),
        "genuine",
        "question reply → genuine",
    )


def test_classify_bounce():
    print("\n--- Test: Bounce classification ---")

    assert_eq(
        classify_reply("mailer-daemon@googlemail.com", "Delivery Status Notification (Failure)", ""),
        "bounce",
        "mailer-daemon sender → bounce",
    )
    assert_eq(
        classify_reply("postmaster@acme.com", "Undelivered Mail Returned to Sender", ""),
        "bounce",
        "postmaster + undelivered → bounce",
    )
    assert_eq(
        classify_reply("random@x.com", "Re: deal", "Address not found. 550 5.1.1 user unknown"),
        "bounce",
        "bounce content in body → bounce",
    )


def test_classify_unsubscribe():
    print("\n--- Test: Unsubscribe classification ---")

    assert_eq(
        classify_reply("alice@acme.com", "Re: Nike deal", "Please unsubscribe me from this list."),
        "unsubscribe",
        "unsubscribe request → unsubscribe",
    )
    assert_eq(
        classify_reply("bob@x.com", "Re: offer", "Take me off your list, do not contact again."),
        "unsubscribe",
        "remove request → unsubscribe",
    )


def test_classify_auto_reply():
    print("\n--- Test: Auto-reply classification ---")

    assert_eq(
        classify_reply("alice@acme.com", "Out of Office: Re: Nike deal", "I am currently away until Monday."),
        "auto_reply",
        "OOO → auto_reply",
    )
    assert_eq(
        classify_reply("bob@x.com", "Automatic reply: your message", "On vacation."),
        "auto_reply",
        "automatic reply → auto_reply",
    )
    assert_eq(
        classify_reply("hr@x.com", "Re: deal", "Bob has left the company."),
        "auto_reply",
        "left company → auto_reply",
    )


def test_classify_precedence():
    print("\n--- Test: Classification precedence ---")

    # Bounce beats everything
    assert_eq(
        classify_reply("mailer-daemon@x.com", "Out of office", "unsubscribe"),
        "bounce",
        "bounce sender wins over auto/unsub content",
    )
    # Unsubscribe beats auto-reply
    assert_eq(
        classify_reply("alice@x.com", "Out of office", "Also please unsubscribe me"),
        "unsubscribe",
        "unsubscribe wins over auto-reply",
    )


def test_engagement_helpers():
    print("\n--- Test: Engagement helper functions ---")

    assert_true(is_positive_engagement("genuine"), "genuine is positive")
    assert_true(not is_positive_engagement("auto_reply"), "auto_reply not positive")
    assert_true(not is_positive_engagement("bounce"), "bounce not positive")

    assert_true(should_suppress("unsubscribe"), "unsubscribe → suppress")
    assert_true(should_suppress("bounce"), "bounce → suppress")
    assert_true(not should_suppress("genuine"), "genuine → don't suppress")
    assert_true(not should_suppress("auto_reply"), "auto_reply → don't suppress")


# ============================================================================
# Test: Subject normalization (audit error 7.11)
# ============================================================================

def test_normalize_subject():
    print("\n--- Test: Subject normalization (audit error 7.11) ---")

    assert_eq(normalize_subject("Re: Confirmed media buy"), "confirmed media buy", "Re: stripped")
    assert_eq(normalize_subject("RE: Confirmed media buy"), "confirmed media buy", "RE: case-insensitive")
    assert_eq(normalize_subject("Re: Re: Confirmed media buy"), "confirmed media buy", "double Re: stripped")
    assert_eq(normalize_subject("Fwd: Re: Confirmed media buy"), "confirmed media buy", "Fwd: Re: stripped")
    assert_eq(normalize_subject("Confirmed media buy"), "confirmed media buy", "no prefix unchanged")
    assert_eq(normalize_subject("  RE:  Spaced  Out  "), "spaced out", "whitespace collapsed")
    assert_eq(normalize_subject("AW: German reply"), "german reply", "AW: (German) stripped")


# ============================================================================
# Test: Thread ID matching (audit error 7.13)
# ============================================================================

def test_match_by_thread_id():
    print("\n--- Test: Thread ID matching ---")

    sent_rows = [
        {"thread_id": "thread-A", "recipient_email": "a@x.com", "sent_at": "2026-06-01T10:00:00Z", "idempotency_key": "k1"},
        {"thread_id": "thread-B", "recipient_email": "b@x.com", "sent_at": "2026-06-01T11:00:00Z", "idempotency_key": "k2"},
    ]

    match = match_by_thread_id("thread-B", sent_rows)
    assert_true(match is not None, "thread-B matched")
    assert_eq(match["idempotency_key"], "k2", "matched correct row")

    no_match = match_by_thread_id("thread-Z", sent_rows)
    assert_eq(no_match, None, "unknown thread → None")

    empty = match_by_thread_id("", sent_rows)
    assert_eq(empty, None, "empty thread id → None")


def test_match_thread_id_most_recent():
    print("\n--- Test: Thread match picks most recent (audit error 7.13) ---")

    # Same thread, two sends (Outreach then FollowUp)
    sent_rows = [
        {"thread_id": "thread-A", "recipient_email": "a@x.com", "sent_at": "2026-06-01T10:00:00Z", "idempotency_key": "outreach"},
        {"thread_id": "thread-A", "recipient_email": "a@x.com", "sent_at": "2026-06-05T10:00:00Z", "idempotency_key": "followup"},
    ]

    match = match_by_thread_id("thread-A", sent_rows)
    assert_eq(match["idempotency_key"], "followup", "matched most recent send in thread")


# ============================================================================
# Test: Subject fallback matching (audit error 7.4)
# ============================================================================

def test_match_by_subject():
    print("\n--- Test: Subject + recipient fallback matching ---")

    sent_rows = [
        {"thread_id": "", "recipient_email": "alice@acme.com", "subject": "Confirmed media buy for Nike", "sent_at": "2026-06-01T10:00:00Z", "idempotency_key": "k1"},
        {"thread_id": "", "recipient_email": "bob@x.com", "subject": "Different subject", "sent_at": "2026-06-01T11:00:00Z", "idempotency_key": "k2"},
    ]

    # Reply from alice with "Re:" prefix
    match = match_by_subject("alice@acme.com", "Re: Confirmed media buy for Nike", sent_rows)
    assert_true(match is not None, "subject match found")
    assert_eq(match["idempotency_key"], "k1", "matched correct row by subject+recipient")

    # Wrong sender
    no_match = match_by_subject("stranger@x.com", "Re: Confirmed media buy for Nike", sent_rows)
    assert_eq(no_match, None, "wrong sender → no match")

    # Right sender, wrong subject
    no_match2 = match_by_subject("alice@acme.com", "Re: Totally unrelated", sent_rows)
    assert_eq(no_match2, None, "wrong subject → no match")


def test_match_combined_thread_priority():
    print("\n--- Test: Combined match prefers thread ID ---")

    sent_rows = [
        # This row matches by subject but is older
        {"thread_id": "", "recipient_email": "alice@acme.com", "subject": "Nike deal", "sent_at": "2026-06-01T10:00:00Z", "idempotency_key": "subj-match"},
        # This row matches by thread_id and is the real target
        {"thread_id": "thread-X", "recipient_email": "alice@acme.com", "subject": "Nike deal", "sent_at": "2026-06-02T10:00:00Z", "idempotency_key": "thread-match"},
    ]

    # Reply has thread-X → should match thread, not subject
    match = match_reply_to_sent("thread-X", "alice@acme.com", "Re: Nike deal", sent_rows)
    assert_eq(match["idempotency_key"], "thread-match", "thread ID takes priority over subject")


def test_match_combined_subject_fallback():
    print("\n--- Test: Combined match falls back to subject when no thread ---")

    sent_rows = [
        {"thread_id": "", "recipient_email": "alice@acme.com", "subject": "Nike deal", "sent_at": "2026-06-01T10:00:00Z", "idempotency_key": "k1"},
    ]

    # Reply has no thread id → falls back to subject
    match = match_reply_to_sent("", "alice@acme.com", "Re: Nike deal", sent_rows)
    assert_eq(match["idempotency_key"], "k1", "fell back to subject match")


# ============================================================================
# Test: Engagement aggregation (audit errors 7.7, 7.15, 7.16)
# ============================================================================

def _make_row(template_id="outreach_v1", template_version="1", status="Sent",
              reply_status="none", campaign_id="c1", brand="Nike",
              subject_choice=None):
    """Build a fake Emails row for testing."""
    spin = {"subject": [], "body": []}
    if subject_choice:
        spin["subject"] = [{"pos": 0, "text": subject_choice}]
    return {
        "template_id": template_id,
        "template_version": template_version,
        "status": status,
        "reply_status": reply_status,
        "campaign_id": campaign_id,
        "brand": brand,
        "spin_path_json": json.dumps(spin),
    }


def test_overall_stats():
    print("\n--- Test: Overall stats computation ---")

    rows = [
        _make_row(reply_status="genuine"),
        _make_row(reply_status="genuine"),
        _make_row(reply_status="auto_reply"),
        _make_row(reply_status="bounce"),
        _make_row(reply_status="none"),
        _make_row(status="Queued"),  # not sent — excluded
    ]

    stats = compute_overall_stats(rows)
    assert_eq(stats.total_sent, 5, "5 sent (Queued excluded)")
    assert_eq(stats.genuine_replies, 2, "2 genuine replies")
    assert_eq(stats.auto_replies, 1, "1 auto reply")
    assert_eq(stats.bounces, 1, "1 bounce")
    assert_eq(stats.no_response, 1, "1 no response")
    assert_eq(stats.reply_rate, 40.0, "reply rate = 2/5 = 40%")
    assert_eq(stats.bounce_rate, 20.0, "bounce rate = 1/5 = 20%")


def test_variant_stats_grouping():
    print("\n--- Test: Variant stats group by template+version (audit error 7.16) ---")

    rows = [
        _make_row(template_id="outreach_v1", template_version="1", reply_status="genuine"),
        _make_row(template_id="outreach_v1", template_version="1", reply_status="none"),
        # Same template, DIFFERENT version → separate group
        _make_row(template_id="outreach_v1", template_version="2", reply_status="genuine"),
        _make_row(template_id="followup_v1", template_version="1", reply_status="none"),
    ]

    stats = compute_variant_stats(rows)
    # 3 distinct (template_id, version) groups
    assert_eq(len(stats), 3, "3 variant groups (version separates)")

    # Find outreach v1
    v1 = next(s for s in stats if s.template_id == "outreach_v1" and s.template_version == "1")
    assert_eq(v1.sent, 2, "outreach v1 sent = 2")
    assert_eq(v1.genuine_replies, 1, "outreach v1 genuine = 1")
    assert_eq(v1.reply_rate, 50.0, "outreach v1 reply rate = 50%")


def test_sample_size_guard():
    print("\n--- Test: Sample-size guard (audit errors 7.7, 7.15) ---")

    # A variant with only 3 sends — below MIN_SAMPLE_FOR_RANKING
    small_rows = [_make_row(reply_status="genuine") for _ in range(3)]
    stats = compute_variant_stats(small_rows)
    assert_true(not stats[0].has_sufficient_sample, "3 sends → insufficient sample")

    # A variant with MIN_SAMPLE sends — sufficient
    big_rows = [_make_row(reply_status="none") for _ in range(MIN_SAMPLE_FOR_RANKING)]
    stats = compute_variant_stats(big_rows)
    assert_true(stats[0].has_sufficient_sample, f"{MIN_SAMPLE_FOR_RANKING} sends → sufficient")


def test_identify_best_variant_guards():
    print("\n--- Test: Winner detection respects sample size ---")

    # All variants below threshold → no winner declared
    small_rows = [
        _make_row(template_id="a", reply_status="genuine"),
        _make_row(template_id="b", reply_status="none"),
    ]
    stats = compute_variant_stats(small_rows)
    best = identify_best_variant(stats)
    assert_eq(best, None, "no winner when all below sample threshold")

    # One variant with enough data → it wins
    big_rows = (
        [_make_row(template_id="winner", reply_status="genuine") for _ in range(10)] +
        [_make_row(template_id="winner", reply_status="none") for _ in range(10)]
    )
    stats = compute_variant_stats(big_rows)
    best = identify_best_variant(stats)
    assert_true(best is not None, "winner declared with sufficient sample")
    assert_eq(best.template_id, "winner", "correct winner")


def test_subject_choice_stats():
    print("\n--- Test: Subject choice attribution ---")

    rows = [
        _make_row(subject_choice="Confirmed media buy", reply_status="genuine"),
        _make_row(subject_choice="Confirmed media buy", reply_status="none"),
        _make_row(subject_choice="Active campaign", reply_status="none"),
        _make_row(subject_choice="Active campaign", reply_status="none"),
    ]

    stats = compute_subject_choice_stats(rows)
    assert_eq(len(stats), 2, "2 distinct subject choices")

    confirmed = next(s for s in stats if s.subject_choice == "Confirmed media buy")
    assert_eq(confirmed.sent, 2, "Confirmed sent = 2")
    assert_eq(confirmed.genuine_replies, 1, "Confirmed replies = 1")
    assert_eq(confirmed.reply_rate, 50.0, "Confirmed reply rate = 50%")


def test_subject_choice_skips_missing_spin():
    print("\n--- Test: Subject choice skips rows without spin data ---")

    rows = [
        _make_row(subject_choice="Has choice", reply_status="genuine"),
        _make_row(subject_choice=None, reply_status="genuine"),  # no spin path
    ]
    stats = compute_subject_choice_stats(rows)
    assert_eq(len(stats), 1, "only row with spin choice counted")


def test_campaign_stats():
    print("\n--- Test: Campaign stats grouping ---")

    rows = [
        _make_row(campaign_id="c1", brand="Nike", reply_status="genuine"),
        _make_row(campaign_id="c1", brand="Nike", reply_status="none"),
        _make_row(campaign_id="c2", brand="Adidas", reply_status="genuine"),
    ]

    stats = compute_campaign_stats(rows)
    assert_eq(len(stats), 2, "2 campaigns")

    c1 = next(s for s in stats if s.campaign_id == "c1")
    assert_eq(c1.sent, 2, "c1 sent = 2")
    assert_eq(c1.reply_rate, 50.0, "c1 reply rate = 50%")


# ============================================================================
# RUNNER
# ============================================================================

def run_all():
    print("=" * 60)
    print("Stage 7 Test Suite — Reply Tracking & Engagement")
    print("=" * 60)

    test_classify_genuine()
    test_classify_bounce()
    test_classify_unsubscribe()
    test_classify_auto_reply()
    test_classify_precedence()
    test_engagement_helpers()
    test_normalize_subject()
    test_match_by_thread_id()
    test_match_thread_id_most_recent()
    test_match_by_subject()
    test_match_combined_thread_priority()
    test_match_combined_subject_fallback()
    test_overall_stats()
    test_variant_stats_grouping()
    test_sample_size_guard()
    test_identify_best_variant_guards()
    test_subject_choice_stats()
    test_subject_choice_skips_missing_spin()
    test_campaign_stats()

    print("\n" + "=" * 60)
    print("Test suite complete.")
    print("=" * 60)


if __name__ == "__main__":
    run_all()
