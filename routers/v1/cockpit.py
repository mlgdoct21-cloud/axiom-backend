"""
Cockpit Router — Faz A.3 σ-sapma (rolling z-score) endpoint.

Dashboard'daki Kokpit chip'leri (Netflow, Funding, BTC fiyat) için 90g
rolling z-score döner. Kullanıcı tier'ına bakmaz — tarihsel istatistik
herkesin görebileceği bir bilgi (sadece Premium chip içerikleri zaten
backend tarafında ayrıca gate ediliyor).

GET /api/v1/cockpit/sigmas
    → {
        "symbol": "BTC",
        "window_days": 90,
        "netflow":   { "sigma": -1.84, "current": -2340, "mean_90d": 120, ... },
        "funding":   { "sigma": 0.42,  "current": 0.034, ... },
        "btc_price": { "sigma": 1.13,  "current_return_pct": 2.34, ... },
        "fetched_at": "2026-05-21T..."
      }

Cache: services/cockpit_sigmas.py içinde 1 saatlik TTL ile postgres'te.
Endpoint cevabı ek olarak Next.js edge'inde 5 dakika cache'lenir.
"""

import logging
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from services.cockpit_sigmas import get_all_sigmas
from services.cockpit_cvd import compute_cvd

logger = logging.getLogger("cockpit_router")
router = APIRouter()


@router.get("/cockpit/sigmas")
async def cockpit_sigmas():
    """
    Tüm Kokpit chip'leri için 90g rolling z-score.

    Response 200 daima — eksik metrik için ilgili alan null döner
    (frontend fallback "demo" rozeti gösterir).
    """
    try:
        payload = await get_all_sigmas()
        return JSONResponse(
            content=payload,
            headers={
                # CDN/Vercel edge cache 5 dakika
                "Cache-Control": "public, max-age=300, s-maxage=300, stale-while-revalidate=600",
            },
        )
    except Exception as e:
        logger.error(f"cockpit_sigmas error: {e}", exc_info=True)
        return JSONResponse(
            status_code=200,  # 500 değil — frontend "demo" rozetine düşmesi yeterli
            content={
                "symbol": "BTC",
                "window_days": 90,
                "netflow": None,
                "funding": None,
                "btc_price": None,
                "error": str(type(e).__name__),
            },
        )


@router.get("/cockpit/cvd")
async def cockpit_cvd():
    """
    BTC 24h CVD (Cumulative Volume Delta) — Binance public klines.
    On-Chain panel'in "Türev Piyasası" bölümünde gösterilir.
    """
    try:
        data = await compute_cvd()
        if data is None:
            return JSONResponse(content={"error": "no_data"}, status_code=200)
        return JSONResponse(
            content=data,
            headers={"Cache-Control": "public, max-age=300, s-maxage=300, stale-while-revalidate=600"},
        )
    except Exception as e:
        logger.error(f"cockpit_cvd error: {e}", exc_info=True)
        return JSONResponse(content={"error": str(type(e).__name__)}, status_code=200)
