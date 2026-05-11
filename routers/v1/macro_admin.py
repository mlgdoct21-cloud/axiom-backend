"""Admin Macro router — reliability stats + manual probe trigger.

Hafta 1 verification kriteri (>=99% uptime, p95<3s) bu endpoint'ten
kontrol edilir. BOT_INTERNAL_SECRET ile auth.
"""
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Body, Header, HTTPException, Query
from sqlalchemy import text

from core.database import engine
from core.logger import get_logger
from services.macro_calendar import (
    invalidate_calendar_cache,
    load_calendar,
    upcoming_events,
)
from services.macro_sources.fred_calendar import cache_status as fred_calendar_cache_status
from services.macro_broadcaster import broadcast_release, broadcast_story
from services.macro_narrative import generate_narrative
from services.macro_revisions import broadcast_revision
from services.macro_storyteller import generate_story
from services.macro_sources.reliability_probe import (
    probe_once,
    rolling_health_report,
)
from services.macro_track_record import (
    insert_manual_verdict,
    validate_outcome_manually,
    list_outcomes,
    get_hit_rate,
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
        for e in await upcoming_events(now, days=days)
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
    return {
        "now": now.isoformat(),
        "days": days,
        "upcoming": upcoming,
        "recent": recent,
        "fred_calendar_cache": fred_calendar_cache_status(),
    }


@router.post("/calendar/refresh")
async def calendar_refresh(x_internal_secret: Optional[str] = Header(None)):
    """Drop the merged-calendar cache + FRED's 24h release/dates cache, then
    rebuild from scratch. Useful after a BLS reschedule or when adding a new
    event_type to macro_calendar.yaml without bouncing the process."""
    _check_auth(x_internal_secret)
    invalidate_calendar_cache()
    events = await load_calendar(force_refresh=True)
    return {
        "merged_event_count": len(events),
        "fred_calendar_cache": fred_calendar_cache_status(),
    }


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
    # Sectors / narrative may have changed — drop the inline-keyboard cache so
    # the next [💼 Hisseler] / [📊 Tarihsel] click reads the fresh state.
    try:
        from services.macro_callback import invalidate_event
        invalidate_event(event_id)
    except Exception:
        pass
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


@router.post("/user/tier")
async def set_user_tier(
    telegram_id: str = Query(..., description="User's telegram_id (string)"),
    tier: str = Query(..., description="free | premium | advance"),
    x_internal_secret: Optional[str] = Header(None),
):
    """Flip a user's subscription tier — used for ops + dev testing.
    Validates tier is one of the allowed values; idempotent UPDATE.
    """
    _check_auth(x_internal_secret)
    if tier not in ("free", "premium", "advance"):
        raise HTTPException(status_code=400, detail="tier must be free|premium|advance")
    sql = text(
        "UPDATE users SET tier = :tier WHERE telegram_id = :tid "
        "RETURNING id, telegram_id, username, tier"
    )
    async with engine.begin() as conn:
        row = (await conn.execute(sql, {"tier": tier, "tid": telegram_id})).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail=f"user {telegram_id} not found")
    return {
        "id": row["id"],
        "telegram_id": row["telegram_id"],
        "username": row["username"],
        "tier": row["tier"],
    }


@router.post("/reaction/{event_id:path}")
async def trigger_market_reaction(
    event_id: str,
    x_internal_secret: Optional[str] = Header(None),
):
    """Force-capture T+0 + T+5min market reaction for an existing release.
    Useful for: (a) backfilling a release that landed before this feature
    deployed, (b) debugging FMP wiring. Returns immediately — the actual
    capture runs as a background task and will write rows when complete.
    """
    _check_auth(x_internal_secret)
    from services.macro_market_reaction import trigger_reaction
    trigger_reaction(event_id)
    return {"event_id": event_id, "queued": True, "note": "T+0 immediate, T+5min after 300s"}


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


@router.post("/revision/broadcast/{event_id:path}")
async def trigger_revision_broadcast(
    event_id: str,
    force: bool = Query(False, description="Re-broadcast even if already stamped"),
    x_internal_secret: Optional[str] = Header(None),
):
    """Manual Advance-tier revision push. Useful for backfilling old
    revisions that landed before this wiring shipped, or re-pushing after
    a Telegram outage. Eşik altı revizyonlar `below_threshold` döner ve
    stamp'lenir; force=True ile eşik kontrolü atlanmaz (eşik tasarım
    kararı — gerçekten anlamlı revizyonu push'luyoruz).
    """
    _check_auth(x_internal_secret)
    return await broadcast_revision(event_id, force=force)


