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
from typing import Optional

from fastapi import APIRouter, Query, Header, HTTPException
from fastapi.responses import JSONResponse

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
