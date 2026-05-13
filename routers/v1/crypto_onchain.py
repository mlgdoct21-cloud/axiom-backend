"""
Public + admin routes for CryptoQuant on-chain intelligence.

Public:
  GET /api/v1/crypto/onchain?symbol=BTC — full snapshot

Admin (BOT_INTERNAL_SECRET):
  POST /api/v1/admin/crypto/alert-sweep    — force a threshold sweep
  POST /api/v1/admin/crypto/morning-briefing — fire briefing now
  POST /api/v1/admin/crypto/refresh        — bust cache + refresh
"""
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Query, Header, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import text

from core.database import engine
from core.logger import get_logger
from core.security import assert_internal_secret
from services.cryptoquant_service import (
    get_onchain_snapshot,
    _is_configured,
    refresh_all_metrics,
    _cache_get_any,
)

logger = get_logger("crypto_onchain_router")

# Route-seviye sert üst-sınır. Service `get_onchain_snapshot` zaten 10s sınırlı
# build + stale fallback yapıyor; bu route timeout sadece beklenmedik bir await
# blokajına karşı son katman (defansif). Frontend en geç 12s'de yanıt alır.
_ONCHAIN_ROUTE_TIMEOUT = 12.0
from services.cryptoquant_alerts import sweep_and_dispatch, morning_briefing
from services.cryptoquant_market import (
    get_erc20_radar,
    get_stablecoin_pulse,
    get_altseason_score,
    refresh_market_metrics,
)
from services.onchain_storyteller import get_onchain_story, refresh_story

router = APIRouter()


@router.get("/crypto/onchain")
async def onchain_snapshot(symbol: str = Query(default="BTC", max_length=10)):
    symbol = symbol.upper().strip()
    if not _is_configured():
        return JSONResponse(
            status_code=503,
            content={"error": "cryptoquant_not_configured", "symbol": symbol},
        )
    try:
        data = await asyncio.wait_for(
            get_onchain_snapshot(symbol),
            timeout=_ONCHAIN_ROUTE_TIMEOUT,
        )
        return JSONResponse(content=data)
    except asyncio.TimeoutError:
        logger.error(f"onchain route timeout (>{_ONCHAIN_ROUTE_TIMEOUT}s) — {symbol}, stale fallback")
        stale = await _cache_get_any("snapshot", symbol, "day")
        if stale:
            return JSONResponse(content={**stale, "_stale": True, "_reason": "route_timeout"})
        return JSONResponse(
            status_code=503,
            content={"error": "snapshot_unavailable", "symbol": symbol, "_reason": "route_timeout"},
        )
    except Exception as e:
        logger.error(f"onchain route error {symbol}: {e}")
        stale = await _cache_get_any("snapshot", symbol, "day")
        if stale:
            return JSONResponse(content={**stale, "_stale": True, "_reason": str(type(e).__name__)})
        return JSONResponse(
            status_code=503,
            content={"error": "snapshot_unavailable", "symbol": symbol, "_reason": str(type(e).__name__)},
        )