@router.post("/revision/simulate/{event_id:path}")
async def simulate_revision(
    event_id: str,
    new_actual_value: float = Query(..., description="Simulated new actual_value"),
    x_internal_secret: Optional[str] = Header(None),
):
    """Test/smoke endpoint — manually inject a revision audit row + fire
    broadcast without waiting for FRED to revise. Reads current actual_value,
    writes a macro_release_revisions audit, then triggers broadcast_revision.
    Used in non-prod / staging to verify the loop end-to-end.

    NOT FOR PRODUCTION ROUTINE USE — actual_value on macro_releases is NOT
    overwritten; this only seeds the audit table so the broadcast path can
    exercise.
    """
    _check_auth(x_internal_secret)
    from decimal import Decimal
    new_val = Decimal(str(new_actual_value))
    sql_read = text(
        "SELECT event_type, actual_value, source FROM macro_releases "
        "WHERE event_id = :eid"
    )
    sql_insert = text("""
        INSERT INTO macro_release_revisions
        (event_id, event_type, source, old_actual_value, new_actual_value,
         delta_abs, delta_pct)
        VALUES
        (:eid, :etype, :src, :old, :new, :delta_abs, :delta_pct)
        RETURNING id
    """)
    async with engine.begin() as conn:
        row = (await conn.execute(sql_read, {"eid": event_id})).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail=f"release {event_id} not found")
        old_val = row["actual_value"]
        if old_val is None:
            raise HTTPException(status_code=400, detail="release has no actual_value")
        delta_abs = new_val - old_val
        delta_pct = (delta_abs / abs(old_val) * Decimal("100")) if old_val != 0 else None
        ins = (await conn.execute(sql_insert, {
            "eid": event_id, "etype": row["event_type"], "src": row["source"],
            "old": old_val, "new": new_val,
            "delta_abs": delta_abs, "delta_pct": delta_pct,
        })).mappings().first()
    broadcast_result = await broadcast_revision(event_id, force=False)
    return {
        "simulated_revision_id": ins["id"] if ins else None,
        "old_actual_value": float(old_val),
        "new_actual_value": float(new_val),
        "delta_abs": float(delta_abs),
        "broadcast": broadcast_result,
    }


@router.post("/story/broadcast/{event_id:path}")
async def trigger_story_broadcast(
    event_id: str,
    tier: str = Query("premium", pattern="^(premium|advance)$"),
    force: bool = Query(False, description="Re-broadcast even if already stamped"),
    x_internal_secret: Optional[str] = Header(None),
):
    """Manual Telegram push for an existing storyteller output. Useful for:
    (a) re-sending after a Telegram outage,
    (b) backfilling old releases that landed before broadcast wiring,
    (c) testing tier targeting before a real release.

    Idempotency guard lives in broadcast_story (skips if
    `broadcasted_<tier>_at` already stamped); pass `force=true` to bypass.
    """
    _check_auth(x_internal_secret)
    return await broadcast_story(event_id, tier=tier, force=force)


@router.post("/sep")
async def upsert_sep(
    payload: dict = Body(...),
    x_internal_secret: Optional[str] = Header(None),
):
    """Manual SEP (Summary of Economic Projections) entry — FAZ B.

    Fed her 3 ayda bir FOMC toplantısının 4'ünde SEP yayımlar. Dot plot
    medianlarını payload data olarak macro_releases'a yazıyoruz; storyteller
    FOMC_STATEMENT için aynı released_at'te SEP rows varsa "Dot Plot şifti"
    bölümünü prompt'a ekler.

    Body örneği:
    ```json
    {
      "released_at": "2026-03-19T18:00:00Z",
      "end_year_0": 3.4,
      "end_year_1": 2.9,
      "end_year_2": 2.6,
      "longer_run": 2.5
    }
    ```

    Yazılan event_id'ler: `sep:FUNDS_END_0:<YYYY-MM-DD>`, vb. Idempotent
    (ON CONFLICT DO UPDATE actual_value).
    """
    _check_auth(x_internal_secret)
    released_at = payload.get("released_at")
    if not released_at:
        raise HTTPException(status_code=400, detail="released_at required")
    try:
        ts = datetime.fromisoformat(released_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="released_at parse failed (use ISO 8601)")
    date_key = ts.date().isoformat()

    mapping = {
        "end_year_0":   "SEP_FUNDS_END_0",
        "end_year_1":   "SEP_FUNDS_END_1",
        "end_year_2":   "SEP_FUNDS_END_2",
        "longer_run":   "SEP_FUNDS_LONGER_RUN",
    }
    upserts = []
    for body_key, event_type in mapping.items():
        v = payload.get(body_key)
        if v is None:
            continue
        try:
            actual = float(v)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{body_key} not numeric")
        eid = f"sep:{event_type[4:]}:{date_key}"  # sep:FUNDS_END_0:2026-03-19
        upserts.append({
            "event_id": eid, "event_type": event_type,
            "released_at": ts, "actual": actual,
        })

    if not upserts:
        raise HTTPException(status_code=400, detail="no median fields provided")

    sql = text("""
        INSERT INTO macro_releases
        (event_id, event_type, country, released_at, actual_value, source, source_url)
        VALUES (:event_id, :event_type, 'US', :released_at, :actual,
                'fed_sep_manual',
                'https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm')
        ON CONFLICT (event_id) DO UPDATE
        SET actual_value = EXCLUDED.actual_value,
            released_at = EXCLUDED.released_at
    """)
    written = 0
    async with engine.begin() as conn:
        for u in upserts:
            await conn.execute(sql, u)
            written += 1
    return {"upserted": written, "event_ids": [u["event_id"] for u in upserts]}


