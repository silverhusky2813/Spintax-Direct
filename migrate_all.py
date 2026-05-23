"""
migrate_all.py
===============
Run ALL schema migrations in order, idempotently. One command sets up every
tab and column the workflow needs.

Usage:
  python migrate_all.py

Or from inside Streamlit (one-time):
  from migrate_all import run_all_migrations
  run_all_migrations()

Order matters — each migration builds on the previous:
  v1 (base)  → Campaigns, Presets, Suppression tabs; campaign_id on Emails
  v2         → Publishers, cpm_rates tabs; target_geo + variant columns
  v3         → html_body, from_account, idempotency_key, retry columns
  v4         → sender_accounts, send_log tabs; priority_score, next_retry_at
  v5         → thread_id, reply_status, reply columns; reply_log, tracking_meta
  v6         → warm-up + pause columns; account_health_log

All migrations are individually idempotent, so re-running this is safe.
"""

import sys


def run_all_migrations(verbose: bool = True):
    """Run every schema migration in dependency order."""
    if verbose:
        print("\n" + "=" * 64)
        print("  PremiumAds Spintax Tool — Full Schema Migration")
        print("=" * 64)

    steps = [
        ("Base schema (Campaigns, Presets, Suppression)", _run_v1),
        ("Stage 2 (Publishers, cpm_rates, variant tracking)", _run_v2),
        ("Stage 3 (HTML body, sender, idempotency, retries)", _run_v3),
        ("Stage 5 (sender_accounts, send_log, priority)", _run_v4),
        ("Stage 7 (reply tracking, reply_log, watermark)", _run_v5),
        ("Stage 6 (warm-up, pause tracking, health log)", _run_v6),
    ]

    for i, (label, fn) in enumerate(steps, 1):
        if verbose:
            print(f"\n[{i}/{len(steps)}] {label}")
            print("-" * 64)
        try:
            fn()
        except Exception as e:
            print(f"  ✗ MIGRATION FAILED at step {i}: {type(e).__name__}: {e}")
            print("  Fix the issue and re-run — migrations are idempotent.")
            sys.exit(1)

    if verbose:
        print("\n" + "=" * 64)
        print("  ✓ All migrations complete. Schema is ready.")
        print("=" * 64 + "\n")


# Each wrapper imports lazily so a failure in one module doesn't block others
def _run_v1():
    from schema_setup import run_migration
    run_migration(verbose=True)

def _run_v2():
    from schema_setup_v2 import run_migration_v2
    run_migration_v2(verbose=True)

def _run_v3():
    from schema_setup_v3 import run_migration_v3
    run_migration_v3(verbose=True)

def _run_v4():
    from schema_setup_v4 import run_migration_v4
    run_migration_v4(verbose=True)

def _run_v5():
    from schema_setup_v5 import run_migration_v5
    run_migration_v5(verbose=True)

def _run_v6():
    from schema_setup_v6 import run_migration_v6
    run_migration_v6(verbose=True)


if __name__ == "__main__":
    run_all_migrations()
