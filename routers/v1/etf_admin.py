"""
Admin ETF cache router — manuel CoinGlass değerlerini cache'e yazma endpoint'i.

Kullanım: BOT_INTERNAL_SECRET header ile authenticate, gerçek CoinGlass
değerlerini cache'e yaz. Bitbo Apr 30 yayınlanmadan önce manual override için.
"""
import os
from typing import Optional
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from services.etf_flow_cache_service import save_etf_flow
from core.logger import get_logger

logger = get_logger("etf_admin")

router = APIRouter(prefix="/admin/etf", tags=["admin"])


class EtfFlowUpsert(BaseModel):
    symbol: str  # 'BTC' or 'ETH'
    net_flow_usd: float
    net_flow_coins: float
    source: str = "coinglass_manual"
    spot_price: Optional[float] = None
    total_aum_usd: Optional[float] = None
    total_holdings_coins: Optional[float] = None


@router.post("/cache")
async def upsert_etf_cache(
    payload: EtfFlowUpsert,
    x_internal_secret: Optional[str] = Header(None),
):
    """
    Manuel cache write. BOT_INTERNAL_SECRET ile auth.
    """
    expected = os.getenv("BOT_INTERNAL_SECRET", "").strip()
    if not expected or x_internal_secret != expected:
        raise HTTPException(status_code=403, detail="Forbidden")

    if payload.symbol not in ("BTC", "ETH"):
        raise HTTPException(status_code=400, detail="symbol must be BTC or ETH")

    new_id = await save_etf_flow(
        symbol=payload.symbol,
        net_flow_usd=payload.net_flow_usd,
        net_flow_coins=payload.net_flow_coins,
        source=payload.source,
        spot_price=payload.spot_price,
        total_aum_usd=payload.total_aum_usd,
        total_holdings_coins=payload.total_holdings_coins,
        raw_data={"manual_insert_at": datetime.now(timezone.utc).isoformat()},
    )

    if new_id is None:
        raise HTTPException(status_code=500, detail="Cache save failed")

    return {
        "ok": True,
        "id": new_id,
        "symbol": payload.symbol,
        "net_flow_usd": payload.net_flow_usd,
        "net_flow_coins": payload.net_flow_coins,
        "source": payload.source,
    }