@router.get("/crypto/score-history")
async def score_history(
    symbol: str = Query(default="BTC", max_length=10),
    days: int = Query(default=90, ge=7, le=365),
):
    """Daily Axiom Score snapshots for the sparkline widget.

    Returns up to `days` rows ascending by date. One row per (symbol, day);
    morning briefing upserts via _record_score_snapshot.
    """
    sym = symbol.upper().strip()
    sql = text("""
        SELECT recorded_date, score, score_zone, recorded_at
        FROM axiom_score_history
        WHERE symbol = :sym
          AND recorded_date >= ((NOW() AT TIME ZONE 'UTC')::date - make_interval(days => :days))
        ORDER BY recorded_date ASC
    """)
    try:
        async with engine.begin() as conn:
            rows = (await conn.execute(sql, {"sym": sym, "days": days})).fetchall()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

    items = [
        {
            "date": r[0].isoformat(),
            "score": float(r[1]),
            "zone": r[2],
            "recorded_at": r[3].isoformat(),
        }
        for r in rows
    ]
    return JSONResponse(
        content={"symbol": sym, "days": days, "count": len(items), "items": items},
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/crypto/alerts/history")
async def alert_history(days: int = Query(default=7, ge=1, le=30)):
    """Returns the last N days of fired alerts (all users aggregated, no PII).
    Used by the 'Son 7 Gün Alarmlar' dashboard widget — public, surfaces what
    the system has been signaling so prospective users see real activity.
    """
    sql = text("""
        SELECT alert_key, severity, title, sent_at, sent_date
        FROM cryptoquant_alert_log
        WHERE sent_at > NOW() - make_interval(days => :days)
        ORDER BY sent_at DESC
        LIMIT 100
    """)
    try:
        async with engine.begin() as conn:
            rows = (await conn.execute(sql, {"days": days})).fetchall()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

    # De-duplicate by (alert_key, sent_date) so 5 user-fan-outs of the same
    # alert show as one entry — what we care about is "what fired", not
    # "to how many users". Latest sent_at wins per group.
    seen: dict = {}
    for r in rows:
        key = (r[0], r[4].isoformat())
        if key not in seen:
            seen[key] = {
                "alert_key": r[0],
                "severity": r[1],
                "title": r[2],
                "sent_at": r[3].isoformat(),
                "sent_date": r[4].isoformat(),
                "fanout_count": 1,
            }
        else:
            seen[key]["fanout_count"] += 1

    items = list(seen.values())
    return JSONResponse(
        content={"days": days, "count": len(items), "items": items},
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.post("/admin/crypto/alert-sweep")
async def admin_alert_sweep(x_internal_secret: Optional[str] = Header(None)):
    assert_internal_secret(x_internal_secret)
    return await sweep_and_dispatch()


@router.post("/admin/crypto/morning-briefing")
async def admin_morning_briefing(x_internal_secret: Optional[str] = Header(None)):
    assert_internal_secret(x_internal_secret)
    return await morning_briefing()


@router.post("/admin/crypto/refresh")
async def admin_refresh(x_internal_secret: Optional[str] = Header(None)):
    assert_internal_secret(x_internal_secret)
    await refresh_all_metrics()
    await refresh_market_metrics()
    return {"status": "refreshed"}


# ── Market-Wide Endpoints ─────────────────────────────────────────────────

@router.get("/market/erc20-radar")
async def market_erc20_radar():
    """9 ERC20 DeFi tokenı için akıllı para hareketi (netflow + reserve)."""
    if not _is_configured():
        return JSONResponse(
            status_code=503,
            content={"error": "cryptoquant_not_configured"},
        )
    data = await get_erc20_radar()
    return JSONResponse(
        content=data,
        headers={"Cache-Control": "public, max-age=900"},
    )


@router.get("/market/stablecoin-pulse")
async def market_stablecoin_pulse():
    """USDC + DAI borsa akış nabzı + SSR proxy ('kuru barut' göstergesi)."""
    if not _is_configured():
        return JSONResponse(
            status_code=503,
            content={"error": "cryptoquant_not_configured"},
        )
    data = await get_stablecoin_pulse()
    return JSONResponse(
        content=data,
        headers={"Cache-Control": "public, max-age=900"},
    )


@router.get("/market/altseason")
async def market_altseason():
    """5 girdili Alt Sezon Composite skoru (0-100)."""
    if not _is_configured():
        return JSONResponse(
            status_code=503,
            content={"error": "cryptoquant_not_configured"},
        )
    data = await get_altseason_score()
    return JSONResponse(
        content=data,
        headers={"Cache-Control": "public, max-age=900"},
    )


@router.post("/admin/market/refresh")
async def admin_market_refresh(x_internal_secret: Optional[str] = Header(None)):
    assert_internal_secret(x_internal_secret)
    await refresh_market_metrics()
    return {"status": "market_refreshed"}


# ── Crypto Intel Storyteller ──────────────────────────────────────────────

@router.get("/market/intel-story")
async def market_intel_story(
    tab: str = Query(..., pattern="^(overview|erc20|stable)$"),
    tier: str = Query(default="premium", pattern="^(premium|advance)$"),
):
    """Crypto Intel Storyteller — Pusula/ERC20/Stablecoin tab'ları için
    Gemini-üretimi market commentary + deterministic action box.

    Scheduler 6 saatte bir pre-generate eder; bu endpoint cache okur.
    Premium = ~45-75 kelime narrative + action box; Advance = ~90-140 kelime + action box.
    Free tier UI'de bu endpoint çağrılmaz (gating frontend tarafında).
    """
    from services.crypto_intel_storyteller import get_intel_story
    result = await get_intel_story(tab, tier)  # type: ignore[arg-type]
    if not result:
        return JSONResponse(
            status_code=204,  # No Content — scheduler henüz doldurmadı
            content=None,
        )
    return JSONResponse(
        content={
            "tab": result.tab,
            "tier": result.tier,
            "story_md": result.story_md,
            "action_box": result.action_box,
            "generated_at": result.generated_at.isoformat() if result.generated_at else None,
            "source_snapshot": result.source_snapshot,
        },
        headers={"Cache-Control": "public, max-age=900, stale-while-revalidate=3600"},
    )


@router.post("/admin/market/intel-story-refresh")
async def admin_intel_story_refresh(x_internal_secret: Optional[str] = Header(None)):
    """Tüm intel story'leri zorla yeniden üret (scheduler'ı beklemeden)."""
    assert_internal_secret(x_internal_secret)
    from services.crypto_intel_storyteller import refresh_all_intel_stories
    return await refresh_all_intel_stories()


# ── On-Chain Storyteller ─────────────────────────────────────────────────

@router.get("/crypto/onchain-story")
async def onchain_story(symbol: str = Query(default="BTC", max_length=10)):
    """Gemini ile üretilmiş on-chain hikâye (12h cache). BTC/ETH/XRP."""
    sym = symbol.upper().strip()
    data = await get_onchain_story(sym)
    status = 200
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        if err == "cryptoquant_not_configured":
            status = 503
        elif err == "symbol_not_supported":
            status = 400
        elif err in ("snapshot_unavailable", "no_signals", "story_generation_failed"):
            status = 502
    headers = (
        {"Cache-Control": "public, max-age=3600, stale-while-revalidate=21600"}
        if status == 200 else {}
    )
    return JSONResponse(content=data, status_code=status, headers=headers)


@router.post("/admin/crypto/story-refresh")
async def admin_story_refresh(
    symbol: str = Query(default="BTC", max_length=10),
    x_internal_secret: Optional[str] = Header(None),
):
    assert_internal_secret(x_internal_secret)
    return await refresh_story(symbol.upper().strip())
