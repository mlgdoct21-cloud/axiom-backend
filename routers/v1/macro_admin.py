"""Admin Macro router — reliability stats + manual probe trigger.

Hafta 1 verification kriteri (>=99% uptime, p95<3s) bu endpoint'ten
kontrol edilir. BOT_INTERNAL_SECRET ile auth.
"""
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query
from sqlalchemy import text

from core.database import engine
from core.logger import get_logger
from services.macro_calendar import upcoming_events
from services.macro_broadcaster import broadcast_release
from services.macro_narrative import generate_narrative
from services.macro_sources.reliability_probe import (
    probe_once,
    rolling_health_report,
)

logger = get_logger("macro_admin")

router = APIRouter(prefix="/admin/macro", tags=["admin"])


def _check_auth(x_internal_secret: Optional[str]) -> None:
    expected = os.getenv("BOT_INTERNAL_SECRET", "").strip()
    if not expected or x_internal_secret != expected:
        raise HTTPException(status_code=403, detail="Forbidden")


@router.get("/health")
async def health_report(
    source: str = Query("fed_rss"),
    hours: int = Query(168, ge=1, le=720),
    x_internal_secret: Optional[str] = Header(None),
):
    _check_auth(x_internal_secret)
    return await rolling_health_report(source=source, hours=hours)


@router.post("/probe")
async def trigger_probe(x_internal_secret: Optional[str] = Header(None)):
    """Force-probe now (debug). Bypasses the 5-min interval."""
    _check_auth(x_internal_secret)
    return await probe_once()


@router.get("/calendar")
async def calendar_view(
    days: int = Query(14, ge=1, le=180),
    x_internal_secret: Optional[str] = Header(None),
):
    """Combined view: upcoming YAML events + recent macro_releases rows."""
    _check_auth(x_internal_secret)
    now = datetime.now(timezone.utc)
    upcoming = [
        {
            "event_type": e.event_type,
            "label": e.label,
            "scheduled_at": e.scheduled_at.isoformat(),
            "sources_to_accelerate": list(e.sources_to_accelerate),
        }
        for e in upcoming_events(now, days=days)
    ]
    sql = text("""
        SELECT event_id, event_type, source, released_at, actual_value, prior_value, narrative_md
        FROM macro_releases
        WHERE released_at >= NOW() - make_interval(days => :days)
        ORDER BY released_at DESC
        LIMIT 50
    """)
    async with engine.begin() as conn:
        rows = (await conn.execute(sql, {"days": days})).mappings().all()
    recent = [
        {
            "event_id": r["event_id"],
            "event_type": r["event_type"],
            "source": r["source"],
            "released_at": r["released_at"].isoformat() if r["released_at"] else None,
            "actual_value": float(r["actual_value"]) if r["actual_value"] is not None else None,
            "prior_value": float(r["prior_value"]) if r["prior_value"] is not None else None,
            "has_narrative": bool(r["narrative_md"]),
        }
        for r in rows
    ]
    return {"now": now.isoformat(), "days": days, "upcoming": upcoming, "recent": recent}


@router.post("/narrative/{event_id}")
async def regenerate_narrative(
    event_id: str,
    force: bool = Query(False, description="If true, NULL the existing narrative_md before regen"),
    x_internal_secret: Optional[str] = Header(None),
):
    """Manual narrative (re)generation. With ?force=true, clears the existing
    narrative first so the idempotent UPDATE inside generate_narrative writes."""
    _check_auth(x_internal_secret)
    if force:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE macro_releases SET narrative_md = NULL, sentiment_score = NULL "
                     "WHERE event_id = :eid"),
                {"eid": event_id},
            )
    res = await generate_narrative(event_id)
    return {
        "event_id": res.event_id,
        "written": res.written,
        "used_fallback": res.used_fallback,
        "rejection_reason": res.rejection_reason,
        "narrative_md": res.narrative_md,
        "sentiment_score": res.sentiment_score,
    }


@router.post("/broadcast/{event_id}")
async def trigger_broadcast(
    event_id: str,
    x_internal_secret: Optional[str] = Header(None),
):
    """Manual Telegram broadcast for an existing release. Useful for debug or
    re-sending after a Telegram outage. Idempotent guard lives upstream in
    generate_narrative (broadcast only auto-fires on a NEW narrative_md write)
    — calling this manually intentionally bypasses that gate."""
    _check_auth(x_internal_secret)
    return await broadcast_release(event_id)


@router.post("/expected/{event_id:path}")
async def set_expected(
    event_id: str,
    expected_mom_pct: Optional[float] = Query(None, description="Consensus MoM % (e.g. 1.0)"),
    expected_yoy_pct: Optional[float] = Query(None, description="Consensus YoY % (e.g. 3.4)"),
    x_internal_secret: Optional[str] = Header(None),
):
    """Admin entry for the consensus % readings. Pass either or both;
    omitting one leaves the existing value untouched. Returns the row's
    new state. Idempotent — safe to call repeatedly to correct typos.

    There's deliberately no public scrape source for consensus values
    (Trading Economics paid, ForexFactory ToS-restricted) — admin entry
    is the lean path until we add a paid feed.
    """
    _check_auth(x_internal_secret)
    if expected_mom_pct is None and expected_yoy_pct is None:
        raise HTTPException(status_code=400, detail="Provide expected_mom_pct or expected_yoy_pct")
    sets = []
    params: dict = {"eid": event_id}
    if expected_mom_pct is not None:
        sets.append("expected_mom_pct = :mom")
        params["mom"] = expected_mom_pct
    if expected_yoy_pct is not None:
        sets.append("expected_yoy_pct = :yoy")
        params["yoy"] = expected_yoy_pct
    sql = text(
        f"UPDATE macro_releases SET {', '.join(sets)} "
        "WHERE event_id = :eid "
        "RETURNING event_id, event_type, expected_mom_pct, expected_yoy_pct"
    )
    async with engine.begin() as conn:
        row = (await conn.execute(sql, params)).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail=f"event_id {event_id} not found")
    return {
        "event_id": row["event_id"],
        "event_type": row["event_type"],
        "expected_mom_pct": float(row["expected_mom_pct"]) if row["expected_mom_pct"] is not None else None,
        "expected_yoy_pct": float(row["expected_yoy_pct"]) if row["expected_yoy_pct"] is not None else None,
    }
