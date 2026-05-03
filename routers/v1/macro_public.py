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


# Event types where actual/prior are price indices and MoM/YoY % are the
# relevant readings. Headline ↔ Core pairs share the same observation period
# so we look up the paired Core release by date.
_PCT_DELTA_EVENTS = frozenset({"CPI", "PCE", "CORE_CPI", "CORE_PCE"})

# Headline → Core paired event_type for the same period (both are indices,
# both report MoM%/YoY% the same way).
_CORE_PAIR = {"CPI": "CORE_CPI", "PCE": "CORE_PCE"}


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


def _pct(actual: Optional[float], reference: Optional[float]) -> Optional[float]:
    if actual is None or reference is None or reference == 0:
        return None
    return round((actual - reference) / abs(reference) * 100, 2)


async def _fetch_market_reaction(conn, event_id: str) -> Optional[dict]:
    """Look up T+0 + T+5min snapshots for a release and return deltas:
    DXY/SPY as %, US10Y as bp (basis points). Returns None when neither
    snapshot is available — the broadcaster + modal then skip the line.
    """
    sql = text("""
        SELECT t_offset_seconds, dxy, spy, us10y, taken_at
        FROM macro_release_market_snapshots
        WHERE event_id = :eid
        ORDER BY t_offset_seconds ASC
    """)
    rows = (await conn.execute(sql, {"eid": event_id})).mappings().all()
    if not rows:
        return None
    by_offset = {r["t_offset_seconds"]: r for r in rows}
    t0 = by_offset.get(0)
    t5 = by_offset.get(300)
    if not t0 or not t5:
        # Have one but not both — show absolute T+0 only.
        ref = t0 or t5
        return {
            "t_offset_seconds": int(ref["t_offset_seconds"]),
            "dxy_change_pct": None,
            "spy_change_pct": None,
            "us10y_change_bp": None,
            "snapshot": {
                "dxy": float(ref["dxy"]) if ref["dxy"] is not None else None,
                "spy": float(ref["spy"]) if ref["spy"] is not None else None,
                "us10y": float(ref["us10y"]) if ref["us10y"] is not None else None,
            },
        }

    def _pct_delta(a, b):
        if a is None or b is None or b == 0:
            return None
        return round((float(a) - float(b)) / abs(float(b)) * 100, 3)

    def _bp_delta(a, b):
        if a is None or b is None:
            return None
        # ^TNX value is in % (4.25 = 4.25%); 1 bp = 0.01%.
        return round((float(a) - float(b)) * 100, 1)

    return {
        "t_offset_seconds": 300,
        "dxy_change_pct": _pct_delta(t5["dxy"], t0["dxy"]),
        "spy_change_pct": _pct_delta(t5["spy"], t0["spy"]),
        "us10y_change_bp": _bp_delta(t5["us10y"], t0["us10y"]),
        "snapshot": {
            "dxy": float(t5["dxy"]) if t5["dxy"] is not None else None,
            "spy": float(t5["spy"]) if t5["spy"] is not None else None,
            "us10y": float(t5["us10y"]) if t5["us10y"] is not None else None,
        },
    }


async def _fetch_history_value(
    conn, event_type: str, source: str, target_date: datetime,
) -> Optional[float]:
    """Look up actual_value of a release older than `target_date` for the same
    event_type+source. We sort DESC and pick the first row whose released_at
    is at-or-before target_date — handles both 1-month-prior (for prior_pct)
    and 12-month-prior (for YoY) by passing different target_date values.
    """
    sql = text("""
        SELECT actual_value FROM macro_releases
        WHERE event_type = :et AND source = :src
          AND released_at <= :ts
          AND actual_value IS NOT NULL
        ORDER BY released_at DESC LIMIT 1
    """)
    row = (await conn.execute(sql, {"et": event_type, "src": source, "ts": target_date})).first()
    if row is None:
        return None
    return float(row[0]) if row[0] is not None else None


