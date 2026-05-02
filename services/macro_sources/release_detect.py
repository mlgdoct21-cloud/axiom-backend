"""Release detection — write new macro events into `macro_releases`.

Idempotent: every event has a deterministic `event_id`; INSERT ON CONFLICT
DO NOTHING ensures repeated probes don't re-insert the same period.

Triggered from `reliability_probe` after each successful source fetch:
- FRED: per-series, observation-based event_id (`fred:CPI:2025-04-01`)
- fed_rss: per-event, parser already produced `fed_rss:<sha1>` ids

For FRED, `released_at` stores the observation period start date — not the
true publication date, which FRED's series/observations endpoint doesn't
expose. The macro_narrative phase will fill in publication dates from the
`fred/releases/dates` endpoint when we need them.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy import text

from core.database import engine
from core.logger import get_logger
from services.macro_sources.fed_rss import ReleaseEvent
from services.macro_sources.fred_api import SERIES as FRED_SERIES

logger = get_logger("macro.release_detect")


# FRED source name → macro_releases.event_type canonical label.
_FRED_EVENT_TYPE = {
    "fred_cpi": "CPI",
    "fred_nfp": "NFP",
}


def _decimal_or_none(raw: Optional[str]) -> Optional[Decimal]:
    if raw is None or raw == "" or raw == ".":
        return None
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        return None


def _parse_obs_date(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


async def record_fred_observation(
    source: str,
    latest_date: Optional[str],
    latest_value: Optional[str],
    prior_value: Optional[str],
) -> bool:
    """Insert one FRED observation as a `macro_releases` row.

    Returns True only when a new row was inserted (deterministic event_id +
    ON CONFLICT DO NOTHING). Missing observations ("." values) are skipped.
    """
    event_type = _FRED_EVENT_TYPE.get(source)
    if not event_type or not latest_date:
        return False
    series_id = FRED_SERIES.get(source, "")
    event_id = f"fred:{event_type}:{latest_date}"

    released_at = _parse_obs_date(latest_date)
    if released_at is None:
        return False

    actual = _decimal_or_none(latest_value)
    prior = _decimal_or_none(prior_value)
    if actual is None:
        return False

    sql = text("""
        INSERT INTO macro_releases
        (event_id, event_type, country, released_at, prior_value, actual_value, source, source_url)
        VALUES
        (:event_id, :event_type, 'US', :released_at, :prior_value, :actual_value, 'fred', :source_url)
        ON CONFLICT (event_id) DO NOTHING
        RETURNING event_id
    """)
    params = {
        "event_id": event_id,
        "event_type": event_type,
        "released_at": released_at,
        "prior_value": prior,
        "actual_value": actual,
        "source_url": f"https://fred.stlouisfed.org/series/{series_id}",
    }
    try:
        async with engine.begin() as conn:
            row = (await conn.execute(sql, params)).first()
        if row is not None:
            logger.info(f"new release: {event_id} actual={actual} prior={prior}")
            return True
        return False
    except Exception as e:
        logger.error(f"record_fred_observation failed for {event_id}: {e}")
        return False


async def record_fed_rss_events(events: list[ReleaseEvent]) -> int:
    """Bulk INSERT new fed_rss events. Returns count of newly inserted rows."""
    if not events:
        return 0
    sql = text("""
        INSERT INTO macro_releases
        (event_id, event_type, country, released_at, source, source_url, narrative_md)
        VALUES
        (:event_id, :event_type, 'US', :released_at, 'fed_rss', :source_url, :narrative_md)
        ON CONFLICT (event_id) DO NOTHING
        RETURNING event_id
    """)
    inserted = 0
    try:
        async with engine.begin() as conn:
            for ev in events:
                params = {
                    "event_id": ev.event_id,
                    "event_type": ev.event_type,
                    "released_at": ev.released_at,
                    "source_url": ev.url,
                    "narrative_md": (ev.title or "").strip()[:500] or None,
                }
                row = (await conn.execute(sql, params)).first()
                if row is not None:
                    inserted += 1
                    logger.info(f"new release: {ev.event_id} type={ev.event_type}")
    except Exception as e:
        logger.error(f"record_fed_rss_events failed: {e}")
    return inserted
