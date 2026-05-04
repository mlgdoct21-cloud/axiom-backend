"""
Public + admin routes for CryptoQuant on-chain intelligence.

Public:
  GET /api/v1/crypto/onchain?symbol=BTC — full snapshot

Admin (BOT_INTERNAL_SECRET):
  POST /api/v1/admin/crypto/alert-sweep    — force a threshold sweep
  POST /api/v1/admin/crypto/morning-briefing — fire briefing now
  POST /api/v1/admin/crypto/refresh        — bust cache + refresh
"""
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Query, Header, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import text

from core.database import engine
from services.cryptoquant_service import get_onchain_snapshot, _is_configured, refresh_all_metrics
from services.cryptoquant_alerts import sweep_and_dispatch, morning_briefing

router = APIRouter()


@router.get("/crypto/onchain")
async def onchain_snapshot(symbol: str = Query(default="BTC", max_length=10)):
    symbol = symbol.upper().strip()
    if not _is_configured():
        return JSONResponse(
            status_code=503,
            content={"error": "cryptoquant_not_configured", "symbol": symbol},
        )
    data = await get_onchain_snapshot(symbol)
    return JSONResponse(content=data)


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


def _check_auth(x_internal_secret: Optional[str]) -> None:
    expected = os.getenv("BOT_INTERNAL_SECRET", "").strip()
    if not expected or x_internal_secret != expected:
        raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/admin/crypto/alert-sweep")
async def admin_alert_sweep(x_internal_secret: Optional[str] = Header(None)):
    _check_auth(x_internal_secret)
    return await sweep_and_dispatch()


@router.post("/admin/crypto/morning-briefing")
async def admin_morning_briefing(x_internal_secret: Optional[str] = Header(None)):
    _check_auth(x_internal_secret)
    return await morning_briefing()


@router.post("/admin/crypto/refresh")
async def admin_refresh(x_internal_secret: Optional[str] = Header(None)):
    _check_auth(x_internal_secret)
    await refresh_all_metrics()
    return {"status": "refreshed"}