async def _enrich_release(conn, r) -> dict:
    """Compute all derivative %s and the paired Core release on top of a row."""
    actual = float(r["actual_value"]) if r["actual_value"] is not None else None
    prior = float(r["prior_value"]) if r["prior_value"] is not None else None
    et = (r["event_type"] or "").upper()
    src = r["source"] or ""
    released_at = r["released_at"]

    mom_pct: Optional[float] = None
    yoy_pct: Optional[float] = None
    prior_mom_pct: Optional[float] = None
    prior_yoy_pct: Optional[float] = None
    if et in _PCT_DELTA_EVENTS:
        mom_pct = _pct(actual, prior)
        # 12-mo-prior: query historical row from ~365 days before released_at.
        if released_at is not None:
            from datetime import timedelta
            yoy_target = released_at - timedelta(days=350)
            yr_ago = await _fetch_history_value(conn, et, src, yoy_target)
            yoy_pct = _pct(actual, yr_ago)
            # Previous month's MoM = look up T-1 row, then compute its mom.
            prev_target = released_at - timedelta(days=20)
            prev_actual = await _fetch_history_value(conn, et, src, prev_target)
            if prev_actual is not None:
                prev_prior_target = released_at - timedelta(days=50)
                prev_prior = await _fetch_history_value(conn, et, src, prev_prior_target)
                prior_mom_pct = _pct(prev_actual, prev_prior)
                if prev_actual is not None:
                    prev_yr_target = released_at - timedelta(days=380)
                    prev_yr_ago = await _fetch_history_value(conn, et, src, prev_yr_target)
                    prior_yoy_pct = _pct(prev_actual, prev_yr_ago)

    expected_mom_pct = (
        float(r["expected_mom_pct"]) if "expected_mom_pct" in r and r["expected_mom_pct"] is not None
        else None
    )
    expected_yoy_pct = (
        float(r["expected_yoy_pct"]) if "expected_yoy_pct" in r and r["expected_yoy_pct"] is not None
        else None
    )

    surprise_mom_pp: Optional[float] = None
    surprise_yoy_pp: Optional[float] = None
    if mom_pct is not None and expected_mom_pct is not None:
        surprise_mom_pp = round(mom_pct - expected_mom_pct, 2)
    if yoy_pct is not None and expected_yoy_pct is not None:
        surprise_yoy_pp = round(yoy_pct - expected_yoy_pct, 2)

    market_reaction = await _fetch_market_reaction(conn, r["event_id"])

    return {
        "event_id": r["event_id"],
        "event_type": r["event_type"],
        "country": r["country"],
        "source": r["source"],
        "released_at": released_at.isoformat() if released_at else None,
        "actual_value": actual,
        "prior_value": prior,
        "mom_pct": mom_pct,
        "yoy_pct": yoy_pct,
        "prior_mom_pct": prior_mom_pct,
        "prior_yoy_pct": prior_yoy_pct,
        "expected_mom_pct": expected_mom_pct,
        "expected_yoy_pct": expected_yoy_pct,
        "surprise_mom_pp": surprise_mom_pp,
        "surprise_yoy_pp": surprise_yoy_pp,
        "market_reaction": market_reaction,
        "narrative_md": r["narrative_md"],
        "sentiment_score": float(r["sentiment_score"]) if r["sentiment_score"] is not None else None,
        "source_url": r["source_url"],
        "sectors_positive": _coerce_jsonb_list(r["sectors_positive"] if "sectors_positive" in r else None),
        "sectors_negative": _coerce_jsonb_list(r["sectors_negative"] if "sectors_negative" in r else None),
    }


_SELECT_COLS = (
    "event_id, event_type, country, source, released_at, "
    "actual_value, prior_value, narrative_md, sentiment_score, source_url, "
    "sectors_positive, sectors_negative, expected_mom_pct, expected_yoy_pct"
)


async def _fetch_paired_core(conn, headline) -> Optional[dict]:
    """If `headline` is CPI/PCE, look up its Core sibling for the same period.
    Returns enriched dict or None if no paired row exists.
    """
    et = (headline["event_type"] or "").upper()
    pair_et = _CORE_PAIR.get(et)
    if not pair_et or not headline["released_at"]:
        return None
    sql = text(f"""
        SELECT {_SELECT_COLS}
        FROM macro_releases
        WHERE event_type = :et AND source = :src AND released_at = :ts
        LIMIT 1
    """)
    row = (await conn.execute(sql, {
        "et": pair_et, "src": headline["source"], "ts": headline["released_at"],
    })).mappings().first()
    if not row:
        return None
    return await _enrich_release(conn, row)


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
    sql = text(f"""
        SELECT {_SELECT_COLS}
        FROM macro_releases
        WHERE narrative_md IS NOT NULL
          AND released_at >= NOW() - INTERVAL '180 days'
          AND event_type NOT LIKE 'CORE_%'
        ORDER BY (actual_value IS NOT NULL) DESC,
                 released_at DESC NULLS LAST,
                 created_at DESC
        LIMIT 1
    """)
    async with engine.begin() as conn:
        row = (await conn.execute(sql)).mappings().first()
        if not row:
            return {
                "now": datetime.now(timezone.utc).isoformat(),
                "release": None,
                "core_release": None,
            }
        enriched = await _enrich_release(conn, row)
        core = await _fetch_paired_core(conn, row)
    return {
        "now": datetime.now(timezone.utc).isoformat(),
        "release": enriched,
        "core_release": core,
    }


