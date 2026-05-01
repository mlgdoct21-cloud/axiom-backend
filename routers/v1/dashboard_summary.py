"""
Dashboard Summary endpoint — 6 panel data tek endpoint'te.

GET /api/v1/dashboard-summary
  → overnight_markets, etf_flows, economic_calendar,
    premarket_movers, earnings_today, sector_performance

In-memory cache: 5 dk TTL (Gemini-free olduğu için ucuz, sadece FMP rate limit'i koruyor).
"""
import time
from typing import Optional, Dict, Any
from fastapi import APIRouter, HTTPException, status

from services.market_summary_service import get_full_dashboard_summary
from core.logger import get_logger

logger = get_logger("dashboard_summary_router")

router = APIRouter(
    prefix="/dashboard-summary",
    tags=["dashboard-summary"],
)

# ─── In-memory cache ──────────────────────────────────────────────────────
_cache: Dict[str, Any] = {"data": None, "expires_at": 0.0}
CACHE_TTL_SEC = 300  # 5 dk


@router.get("")
async def fetch_dashboard_summary(force_refresh: bool = False):
    """
    6-panel dashboard summary.

    Args:
        force_refresh: True ise cache bypass — fresh fetch zorla.

    Response:
    {
        "overnight_markets": {"asia": [...], "europe": [...], "us_futures": [...]},
        "etf_flows": {"btc": {...}, "eth": {...}},
        "economic_calendar": [...],
        "premarket_movers": {"gainers": [...], "losers": [...], "actives": [...]},
        "earnings_today": [...],
        "sector_performance": [...],
        "last_updated": "...",
        "cache_status": "hit" | "miss"
    }
    """
    now = time.time()

    # Cache hit?
    if not force_refresh and _cache["data"] is not None and now < _cache["expires_at"]:
        cached = dict(_cache["data"])
        cached["cache_status"] = "hit"
        cached["cache_age_sec"] = int(now - (_cache["expires_at"] - CACHE_TTL_SEC))
        return cached

    # Cache miss — fresh fetch
    try:
        data = await get_full_dashboard_summary()
        _cache["data"] = data
        _cache["expires_at"] = now + CACHE_TTL_SEC

        response = dict(data)
        response["cache_status"] = "miss"
        response["cache_age_sec"] = 0
        return response

    except Exception as e:
        logger.error(f"Dashboard summary endpoint error: {e}")
        # Eski cache hâlâ varsa onu döndür (degraded mode)
        if _cache["data"] is not None:
            stale = dict(_cache["data"])
            stale["cache_status"] = "stale"
            stale["error"] = str(e)
            return stale
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Dashboard summary geçici olarak kullanılamıyor.",
        )
