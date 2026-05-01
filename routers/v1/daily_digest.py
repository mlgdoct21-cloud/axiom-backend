"""Daily digest endpoint - Breaking news + market insights özeti"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from services.daily_digest_service import DailyDigestService
from core.logger import get_logger

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
        "quant_analysis": {
            "title": "AXIOM KANTITATIF ANALIZ",
            "trigger": "...",
            "symbols": ["AAPL", "ETH", ...],
            "color": "green|yellow|red"
        },
        "portfolio_signal": {
            "title": "PORTFÖY SINYAL",
            "recommendation": "...",
            "symbols": ["BTC", "USDC", ...],
            "color": "blue"
        },
        "last_updated": "2026-05-01T12:34:56.789Z"
    }

    Açıklama:
    - Risk Radar: Makro riskler, Fed kararları, volatilite
    - Kantitatif Analiz: Teknik sinyaller, momentum indicators
    - Portföy Sinyalı: Önerilen pozisyonlar, entry/exit points
    """
    try:
        digest = await DailyDigestService.get_daily_digest(db)

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

    except Exception as e:
        logger.error(f"Daily digest endpoint error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate daily digest",
        )
