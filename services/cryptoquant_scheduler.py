"""
CryptoQuant Scheduler — background refresh + alert + briefing supervisor.

Four concurrent loops:
  1. Cache refresh (BTC/ETH/XRP) — every 4h
  2. Market metrics (ERC20/Stablecoin/AltSeason) — every 6h, sequential w/ 30s
     gap per-token so CryptoQuant Pro rate-limit stays comfortable. Endpoints
     run cache-only so HTTP traffic never triggers fetches.
  3. Alert sweep — every 30min (threshold breach → push to premium+)
  4. Morning briefing — daily at 06:00 UTC = 09:00 TR
"""
import asyncio
from datetime import datetime, timezone, timedelta

from core.logger import get_logger
from services.cryptoquant_service import refresh_all_metrics, _is_configured
from services.cryptoquant_alerts import sweep_and_dispatch, morning_briefing
from services.cryptoquant_market import refresh_market_metrics

logger = get_logger("cryptoquant_scheduler")

_REFRESH_INTERVAL = timedelta(hours=4)
_MARKET_REFRESH_INTERVAL = timedelta(hours=6)
_ALERT_SWEEP_INTERVAL = timedelta(minutes=30)
_BRIEFING_HOUR_UTC = 6  # 09:00 İstanbul time


async def _refresh_loop() -> None:
    """Cache refresh every 4h."""
    try:
        await refresh_all_metrics()
    except Exception as e:
        logger.error(f"CryptoQuant startup fetch error: {e}")

    while True:
        try:
            await asyncio.sleep(_REFRESH_INTERVAL.total_seconds())
            await refresh_all_metrics()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"CryptoQuant refresh loop error: {e}. Retrying in 15min.")
            await asyncio.sleep(900)


async def _market_loop() -> None:
    """Market-wide cache refresh every 6h (ERC20 radar + stablecoin pulse +
    altseason). 9 ERC20 tokens × 3 endpoints × 30s gap ≈ 14 min cold; the 6h
    cadence gives 4 refreshes/day with plenty of headroom."""
    # 90s grace period at startup so the BTC/ETH refresh has the rate-limit
    # budget first; market-wide is lower priority.
    await asyncio.sleep(90)
    try:
        await refresh_market_metrics()
    except Exception as e:
        logger.error(f"CryptoQuant market startup refresh error: {e}")

    while True:
        try:
            await asyncio.sleep(_MARKET_REFRESH_INTERVAL.total_seconds())
            await refresh_market_metrics()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"CryptoQuant market loop error: {e}. Retrying in 30min.")
            await asyncio.sleep(1800)


async def _alert_loop() -> None:
    """Threshold alert sweep every 30min."""
    # 60s grace period at startup so refresh has a chance to populate cache
    await asyncio.sleep(60)
    while True:
        try:
            await sweep_and_dispatch()
            await asyncio.sleep(_ALERT_SWEEP_INTERVAL.total_seconds())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"CryptoQuant alert loop error: {e}. Retrying in 5min.")
            await asyncio.sleep(300)


async def _briefing_loop() -> None:
    """Send morning briefing daily at 06:00 UTC. Sleeps until next 06:00."""
    while True:
        try:
            now = datetime.now(timezone.utc)
            target = now.replace(hour=_BRIEFING_HOUR_UTC, minute=0, second=0, microsecond=0)
            if now >= target:
                target = target + timedelta(days=1)
            wait_seconds = (target - now).total_seconds()
            logger.info(f"Morning briefing scheduled in {wait_seconds/3600:.1f}h ({target.isoformat()})")
            await asyncio.sleep(wait_seconds)
            await morning_briefing()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Morning briefing loop error: {e}. Retrying in 1h.")
            await asyncio.sleep(3600)


async def cryptoquant_supervisor() -> None:
    if not _is_configured():
        logger.info("CryptoQuant API key not set — scheduler idle.")
        return

    logger.info("CryptoQuant scheduler starting (refresh + alerts + briefing)...")

    try:
        await asyncio.gather(
            _refresh_loop(),
            _market_loop(),
            _alert_loop(),
            _briefing_loop(),
        )
    except asyncio.CancelledError:
        logger.info("CryptoQuant scheduler cancelled.")
        raise
