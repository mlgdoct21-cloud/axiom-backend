"""
GET /api/v1/crypto/onchain?symbol=BTC

Returns CryptoQuant on-chain snapshot (exchange flows, whale ratio,
miner pressure, stablecoin inflow, derivatives sentiment, cycle indicators).
Served from cache; background scheduler refreshes every 4h.
"""
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from services.cryptoquant_service import get_onchain_snapshot, _is_configured

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
