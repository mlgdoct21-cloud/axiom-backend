"""Public Macro router — read-only endpoints consumed by the dashboard chip.

No auth: these power the public Macro Pulse mini-chip and its release-detail
modal. Admin endpoints in `macro_admin.py` keep BOT_INTERNAL_SECRET gating.

Two endpoints:
- GET /macro/latest         single most-recent release w/ narrative
- GET /macro/recent?days=14 list of recent releases (chip drill-down list)

Cache headers are short (60s) — releases drop hourly at most, but the chip
should feel fresh; the dashboard hits this on load and on its own refresh tick.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Query, Response
from sqlalchemy import text

from core.database import engine
from core.logger import get_logger

logger = get_logger("macro_public")

router = APIRouter(prefix="/macro", tags=["macro"])

_CACHE_HEADER = "public, max-age=60, stale-while-revalidate=120"


def _row_to_release(r) -> dict:
    return {
        "event_id": r["event_id"],
        "event_type": r["event_type"],
        "country": r["country"],
        "source": r["source"],
        "released_at": r["released_at"].isoformat() if r["released_at"] else None,
        "actual_value": float(r["actual_value"]) if r["actual_value"] is not None else None,
        "prior_value": float(r["prior_value"]) if r["prior_value"] is not None else None,
        "narrative_md": r["narrative_md"],
        "sentiment_score": float(r["sentiment_score"]) if r["sentiment_score"] is not None else None,
        "source_url": r["source_url"],
        "sectors_positive": _coerce_jsonb_list(r["sectors_positive"] if "sectors_positive" in r else None),
        "sectors_negative": _coerce_jsonb_list(r["sectors_negative"] if "sectors_negative" in r else None),
    }


def _coerce_jsonb_list(v) -> list:
    """JSONB columns can come back as list (asyncpg) or str (string driver).
    Normalise to a plain list of strings; empty fallback on anything weird.
    """
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if x]
    if isinstance(v, str):
        try:
            import json as _json
            parsed = _json.loads(v)
            return [str(x) for x in parsed if x] if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


@router.get("/latest")
async def latest_release(response: Response):
    """Single most-recent release that has a narrative_md filled in.

    Sort intent: a rich CPI/NFP narrative ("Önceki dönem 327.46 iken bu dönem
    330.293 geldi…") is more useful than a bare fed_rss title even if the
    fed_rss row is more recent. So we rank `actual_value IS NOT NULL` rows
    first, then by recency. 180-day window prevents an ancient rich row from
    sticking forever; FRED's `released_at` stores the observation-period start
    (e.g. March CPI sits on 2026-03-01) not the publication date, so a tight
    60-day window would falsely exclude perfectly fresh prints.

    Returns 200 with `release: null` rather than 404 when there's nothing yet,
    so the dashboard chip can render an "Henüz yeni release yok" state without
    triggering an error path.
    """
    response.headers["Cache-Control"] = _CACHE_HEADER
    sql = text("""
        SELECT event_id, event_type, country, source, released_at,
               actual_value, prior_value, narrative_md, sentiment_score, source_url,
               sectors_positive, sectors_negative
        FROM macro_releases
        WHERE narrative_md IS NOT NULL
          AND released_at >= NOW() - INTERVAL '180 days'
        ORDER BY (actual_value IS NOT NULL) DESC,
                 released_at DESC NULLS LAST,
                 created_at DESC
        LIMIT 1
    """)
    async with engine.begin() as conn:
        row = (await conn.execute(sql)).mappings().first()
    return {
        "now": datetime.now(timezone.utc).isoformat(),
        "release": _row_to_release(row) if row else None,
    }


@router.get("/recent")
async def recent_releases(
    response: Response,
    days: int = Query(14, ge=1, le=90),
    limit: int = Query(20, ge=1, le=100),
):
    """Recent releases (with or without narrative). Powers the modal drill-down."""
    response.headers["Cache-Control"] = _CACHE_HEADER
    sql = text("""
        SELECT event_id, event_type, country, source, released_at,
               actual_value, prior_value, narrative_md, sentiment_score, source_url,
               sectors_positive, sectors_negative
        FROM macro_releases
        WHERE released_at >= NOW() - make_interval(days => :days)
        ORDER BY released_at DESC NULLS LAST, created_at DESC
        LIMIT :limit
    """)
    async with engine.begin() as conn:
        rows = (await conn.execute(sql, {"days": days, "limit": limit})).mappings().all()
    return {
        "days": days,
        "count": len(rows),
        "releases": [_row_to_release(r) for r in rows],
    }


@router.get("/release/{event_id:path}")
async def release_detail(event_id: str, response: Response):
    """Single release lookup by event_id. `:path` lets the colons in
    `fred:CPI:2026-03-01` style IDs pass through unencoded.
    """
    response.headers["Cache-Control"] = _CACHE_HEADER
    sql = text("""
        SELECT event_id, event_type, country, source, released_at,
               actual_value, prior_value, narrative_md, sentiment_score, source_url,
               sectors_positive, sectors_negative
        FROM macro_releases
        WHERE event_id = :eid
    """)
    async with engine.begin() as conn:
        row = (await conn.execute(sql, {"eid": event_id})).mappings().first()
    return {"release": _row_to_release(row) if row else None}
