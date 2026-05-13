"""Daily digest endpoint - Breaking news + market insights özeti"""

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from services.daily_digest_service import DailyDigestService
from core.logger import get_logger

# Tüm digest payload'ı için sert üst-sınır. Service zaten per-task 5s sınırlı,
# ama route bu degraded fallback'i garanti ediyor → frontend en geç 8s'de yanıt alır.
_DIGEST_ROUTE_TIMEOUT = 8.0


def _degraded_payload(reason: str) -> dict:
    return {
        "risk_radar": {
            "title": "AXIOM RISK RADAR",
            "analysis": "Veri kaynağı şu an yavaş, kısa süre sonra tekrar deneyin.",
            "symbols": [],
            "color": "yellow",
        },
        "portfolio_signal": {
            "title": "PORTFÖY SINYAL",
            "recommendation": "Pazar verisi güncellenirken bekleyin.",
            "symbols": [],
            "color": "blue",
        },
        "vix": None,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "status": "degraded",
        "reason": reason,
    }

logger = get_logger("daily_digest_router")

router = APIRouter(
    prefix="/daily-digest",
    tags=["daily-digest"],
)


@router.get("")
async def get_daily_digest(db: AsyncSession = Depends(get_db)):
    """
    Günlük pazar özeti - Breaking news + urgent events.

    Döner:
    {
        "risk_radar": {
            "title": "AXIOM RISK RADAR",
            "analysis": "...",
            "symbols": ["BTC", "USD", ...],
            "color": "red|yellow|green"
        },
        "portfolio_signal": {
            "title": "PORTFÖY SINYAL",
            "recommendation": "...",
            "symbols": ["BTC", "USDC", ...],
            "color": "blue"
        },
        "vix": { "current": 18.4, "status": "...", "color": "yellow" },
        "last_updated": "2026-05-01T12:34:56.789Z"
    }

    Açıklama:
    - Risk Radar: VIX + BTC on-chain (netflow + funding) + Asya endeksleri + acil haber
      sentezi → frontend modal'ı verdict + faktör kartları + aksiyon önerisi gösterir.
    - Portföy Sinyalı: Top movers + ETF akışları.
    - Kantitatif kart kaldırıldı (Day 28 part 5) — sektör verisi MiniSectorChip + Risk
      Radar modal'ında, earnings sayısı MiniEarningsChip'te.
    """
    try:
        digest = await asyncio.wait_for(
            DailyDigestService.get_daily_digest(db),
            timeout=_DIGEST_ROUTE_TIMEOUT,
        )

        if "error" in digest:
            logger.warning("Digest generation partially failed, returning fallback")
            return {
                **digest,
                "status": "degraded",
            }

        return {
            **digest,
            "status": "ok",
        }

    except asyncio.TimeoutError:
        # Route-level safety net: service per-task timeout'ları yeterli olmalıydı,
        # buraya düşersek bir bağımlılık await beklenmedik şekilde uzadı demektir.
        logger.error(f"Daily digest route timeout (>{_DIGEST_ROUTE_TIMEOUT}s) — degraded fallback")
        return _degraded_payload("route_timeout")
    except Exception as e:
        logger.error(f"Daily digest endpoint error: {e}")
        return _degraded_payload(f"exception: {type(e).__name__}")
