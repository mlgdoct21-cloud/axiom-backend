"""
Health / Monitoring endpoints.

GET /api/v1/health/status  → Full pipeline health (Gemini models, loops, DB).
GET /api/v1/health/live    → Simple liveness probe (200 OK).

Production monitoring kancası — Railway/Uptimerobot/Grafana buradan okur.
"""
import time
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select

from core.database import get_db
from models.news import NewsItem
from services.crawler import pipeline_health
from services.ai_service import get_model_status

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def liveness() -> dict:
    """Liveness probe — uygulama ayakta mı?"""
    return {"status": "ok", "ts": time.time()}


@router.get("/status")
async def pipeline_status(db: AsyncSession = Depends(get_db)) -> dict:
    """
    Production pipeline status — tüm kritik metrics tek endpoint'te.

    Alt sistemler:
      - pipeline: fast_fetch / batch_analyze / digest loop'ları
      - gemini: primary/fallback model + circuit breaker durumu
      - db: toplam haber, analyzed/pending counts, son haber timestamp
    """
    now = time.time()

    # DB istatistikleri
    db_stats: dict = {}
    try:
        total = (await db.execute(select(func.count(NewsItem.id)))).scalar() or 0
        analyzed = (
            await db.execute(
                select(func.count(NewsItem.id)).where(NewsItem.analyzed == True)  # noqa: E712
            )
        ).scalar() or 0
        pending = total - analyzed

        last_news_ts = (
            await db.execute(
                select(func.max(NewsItem.created_at))
            )
        ).scalar()
        last_analyzed_row = (
            await db.execute(
                select(func.max(NewsItem.id)).where(NewsItem.analyzed == True)  # noqa: E712
            )
        ).scalar()

        db_stats = {
            "total_news": total,
            "analyzed": analyzed,
            "pending_analysis": pending,
            "last_news_at": last_news_ts.isoformat() if last_news_ts else None,
            "last_analyzed_id": last_analyzed_row,
        }
    except Exception as e:
        db_stats = {"error": str(e)}

    # Pipeline loop health (freshness — son kaç saniye önce çalıştı?)
    loops = {}
    for loop_name, data in pipeline_health.items():
        last_ts = data.get("last_run_ts", 0.0)
        loops[loop_name] = {
            **data,
            "seconds_since_last_run": int(now - last_ts) if last_ts else None,
            "healthy": (
                (now - last_ts) < 300  # 5 dk içinde çalıştıysa sağlıklı
                and data.get("consecutive_errors", 0) < 3
            ) if last_ts else False,
        }

    # Gemini model status (circuit breaker)
    gemini = get_model_status()

    # Overall health — tüm loop'lar healthy ve en az bir model cool ise OK
    all_loops_healthy = all(v.get("healthy", False) for v in loops.values())
    any_model_cool = (
        not gemini["cooldowns"].get(gemini["primary_model"], {}).get("cooling_down", False)
        or not gemini["cooldowns"].get(gemini["fallback_model"], {}).get("cooling_down", False)
    )

    return {
        "overall": "healthy" if (all_loops_healthy and any_model_cool) else "degraded",
        "ts": now,
        "pipeline": loops,
        "gemini": gemini,
        "db": db_stats,
    }
