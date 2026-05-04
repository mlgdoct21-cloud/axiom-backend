"""
CryptoQuant Scheduler — background refresh loop.

Schedule:
  - Startup: immediate fetch if cache is stale (> 3h old)
  - Every 4 hours thereafter (exchange flow data refreshes daily,
    but funding rates are 30-min, so we do a full refresh every 4h)
"""
import asyncio
from datetime import datetime, timezone, timedelta

from core.logger import get_logger
from services.cryptoquant_service import refresh_all_metrics, _is_configured

logger = get_logger("cryptoquant_scheduler")

_REFRESH_INTERVAL = timedelta(hours=4)
_STARTUP_THRESHOLD = timedelta(hours=3)


async def cryptoquant_supervisor() -> None:
    if not _is_configured():
        logger.info("CryptoQuant API key not set — scheduler idle.")
        return

    logger.info("CryptoQuant scheduler starting...")

    # Startup fetch
    try:
        await refresh_all_metrics()
    except Exception as e:
        logger.error(f"CryptoQuant startup fetch error: {e}")

    while True:
        try:
            await asyncio.sleep(_REFRESH_INTERVAL.total_seconds())
            await refresh_all_metrics()
        except asyncio.CancelledError:
            logger.info("CryptoQuant scheduler cancelled.")
            break
        except Exception as e:
            logger.error(f"CryptoQuant scheduler error: {e}. Retrying in 15min.")
            await asyncio.sleep(900)
