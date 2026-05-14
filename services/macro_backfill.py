"""Periodic catch-up for releases that landed but their narrative/story
fire-and-forget tasks were lost (GC, restart, exception in async chain).

Fired from `reliability_probe.probe_once` on a ~5min cadence — covers the
release-detect → generate_narrative → broadcast / generate_story chain
end-to-end. Idempotent: skips rows that are already complete.

Incident that motivated this: 2026-05-13 PPI Apr. Release inserted at
12:34:41 UTC via FMP probe, but the fire-and-forget narrative task was
GC'd before `generate_narrative` ran. narrative_md stayed NULL, no
Telegram broadcast went out, no storyteller story row written.

Strong-ref pattern fix in release_detect/macro_narrative is the primary
guard. This module is the safety net — even if a task is lost for any
reason (restart mid-flight, Gemini timeout exception escape, etc.), the
next tick will pick it up within 5 minutes.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text

from core.database import engine
from core.logger import get_logger

logger = get_logger("macro.backfill")

# Look-back window: 6h covers normal release-detect → narrative → broadcast
# latency, restarts, and Gemini/Telegram transient outages, without dragging
# in day-old "already missed the moment" releases. Filter is on `created_at`
# (DB insert wall-clock) not `released_at` (observation-period start, which
# is always the 1st of the previous month for monthly releases — useless
# for freshness filtering).
#
# 2026-05-13 PPI Apr context: filed at created_at=12:34:41 UTC, this catches
# it within 6h. Day-old JOBLESS releases (created 2026-05-12) fall outside
# this window — they need an explicit admin /backfill call, NOT an
# automatic 24h-late Telegram push that would look broken to users.
_BACKFILL_WINDOW = timedelta(hours=6)

# Minimum spacing between full backfill passes. probe_once ticks every
# 30s but backfill is heavier (Gemini calls), so we throttle to 5 min.
_MIN_INTERVAL = timedelta(minutes=5)

_LAST_RUN_AT: Optional[datetime] = None

# Cost guard: per-event retry counter (process-life). After MAX_RETRIES
# attempts where the row is still NULL or the story tier is still missing,
# subsequent backfill ticks skip that event. Prevents a stuck release
# (validator-reject loop, persistent Gemini outage, broken event_type)
# from burning Gemini calls indefinitely. Restart clears the counter,
# which is the correct semantic — operator restarts after a fix should
# allow retry.
MAX_RETRIES_PER_EVENT = 3
_NARRATIVE_RETRY: dict[str, int] = {}
_STORY_RETRY: dict[tuple[str, str], int] = {}


def _is_due(now: datetime) -> bool:
    global _LAST_RUN_AT
    if _LAST_RUN_AT is None:
        return True
    return (now - _LAST_RUN_AT) >= _MIN_INTERVAL


async def _backfill_narratives(now: datetime) -> int:
    """Find macro_releases rows missing narrative_md, regenerate.

    Filters mirror the original trigger semantics (release_detect skips
    data-point sub-series like FED_FUNDS_*, NFP sectors), so this only
    looks at headline event types where a narrative is expected.
    """
    cutoff = now - _BACKFILL_WINDOW
    # Cost guard in SQL: skip patologically-revising rows (e.g. an event
    # caught in an FMP variant collision loop). revision_count >= 5
    # signals the row is unstable; don't waste Gemini calls until ops fix.
    sql = text("""
        SELECT event_id
        FROM macro_releases
        WHERE narrative_md IS NULL
          AND created_at >= :cutoff
          AND revision_count < 5
          AND event_type NOT LIKE 'SEP_%'
          AND event_type NOT LIKE 'NFP_%'
          AND event_type NOT LIKE 'CPI_%'
          AND event_type NOT LIKE 'PPI_%'
          AND event_type NOT IN ('FED_FUNDS_UPPER', 'FED_FUNDS_LOWER')
        ORDER BY created_at DESC
        LIMIT 20
    """)
    try:
        async with engine.begin() as conn:
            rows = (await conn.execute(sql, {"cutoff": cutoff})).all()
    except Exception as e:
        logger.warning(f"backfill narrative query failed: {e}")
        return 0

    event_ids = [r[0] for r in rows]
    if not event_ids:
        return 0

    # Cost guard in-process: drop event_ids that already exceeded MAX_RETRIES.
    eligible = [eid for eid in event_ids if _NARRATIVE_RETRY.get(eid, 0) < MAX_RETRIES_PER_EVENT]
    skipped = set(event_ids) - set(eligible)
    if skipped:
        logger.warning(
            f"backfill narrative: skipping {len(skipped)} event(s) at retry cap "
            f"({MAX_RETRIES_PER_EVENT}): {sorted(skipped)}"
        )
    if not eligible:
        return 0

    logger.info(f"backfill narratives: {len(eligible)} missing → {eligible}")
    fired = 0
    try:
        from services.macro_narrative import generate_narrative_safe
    except Exception as e:
        logger.error(f"backfill narrative import failed: {e}")
        return 0

    for eid in eligible:
        _NARRATIVE_RETRY[eid] = _NARRATIVE_RETRY.get(eid, 0) + 1
        try:
            await generate_narrative_safe(eid)
            fired += 1
        except Exception as e:
            logger.warning(f"backfill narrative fire {eid} failed: {e}")
    return fired


async def _backfill_stories(now: datetime) -> int:
    """Find releases with narrative_md filled but missing macro_stories
    rows for premium and/or advance tier, regenerate the missing tier(s).

    Triggers via generate_story_safe which is idempotent on (event_id, tier).
    """
    cutoff = now - _BACKFILL_WINDOW
    sql = text("""
        SELECT r.event_id,
               EXISTS(SELECT 1 FROM macro_stories s
                      WHERE s.event_id = r.event_id AND s.tier = 'premium') AS has_premium,
               EXISTS(SELECT 1 FROM macro_stories s
                      WHERE s.event_id = r.event_id AND s.tier = 'advance') AS has_advance
        FROM macro_releases r
        WHERE r.narrative_md IS NOT NULL
          AND r.created_at >= :cutoff
          AND r.revision_count < 5
          AND r.event_type NOT LIKE 'SEP_%'
          AND r.event_type NOT LIKE 'NFP_%'
          AND r.event_type NOT LIKE 'CPI_%'
          AND r.event_type NOT LIKE 'PPI_%'
          AND r.event_type NOT IN (
              'FED_FUNDS_UPPER', 'FED_FUNDS_LOWER',
              -- Core companions: storyteller anchors these to their parent
              -- series; standalone story would duplicate content.
              'CORE_CPI', 'CORE_PPI', 'CORE_PCE', 'CORE_RETAIL_SALES'
          )
        ORDER BY r.created_at DESC
        LIMIT 20
    """)
    try:
        async with engine.begin() as conn:
            rows = (await conn.execute(sql, {"cutoff": cutoff})).mappings().all()
    except Exception as e:
        logger.warning(f"backfill story query failed: {e}")
        return 0

    missing: list[tuple[str, str]] = []
    for r in rows:
        if not r["has_premium"]:
            missing.append((r["event_id"], "premium"))
        if not r["has_advance"]:
            missing.append((r["event_id"], "advance"))

    if not missing:
        return 0

    # Cost guard in-process: drop pairs at retry cap.
    eligible = [(eid, tier) for (eid, tier) in missing if _STORY_RETRY.get((eid, tier), 0) < MAX_RETRIES_PER_EVENT]
    skipped = set(missing) - set(eligible)
    if skipped:
        logger.warning(
            f"backfill story: skipping {len(skipped)} pair(s) at retry cap "
            f"({MAX_RETRIES_PER_EVENT}): {sorted(skipped)}"
        )
    if not eligible:
        return 0

    logger.info(f"backfill stories: {len(eligible)} (event_id, tier) pairs missing")
    fired = 0
    try:
        from services.macro_storyteller import generate_story_safe
    except Exception as e:
        logger.error(f"backfill story import failed: {e}")
        return 0

    for eid, tier in eligible:
        _STORY_RETRY[(eid, tier)] = _STORY_RETRY.get((eid, tier), 0) + 1
        try:
            await generate_story_safe(eid, tier, force=False)
            fired += 1
        except Exception as e:
            logger.warning(f"backfill story fire {eid}/{tier} failed: {e}")
    return fired


async def backfill_missing_narratives_and_stories_safe() -> dict:
    """Public entry point. Never raises. Throttled to _MIN_INTERVAL."""
    global _LAST_RUN_AT
    now = datetime.now(timezone.utc)
    if not _is_due(now):
        return {"skipped": "throttle", "next_eligible_at": _LAST_RUN_AT + _MIN_INTERVAL if _LAST_RUN_AT else None}
    _LAST_RUN_AT = now
    try:
        narratives = await _backfill_narratives(now)
        # Yield so generate_narrative's own _trigger_broadcast tasks
        # (which schedule storyteller in turn) can register before the
        # story backfill query runs — keeps redundant work minimal.
        await asyncio.sleep(0)
        stories = await _backfill_stories(now)
        return {"narratives_fired": narratives, "stories_fired": stories}
    except Exception as e:
        logger.error(f"backfill safe wrapper crashed: {e}")
        return {"error": str(e)}