@router.get("/recent")
async def recent_releases(
    response: Response,
    days: int = Query(14, ge=1, le=90),
    limit: int = Query(20, ge=1, le=100),
):
    """Recent releases (with or without narrative). Powers the modal drill-down."""
    response.headers["Cache-Control"] = _CACHE_HEADER
    sql = text(f"""
        SELECT {_SELECT_COLS}
        FROM macro_releases
        WHERE released_at >= NOW() - make_interval(days => :days)
        ORDER BY released_at DESC NULLS LAST, created_at DESC
        LIMIT :limit
    """)
    async with engine.begin() as conn:
        rows = (await conn.execute(sql, {"days": days, "limit": limit})).mappings().all()
        out = [await _enrich_release(conn, r) for r in rows]
    return {
        "days": days,
        "count": len(out),
        "releases": out,
    }


@router.get("/history/{event_type}")
async def history(
    event_type: str,
    response: Response,
    months: int = Query(14, ge=1, le=60),
    source: str = Query("fred"),
):
    """Time series of MoM% / YoY% for a single event_type, oldest-first.
    Powers the dashboard's modal-embedded line chart.

    `event_type` is the canonical label (CPI, CORE_CPI, PCE, CORE_PCE).
    """
    response.headers["Cache-Control"] = _CACHE_HEADER
    et = (event_type or "").upper()
    sql = text("""
        SELECT event_id, released_at, actual_value, prior_value
        FROM macro_releases
        WHERE event_type = :et AND source = :src AND actual_value IS NOT NULL
        ORDER BY released_at DESC
        LIMIT :months
    """)
    async with engine.begin() as conn:
        rows = (await conn.execute(sql, {
            "et": et, "src": source, "months": months,
        })).mappings().all()
        if not rows:
            return {"event_type": et, "source": source, "points": []}
        # Reverse to ascending for chart consumption.
        rows = list(reversed(rows))
        out = []
        for r in rows:
            actual = float(r["actual_value"]) if r["actual_value"] is not None else None
            prior = float(r["prior_value"]) if r["prior_value"] is not None else None
            mom = _pct(actual, prior) if et in _PCT_DELTA_EVENTS else None
            yoy_target = r["released_at"] - timedelta_days(350)
            yr_ago = await _fetch_history_value(conn, et, source, yoy_target)
            yoy = _pct(actual, yr_ago) if et in _PCT_DELTA_EVENTS else None
            out.append({
                "event_id": r["event_id"],
                "released_at": r["released_at"].isoformat(),
                "actual_value": actual,
                "prior_value": prior,
                "mom_pct": mom,
                "yoy_pct": yoy,
            })
    return {"event_type": et, "source": source, "points": out}


def timedelta_days(days: int):
    """Local alias to keep the import section tidy."""
    from datetime import timedelta
    return timedelta(days=days)


@router.get("/release/{event_id:path}")
async def release_detail(event_id: str, response: Response):
    """Single release lookup by event_id. `:path` lets the colons in
    `fred:CPI:2026-03-01` style IDs pass through unencoded.
    """
    response.headers["Cache-Control"] = _CACHE_HEADER
    sql = text(f"""
        SELECT {_SELECT_COLS}
        FROM macro_releases
        WHERE event_id = :eid
    """)
    async with engine.begin() as conn:
        row = (await conn.execute(sql, {"eid": event_id})).mappings().first()
        if not row:
            return {"release": None, "core_release": None}
        enriched = await _enrich_release(conn, row)
        core = await _fetch_paired_core(conn, row)
    return {"release": enriched, "core_release": core}
