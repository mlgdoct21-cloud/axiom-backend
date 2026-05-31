"""BIST public router — TL UFRS bilanço/gelir/nakit verisi (isyatirimhisse).

Dashboard `FundamentalTab.tsx` BIST modunda bu endpoint'i tüketir:
  GET /api/v1/bist/financials/{symbol}

Veri haftalık cron tarafından `bist_financials` tablosuna yazılır
(services/bist_financials_service.py · bist_financials_loop). Endpoint
sadece tablodan okur; canlı scrape YAPMAZ (rate-limit + IP-ban koruması).

Tier: PUBLIC. FMP fundamental-enrichment ile aynı seviye serbest
(yorum/strateji premium kalmaya devam ediyor; ham veri free).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Path, Query, Response

from core.logger import get_logger
from services.bist_financials_service import (
    get_symbol_financials,
    refresh_one_symbol,
    BIST_FINANCIALS_GROUP,
)
from services.market_detector import BIST_SYMBOLS

logger = get_logger("bist_router")

router = APIRouter(prefix="/bist", tags=["bist"])

# Bilanço çeyreklik güncellenir; dashboard refresh'i için makul cache.
_CACHE_HEADER = "public, max-age=900, stale-while-revalidate=3600"


@router.get("/symbols")
async def list_bist_symbols(response: Response) -> dict:
    """Takip ettiğimiz BIST sembolleri (BIST_SYMBOLS env)."""
    response.headers["Cache-Control"] = "public, max-age=3600"
    return {"symbols": list(BIST_SYMBOLS), "count": len(BIST_SYMBOLS)}


@router.get("/financials/{symbol}")
async def bist_financials(
    response: Response,
    symbol: str = Path(..., min_length=2, max_length=16, description="BIST sembolü, örn: ASELS"),
    group: str = Query(BIST_FINANCIALS_GROUP, description="Financial group: 1=XI_29, 2=UFRS, 3=UFRS_K"),
) -> dict:
    """Tek sembolün TL UFRS bilanço/gelir/nakit-akış long-format verisi.

    Boş döndüğünde frontend `items=[]` + `periods=[]` görür ve
    "Henüz veri yok — bir sonraki haftalık cron çalışmasını bekleyin"
    mesajı gösterir.
    """
    sym = symbol.upper().strip()
    if not sym.isalnum():
        raise HTTPException(status_code=400, detail="Geçersiz sembol formatı")
    response.headers["Cache-Control"] = _CACHE_HEADER
    data = await get_symbol_financials(sym, group=group)
    return data


@router.post("/financials/{symbol}/refresh")
async def refresh_bist_financials(
    symbol: str = Path(..., min_length=2, max_length=16),
) -> dict:
    """Admin/dev — tek sembolün canlı scrape + UPSERT'i. Senkron çalışır.

    Production'da haftalık cron yeterli; bu endpoint manual backfill +
    yeni sembol eklendiğinde anında doldurmak için. Auth eklenmedi —
    Railway ortamında dahili kullanım amaçlı (rate-limit dışarıda yok).
    """
    sym = symbol.upper().strip()
    if not sym.isalnum():
        raise HTTPException(status_code=400, detail="Geçersiz sembol formatı")
    written = await refresh_one_symbol(sym)
    return {"symbol": sym, "rows_written": written}
