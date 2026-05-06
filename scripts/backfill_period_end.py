"""One-shot backfill of users.current_period_end for existing paid subs.

The webhook handler writes current_period_end going forward (alembic 016 +
services/stripe_billing._period_end_from). Users who upgraded BEFORE this
landed have NULL — Stripe still knows the period_end, but we never stored it.

This script reads every users row with stripe_subscription_id IS NOT NULL
AND current_period_end IS NULL, fetches the subscription from Stripe, and
writes the period_end. Idempotent — safe to re-run.

Run on Railway:
    railway run --service vivacious-growth -- python scripts/backfill_period_end.py
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from core.database import AsyncSessionLocal  # noqa: E402
from core.logger import get_logger  # noqa: E402

logger = get_logger("backfill_period_end")


async def main() -> int:
    try:
        import stripe  # noqa: WPS433
    except Exception as e:
        print(f"FATAL: stripe SDK not importable: {e}")
        return 2

    api_key = os.getenv("STRIPE_SECRET_KEY", "").strip()
    if not api_key:
        print("FATAL: STRIPE_SECRET_KEY not set")
        return 2
    stripe.api_key = api_key

    select_sql = text("""
        SELECT id, telegram_id, stripe_subscription_id
        FROM users
        WHERE stripe_subscription_id IS NOT NULL
          AND current_period_end IS NULL
        ORDER BY id ASC
    """)
    update_sql = text("""
        UPDATE users SET current_period_end = :cpe
        WHERE id = :uid
    """)

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(select_sql)).fetchall()

    if not rows:
        print("No rows to backfill — exiting clean.")
        return 0

    print(f"Found {len(rows)} candidate row(s). Fetching from Stripe...")
    updated = 0
    skipped = 0
    failed = 0
    for row in rows:
        uid, tid, sub_id = row[0], row[1], row[2]
        try:
            sub = stripe.Subscription.retrieve(sub_id)
            cpe_ts = sub.get("current_period_end") if hasattr(sub, "get") else getattr(sub, "current_period_end", None)
            if not cpe_ts:
                print(f"  user {uid} (tid={tid}, sub={sub_id}): no current_period_end on Stripe object — skip")
                skipped += 1
                continue
            cpe = datetime.fromtimestamp(int(cpe_ts), tz=timezone.utc)
            async with AsyncSessionLocal() as session:
                await session.execute(update_sql, {"uid": uid, "cpe": cpe})
                await session.commit()
            print(f"  user {uid} (tid={tid}): current_period_end → {cpe.isoformat()}")
            updated += 1
        except Exception as e:
            print(f"  user {uid} (tid={tid}, sub={sub_id}): FAIL {e}")
            failed += 1

    print(f"\nBackfill done. updated={updated} skipped={skipped} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