@router.post("/story/{event_id:path}")
async def force_generate_story(
    event_id: str,
    tier: str = Query("premium", pattern="^(premium|advance)$"),
    force: bool = Query(False, description="Overwrite existing story for this (event_id, tier)"),
    x_internal_secret: Optional[str] = Header(None),
):
    """Trigger the storyteller for one release + tier. Returns the validator
    report so we can see why a rejection happened.

    Free tier'a yazmıyor (zaten `macro_releases.narrative_md` Free hap'tır).
    Premium/Advance için ayrı çıktı `macro_stories` tablosuna gider, public
    `/macro/story/{event_id}` endpoint'i tier'a göre serve eder.
    """
    _check_auth(x_internal_secret)
    result = await generate_story(event_id, tier=tier, force=force)
    validator = result.validator
    return {
        "event_id": result.event_id,
        "tier": result.tier,
        "written": result.written,
        "rejection_reason": result.rejection_reason,
        "story_md_preview": (result.story_md[:300] + "…") if result.story_md and len(result.story_md) > 300 else result.story_md,
        "sources_cited": result.sources_cited,
        "validator": {
            "ok": validator.ok if validator else None,
            "word_count": validator.word_count if validator else None,
            "unknown_numbers": validator.unknown_numbers if validator else None,
            "numbers_without_citation": validator.numbers_without_citation if validator else None,
            "missing_temporal_marker": validator.missing_temporal_marker if validator else None,
            "banned_phrases_found": validator.banned_phrases_found if validator else None,
            "out_of_word_bounds": validator.out_of_word_bounds if validator else None,
            "unknown_sources": validator.unknown_sources if validator else None,
        } if validator else None,
    }


# ---------- Track Record / İsabet Skorboard (FAZ D) ----------

@router.post("/track-record/verdict/{event_id:path}")
async def track_record_set_verdict(
    event_id: str,
    tier: str = Query("premium", pattern="^(premium|advance)$"),
    event_type: str = Query(...),
    verdict: str = Query(..., description="bullish_btc|bearish_btc|hawkish_fed|dovish_fed|risk_on|risk_off|inflation_up|inflation_down|bullish_usd|bearish_usd|neutral"),
    horizon_days: int = Query(30, ge=1, le=180),
    notes: Optional[str] = Query(None),
    x_internal_secret: Optional[str] = Header(None),
):
    """Bir story için manuel verdict pre-populate et (validation öncesi).

    Auto-extract OFF iken (feature flag false) 3-5 Mart 2026 hikayesini
    elle review ederken kullanılır.
    """
    _check_auth(x_internal_secret)
    ok = await insert_manual_verdict(
        story_event_id=event_id, tier=tier, event_type=event_type,
        predicted_verdict=verdict, horizon_days=horizon_days, notes=notes,
    )
    return {"inserted": ok, "event_id": event_id, "tier": tier, "verdict": verdict}


@router.post("/track-record/validate/{event_id:path}")
async def track_record_validate(
    event_id: str,
    tier: str = Query("premium", pattern="^(premium|advance)$"),
    hit_score: float = Query(..., ge=-1.0, le=1.0, description="-1 miss / 0 neutral / +1 hit (fractional ok)"),
    validated_by: str = Query("admin"),
    notes: Optional[str] = Query(None),
    compared_event_id: Optional[str] = Query(None),
    x_internal_secret: Optional[str] = Header(None),
):
    """Manuel hit/miss validasyon. Önce /verdict endpoint ile verdict
    insert edilmiş olmalı."""
    _check_auth(x_internal_secret)
    ok = await validate_outcome_manually(
        story_event_id=event_id, tier=tier, hit_score=hit_score,
        validated_by=validated_by, notes=notes, compared_event_id=compared_event_id,
    )
    return {"updated": ok, "event_id": event_id, "tier": tier, "hit_score": hit_score}


@router.get("/track-record/list")
async def track_record_list(
    event_type: Optional[str] = Query(None),
    tier: Optional[str] = Query(None, pattern="^(premium|advance)$"),
    limit: int = Query(50, ge=1, le=200),
    x_internal_secret: Optional[str] = Header(None),
):
    _check_auth(x_internal_secret)
    rows = await list_outcomes(event_type=event_type, tier=tier, limit=limit)
    rate = await get_hit_rate(event_type=event_type, tier=tier, min_validated=1)
    return {"outcomes": rows, "aggregate": rate}
